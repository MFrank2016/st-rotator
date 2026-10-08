"""modeltest 并发模型测试执行器的单元测试。

全部通过假客户端（记录请求、吐出罐头响应）+ 假 DEFAULT_CLIENT_FACTORY 完成，
**不访问任何真实网络**。覆盖：payload 构造、非流式/流式成功、错误分类（invalid /
model / retry / fatal / network）、未知账号与空 Key 隔离、并发语义与返回顺序。
"""

import json
import threading
import unittest
from typing import Any, Callable, Sequence
from unittest import mock

from st_rotator import modeltest
from st_rotator.config import AccountConfig, Config
from st_rotator.transport import HttpClient, NetworkError, Response


# ---------------------------------------------------------------- 构造小工具


class _FakeClient:
    """假客户端：逐条吐出预设响应，并记录每次请求的关键信息。"""

    def __init__(self, responses: Sequence[Response]) -> None:
        self.responses = list(responses)
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
        return self.responses.pop(0)


class _BarrierClient(_FakeClient):
    """request 里先等 barrier——两个账号请求必须并发到达才算通过。"""

    def __init__(self, barrier: threading.Barrier, response: Response) -> None:
        super().__init__([response])
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


class _AuthKeyedClient(_FakeClient):
    """按 Authorization 头选取响应——让不同账号在同一批次里拿到不同响应。"""

    def __init__(self, by_auth: dict[str, Response]) -> None:
        super().__init__([])
        self._by_auth = by_auth

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
        return self._by_auth[(headers or {}).get("Authorization", "")]


class _RaisingClient(_FakeClient):
    """request 里直接抛异常（测 network 路径）。"""

    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self.exc = exc

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
        raise self.exc


def _config(accounts: Sequence[AccountConfig] | None = None) -> Config:
    """一份指向死端口的基础配置（绝不发真实网络）。"""
    return Config(
        base_url="http://127.0.0.1:9/v1",
        accounts=list(accounts)
        if accounts
        else [
            AccountConfig(name="账号1", api_keys=["sk-1", "sk-2"]),
            AccountConfig(name="账号2", api_keys=["sk-3"]),
        ],
    )


