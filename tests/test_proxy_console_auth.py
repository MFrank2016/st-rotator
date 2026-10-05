"""网关控制台鉴权：登录页 + HttpOnly Cookie 的真实 HTTP 行为测试。

这些测试起一个真实的 ``ThreadingHTTPServer``（端口 0 自动分配），用
``http.client`` 发请求并断言精确的状态码 / 响应头 / 响应体，不自动跟随跳转。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest

from st_rotator.config import Config, ConfigStore
from st_rotator.proxy import create_server
from st_rotator.ui import ConsoleState

TOKEN = "testtoken123456"


class _FakeLimiter:
    def stats(self) -> dict:
        return {"mode": "off", "qps": 0.0}


class _FakePool:
    def summary(self) -> dict:
        return {"total": 1, "available": 1}

    def snapshot(self) -> list:
        return []


class FakeRotator:
    """鸭子类型的轮换客户端，只提供网关/控制台需要的表面。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.limiter = _FakeLimiter()
        self.pool = _FakePool()
        self.upstream_attempts = 0
        self.closed = False

    def available_models(self) -> list:
        return []

    def status(self) -> dict:
        return {"status": "ok", "keys": []}

    def request(self, path, method="GET", json_body=None, headers=None) -> dict:
        return {"object": "list", "data": [], "path": path, "method": method}

    def chat(self, messages, model=None, **kwargs) -> dict:
        return {"choices": []}

    def close(self) -> None:
        self.closed = True


def _minimal_config() -> dict:
    return {
        "base_url": "http://127.0.0.1:8080/v1",
        "default_model": "deepseek-v4-flash",
        "accounts": [
            {
                "name": "a",
                "api_keys": ["k"],
                "rpm_limit": 2,
                "max_concurrency": 4,
                "weight": 1,
            }
        ],
    }


class _ProxyServerTestCase(unittest.TestCase):
    """起真实 HTTP 服务，tearDown 关停。

    子类可用 ``token`` 指定初始服务口令；S3 里需要多种口令，因此 ``auto_start=False``
    时不在 setUp 起服务，由用例自己调用 ``start(token)``。
    """

    token: str | None = TOKEN
    auto_start = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        cfg_path = os.path.join(self._tmp.name, "config.json")
        raw = _minimal_config()
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        self.store = ConfigStore.load(cfg_path)
        self.fake = FakeRotator(Config.from_dict(raw))
        self.console = ConsoleState(
            store=self.store,
            rotator=self.fake,
            host="127.0.0.1",
            port=0,
            token=self.token,
        )
        self.server = None
        self.thread = None
        if self.auto_start:
            self.start(self.token)

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._tmp.cleanup()

    def start(self, token: str | None) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.console.token = token
        self.server = create_server(
            self.fake,
            host="127.0.0.1",
            port=0,
            token=token,
            console=self.console,
        )
        self.console.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp, data
        finally:
            conn.close()

    def login(self) -> str:
        resp, _ = self.request(
            "POST",
            "/login",
            body=f"token={self.token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 303)
        return resp.getheader("Set-Cookie").split(";", 1)[0]


class S1HappyPathTest(_ProxyServerTestCase):
    def test_get_root_unauth_returns_login(self) -> None:
        resp, body = self.request("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('action="/login"', text)
        self.assertNotIn('id="kpis"', text)

    def test_login_correct_303_and_set_cookie(self) -> None:
        resp, _ = self.request(
            "POST",
            "/login",
            body=f"token={self.token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        cookie = resp.getheader("Set-Cookie") or ""
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/")
        self.assertIn("st_rotator_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=604800", cookie)

    def test_root_with_cookie_returns_dashboard(self) -> None:
        pair = self.login()
        resp, body = self.request("GET", "/", headers={"Cookie": pair})
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('id="kpis"', text)

    def test_api_state_with_cookie_200(self) -> None:
        pair = self.login()
        resp, body = self.request("GET", "/api/state", headers={"Cookie": pair})
        self.assertEqual(resp.status, 200)
        parsed = json.loads(body)
        self.assertIn("summary", parsed)


class S2NegativeTest(_ProxyServerTestCase):
    def test_wrong_token_401_static_error_no_cookie(self) -> None:
        resp, body = self.request(
            "POST",
            "/login",
            body="token=wrong",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 401)
        self.assertIn("口令不正确", text)
        self.assertNotIn("wrong", text)
        self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_api_state_unauth_401_json(self) -> None:
        resp, body = self.request("GET", "/api/state")
        self.assertEqual(resp.status, 401)
        parsed = json.loads(body)
        self.assertIn("error", parsed)

    def test_v1_models_cookie_only_401(self) -> None:
        pair = self.login()
        resp, _ = self.request("GET", "/v1/models", headers={"Cookie": pair})
        self.assertEqual(resp.status, 401)

    def test_get_login_200(self) -> None:
        resp, body = self.request("GET", "/login")
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('action="/login"', text)

    def test_logout_clears_cookie_and_redirects(self) -> None:
        resp, _ = self.request("POST", "/logout")
        cookie = resp.getheader("Set-Cookie") or ""
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/login")
        self.assertIn("Max-Age=0", cookie)


class S3RegressionTest(_ProxyServerTestCase):
    auto_start = False

    def test_no_token_root_returns_dashboard(self) -> None:
        self.start(None)
        resp, body = self.request("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('id="kpis"', text)

    def test_bearer_still_works(self) -> None:
        self.start(TOKEN)
        resp, _ = self.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(resp.status, 200)

    def test_healthz_and_stats_public(self) -> None:
        self.start(TOKEN)
        health, _ = self.request("GET", "/healthz")
        stats, _ = self.request("GET", "/stats")
        self.assertEqual(health.status, 200)
        self.assertEqual(stats.status, 200)


class AuthHardeningTest(_ProxyServerTestCase):
    """MF-1 / MF-2：非 ASCII 凭据与畸形 Content-Length 必须返回 HTTP 响应，而不是断开连接。"""

    def test_non_ascii_login_token_returns_401(self) -> None:
        resp, body = self.request(
            "POST",
            "/login",
            body="token=你好".encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 401)
        self.assertIn("口令不正确", body.decode("utf-8"))

    def test_non_ascii_bearer_returns_401(self) -> None:
        resp, _ = self.request(
            "GET", "/v1/models", headers={"Authorization": "Bearer \x80"}
        )
        self.assertEqual(resp.status, 401)

    def test_non_ascii_cookie_returns_401(self) -> None:
        resp, _ = self.request(
            "GET", "/api/state", headers={"Cookie": "st_rotator_session=\x80"}
        )
        self.assertEqual(resp.status, 401)

    def test_malformed_content_length_login_413(self) -> None:
        resp, _ = self.request("POST", "/login", headers={"Content-Length": "abc"})
        self.assertEqual(resp.status, 413)

    def test_oversized_content_length_login_413(self) -> None:
        resp, _ = self.request(
            "POST", "/login", headers={"Content-Length": "40000000"}
        )
        self.assertEqual(resp.status, 413)


if __name__ == "__main__":
    unittest.main()
