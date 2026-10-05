"""账号余量（额度）查询：登录换取控制台 JWT + 拉取积分池用量。

参考实现：shaobingtongzhi/sensenova-usage-dashboard。密码用 JWE(RSA-OAEP+A256GCM)
加密，依赖可选库 jwcrypto（懒加载）。本模块只做网络与解析，失败一律降级为错误文案。
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .config import Config

# ---------------------------------------------------------------- 常量
IAM_BASE = "https://iam.sensecoreapi.cn"
OIDC_AUTH = "https://platform.sensenova.cn/oauth2/auth"
OIDC_TOKEN = "https://signin.sensecore.cn/oauth2/token"
JWKS_URL = "https://signin.sensecore.cn/.well-known/jwks.json"
CLIENT_ID = "nova"
REDIRECT_URI = "https://platform.sensenova.cn"
SCOPE = "openid offline offline_access"
JWKS_KID = "public:hydra.openid.id-token"
USAGE_URL = "https://platform.sensenova.cn/lite/console/v1/tokenplan/pool-usage"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20

# 缓存 token 距过期不足该秒数时，下一次拉取前主动重登（spec §6.4）
TOKEN_EXP_MARGIN = 300.0


class QuotaUnavailable(RuntimeError):
    """余量功能不可用（如未安装 jwcrypto）。"""


class QuotaAuthError(RuntimeError):
    """登录/凭据校验失败。"""


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_in: int
    acquired_at: float


@dataclass(frozen=True)
class QuotaWindow:
    limit: float
    used: float
    remaining: float | None
    reset_at: int | None


@dataclass(frozen=True)
class QuotaPool:
    name: str
    pool_type: str
    model_ids: tuple[str, ...]
    window_5h: QuotaWindow | None
    window_7d: QuotaWindow | None
    grant_balance: float


@dataclass(frozen=True)
class WindowPair:
    h5: QuotaWindow | None
    d7: QuotaWindow | None


@dataclass
class AccountQuota:
    account: str
    user: str
    phone: str
    status: str          # "ok" | "unconfigured" | "error"
    error: str | None
    general: WindowPair | None
    flash_lite: WindowPair | None
    fetched_at: float | None


# ---------------------------------------------------------------- 纯函数
def jwt_exp(token: str) -> int | None:
    """从 JWT payload 读 exp（不校验签名）；失败返回 None。"""
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_optional_float(value: Any) -> float | None:
    """把原始值转 float；缺失 / 空串 / 非数值一律 None（区别于真实的 0）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_reset_at(value: Any) -> int | None:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _window(raw: Any) -> QuotaWindow | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    return QuotaWindow(
        limit=_to_float(raw.get("limit")),
        used=_to_float(raw.get("used")),
        remaining=_to_optional_float(raw.get("remaining")),
        reset_at=_to_reset_at(raw.get("reset_at")),
    )


def normalize_pools(raw: Mapping[str, Any]) -> list[QuotaPool]:
    pools = raw.get("pools") or []
    if isinstance(pools, Mapping):
        pools = list(pools.values())
    result: list[QuotaPool] = []
    for pool in pools:
        if not isinstance(pool, Mapping):
            continue
        model_ids = pool.get("model_ids") or []
        result.append(
            QuotaPool(
                name=str(pool.get("name", "")),
                pool_type=str(pool.get("pool_type", "")),
                model_ids=tuple(str(m) for m in model_ids),
                window_5h=_window(pool.get("window_5h")),
                window_7d=_window(pool.get("window_7d")),
                grant_balance=_to_float(pool.get("grant_balance")),
            )
        )
    return result


def _matches_flash_lite(pool: QuotaPool) -> bool:
    text = pool.name.lower() + " " + " ".join(pool.model_ids).lower()
    import re

    return re.search(r"flash[-_ ]?lite", text) is not None


def select_general(pools: list[QuotaPool]) -> QuotaPool | None:
    for pool in pools:
        if pool.pool_type == "default":
            return pool
    for pool in pools:
        if not _matches_flash_lite(pool):
            return pool
    return pools[0] if pools else None


def select_flash_lite(pools: list[QuotaPool]) -> QuotaPool | None:
    dedicated = [p for p in pools if p.pool_type != "default"]
    for pool in dedicated:
        if _matches_flash_lite(pool):
            return pool
    if len(dedicated) == 1:
        return dedicated[0]
    return None


def _pair(pool: QuotaPool | None) -> WindowPair | None:
    if pool is None:
        return None
    return WindowPair(h5=pool.window_5h, d7=pool.window_7d)


