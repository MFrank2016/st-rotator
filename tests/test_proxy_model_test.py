"""控制台 ``POST /api/model-test`` 接口的 HTTP 行为测试。

复用 ``_ProxyServerTestCase`` 夹具（真实 ThreadingHTTPServer + 端口 0 + http.client），
上游一律换成假客户端：``modeltest.DEFAULT_CLIENT_FACTORY`` 被 mock 掉，**绝不访问
真实网络**（base_url 指向死端口）。覆盖：鉴权（Bearer / Cookie / 未鉴权 401）、
非流式 JSON 汇总、并发语义、流式 SSE 事件序列、输入校验、账号级错误隔离、
响应不回显明文 Key，以及同服务器上 /v1/chat 的回归。
"""

from __future__ import annotations

import contextlib
import json
import threading
import unittest
from typing import Any, Callable, Iterator
from unittest import mock

from st_rotator import modeltest
from st_rotator.transport import Response

from tests.test_proxy_console_auth import TOKEN, _ProxyServerTestCase


# ---------------------------------------------------------------- 假上游


class _RecordingClient:
    """假客户端：按 Authorization 头挑选罐头响应，并记录每次上游请求。"""

    def __init__(self, by_auth: dict[str, Response]) -> None:
        self.by_auth = by_auth
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> Response:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "headers": headers,
                "stream": stream,
            }
        )
        return self.by_auth[(headers or {}).get("Authorization", "")]


class _BarrierClient(_RecordingClient):
    """request 里先等 barrier：两个账号必须并发到达才算通过。"""

    def __init__(
        self, barrier: threading.Barrier, by_auth: dict[str, Response]
    ) -> None:
        super().__init__(by_auth)
        self._barrier = barrier

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> Response:
        self._barrier.wait(timeout=10)
        return super().request(
            method, path, json_body=json_body, headers=headers, stream=stream
        )


def _ok_response(content: str = "hi", usage: dict[str, Any] | None = None) -> Response:
    payload = {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"total_tokens": 3},
    }
    return Response(200, {}, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _stream_response() -> Response:
    """OpenAI 风格 SSE 流式响应：一个文本增量 + DONE。"""
    sse = 'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: [DONE]\n\n'
    return Response(200, {}, sse.encode("utf-8"))


def _error_response(status: int, body: str) -> Response:
    return Response(status, {}, body.encode("utf-8"))


@contextlib.contextmanager
def _patched_factory(
    make_client: Callable[[], Any], clients: list[_RecordingClient] | None = None
) -> Iterator[None]:
    """把 ``modeltest.DEFAULT_CLIENT_FACTORY`` 换成返回假客户端的工厂。"""

    def factory() -> Any:
        client = make_client()
        if clients is not None:
            clients.append(client)
        return client

    with mock.patch.object(modeltest, "DEFAULT_CLIENT_FACTORY", lambda config: factory):
        yield


# ---------------------------------------------------------------- 测试夹具


class _ModelTestProxyTestCase(_ProxyServerTestCase):
    """两个账号（第一把 Key 分别为 sk-A / sk-B）的网关夹具。"""

    def raw_config(self) -> dict[str, Any]:
        return {
            "base_url": "http://127.0.0.1:9/v1",  # 死端口，靠假工厂兜底
            "default_model": "deepseek-v4-flash",
            "accounts": [
                {
                    "name": "账号1",
                    "api_keys": ["sk-A"],
                    "rpm_limit": 2,
                    "max_concurrency": 4,
                    "weight": 1,
                },
                {
                    "name": "账号2",
                    "api_keys": ["sk-B"],
                    "rpm_limit": 2,
                    "max_concurrency": 4,
                    "weight": 1,
                },
            ],
        }

    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def post_model_test(
        self, body: dict[str, Any], headers: dict[str, str] | None = None
    ):
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return self.request(
            "POST",
            "/api/model-test",
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=h,
        )


# ---------------------------------------------------------------- 测试


class ModelTestAuthTest(_ModelTestProxyTestCase):
    """鉴权口径：/api/* 必须 Bearer 或 Cookie，未鉴权 401 JSON。"""

    def test_without_token_returns_401_json(self) -> None:
        resp, body = self.post_model_test(
            {"model": "m1", "accounts": ["账号1"], "prompt": "你好"}
        )
        self.assertEqual(resp.status, 401)
        self.assertIn("error", json.loads(body))

    def test_cookie_login_allowed(self) -> None:
        pair = self.login()
        with _patched_factory(lambda: _RecordingClient({})):
            resp, body = self.post_model_test(
                {"model": "m1", "accounts": ["账号1"], "prompt": "你好"},
                headers={"Cookie": pair},
            )
        self.assertEqual(resp.status, 200)
        parsed = json.loads(body)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["results"][0]["account"], "账号1")

    def test_bearer_allowed(self) -> None:
        with _patched_factory(lambda: _RecordingClient({})):
            resp, _ = self.post_model_test(
                {"model": "m1", "accounts": ["账号1"], "prompt": "你好"},
                headers=self.bearer(),
            )
        self.assertEqual(resp.status, 200)


