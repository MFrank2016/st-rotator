"""本地控制台：网关的图形界面后端。

定位
----
把「命令行 + 手改 config.json」变成「打开一个窗口就能看、能点」。具体提供：

* 运行状态总览（Key 池、限速、吞吐、运行时长）
* Key 的增 / 删 / 单把体检 —— 不用再手改配置文件
* 模型选择（清单从上游 ``/models`` 实时拉，带缓存）
* 网关接入信息 + 各语言可直接复制的接入片段
* 实时日志（内存环形缓冲，游标增量拉取，不重复传输）

架构
----
控制台不是独立进程，而是**挂在同一个网关进程上的路由**：

    Edge(app 模式窗口) ──► http://127.0.0.1:8080/  ──► ConsoleState（读状态 / 改配置）
                                     │
                                     └─► /v1/*  ──► 轮换池 ──► 商汤

好处是一个端口同时提供「给上层应用用的 API」和「给人看的界面」，不需要第二个服务、
不需要额外的进程管理。改动立刻生效，因为改的就是当前正在跑的这份内存配置。

安全边界
--------
* 只监听 127.0.0.1（由 CLI 保证），不对外暴露。
* 若网关设了 ``--token``，``/api/*`` 一律要求 Bearer 鉴权；页面本身是空壳，不含密钥，
  所以可以免鉴权加载，Token 由用户在前端输入后存 localStorage。
* 页面展示的 Key 永远是脱敏值；对 Key 的操作走数字 ``id``（池内顺序递增），
  前端拿不到也不需要明文。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .client import StRotator
from .config import (
    STRATEGIES,
    Config,
    ConfigStore,
    FlashLiteExchangeConfig,
    RateControlConfig,
    ReplenishConfig,
    next_account_name,
)
from .dashboard import DASHBOARD_HTML, LOGIN_HTML, LOGIN_HTML_INVALID
from .errors import ConfigError, RotatorError
from .logs import LogBuffer
from .autorenew import AutoRenewWorker
from .quota import QuotaService, QuotaWindow, WindowPair
from .replenish import count_available, count_unavailable, today_str
from .version import __version__

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注用，运行时字段默认 None
    from .replenish import ReplenishWorker
    from .guard import LeakGuardWorker
    from .registry import Registry

# 控制台页面路径（免鉴权，内容只是空壳）
PAGE_PATHS = frozenset({"/", "/ui", "/ui/", "/admin", "/admin/"})
# 控制台接口前缀（需要鉴权）
API_PREFIX = "/api/"

MAX_KEYS_PER_REQUEST = 20
MAX_IMPORT_LINES = 50
_KEY_SPLIT = re.compile(r"[\s,;]+")


def parse_key_list(raw: str) -> list[str]:
    """把用户粘贴的一大坨文本拆成 Key 列表（逗号 / 分号 / 换行 / 空格都认）。"""
    seen: set[str] = set()
    result: list[str] = []
    for token in _KEY_SPLIT.split(raw or ""):
        token = token.strip().strip("\"'")
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def parse_import_lines(raw: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """逐行解析导入文本：返回 [(行号, 原文, 段元组)]，跳过空行。"""
    rows: list[tuple[int, str, tuple[str, ...]]] = []
    for idx, line in enumerate((raw or "").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        parts = tuple(p.strip() for p in text.split("--"))
        rows.append((idx, text, parts))
    return rows


# ---------------------------------------------------------------- 指标


class UsageTracker:
    """按小时分桶的 token 用量统计（线程安全，只保留最近 N 小时）。"""

    def __init__(
        self, *, hours: int = 720, clock: Callable[[], float] = time.time
    ) -> None:
        self._hours = max(1, int(hours))
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[int, dict[str, int]] = {}
        # 最近一次记录用量的墙钟时间（None = 从未记录），供「自某时刻起是否有用量」查询
        self._last_at: float | None = None

    @staticmethod
    def _hour_of(ts: float) -> int:
        return int(ts // 3600) * 3600

    def note(
        self, *, prompt_tokens: int = 0, completion_tokens: int = 0, requests: int = 1
    ) -> None:
        hour = self._hour_of(self._clock())
        with self._lock:
            self._last_at = self._clock()
            bucket = self._buckets.get(hour)
            if bucket is None:
                bucket = {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
                self._buckets[hour] = bucket
            bucket["prompt"] += int(prompt_tokens or 0)
            bucket["completion"] += int(completion_tokens or 0)
            bucket["total"] = bucket["prompt"] + bucket["completion"]
            bucket["requests"] += int(requests or 0)
            cutoff = hour - (self._hours - 1) * 3600
            for old in [h for h in self._buckets if h < cutoff]:
                del self._buckets[old]

    def series(self, *, hours: int = 24) -> list[dict[str, int]]:
        hours = max(1, min(int(hours), self._hours))
        now_hour = self._hour_of(self._clock())
        start = now_hour - (hours - 1) * 3600
        out: list[dict[str, int]] = []
        with self._lock:
            for i in range(hours):
                h = start + i * 3600
                b = self._buckets.get(h)
                out.append(
                    {
                        "hour": h,
                        "prompt": b["prompt"] if b else 0,
                        "completion": b["completion"] if b else 0,
                        "total": b["total"] if b else 0,
                        "requests": b["requests"] if b else 0,
                    }
                )
        return out

    def last_at(self) -> float | None:
        """最近一次记录用量的墙钟时间；从未记录过则为 None。"""
        with self._lock:
            return self._last_at

    def has_usage_since(self, ts: float) -> bool:
        """自 ``ts`` 起是否记录过用量（``_last_at >= ts``，含相等边界）。"""
        with self._lock:
            return self._last_at is not None and self._last_at >= ts


class GatewayMetrics:
    """网关侧计数。线程安全，读多写少。

    只统计"客户端视角"的量。上游尝试次数由 ``StRotator`` 自己维护——
    因为重试发生在客户端内部，网关层看不到轮换了几次。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.client_requests = 0
        self.stream_requests = 0
        self.errors = 0
        self.paused = False
        self.usage = UsageTracker()

    def note_request(self, *, stream: bool = False) -> None:
        with self._lock:
            self.client_requests += 1
            if stream:
                self.stream_requests += 1

    def note_error(self) -> None:
        with self._lock:
            self.errors += 1

    def note_usage(
        self, *, prompt_tokens: int = 0, completion_tokens: int = 0, requests: int = 1
    ) -> None:
        self.usage.note(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            requests=requests,
        )

    def has_usage_since(self, ts: float) -> bool:
        """自 ``ts`` 起是否记录过用量（薄代理，转发给 ``usage``）。"""
        return self.usage.has_usage_since(ts)

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            self.paused = bool(paused)
            return self.paused

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self.paused

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "client_requests": self.client_requests,
                "stream_requests": self.stream_requests,
                "errors": self.errors,
                "paused": self.paused,
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
            }


