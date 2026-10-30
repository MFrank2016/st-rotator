"""批次 3（transport.py）：close 后归还连接、信号量排队计入 deadline、流式读取异常包装。"""

from __future__ import annotations

import http.client
import time
import unittest

from st_rotator.transport import HttpClient, NetworkError, StreamResponse


class _FakeConn:
    def __init__(self) -> None:
        self.sock = object()
        self.closed = False

    def request(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _BadRaw:
    """readline 抛指定异常的最小假响应。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def readline(self):
        raise self._exc

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class CheckinAfterCloseTest(unittest.TestCase):
    def test_connection_returned_after_close_is_dropped(self) -> None:
        client = HttpClient("http://127.0.0.1:9/v1")
        conn = _FakeConn()
        client.close()
        client._checkin(conn)  # 关闭后才归还的在途连接
        self.assertEqual(client._idle, [])  # 不再放回已关闭的池
        self.assertTrue(conn.closed)  # 直接关掉，不泄漏 socket


class SemaphoreDeadlineTest(unittest.TestCase):
    def test_waiting_for_slot_is_bounded_by_deadline(self) -> None:
        client = HttpClient("http://127.0.0.1:9/v1", max_connections=1)
        client._sem.acquire()  # 占满唯一额度
        try:
            started = time.monotonic()
            with self.assertRaises(NetworkError) as ctx:
                client.request("GET", "/models", deadline=time.monotonic() + 0.05)
            self.assertIn("并发额度", str(ctx.exception))
            self.assertLess(time.monotonic() - started, 5.0)
        finally:
            client._sem.release()

    def test_expired_deadline_fails_before_queueing(self) -> None:
        client = HttpClient("http://127.0.0.1:9/v1")
        with self.assertRaises(NetworkError) as ctx:
            client.request("GET", "/models", deadline=time.monotonic() - 1.0)
        self.assertIn("deadline", str(ctx.exception))


class StreamReadErrorTest(unittest.TestCase):
    def _response(self, exc: BaseException) -> StreamResponse:
        client = HttpClient("http://127.0.0.1:9/v1")
        return StreamResponse(_BadRaw(exc), client, _FakeConn(), 200, {})

    def test_oserror_is_wrapped_as_network_error(self) -> None:
        with self.assertRaises(NetworkError):
            next(self._response(OSError("boom")).iter_lines())

    def test_http_exception_is_wrapped_as_network_error(self) -> None:
        with self.assertRaises(NetworkError):
            next(self._response(http.client.IncompleteRead(b"", 10)).iter_lines())


if __name__ == "__main__":
    unittest.main()
