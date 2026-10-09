"""模型测试并发执行器：逐账号并行发起一次真实模型调用并汇总。

设计要点：账号间严格隔离（一个账号失败绝不波及其它）、并发执行但结果按请求顺序
返回、不做 Key 池轮换（每账号固定第一把 Key）、错误分类复用 client.py 的
口径（invalid/retry/model/fatal，网络异常归 network），凭据不回显且错误消息
截断到 DETAIL_LIMIT。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .client import StRotator, classify, extract_error, safe_text
from .config import AccountConfig, Config
from .transport import HttpClient, NetworkError

#: 错误信息最长保留多少字符（SSE 面板展示与日志都够用）
DETAIL_LIMIT = 200
#: 支持的推理档位；"none" 也要原样下发——它用于关闭上游的思考，不是空值
REASONING_EFFORT_VALUES = ("none", "low", "medium", "high")
#: 上游补全端点路径（与 client.py 一致，不带前导斜杠）
UPSTREAM_PATH = "chat/completions"

#: 客户端工厂：调用一次得到一个新的 HttpClient（每个账号独立连接，互不串扰）
ClientFactory = Callable[[], HttpClient]
#: SSE 事件回调：worker 线程内被调用，透传一个事件 dict
Emitter = Callable[[dict[str, Any]], None]


@dataclass
class ModelTestRequest:
    """一次模型测试请求的输入（reasoning_effort 取 REASONING_EFFORT_VALUES 或 None）。"""

    model: str
    accounts: Sequence[str]
    prompt: str
    reasoning_effort: str | None = None  # 必须是 REASONING_EFFORT_VALUES 之一，或 None
    stream: bool = False


@dataclass
class ModelTestResult:
    """单个账号的测试结果；error 为 ``{"code":..., "message":...}``（message ≤ DETAIL_LIMIT）。"""

    account: str
    status: str  # "ok" | "error"
    latency_ms: int
    text: str = ""
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None  # {"code":..., "message":...}


def payload_for(
    model: str, prompt: str, reasoning_effort: str | None
) -> dict[str, Any]:
    """构造补全载荷；``reasoning_effort`` 仅在非空字符串时写入（"none" 也要下发）。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if reasoning_effort is not None and reasoning_effort != "":
        payload["reasoning_effort"] = reasoning_effort
    return payload


def build_client_factory(config: Config) -> ClientFactory:
    """构造 HttpClient 工厂（参数与 client.py:344 的构建完全一致）。"""

    def _factory() -> HttpClient:
        return HttpClient(
            config.base_url,
            timeout=config.timeout,
            connect_timeout=config.connect_timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "st-rotator/1.0",
                **config.extra_headers,
            },
            max_connections=config.max_connections,
            max_keepalive=config.max_keepalive,
        )

    return _factory


#: 默认工厂（模块级属性，测试与代理可整体替换成假工厂）
DEFAULT_CLIENT_FACTORY = build_client_factory


def _noop(_event: dict[str, Any]) -> None:
    """未提供 emit 时的空实现。"""


def _failure(
    name: str, started: float, code: str, message: str, emit: Emitter
) -> ModelTestResult:
    """构造错误结果：消息截断到 DETAIL_LIMIT，并发出 error 事件。"""
    latency_ms = round((time.monotonic() - started) * 1000)
    message = (message or "")[:DETAIL_LIMIT]
    emit({"type": "error", "account": name, "code": code, "message": message})
    return ModelTestResult(
        account=name,
        status="error",
        latency_ms=latency_ms,
        error={"code": code, "message": message},
    )


def _http_error(
    name: str, started: float, resp: Any, body_text: str, emit: Emitter
) -> ModelTestResult:
    """把 HTTP 错误归类成结果：code ∈ {invalid, retry, model}，否则 fatal。"""
    action, _retry_after = classify(resp, body_text)
    code = action if action in ("invalid", "retry", "model") else "fatal"
    message = extract_error(body_text) or f"HTTP {resp.status}"
    return _failure(name, started, code, message, emit)


