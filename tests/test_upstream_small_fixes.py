"""上游吸收批次 1：数值参数转换、透传端点清洗、凭据头剔除、403 歧义归类。

网关用例起真实 ``ThreadingHTTPServer``，用假 rotator 记录上游实收内容，
绝不访问网络。
"""

from __future__ import annotations

import http.client
import http.server
import json
import socketserver
import threading
import unittest
from unittest import mock

from st_rotator import proxy as proxy_mod
from st_rotator.client import classify
from st_rotator.keypool import user_context
from st_rotator.proxy import RotatorProxyHandler, _sanitize_params, create_server
from st_rotator.transport import Response

TOKEN = "testtoken123456"


class _Headers:
    """只提供 ``items()`` 的假 headers 容器。"""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self):
        return list(self._pairs)


class SanitizeParamsTest(unittest.TestCase):
    """数值参数字符串转数字：可解析→float，不可解析→原样，数字→不变。"""

    def test_numeric_strings_converted_to_float(self) -> None:
        params = {
            "temperature": "0.7",
            "top_p": "1",
            "frequency_penalty": "0.5",
            "presence_penalty": " -0.25 ",
        }
        _sanitize_params(params)
        self.assertEqual(
            params,
            {
                "temperature": 0.7,
                "top_p": 1.0,
                "frequency_penalty": 0.5,
                "presence_penalty": -0.25,
            },
        )
        self.assertIsInstance(params["temperature"], float)

    def test_unparsable_strings_kept_as_is(self) -> None:
        params = {"temperature": "hot", "top_p": ""}
        _sanitize_params(params)
        self.assertEqual(params, {"temperature": "hot", "top_p": ""})

    def test_numbers_and_other_types_untouched(self) -> None:
        params = {"temperature": 3, "top_p": None, "frequency_penalty": [0.5]}
        _sanitize_params(params)
        self.assertEqual(
            params, {"temperature": 3, "top_p": None, "frequency_penalty": [0.5]}
        )

    def test_unsupported_fields_removed(self) -> None:
        params = {
            "logprobs": True,
            "top_logprobs": 3,
            "metadata": {"a": 1},
            "model": "m",
        }
        _sanitize_params(params)
        self.assertEqual(params, {"model": "m"})

    def test_max_tokens_string_untouched(self) -> None:
        # max_tokens 不在转换名单里：下限抬升逻辑依赖它保持整数
        params = {"max_tokens": "100"}
        _sanitize_params(params)
        self.assertEqual(params, {"max_tokens": "100"})


class ForwardableHeadersTest(unittest.TestCase):
    """凭据头（大小写不敏感）不透传，追踪头照旧。"""

    class _Fake:
        """极简 handler 替身：``_forwardable_headers`` 只用到 ``headers.items()``。"""

        def __init__(self, pairs: list[tuple[str, str]]) -> None:
            self.headers = _Headers(pairs)

    def test_credential_headers_dropped(self) -> None:
        fake = self._Fake(
            [
                ("X-Api-Key", "sk-caller"),
                ("Authorization", f"Bearer {TOKEN}"),
                ("Proxy-Authorization", "Basic x"),
                ("Cookie", "session=abc"),
                ("X-Request-Id", "rid-1"),
                ("X-Custom", "keep"),
            ]
        )
        headers = RotatorProxyHandler._forwardable_headers(fake)  # type: ignore[arg-type]
        lowered = {key.lower() for key in headers}
        for dropped in ("x-api-key", "authorization", "proxy-authorization", "cookie"):
            self.assertNotIn(dropped, lowered)
        self.assertEqual(headers["X-Request-Id"], "rid-1")
        self.assertEqual(headers["X-Custom"], "keep")


class Classify403Test(unittest.TestCase):
    """403 只在错误体指向鉴权时判失效，否则按模型/端点不可用换 Key。"""

    @staticmethod
    def _response(status: int) -> Response:
        return Response(status, {}, b"")

    def test_403_with_auth_hint_is_invalid(self) -> None:
        action, wait = classify(self._response(403), '{"error":"permission denied"}')
        self.assertEqual(action, "invalid")
        self.assertIsNone(wait)

    def test_403_without_auth_hint_is_model(self) -> None:
        action, _ = classify(self._response(403), '{"error":"Forbidden"}')
        self.assertEqual(action, "model")

    def test_401_still_invalid(self) -> None:
        action, _ = classify(self._response(401), '{"error":"Forbidden"}')
        self.assertEqual(action, "invalid")

    def test_model_unavailable_hint_checked_first(self) -> None:
        action, _ = classify(
            self._response(403),
            '{"error":"model is not available in the current token plan"}',
        )
        self.assertEqual(action, "model")


