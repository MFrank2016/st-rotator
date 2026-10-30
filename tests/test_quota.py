import base64
import http.server
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from st_rotator import quota
from st_rotator.config import Config


def _jwt(exp: int) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg':'none'})}.{seg({'exp': exp})}.sig"


SAMPLE = {
    "plan": {"id": "free"},
    "pools": [
        {
            "name": "通用积分池",
            "pool_type": "default",
            "model_ids": ["deepseek-v4-flash", "sensenova-6.8-flash-lite"],
            "window_5h": {"limit": "60000", "used": "19020.9", "remaining": "40979.05856", "reset_at": "1791206310"},
            "window_7d": {"limit": "600000", "used": "554803.9", "remaining": "45196.0", "reset_at": "1791321510"},
            "grant_balance": "74.114",
        },
        {
            "name": "Flash-Lite积分池",
            "pool_type": "dedicated",
            "model_ids": ["sensenova-6.7-flash-lite", "sensenova-6.8-flash-lite"],
            "window_5h": {"limit": "60000", "used": "0", "remaining": "60000", "reset_at": "1791206310"},
            "window_7d": {"limit": "600000", "used": "0", "remaining": "600000", "reset_at": "1791321510"},
            "grant_balance": "0",
        },
    ],
}


class PureHelpersTest(unittest.TestCase):
    def test_jwt_exp(self):
        self.assertEqual(quota.jwt_exp(_jwt(1791210063)), 1791210063)
        self.assertIsNone(quota.jwt_exp("not-a-jwt"))

    def test_normalize_pools(self):
        pools = quota.normalize_pools(SAMPLE)
        self.assertEqual(len(pools), 2)
        g = pools[0]
        self.assertEqual(g.name, "通用积分池")
        self.assertEqual(g.pool_type, "default")
        self.assertAlmostEqual(g.window_5h.remaining, 40979.05856)
        self.assertEqual(g.window_5h.reset_at, 1791206310)
        self.assertAlmostEqual(g.grant_balance, 74.114)

    def test_normalize_missing_window_is_none(self):
        pools = quota.normalize_pools({"pools": [{"name": "x", "pool_type": "default"}]})
        self.assertIsNone(pools[0].window_5h)
        self.assertIsNone(pools[0].window_7d)

    def test_normalize_reset_at_zero_is_none(self):
        pools = quota.normalize_pools(
            {"pools": [{"name": "x", "pool_type": "default", "window_5h": {"reset_at": "0"}}]}
        )
        self.assertIsNone(pools[0].window_5h.reset_at)

    def test_normalize_remaining_empty_is_none(self):
        pools = quota.normalize_pools(
            {"pools": [{"name": "x", "pool_type": "default", "window_5h": {"remaining": ""}}]}
        )
        self.assertIsNone(pools[0].window_5h.remaining)

    def test_normalize_remaining_zero_stays_zero(self):
        pools = quota.normalize_pools(
            {"pools": [{"name": "x", "pool_type": "default", "window_5h": {"remaining": "0"}}]}
        )
        self.assertEqual(pools[0].window_5h.remaining, 0.0)

    def test_select_general_and_flash_lite(self):
        pools = quota.normalize_pools(SAMPLE)
        self.assertEqual(quota.select_general(pools).name, "通用积分池")
        self.assertEqual(quota.select_flash_lite(pools).name, "Flash-Lite积分池")

    def test_select_flash_lite_not_fooled_by_default_pool(self):
        # 只有通用池（其 model_ids 也含 flash-lite）时，专属应为 None
        pools = quota.normalize_pools(
            {"pools": [{"name": "通用积分池", "pool_type": "default",
                        "model_ids": ["sensenova-6.8-flash-lite"]}]}
        )
        self.assertIsNone(quota.select_flash_lite(pools))

    def test_select_flash_lite_single_dedicated_fallback(self):
        pools = quota.normalize_pools(
            {"pools": [{"name": "其它专属", "pool_type": "dedicated", "model_ids": ["foo"]}]}
        )
        self.assertEqual(quota.select_flash_lite(pools).name, "其它专属")

    def test_map_account_quota(self):
        aq = quota.map_account_quota("账号1", "u1", "138", quota.normalize_pools(SAMPLE), fetched_at=1.0)
        self.assertEqual(aq.status, "ok")
        self.assertAlmostEqual(aq.general.h5.remaining, 40979.05856)
        self.assertAlmostEqual(aq.flash_lite.d7.remaining, 600000.0)


