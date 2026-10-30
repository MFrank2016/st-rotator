"""HttpClient 的复用连接重试策略：只对幂等方法重发，POST 绝不重发。"""

from __future__ import annotations

import http.client
import unittest

from st_rotator.transport import HttpClient, NetworkError


class _FakeRaw:
    status = 200

    def getheaders(self):
        return []

    def read(self) -> bytes:
        return b"ok"

    def getheader(self, name, default=None):
        return default


class _FakeConn:
    """可配置的假连接：``fail=True`` 时 request 抛 RemoteDisconnected（模拟对端已关）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.methods: list[str] = []
        self.closed = False
        self.sock = object()  # _checkin 只判断非 None

    def request(self, method, url, body=None, headers=None):
        self.methods.append(method)
        if self.fail:
            raise http.client.RemoteDisconnected("peer closed")

    def getresponse(self):
        return _FakeRaw()

    def close(self) -> None:
        self.closed = True


def _no_reconnect():
    raise AssertionError("非幂等请求不允许重连重发")


class ReusedConnectionRetryTest(unittest.TestCase):
    def _client(self, max_connections: int = 1) -> HttpClient:
        return HttpClient(
            "http://127.0.0.1:9/v1",
            timeout=1.0,
            connect_timeout=1.0,
            max_connections=max_connections,
        )

    def test_idempotent_get_retries_on_reused_connection(self) -> None:
        client = self._client()
        stale = _FakeConn(fail=True)
        fresh = _FakeConn()
        client._checkout = lambda: (stale, True)  # type: ignore[method-assign]
        client._new_connection = lambda: fresh  # type: ignore[method-assign]
        resp = client.request("GET", "/models")
        self.assertEqual(resp.status, 200)
        self.assertEqual(fresh.methods, ["GET"])
        self.assertTrue(stale.closed)

    def test_post_is_not_retried_on_reused_connection(self) -> None:
        client = self._client()
        stale = _FakeConn(fail=True)
        client._checkout = lambda: (stale, True)  # type: ignore[method-assign]
        client._new_connection = _no_reconnect  # type: ignore[method-assign]
        with self.assertRaises(NetworkError):
            client.request("POST", "/chat/completions", json_body={"a": 1})
        self.assertTrue(stale.closed)

    def test_new_connection_failure_is_not_retried(self) -> None:
        client = self._client()
        stale = _FakeConn(fail=True)
        client._checkout = lambda: (stale, False)  # type: ignore[method-assign]
        client._new_connection = _no_reconnect  # type: ignore[method-assign]
        with self.assertRaises(NetworkError):
            client.request("GET", "/models")

    def test_semaphore_released_after_refused_retry(self) -> None:
        client = self._client(max_connections=1)
        client._checkout = lambda: (_FakeConn(fail=True), True)  # type: ignore[method-assign]
        client._new_connection = _no_reconnect  # type: ignore[method-assign]
        with self.assertRaises(NetworkError):
            client.request("POST", "/chat/completions", json_body={})
        # 额度没被吞掉：下一次请求还能立刻拿到（否则这里会永久阻塞）
        self.assertEqual(client._sem._value, 1)


if __name__ == "__main__":
    unittest.main()
