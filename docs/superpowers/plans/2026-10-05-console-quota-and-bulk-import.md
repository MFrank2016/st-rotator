# 控制台增强（账号余量 + 批量新增 Key）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 st-rotator 控制台展示每个已配置凭据账号的 5h/7d 余量（KPI 卡片 + Key 池字段），并新增支持两种格式的「批量新增 Key」弹窗，逐行校验后导入。

**Architecture:** 新增独立模块 `st_rotator/quota.py`（登录 + 用量获取 + 池映射 + 带缓存的 `QuotaService`），通过 `QuotaTransport` 协议抽象网络（默认 stdlib HTTP + 可选 `jwcrypto`），测试注入 Fake。控制台后端在 `ui.py` 暴露 `GET /api/quota` 与 `POST /api/keys/import`（二者都在既有 `/api/*` 路由下，proxy 无需改动）；前端 `dashboard.py` 在既有 KPI 卡片与 Key 池表格上追加字段，并新增一个原生 `<dialog>` 导入弹窗。凭据随账号存入 `config.json`，永不回显。

**Tech Stack:** Python 3.10+ 标准库（`http.server`/`urllib`/`hmac`/`hashlib`/`dataclasses`）；可选 `jwcrypto`（仅余量/凭据校验用，懒加载）；前端为单文件内联 HTML/CSS/JS（零 CDN）；测试用 stdlib `unittest`。

**Spec:** `docs/superpowers/specs/2026-10-05-account-quota-design.md`

## Global Constraints

- **零硬依赖**：核心代码只用标准库；`jwcrypto` 必须**懒加载**，缺失时余量/凭据校验降级为错误文案，不得影响网关推理。
- **Python 3.10+** 语法（`X | Y`）；全量类型标注；中文注释/文案。
- **密钥安全**：`password` 与明文 `apikey` 绝不写入日志、绝不回显；API 响应里的 key 一律用 `mask_key` 脱敏；`Config.to_dict()` 不含 `password`。
- **不破坏既有行为**：网关转发、Key 轮换、既有 `/api/*`、`/healthz`、`/stats`、控制台登录/Cookie 鉴权全部保持不变；既有 45 个测试必须继续通过。
- **接口鉴权**：`/api/quota`、`/api/keys/import` 与其它 `/api/*` 一样，需要 Bearer 或会话 Cookie，未鉴权返回 JSON 401。
- **池映射**（spec §6.5）：通用池 = `pool_type=="default"`；Flash-Lite 专属池 = `pool_type!="default"` 且 `name`/`model_ids` 匹配 `flash[-_ ]?lite`（忽略大小写），否则若恰有一个非 default 池则兜底取它。**必须先排除 `default` 再匹配**。
- **批量导入**（spec §16）：分隔符 `--`；1 段=纯 key，4 段=`手机--用户名--密码--apikey`，其它段数=格式错误；单次上限 50 行。
- **测试运行**：仓库根目录 `python3 -m unittest discover -s tests -t . -v`。
- **提交**：每个任务一个原子提交，沿用仓库风格（中文 `feat:`/`fix:`/`docs:`）。

## Review Focus

（spec 隐含、但没有任何任务的测试覆盖，且最可能坑到真实使用者的输入/情形——每行都在其所属任务里补了对应测试。）

1. **上游字段缺省**：某账号的 `pools` 里没有 `window_5h`/`window_7d`，或 `remaining`/`reset_at` 为 `"0"`/空 —— 必须降级为 `None`（前端 `—`），不得抛异常或显示 `0` 误导。
2. **凭证/依赖缺失**：账号只填了 `user` 没填 `password`（或反之）—— 视为「未配置」，不发起任何网络请求。
3. **批量行内多分隔符**：`apikey` 里意外含 `--`，或格式 2 某段为空（如 `手机----密码--key`）—— 必须判定为该行错误并给出明确原因，不得静默吞掉。
4. **重复导入**：同一把 key 在同一批内出现两次、或已存在于池中 —— 只导入一次，其余标「重复」。
5. **并发/超时**：登录或用量接口超时/连接失败 —— 只把该账号标 `error`，控制台其它请求与网关推理不受影响。

---

### Task 1: 配置新增账号凭据字段

**Files:**
- Modify: `st_rotator/config.py`（`AccountConfig` 与其 `from_dict`/`to_dict`）
- Test: `tests/test_config_account_credentials.py`

**Interfaces:**
- Produces: `AccountConfig.user: str`, `AccountConfig.phone: str`, `AccountConfig.password: str`（均默认 `""`，支持 `${ENV}`）；`AccountConfig.to_dict()` 含 `user`/`phone`，**不含** `password`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_account_credentials.py
import os
import unittest
from unittest import mock

from st_rotator.config import AccountConfig, Config
from st_rotator.errors import ConfigError


def _cfg(**acct):
    base = {"name": "账号1", "api_keys": ["sk-abc12345"], "rpm_limit": 2}
    base.update(acct)
    return {"base_url": "http://127.0.0.1:9/v1", "accounts": [base]}