def _ok_response(**overrides: Any) -> Response:
    """非流式 200 响应。"""
    payload = {
        "choices": [{"message": {"content": "你好"}}],
        "usage": {"total_tokens": 3},
    }
    payload.update(overrides)
    return Response(200, {}, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _stream_response() -> Response:
    """OpenAI 风格 SSE 流式响应：两块文本 + 一个 usage + DONE。"""
    sse = (
        'data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "好"}}]}\n\n'
        'data: {"usage": {"total_tokens": 3}}\n\n'
        "data: [DONE]\n\n"
    )
    return Response(200, {}, sse.encode("utf-8"))


def _error_response(status: int, body: str) -> Response:
    return Response(status, {}, body.encode("utf-8"))


def _run_test(
    config: Config,
    req: modeltest.ModelTestRequest,
    clients: list[_FakeClient],
    make_factory: Callable[[Config], Callable[[], Any]],
    *,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[modeltest.ModelTestResult]:
    """用假工厂替换 DEFAULT_CLIENT_FACTORY 后运行 run_test（每次新客户端记入 clients）。"""
    with mock.patch.object(modeltest, "DEFAULT_CLIENT_FACTORY", make_factory):
        return modeltest.run_test(config, req, emit=emit)


def _single_factory(
    clients: list[_FakeClient], responses: Sequence[Response]
) -> Callable[[Config], Callable[[], Any]]:
    """每个账号一个新客户端，统一吐出同一条响应链。"""

    def make_factory(_config: Config) -> Callable[[], Any]:
        def factory() -> Any:
            client = _FakeClient(responses)
            clients.append(client)
            return client

        return factory

    return make_factory


# ---------------------------------------------------------------- 测试


class PayloadTest(unittest.TestCase):
    """RED：payload_for 尚不存在；GREEN：reasoning_effort 按规则写入载荷。"""

    def test_omits_reasoning_effort_when_none(self):
        payload = modeltest.payload_for("m1", "你好", None)
        self.assertEqual(
            payload,
            {"model": "m1", "messages": [{"role": "user", "content": "你好"}]},
        )
        self.assertNotIn("reasoning_effort", payload)

    def test_includes_reasoning_effort_for_all_values(self):
        # "none" 也要下发——它用于关闭上游的思考，不能当空值吞掉
        for value in ("none", "low", "medium", "high"):
            with self.subTest(value=value):
                payload = modeltest.payload_for("m1", "你好", value)
                self.assertEqual(payload["reasoning_effort"], value)
                self.assertEqual(payload["model"], "m1")

    def test_omits_reasoning_effort_for_empty_string(self):
        payload = modeltest.payload_for("m1", "你好", "")
        self.assertNotIn("reasoning_effort", payload)


class InterfaceTest(unittest.TestCase):
    """模块级接口匹配冻结契约。"""

    def test_constants_and_factory(self):
        self.assertEqual(modeltest.DETAIL_LIMIT, 200)
        self.assertEqual(
            modeltest.REASONING_EFFORT_VALUES, ("none", "low", "medium", "high")
        )
        self.assertEqual(modeltest.UPSTREAM_PATH, "chat/completions")
        factory = modeltest.build_client_factory(_config())
        client = factory()
        self.assertIsInstance(client, HttpClient)
        client.close()


class RunNonStreamTest(unittest.TestCase):
    """RED：run_test 尚不存在；GREEN：非流式成功 / 各类错误。"""

    def test_non_stream_ok(self):
        clients: list[_FakeClient] = []
        config = _config()
        req = modeltest.ModelTestRequest(
            model="m1",
            accounts=["账号1"],
            prompt="测试提示",
            reasoning_effort="none",
        )
        results = _run_test(
            config, req, clients, _single_factory(clients, [_ok_response()])
        )

        self.assertEqual([r.account for r in results], ["账号1"])
        result = results[0]
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.usage, {"total_tokens": 3})
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.latency_ms, 0)

        call = clients[0].calls[0]
        # 第一把 Key 作为 Authorization，且路径/请求体正确
        self.assertEqual(call["headers"], {"Authorization": "Bearer sk-1"})
        self.assertEqual(call["path"], "chat/completions")
        self.assertFalse(call["stream"])
        self.assertEqual(call["json_body"]["model"], "m1")
        self.assertEqual(
            call["json_body"]["messages"],
            [{"role": "user", "content": "测试提示"}],
        )
        self.assertEqual(call["json_body"]["reasoning_effort"], "none")

    def test_401_maps_to_invalid(self):
        clients: list[_FakeClient] = []
        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        result = _run_test(
            _config(),
            req,
            clients,
            _single_factory(
                clients,
                [_error_response(401, '{"error":{"message":"invalid api key"}}')],
            ),
        )[0]
        self.assertEqual(result.status, "error")
        self.assertEqual(
            result.error, {"code": "invalid", "message": "invalid api key"}
        )

    def test_model_unavailable_maps_to_model(self):
        clients: list[_FakeClient] = []
        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        result = _run_test(
            _config(),
            req,
            clients,
            _single_factory(
                clients,
                [
                    _error_response(
                        403,
                        '{"error":{"message":"model is not available in the current token plan"}}',
                    )
                ],
            ),
        )[0]
        self.assertEqual(
            result.error,
            {
                "code": "model",
                "message": "model is not available in the current token plan",
            },
        )

    def test_429_maps_to_retry(self):
        clients: list[_FakeClient] = []
        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        result = _run_test(
            _config(),
            req,
            clients,
            _single_factory(
                clients, [_error_response(429, '{"error":{"message":"rate limited"}}')]
            ),
        )[0]
        self.assertEqual(result.error, {"code": "retry", "message": "rate limited"})

    def test_long_error_message_truncated(self):
        clients: list[_FakeClient] = []
        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        body = '{"error":{"message":"%s"}}' % ("x" * 1000)
        result = _run_test(
            _config(),
            req,
            clients,
            _single_factory(clients, [_error_response(400, body)]),
        )[0]
        self.assertEqual(result.status, "error")
        err = result.error or {}
        self.assertEqual(err["code"], "fatal")
        self.assertLessEqual(len(err["message"]), 200)

    def test_network_error_maps_to_network(self):
        config = _config()
        clients: list[_FakeClient] = []

        def make_factory(_config: Config) -> Callable[[], Any]:
            def factory() -> Any:
                client = _RaisingClient(NetworkError("connection refused"))
                clients.append(client)
                return client

            return factory

        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        result = _run_test(config, req, clients, make_factory)[0]
        self.assertEqual(result.status, "error")
        self.assertEqual((result.error or {}).get("code"), "network")
        self.assertEqual(len(clients[0].calls), 1)

    def test_oserror_maps_to_network(self):
        config = _config()
        clients: list[_FakeClient] = []

        def make_factory(_config: Config) -> Callable[[], Any]:
            def factory() -> Any:
                client = _RaisingClient(OSError("socket closed"))
                clients.append(client)
                return client

            return factory

        req = modeltest.ModelTestRequest(model="m1", accounts=["账号1"], prompt="p")
        result = _run_test(config, req, clients, make_factory)[0]
        self.assertEqual((result.error or {}).get("code"), "network")