def map_account_quota(
    account: str, user: str, phone: str, pools: list[QuotaPool], *, fetched_at: float | None
) -> AccountQuota:
    return AccountQuota(
        account=account,
        user=user,
        phone=phone,
        status="ok",
        error=None,
        general=_pair(select_general(pools)),
        flash_lite=_pair(select_flash_lite(pools)),
        fetched_at=fetched_at,
    )


class QuotaTransport(Protocol):
    def login(self, user: str, password: str) -> TokenBundle: ...
    def fetch_pools(self, access_token: str) -> list[QuotaPool]: ...


def _jwcrypto() -> tuple[Any, Any, Any]:
    """懒加载 jwcrypto；缺失抛 QuotaUnavailable。"""
    try:
        from jwcrypto import jwe, jwk  # type: ignore
        from jwcrypto.common import json_encode  # type: ignore
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        raise QuotaUnavailable("未安装 jwcrypto，无法查询余量") from exc
    return jwe, jwk, json_encode


def _pkce() -> tuple[str, str]:
    import hashlib

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def encrypt_password(password: str, *, pubkey: Any) -> str:
    """用 RSA-OAEP + A256GCM 加密密码，返回紧凑 JWE。"""
    jwe, _jwk, json_encode = _jwcrypto()
    return jwe.JWE(
        password.encode("utf-8"),
        recipient=pubkey,
        protected=json_encode({"alg": "RSA-OAEP", "enc": "A256GCM"}),
    ).serialize(compact=True)


class HttpQuotaTransport:
    """默认实现：stdlib HTTP + 懒加载 jwcrypto。"""

    def __init__(self, *, timeout: int = REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self._pubkey = None

    # --- 低层 HTTP（手动跟随重定向） ---
    def _opener(self) -> Any:
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _get(
        self, opener: Any, url: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        return opener.open(req, timeout=self.timeout)

    def _public_key(self) -> Any:
        if self._pubkey is not None:
            return self._pubkey
        _jwe, jwk, _je = _jwcrypto()
        opener = self._opener()
        resp = opener.open(
            urllib.request.Request(JWKS_URL, headers={"User-Agent": USER_AGENT}), timeout=self.timeout
        )
        jwks = json.loads(resp.read().decode("utf-8"))
        key = next(k for k in jwks["keys"] if k.get("kid") == JWKS_KID)
        self._pubkey = jwk.JWK(kty="RSA", n=key["n"], e=key["e"])
        return self._pubkey

    def login(self, user: str, password: str) -> TokenBundle:
        import time as _time

        _jwe, _jwk, _je = _jwcrypto()
        opener = self._opener()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        resp = self._get(
            opener,
            OIDC_AUTH,
            params={
                "client_id": CLIENT_ID,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "state": state,
            },
        )
        challenge_loc = self._follow_until(opener, resp.geturl(), lambda u: "login_challenge=" in u)
        if not challenge_loc:
            raise QuotaAuthError("未能获取 login_challenge")
        import re

        login_challenge = re.search(r"login_challenge=([^&]+)", challenge_loc).group(1)
        enc = encrypt_password(password, pubkey=self._public_key())
        body = json.dumps(
            {"username": user, "password": enc, "challenge": login_challenge, "is_encrypt": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{IAM_BASE}/iam/authn/v1/auth/nova/login",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://platform.sensenova.cn",
                "Referer": "https://platform.sensenova.cn/",
                "User-Agent": USER_AGENT,
            },
        )
        resp = opener.open(req, timeout=self.timeout)
        payload = json.loads(resp.read().decode("utf-8"))
        redirect = payload.get("redirect")
        if not redirect:
            raise QuotaAuthError(str(payload.get("message") or payload.get("error") or "登录失败"))
        code_loc = self._follow_until(opener, redirect, lambda u: "code=" in u)
        if not code_loc:
            raise QuotaAuthError("未能获取 authorization code")
        code = re.search(r"[?&]code=([^&]+)", code_loc).group(1)
        token_req = urllib.request.Request(
            OIDC_TOKEN,
            data=urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "scope": SCOPE,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        )
        tok = json.loads(opener.open(token_req, timeout=self.timeout).read().decode("utf-8"))
        access = tok.get("access_token")
        if not access:
            raise QuotaAuthError(str(tok.get("error_description") or tok.get("error") or "未返回 access_token"))
        return TokenBundle(
            access_token=access,
            refresh_token=str(tok.get("refresh_token", "")),
            expires_in=int(tok.get("expires_in", 10800)),
            acquired_at=_time.time(),
        )

    def _follow_until(
        self,
        opener: Any,
        location: str | None,
        predicate: Callable[[str], bool],
        *,
        max_hops: int = 6,
    ) -> str | None:
        import re

        for _ in range(max_hops):
            if not location:
                return None
            if predicate(location):
                return location
            resp = opener.open(
                urllib.request.Request(location, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}),
                timeout=self.timeout,
            )
            final_url = resp.geturl()
            if final_url and predicate(final_url):
                return final_url
            new_loc = resp.headers.get("Location")
            if not new_loc:
                text = resp.read().decode("utf-8", "replace")
                m = re.search(r"(https?://[^\"'\s<>]+[?&]code=[^&\"'\s<>]+)", text)
                if m and predicate(m.group(1)):
                    return m.group(1)
            location = new_loc
        return None

    def fetch_pools(self, access_token: str) -> list[QuotaPool]:
        opener = self._opener()
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN",
                "Authorization": f"Bearer {access_token}",
                "Referer": "https://platform.sensenova.cn/console",
                "User-Agent": USER_AGENT,
            },
        )
        resp = opener.open(req, timeout=self.timeout)
        return normalize_pools(json.loads(resp.read().decode("utf-8")))