class AccountCredentialsTest(unittest.TestCase):
    def test_defaults_empty(self):
        acct = Config.from_dict(_cfg()).accounts[0]
        self.assertEqual(acct.user, "")
        self.assertEqual(acct.phone, "")
        self.assertEqual(acct.password, "")

    def test_env_expansion(self):
        with mock.patch.dict(os.environ, {"SN_USER": "u1", "SN_PW": "p1"}):
            acct = Config.from_dict(
                _cfg(user="${SN_USER}", phone="13800000000", password="${SN_PW}")
            ).accounts[0]
        self.assertEqual(acct.user, "u1")
        self.assertEqual(acct.phone, "13800000000")
        self.assertEqual(acct.password, "p1")

    def test_env_missing_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(password="${ULW_MISSING_PW_XYZ}"))

    def test_to_dict_includes_user_phone_not_password(self):
        acct = Config.from_dict(_cfg(user="u1", phone="138", password="secret")).accounts[0]
        d = acct.to_dict()
        self.assertEqual(d["user"], "u1")
        self.assertEqual(d["phone"], "138")
        self.assertNotIn("password", d)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_config_account_credentials -v`
Expected: FAIL —— `AccountConfig` 无 `user`/`phone`/`password` 字段（`TypeError`/`AttributeError` 或未知字段 `ConfigError`）。

- [ ] **Step 3: Write minimal implementation**

在 `st_rotator/config.py` 的 `AccountConfig`（`@dataclass`）字段中，紧随 `api_keys` 之后加入：

```python
    user: str = ""
    phone: str = ""
    password: str = ""
```

在 `AccountConfig.from_dict` 中，对这三个字段做 `${ENV}` 展开（与 `api_keys` 同法）。将现有展开 `api_keys` 的 `from_dict` 改写为同时展开凭据字段，例如：

```python
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccountConfig":
        data = dict(data)
        name = str(data.pop("name", "") or "").strip()
        if not name:
            raise ConfigError("account 缺少 name")
        keys = data.pop("api_keys", None)
        if not isinstance(keys, list) or not keys:
            raise ConfigError(f"账号 {name} 缺少 api_keys")
        for field_name in ("user", "phone", "password"):
            if field_name in data and data[field_name] is not None:
                data[field_name] = expand_env(str(data[field_name])).strip()
        known = set(cls.__dataclass_fields__) - {"api_keys"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"账号 {name} 存在未知字段: {sorted(unknown)}")
        keys = [expand_env(str(k)).strip() for k in keys if str(k).strip()]
        return cls(name=name, api_keys=keys, **data)
```

在 `AccountConfig.to_dict()` 中，`api_keys` 一行后加入：

```python
            "user": self.user,
            "phone": self.phone,
            # 注意：绝不输出 password
```

（保持既有 `to_dict` 结构；不要加入 `password`。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_config_account_credentials -v`
Expected: PASS（4 个）。

- [ ] **Step 5: Run the full suite (regression)**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: 既有测试全绿 + 新增 4 个通过。

- [ ] **Step 6: Commit**

```bash
git add st_rotator/config.py tests/test_config_account_credentials.py
git commit -m "feat: 账号配置新增可选的 user/phone/password 凭据字段"
```

---

### Task 2: ConfigStore 写入账号凭据

**Files:**
- Modify: `st_rotator/config.py`（`ConfigStore`）
- Test: `tests/test_config_account_credentials.py`（追加一个类）

**Interfaces:**
- Consumes: `ConfigStore.load(path)`（既有）、`AccountConfig` 字段（Task 1）。
- Produces: `ConfigStore.set_account_credentials(name: str, *, user: str, phone: str, password: str) -> None`，定点更新 `_raw` 里对应账号的字段并原子落盘；`name` 不存在则 `ConfigError`。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_config_account_credentials.py
import json
import tempfile
from pathlib import Path

from st_rotator.config import ConfigStore


class SetAccountCredentialsTest(unittest.TestCase):
    def _store(self, tmp: str) -> ConfigStore:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(_cfg()), encoding="utf-8")
        return ConfigStore.load(path)

    def test_sets_credentials_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set_account_credentials("账号1", user="u1", phone="138", password="p1")
            store.save()
            reloaded = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
            acct = reloaded["accounts"][0]
            self.assertEqual(acct["user"], "u1")
            self.assertEqual(acct["phone"], "138")
            self.assertEqual(acct["password"], "p1")
            self.assertEqual(store.config.accounts[0].user, "u1")

    def test_unknown_account_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ConfigError):
                store.set_account_credentials("不存在", user="u", phone="", password="p")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_config_account_credentials -v`
Expected: FAIL —— `ConfigStore` 无 `set_account_credentials`。

- [ ] **Step 3: Write minimal implementation**

在 `ConfigStore` 中，仿照既有 `add_key` 的定点修改风格加入：

```python
    def set_account_credentials(
        self, name: str, *, user: str, phone: str, password: str
    ) -> None:
        """定点写入某账号的登录凭据（保留其余字段与占位符）。"""
        accounts = self._accounts_raw()
        for item in accounts:
            if isinstance(item, dict) and item.get("name") == name:
                item["user"] = user
                item["phone"] = phone
                item["password"] = password
                self.reload()
                return
        raise ConfigError(f"账号 {name} 不存在")
```

（`reload()` 为既有方法，用于把 `_raw` 重新解析回 `self.config`；若实现里方法名不同，沿用本文件既有做法把 `_raw` 同步进 `self.config`。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_config_account_credentials -v`
Expected: PASS（累计 6 个）。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/config.py tests/test_config_account_credentials.py
git commit -m "feat: ConfigStore 支持定点写入账号凭据"
```

---

### Task 3: quota.py —— 类型、纯函数与池映射

**Files:**
- Create: `st_rotator/quota.py`
- Test: `tests/test_quota.py`

**Interfaces:**
- Produces（后续任务依赖的精确签名）：
  - 数据类型：`TokenBundle(access_token, refresh_token, expires_in, acquired_at)`；`QuotaWindow(limit, used, remaining, reset_at)`；`QuotaPool(name, pool_type, model_ids, window_5h, window_7d, grant_balance)`；`WindowPair(h5, d7)`；`AccountQuota(account, user, phone, status, error, general, flash_lite, fetched_at)`。
  - 异常：`QuotaUnavailable(RuntimeError)`、`QuotaAuthError(RuntimeError)`。
  - `jwt_exp(token: str) -> int | None`
  - `normalize_pools(raw: Mapping[str, Any]) -> list[QuotaPool]`
  - `select_general(pools: list[QuotaPool]) -> QuotaPool | None`
  - `select_flash_lite(pools: list[QuotaPool]) -> QuotaPool | None`
  - `map_account_quota(account: str, user: str, phone: str, pools: list[QuotaPool], *, fetched_at: float | None) -> AccountQuota`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quota.py
import base64
import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_quota -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'st_rotator.quota'`。

