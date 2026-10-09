"""autorenew 传输层（平台 Key 管理 API 客户端）的本地 HTTP 测试。

测试模式仿 test_quota.FollowUntilTest：ThreadingHTTPServer 起在线程里，
base 注入本地端口，由 server 罐头化响应并记录收到的请求。
"""

import http.server
import json
import socket
import threading
import unittest
import urllib.parse
from unittest import mock

from st_rotator import autorenew
from st_rotator.quota import USER_AGENT


# 路径不含 /lite：/lite 属于 BASE（真实默认 base 为 https://platform.sensenova.cn/lite），
# 测试注入的 base 是裸 http://127.0.0.1:{port}，故此处只写 {base} 之后的路径。
KEYS_PATH = "/console/v1/metered/api-keys"


def _handler_class(responder):
    """每个测试一个 handler 类：记录请求，按 responder 出罐头响应。"""

    class H(http.server.BaseHTTPRequestHandler):
        recorded: list = []

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            H.recorded.append(
                {
                    "method": method,
                    "path": self.path,
                    "headers": headers,
                    "body": raw.decode("utf-8", "replace"),
                }
            )
            status, payload = responder(method, self.path, headers, raw)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if payload is not None:
                self.wfile.write(payload)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def log_message(self, *args: object) -> None:
            pass

    return H