class ModelTestNonStreamTest(_ModelTestProxyTestCase):
    """非流式：200 JSON 汇总，按请求顺序返回，且只发每账号第一把 Key。"""

    def test_two_accounts_ordered_with_usage(self) -> None:
        clients: list[_RecordingClient] = []
        by_auth = {
            "Bearer sk-A": _ok_response(content="hi-A"),
            "Bearer sk-B": _ok_response(content="hi-B", usage={"total_tokens": 5}),
        }
        with _patched_factory(lambda: _RecordingClient(by_auth), clients):
            resp, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                },
                headers=self.bearer(),
            )

        self.assertEqual(resp.status, 200)
        parsed = json.loads(body)
        self.assertTrue(parsed["ok"])
        results = parsed["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual([r["account"] for r in results], ["账号1", "账号2"])
        self.assertTrue(all(r["status"] == "ok" for r in results))
        self.assertEqual(results[0]["text"], "hi-A")
        self.assertEqual(results[0]["usage"], {"total_tokens": 3})
        self.assertEqual(results[1]["usage"], {"total_tokens": 5})
        self.assertIsNone(results[0]["error"])
        self.assertIsNone(results[1]["error"])

        # 每个账号各自独立客户端，且只带自己第一把 Key 的 Authorization
        auths = sorted(
            call["headers"].get("Authorization", "")
            for client in clients
            for call in client.calls
        )
        self.assertEqual(auths, ["Bearer sk-A", "Bearer sk-B"])
        for client in clients:
            call = client.calls[0]
            self.assertEqual(call["path"], "chat/completions")
            self.assertFalse(call["stream"])
            self.assertEqual(call["json_body"]["model"], "m1")
            self.assertEqual(
                call["json_body"]["messages"],
                [{"role": "user", "content": "你好"}],
            )

    def test_concurrent_accounts_overlap(self) -> None:
        barrier = threading.Barrier(2)
        by_auth = {"Bearer sk-A": _ok_response(), "Bearer sk-B": _ok_response()}
        with _patched_factory(lambda: _BarrierClient(barrier, by_auth)):
            resp, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                },
                headers=self.bearer(),
            )
        self.assertEqual(resp.status, 200)
        results = json.loads(body)["results"]
        self.assertEqual(len(results), 2)
        # 若串行执行，barrier 会超时 -> fatal；全 ok 才证明两个请求重叠
        self.assertTrue(all(r["status"] == "ok" for r in results))


class ModelTestStreamTest(_ModelTestProxyTestCase):
    """流式：text/event-stream，事件序列含 start/token/done/complete 与 account 字段。"""

    def test_stream_sse_event_sequence(self) -> None:
        by_auth = {
            "Bearer sk-A": _stream_response(),
            "Bearer sk-B": _stream_response(),
        }
        with _patched_factory(lambda: _RecordingClient(by_auth)):
            resp, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                    "stream": True,
                },
                headers=self.bearer(),
            )

        self.assertEqual(resp.status, 200)
        ctype = resp.getheader("Content-Type") or ""
        self.assertTrue(ctype.startswith("text/event-stream"), ctype)
        text = body.decode("utf-8")
        for name in ("start", "token", "done", "complete"):
            self.assertIn(f"event: {name}", text)
        self.assertIn('"account": "账号1"', text)
        self.assertIn('"account": "账号2"', text)

    def test_stream_error_isolation(self) -> None:
        """账号1 401 -> error 事件；账号2 ok -> done 事件；两不相扰。"""
        by_auth = {
            "Bearer sk-A": _error_response(
                401, '{"error":{"message":"invalid api key"}}'
            ),
            "Bearer sk-B": _stream_response(),
        }
        with _patched_factory(lambda: _RecordingClient(by_auth)):
            resp, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                    "stream": True,
                },
                headers=self.bearer(),
            )

        self.assertEqual(resp.status, 200)
        text = body.decode("utf-8")
        self.assertIn("event: error", text)
        self.assertIn('"account": "账号1"', text)
        self.assertIn('"code": "invalid"', text)
        self.assertIn("event: done", text)
        self.assertIn('"account": "账号2"', text)
        self.assertIn("event: complete", text)


