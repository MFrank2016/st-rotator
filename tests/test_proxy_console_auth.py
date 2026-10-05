"""网关控制台鉴权：登录页 + HttpOnly Cookie 的真实 HTTP 行为测试。

这些测试起一个真实的 ``ThreadingHTTPServer``（端口 0 自动分配），用
``http.client`` 发请求并断言精确的状态码 / 响应头 / 响应体，不自动跟随跳转。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from st_rotator.config import Config, ConfigStore
from st_rotator.proxy import LoginThrottle, _has_parent_segment, create_server
from st_rotator.ui import ConsoleState

TOKEN = "testtoken123456"

def _read_status(fp) -> int:
    """从 keep-alive 连接按 Content-Length 读一条 HTTP 响应，返回状态码。"""
    status_line = fp.readline().decode("latin-1")
    status = int(status_line.split()[1])
    length = 0
    while True:
        line = fp.readline().decode("latin-1")
        if line in ("\r\n", "\n", ""):
            break
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip() or 0)
    if length:
        fp.read(length)
    return status


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
        self.request_calls = 0
        self.closed = False

    def available_models(self) -> list:
        return []

    def status(self) -> dict:
        return {"status": "ok", "keys": []}

    def request(self, path, method="GET", json_body=None, headers=None) -> dict:
        self.request_calls += 1
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
    login_throttle = None

    def raw_config(self) -> dict:
        return _minimal_config()

    def make_rotator(self, config: Config):
        return FakeRotator(config)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        cfg_path = os.path.join(self._tmp.name, "config.json")
        raw = self.raw_config()
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        self.store = ConfigStore.load(cfg_path)
        self.fake = self.make_rotator(Config.from_dict(raw))
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
            login_throttle=self.login_throttle,
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

    def test_api_state_uses_request_host(self) -> None:
        pair = self.login()
        resp, body = self.request(
            "GET",
            "/api/state",
            headers={
                "Cookie": pair,
                "Host": "st-rotator.061995.xyz",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(resp.status, 200)
        gw = json.loads(body)["gateway"]
        self.assertEqual(gw["base_url"], "https://st-rotator.061995.xyz/v1")
        self.assertEqual(gw["console_url"], "https://st-rotator.061995.xyz/")

    def test_api_state_falls_back_to_listen_addr(self) -> None:
        pair = self.login()
        resp, body = self.request("GET", "/api/state", headers={"Cookie": pair})
        gw = json.loads(body)["gateway"]
        self.assertTrue(gw["base_url"].startswith("http://127.0.0.1:"))


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


class LoginThrottleTest(_ProxyServerTestCase):
    """MF-3：登录失败限流（同一来源窗口内失败过多 -> 429 + Retry-After）。"""

    def setUp(self) -> None:
        self.login_throttle = LoginThrottle(max_failures=2, window=60.0)
        super().setUp()

    def _wrong_login(self):
        return self.request(
            "POST",
            "/login",
            body="token=wrong",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_throttle_after_failures(self) -> None:
        first, _ = self._wrong_login()
        second, _ = self._wrong_login()
        third, _ = self._wrong_login()
        self.assertEqual(first.status, 401)
        self.assertEqual(second.status, 401)
        self.assertEqual(third.status, 429)
        self.assertIsNotNone(third.getheader("Retry-After"))

    def test_success_resets(self) -> None:
        wrong, _ = self._wrong_login()
        self.assertEqual(wrong.status, 401)
        resp, _ = self.request(
            "POST",
            "/login",
            body=f"token={self.token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 303)
        again, _ = self._wrong_login()
        self.assertEqual(again.status, 401)

    def test_retry_after_unit(self) -> None:
        now = [0.0]
        throttle = LoginThrottle(max_failures=2, window=10.0, clock=lambda: now[0])
        throttle.record_failure("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        first = throttle.retry_after("1.2.3.4")
        self.assertGreater(first, 0.0)
        now[0] = 5.0
        second = throttle.retry_after("1.2.3.4")
        self.assertGreater(second, 0.0)
        self.assertLess(second, first)
        now[0] = 10.0
        self.assertEqual(throttle.retry_after("1.2.3.4"), 0.0)

    def test_sweep_bounds_memory(self) -> None:
        now = [0.0]
        throttle = LoginThrottle(max_failures=2, window=10.0, clock=lambda: now[0])
        with mock.patch.object(LoginThrottle, "_MAX_TRACKED_KEYS", 3, create=True):
            for i in range(4):
                throttle.record_failure(f"10.0.0.{i}")
            self.assertEqual(len(throttle._failures), 4)
            now[0] = 100.0  # 远超 10s 窗口，旧时间戳全部过期
            throttle.record_failure("10.0.0.99")
            self.assertLess(len(throttle._failures), 4)


class RevealTokenTest(_ProxyServerTestCase):
    """MF-4：token 只对 Bearer 鉴权的调用方显示；Cookie 会话不回显。"""

    def test_api_state_bearer_reveals_token(self) -> None:
        resp, body = self.request(
            "GET", "/api/state", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(resp.status, 200)
        parsed = json.loads(body)
        self.assertEqual(parsed["gateway"]["token"], TOKEN)

    def test_api_state_cookie_hides_token(self) -> None:
        pair = self.login()
        resp, body = self.request("GET", "/api/state", headers={"Cookie": pair})
        self.assertEqual(resp.status, 200)
        parsed = json.loads(body)
        self.assertEqual(parsed["gateway"]["token"], "")


class PathTraversalTest(_ProxyServerTestCase):
    """MF-5：/v1/* 透传拒绝 `..` 路径段，且不得触达上游。"""

    def test_v1_dotdot_rejected(self) -> None:
        resp, _ = self.request(
            "GET", "/v1/../healthz", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.fake.request_calls, 0)

    def test_has_parent_segment_detects_encoded_traversal(self) -> None:
        cases = (
            "../x",
            "/v1/../x",
            "%2e%2e/x",
            "..%2fhealthz",
            "%2e%2e%2fmodels",
            "..%5csecret",
            "..\\secret",
            "%252e%252e%252f",
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertTrue(_has_parent_segment(case), case)

    def test_has_parent_segment_allows_safe_paths(self) -> None:
        for case in ("v1/models", "..foo/bar"):
            with self.subTest(case=case):
                self.assertFalse(_has_parent_segment(case), case)

    def test_v1_encoded_slash_traversal_rejected(self) -> None:
        resp, _ = self.request(
            "GET", "/v1/..%2fhealthz", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.fake.request_calls, 0)


class KeepAliveTest(_ProxyServerTestCase):
    """MF-6：无 token 时 /login 也必须读完请求体，否则 HTTP/1.1 管线错位。"""

    auto_start = False

    def test_no_token_login_drains_body(self) -> None:
        self.start(None)
        sock = socket.create_connection(
            ("127.0.0.1", self.server.server_address[1]), timeout=5
        )
        try:
            payload = (
                b"POST /login HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"Content-Length: 14\r\n"
                b"Content-Type: application/x-www-form-urlencoded\r\n"
                b"\r\n"
                b"token=whatever"
                b"GET /healthz HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"\r\n"
            )
            sock.sendall(payload)
            reader = sock.makefile("rb")
            first = _read_status(reader)
            second = _read_status(reader)
        finally:
            sock.close()
        self.assertEqual(first, 303)
        self.assertEqual(second, 200)


class AdminPathTest(_ProxyServerTestCase):
    """/admin 作为控制台入口；登录后回到安全的来源页（防开放重定向）。"""

    def test_admin_unauth_returns_login(self) -> None:
        resp, body = self.request("GET", "/admin")
        text = body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('action="/login"', text)

    def test_admin_with_cookie_returns_dashboard(self) -> None:
        pair = self.login()
        resp, body = self.request("GET", "/admin", headers={"Cookie": pair})
        self.assertEqual(resp.status, 200)
        self.assertIn('id="kpis"', body.decode("utf-8"))

    def test_login_redirects_to_safe_next(self) -> None:
        resp, _ = self.request(
            "POST",
            "/login",
            body=f"token={self.token}&next=/admin",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/admin")

    def test_login_ignores_unsafe_next(self) -> None:
        for bad in ("//evil.com", "/v1/models", "/api/state", "http://evil.com"):
            resp, _ = self.request(
                "POST",
                "/login",
                body=f"token={self.token}&next={bad}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp.status, 303)
            self.assertEqual(resp.getheader("Location"), "/", f"next={bad} 应回退到 /")


class _RecordingRotator(FakeRotator):
    """记录最近一次 chat / chat_stream_raw 的入参，并返回带 usage 的响应。"""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.last_chat_params: dict | None = None
        self.last_stream_params: dict | None = None

    def chat(self, messages, model=None, **kwargs) -> dict:
        self.last_chat_params = kwargs
        return {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 12}}

    def chat_stream_raw(self, messages, model=None, **kwargs):
        self.last_stream_params = kwargs
        yield {"choices": [{"delta": {"content": "hi"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}


class ParamsAndUsageTest(_ProxyServerTestCase):
    """min_max_tokens 下限、流式 include_usage 注入、token 用量统计与 /api/usage。"""

    def raw_config(self) -> dict:
        cfg = _minimal_config()
        cfg["min_max_tokens"] = 2048
        return cfg

    def make_rotator(self, config: Config):
        self.rec = _RecordingRotator(config)
        return self.rec

    def _chat(self, body: dict, headers: dict | None = None):
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        if headers:
            h.update(headers)
        return self.request("POST", "/v1/chat/completions", body=json.dumps(body), headers=h)

    def test_min_max_tokens_floor_raises_small(self) -> None:
        resp, _ = self._chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.rec.last_chat_params.get("max_tokens"), 2048)

    def test_min_max_tokens_keeps_larger(self) -> None:
        resp, _ = self._chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 9000})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.rec.last_chat_params.get("max_tokens"), 9000)

    def test_usage_recorded_non_stream(self) -> None:
        pair = self.login()
        self._chat({"messages": [{"role": "user", "content": "hi"}]})
        resp, body = self.request("GET", "/api/usage?hours=1", headers={"Cookie": pair})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["buckets"][-1]["total"], 42)

    def test_stream_injects_include_usage_and_records(self) -> None:
        pair = self.login()
        resp, _ = self._chat({"messages": [{"role": "user", "content": "hi"}], "stream": True})
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.rec.last_stream_params.get("stream_options", {}).get("include_usage"))
        resp2, body = self.request("GET", "/api/usage?hours=1", headers={"Cookie": pair})
        self.assertEqual(json.loads(body)["buckets"][-1]["total"], 12)

    def test_api_usage_requires_auth(self) -> None:
        resp, _ = self.request("GET", "/api/usage")
        self.assertEqual(resp.status, 401)

if __name__ == "__main__":
    unittest.main()