class IsolationTest(unittest.TestCase):
    """账号级隔离：一个账号的失败 / 缺失绝不连累其它账号。"""

    def test_unknown_account_isolated(self):
        clients: list[_FakeClient] = []
        config = _config()
        req = modeltest.ModelTestRequest(
            model="m1", accounts=["账号1", "幽灵账号"], prompt="p"
        )
        results = _run_test(
            config, req, clients, _single_factory(clients, [_ok_response()])
        )

        self.assertEqual([r.account for r in results], ["账号1", "幽灵账号"])
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[0].text, "你好")
        self.assertEqual(results[1].status, "error")
        err = results[1].error or {}
        self.assertEqual(err["code"], "fatal")
        self.assertIn("账号不存在", err["message"])
        self.assertIn("幽灵账号", err["message"])
        # 未知账号不发网络请求：只有账号 1 真正调用了 request
        total_calls = sum(len(c.calls) for c in clients)
        self.assertEqual(total_calls, 1)

    def test_empty_api_keys_isolated(self):
        empty = object.__new__(AccountConfig)
        empty.name = "空账号"
        empty.api_keys = []
        config = _config([AccountConfig(name="账号1", api_keys=["sk-1"]), empty])
        clients: list[_FakeClient] = []
        req = modeltest.ModelTestRequest(
            model="m1", accounts=["账号1", "空账号"], prompt="p"
        )
        results = _run_test(
            config, req, clients, _single_factory(clients, [_ok_response()])
        )

        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[1].status, "error")
        err = results[1].error or {}
        self.assertEqual(err["code"], "fatal")
        self.assertEqual(err["message"], "空账号 无可用 Key")
        total_calls = sum(len(c.calls) for c in clients)
        self.assertEqual(total_calls, 1)

    def test_non_stream_error_does_not_break_sibling(self):
        config = _config(
            [
                AccountConfig(name="坏账号", api_keys=["sk-bad"]),
                AccountConfig(name="好账号", api_keys=["sk-good"]),
            ]
        )
        by_auth = {
            "Bearer sk-bad": _error_response(
                401, '{"error":{"message":"invalid api key"}}'
            ),
            "Bearer sk-good": _ok_response(),
        }
        clients: list[_FakeClient] = []

        def make_factory(_config: Config) -> Callable[[], Any]:
            def factory() -> Any:
                client = _AuthKeyedClient(by_auth)
                clients.append(client)
                return client

            return factory

        req = modeltest.ModelTestRequest(
            model="m1", accounts=["坏账号", "好账号"], prompt="p"
        )
        results = _run_test(config, req, clients, make_factory)
        self.assertEqual([r.account for r in results], ["坏账号", "好账号"])
        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[1].status, "ok")
        self.assertEqual(results[1].text, "你好")