class EncryptPasswordTest(unittest.TestCase):
    def test_missing_jwcrypto_raises_unavailable(self):
        with mock.patch.dict("sys.modules", {"jwcrypto": None}):
            with self.assertRaises(quota.QuotaUnavailable):
                quota.encrypt_password("pw", pubkey=object())

    def test_encrypts_with_jwcrypto_when_present(self):
        try:
            __import__("jwcrypto", fromlist=["jwe"])  # noqa: F401
        except ImportError:
            pass
        raise unittest.SkipTest("需要真实 jwcrypto 公钥，仅在有依赖时手动运行")


class FollowUntilTest(unittest.TestCase):
    def test_follow_until_sees_code_in_effective_url(self):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/start"):
                    self.send_response(302)
                    self.send_header("Location", "/next?code=ABC123")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

            def log_message(self, *args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            transport = quota.HttpQuotaTransport()
            opener = transport._opener()
            url = transport._follow_until(
                opener, f"http://127.0.0.1:{port}/start", lambda u: "code=" in u
            )
            self.assertIsNotNone(url)
            self.assertIn("code=ABC123", url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class HttpErrorDetailTest(unittest.TestCase):
    def test_http_error_carries_status_url_and_body(self):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"account forbidden"}')

            def log_message(self, *args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            transport = quota.HttpQuotaTransport()
            opener = transport._opener()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/nova/login", data=b"{}")
            with self.assertRaises(quota.QuotaHTTPError) as ctx:
                transport._open(opener, req, step="nova-login")
            msg = str(ctx.exception)
            self.assertIn("403", msg)
            self.assertIn("nova-login", msg)
            self.assertIn("/nova/login", msg)
            self.assertIn("account forbidden", msg)
            self.assertEqual(ctx.exception.request, "POST /nova/login")
            self.assertIn("account forbidden", ctx.exception.response or "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class WrongCredentialsTest(unittest.TestCase):
    def _exc(self, body: str) -> quota.QuotaHTTPError:
        return quota.QuotaHTTPError("HTTP err", request="POST /x", response=body)

    def test_incorrect_username_or_password_detected(self):
        self.assertTrue(
            quota._is_wrong_credentials(
                self._exc('{"details":[{"reason":"incorrectUsernameOrPassword"}]}')
            )
        )

    def test_account_locked_is_not_wrong_credentials(self):
        self.assertFalse(
            quota._is_wrong_credentials(
                self._exc('{"details":[{"reason":"forbidLoginForMoment"}]}')
            )
        )

    def test_login_maps_wrong_credentials_to_auth_error(self):
        transport = quota.HttpQuotaTransport()
        resp = mock.Mock()
        resp.geturl.return_value = "http://x/login?login_challenge=LC1"

        def fake_open(opener, req, *, step):
            raise quota.QuotaHTTPError(
                "HTTP 400 Bad Request [nova-login]",
                request="POST /iam/authn/v1/auth/nova/login",
                response='{"details":[{"reason":"incorrectUsernameOrPassword"}]}',
            )

        with (
            mock.patch.object(transport, "_get", lambda *a, **k: resp),
            mock.patch.object(
                transport,
                "_follow_until",
                lambda *a, **k: "http://x/login?login_challenge=LC1",
            ),
            mock.patch.object(transport, "_public_key", lambda: object()),
            mock.patch.object(transport, "_open", fake_open),
            mock.patch.object(quota, "_jwcrypto", lambda: (None, None, None)),
            mock.patch.object(quota, "encrypt_password", lambda pw, *, pubkey: "enc"),
        ):
            with self.assertRaises(quota.QuotaAuthError):
                transport.login("u", "p")


class _FakeTransport:
    def __init__(self, *, fail_login=False, fail_fetch=False):
        self.calls = {"login": 0, "fetch": 0}
        self.fail_login = fail_login
        self.fail_fetch = fail_fetch

    def login(self, user, password):
        self.calls["login"] += 1
        if self.fail_login:
            raise quota.QuotaAuthError("用户名或密码错误")
        return quota.TokenBundle("jwt", "", 10800, time.time())

    def fetch_pools(self, access_token):
        self.calls["fetch"] += 1
        if self.fail_fetch:
            raise RuntimeError("boom")
        return quota.normalize_pools(SAMPLE)


class _FailOnceTransport(_FakeTransport):
    """首次 fetch 抛错、第二次成功（用于验证 error 行不被缓存、下次自动重试）。"""

    def fetch_pools(self, access_token):
        self.calls["fetch"] += 1
        if self.calls["fetch"] == 1:
            raise RuntimeError("boom")
        return quota.normalize_pools(SAMPLE)


class _NearExpiryTransport(_FakeTransport):
    """login 返回一枚即将过期的 JWT（exp = now + 100s），验证过期前主动重登。"""

    def login(self, user, password):
        self.calls["login"] += 1
        return quota.TokenBundle(_jwt(int(time.time()) + 100), "", 10800, time.time())


class _Retry401Transport(_FakeTransport):
    """首次 fetch 抛 401（真实传输层的 QuotaHTTPError），重新登录后第二次成功。"""

    def fetch_pools(self, access_token):
        self.calls["fetch"] += 1
        if self.calls["fetch"] == 1:
            raise quota.QuotaHTTPError("HTTP 401 Unauthorized", status=401)
        return quota.normalize_pools(SAMPLE)


class _Non401Transport(_FakeTransport):
    """非 401 的 HTTP 错误不能被当成 token 失效重试。"""

    def fetch_pools(self, access_token):
        self.calls["fetch"] += 1
        raise quota.QuotaHTTPError("HTTP 500 boom", status=500)


def _config_with(accounts):
    return Config.from_dict({"base_url": "http://127.0.0.1:9/v1", "accounts": accounts})


class QuotaServiceTest(unittest.TestCase):
    def _svc(self, transport, clock=None):
        cfg = _config_with([
            {"name": "账号1", "api_keys": ["k1"], "user": "u1", "password": "p1"},
            {"name": "账号2", "api_keys": ["k2"]},  # 未配置
        ])
        return quota.QuotaService(cfg, ttl=300.0, transport=transport, clock=clock or (lambda: 0.0))

    def test_unconfigured_skips_network(self):
        t = _FakeTransport()
        out = self._svc(t).snapshot()
        self.assertEqual(t.calls, {"login": 1, "fetch": 1})  # 仅账号1（已配置）走网络
        by = {a.account: a for a in out}
        self.assertEqual(by["账号2"].status, "unconfigured")
        self.assertEqual(by["账号1"].status, "ok")

    def test_cache_avoids_repeat_and_force_refreshes(self):
        t = _FakeTransport()
        svc = self._svc(t)
        svc.snapshot()
        svc.snapshot()
        self.assertEqual(t.calls["fetch"], 1)
        svc.snapshot(force=True)
        self.assertEqual(t.calls["fetch"], 2)

    def test_login_failure_marks_error(self):
        out = self._svc(_FakeTransport(fail_login=True)).snapshot()
        aq = next(a for a in out if a.account == "账号1")
        self.assertEqual(aq.status, "error")
        self.assertIn("用户名或密码错误", aq.error)

    def test_fetch_failure_marks_error_without_breaking_others(self):
        out = self._svc(_FakeTransport(fail_fetch=True)).snapshot()
        self.assertEqual(next(a for a in out if a.account == "账号1").status, "error")

    def test_error_cached_for_error_ttl_then_retried(self):
        t = _FailOnceTransport()
        now = {"t": 0.0}
        svc = self._svc(t, clock=lambda: now["t"])  # error 缓存 60s，ok 缓存 300s
        first = next(a for a in svc.snapshot() if a.account == "账号1")
        self.assertEqual(first.status, "error")
        # 错误被缓存：未过 error_ttl 前第二次快照不再发起网络请求
        second = next(a for a in svc.snapshot() if a.account == "账号1")
        self.assertEqual(second.status, "error")
        self.assertEqual(t.calls["fetch"], 1)
        # 越过 error_ttl 后重新尝试，第二次成功
        now["t"] = 61.0
        third = next(a for a in svc.snapshot() if a.account == "账号1")
        self.assertEqual(third.status, "ok")
        self.assertEqual(t.calls["fetch"], 2)

    def test_near_expiry_token_triggers_relogin(self):
        t = _NearExpiryTransport()
        cfg = _config_with([{"name": "账号1", "api_keys": ["k1"], "user": "u1", "password": "p1"}])
        svc = quota.QuotaService(cfg, ttl=0.0, transport=t, clock=lambda: 0.0)
        svc.snapshot()
        svc.snapshot()
        self.assertEqual(t.calls["login"], 2)
        self.assertEqual(t.calls["fetch"], 2)

    def test_401_drops_token_relogins_and_retries_once(self):
        t = _Retry401Transport()
        out = self._svc(t).snapshot()
        aq = next(a for a in out if a.account == "账号1")
        self.assertEqual(aq.status, "ok")
        self.assertEqual(t.calls["login"], 2)
        self.assertEqual(t.calls["fetch"], 2)

    def test_open_maps_http_error_status(self):
        """_open 必须把 HTTPError.code 带进 QuotaHTTPError.status（否则 401 分支不可达）。"""
        class _Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(
                    "http://x/y", 401, "Unauthorized", {}, io.BytesIO(b"body")
                )

        transport = quota.HttpQuotaTransport()
        with self.assertRaises(quota.QuotaHTTPError) as ctx:
            transport._open(
                _Opener(), urllib.request.Request("http://x/y"), step="pools"
            )
        self.assertEqual(ctx.exception.status, 401)

    def test_quota_http_error_status_defaults_to_none(self):
        self.assertIsNone(quota.QuotaHTTPError("x").status)

    def test_non_401_http_error_is_not_retried(self):
        t = _Non401Transport()
        out = self._svc(t).snapshot()
        aq = next(a for a in out if a.account == "账号1")
        self.assertEqual(aq.status, "error")
        self.assertEqual(t.calls, {"login": 1, "fetch": 1})

    def test_snapshot_does_not_hold_lock_during_fetch(self):
        """网络往返必须在锁外：fetch 期间其它线程要能读写缓存。"""
        holder = {}

        class _ProbeTransport(_FakeTransport):
            def fetch_pools(self, access_token):
                # 同线程再取同一把锁：被持有则阻塞在这里（用非阻塞探测避免挂死）
                locked = not holder["svc"]._lock.acquire(blocking=False)
                if not locked:
                    holder["svc"]._lock.release()
                holder["lock_held_during_fetch"] = locked
                return super().fetch_pools(access_token)

        svc = self._svc(_ProbeTransport())
        holder["svc"] = svc
        svc.snapshot()
        self.assertFalse(holder["lock_held_during_fetch"])

    def test_close_joins_sampler_and_clears_cache(self):
        svc = quota.QuotaService(
            _config_with(
                [{"name": "账号1", "api_keys": ["k1"], "user": "u1", "password": "p1"}]
            ),
            ttl=300.0,
            transport=_FakeTransport(),
            clock=lambda: 0.0,
            sampler_interval=0.01,
        )
        svc.snapshot()
        sampler = svc._sampler
        self.assertIsNotNone(sampler)
        svc.close()
        self.assertFalse(sampler.is_alive())  # 已 join，不会残留线程
        self.assertEqual(svc._cache, {})
        self.assertEqual(svc._tokens, {})

    def test_close_blocks_post_close_cache_write(self):
        """关闭后即使有在途抓取回来，也不能再写回缓存。"""
        t = _FakeTransport()
        svc = self._svc(t)
        svc.close()  # 先关闭（模拟采样线程还在 HTTP 里）
        out = svc.snapshot()
        self.assertEqual(len(out), 2)
        self.assertEqual(svc._cache, {})

    def test_verify_credentials(self):
        svc = self._svc(_FakeTransport())
        self.assertIsNone(svc.verify_credentials("u1", "p1"))
        bad = self._svc(_FakeTransport(fail_login=True))
        self.assertIn("用户名或密码错误", bad.verify_credentials("u1", "x"))

    def test_credits_tracked_from_pool_used_deltas(self):
        class _MutableTransport:
            def __init__(self):
                self.used = 10.0
            def login(self, user, password):
                return quota.TokenBundle("jwt", "", 10800, 0.0)
            def fetch_pools(self, access_token):
                return quota.normalize_pools({"pools": [
                    {"name": "通用积分池", "pool_type": "default", "model_ids": ["m"],
                     "window_7d": {"used": str(self.used), "reset_at": "100"}},
                ]})

        t = _MutableTransport()
        cfg = _config_with([{"name": "账号1", "api_keys": ["k1"], "user": "u1", "password": "p1"}])
        svc = quota.QuotaService(cfg, ttl=0.0, transport=t, clock=lambda: 0.0)
        svc.snapshot(force=True)  # 首次采样：无差值
        t.used = 25.0
        svc.snapshot(force=True)  # 差值 15
        self.assertEqual(svc.credits.snapshot()["general"]["h1"], 15.0)
        svc.close()


if __name__ == "__main__":
    unittest.main()