# ---------------------------------------------------------------- 响应对象


@dataclass
class UiResponse:
    """控制台路由的处理结果。

    用数据对象而不是直接往 socket 写，是为了让路由逻辑可以脱离真实 HTTP 连接做单测。
    """

    status: int = 200
    payload: Any = None
    raw: str | None = None
    content_type: str = "application/json; charset=utf-8"
    retry_after: float | None = None

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "UiResponse":
        return cls(status=status, payload=payload)

    @classmethod
    def error(
        cls, message: str, status: int = 400, code: str | None = None
    ) -> "UiResponse":
        return cls(
            status=status,
            payload={
                "ok": False,
                "error": {"message": message, "code": code, "type": "console_error"},
            },
        )

    @classmethod
    def html(cls, text: str) -> "UiResponse":
        return cls(payload=None, raw=text, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- 控制台状态


@dataclass
class ConsoleState:
    """控制台的读写入口：一份内存配置 + 一个配置文件 + 一个日志缓冲。

    Attributes:
        store: 配置文件读写器（保留原始结构，支持 ${ENV} 占位符）。
        rotator: 正在跑的轮换客户端（改它就是改运行中的服务）。
        host / port: 网关监听地址，用于拼接入信息。
        token: 本地鉴权 Token；为空表示不鉴权。
        buffer: 日志环形缓冲。
        log_file: 日志文件路径（仅用于展示）。
    """

    store: ConfigStore
    rotator: StRotator
    host: str = "127.0.0.1"
    port: int = 8080
    token: str | None = None
    buffer: LogBuffer = field(default_factory=LogBuffer)
    log_file: str | None = None
    metrics: GatewayMetrics = field(default_factory=GatewayMetrics)
    quota: QuotaService | None = None
    auto_renew: AutoRenewWorker | None = None
    replenish: ReplenishWorker | None = None
    replenish_factory: Callable[[], ReplenishWorker | None] | None = None
    registry: Registry | None = None
    leak_guard: LeakGuardWorker | None = None
    # 补号保存后行为：sms_factory(token) 构造易码传输；sms_token_ok 记录最近一次
    # 校验结果（None=未配置 / True=可用 / False=已标记不可用）
    sms_factory: Callable[[str], Any] | None = None
    sms_token_ok: bool | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------ 只读

    @property
    def config(self) -> Config:
        return self.rotator.config

    def gateway_info(
        self, *, reveal_token: bool = False, request_base: str | None = None
    ) -> dict[str, Any]:
        """给上层应用抄的接入信息。

        ``request_base`` 为访问控制台所用的对外地址（如反代后的 ``https://域名``）；
        传入时优先用它拼接入信息，否则回退到监听地址。
        """
        base = request_base or f"http://{self.host}:{self.port}"
        return {
            "listen": f"{self.host}:{self.port}",
            "base_url": f"{base}/v1",
            "chat_endpoint": f"{base}/v1/chat/completions",
            "models_endpoint": f"{base}/v1/models",
            "health_endpoint": f"{base}/healthz",
            "stats_endpoint": f"{base}/stats",
            "console_url": f"{base}/",
            "token": (self.token or "") if reveal_token else "",
            "model": self.config.default_model,
            "upstream": self.config.base_url,
        }

    def usage_payload(self, *, hours: int = 24) -> dict[str, Any]:
        """近 N 小时的 token 用量（按小时分桶，最多保留 30 天）。"""
        return {"hours": hours, "buckets": self.metrics.usage.series(hours=hours)}

    def export_accounts(self) -> dict[str, Any]:
        """把全部「有登录凭据」的账号导出为导入格式（``手机--用户名--密码--apikey``）。

        每个账号的每一把 Key 一行；取已加载配置（``${ENV}`` 已展开）里的**明文**，
        以便直接粘回「批量新增」重新导入。缺 user/password 或无 Key 的账号无法用
        该格式重新导入，跳过并计入 ``skipped``。
        """
        lines: list[str] = []
        accounts = 0
        skipped = 0
        for acct in self.config.accounts:
            keys = [k for k in acct.api_keys if str(k).strip()]
            if not (acct.user and acct.password) or not keys:
                skipped += 1
                continue
            accounts += 1
            for key in keys:
                lines.append(f"{acct.phone}--{acct.user}--{acct.password}--{key}")
        return {
            "text": "\n".join(lines),
            "accounts": accounts,
            "lines": len(lines),
            "skipped": skipped,
        }

    def snapshot(
        self, *, reveal_token: bool = False, request_base: str | None = None
    ) -> dict[str, Any]:
        """整页刷新所需的全部状态。"""
        rate = self.rotator.limiter.stats()
        metrics = self.metrics.snapshot()
        metrics["upstream_attempts"] = self.rotator.upstream_attempts
        return {
            "version": __version__,
            "account_names": [a.name for a in self.config.accounts],
            "gateway": self.gateway_info(
                reveal_token=reveal_token, request_base=request_base
            ),
            "summary": self.rotator.pool.summary(),
            "keys": self._pool_snapshot(),
            "rate_control": rate,
            "metrics": metrics,
            "models": self.rotator.available_models(),
            "default_model": self.config.default_model,
            "log_file": self.log_file,
            "account_status": self.auto_renew.account_status()
            if self.auto_renew
            else {},
            "replenish_status": self.replenish.account_status()
            if self.replenish
            else {},
            "leak_guard_status": self.leak_guard_payload(),
            "replenish_state": self._replenish_state_payload(),
            "options": {
                "strategy": self.config.strategy,
                "rate_mode": self.config.rate_control.mode,
                "qps": self.config.rate_control.qps,
                "min_qps": self.config.rate_control.min_qps,
                "max_qps": self.config.rate_control.max_qps,
                "max_total_wait": self.config.max_total_wait,
                "max_attempts": self.config.max_attempts,
                "strategies": list(STRATEGIES),
                "rate_modes": list(RateControlConfig.MODES),
                "flash_lite": {
                    "enabled": self.config.flash_lite_exchange.enabled,
                    "model": self.config.flash_lite_exchange.model,
                    "concurrency": self.config.flash_lite_exchange.concurrency,
                    "requests_per_trigger": self.config.flash_lite_exchange.requests_per_trigger,
                    "min_interval_s": self.config.flash_lite_exchange.min_interval_s,
                    "long_text_max_tokens": self.config.flash_lite_exchange.long_text_max_tokens,
                    "image_enabled": self.config.flash_lite_exchange.image_enabled,
                    "image_size": self.config.flash_lite_exchange.image_size,
                    "multi_image_count": self.config.flash_lite_exchange.multi_image_count,
                    "yield_to_serve": self.config.flash_lite_exchange.yield_to_serve,
                    "min_available_mb": self.config.flash_lite_exchange.min_available_mb,
                },
                "replenish": {
                    "enabled": self.config.replenish.enabled,
                    "target_count": self.config.replenish.target_count,
                    "interval_seconds": self.config.replenish.interval_seconds,
                    "keyword": self.config.replenish.keyword,
                    "daily_spend_cap": self.config.replenish.daily_spend_cap,
                    "sms_poll_interval": self.config.replenish.sms_poll_interval,
                    "sms_poll_timeout": self.config.replenish.sms_poll_timeout,
                    "key_name": self.config.replenish.key_name,
                    "key_type": self.config.replenish.key_type,
                    "sms_token_configured": bool(self.config.replenish.sms_token),
                    "sms_token_ok": self.sms_token_ok,
                },
            },
        }

    def _pool_snapshot(self) -> list[dict[str, Any]]:
        """Key 池快照，并补上账号的用户名 / 手机号 / Key 最近写入时间，供控制台展示。"""
        keys = self.rotator.pool.snapshot()
        meta = {a.name: a for a in self.config.accounts}
        for item in keys:
            acct = meta.get(str(item.get("account")))
            item["username"] = acct.user if acct else ""
            item["phone"] = acct.phone if acct else ""
            item["updated_at"] = acct.updated_at if acct else None
        return keys

    def logs_since(self, cursor: int) -> dict[str, Any]:
        new_cursor, items = self.buffer.since(cursor)
        return {"cursor": new_cursor, "items": items}

    def quota_payload(self, *, force: bool) -> dict[str, Any]:
        """把余量快照转成 JSON（不含任何密钥）。"""
        if self.quota is None:
            return {"accounts": [], "consumption": _empty_consumption()}
        accounts: list[dict[str, Any]] = []
        for aq in self.quota.snapshot(force=force):
            accounts.append(
                {
                    "account": aq.account,
                    "user": aq.user,
                    "phone": aq.phone,
                    "status": aq.status,
                    "error": aq.error,
                    "fetched_at": aq.fetched_at,
                    "general": _pair_payload(aq.general),
                    "flash_lite": _pair_payload(aq.flash_lite),
                }
            )
        cons = dict(self.quota.credits.snapshot())
        cons["series"] = self.quota.credits.series(hours=24)
        return {"accounts": accounts, "consumption": cons}

    def credits_samples_payload(
        self, *, limit: int = 200, nonzero: bool = False
    ) -> dict[str, Any]:
        """最近的积分采样原始值（含消耗增量），用于排查尖峰来源。"""
        if self.quota is None:
            return {"samples": []}
        return {"samples": self.quota.credits.samples(limit=limit, nonzero=nonzero)}

    def leak_guard_payload(self) -> dict[str, Any]:
        """泄漏守卫状态；无 worker 时给出安全默认（含「已开启但未运行」提示）。"""
        if self.leak_guard is not None:
            return self.leak_guard.status()
        cfg = self.config.leak_guard
        enabled = bool(cfg.enabled)
        return {
            "enabled": enabled,
            "status": "not_running" if enabled else "disabled",
            "message": (
                "已开启但未运行：需配置账号登录凭据（user/password），或重启 ui / tray 后生效"
                if enabled
                else "未启用"
            ),
            "pending": [],
            "pending_count": 0,
            "last_scan_at": None,
            "last_rotate_at": None,
            "last_rotate_date": "",
            "next_rotate_at": None,
            "window_seconds": cfg.window_seconds,
            "rotate_hour": cfg.rotate_hour,
            "rotate_minute": cfg.rotate_minute,
        }

    def _replenish_state_payload(self) -> dict[str, Any]:
        """补号状态：可用/目标、当日花销、取号数、注册成功数、轮换数、失效账号数。

        可用数与 worker 用同一判定来源（auto_renew 的账号状态）：只有密码错误的账号算
        不可用。无 worker / registry 时给出安全默认（花销与计数为空）。
        """
        status_source = self.auto_renew.account_status() if self.auto_renew else {}
        available = count_available(self.config.accounts, status_source)
        invalid = count_unavailable(self.config.accounts, status_source)
        spend = self.registry.spend_state() if self.registry else {}
        used = len(self.registry.used_phones()) if self.registry else 0
        registrations_ok = (
            self.registry.count_registrations(True) if self.registry else 0
        )
        rotations = self.registry.rotation_count() if self.registry else 0
        return {
            "available": available,
            "target": self.config.replenish.target_count,
            "spend": spend,
            "consumed": _consumed_today(spend),
            "used_phones": used,
            "registrations_ok": registrations_ok,
            "rotations": rotations,
            "invalid_accounts": invalid,
            "running": self.replenish is not None,
        }

    # ------------------------------------------------------------ 写操作

    def add_keys(
        self,
        raw: str,
        *,
        account: str | None = None,
        max_concurrency: int = 4,
        rpm_limit: int | None = None,
        verify: bool = True,
    ) -> UiResponse:
        """批量加 Key：先体检，再把好 Key 同时写进内存池和配置文件。

        「体检」的判定标准很讲究：只有 **401/403** 才拒收。429 不算——限流恰恰说明
        凭据有效，只是当下没额度；如果把它当成失效，批量导入时会误杀好 Key。
        """
        candidates = parse_key_list(raw)
        if not candidates:
            return UiResponse.error("没有解析出任何 Key")
        if len(candidates) > MAX_KEYS_PER_REQUEST:
            return UiResponse.error(
                f"一次最多添加 {MAX_KEYS_PER_REQUEST} 把 Key，当前 {len(candidates)} 把"
            )

        added: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = []

        # 整批 Key 落在同一个账号下。账号级 rpm_limit 与 429 冷却都是按账号聚合的
        # （见 keypool.AccountState），如果逐把 Key 现算名字，它们会被拆成 N 个账号，
        # 聚合与联动冷却就全失效了 —— 每把 Key 独占一份配额，同账号雪崩照旧。
        # 名字只算一次、整批复用；想让它们分属不同账号，请分多批并显式指定账号名。
        with self.lock:
            target_account = account or next_account_name(self.store.account_names())

        for key in candidates:
            if len(key) < 8:
                rejected.append(
                    {"key": _mask(key), "reason": "长度不足 8 位，不像有效 Key"}
                )
                continue
            if self.rotator.pool.find_key(key) is not None:
                rejected.append({"key": _mask(key), "reason": "已在池中，跳过"})
                continue

            verdict, detail = ("ok", "未校验")
            if verify:
                verdict, detail = self.rotator.probe_key(key)
                if verdict == "invalid":
                    rejected.append(
                        {"key": _mask(key), "reason": f"凭据无效：{detail}"}
                    )
                    continue
                if verdict == "unknown":
                    warnings.append(
                        f"{_mask(key)} 暂时无法确认（{detail}），已按可用处理"
                    )

            with self.lock:
                try:
                    self.store.add_key(
                        key,
                        target_account,
                        max_concurrency=max_concurrency,
                        rpm_limit=rpm_limit,
                    )
                except ConfigError as exc:
                    rejected.append({"key": _mask(key), "reason": str(exc)})
                    continue
                try:
                    item = self.rotator.add_key(
                        key,
                        account=target_account,
                        max_concurrency=max_concurrency,
                        rpm_limit=rpm_limit,
                    )
                except ConfigError as exc:
                    # 内存池拒绝 → 回滚刚才写进 store 的那一条，保持两边一致
                    self.store.remove_key(key)
                    rejected.append({"key": _mask(key), "reason": str(exc)})
                    continue
                self.store.reload()
                self.rotator.config.accounts = list(self.store.config.accounts)
            added.append(
                {"id": item.id, "key": item.masked, "account": item.account}
            )

        if added:
            self._save_and_log(
                f"通过控制台新增 {len(added)} 把 Key（账号 {target_account}）"
            )
        if not added and not rejected:
            return UiResponse.error("没有可添加的 Key")
        message = f"新增 {len(added)} 把"
        if added:
            message += f"（归入账号 {target_account}，同账号共享配额与 429 冷却）"
        if rejected:
            message += f"，跳过 {len(rejected)} 把"
        return UiResponse.json(
            {
                "ok": bool(added),
                "account": target_account,
                "added": added,
                "rejected": rejected,
                "warnings": warnings,
                "message": message,
            }
        )

    def import_keys(
        self, raw: str, *, account: str | None, max_concurrency: int
    ) -> UiResponse:
        """批量导入：支持「纯 Key」与「手机--用户名--密码--apikey」两种格式，逐行校验。

        整批一次最多 ``MAX_IMPORT_LINES`` 行；重复（本批内 / 已在池中）跳过，
        校验失败的逐行报错，互不影响。
        """
        rows = parse_import_lines(raw)
        if not rows:
            return UiResponse.error("没有解析出任何内容")
        if len(rows) > MAX_IMPORT_LINES:
            return UiResponse.error(
                f"一次最多导入 {MAX_IMPORT_LINES} 行，当前 {len(rows)} 行"
            )

        def row(
            line_no: int, parts: tuple[str, ...], status: str, reason: str, masked: str
        ) -> dict[str, Any]:
            return {
                "line": line_no,
                "input_masked": masked,
                "format": len(parts),
                "status": status,
                "reason": reason,
            }

        format_error = (
            "格式错误：应为 1 段（纯 key）或 4 段（手机--用户名--密码--apikey）"
        )

        results: list[dict[str, Any]] = []
        added: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        ok = error = skipped = 0

        with self.lock:
            default_account = account or next_account_name(self.store.account_names())

        for line_no, text, parts in rows:
            if len(parts) not in (1, 4):
                results.append(row(line_no, parts, "error", format_error, _mask(text)))
                error += 1
                continue

            if len(parts) == 1:
                key = parts[0]
                phone = user = password = ""
                target = default_account
            else:
                phone, user, password, key = parts
                target = self._import_account_for(user, phone)

            if key in seen_keys:
                results.append(
                    row(line_no, parts, "skipped", "重复：本批已出现", _mask(key))
                )
                skipped += 1
                continue
            if self.rotator.pool.find_key(key) is not None:
                results.append(
                    row(line_no, parts, "skipped", "重复：已在池中", _mask(key))
                )
                skipped += 1
                continue
            seen_keys.add(key)

            # 格式 2：先校验凭据（用户名/密码需齐全，且需要 jwcrypto）
            if len(parts) == 4:
                if not (user and password):
                    results.append(
                        row(
                            line_no,
                            parts,
                            "error",
                            "格式错误：用户名或密码为空",
                            _mask(text),
                        )
                    )
                    error += 1
                    continue
                svc = self.quota or QuotaService(self.config)
                self.quota = svc
                err = svc.verify_credentials(user, password)
                if err is not None:
                    # 缺 jwcrypto 属「校验不可用」，不是凭据本身无效，不加误导性前缀
                    reason = err if "jwcrypto" in err else f"凭据无效：{err}"
                    results.append(row(line_no, parts, "error", reason, _mask(text)))
                    error += 1
                    continue

            # 校验 Key 本身（有效性由上游探测决定）
            verdict, detail = self.rotator.probe_key(key)
            if verdict == "invalid":
                results.append(
                    row(line_no, parts, "error", f"Key 无效：{detail}", _mask(key))
                )
                error += 1
                continue

            # 写入：先落配置文件，再进内存池；内存池接受后才写凭据，最后同步内存配置
            try:
                with self.lock:
                    self.store.add_key(key, target, max_concurrency=max_concurrency)
                    try:
                        item = self.rotator.add_key(
                            key, account=target, max_concurrency=max_concurrency
                        )
                    except Exception:
                        # 内存池拒绝 → 回滚刚落进 store 的那一条（此时尚未写凭据），保持两边一致
                        try:
                            self.store.remove_key(key)
                        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
                            pass
                        raise
                    if len(parts) == 4:
                        self.store.set_account_credentials(
                            target, user=user, phone=phone, password=password
                        )
                    self.store.reload()
                    self.rotator.config.accounts = list(self.store.config.accounts)
                added.append(
                    {"id": item.id, "key": item.masked, "account": item.account}
                )
                results.append(row(line_no, parts, "ok", "已导入", item.masked))
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单行失败不影响其余
                results.append(row(line_no, parts, "error", str(exc), _mask(key)))
                error += 1

        if added:
            self._save_and_log(f"通过控制台批量导入 {len(added)} 把 Key")
        return UiResponse.json(
            {
                "ok": bool(added),
                "added": added,
                "results": results,
                "summary": {"ok": ok, "error": error, "skipped": skipped},
            }
        )

    def _import_account_for(self, user: str, phone: str) -> str:
        """格式 2 的归属账号：优先复用已有同名 ``user`` 的账号，否则按 ``user or phone`` 新建。

        多行同一 ``user`` 会归入同一个账号（与格式 1 的账号聚合语义一致），
        已存在同 ``user`` 的账号也不会被重复创建。
        """
        if user:
            for account in self.store.config.accounts:
                if account.user and account.user == user:
                    return account.name
        return user or phone

    def verify_one(self, identifier: str) -> UiResponse:
        """体检池中某一把 Key（identifier 可以是数字 id、key_id 或明文）。"""
        item = self.rotator.pool.find_by_id(identifier)
        if item is None:
            return UiResponse.error("池中找不到这把 Key（可能已被删除）", status=404)
        verdict, detail = self.rotator.probe_key(item.key)
        labels = {"ok": "凭据有效", "invalid": "凭据无效", "unknown": "暂时无法确认"}
        return UiResponse.json(
            {
                "ok": verdict != "invalid",
                "verdict": verdict,
                "detail": detail,
                "message": f"{item.account} / {item.masked}：{labels.get(verdict, verdict)}（{detail}）",
            }
        )

    def remove_one(self, identifier: str) -> UiResponse:
        """从内存池和配置文件里同时删除一把 Key。"""
        item = self.rotator.pool.find_by_id(identifier)
        if item is None:
            return UiResponse.error("池中找不到这把 Key（可能已被删除）", status=404)
        plain, label = item.key, f"{item.account} / {item.masked}"
        if (
            self.rotator.pool.find_key(plain) is not None
            and len(self.rotator.pool) <= 1
        ):
            return UiResponse.error(
                "这是池里最后一把 Key，删掉后就无法提供服务了；请先添加新 Key"
            )

        with self.lock:
            removed = self.rotator.remove_key(plain)
            self.store.remove_key(plain)
            self.store.reload()
            self.rotator.config.accounts = list(self.store.config.accounts)
        if not removed:
            return UiResponse.error("删除失败：Key 已不在池中", status=409)
        self._save_and_log(f"通过控制台删除 Key：{label}")
        return UiResponse.json({"ok": True, "message": f"已删除 {label}"})

    def set_model(self, model: str) -> UiResponse:
        model = (model or "").strip()
        if not model:
            return UiResponse.error("模型名不能为空")
        known = {m["id"] for m in self.rotator.available_models().get("models", [])}
        with self.lock:
            self.rotator.set_default_model(model)
            self.store.set_default_model(model)
        self._save_and_log(f"默认模型切换为 {model}")
        note = "" if not known or model in known else "（不在上游清单里，请确认拼写）"
        return UiResponse.json(
            {"ok": True, "model": model, "message": f"默认模型已切换为 {model}{note}"}
        )

    def set_options(self, payload: Mapping[str, Any]) -> UiResponse:
        """改运行参数：调度策略 / 限速 / 重试预算。改动立即生效并落盘。"""
        changes: list[str] = []

        strategy = payload.get("strategy")
        if strategy is not None and strategy != self.config.strategy:
            if strategy not in STRATEGIES:
                return UiResponse.error(f"调度策略只能是 {list(STRATEGIES)} 之一")
            with self.lock:
                self.rotator.set_strategy(strategy)
                self.store.set_strategy(strategy)
            changes.append(f"策略={strategy}")

        rate_fields: dict[str, Any] = {}
        mode = payload.get("rate_mode")
        if mode is not None and mode != self.config.rate_control.mode:
            if mode not in RateControlConfig.MODES:
                return UiResponse.error(
                    f"限速模式只能是 {list(RateControlConfig.MODES)} 之一"
                )
            rate_fields["mode"] = mode
        qps = payload.get("qps")
        if qps is not None:
            try:
                qps = float(qps)
            except (TypeError, ValueError):
                return UiResponse.error(f"QPS 不是合法数字：{qps!r}")
            if qps < 0:
                return UiResponse.error("QPS 不能为负")
            effective_mode = rate_fields.get("mode") or self.config.rate_control.mode
            if effective_mode != "off" and qps <= 0:
                return UiResponse.error(
                    f"限速模式为 {effective_mode} 时 QPS 必须大于 0"
                )
            if qps != self.config.rate_control.qps:
                rate_fields["qps"] = qps
        if rate_fields:
            try:
                with self.lock:
                    self.rotator.set_rate_control(**rate_fields)
                    self.store.set_rate_control(**rate_fields)
            except ConfigError as exc:
                return UiResponse.error(str(exc))
            changes.append(
                "限速=" + " ".join(f"{k}:{v}" for k, v in rate_fields.items())
            )

        for field_name, label in (
            ("max_total_wait", "等待预算"),
            ("max_attempts", "最大重试"),
        ):
            value = payload.get(field_name)
            if value is None:
                continue
            try:
                converted = int(value) if field_name == "max_attempts" else float(value)
            except (TypeError, ValueError):
                return UiResponse.error(f"{label} 不是合法数字：{value!r}")
            if field_name == "max_attempts" and converted < 1:
                return UiResponse.error("最大重试次数必须 >= 1")
            if field_name == "max_total_wait" and converted < 0:
                return UiResponse.error("等待预算不能为负")
            if converted == getattr(self.config, field_name):
                continue
            with self.lock:
                setattr(self.config, field_name, converted)
                self.store.set_scalar(field_name, converted)
            changes.append(f"{label}={converted}")

        fx_payload = payload.get("flash_lite")
        if fx_payload is not None:
            if not isinstance(fx_payload, dict):
                return UiResponse.error("flash_lite 必须是对象")
            fx_cfg = self.config.flash_lite_exchange
            fx_changes: dict[str, Any] = {}
            for f in ("enabled", "image_enabled", "yield_to_serve"):
                if f in fx_payload:
                    fx_changes[f] = bool(fx_payload[f])
            fx_num_fields = (
                ("concurrency", int, 1, 64, "一换一并发"),
                ("requests_per_trigger", int, 1, 2048, "一换一单轮上限"),
                ("min_interval_s", float, 60.0, 86400.0, "一换一触发间隔"),
                ("long_text_max_tokens", int, 128, 16384, "一换一长文预算"),
                ("image_size", int, 256, 2048, "一换一大图边长"),
                ("multi_image_count", int, 1, 9, "一换一每请求图片数"),
                ("min_available_mb", float, 0.0, 8192.0, "一换一内存下限(MB)"),
            )
            for f, typ, lo, hi, label in fx_num_fields:
                if f not in fx_payload:
                    continue
                try:
                    v = typ(fx_payload[f])
                except (TypeError, ValueError):
                    return UiResponse.error(f"{label}不是合法数字：{fx_payload[f]!r}")
                if not lo <= v <= hi:
                    return UiResponse.error(f"{label}需在 {lo:g}~{hi:g} 之间")
                fx_changes[f] = v
            fx_changes = {
                k: v for k, v in fx_changes.items() if getattr(fx_cfg, k) != v
            }
            if fx_changes:
                # 先在纯数据上整体校验（不改内存态），通过后再落盘 + 生效
                candidate = {
                    f: getattr(fx_cfg, f)
                    for f in FlashLiteExchangeConfig.__dataclass_fields__
                }
                candidate.update(fx_changes)
                try:
                    FlashLiteExchangeConfig.from_dict(candidate)
                except ConfigError as exc:
                    return UiResponse.error(str(exc))
                with self.lock:
                    for k, v in fx_changes.items():
                        setattr(fx_cfg, k, v)
                    self.store.set_section("flash_lite_exchange", fx_changes)
                changes.append(
                    "一换一 " + "，".join(f"{k}:{v}" for k, v in fx_changes.items())
                )

        rp_payload = payload.get("replenish")
        token_msg = ""
        if rp_payload is not None:
            if not isinstance(rp_payload, dict):
                return UiResponse.error("replenish 必须是对象")
            rp_cfg = self.config.replenish
            rp_changes: dict[str, Any] = {}
            if "enabled" in rp_payload:
                rp_changes["enabled"] = bool(rp_payload["enabled"])
            for field_name, label in (
                ("target_count", "目标账号数"),
                ("interval_seconds", "检测间隔(秒)"),
                ("sms_poll_interval", "短信轮询间隔(秒)"),
                ("sms_poll_timeout", "短信等待超时(秒)"),
            ):
                if field_name not in rp_payload:
                    continue
                try:
                    caster = int if field_name == "target_count" else float
                    rp_changes[field_name] = caster(rp_payload[field_name])
                except (TypeError, ValueError):
                    return UiResponse.error(
                        f"{label}不是合法数字：{rp_payload[field_name]!r}"
                    )
            if "daily_spend_cap" in rp_payload:
                try:
                    rp_changes["daily_spend_cap"] = float(rp_payload["daily_spend_cap"])
                except (TypeError, ValueError):
                    return UiResponse.error(
                        f"单日消费上限不是合法数字：{rp_payload['daily_spend_cap']!r}"
                    )
            for field_name in ("keyword", "key_name", "key_type", "sms_token"):
                if field_name not in rp_payload or rp_payload[field_name] is None:
                    continue
                value = str(rp_payload[field_name]).strip()
                if field_name == "sms_token" and not value:
                    continue  # 空 token 忽略，绝不清空既有配置
                if field_name == "keyword" and not value:
                    return UiResponse.error("短信关键词不能为空")
                rp_changes[field_name] = value
            rp_changes = {
                k: v for k, v in rp_changes.items() if getattr(rp_cfg, k) != v
            }
            if rp_changes:
                # 先在纯数据上整体校验（不改内存态），通过后再落盘 + 生效
                candidate = {
                    f: getattr(rp_cfg, f) for f in ReplenishConfig.__dataclass_fields__
                }
                candidate.update(rp_changes)
                try:
                    ReplenishConfig.from_dict(candidate)
                except ConfigError as exc:
                    return UiResponse.error(str(exc))
                with self.lock:
                    for k, v in rp_changes.items():
                        setattr(rp_cfg, k, v)
                    if self.store is not None:
                        self.store.set_section("replenish", rp_changes)
                changes.append(
                    "补号 "
                    + "，".join(
                        f"{k}:***" if k == "sms_token" else f"{k}:{v}"
                        for k, v in rp_changes.items()
                    )
                )
                self._sync_replenish(rebuild="sms_token" in rp_changes)
                self._reconcile_replenish_after_save()
                if not self.config.replenish.sms_token:
                    self.sms_token_ok = None
                elif self.sms_factory is not None:
                    try:
                        self.sms_factory(self.config.replenish.sms_token).left_amount()
                        self.sms_token_ok = True
                    except Exception:  # noqa: BLE001 - Token 校验失败只标记不可用，不打断落盘
                        self.sms_token_ok = False
                        token_msg = "；易码 Token 校验失败，已标记不可用"

        if not changes:
            return UiResponse.json({"ok": True, "message": "没有需要改动的参数"})
        self._save_and_log("运行参数已更新：" + "，".join(changes))
        return UiResponse.json(
            {"ok": True, "message": "已更新：" + "，".join(changes) + token_msg}
        )

    def _reconcile_replenish_after_save(self) -> None:
        """保存补号配置后立即对账：可用数仍低于目标则确保 worker 并跑一轮。

        全程吞异常——对账失败绝不能让配置保存失败。
        """
        try:
            available = count_available(
                self.config.accounts,
                self.auto_renew.account_status() if self.auto_renew else {},
            )
            target = self.config.replenish.target_count
            if self.config.replenish.enabled and available < target:
                if self.replenish is None and self.replenish_factory is not None:
                    self.replenish = self.replenish_factory()
                # 只唤醒 worker 的线程去跑，绝不在请求线程里跑补号——否则会与
                # worker 自身的循环并发（补号必须串行、一个一个来）。
                wake = getattr(self.replenish, "wake", None)
                if wake is not None:
                    wake()
                self._log(f"[补号] 可用 {available} < 目标 {target}，已开启自动补号")
        except Exception:  # noqa: BLE001 - 对账失败不阻止保存
            pass

    def _sync_replenish(self, *, rebuild: bool = False) -> None:
        """把运行中的补号 worker 与最新 config.replenish 对齐（保存后即时生效）。

        未启用/目标<=0/无 sms_token 时停机；需要运行但无 worker 或需重建时用
        ``replenish_factory`` 新建；否则对现有 worker 热更新标量参数。
        """
        rc = self.config.replenish
        want = bool(rc.enabled and rc.target_count > 0 and rc.sms_token)
        if not want:
            if self.replenish is not None:
                self.replenish.stop()
                self.replenish = None
            return
        if self.replenish is None or rebuild:
            if self.replenish is not None:
                self.replenish.stop()
                self.replenish = None
            if self.replenish_factory is not None:
                self.replenish = self.replenish_factory()
            return
        reconfigure = getattr(self.replenish, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(
                target=rc.target_count,
                interval=rc.interval_seconds,
                keyword=rc.keyword,
                sms_poll_interval=rc.sms_poll_interval,
                sms_poll_timeout=rc.sms_poll_timeout,
                daily_spend_cap=rc.daily_spend_cap,
                key_name=rc.key_name,
                key_type=rc.key_type,
            )

    def set_paused(self, paused: bool) -> UiResponse:
        """暂停 / 恢复对外服务（网关进程和控制台都还活着）。"""
        state = self.metrics.set_paused(paused)
        self._log(f"[控制台] {'暂停' if state else '恢复'}对外接入")
        return UiResponse.json(
            {
                "ok": True,
                "paused": state,
                "message": "已暂停接入，上层会收到 503" if state else "已恢复接入",
            }
        )

    def refresh_models(self) -> UiResponse:
        try:
            catalog = self.rotator.available_models(refresh=True)
        except RotatorError as exc:
            return UiResponse.error(f"拉取模型清单失败：{exc}", status=502)
        if catalog.get("error") and not catalog.get("models"):
            return UiResponse.error(f"拉取模型清单失败：{catalog['error']}", status=502)
        return UiResponse.json(
            {
                "ok": True,
                "count": len(catalog.get("models", [])),
                "message": f"已拉取 {len(catalog.get('models', []))} 个模型",
            }
        )

    # ------------------------------------------------------------ 内部

    def _save_and_log(self, note: str) -> None:
        """落盘 + 记一条日志。落盘失败不该让操作回滚（内存里已经生效了）。"""
        try:
            self.store.save()
        except OSError as exc:
            self._log(f"[警告] 配置写盘失败：{exc}（本次改动只在内存中生效）")
        self._log(f"[控制台] {note}")

    def _log(self, message: str) -> None:
        """写一条日志。

        复用网关的日志出口（``rotator._log`` → 文件 + 环形缓冲 + echo）。
        ⚠️ 出口本身已经写环形缓冲，所以这里**不能**再直接 ``buffer.append``，
        否则同一条日志会在控制台出现两遍。出口缺失/不可用时退回直接写缓冲。
        """
        sink = getattr(self.rotator, "_log", None)
        if sink is None:
            self.buffer.append(message)
            return
        try:
            sink(message)
        except Exception:  # pragma: no cover
            self.buffer.append(message)

    # ------------------------------------------------------------ 路由

    @staticmethod
    def is_console_path(path: str) -> bool:
        return path in PAGE_PATHS or path.startswith(API_PREFIX)

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Sequence[str]] | None = None,
        body: Mapping[str, Any] | None = None,
        reveal_token: bool = False,
        request_base: str | None = None,
    ) -> UiResponse | None:
        """处理控制台请求；路径不属于控制台时返回 None，交回网关处理。"""
        if method == "GET" and path in PAGE_PATHS:
            return UiResponse.html(DASHBOARD_HTML)
        if not path.startswith(API_PREFIX):
            return None

        try:
            if method == "GET":
                if path == "/api/state":
                    return UiResponse.json(
                        self.snapshot(
                            reveal_token=reveal_token, request_base=request_base
                        )
                    )
                if path == "/api/usage":
                    hours = _clamp_int(_first_int(query, "hours", 24), 24, 1, 720)
                    return UiResponse.json(self.usage_payload(hours=hours))
                if path == "/api/credits/samples":
                    limit = _clamp_int(_first_int(query, "limit", 200), 200, 1, 2000)
                    nonzero = bool(_first_int(query, "nonzero", 0))
                    return UiResponse.json(
                        self.credits_samples_payload(limit=limit, nonzero=nonzero)
                    )
                if path == "/api/logs":
                    cursor = _first_int(query, "cursor", 0)
                    return UiResponse.json(self.logs_since(cursor))
                if path == "/api/quota":
                    force = bool(_first_int(query, "refresh", 0))
                    return UiResponse.json(self.quota_payload(force=force))
                if path == "/api/replenish/registrations":
                    if self.registry is None:
                        return UiResponse.json(
                            {"items": [], "total": 0, "page": 1, "size": 20}
                        )
                    return UiResponse.json(
                        self.registry.registrations_page(
                            page=_clamp_int(
                                _first_int(query, "page", 1), 1, 1, 2**31 - 1
                            ),
                            size=_clamp_int(_first_int(query, "size", 20), 20, 1, 200),
                            q=_first_str(query, "q", ""),
                            status=_first_str(query, "status", ""),
                            kind=_first_str(query, "kind", ""),
                        )
                    )
                if path == "/api/accounts/export":
                    return UiResponse.json(self.export_accounts())
                return UiResponse.error(f"未知接口 {path}", status=404)

            if method == "POST":
                payload = dict(body or {})
                if path == "/api/keys/add":
                    rpm_limit_raw = payload.get("rpm_limit")
                    rpm_limit: int | None = None
                    if rpm_limit_raw is not None and str(rpm_limit_raw).strip() != "":
                        try:
                            rpm_limit = _clamp_int(rpm_limit_raw, 30, 1, 10000)
                        except Exception:
                            rpm_limit = None
                    return self.add_keys(
                        str(payload.get("keys") or payload.get("key") or ""),
                        account=(str(payload.get("account")).strip() or None)
                        if payload.get("account")
                        else None,
                        max_concurrency=_clamp_int(
                            payload.get("max_concurrency"), 4, 1, 64
                        ),
                        rpm_limit=rpm_limit,
                    )
                if path == "/api/keys/import":
                    return self.import_keys(
                        str(payload.get("lines") or ""),
                        account=(str(payload.get("account")).strip() or None)
                        if payload.get("account")
                        else None,
                        max_concurrency=_clamp_int(
                            payload.get("max_concurrency"), 4, 1, 64
                        ),
                    )
                if path == "/api/keys/verify":
                    return self.verify_one(
                        str(payload.get("id") or payload.get("key") or "")
                    )
                if path == "/api/keys/remove":
                    return self.remove_one(
                        str(payload.get("id") or payload.get("key") or "")
                    )
                if path == "/api/model":
                    return self.set_model(str(payload.get("model") or ""))
                if path == "/api/models/refresh":
                    return self.refresh_models()
                if path == "/api/options":
                    return self.set_options(payload)
                if path == "/api/pause":
                    return self.set_paused(bool(payload.get("paused")))
                return UiResponse.error(f"未知接口 {path}", status=404)

            return UiResponse.error(f"不支持的方法 {method}", status=405)
        except ConfigError as exc:
            return UiResponse.error(str(exc))
        except RotatorError as exc:
            return UiResponse.error(f"{type(exc).__name__}: {exc}", status=502)
        except Exception as exc:  # pragma: no cover - 控制台不该把网关搞崩
            return UiResponse.error(
                f"控制台内部错误：{type(exc).__name__}: {exc}", status=500
            )