class ConcurrencyTest(unittest.TestCase):
    """并发语义：账号并行执行，结果仍按请求顺序返回。"""

    def test_multi_account_concurrent_and_ordered(self):
        config = _config()
        barrier = threading.Barrier(2)
        clients: list[_FakeClient] = []

        def make_factory(_config: Config) -> Callable[[], Any]:
            def factory() -> Any:
                client = _BarrierClient(barrier, _ok_response())
                clients.append(client)
                return client

            return factory

        # 请求顺序故意与配置顺序不同：结果必须按请求顺序返回
        req = modeltest.ModelTestRequest(
            model="m1", accounts=["账号2", "账号1"], prompt="p"
        )
        results = _run_test(config, req, clients, make_factory)

        self.assertEqual([r.account for r in results], ["账号2", "账号1"])
        self.assertTrue(all(r.status == "ok" for r in results))
        self.assertEqual(len(clients), 2)
        # 每个账号各发了一发
        self.assertEqual([len(c.calls) for c in clients], [1, 1])


class StreamTest(unittest.TestCase):
    """流式路径：SSE 事件序列与文本拼接。"""

    def test_stream_emit_sequence(self):
        config = _config([AccountConfig(name="账号1", api_keys=["sk-1"])])
        clients: list[_FakeClient] = []
        events: list[dict[str, Any]] = []
        req = modeltest.ModelTestRequest(
            model="m1", accounts=["账号1"], prompt="p", stream=True
        )
        results = _run_test(
            config,
            req,
            clients,
            _single_factory(clients, [_stream_response()]),
            emit=events.append,
        )

        result = results[0]
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "你好")
        self.assertTrue(clients[0].calls[0]["stream"])

        # 单账号：事件顺序完全确定
        self.assertEqual(
            [e["type"] for e in events],
            ["start", "token", "token", "usage", "done", "complete"],
        )
        self.assertEqual(
            events[0], {"type": "start", "account": "账号1", "model": "m1"}
        )
        self.assertEqual(
            events[1], {"type": "token", "account": "账号1", "token": "你"}
        )
        self.assertEqual(
            events[2], {"type": "token", "account": "账号1", "token": "好"}
        )
        self.assertEqual(
            events[3],
            {"type": "usage", "account": "账号1", "usage": {"total_tokens": 3}},
        )
        self.assertEqual(events[4]["type"], "done")
        self.assertEqual(events[4]["status"], "ok")
        self.assertGreaterEqual(events[4]["latency_ms"], 0)
        self.assertEqual(events[5], {"type": "complete"})

    def test_stream_error_isolation(self):
        config = _config(
            [
                AccountConfig(name="坏账号", api_keys=["sk-bad"]),
                AccountConfig(name="好账号", api_keys=["sk-good"]),
            ]
        )
        by_auth = {
            "Bearer sk-bad": _error_response(
                401, '{"error":{"message":"invalid api key"}}'
            ),
            "Bearer sk-good": _stream_response(),
        }
        clients: list[_FakeClient] = []
        events: list[dict[str, Any]] = []

        def make_factory(_config: Config) -> Callable[[], Any]:
            def factory() -> Any:
                client = _AuthKeyedClient(by_auth)
                clients.append(client)
                return client

            return factory

        req = modeltest.ModelTestRequest(
            model="m1", accounts=["坏账号", "好账号"], prompt="p", stream=True
        )
        results = _run_test(config, req, clients, make_factory, emit=events.append)

        self.assertEqual([r.account for r in results], ["坏账号", "好账号"])
        self.assertEqual(results[0].status, "error")
        self.assertEqual((results[0].error or {}).get("code"), "invalid")
        self.assertEqual(results[1].status, "ok")
        self.assertEqual(results[1].text, "你好")

        good_events = [e for e in events if e.get("account") == "好账号"]
        self.assertEqual(
            [e["type"] for e in good_events],
            ["start", "token", "token", "usage", "done"],
        )
        bad_errors = [
            e for e in events if e.get("account") == "坏账号" and e["type"] == "error"
        ]
        self.assertEqual(bad_errors[0]["code"], "invalid")
        self.assertEqual(events[-1], {"type": "complete"})


if __name__ == "__main__":
    unittest.main()