class QuotaService:
    """按账号查询余量，带缓存与容错。"""

    def __init__(
        self,
        config: Config,
        *,
        ttl: float = 300.0,
        error_ttl: float = 60.0,
        transport: QuotaTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._ttl = ttl
        self.error_ttl = error_ttl
        self._transport: QuotaTransport = transport or HttpQuotaTransport()
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, AccountQuota] = {}
        self._tokens: dict[str, TokenBundle] = {}

    def _accounts(self) -> list[tuple[str, str, str, str]]:
        return [(a.name, a.user, a.phone, a.password) for a in self._config.accounts]

    def verify_credentials(self, user: str, password: str) -> str | None:
        try:
            self._transport.login(user, password)
        except QuotaUnavailable as exc:
            return str(exc)
        except QuotaAuthError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        return None

    def _fetch_one(self, name: str, user: str, phone: str) -> AccountQuota:
        try:
            token = self._tokens.get(name)
            if token is not None and self._token_needs_refresh(token):
                token = None
                self._tokens.pop(name, None)
            if token is None:
                token = self._transport.login(user, self._password_of(name))
                self._tokens[name] = token
            try:
                pools = self._transport.fetch_pools(token.access_token)
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
                # 401：token 失效 → 丢弃、重新登录一次，再重试一次 fetch（spec §6.4）
                self._tokens.pop(name, None)
                token = self._transport.login(user, self._password_of(name))
                self._tokens[name] = token
                pools = self._transport.fetch_pools(token.access_token)
            return map_account_quota(name, user, phone, pools, fetched_at=self._clock())
        except QuotaUnavailable as exc:
            return AccountQuota(name, user, phone, "error", str(exc), None, None, self._clock())
        except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其它
            self._tokens.pop(name, None)
            return AccountQuota(
                name, user, phone, "error", f"{type(exc).__name__}: {exc}", None, None, self._clock()
            )

    def _token_needs_refresh(self, token: TokenBundle) -> bool:
        """判断缓存的 token 是否应在下一次拉取前重登。

        优先用 JWT 的 ``exp``（真实过期时刻，epoch 秒）；解析不出时退回到
        ``acquired_at + expires_in`` 估算（``acquired_at`` 为 0 视为未知，不判过期）。
        """
        exp = jwt_exp(token.access_token)
        if exp is not None:
            return exp - time.time() <= TOKEN_EXP_MARGIN
        if token.acquired_at > 0:
            return time.time() - token.acquired_at >= max(0.0, token.expires_in - TOKEN_EXP_MARGIN)
        return False

    def _password_of(self, name: str) -> str:
        for a in self._config.accounts:
            if a.name == name:
                return a.password
        return ""

    def snapshot(self, *, force: bool = False) -> list[AccountQuota]:
        with self._lock:
            out: list[AccountQuota] = []
            for name, user, phone, password in self._accounts():
                if not (user and password):
                    out.append(
                        AccountQuota(name, user, phone, "unconfigured", None, None, None, None)
                    )
                    continue
                cached = self._cache.get(name)
                if cached is not None and not force:
                    ttl = self._ttl if cached.status == "ok" else self.error_ttl
                    if cached.fetched_at is not None and self._clock() - cached.fetched_at < ttl:
                        out.append(cached)
                        continue
                aq = self._fetch_one(name, user, phone)
                self._cache[name] = aq
                out.append(aq)
            return out

    def close(self) -> None:
        self._cache.clear()
        self._tokens.clear()
