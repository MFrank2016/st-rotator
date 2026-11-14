"""离线契约测试：Anthropic 端点路由、凭据头不透传、清洗生效（不联网）。

起真实 ``ThreadingHTTPServer``，上游用 ``HttpClient(handler=...)`` 的假实现接住，
只断言"网关发给上游的那一份请求"，绝不发真实网络请求。
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest

from st_rotator.client import StRotator
from st_rotator.config import (
    AccountConfig,
    Config,
    ModelFilterConfig,
    RateControlConfig,
)
from st_rotator.proxy import create_server
from st_rotator.transport import HttpClient, Request, Response

GATEWAY_TOKEN = "tk-regression"
POOL_KEY = "sk-upstream-key-1"
ALLOWED_MODEL = "deepseek-v4-flash"


class _Trap:
    """假上游：记录收到的请求头与请求体，回一个成功响应。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, req: Request) -> Response:
        with self.lock:
            self.calls.append(
                {
                    "path": req.path,
                    "headers": dict(req.headers),
                    "body": (req.body or b"").decode("utf-8", "replace"),
                }
            )
        path = (req.path or "").rstrip("/")
        if path.endswith("messages"):
            payload = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": ALLOWED_MODEL,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                },
            }
        else:
            payload = {
                "object": "list",
                "data": [
                    {"id": ALLOWED_MODEL, "object": "model"},
                    {"id": "model-not-allowed", "object": "model"},
                ],
            }
        return Response(
            200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
        )

    def last(self) -> dict | None:
        with self.lock:
            return self.calls[-1] if self.calls else None


class GatewayContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.trap = _Trap()
        config = Config(
            base_url="http://127.0.0.1:9/v1",
            default_model=ALLOWED_MODEL,
            accounts=[AccountConfig(name="a1", api_keys=[POOL_KEY], max_concurrency=4)],
            rate_control=RateControlConfig(mode="off"),
            model_filter=ModelFilterConfig(available=[ALLOWED_MODEL]),
            max_attempts=2,
        )
        self.rotator = StRotator(
            config,
            client=HttpClient(config.base_url, handler=self.trap),
            logger=lambda _line: None,
        )
        self.server = create_server(
            self.rotator, host="127.0.0.1", port=0, token=GATEWAY_TOKEN
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, body=None, headers=None
    ) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=payload, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def _post(self, path: str, body, headers) -> tuple[int, str]:
        return self._request("POST", path, body, headers)

    def _lower_headers(self, call: dict) -> dict:
        return {k.lower(): v for k, v in call["headers"].items()}

    # ---- (a) Anthropic 路径凭据头不透传，改用池 Key 的 x-api-key ----
    def test_anthropic_uses_pool_key_and_drops_caller_credentials(self) -> None:
        message = {
            "model": ALLOWED_MODEL,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
        status, body = self._post(
            "/anthropic/v1/messages",
            message,
            {
                "x-api-key": GATEWAY_TOKEN,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        self.assertEqual(status, 200, body[:200])
        call = self.trap.last()
        self.assertIsNotNone(call)
        headers = self._lower_headers(call)
        self.assertEqual(headers.get("x-api-key"), POOL_KEY)
        self.assertNotIn("authorization", headers)
        self.assertNotIn(GATEWAY_TOKEN, json.dumps(headers))

    # ---- (b) OpenAI 路径双凭据时，上游只拿到池 Key 的 Authorization ----
    def test_openai_path_forwards_pool_key_only(self) -> None:
        self._post(
            "/v1/chat/completions",
            {"model": ALLOWED_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            {
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "x-api-key": GATEWAY_TOKEN,
                "content-type": "application/json",
            },
        )
        call = self.trap.last()
        self.assertIsNotNone(call)
        headers = self._lower_headers(call)
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(headers.get("authorization"), f"Bearer {POOL_KEY}")

    # ---- (c) assistant.content=null 经网关后被清洗成 "" ----
    def test_null_content_sanitized_before_upstream(self) -> None:
        message = {
            "model": ALLOWED_MODEL,
            "max_tokens": 16,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None},
            ],
        }
        status, body = self._post(
            "/anthropic/v1/messages",
            message,
            {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"},
        )
        self.assertEqual(status, 200, body[:200])
        sent = json.loads(self.trap.last()["body"])
        self.assertEqual(sent["messages"][1]["content"], "")

    # ---- (d) 白名单外模型 → 400 且是 Anthropic 错误形状 ----
    def test_blocked_model_returns_anthropic_error(self) -> None:
        status, body = self._post(
            "/anthropic/v1/messages",
            {
                "model": "model-not-allowed",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"},
        )
        self.assertEqual(status, 400, body[:200])
        parsed = json.loads(body)
        self.assertEqual(parsed.get("type"), "error")
        self.assertEqual(parsed["error"]["type"], "invalid_request_error")
        # 白名单拦截不该打到上游
        self.assertIsNone(self.trap.last())

    # ---- (e) 未知 /anthropic/ 路径 → 404 Anthropic 错误形状 ----
    def test_unknown_anthropic_route_404(self) -> None:
        status, body = self._post(
            "/anthropic/unknown",
            {"model": ALLOWED_MODEL},
            {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"},
        )
        self.assertEqual(status, 404, body[:200])
        parsed = json.loads(body)
        self.assertEqual(parsed.get("type"), "error")
        self.assertEqual(parsed["error"]["type"], "not_found_error")

    # ---- (f) GET /anthropic/v1/models 只含白名单模型且带 type/display_name ----
    def test_anthropic_models_filtered_and_annotated(self) -> None:
        status, body = self._request(
            "GET",
            "/anthropic/v1/models",
            headers={"x-api-key": GATEWAY_TOKEN},
        )
        self.assertEqual(status, 200, body[:200])
        parsed = json.loads(body)
        ids = [item["id"] for item in parsed["data"]]
        self.assertEqual(ids, [ALLOWED_MODEL])
        self.assertEqual(parsed["data"][0]["type"], "model")
        self.assertEqual(parsed["data"][0]["display_name"], ALLOWED_MODEL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
