"""单请求墙钟 deadline（``deadline_seconds``）的针对性测试。

用假上游构造，不发真实网络请求：
* 客户端层用 ``HttpClient(handler=...)`` 模拟慢上游；
* 网关层用鸭子类型的 rotator 直接抛 ``DeadlineExceeded``，验证错误响应与访问日志。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest

from st_rotator.client import (
    HttpClient,
    StRotator,
    begin_access_trace,
    end_access_trace,
)
from st_rotator.config import AccountConfig, Config, RateControlConfig
from st_rotator.errors import DeadlineExceeded
from st_rotator.proxy import create_server
from st_rotator.transport import Response


def _config(deadline: float, attempts: int = 1) -> Config:
    return Config(
        accounts=[AccountConfig(name="a", api_keys=["k-0123456789abcdef"])],
        rate_control=RateControlConfig(mode="off"),
        max_attempts=attempts,
        max_total_wait=0,
        deadline_seconds=deadline,
    )


def _slow_handler(delay: float):
    def handler(_request) -> Response:
        time.sleep(delay)
        body = json.dumps({"choices": [], "usage": {}}).encode("utf-8")
        return Response(200, {"content-type": "application/json"}, body)

    return handler


class DeadlineClientTest(unittest.TestCase):
    """客户端层：deadline 为 0 不生效；非 0 时到点抛 DeadlineExceeded。"""

    def _chat(self, deadline: float):
        rotator = StRotator(
            _config(deadline),
            client=HttpClient("http://127.0.0.1:9/v1", handler=_slow_handler(0.15)),
            logger=lambda _msg: None,
        )
        trace = begin_access_trace(deadline)
        try:
            rotator.chat([{"role": "user", "content": "hi"}])
            return trace
        finally:
            end_access_trace()

    def test_disabled_by_default(self) -> None:
        """deadline_seconds=0（默认）时不挂截止时刻，慢上游照旧返回。"""
        trace = self._chat(0)
        self.assertIsNone(trace.deadline_at)
        self.assertEqual(trace.attempts, 1)

    def test_nonstream_deadline_exceeded(self) -> None:
        with self.assertRaises(DeadlineExceeded):
            self._chat(0.05)


class _DeadlineRotator:
    """鸭子类型：chat 直接到点失败，流式先吐一块再失败。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    def chat(self, messages, model=None, **kwargs):
        raise DeadlineExceeded("超出单请求时限 0.05s")

    def chat_stream_raw(self, messages, model=None, **kwargs):
        def _gen():
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}
            raise DeadlineExceeded("超出单请求时限 0.05s")

        return _gen()


class DeadlineProxyTest(unittest.TestCase):
    """网关层：非流式 502 错误 JSON；流式补 error 事件；两者都记 complete=0。"""

    def setUp(self) -> None:
        self.logs: list[str] = []
        config = _config(0.05)
        self.server = create_server(
            _DeadlineRotator(config),
            host="127.0.0.1",
            port=0,
            log_sink=self.logs.append,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _post(self, *, stream: bool) -> tuple[int, str]:
        body = json.dumps(
            {
                "model": "fake-model",
                "stream": stream,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def _access_line(self) -> str:
        lines = [line for line in self.logs if "access rid=" in line]
        self.assertEqual(len(lines), 1, self.logs)
        return lines[0]

    def test_nonstream_deadline_returns_502(self) -> None:
        status, body = self._post(stream=False)
        self.assertEqual(status, 502)
        self.assertIn("超出单请求时限", body)
        line = self._access_line()
        self.assertIn("status=502", line)
        self.assertIn("err=DeadlineExceeded", line)
        self.assertIn("complete=0", line)

    def test_stream_deadline_emits_error_event(self) -> None:
        status, body = self._post(stream=True)
        self.assertEqual(status, 200)
        self.assertIn("data: [DONE]", body)
        self.assertIn('"error"', body)
        line = self._access_line()
        self.assertIn("status=200", line)
        self.assertIn("err=DeadlineExceeded", line)
        self.assertIn("complete=0", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
