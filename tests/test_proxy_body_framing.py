"""网关请求体分帧：401 未读 body 与畸形 chunk-size 都不能污染 keep-alive 连接。"""

from __future__ import annotations

import socket
import threading
import unittest

from st_rotator.keypool import user_context
from st_rotator.proxy import create_server

TOKEN = "testtoken123456"


class _FakePool:
    def summary(self) -> dict:
        return {"total": 0, "available": 0}

    def snapshot(self, *, include_users: bool = False) -> list:
        return []

    def user_context(self, label: str):
        return user_context(label)


class _FakeLimiter:
    def stats(self) -> dict:
        return {"mode": "off", "qps": 0.0}


class _FakeRotator:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.limiter = _FakeLimiter()
        self.calls: list[tuple] = []

    def request(self, path, method="GET", json_body=None, headers=None) -> dict:
        self.calls.append((path, method, json_body))
        return {"object": "list", "data": []}

    def status(self) -> dict:
        return {"status": "ok"}


class BodyFramingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rotator = _FakeRotator()
        self.server = create_server(self.rotator, host="127.0.0.1", port=0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _exchange(self, payload: bytes) -> bytes:
        """发一整段字节并读到服务端关连接（两种用例都必须关连接）。"""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(payload)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks)

    def _exchange_one(self, payload: bytes) -> bytes:
        """发一段字节、只读一条完整响应（keep-alive 场景用，不能等 EOF）。"""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(payload)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += sock.recv(65536)
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip() or 0)
            while len(rest) < length:
                rest += sock.recv(65536)
            return head + b"\r\n\r\n" + rest

    def test_401_closes_connection_before_reading_body(self) -> None:
        # 401 之后紧跟一条伪装成「下一条请求」的字节：若服务端不关连接就会解析它
        resp = self._exchange(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer wrong-token\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"hello"
            b"GET /stats HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        first_line = resp.split(b"\r\n", 1)[0]
        self.assertIn(b" 401 ", first_line)
        self.assertIn(b"connection: close", resp.lower())
        self.assertEqual(resp.count(b"HTTP/1.1 "), 1)  # 残留字节没被当成第二条请求
        self.assertEqual(self.rotator.calls, [])

    def test_malformed_chunk_size_is_rejected_and_closes(self) -> None:
        resp = self._exchange(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"zz\r\nhello\r\n0\r\n\r\n"
        )
        self.assertIn(b" 413 ", resp.split(b"\r\n", 1)[0])
        self.assertIn(b"connection: close", resp.lower())
        self.assertEqual(self.rotator.calls, [])

    def test_valid_chunked_body_still_forwarded(self) -> None:
        resp = self._exchange_one(
            b"POST /v1/embeddings HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b'8\r\n{"a": 1}\r\n0\r\n\r\n'
        )
        self.assertIn(b" 200 ", resp.split(b"\r\n", 1)[0])
        self.assertEqual(self.rotator.calls[0][2], {"a": 1})


if __name__ == "__main__":
    unittest.main()
