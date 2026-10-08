"""authn 模块测试：SenseNova IAM/OIDC 认证原语。

全部走本地 ThreadingHTTPServer 假服务（无真实网络）；base URL 通过
``HttpAuthn(iam_base=..., oidc_base=...)`` 注入。
"""

import base64
import hashlib
import http.server
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


class SmsLoginTest(unittest.TestCase):
    SMSLOGIN = "/iam/authn/v1/auth/nova/smsLogin"

    def test_sms_login_redirect(self):
        seen = {}

        def do_sms(rec):
            seen["body"] = json.loads(rec["body"].decode("utf-8"))
            return _json({"redirect": "https://console/oauth2/cb"})

        server, thread, base = serve({("POST", self.SMSLOGIN): do_sms})
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            redirect = auth.sms_login(
                token_code="TK", verify_code="123456", challenge="CHAL"
            )
            self.assertEqual(redirect, "https://console/oauth2/cb")
            self.assertEqual(
                seen["body"],
                {"token_code": "TK", "verify_code": "123456", "challenge": "CHAL"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sms_login_tenant_list_reason(self):
        server, thread, base = serve(
            {
                ("POST", self.SMSLOGIN): lambda rec: _json(
                    {"tenant_list": [{"user_id": "u1"}]}
                )
            }
        )
        try:
            auth = authn.HttpAuthn(timeout=5.0, iam_base=base, oidc_base=base)
            with self.assertRaises(authn.AuthnError) as ctx:
                auth.sms_login(token_code="TK", verify_code="123456", challenge="CHAL")
            self.assertEqual(ctx.exception.reason, "tenant_list")
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


if __name__ == "__main__":
    unittest.main()