- [ ] **Step 3: Write minimal implementation**

创建 `st_rotator/quota.py`：

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_quota -v`
Expected: PASS（8 个）。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/quota.py tests/test_quota.py
git commit -m "feat: quota 模块类型与池映射纯函数"
```

---

### Task 4: quota.py —— 密码 JWE 加密与 HTTP transport

**Files:**
- Modify: `st_rotator/quota.py`
- Test: `tests/test_quota.py`（追加类）

**Interfaces:**
- Produces:
  - `QuotaTransport`（`Protocol`）：`login(self, user: str, password: str) -> TokenBundle`；`fetch_pools(self, access_token: str) -> list[QuotaPool]`。
  - `encrypt_password(password: str, *, pubkey: Any) -> str`（紧凑 JWE；`jwcrypto` 缺失抛 `QuotaUnavailable`）。
  - `class HttpQuotaTransport: ...`（默认实现，stdlib HTTP + 懒加载 `jwcrypto`）。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_quota.py
from unittest import mock


class EncryptPasswordTest(unittest.TestCase):
    def test_missing_jwcrypto_raises_unavailable(self):
        with mock.patch.dict("sys.modules", {"jwcrypto": None}):
            with self.assertRaises(quota.QuotaUnavailable):
                quota.encrypt_password("pw", pubkey=object())

    def test_encrypts_with_jwcrypto_when_present(self):
        jwe = __import__("jwcrypto", fromlist=["jwe"])  # noqa: F401
        raise unittest.SkipTest("需要真实 jwcrypto 公钥，仅在有依赖时手动运行")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_quota.EncryptPasswordTest -v`
Expected: FAIL —— `quota` 无 `encrypt_password` / `QuotaUnavailable` 未在该路径触发。

- [ ] **Step 3: Write minimal implementation**

在 `quota.py` 追加：

```python
import secrets
import urllib.parse
import urllib.request
from typing import Protocol


class QuotaTransport(Protocol):
    def login(self, user: str, password: str) -> TokenBundle: ...
    def fetch_pools(self, access_token: str) -> list[QuotaPool]: ...