def _run_non_stream(
    client: Any,
    payload: dict[str, Any],
    headers: dict[str, str],
    name: str,
    started: float,
    emit: Emitter,
) -> ModelTestResult:
    resp = client.request(
        "POST", UPSTREAM_PATH, json_body=payload, headers=headers, stream=False
    )
    if resp.status >= 400:
        return _http_error(name, started, resp, safe_text(resp), emit)
    body = resp.json()
    choices = body.get("choices") or []
    message = (choices[0] if choices else {}).get("message") or {}
    latency_ms = round((time.monotonic() - started) * 1000)
    return ModelTestResult(
        account=name,
        status="ok",
        latency_ms=latency_ms,
        text=message.get("content") or "",
        usage=body.get("usage"),
    )


def _reasoning_text(chunk: Any) -> str:
    """取出流式分片里的思考内容（``delta.reasoning_content``，与正文分离）。"""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = (choices[0] or {}).get("delta") or {}
    return delta.get("reasoning_content") or ""


def _run_stream(
    req: ModelTestRequest,
    client: Any,
    payload: dict[str, Any],
    headers: dict[str, str],
    name: str,
    started: float,
    emit: Emitter,
) -> ModelTestResult:
    resp = client.request(
        "POST", UPSTREAM_PATH, json_body=payload, headers=headers, stream=True
    )
    if resp.status >= 400:
        return _http_error(name, started, resp, safe_text(resp), emit)
    emit({"type": "start", "account": name, "model": req.model})
    pieces: list[str] = []
    for chunk in StRotator._iter_sse_chunks(resp):
        reasoning = _reasoning_text(chunk)
        if reasoning:
            emit({"type": "reasoning", "account": name, "text": reasoning})
        piece = StRotator._chunk_text(chunk)
        if piece:
            pieces.append(piece)
            emit({"type": "token", "account": name, "token": piece})
        usage = chunk.get("usage")
        if usage:
            emit({"type": "usage", "account": name, "usage": usage})
    latency_ms = round((time.monotonic() - started) * 1000)
    emit({"type": "done", "account": name, "status": "ok", "latency_ms": latency_ms})
    return ModelTestResult(
        account=name, status="ok", latency_ms=latency_ms, text="".join(pieces)
    )


def _run_one(
    req: ModelTestRequest,
    factory: ClientFactory,
    emit: Emitter,
    account: AccountConfig | None,
    name: str,
) -> ModelTestResult:
    """跑一个账号的测试；account 为 None 表示账号不存在。所有异常只在本函数内隔离。"""
    started = time.monotonic()
    if account is None:
        return _failure(name, started, "fatal", f"账号不存在：{name}", emit)
    if not account.api_keys:
        return _failure(name, started, "fatal", f"{name} 无可用 Key", emit)
    key = account.api_keys[0]
    try:
        client = factory()
        payload = payload_for(req.model, req.prompt, req.reasoning_effort)
        headers = {"Authorization": f"Bearer {key}"}
        if req.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            return _run_stream(req, client, payload, headers, name, started, emit)
        return _run_non_stream(client, payload, headers, name, started, emit)
    except NetworkError as exc:
        return _failure(name, started, "network", str(exc), emit)
    except OSError as exc:
        # 底层 socket 失败同样视为网络问题
        return _failure(name, started, "network", str(exc), emit)
    except Exception as exc:  # noqa: BLE001 - emit 抛错（客户端断开）也要能记下结果
        return _failure(name, started, "fatal", str(exc), emit)


def run_test(
    config: Config,
    req: ModelTestRequest,
    *,
    factory: ClientFactory | None = None,
    emit: Emitter | None = None,
    max_workers: int = 8,
) -> list[ModelTestResult]:
    """并发测试，按请求顺序返回每账号结果；有 emit 时最后发 complete 事件。"""
    client_factory = factory if factory is not None else DEFAULT_CLIENT_FACTORY(config)
    emit_call = emit if emit is not None else _noop
    accounts_by_name = {acct.name: acct for acct in config.accounts}
    names = list(dict.fromkeys(req.accounts))
    workers = max(1, min(int(max_workers), len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda name: _run_one(
                    req,
                    client_factory,
                    emit_call,
                    accounts_by_name.get(name),
                    name,
                ),
                names,
            )
        )
    if emit is not None:
        emit({"type": "complete"})
    return results
