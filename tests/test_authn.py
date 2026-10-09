"""authn 模块测试：SenseNova IAM/OIDC 认证原语。

全部走本地 ThreadingHTTPServer 假服务（无真实网络）；base URL 通过
``HttpAuthn(iam_base=..., oidc_base=...)`` 注入。
"""

import base64
import hashlib
import http.server
import io
import json
import threading
import unittest
import urllib.parse

from st_rotator import authn
from st_rotator.quota import REDIRECT_URI


def _jwt_with(**claims: object) -> str:
    def seg(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def _pkce_match(verifier: str, challenge: str) -> bool:
    expect = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return expect == challenge


class _FakeAuthn(http.server.BaseHTTPRequestHandler):
    """可配置的本地假服务：routes[(method, path)] -> (status, headers, body) 处理器。

    每次请求都会把摘要记入 ``requests``（测试断言请求形状用）。
    """

    routes: dict[tuple[str, str], object] = {}
    requests: list[dict] = []

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = {
            "method": self.command,
            "path": parsed.path,
            "query": urllib.parse.parse_qs(parsed.query),
            "headers": dict(self.headers),
            "body": body,
        }
        type(self).requests.append(rec)
        responder = type(self).routes.get((self.command, parsed.path))
        if responder is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, payload = responder(rec)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # 静默访问日志
        pass


def serve(
    routes: dict[tuple[str, str], object],
) -> tuple[http.server.ThreadingHTTPServer, threading.Thread, str]:
    """启动一个带给定路由表的假服务，返回 (server, thread, base_url)。"""
    _FakeAuthn.routes = routes
    _FakeAuthn.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeAuthn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def _json(data: dict) -> tuple[int, dict, bytes]:
    return 200, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")


def _redirect(location: str) -> tuple[int, dict, bytes]:
    return 302, {"Location": location}, b""


class JwtSubTest(unittest.TestCase):
    def test_jwt_sub_decodes_claim(self):
        self.assertEqual(authn.jwt_sub(_jwt_with(sub="user-abc-123")), "user-abc-123")
        self.assertIsNone(authn.jwt_sub("not-a-jwt"))
        self.assertIsNone(authn.jwt_sub(_jwt_with(exp=123)))  # 无 sub
        self.assertIsNone(authn.jwt_sub(_jwt_with(sub=123)))  # sub 非字符串


class MintLoginChallengeTest(unittest.TestCase):
    def test_mint_login_challenge_follows_to_login_challenge(self):
        def challenge(rec):
            if rec["query"].get("intent"):
                return _redirect("/login?login_challenge=CHAL_REG")
            return _redirect("/login?login_challenge=CHAL_1")

        server, thread, base = serve(
            {
                ("GET", "/oauth2/auth"): challenge,
                ("GET", "/login"): lambda rec: (200, {}, b"ok"),
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            challenge, verifier = auth.mint_login_challenge(intent="register")
            self.assertEqual(challenge, "CHAL_REG")
            self.assertTrue(verifier)

            req = _FakeAuthn.requests[0]
            self.assertEqual(req["method"], "GET")
            self.assertEqual(req["path"], "/oauth2/auth")
            q = req["query"]
            self.assertEqual(q["client_id"], ["nova"])
            self.assertEqual(q["response_type"], ["code"])
            self.assertEqual(q["redirect_uri"], [REDIRECT_URI])
            self.assertEqual(q["scope"], ["openid offline offline_access"])
            self.assertEqual(q["code_challenge_method"], ["S256"])
            self.assertEqual(q["intent"], ["register"])
            self.assertTrue(q["state"])
            self.assertTrue(_pkce_match(verifier, q["code_challenge"][0]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_mint_login_challenge_without_intent(self):
        server, thread, base = serve(
            {
                ("GET", "/oauth2/auth"): lambda rec: _redirect(
                    "/login?login_challenge=CHAL_2"
                ),
                ("GET", "/login"): lambda rec: (200, {}, b"ok"),
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            challenge, verifier = auth.mint_login_challenge()
            self.assertEqual(challenge, "CHAL_2")
            self.assertNotIn("intent", _FakeAuthn.requests[0]["query"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ExchangeCodeTest(unittest.TestCase):
    def test_exchange_code_posts_token_form_with_verifier(self):
        token_stats = {}

        def do_token(rec):
            token_stats["form"] = urllib.parse.parse_qs(rec["body"].decode("utf-8"))
            return _json(
                {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600}
            )

        server, thread, base = serve(
            {
                ("GET", "/start"): lambda rec: _redirect("/cb?code=SECRET_CODE"),
                ("GET", "/cb"): lambda rec: (200, {}, b"ok"),
                ("POST", "/oauth2/token"): do_token,
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            bundle = auth.exchange_code(base + "/start", "VERIFIER-X")
            self.assertEqual(bundle.access_token, "AT-1")
            self.assertEqual(bundle.refresh_token, "RT-1")
            self.assertEqual(bundle.expires_in, 3600)
            self.assertGreater(bundle.acquired_at, 0)

            form = token_stats["form"]
            self.assertEqual(form["code"], ["SECRET_CODE"])
            self.assertEqual(form["code_verifier"], ["VERIFIER-X"])
            self.assertEqual(form["client_id"], ["nova"])
            self.assertEqual(form["redirect_uri"], [REDIRECT_URI])
            self.assertEqual(form["grant_type"], ["authorization_code"])
            tok_req = _FakeAuthn.requests[-1]
            self.assertEqual(
                tok_req["headers"]["Content-Type"], "application/x-www-form-urlencoded"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ExchangeSessionCookieTest(unittest.TestCase):
    """register 建立的会话 Cookie 必须在 exchange_code 跟随重定向时可见（回归）。

    真实 OIDC：register 成功后返回 redirect，跟随它时 /oauth2/auth 依赖 register 会话
    Cookie 才返回 code；若 exchange 用全新 Cookie 罐则拿不到 code（本次线上故障）。
    """

    def test_exchange_reuses_session_cookie_from_register(self):
        for force_urllib in (False, True):
            with self.subTest(force_urllib=force_urllib):
                self._run_cookie_flow(force_urllib=force_urllib)

    def _run_cookie_flow(self, *, force_urllib: bool) -> None:
        holder = {}

        def do_register(rec):
            return (
                200,
                {
                    "Content-Type": "application/json",
                    "Set-Cookie": "sid=abc; Path=/",
                },
                json.dumps({"redirect": holder["base"] + "/oauth2/consent"}).encode(),
            )

        def do_consent(rec):
            if "sid=abc" in rec["headers"].get("Cookie", ""):
                return _redirect(holder["base"] + "/cb?code=CODE123")
            return _redirect(holder["base"] + "/login")

        server, thread, base = serve(
            {
                ("POST", "/iam/authn/v1/auth/nova/register"): do_register,
                ("GET", "/oauth2/consent"): do_consent,
                ("GET", "/cb"): lambda rec: (200, {}, b"ok"),
                ("GET", "/login"): lambda rec: (200, {}, b"login"),
                ("POST", "/oauth2/token"): lambda rec: _json(
                    {"access_token": "AT-COOKIE", "expires_in": 3600}
                ),
            }
        )
        holder["base"] = base
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            if force_urllib:
                auth._curl = None
            redirect = auth.register(
                token_code="TK", user_name="u", password="P", challenge="CHAL"
            )
            bundle = auth.exchange_code(redirect, "VERIFIER")
            self.assertEqual(bundle.access_token, "AT-COOKIE")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SendSmsCodeTest(unittest.TestCase):
    SEND = "/iam/authn/v1/auth/nova/sendSmsCode"

    def test_send_sms_code_returns_token_code(self):
        seen = {}

        def do_send(rec):
            seen["body"] = json.loads(rec["body"].decode("utf-8"))
            return _json({"token_code": "TK-1"})

        server, thread, base = serve({("POST", self.SEND): do_send})
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            token = auth.send_sms_code("13800138000")
            self.assertEqual(token, "TK-1")
            self.assertEqual(
                seen["body"], {"phone": "13800138000", "region_code": "86"}
            )

            auth.send_sms_code("13800138000", code_key="CAP-KEY")
            self.assertEqual(
                seen["body"],
                {"phone": "13800138000", "region_code": "86", "code_key": "CAP-KEY"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_send_sms_code_returns_none_when_captcha_required(self):
        envelope = {
            "code": 3,
            "message": "InvalidArgument",
            "details": [
                {"@type": "google.rpc.ErrorInfo", "reason": "invalidCaptcha"},
                {"@type": "LocalizedMessage", "message": "invalid captcha"},
            ],
        }
        server, thread, base = serve(
            {
                ("POST", self.SEND): lambda rec: (
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps(envelope).encode("utf-8"),
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            self.assertIsNone(auth.send_sms_code("13800138000"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class RegisterTest(unittest.TestCase):
    REGISTER = "/iam/authn/v1/auth/nova/register"

    def test_register_request_shape_and_redirect(self):
        seen = {}

        def do_register(rec):
            seen["body"] = json.loads(rec["body"].decode("utf-8"))
            return _json({"redirect": "https://console/oauth2/cb"})

        server, thread, base = serve({("POST", self.REGISTER): do_register})
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            redirect = auth.register(
                token_code="TK",
                user_name="user01",
                password="Passw0rd!",
                challenge="CHAL",
            )
            self.assertEqual(redirect, "https://console/oauth2/cb")
            self.assertEqual(
                seen["body"],
                {
                    "token_code": "TK",
                    "user_name": "user01",
                    "password": "Passw0rd!",
                    "challenge": "CHAL",
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_register_already_registered_reason(self):
        envelope = {
            "code": 3,
            "message": "InvalidArgument",
            "details": [{"@type": "LocalizedMessage", "message": "手机号已注册"}],
        }
        server, thread, base = serve(
            {
                ("POST", self.REGISTER): lambda rec: (
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps(envelope).encode("utf-8"),
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            with self.assertRaises(authn.AuthnError) as ctx:
                auth.register(
                    token_code="TK",
                    user_name="user01",
                    password="Passw0rd!",
                    challenge="CHAL",
                )
            self.assertEqual(ctx.exception.reason, "already_registered")
            self.assertEqual(ctx.exception.status, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_register_username_taken_reason(self):
        envelope = {
            "code": 3,
            "message": "InvalidArgument",
            "details": [{"@type": "LocalizedMessage", "message": "该用户名已被注册"}],
        }
        server, thread, base = serve(
            {
                ("POST", self.REGISTER): lambda rec: (
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps(envelope).encode("utf-8"),
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            with self.assertRaises(authn.AuthnError) as ctx:
                auth.register(
                    token_code="TK",
                    user_name="user01",
                    password="Passw0rd!",
                    challenge="CHAL",
                )
            self.assertEqual(ctx.exception.reason, "username_taken")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_register_incorrect_sms_code_reason(self):
        envelope = {
            "code": 3,
            "message": "InvalidArgument",
            "details": [
                {"@type": "LocalizedMessage", "reason": "incorrectSmsCode"},
                {"@type": "LocalizedMessage", "message": "验证码错误"},
            ],
        }
        server, thread, base = serve(
            {
                ("POST", self.REGISTER): lambda rec: (
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps(envelope).encode("utf-8"),
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            with self.assertRaises(authn.AuthnError) as ctx:
                auth.register(
                    token_code="TK",
                    user_name="user01",
                    password="Passw0rd!",
                    challenge="CHAL",
                )
            self.assertEqual(ctx.exception.reason, "incorrect_sms_code")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SmsLoginTest(unittest.TestCase):
    SMSLOGIN = "/iam/authn/v1/auth/nova/smsLogin"

    def test_sms_login_returns_raw_response_with_redirect(self):
        seen = {}

        def do_sms(rec):
            seen["body"] = json.loads(rec["body"].decode("utf-8"))
            return _json({"redirect": "https://console/oauth2/cb"})

        server, thread, base = serve({("POST", self.SMSLOGIN): do_sms})
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            data = auth.sms_login(
                token_code="TK", verify_code="123456", challenge="CHAL"
            )
            self.assertEqual(
                authn.sms_login_redirect(data), "https://console/oauth2/cb"
            )
            self.assertEqual(authn.sms_login_tenants(data), [])
            self.assertEqual(
                seen["body"],
                {"token_code": "TK", "verify_code": "123456", "challenge": "CHAL"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sms_login_empty_tenant_list_means_unregistered(self):
        server, thread, base = serve(
            {("POST", self.SMSLOGIN): lambda rec: _json({"tenant_list": []})}
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            data = auth.sms_login(
                token_code="TK", verify_code="123456", challenge="CHAL"
            )
            self.assertEqual(authn.sms_login_tenants(data), [])
            self.assertIsNone(authn.sms_login_redirect(data))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sms_login_returns_tenants_when_registered(self):
        server, thread, base = serve(
            {
                ("POST", self.SMSLOGIN): lambda rec: _json(
                    {"tenant_list": [{"user_id": "u1"}]}
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            data = auth.sms_login(
                token_code="TK", verify_code="123456", challenge="CHAL"
            )
            self.assertEqual(authn.sms_login_tenants(data), [{"user_id": "u1"}])
            self.assertIsNone(authn.sms_login_redirect(data))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ChangePasswordFlowTest(unittest.TestCase):
    def test_change_password_flow_request_shapes(self):
        step1 = "/iam/idp/v1/users/u-123:novaSendUserSmsCode"
        step2 = "/iam/idp/v1/users/u-123:novaUpdatePassword"
        seen = {}

        def do_step1(rec):
            seen["step1"] = {
                "body": rec["body"],
                "auth": rec["headers"].get("Authorization"),
            }
            return _json({"token_code": "PW_TK"})

        def do_step2(rec):
            seen["step2"] = {
                "body": json.loads(rec["body"].decode("utf-8")),
                "auth": rec["headers"].get("Authorization"),
            }
            return _json({})

        server, thread, base = serve(
            {("POST", step1): do_step1, ("POST", step2): do_step2}
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            token = auth.request_change_password_code("AT-1", "u-123")
            self.assertEqual(token, "PW_TK")
            self.assertEqual(seen["step1"]["body"], b"{}")
            self.assertEqual(seen["step1"]["auth"], "Bearer AT-1")

            self.assertIsNone(
                auth.change_password(
                    "AT-1",
                    "u-123",
                    token_code="PW_TK",
                    verify_code="654321",
                    password="NewPass1!",
                )
            )
            self.assertEqual(
                seen["step2"]["body"],
                {
                    "token_code": "PW_TK",
                    "password": "NewPass1!",
                    "verify_code": "654321",
                },
            )
            self.assertEqual(seen["step2"]["auth"], "Bearer AT-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class GetUserInfoTest(unittest.TestCase):
    def test_get_user_info_profile(self):
        profile = {"user_id": "u-123", "user_name": "bob", "phone": "13800138000"}
        seen = {}

        def do_get(rec):
            seen["auth"] = rec["headers"].get("Authorization")
            return _json(profile)

        server, thread, base = serve({("GET", "/iam/idp/v1/users/u-123"): do_get})
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            info = auth.get_user_info("AT-1", "u-123")
            self.assertEqual(dict(info), profile)
            self.assertEqual(seen["auth"], "Bearer AT-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class MatchSliderTest(unittest.TestCase):
    """滑块求解：合成背景 + 已知缺口，断言归一化互相关命中正确列。"""

    def test_locates_gap_column(self) -> None:
        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            self.skipTest("需要可选依赖 numpy + Pillow")
        rng = np.random.default_rng(0)
        bg = rng.integers(0, 256, size=(190, 316, 3), dtype=np.uint8)
        gap_x = 123
        blk_rgb = bg[:, gap_x : gap_x + 47, :]
        blk = np.dstack([blk_rgb, np.full((190, 47), 255, np.uint8)])

        def png_b64(arr: object) -> str:
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, format="PNG")  # type: ignore[arg-type]
            return base64.b64encode(buf.getvalue()).decode()

        self.assertEqual(authn._match_slider(png_b64(bg), png_b64(blk)), gap_x)

    def test_returns_none_on_bad_input(self) -> None:
        self.assertIsNone(authn._match_slider("not-base64!!", "also-bad!!"))


if __name__ == "__main__":
    unittest.main()