def _jwcrypto():
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
```

（`HttpQuotaTransport` 的具体实现见本任务 Step 3b，供 Task 5 使用；它内部使用标准库
`urllib.request` 手动跟随重定向、`http.cookiejar` 保持 cookie，并调用上面的
`_pkce`/`encrypt_password`。测试通过注入 Fake transport 覆盖，故此处不为其写网络测试。）

**Step 3b: 追加 `HttpQuotaTransport`（无独立测试，由 Task 5 的 Fake 注入覆盖逻辑）**

```python
class HttpQuotaTransport:
    """默认实现：stdlib HTTP + 懒加载 jwcrypto。"""

    def __init__(self, *, timeout: int = REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self._pubkey = None

    # --- 低层 HTTP（手动跟随重定向） ---
    def _opener(self):
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _get(self, opener, url, *, params=None, follow=True):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            return opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:  # 3xx 已由 opener 跟随；4xx/5xx 抛
            raise

    def _public_key(self):
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

    def _follow_until(self, opener, location, predicate, *, max_hops: int = 6):
        import html
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_quota -v`
Expected: PASS（`EncryptPasswordTest.test_missing_jwcrypto_raises_unavailable` 通过；另一个为 skip）。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/quota.py tests/test_quota.py
git commit -m "feat: quota 密码 JWE 加密与 stdlib HTTP transport"
```

---

### Task 5: quota.py —— QuotaService（缓存 / 刷新 / 401 / 降级）

**Files:**
- Modify: `st_rotator/quota.py`
- Test: `tests/test_quota.py`（追加类）

**Interfaces:**
- Consumes: `QuotaTransport`（Task 4）、`Config.accounts[].user/phone/password`（Task 1）、`normalize_pools`/`map_account_quota`（Task 3）。
- Produces:
  - `class QuotaService:` `__init__(self, config, *, ttl=300.0, transport=None, clock=time.monotonic)`；`snapshot(self, *, force=False) -> list[AccountQuota]`；`verify_credentials(self, user, password) -> str | None`（None=成功，否则错误文案）；`close(self) -> None`。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_quota.py
from st_rotator.config import Config


class _FakeTransport:
    def __init__(self, *, fail_login=False, fail_fetch=False):
        self.calls = {"login": 0, "fetch": 0}
        self.fail_login = fail_login
        self.fail_fetch = fail_fetch

    def login(self, user, password):
        self.calls["login"] += 1
        if self.fail_login:
            raise quota.QuotaAuthError("用户名或密码错误")
        return quota.TokenBundle("jwt", "", 10800, 0.0)

    def fetch_pools(self, access_token):
        self.calls["fetch"] += 1
        if self.fail_fetch:
            raise RuntimeError("boom")
        return quota.normalize_pools(SAMPLE)


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
        self.assertEqual(t.calls, {"login": 0, "fetch": 0})
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

    def test_verify_credentials(self):
        svc = self._svc(_FakeTransport())
        self.assertIsNone(svc.verify_credentials("u1", "p1"))
        bad = self._svc(_FakeTransport(fail_login=True))
        self.assertIn("用户名或密码错误", bad.verify_credentials("u1", "x"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_quota.QuotaServiceTest -v`
Expected: FAIL —— `quota` 无 `QuotaService`。

- [ ] **Step 3: Write minimal implementation**

在 `quota.py` 追加：

```python
import threading
import time


class QuotaService:
    """按账号查询余量，带缓存与容错。"""

    def __init__(self, config, *, ttl: float = 300.0, transport: QuotaTransport | None = None,
                 clock=time.monotonic) -> None:
        self._config = config
        self._ttl = ttl
        self._transport = transport or HttpQuotaTransport()
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, AccountQuota] = {}
        self._tokens: dict[str, TokenBundle] = {}

    def _accounts(self):
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

    def _fetch_one(self, name, user, phone) -> AccountQuota:
        try:
            token = self._tokens.get(name)
            if token is None:
                token = self._transport.login(user, self._password_of(name))
                self._tokens[name] = token
            pools = self._transport.fetch_pools(token.access_token)
            return map_account_quota(name, user, phone, pools, fetched_at=self._clock())
        except QuotaUnavailable as exc:
            return AccountQuota(name, user, phone, "error", str(exc), None, None, None)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其它
            self._tokens.pop(name, None)
            return AccountQuota(name, user, phone, "error", f"{type(exc).__name__}: {exc}", None, None, None)

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
                    out.append(AccountQuota(name, user, phone, "unconfigured", None, None, None, None))
                    continue
                cached = self._cache.get(name)
                if cached is not None and not force and self._clock() - (cached.fetched_at or 0) < self._ttl:
                    out.append(cached)
                    continue
                aq = self._fetch_one(name, user, phone)
                self._cache[name] = aq
                out.append(aq)
            return out

    def close(self) -> None:
        self._cache.clear()
        self._tokens.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_quota -v`
Expected: PASS（全部）。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/quota.py tests/test_quota.py
git commit -m "feat: QuotaService 缓存/刷新/容错"
```

---

### Task 6: 控制台后端 —— /api/quota 接口

**Files:**
- Modify: `st_rotator/ui.py`（`ConsoleState` 字段、`handle` 路由、`quota_snapshot`）
- Test: `tests/test_console_quota.py`

**Interfaces:**
- Consumes: `QuotaService.snapshot()`（Task 5）。
- Produces: `ConsoleState.quota: QuotaService | None = None`；`ConsoleState.quota_payload(self, *, force: bool) -> dict`（返回 `{"accounts": [ ... ]}`，不含密钥）；`handle` 支持 `GET /api/quota`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_console_quota.py
import unittest

from st_rotator import quota
from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState

SAMPLE = {
    "pools": [
        {"name": "通用积分池", "pool_type": "default", "model_ids": ["m"],
         "window_5h": {"remaining": "40979", "reset_at": "1791206310"},
         "window_7d": {"remaining": "45196", "reset_at": "1791321510"}},
        {"name": "Flash-Lite积分池", "pool_type": "dedicated", "model_ids": ["sensenova-6.8-flash-lite"],
         "window_5h": {"remaining": "60000", "reset_at": "1791206310"},
         "window_7d": {"remaining": "600000", "reset_at": "1791321510"}},
    ]
}


class _FakeTransport:
    def login(self, user, password):
        return quota.TokenBundle("jwt", "", 10800, 0.0)

    def fetch_pools(self, access_token):
        return quota.normalize_pools(SAMPLE)


class _FakeRotator:
    def __init__(self, config):
        self.config = config


class ConsoleQuotaTest(unittest.TestCase):
    def _state(self):
        cfg = Config.from_dict({
            "base_url": "http://127.0.0.1:9/v1",
            "accounts": [{"name": "账号1", "api_keys": ["k1"], "user": "u1", "phone": "138", "password": "p1"}],
        })
        svc = quota.QuotaService(cfg, transport=_FakeTransport())
        return ConsoleState(store=None, rotator=_FakeRotator(cfg), quota=svc)  # type: ignore[arg-type]

    def test_quota_payload_shape_and_no_secret(self):
        payload = self._state().quota_payload(force=False)
        acct = payload["accounts"][0]
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["user"], "u1")
        self.assertAlmostEqual(acct["general"]["h5"]["remaining"], 40979.0)
        self.assertAlmostEqual(acct["flash_lite"]["d7"]["remaining"], 600000.0)
        self.assertNotIn("password", str(payload))

    def test_no_service_returns_empty(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1", "accounts": [{"name": "a", "api_keys": ["k"]}]})
        st = ConsoleState(store=None, rotator=_FakeRotator(cfg), quota=None)  # type: ignore[arg-type]
        self.assertEqual(st.quota_payload(force=False), {"accounts": []})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_console_quota -v`
Expected: FAIL —— `ConsoleState` 无 `quota` 字段 / `quota_payload`。

- [ ] **Step 3: Write minimal implementation**

在 `st_rotator/ui.py`：
1. `ConsoleState` 数据类新增字段（放在 `metrics` 附近，带默认值）：`quota: "QuotaService | None" = None`。
2. 顶部 `from .quota import QuotaService`（若担心循环导入，用 `TYPE_CHECKING` + 字符串标注，运行时按需 import）。
3. 新增方法：

```python
    def quota_payload(self, *, force: bool) -> dict[str, Any]:
        """把余量快照转成 JSON（不含任何密钥）。"""
        if self.quota is None:
            return {"accounts": []}
        accounts = []
        for aq in self.quota.snapshot(force=force):
            accounts.append({
                "account": aq.account,
                "user": aq.user,
                "phone": aq.phone,
                "status": aq.status,
                "error": aq.error,
                "fetched_at": aq.fetched_at,
                "general": _pair_payload(aq.general),
                "flash_lite": _pair_payload(aq.flash_lite),
            })
        return {"accounts": accounts}
```

4. 模块级辅助：

```python
def _window_payload(window) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"remaining": window.remaining, "reset_at": window.reset_at}


def _pair_payload(pair) -> dict[str, Any] | None:
    if pair is None:
        return None
    return {"h5": _window_payload(pair.h5), "d7": _window_payload(pair.d7)}
```

5. 在 `handle` 的 `GET` 分支加入：

```python
                if path == "/api/quota":
                    force = bool(_first_int(query, "refresh", 0))
                    return UiResponse.json(self.quota_payload(force=force))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_console_quota -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/ui.py tests/test_console_quota.py
git commit -m "feat: 控制台新增 /api/quota 接口"
```

---

### Task 7: 控制台后端 —— /api/keys/import 批量导入

**Files:**
- Modify: `st_rotator/ui.py`（`ConsoleState.import_keys`、`handle` POST 路由、解析辅助）
- Test: `tests/test_keys_import.py`

**Interfaces:**
- Consumes: `ConfigStore.set_account_credentials`（Task 2）、`QuotaService.verify_credentials`（Task 5）、既有 `rotator.probe_key`、`pool.find_key`、`store.add_key`、`rotator.add_key`、`mask_key`。
- Produces: `parse_import_lines(raw: str) -> list[tuple[int, str, tuple[str, ...]]]`（`(行号, 原文, 段)`）；`ConsoleState.import_keys(raw: str, *, account: str | None, max_concurrency: int) -> UiResponse`；`handle` 支持 `POST /api/keys/import`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_keys_import.py
import json
import tempfile
import unittest
from pathlib import Path

from st_rotator import quota
from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState, parse_import_lines


class _FakePool:
    def __init__(self): self.keys = set()
    def find_key(self, key): return object() if key in self.keys else None


class _FakeRotator:
    def __init__(self, config):
        self.config = config
        self.pool = _FakePool()
        self.probed = []
    def probe_key(self, key):
        self.probed.append(key)
        return ("ok", "未校验") if key.startswith("sk-good") else ("invalid", "401")
    def add_key(self, key, *, account, max_concurrency=4, rpm_limit=None):
        self.pool.keys.add(key)
        class _Item:
            key_id = "id-" + key[-4:]
            masked = key[:6] + "..." + key[-4:]
            account_ = account
        it = _Item(); it.account = account
        return it


class _FakeTransport:
    def login(self, user, password):
        if password == "right":
            return quota.TokenBundle("jwt", "", 10800, 0.0)
        raise quota.QuotaAuthError("用户名或密码错误")
    def fetch_pools(self, token):
        return []


def _store(tmp):
    p = Path(tmp) / "config.json"
    p.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1",
                             "accounts": [{"name": "账号1", "api_keys": ["sk-seed1234"]}]}), encoding="utf-8")
    return ConfigStore.load(p)


class ParseImportTest(unittest.TestCase):
    def test_parses_forms_and_skips_blank(self):
        raw = "sk-aaa\n\n138--u1--p1--sk-bbb\nbad--seg"
        rows = parse_import_lines(raw)
        self.assertEqual([r[0] for r in rows], [1, 3, 4])
        self.assertEqual(rows[0][2], ("sk-aaa",))
        self.assertEqual(rows[1][2], ("138", "u1", "p1", "sk-bbb"))
        self.assertEqual(rows[2][2], ("bad", "seg"))


class ImportKeysTest(unittest.TestCase):
    def _state(self, tmp):
        store = _store(tmp)
        fake = _FakeRotator(store.config)
        svc = quota.QuotaService(store.config, transport=_FakeTransport())
        return ConsoleState(store=store, rotator=fake, quota=svc)  # type: ignore[arg-type]

    def test_format1_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("sk-good1\nsk-bad1", account=None, max_concurrency=4)
            body = resp.payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(body["summary"]["error"], 1)
            self.assertTrue(any("无效" in r["reason"] for r in body["results"] if r["status"] == "error"))

    def test_format2_login_and_key_and_credentials_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("138--u1--right--sk-good2", account=None, max_concurrency=4)
            self.assertEqual(resp.payload["summary"]["ok"], 1)
            acct = next(a for a in st.store.config.accounts if a.name == "u1")
            self.assertEqual(acct.user, "u1")
            self.assertEqual(acct.phone, "138")
            self.assertEqual(acct.password, "right")

    def test_format2_bad_login_rejected_no_password_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("138--u1--WRONGPW--sk-good3", account=None, max_concurrency=4)
            body = resp.payload
            self.assertEqual(body["summary"]["ok"], 0)
            self.assertNotIn("WRONGPW", json.dumps(body, ensure_ascii=False))

    def test_duplicate_within_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            body = st.import_keys("sk-good9\nsk-good9", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(body["summary"]["skipped"], 1)

    def test_bad_segment_count_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            body = st.import_keys("a--b--c", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["error"], 1)
            self.assertIn("格式", body["results"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_keys_import -v`
Expected: FAIL —— 无 `parse_import_lines` / `import_keys`。

- [ ] **Step 3: Write minimal implementation**

在 `st_rotator/ui.py` 追加模块级解析函数与常量：

```python
MAX_IMPORT_LINES = 50


def parse_import_lines(raw: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """逐行解析导入文本：返回 [(行号, 原文, 段元组)]，跳过空行。"""
    rows: list[tuple[int, str, tuple[str, ...]]] = []
    for idx, line in enumerate((raw or "").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        parts = tuple(p.strip() for p in text.split("--"))
        rows.append((idx, text, parts))
    return rows
```

在 `ConsoleState` 追加：

```python
    def import_keys(self, raw: str, *, account: str | None, max_concurrency: int) -> UiResponse:
        rows = parse_import_lines(raw)
        if not rows:
            return UiResponse.error("没有解析出任何内容")
        if len(rows) > MAX_IMPORT_LINES:
            return UiResponse.error(f"一次最多导入 {MAX_IMPORT_LINES} 行，当前 {len(rows)} 行")

        results: list[dict[str, Any]] = []
        added: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        ok = error = skipped = 0

        for line_no, text, parts in rows:
            def _row(status, reason, masked):
                return {"line": line_no, "input_masked": masked, "format": len(parts), "status": status, "reason": reason}

            if len(parts) not in (1, 4):
                results.append(_row("error", "格式错误：应为 1 段（纯 key）或 4 段（手机--用户名--密码--apikey）", _mask(text)))
                error += 1
                continue

            if len(parts) == 1:
                key = parts[0]
                phone = user = password = ""
                target = account
            else:
                phone, user, password, key = parts
                target = user or phone

            if key in seen_keys:
                results.append(_row("skipped", "重复：本批已出现", _mask(key))); skipped += 1; continue
            if self.rotator.pool.find_key(key) is not None:
                results.append(_row("skipped", "重复：已在池中", _mask(key))); skipped += 1; continue
            seen_keys.add(key)

            # 格式 2：先校验凭据
            if len(parts) == 4:
                if not (user and password):
                    results.append(_row("error", "格式错误：用户名或密码为空", _mask(text))); error += 1; continue
                if self.quota is None:
                    results.append(_row("error", "未安装 jwcrypto，无法校验凭据", _mask(text))); error += 1; continue
                err = self.quota.verify_credentials(user, password)
                if err is not None:
                    results.append(_row("error", f"凭据无效：{err}", _mask(text))); error += 1; continue

            # 校验 key
            if len(key) < 8:
                results.append(_row("error", "Key 长度不足 8 位", _mask(key))); error += 1; continue
            verdict, detail = self.rotator.probe_key(key)
            if verdict == "invalid":
                results.append(_row("error", f"Key 无效：{detail}", _mask(key))); error += 1; continue

            # 写入
            try:
                with self.lock:
                    if len(parts) == 4:
                        names = self.store.account_names()
                        if target not in names:
                            # 新建账号后写入凭据与 key
                            self.store.add_key(key, target, max_concurrency=max_concurrency)
                            self.store.set_account_credentials(target, user=user, phone=phone, password=password)
                        else:
                            self.store.add_key(key, target, max_concurrency=max_concurrency)
                            self.store.set_account_credentials(target, user=user, phone=phone, password=password)
                    else:
                        target_name = target or next_account_name(self.store.account_names())
                        target = target_name
                        self.store.add_key(key, target_name, max_concurrency=max_concurrency)
                    item = self.rotator.add_key(key, account=target, max_concurrency=max_concurrency)
                    self.store.reload()
                    self.rotator.config.accounts = list(self.store.config.accounts)
                added.append({"id": item.key_id, "key": item.masked, "account": item.account})
                results.append(_row("ok", "已导入", item.masked)); ok += 1
            except Exception as exc:  # noqa: BLE001
                results.append(_row("error", str(exc), _mask(key))); error += 1

        if added:
            self._save_and_log(f"通过控制台批量导入 {len(added)} 把 Key")
        return UiResponse.json({
            "ok": bool(added),
            "added": added,
            "results": results,
            "summary": {"ok": ok, "error": error, "skipped": skipped},
        })
```

在 `handle` 的 `POST` 分支加入：

```python
                if path == "/api/keys/import":
                    return self.import_keys(
                        str(payload.get("lines") or ""),
                        account=(str(payload.get("account")).strip() or None) if payload.get("account") else None,
                        max_concurrency=_clamp_int(payload.get("max_concurrency"), 4, 1, 64),
                    )
```

（复用既有 `_mask`/`_clamp_int`/`next_account_name`/`self.lock`/`_save_and_log`。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_keys_import -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/ui.py tests/test_keys_import.py
git commit -m "feat: 控制台新增 /api/keys/import 批量导入（两种格式 + 逐行校验）"
```

---

### Task 8: 前端 —— KPI 卡片 + Key 池余量列 + 倒计时

**Files:**
- Modify: `st_rotator/dashboard.py`（HTML 骨架 + JS）
- Test: `tests/test_dashboard_quota.py`

**Interfaces:**
- Consumes: `GET /api/quota`（Task 6）。
- Produces: `DASHBOARD_HTML` 含 6 个新 KPI 卡片与 Key 池 6 个新列表头；`S.quota` 状态；`renderQuota()`；`fmtCountdown()`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_quota.py
import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardQuotaTest(unittest.TestCase):
    def test_kpi_and_columns_present(self):
        for text in [
            "通用积分 5h 累计余量", "通用积分 7d 累计余量",
            "Flash-Lite 专属积分 5h 累计余量", "Flash-Lite 专属积分 7d 累计余量",
            "5h 重置倒计时", "7d 重置倒计时",
            "通用 5h 余量", "FL 专属 5h 余量",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_quota_fetch_present(self):
        self.assertIn("/api/quota", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_dashboard_quota -v`
Expected: FAIL —— 文案不存在。

- [ ] **Step 3: Write minimal implementation**

在 `dashboard.py` 的 `DASHBOARD_HTML` 内：
1. JS 状态对象 `S` 增加 `quota: []`。
2. 新增函数（放入脚本区）：

```javascript
function fmtCountdown(resetAt, longForm) {
  if (!resetAt) return "—";
  var secs = resetAt - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "—";
  var d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return longForm ? (d + " d " + h + " h " + m + " m") : (h + " h " + m + " m");
}

function quotaAggregate() {
  var agg = {g5: 0, g7: 0, f5: 0, f7: 0, reset5: null, reset7: null};
  (S.quota || []).forEach(function (a) {
    if (a.status !== "ok") return;
    function add(pair, k5, k7, r5, r7) {
      if (!pair) return;
      if (pair.h5) { agg[k5] += pair.h5.remaining || 0; agg[r5] = minReset(agg[r5], pair.h5.reset_at); }
      if (pair.d7) { agg[k7] += pair.d7.remaining || 0; agg[r7] = minReset(agg[r7], pair.d7.reset_at); }
    }
    add(a.general, "g5", "g7", "reset5", "reset7");
    add(a.flash_lite, "f5", "f7", "reset5", "reset7");
  });
  return agg;
}

function minReset(a, b) { if (!b) return a; return a ? Math.min(a, b) : b; }

async function fetchQuota(force) {
  try { S.quota = (await api("/api/quota" + (force ? "?refresh=1" : ""))).accounts || []; }
  catch (e) { S.quota = []; }
}
```

3. `renderKpis` 末尾追加 6 张卡片（用 `quotaAggregate()` + `fmtCountdown`）。
4. Key 池表头字符串后追加 6 列；`renderPool` 行内按 `k.account` 关联 `S.quota` 输出对应值（未配置/无数据 `—`）。
5. 页面刷新流程里先 `await fetchQuota(false)` 再 `renderKpis`/`renderPool`；新增 1s `setInterval` 更新带 `data-reset-at` 的元素。

（完整 HTML/JS 片段由实现者按上述函数与既有 `renderKpis`/`renderPool` 结构落地；测试只断言上述关键文案与 `/api/quota` 存在。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_dashboard_quota -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/dashboard.py tests/test_dashboard_quota.py
git commit -m "feat: 控制台 KPI 卡片与 Key 池新增余量列与倒计时"
```

---

### Task 9: 前端 —— 批量新增 Key 弹窗

**Files:**
- Modify: `st_rotator/dashboard.py`
- Test: `tests/test_dashboard_quota.py`（追加断言）

**Interfaces:**
- Consumes: `POST /api/keys/import`（Task 7）。
- Produces: `DASHBOARD_HTML` 含「批量新增」按钮与 `<dialog id="import-dialog">`；`onImportKeys()`。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_dashboard_quota.py
class DashboardImportTest(unittest.TestCase):
    def test_import_ui_present(self):
        for text in ["批量新增", 'id="import-dialog"', "/api/keys/import", "手机--用户名--密码--apikey"]:
            self.assertIn(text, DASHBOARD_HTML)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_dashboard_quota.DashboardImportTest -v`
Expected: FAIL。

- [ ] **Step 3: Write minimal implementation**

在 `dashboard.py` 的 Key 池面板标题行加入按钮：

```html
<button class="primary" id="btn-import">批量新增</button>
```

在 `<body>` 末尾加入弹窗与脚本：

```html
<dialog id="import-dialog">
  <h2>批量新增 Key</h2>
  <p class="muted" style="font-size:11.5px">每行一条，支持两种格式：<br>
    1) 纯 apikey：<code>sk-xxxx</code><br>
    2) 手机--用户名--密码--apikey：<code>13800000000--user--pass--sk-xxxx</code></p>
  <textarea id="import-lines" rows="8" class="mono" placeholder="sk-xxxx&#10;13800000000--user--pass--sk-yyyy"></textarea>
  <label class="field"><span>归属账号（仅格式 1，留空自动命名）</span><input id="import-account" placeholder="留空自动命名"></label>
  <div class="row"><button class="primary" id="btn-import-run">导入</button>
    <button id="btn-import-close">关闭</button></div>
  <div id="import-result"></div>
</dialog>
```

```javascript
$("btn-import").onclick = function () { $("import-dialog").showModal(); };
$("btn-import-close").onclick = function () { $("import-dialog").close(); };
$("btn-import-run").onclick = onImportKeys;

async function onImportKeys() {
  var lines = $("import-lines").value;
  if (!lines.trim()) { toast("请先粘贴内容", "err"); return; }
  var btn = $("btn-import-run"); btn.disabled = true; btn.innerHTML = '<span class="spin">◌</span> 校验中…';
  try {
    var result = await api("/api/keys/import", { method: "POST", body: JSON.stringify({
      lines: lines, account: $("import-account").value.trim(), max_concurrency: 4 }) });
    var s = result.summary || {};
    $("import-result").innerHTML = "<table><tr><th>行</th><th>输入</th><th>判定</th><th>原因</th></tr>" +
      (result.results || []).map(function (r) {
        return "<tr><td>" + r.line + "</td><td class='mono'>" + esc(r.input_masked) + "</td><td>" +
          esc(r.status) + "</td><td>" + esc(r.reason) + "</td></tr>"; }).join("") + "</table>";
    toast("导入完成：成功 " + (s.ok||0) + "，失败 " + (s.error||0) + "，跳过 " + (s.skipped||0), (s.ok ? "ok" : "err"));
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "导入"; }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_dashboard_quota -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/dashboard.py tests/test_dashboard_quota.py
git commit -m "feat: 控制台批量新增 Key 弹窗"
```

---

### Task 10: CLI 注入 QuotaService

**Files:**
- Modify: `st_rotator/cli.py`（`cmd_ui`/`cmd_tray` 构造 `ConsoleState` 处）
- Test: `tests/test_cli_quota.py`

**Interfaces:**
- Consumes: `QuotaService`（Task 5）、`ConsoleState.quota`（Task 6）。
- Produces: `build_quota_service(config) -> QuotaService | None`（无任何账号配置凭据时返回 `None`）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_quota.py
import unittest

from st_rotator.cli import build_quota_service
from st_rotator.config import Config


class BuildQuotaServiceTest(unittest.TestCase):
    def test_none_when_no_credentials(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1",
                                "accounts": [{"name": "a", "api_keys": ["k"]}]})
        self.assertIsNone(build_quota_service(cfg))

    def test_returns_service_when_credentials_present(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1",
                                "accounts": [{"name": "a", "api_keys": ["k"], "user": "u", "password": "p"}]})
        svc = build_quota_service(cfg)
        self.assertIsNotNone(svc)
        svc.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli_quota -v`
Expected: FAIL —— 无 `build_quota_service`。

- [ ] **Step 3: Write minimal implementation**

在 `cli.py` 加入：

```python
def build_quota_service(config: Config):
    """仅当存在配置了 user+password 的账号时才创建 QuotaService。"""
    from .quota import QuotaService

    if not any(a.user and a.password for a in config.accounts):
        return None
    return QuotaService(config)