class _FakeConfig:
    deadline_seconds = 30.0
    min_max_tokens = 0
    track_stream_usage = False


class _FakePool:
    def user_context(self, label: str):
        return user_context(label)


class _FakeRotator:
    """只提供透传端点需要的表面；``request`` 记录上游实收的请求体。"""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.pool = _FakePool()
        self.sent: list[tuple] = []

    def model_block_reason(self, model) -> str:
        return ""

    def request(self, path, *, method="POST", json_body=None, headers=None):
        self.sent.append((path, method, json_body, headers))
        return {"ok": True}


class _GatewayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rotator = _FakeRotator()
        self.server = create_server(self.rotator, host="127.0.0.1", port=0, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path: str, body) -> int:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            conn.request(
                "POST",
                path,
                body=raw,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "X-Request-Id": "rid-1",
                },
            )
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()


class ForwardSimpleSanitizeTest(_GatewayCase):
    """透传端点（/v1/responses 等）执行与 chat 相同的清洗。"""

    def test_passthrough_body_sanitized(self) -> None:
        body = {
            "model": "m",
            "input": [
                {"role": "developer", "content": None},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}],
                },
            ],
            "messages": [
                {"role": "developer", "content": "x"},
                {"role": "assistant", "content": None},
            ],
            "logprobs": True,
            "top_logprobs": 3,
            "metadata": {"a": 1},
            "temperature": "0.7",
            "top_p": "0.9",
        }
        self.assertEqual(self.post("/v1/responses", body), 200)
        _, _, forwarded, _ = self.rotator.sent[0]
        self.assertEqual(
            forwarded["messages"],
            [
                {"role": "system", "content": "x"},
                {"role": "assistant", "content": ""},
            ],
        )
        for field in ("logprobs", "top_logprobs", "metadata"):
            self.assertNotIn(field, forwarded)
        self.assertEqual(forwarded["temperature"], 0.7)
        self.assertEqual(forwarded["top_p"], 0.9)
        # 非 messages 字段（如 responses 的 input）不动
        self.assertEqual(forwarded["input"], body["input"])

    def test_non_object_body_forwarded_as_is(self) -> None:
        self.assertEqual(self.post("/v1/embeddings", ["not", "an", "object"]), 200)
        _, _, forwarded, _ = self.rotator.sent[0]
        self.assertEqual(forwarded, ["not", "an", "object"])


class ListenQueueTest(unittest.TestCase):
    """backlog 必须在**构造期间**就传给 ``socket.listen``。

    ``socketserver.TCPServer.__init__`` 里就会调用 ``server_activate()`` →
    ``socket.listen(self.request_queue_size)``。只断言实例属性 == 128 会让
    「构造之后再赋值」的错误实现也通过，所以这里拦住 server_activate 实际
    读到的那个值。

    继承来的默认值先换成哨兵：不同解释器给 TCPServer 的默认 backlog 不一样
    （Python 3.12 是 5，3.15 起是 128），不换的话测试在 3.15 上会因为
    "默认值刚好等于 128" 而放过旧实现。换掉之后，读到的 128 只可能来自
    网关自己声明的类属性。
    """

    def test_backlog_effective_at_construction(self) -> None:
        seen: list[int] = []
        original = socketserver.TCPServer.server_activate

        def spy(self, *args, **kwargs):  # noqa: ANN001
            seen.append(self.request_queue_size)
            return original(self, *args, **kwargs)

        sentinel = 4242
        with (
            mock.patch.object(socketserver.TCPServer, "request_queue_size", sentinel),
            mock.patch.object(
                http.server.HTTPServer, "request_queue_size", sentinel, create=True
            ),
            mock.patch.object(socketserver.TCPServer, "server_activate", spy),
        ):
            server = create_server(
                _FakeRotator(), host="127.0.0.1", port=0, token=TOKEN
            )
        try:
            self.assertEqual(seen, [128])
        finally:
            server.server_close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
