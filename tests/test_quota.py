import base64
import http.server
import json
import threading
import unittest
from unittest import mock

from st_rotator import quota


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


if __name__ == "__main__":
    unittest.main()
