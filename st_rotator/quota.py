"""账号余量（额度）查询：登录换取控制台 JWT + 拉取积分池用量。

参考实现：shaobingtongzhi/sensenova-usage-dashboard。密码用 JWE(RSA-OAEP+A256GCM)
加密，依赖可选库 jwcrypto（懒加载）。本模块只做网络与解析，失败一律降级为错误文案。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

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
    remaining: float
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
        remaining=_to_float(raw.get("remaining")),
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
