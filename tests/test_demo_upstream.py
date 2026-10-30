"""FakeUpstream：/models 也遵守鉴权/限流开关；计数器并发下不丢更新。"""

from __future__ import annotations

import threading
import unittest

from st_rotator.demo import FakeUpstream
from st_rotator.transport import Request


def _request(key: str, path: str = "/v1/models") -> Request:
    return Request("GET", path, {"Authorization": f"Bearer {key}"}, None)


class ModelsEndpointPolicyTest(unittest.TestCase):
    def test_invalid_key_on_models(self) -> None:
        upstream = FakeUpstream(invalid_keys={"bad"}, latency=(0.0, 0.0))
        self.assertEqual(upstream.handle(_request("bad")).status, 401)

    def test_always_limited_key_on_models(self) -> None:
        upstream = FakeUpstream(always_limited={"lim"}, latency=(0.0, 0.0))
        resp = upstream.handle(_request("lim"))
        self.assertEqual(resp.status, 429)
        self.assertEqual(upstream.rate_limited_total, 1)

    def test_auth_as_429_key_on_models(self) -> None:
        upstream = FakeUpstream(auth_as_429={"fake"}, latency=(0.0, 0.0))
        resp = upstream.handle(_request("fake"))
        self.assertEqual(resp.status, 429)
        self.assertIn(b"authentication failed", resp.content)
        self.assertEqual(upstream.rate_limited_total, 1)

    def test_healthy_key_on_models(self) -> None:
        upstream = FakeUpstream(latency=(0.0, 0.0), flaky_rate=0.0)
        self.assertEqual(upstream.handle(_request("ok-key")).status, 200)

    def test_chat_endpoint_still_works(self) -> None:
        upstream = FakeUpstream(latency=(0.0, 0.0), flaky_rate=0.0)
        resp = upstream.handle(
            Request(
                "POST",
                "/v1/chat/completions",
                {"Authorization": "Bearer ok"},
                b'{"stream": false}',
            )
        )
        self.assertEqual(resp.status, 200)


class CounterConcurrencyTest(unittest.TestCase):
    def test_hits_and_rate_limited_are_not_lost(self) -> None:
        upstream = FakeUpstream(always_limited={"k"}, latency=(0.0, 0.0))
        threads_n, per_thread = 8, 200
        barrier = threading.Barrier(threads_n)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                upstream.handle(_request("k"))

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(upstream.hits["k"], threads_n * per_thread)
        self.assertEqual(upstream.rate_limited_total, threads_n * per_thread)


if __name__ == "__main__":
    unittest.main()