class ModelTestValidationTest(_ModelTestProxyTestCase):
    """输入校验：畸形请求 400 中文错误；未知账号名不 400（只产生 fatal 结果）。"""

    def test_missing_prompt_400(self) -> None:
        resp, body = self.post_model_test(
            {"model": "m1", "accounts": ["账号1"]}, headers=self.bearer()
        )
        self.assertEqual(resp.status, 400)
        self.assertTrue(json.loads(body)["error"]["message"])

    def test_invalid_reasoning_effort_400(self) -> None:
        resp, body = self.post_model_test(
            {
                "model": "m1",
                "accounts": ["账号1"],
                "prompt": "p",
                "reasoning_effort": "x",
            },
            headers=self.bearer(),
        )
        self.assertEqual(resp.status, 400)
        self.assertTrue(json.loads(body)["error"]["message"])

    def test_empty_accounts_400(self) -> None:
        resp, body = self.post_model_test(
            {"model": "m1", "accounts": [], "prompt": "p"}, headers=self.bearer()
        )
        self.assertEqual(resp.status, 400)
        self.assertTrue(json.loads(body)["error"]["message"])

    def test_unknown_account_name_is_not_400(self) -> None:
        """未知账号名交给执行器产生 fatal 结果，而不是 400。"""
        with _patched_factory(lambda: _RecordingClient({})):
            resp, body = self.post_model_test(
                {"model": "m1", "accounts": ["幽灵账号"], "prompt": "你好"},
                headers=self.bearer(),
            )
        self.assertEqual(resp.status, 200)
        result = json.loads(body)["results"][0]
        self.assertEqual(result["account"], "幽灵账号")
        self.assertEqual(result["status"], "error")
        self.assertEqual((result["error"] or {}).get("code"), "fatal")

    def test_reasoning_effort_empty_string_accepted(self) -> None:
        clients: list[_RecordingClient] = []
        with _patched_factory(lambda: _RecordingClient({}), clients):
            resp, _ = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1"],
                    "prompt": "你好",
                    "reasoning_effort": "",
                },
                headers=self.bearer(),
            )
        self.assertEqual(resp.status, 200)
        # 空串应被规整为 None：载荷里没有 reasoning_effort
        self.assertNotIn("reasoning_effort", clients[0].calls[0]["json_body"])


class ModelTestKeyHygieneTest(_ModelTestProxyTestCase):
    """响应体（含流式）绝不回显明文 Key。"""

    def test_no_plaintext_key_in_non_stream_body(self) -> None:
        by_auth = {"Bearer sk-A": _ok_response(), "Bearer sk-B": _ok_response()}
        with _patched_factory(lambda: _RecordingClient(by_auth)):
            _, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                },
                headers=self.bearer(),
            )
        text = body.decode("utf-8")
        self.assertNotIn("sk-A", text)
        self.assertNotIn("sk-B", text)

    def test_no_plaintext_key_in_stream_body(self) -> None:
        by_auth = {
            "Bearer sk-A": _stream_response(),
            "Bearer sk-B": _stream_response(),
        }
        with _patched_factory(lambda: _RecordingClient(by_auth)):
            _, body = self.post_model_test(
                {
                    "model": "m1",
                    "accounts": ["账号1", "账号2"],
                    "prompt": "你好",
                    "stream": True,
                },
                headers=self.bearer(),
            )
        text = body.decode("utf-8")
        self.assertNotIn("sk-A", text)
        self.assertNotIn("sk-B", text)


class ModelTestRegressionTest(_ModelTestProxyTestCase):
    """同一台服务器上既有端点不受影响。"""

    def test_v1_chat_completions_still_works(self) -> None:
        headers = {
            "Content-Type": "application/json",
            **self.bearer(),
        }
        resp, _ = self.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {"messages": [{"role": "user", "content": "hi"}]},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