def _mask(key: str) -> str:
    from .keypool import mask_key

    return mask_key(key)


def _first_int(
    query: Mapping[str, Sequence[str]] | None, name: str, default: int
) -> int:
    if not query:
        return default
    values = query.get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError, IndexError):
        return default


def _first_str(
    query: Mapping[str, Sequence[str]] | None, name: str, default: str
) -> str:
    if not query:
        return default
    values = query.get(name)
    if not values:
        return default
    return str(values[0])


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _window_payload(window: QuotaWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"remaining": window.remaining, "reset_at": window.reset_at}


def _pair_payload(pair: WindowPair | None) -> dict[str, Any] | None:
    if pair is None:
        return None
    return {"h5": _window_payload(pair.h5), "d7": _window_payload(pair.d7)}


def _empty_consumption() -> dict[str, Any]:
    """没有配额服务时返回全 0 的消耗结构，保持前端字段稳定。"""
    zeros = {k: 0.0 for k in ("h1", "h5", "h24", "d7", "d30")}
    return {"general": dict(zeros), "flash_lite": dict(zeros), "series": []}


def _consumed_today(
    spend: Mapping[str, Any], *, today: str | None = None
) -> float:
    """今日已用（元）：换天后视为 0，避免沿用昨天的余额差。

    与 ``replenish.spend_ok`` 同一口径：只有 ``spend["date"] == today`` 时才用
    ``start_balance - last_balance`` 计算；否则（无基线 / 尚未跨到新一天的前值）返回 0。
    """
    today = today or today_str()
    if not spend or spend.get("date") != today:
        return 0.0
    start = spend.get("start_balance")
    last = spend.get("last_balance")
    if start is None or last is None:
        return 0.0
    try:
        return max(0.0, float(start) - float(last))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 开窗


EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_app_browser(explicit: str | None = None) -> str | None:
    """找一个支持 ``--app`` 模式的浏览器（Edge 优先，其次 Chrome）。"""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in EDGE_CANDIDATES + CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def default_profile_dir() -> Path:
    """独立浏览器配置目录。

    用独立 profile 有两个好处：一是窗口不受用户现有浏览器会话/插件影响，真正像
    一个独立应用；二是 localStorage（存的访问 Token）能跨次启动保留。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "st-rotator" / "console-profile"


def open_console_window(
    url: str,
    *,
    browser: str | None = None,
    profile_dir: str | os.PathLike[str] | None = None,
    size: str = "1380,900",
    detach: bool = True,
) -> tuple[bool, str]:
    """用浏览器 app 模式开一个无地址栏的独立窗口。

    Returns:
        ``(是否成功, 说明)``
    """
    executable = find_app_browser(browser)
    if not executable:
        return False, "未找到 Edge / Chrome，请手动用浏览器打开该地址"
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        f"--app={url}",
        f"--window-size={size}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MSImplicitSignin",
    ]
    kwargs: dict[str, Any] = {"close_fds": True}
    if detach:
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                0x00000008 | 0x08000000
            )  # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)  # noqa: S603 - 路径来自固定候选/用户显式指定
    except OSError as exc:
        return False, f"启动 {Path(executable).name} 失败：{exc}"
    return True, f"已用 {Path(executable).name} 打开控制台窗口"


def open_in_default_browser(url: str) -> bool:
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:  # pragma: no cover
        return False
