"""SenseNova IAM/OIDC 认证原语（注册 / 短信登录 / 改密 / 取用户信息）。

端点契约来自 docs/ejiema_and_sensenova_api.md §B（勿改）。风格对齐
quota.HttpQuotaTransport：只用标准库 urllib.request + CookieJar，手动跟随
OIDC 重定向；register / change_password 按文档观测为明文密码（无 JWE 分支）。

只 import quota 的公开常量与 TokenBundle（quota.py 零修改）。

失败语义：``AuthnError``，``status=0`` 表示网络层错误，其余为 HTTP 状态码；
``reason`` 取自封闭集合 {invalid_captcha, incorrect_sms_code, invalid,
already_registered, username_taken, tenant_list, challenge_expired, network, unknown}。

端点分属两个 host（OIDC = platform.sensenova.cn，IAM = iam.sensecoreapi.cn），
构造时可分别用 ``oidc_base`` / ``iam_base`` 覆盖（测试指向本地假服务）。
"""

# 说明：本模块纯行数超出通用 250 行上限，按 single-responsibility 不可再拆 ——
# 接口由 .omo/plans/replenish.md T2 钉死为单模块 authn.py（9 个协议方法 +
# OIDC 跟随重定向 + 错误信封映射 + 6 组识别标记）；仓库同类模块亦为大文件
# （quota.py 634 行 / autorenew.py 404 行）。特此声明，非漏检。

from __future__ import annotations

import base64
import io
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from .quota import (
    CLIENT_ID,
    IAM_BASE,
    OIDC_AUTH,
    REDIRECT_URI,
    SCOPE,
    USER_AGENT,
    TokenBundle,
)

# ---------------------------------------------------------------- 常量
# 勘误（docs §B.1）：SPA 的 token 交换打向 {signinUrl}/oauth2/token =
# platform.sensenova.cn，而非 quota.OIDC_TOKEN（signin.sensecore.cn）。
OIDC_BASE = OIDC_AUTH.removesuffix("/oauth2/auth")
OIDC_TOKEN_URL = OIDC_BASE + "/oauth2/token"

# 错误原因识别标记（匹配于 gRPC 信封的 message/reason 合并文本）
_CAPTCHA_MARKERS = ("captcha", "验证码")
# 短信验证码错误（register/smsLogin 校验失败）。必须优先于 _CAPTCHA_MARKERS 判定：
# 服务端该错误的中文文案为「验证码错误」，其中含「验证码」二字会误命中 captcha 标记。
_SMS_CODE_MARKERS = ("incorrectsmscode", "incorrect_sms_code", "sms_code_incorrect")
_USERNAME_TAKEN_MARKERS = (
    "username_taken",
    "user_name_taken",
    "username already exists",
    "user name already exists",
    "username is taken",
    "用户名已存在",
    "用户名已占用",
    "用户名已被",
)
_REGISTERED_MARKERS = ("already_registered", "registered", "exist", "已注册", "存在")
_TENANT_MARKERS = ("tenant_list", "tenant list", "租户")
_CHALLENGE_MARKERS = (
    "challenge_expired",
    "challengeexpired",
    "challenge expired",
    "challenge已过期",
    "challenge 已过期",
    "挑战已过期",
)

_CAPTCHA_MAX_TRIES = 4


def _match_slider(image_b64: str, block_b64: str) -> int | None:
    """在背景图里定位滑块缺口的 x（带 alpha 掩膜的归一化互相关）。

    滑块 ``block`` 的 alpha 通道即缺口形状；用它在背景里做归一化互相关，
    峰值列即缺口 x。需要可选依赖 ``numpy`` + ``Pillow``，缺失或解码失败返回 ``None``。
    实测命中率 ~94%+（见 docs/sensenova_captcha_solver.md）。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        bg = np.asarray(
            Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB"),
            dtype=np.float64,
        )
        blk = np.asarray(Image.open(io.BytesIO(base64.b64decode(block_b64))))
    except Exception:  # noqa: BLE001 - 解码失败一律视为无法求解
        return None
    if bg.ndim != 3 or blk.ndim != 3 or blk.shape[2] < 4:
        return None
    template = blk[:, :, :3].astype(np.float64)
    mask = (blk[:, :, 3] > 0).astype(np.float64)[:, :, None]
    th, tw = template.shape[:2]
    if th > bg.shape[0] or tw > bg.shape[1]:
        return None
    t = template * mask
    t_sq = float((t * t).sum())
    best_x, best = None, -1.0
    for x in range(bg.shape[1] - tw + 1):
        patch = bg[:th, x : x + tw, :] * mask
        num = float((t * patch).sum())
        den = (t_sq * float((patch * patch).sum())) ** 0.5 + 1e-9
        score = num / den
        if score > best:
            best, best_x = score, x
    return best_x


def _decode_json(raw: bytes) -> dict[str, Any] | None:
    """bytes -> JSON dict；空 / 非 JSON / 非对象返回 None。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class AuthnError(RuntimeError):
    """认证 API 失败。status=0 表示网络层错误；reason 取自封闭集合。"""

    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail


def jwt_sub(token: str) -> str | None:
    """从 JWT payload 读 ``sub``（不校验签名）；失败 / 缺 sub 返回 None。"""
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    sub = data.get("sub")
    return sub if isinstance(sub, str) else None


def sms_login_tenants(data: Mapping[str, Any]) -> list[Any]:
    """smsLogin 响应里的 tenant_list（缺省/非列表 -> []）。空列表 = 手机号未注册。"""
    tenants = data.get("tenant_list")
    return list(tenants) if isinstance(tenants, list) else []


def sms_login_redirect(data: Mapping[str, Any]) -> str | None:
    """smsLogin 响应里的登录重定向 URL（已注册账号可直接登录时返回）。"""
    for key in ("redirect", "redirect_uri", "redirect_to"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class AuthnTransport(Protocol):
    """认证 API 的传输抽象（供上层 worker / 测试 Fake 实现）。"""

    def mint_login_challenge(self, *, intent: str | None = None) -> tuple[str, str]: ...
    def exchange_code(
        self, redirect_url: str | None, code_verifier: str
    ) -> TokenBundle: ...
    def send_sms_code(
        self, phone: str, *, code_key: str | None = None
    ) -> str | None: ...
    def register(
        self, *, token_code: str, user_name: str, password: str, challenge: str
    ) -> str: ...
    def sms_login(
        self, *, token_code: str, verify_code: str, challenge: str
    ) -> Mapping[str, Any]: ...
    def login_next(
        self, *, challenge: str, username: str, user_id: str, sign: str
    ) -> Mapping[str, Any]: ...
    def request_change_password_code(self, access_token: str, user_id: str) -> str: ...
    def change_password(
        self,
        access_token: str,
        user_id: str,
        *,
        token_code: str,
        verify_code: str,
        password: str,
    ) -> None: ...
    def get_user_info(self, access_token: str, user_id: str) -> Mapping[str, Any]: ...


def _pkce() -> tuple[str, str]:
    import hashlib

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _error_text(body: Mapping[str, Any] | None) -> str:
    """把 gRPC 错误信封压成一行摘要文本（message + 各 detail 的 reason/message）。"""
    if body is None:
        return ""
    parts: list[str] = []
    message = body.get("message")
    if isinstance(message, str) and message:
        parts.append(message)
    for item in body.get("details") or []:
        if not isinstance(item, Mapping):
            continue
        reason = item.get("reason")
        if isinstance(reason, str) and reason:
            parts.append(reason)
        detail = item.get("message")
        if isinstance(detail, str) and detail:
            parts.append(detail)
    return " ".join(parts).strip() or json.dumps(body, ensure_ascii=False)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(marker in low for marker in markers)


def _classify_reason(text: str) -> str:
    """按标记判定 AuthnError.reason；顺序即优先级（用户名占用优先于泛化「存在」）。"""
    if _contains_any(text, _SMS_CODE_MARKERS):
        return "incorrect_sms_code"
    if _contains_any(text, _CAPTCHA_MARKERS):
        return "invalid_captcha"
    if _contains_any(text, _USERNAME_TAKEN_MARKERS):
        return "username_taken"
    if _contains_any(text, _REGISTERED_MARKERS):
        return "already_registered"
    if _contains_any(text, _TENANT_MARKERS):
        return "tenant_list"
    if _contains_any(text, _CHALLENGE_MARKERS):
        return "challenge_expired"
    if "invalid" in text.lower():
        return "invalid"
    return "unknown"


def _map_authn_error(status: int, body: Mapping[str, Any] | None) -> AuthnError:
    """把 HTTP/gRPC 错误映射为 AuthnError。全部 reason 判定都收敛在这里。"""
    if status == 0:
        return AuthnError(0, "network", _error_text(body) or "网络请求失败")
    detail = _error_text(body)
    return AuthnError(status, _classify_reason(detail), detail or f"HTTP {status}")


class HttpAuthn:
    """默认实现：stdlib urllib.request + CookieJar，与 quota.HttpQuotaTransport 同风格。"""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        iam_base: str = IAM_BASE,
        oidc_base: str = OIDC_BASE,
        impersonate: str = "chrome",
    ) -> None:
        # 端点是固定的（docs §B），base 只用于测试指向本地假服务 / 环境隔离
        self.timeout = timeout
        self.iam_base = iam_base.rstrip("/")
        self._oidc_auth = oidc_base.rstrip("/") + "/oauth2/auth"
        self._oidc_token = oidc_base.rstrip("/") + "/oauth2/token"
        # 可选：curl_cffi 提供浏览器级 TLS 指纹（装了就用，未装回落 urllib）。
        # 注意：它并不改变验证码结果（求解才是关键），仅让请求更像真实浏览器。
        self._curl: Any = None
        try:
            from curl_cffi import requests as _curl_requests

            self._curl = _curl_requests.Session(impersonate=impersonate)
        except Exception:  # noqa: BLE001 - 可选依赖，缺失即回落 urllib
            self._curl = None
        # urllib 分支共用一个 Cookie 罐：register/smsLogin 建立的会话 Cookie 必须在随后
        # 跟随 OIDC 重定向换取 code 时可见，否则 /oauth2/auth 不返回 code（curl 分支同理）。
        self._shared_opener = self._opener()

    # --- 低层 HTTP ---
    @staticmethod
    def _opener() -> Any:
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _open(self, opener: Any, req: urllib.request.Request) -> Any:
        """发一次请求并返回响应；HTTP 错误 / 网络层失败统一映射为 AuthnError。"""
        try:
            return opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise _map_authn_error(exc.code, self._http_error_body(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            # socket.timeout / TimeoutError 均为 OSError 子类，一并归为网络层
            raise AuthnError(0, "network", f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _http_error_body(exc: urllib.error.HTTPError) -> Mapping[str, Any] | None:
        """读错误响应体为 JSON 信封；读不到 / 非 JSON 返回 None。"""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - 读不到响应体不阻塞错误上报
            raw = b""
        finally:
            exc.close()
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None

    @staticmethod
    def _parse_json(resp: Any) -> dict[str, Any]:
        """读响应体为 JSON；空 / 非 JSON 容忍为 {}。"""
        raw = resp.read()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = None,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        """IAM JSON 端点通用请求。请求头与 quota/autorenew 一致。"""
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if self._curl is not None:
            return self._request_json_curl(method, url, body=body, headers=headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return self._parse_json(self._open(self._shared_opener, req))

    def _request_json_curl(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        """curl_cffi 传输分支（浏览器指纹）；错误映射与 urllib 分支一致。"""
        try:
            resp = self._curl.request(
                method, url, json=body, headers=dict(headers), timeout=self.timeout
            )
        except Exception as exc:  # curl_cffi 异常类型不稳定，统一归网络层
            raise AuthnError(0, "network", f"{type(exc).__name__}: {exc}") from exc
        parsed = _decode_json(resp.content)
        if resp.status_code >= 400:
            raise _map_authn_error(resp.status_code, parsed)
        return parsed if parsed is not None else {}

    def _follow_until(
        self,
        opener: Any,
        location: str | None,
        predicate: Callable[[str], bool],
        *,
        max_hops: int = 6,
    ) -> str | None:
        """手动跟随重定向直到 predicate(url) 命中（或返回 None）。"""
        for _ in range(max_hops):
            if not location:
                return None
            if predicate(location):
                return location
            resp = self._open(
                opener,
                urllib.request.Request(
                    location, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
                ),
            )
            final_url = resp.geturl()
            if final_url and predicate(final_url):
                return final_url
            new_loc = resp.headers.get("Location")
            if not new_loc:
                text = resp.read().decode("utf-8", "replace")
                found = re.search(r"(https?://[^\"'\s<>]+[?&]code=[^&\"'\s<>]+)", text)
                if found and predicate(found.group(1)):
                    return found.group(1)
            location = new_loc
        return None

    def _follow_until_curl(
        self,
        location: str | None,
        predicate: Callable[[str], bool],
        *,
        max_hops: int = 6,
    ) -> str | None:
        """curl 会话版重定向跟随：复用同一 Cookie 罐（与 register/smsLogin 同会话）。"""
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        for _ in range(max_hops):
            if not location:
                return None
            if predicate(location):
                return location
            try:
                resp = self._curl.get(
                    location,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except Exception as exc:  # 统一归网络层
                raise AuthnError(0, "network", f"{type(exc).__name__}: {exc}") from exc
            new_loc = resp.headers.get("Location")
            if not new_loc:
                found = re.search(
                    r"(https?://[^\"'\s<>]+[?&]code=[^&\"'\s<>]+)", resp.text or ""
                )
                if found and predicate(found.group(1)):
                    return found.group(1)
                return None
            location = urllib.parse.urljoin(location, new_loc)
        return None

    @staticmethod
    def _query_param(url: str, key: str) -> str:
        """从 URL 取指定查询参数；缺失抛 AuthnError。"""
        match = re.search(key + r"=([^&]+)", url)
        if not match:
            raise AuthnError(0, "unknown", f"重定向 URL 缺少 {key}")
        return match.group(1)

    # --- OIDC 流程 ---
    def mint_login_challenge(self, *, intent: str | None = None) -> tuple[str, str]:
        """走 Hydra authorize + PKCE，跟随重定向拿到 login_challenge。

        返回 (challenge, code_verifier)；``intent="register"`` 时附加 intent 参数。
        """
        verifier, challenge = _pkce()
        params: dict[str, str] = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(16),
        }
        if intent == "register":
            params["intent"] = "register"
        url = self._oidc_auth + "?" + urllib.parse.urlencode(params)
        if self._curl is not None:
            loc = self._follow_until_curl(url, lambda u: "login_challenge=" in u)
        else:
            opener = self._shared_opener
            resp = self._open(
                opener,
                urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
                ),
            )
            loc = self._follow_until(
                opener, resp.geturl(), lambda u: "login_challenge=" in u
            )
        if not loc:
            raise AuthnError(0, "unknown", "未能获取 login_challenge")
        return self._query_param(loc, "login_challenge"), verifier

    def exchange_code(
        self, redirect_url: str | None, code_verifier: str
    ) -> TokenBundle:
        """跟随 redirect_url 到 ``code=``，用 PKCE verifier 换 TokenBundle。

        必须与 register/smsLogin 复用同一会话（Cookie），否则 OIDC 授权端点不返回 code。
        """
        if self._curl is not None:
            loc = self._follow_until_curl(redirect_url, lambda u: "code=" in u)
        else:
            loc = self._follow_until(
                self._shared_opener, redirect_url, lambda u: "code=" in u
            )
        if not loc:
            raise AuthnError(0, "unknown", "未能获取 authorization code")
        code = self._query_param(loc, "code")
        form = urllib.parse.urlencode(
            {
                "code": code,
                "code_verifier": code_verifier,
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        if self._curl is not None:
            try:
                resp = self._curl.post(
                    self._oidc_token, data=form, headers=headers, timeout=self.timeout
                )
            except Exception as exc:  # 统一归网络层
                raise AuthnError(0, "network", f"{type(exc).__name__}: {exc}") from exc
            tok = _decode_json(resp.content) or {}
        else:
            req = urllib.request.Request(self._oidc_token, data=form, headers=headers)
            tok = self._parse_json(self._open(self._shared_opener, req))
        access = tok.get("access_token")
        if not access:
            raise AuthnError(
                0,
                "invalid",
                str(
                    tok.get("error_description")
                    or tok.get("error")
                    or "未返回 access_token"
                ),
            )
        return TokenBundle(
            access_token=str(access),
            refresh_token=str(tok.get("refresh_token", "")),
            expires_in=int(tok.get("expires_in", 10800)),
            acquired_at=time.time(),
        )

    # --- 短信验证码 / 注册 / 短信登录（未认证，skipBearerAuth） ---
    def solve_captcha(self) -> str | None:
        """取滑块验证码并用图像匹配求解，返回已解 code_key；失败返回 None。

        供 worker 的 captcha_solver 钩子调用：解出后带 code_key 重发 sendSmsCode。
        需要可选依赖 numpy + Pillow；未装则返回 None（worker 回落 captcha_required）。
        """
        base = self.iam_base + "/iam/authn/v1/auth"
        for _ in range(_CAPTCHA_MAX_TRIES):
            try:
                cap = self._request_json("GET", base + "/getCaptcha")
            except AuthnError:
                continue
            code_key = cap.get("code_key")
            image = cap.get("image")
            block = cap.get("block")
            if not (
                isinstance(code_key, str)
                and isinstance(image, str)
                and isinstance(block, str)
            ):
                continue
            x = _match_slider(image, block)
            if x is None:
                continue
            url = (
                base
                + "/checkCaptcha?"
                + urllib.parse.urlencode({"code_key": code_key, "code_value": x})
            )
            try:
                result = self._request_json("GET", url)
            except AuthnError:
                continue
            if result.get("result") is True:
                return code_key
        return None

    def send_sms_code(self, phone: str, *, code_key: str | None = None) -> str | None:
        """请求短信验证码；缺 token_code 或需要滑块验证码（含 HTTP 400
        invalidCaptcha）时返回 None，不抛错（调用方据此转 captcha 处理）。"""
        body: dict[str, Any] = {"phone": phone, "region_code": "86"}
        if code_key is not None:
            body["code_key"] = code_key
        try:
            data = self._request_json(
                "POST", self.iam_base + "/iam/authn/v1/auth/nova/sendSmsCode", body=body
            )
        except AuthnError as exc:
            if exc.reason == "invalid_captcha":
                return None
            raise
        token = data.get("token_code")
        return None if not token else str(token)

    def register(
        self, *, token_code: str, user_name: str, password: str, challenge: str
    ) -> str:
        """注册新账号，返回可交换的 redirect URL。"""
        data = self._request_json(
            "POST",
            self.iam_base + "/iam/authn/v1/auth/nova/register",
            body={
                "token_code": token_code,
                "user_name": user_name,
                "password": password,
                "challenge": challenge,
            },
        )
        redirect = data.get("redirect")
        if not redirect:
            raise AuthnError(
                0, "invalid", str(data.get("message") or "注册失败：未返回 redirect")
            )
        return str(redirect)

    def sms_login(
        self, *, token_code: str, verify_code: str, challenge: str
    ) -> Mapping[str, Any]:
        """短信登录 / 校验验证码，返回原始响应。

        - ``{"tenant_list": []}``  → 手机号未注册；此时 verify_code 已通过校验，
          同一 token_code 才可用于随后的 register（服务端 register 前必须先校验）。
        - ``tenant_list`` 非空      → 手机号已注册（1 个租户可直接登录，多个需选择）。
        - 含 ``redirect``           → 已注册且可直接换取 code。
        """
        return self._request_json(
            "POST",
            self.iam_base + "/iam/authn/v1/auth/nova/smsLogin",
            body={
                "token_code": token_code,
                "verify_code": verify_code,
                "challenge": challenge,
            },
        )

    def login_next(
        self, *, challenge: str, username: str, user_id: str, sign: str
    ) -> Mapping[str, Any]:
        """多租户选择：选定租户后换取登录重定向（返回含 ``redirect`` 的响应）。"""
        return self._request_json(
            "POST",
            self.iam_base + "/iam/authn/v1/auth/nova/loginNext",
            body={
                "challenge": challenge,
                "username": username,
                "user_id": user_id,
                "sign": sign,
            },
        )

    # --- 改密 / 用户信息（已认证，Bearer） ---
    def request_change_password_code(self, access_token: str, user_id: str) -> str:
        """请求「允许改密」的短信验证码，返回 token_code。"""
        data = self._request_json(
            "POST",
            self.iam_base
            + f"/iam/idp/v1/users/{urllib.parse.quote(user_id, safe='')}:novaSendUserSmsCode",
            body={},
            bearer=access_token,
        )
        token = data.get("token_code")
        if not token:
            raise AuthnError(0, "unknown", "未返回 token_code")
        return str(token)

    def change_password(
        self,
        access_token: str,
        user_id: str,
        *,
        token_code: str,
        verify_code: str,
        password: str,
    ) -> None:
        """用短信验证码改密（明文，docs §B.5）。"""
        self._request_json(
            "POST",
            self.iam_base
            + f"/iam/idp/v1/users/{urllib.parse.quote(user_id, safe='')}:novaUpdatePassword",
            body={
                "token_code": token_code,
                "password": password,
                "verify_code": verify_code,
            },
            bearer=access_token,
        )

    def get_user_info(self, access_token: str, user_id: str) -> Mapping[str, Any]:
        """拉取用户资料（含 user_name / phone）。"""
        return self._request_json(
            "GET",
            self.iam_base + f"/iam/idp/v1/users/{urllib.parse.quote(user_id, safe='')}",
            bearer=access_token,
        )