class _Server:
    """本地罐头 HTTP server：responder(method, path, headers, body) -> (status, payload)。"""

    def __init__(self, responder) -> None:
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_class(responder)
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def recorded(self) -> list:
        return self._server.RequestHandlerClass.recorded

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class AutorenewTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._server = None

    def tearDown(self) -> None:
        if self._server is not None:
            self._server.close()

    def _manager(self, responder):
        self._server = _Server(responder)
        return autorenew.HttpKeyManager(base=self._server.base)

    # 1. 模块可导入（RED：ImportError）
    def test_module_importable(self):
        import st_rotator.autorenew as mod

        self.assertTrue(callable(mod.HttpKeyManager))
        self.assertTrue(issubclass(mod.KeyApiError, RuntimeError))
        self.assertTrue(hasattr(mod, "KeyInfo"))
        self.assertTrue(hasattr(mod, "KeyTransport"))

    # 2. list_keys 分页：第一页返回 next_page_token，第二页终止 → 合并、去重、字段正确
    def test_list_keys_pagination_merges_and_dedups(self):
        def responder(method, path, headers, body):
            if "page_token=tok2" in path:
                page = {
                    "api_keys": [
                        {
                            "id": "k3",
                            "displayname": "c",
                            "api_key": "sk-c",
                            "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                            "create_time": "t3",
                        },
                        {
                            "id": "k2",
                            "displayname": "dup",
                            "api_key": "sk-b2",
                            "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                            "create_time": "t2b",
                        },
                    ],
                    "total_count": 3,
                }
            else:
                page = {
                    "api_keys": [
                        {
                            "id": "k1",
                            "displayname": "a",
                            "api_key": "sk-a",
                            "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                            "create_time": "t1",
                        },
                        {
                            "id": "k2",
                            "displayname": "b",
                            "api_key": "sk-b",
                            "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                            "create_time": "t2",
                        },
                    ],
                    "total_count": 3,
                    "next_page_token": "tok2",
                }
            return 200, json.dumps(page).encode()

        manager = self._manager(responder)
        keys = manager.list_keys("tok")
        # 第二页出现重复 k2 → 按 id 去重，保留首次出现
        self.assertEqual([k.id for k in keys], ["k1", "k2", "k3"])
        self.assertEqual(len(self._server.recorded), 2)
        k1 = keys[0]
        self.assertEqual(k1.displayname, "a")
        self.assertEqual(k1.api_key, "sk-a")
        self.assertEqual(k1.key_type, "API_KEY_TYPE_TOKEN_PLAN")
        self.assertEqual(k1.create_time, "t1")
        self.assertEqual(keys[1].displayname, "b")

    # 3. list_keys 把 key_type/page_size/page_token 传到 query（有才带）
    def test_list_keys_sends_query_params(self):
        seen = {}

        def responder(method, path, headers, body):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            seen[path.split("?", 1)[0]] = qs
            return 200, json.dumps({"api_keys": [], "total_count": 0}).encode()

        manager = self._manager(responder)
        manager.list_keys(
            "tok", key_type="API_KEY_TYPE_TOKEN_PLAN", page_size=50, page_token="tok9"
        )
        qs = seen[KEYS_PATH]
        self.assertEqual(qs["key_type"], ["API_KEY_TYPE_TOKEN_PLAN"])
        self.assertEqual(qs["page_size"], ["50"])
        self.assertEqual(qs["page_token"], ["tok9"])
        # 未提供可选参数时不带对应 query：key_type/page_token 有才带，page_size 恒在
        manager.list_keys("tok")
        qs_default = seen[KEYS_PATH]
        self.assertEqual(qs_default["page_size"], ["50"])
        self.assertNotIn("key_type", qs_default)
        self.assertNotIn("page_token", qs_default)

    # 4. create_key 正确发送 POST body，响应含 api_key → 返回 KeyInfo
    def test_create_key_posts_json_body(self):
        seen = {}

        def responder(method, path, headers, body):
            if method == "POST" and path == KEYS_PATH:
                seen["body"] = json.loads(body)
                seen["content_type"] = headers.get("content-type")
                return 200, json.dumps(
                    {
                        "id": "k-new",
                        "displayname": "auto",
                        "api_key": "sk-plain",
                        "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                        "create_time": "t-new",
                    }
                ).encode()
            return 404, b"not found"

        manager = self._manager(responder)
        key = manager.create_key(
            "tok", displayname="auto", key_type="API_KEY_TYPE_TOKEN_PLAN"
        )
        self.assertEqual(
            seen["body"], {"displayname": "auto", "key_type": "API_KEY_TYPE_TOKEN_PLAN"}
        )
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(key.id, "k-new")
        self.assertEqual(key.api_key, "sk-plain")

    def test_create_key_reads_api_key_plain_from_nested_response(self):
        def responder(method, path, headers, body):
            if method == "POST" and path == KEYS_PATH:
                return 200, json.dumps(
                    {
                        "api_key": {
                            "id": "k-new",
                            "displayname": "auto",
                            "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                            "api_key": "sk-****Vrsi",
                            "create_time": "t-new",
                            "status": "enabled",
                            "is_default": False,
                        },
                        "api_key_plain": "sk-kZcgPLAINkeyVrsi",
                    }
                ).encode()
            return 404, b"not found"

        manager = self._manager(responder)
        key = manager.create_key(
            "tok", displayname="auto", key_type="API_KEY_TYPE_TOKEN_PLAN"
        )
        self.assertEqual(key.id, "k-new")
        self.assertEqual(key.displayname, "auto")
        self.assertEqual(key.api_key, "sk-kZcgPLAINkeyVrsi")

    # 5. delete_key 正确调用 DELETE {path}/{id}，Authorization: Bearer 头正确
    def test_delete_key_sends_delete_with_bearer(self):
        seen = {}

        def responder(method, path, headers, body):
            seen["method"] = method
            seen["path"] = path
            seen["headers"] = headers
            return 200, None

        manager = self._manager(responder)
        manager.delete_key("SECRET_TOKEN_XYZ", key_id="key-42")
        self.assertEqual(seen["method"], "DELETE")
        self.assertEqual(seen["path"], f"{KEYS_PATH}/key-42")
        self.assertEqual(seen["headers"]["authorization"], "Bearer SECRET_TOKEN_XYZ")
        self.assertIn("application/json", seen["headers"]["accept"])
        self.assertEqual(
            seen["headers"]["referer"], "https://platform.sensenova.cn/console"
        )
        self.assertEqual(seen["headers"]["user-agent"], USER_AGENT)

    # 6. 非 2xx → KeyApiError(status=xxx)，错误响应体文本含在 detail（≤200 字符）
    def test_http_error_raises_key_api_error_with_detail(self):
        def responder(method, path, headers, body):
            return 401, b"unauthorized token plan"

        manager = self._manager(responder)
        with self.assertRaises(autorenew.KeyApiError) as cm:
            manager.list_keys("tok")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("401", cm.exception.detail)
        self.assertIn("unauthorized token plan", cm.exception.detail)
        self.assertLessEqual(len(cm.exception.detail), 200)

    # 7. 网络层错误（端口未监听/连接拒绝）→ KeyApiError(status=0)，detail 不含 token
    def test_network_error_raises_key_api_error_status_zero(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        manager = autorenew.HttpKeyManager(base=f"http://127.0.0.1:{port}")
        # 跳过环境代理（HTTP_PROXY 会在连不上时返回 502 而非 ConnectionRefused），
        # 让请求直连已关闭端口以触达 URLError/OSError 网络层路径。
        with mock.patch("urllib.request.proxy_bypass", return_value=True):
            with self.assertRaises(autorenew.KeyApiError) as cm:
                manager.list_keys("SECRET_TOKEN_XYZ")
        self.assertEqual(cm.exception.status, 0)
        self.assertTrue(cm.exception.detail)
        self.assertNotIn("SECRET_TOKEN_XYZ", cm.exception.detail)

    # 8. DELETE 返回空体不炸 → 正常返回 None；空体 LIST 容忍为 {}
    def test_empty_body_delete_and_list_tolerated(self):
        def responder(method, path, headers, body):
            return 200, b""

        manager = self._manager(responder)
        self.assertIsNone(manager.delete_key("tok", key_id="key-42"))
        self.assertEqual(manager.list_keys("tok"), [])


if __name__ == "__main__":
    unittest.main()