```

在 `cmd_ui`、`cmd_tray` 构造 `ConsoleState(...)` 时加入 `quota=build_quota_service(config)`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cli_quota -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add st_rotator/cli.py tests/test_cli_quota.py
git commit -m "feat: CLI 按需注入 QuotaService"
```

---

### Task 11: 打包与文档（可选依赖 / Docker / README / 示例）

**Files:**
- Create: `requirements-quota.txt`
- Modify: `Dockerfile`, `.dockerignore`, `config.example.json`, `README.md`

**Interfaces:** 无代码接口（仅打包/文档）。

- [ ] **Step 1: 新增 `requirements-quota.txt`**

```
jwcrypto>=1.5
```

- [ ] **Step 2: 修改 `Dockerfile`**

在 `COPY config.example.json ./config.example.json` 之后、`USER 10001` 之前加入：

```dockerfile
# 余量功能所需的可选依赖（核心仍零依赖；不配置账号凭据时不会用到）
COPY requirements-quota.txt ./requirements-quota.txt
RUN pip install --no-cache-dir -r requirements-quota.txt
```

- [ ] **Step 3: 修改 `.dockerignore`**

确认 `requirements-quota.txt` **不被**忽略（当前规则未排除它，无需改动）；若后续加入了 `*.txt` 之类规则需加 `!requirements-quota.txt`。

