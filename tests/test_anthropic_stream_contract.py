"""Anthropic 流式端到端契约测试：真实本地假上游 + 真实网关。

为什么另开一个文件：``tests/test_gateway_contract.py`` 用 ``HttpClient(handler=...)``
只能返回已完整读入内存的 ``Response``，无法模拟 ``StreamResponse``，所以
``_anthropic_stream`` 的事件透传路径一直没被端到端覆盖。这里把假上游换成
127.0.0.1 上真实的 ``http.server``，让它按 Anthropic SSE 逐条 flush 写出，
客户端按 EOF 结束——整条 网关 → 上游 → 客户端 的流式链路就真跑起来了。

不联网，只用 127.0.0.1。
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from st_rotator import anthropic
from st_rotator.client import StRotator
from st_rotator.config import AccountConfig, Config, RateControlConfig
from st_rotator.proxy import create_server
from st_rotator.transport import HttpClient

GATEWAY_TOKEN = "tk-stream-regression"
POOL_KEY = "sk-upstream-stream-key"
MODEL = "deepseek-v4-flash"


def _sse_events() -> list[tuple[str, dict]]:
    """契约要求的 Anthropic 事件序列（顺序即客户端状态机驱动的顺序）。"""
    return [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "你好"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def _upstream_handler_class(recorded: list, lock: threading.Lock):
    """假上游：记录请求，然后以 SSE 逐条 flush 回应 ``POST /v1/messages``。"""

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = (
            "HTTP/1.0"  # 不设 Content-Length，写完关连接，客户端按 EOF 结束
        )

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            with lock:
                recorded.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": raw.decode("utf-8", "replace"),
                    }
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for name, payload in _sse_events():
                frame = (
                    f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                ).encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.01)
            self.close_connection = True

        def log_message(self, *args: object) -> None:
            pass

    return H


class AnthropicStreamContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded: list[dict] = []
        self.recorded_lock = threading.Lock()
        self.upstream = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _upstream_handler_class(self.recorded, self.recorded_lock)
        )
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        upstream_base = f"http://127.0.0.1:{self.upstream.server_address[1]}/v1"

        config = Config(
            base_url=upstream_base,
            default_model=MODEL,
            accounts=[AccountConfig(name="a1", api_keys=[POOL_KEY], max_concurrency=4)],
            rate_control=RateControlConfig(mode="off"),
            max_attempts=2,
        )
        # proxy="" 必须显式给：transport 默认会读 HTTPS_PROXY 环境变量，
        # 测试机上若设了代理，流量会绕过本地假上游。
        self.rotator = StRotator(
            config,
            client=HttpClient(
                upstream_base, proxy="", timeout=10.0, connect_timeout=5.0
            ),
            logger=lambda _line: None,
        )
        self.gateway = create_server(
            self.rotator, host="127.0.0.1", port=0, token=GATEWAY_TOKEN
        )
        self.gateway_thread = threading.Thread(
            target=self.gateway.serve_forever, daemon=True
        )
        self.gateway_thread.start()
        self.gateway_port = self.gateway.server_address[1]

    def tearDown(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)

    def test_streaming_events_passthrough(self) -> None:
        body = json.dumps(
            {
                "model": MODEL,
                "max_tokens": 16,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/anthropic/v1/messages",
            data=body,
            method="POST",
            headers={
                "x-api-key": GATEWAY_TOKEN,
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
            text = response.read().decode("utf-8", "replace")

        # ---- 事件序列与内容 ----
        events = anthropic.iter_sse_events(text)
        names = [name for name, _data in events]
        self.assertEqual(
            names,
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        for name, data in events:
            payload = json.loads(data)
            self.assertEqual(payload["type"], name)
        delta = json.loads(
            next(data for name, data in events if name == "content_block_delta")
        )
        self.assertEqual(delta["delta"]["text"], "你好")

        # ---- 上游收到的凭据与请求体 ----
        with self.recorded_lock:
            self.assertEqual(len(self.recorded), 1)
            call = self.recorded[0]
        self.assertEqual(call["path"], "/v1/messages")
        self.assertEqual(call["headers"].get("x-api-key"), POOL_KEY)
        self.assertNotIn("authorization", call["headers"])
        sent = json.loads(call["body"])
        self.assertIs(sent["stream"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
