"""批次 3（proxy.py）：流式 usage 传给子 Key、stream_options 类型校验、多重编码穿越。"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from st_rotator.config import AccountConfig, Config, RateControlConfig
from st_rotator.keypool import user_context
from st_rotator.proxy import _has_parent_segment, create_server
from st_rotator.subkeys import SubKeyManager


class _FakePool:
    def summary(self) -> dict:
        return {"total": 0, "available": 0}

    def snapshot(self, *, include_users: bool = False) -> list:
        return []

    def user_context(self, label: str):
        return user_context(label)


class _StreamRotator:
    """chat_stream_raw 吐固定分片；chunks 里可以带 usage 块。"""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.pool = _FakePool()
        self.config = Config(
            accounts=[AccountConfig(name="a", api_keys=["k-0123456789abcdef"])],
            rate_control=RateControlConfig(mode="off"),
        )

    def chat_stream_raw(self, messages, model=None, **kwargs):
        yield from self.chunks

    def status(self) -> dict:
        return {"status": "ok"}


class StreamUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = SubKeyManager(
            Path(self._tmp.name) / "subkeys.json", autosave_interval=0
        )
        self.addCleanup(self.manager.close)
        self.sub = self.manager.create("app")

    def _serve(self, chunks: list[dict]):
        self.server = create_server(
            _StreamRotator(chunks),
            host="127.0.0.1",
            port=0,
            token=None,
            subkeys=self.manager,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _chat(self, extra: dict | None = None) -> tuple[int, bytes]:
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        body.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps(body),
                headers={
                    "Authorization": f"Bearer {self.sub.key}",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_stream_usage_is_recorded_on_subkey(self) -> None:
        self._serve(
            [
                {"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
            ]
        )
        status, _data = self._chat()
        self.assertEqual(status, 200)
        usage = self.manager.snapshot()[0]["usage"]
        self.assertEqual(usage["prompt_tokens"], 5)
        self.assertEqual(usage["completion_tokens"], 7)

    def test_stream_without_usage_falls_back_to_estimate(self) -> None:
        self._serve([{"choices": [{"delta": {"content": "hello world"}}]}])
        status, _data = self._chat()
        self.assertEqual(status, 200)
        usage = self.manager.snapshot()[0]["usage"]
        self.assertEqual(usage["completion_tokens"], 0)
        self.assertGreater(usage["estimated_completion_tokens"], 0)

    def test_non_object_stream_options_is_rejected(self) -> None:
        self._serve([{"choices": [{"delta": {"content": "hi"}}]}])
        status, data = self._chat({"stream_options": "abc"})
        self.assertEqual(status, 400)
        self.assertIn(b"stream_options", data)


class ParentSegmentTest(unittest.TestCase):
    def test_single_and_multi_encoded_traversal(self) -> None:
        for path in (
            "/v1/../etc/passwd",
            "/v1/%2e%2e/etc",
            "/v1/%252e%252e/etc",
            "/v1/%2525252e%2525252e/etc",
            "/v1/..%2fetc",
        ):
            self.assertTrue(_has_parent_segment(path), path)

    def test_normal_paths_are_allowed(self) -> None:
        for path in ("/v1/models", "/v1/chat/completions", "/v1/a..b", "/v1/...."):
            self.assertFalse(_has_parent_segment(path), path)

    def test_over_encoded_input_is_rejected(self) -> None:
        # 每多一层就把 % 再编码成 %25：%2e -> %252e -> %25252e ...
        token = "%2e"
        for _ in range(12):
            token = "%25" + token[1:]
        self.assertTrue(_has_parent_segment("/v1/" + token))


if __name__ == "__main__":
    unittest.main()