- [ ] **Step 4: 修改 `config.example.json`**

在每个账号对象中加入：

```json
      "user": "",
      "phone": "",
      "password": "",
```

- [ ] **Step 5: 更新 `README.md`**

新增「账号余量」小节：字段说明（`user`/`phone`/`password`、`${ENV}`、`phone` 仅展示）、可选依赖安装（`pip install -r requirements-quota.txt`）、安全说明（密码不回显、登录为真实网络请求）、Docker 已默认安装。新增「批量新增 Key」小节：两种格式与逐行校验。同步更新「注意事项」。

- [ ] **Step 6: 验证**

```bash
python3 -c "import json; d=json.load(open('config.example.json')); assert 'user' in d['accounts'][0]; print('ok')"
docker build -t st-rotator:quota-test . && echo BUILD_OK
```

- [ ] **Step 7: Commit**

```bash
git add requirements-quota.txt Dockerfile .dockerignore config.example.json README.md
git commit -m "docs: 账号余量与批量导入的依赖、Docker 与 README 说明"
```

---

## Self-Review

**1. Spec coverage：**
- §3 决策 / §4 依赖 → Task 4、Task 10、Task 11（懒加载 jwcrypto、`requirements-quota.txt`）。
- §5 配置字段 → Task 1、Task 2。
- §6.1 常量 / §6.2 登录 / §6.3 用量 / §6.4 QuotaService / §6.5 池映射 → Task 3、Task 4、Task 5。
- §7 接口 `/api/quota` → Task 6；`/api/keys/import` → Task 7。
- §8.1 KPI / §8.2 Key 池列 / §8.3 倒计时 → Task 8。
- §9 安全（不回显/脱敏）→ Task 1（to_dict）、Task 7（脱敏断言）、Task 6（不含 password）。
- §10 错误处理 → Task 5（error 降级）、Task 7（逐行原因）。
- §11 测试 → Task 1/2/3/4/5/6/7/8/9/10。
- §12 Docker/文档 → Task 11。
- §13 数据流 → Task 6+8 覆盖。
- §14 S1..S10 → S1（Task 5 unconfigured）、S2（Task 6）、S3（Task 5 error）、S4（Task 5 QuotaUnavailable）、S5（Task 5 cache）、S6（Task 6 no-secret）、S7（各任务回归步骤）、S8（Task 8 空态）、S9（Task 7 格式1）、S10（Task 7 格式2）。
- §16 批量导入 → Task 7（后端）+ Task 9（前端）。

**2. Placeholder scan：** 无 TBD/TODO；Task 8/9 的 HTML/JS 给出了函数体与关键文案，实现者按既有结构落地（测试断言文案与 `/api/quota`）。

**3. Type consistency：** `QuotaWindow{limit,used,remaining,reset_at}`、`WindowPair{h5,d7}`、`AccountQuota{...general,flash_lite...}`、`QuotaService.snapshot/verify_credentials`、`ConsoleState.quota/quota_payload/import_keys`、`parse_import_lines`、`build_quota_service` 在各任务间命名一致。

**4. Review Focus：** 五条均已在所属任务补测试 —— ①缺省窗口/`reset_at=0`（Task 3 `test_normalize_missing_window_is_none`/`test_normalize_reset_at_zero_is_none`）；②仅填一半凭据（Task 5 `test_unconfigured_skips_network`）；③段数非法/空段（Task 7 `test_bad_segment_count_is_error`）；④批内重复（Task 7 `test_duplicate_within_batch`）；⑤超时/异常降级（Task 5 `test_fetch_failure_marks_error_without_breaking_others`）。

---

## Execution Handoff

（见对话中的执行方式选择。）
