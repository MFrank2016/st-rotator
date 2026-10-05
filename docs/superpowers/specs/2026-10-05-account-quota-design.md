# 设计文档：账号余量（额度）展示

- 日期：2026-10-05
- 状态：待评审
- 参考实现：`shaobingtongzhi/sensenova-usage-dashboard`（`auth_login.py` / `dashboard.py`，commit e6b05f1）

## 1. 背景与目标

st-rotator 目前把多个账号的多把 API Key 池化成一个本地端点，控制台能看每把 Key 的健康度，
但**看不到各账号在上游还剩多少额度**。上游（商汤日日新）按账号的 5 小时 / 7 天滚动窗口限流，
运营者需要一个直观的「余量」视图来判断该不该加 Key、什么时候会被限。

目标：在控制台管理页面新增「账号余量」面板，展示每个**配置了凭据**的账号的剩余额度。

## 2. 非目标（YAGNI）

- 不做按模型的用量拆分（上游 `pool-usage` 只给到 pool 粒度，没有按模型数字）。
- 不做告警、通知、历史曲线、持久化用量。
- 不改动网关的推理转发路径与 Key 轮换逻辑。
- 不让基础网关产生任何**硬**依赖（见 §4）。

## 3. 已批准的关键决策

1. 密码加密采用**可选依赖 `jwcrypto`**（懒加载）；未安装时余量面板降级为「不可用」，其余功能不受影响。
2. 登录凭据为**用户名 + 密码**（与参考实现一致）。
3. 配置新增三个可选字段：`user`（登录用户名）、`phone`（**仅展示标签**，不参与登录）、`password`（登录密码）。
4. 服务端缓存余量结果 **TTL ≈ 300 秒**，并提供手动「刷新余量」。
5. Docker 镜像**默认安装 `jwcrypto`**（让余量功能开箱可用）；Python 源码仍保持懒加载、零硬依赖。

## 4. 依赖策略

- 新增 `requirements-quota.txt`：内容为 `jwcrypto>=1.5`（供可选安装 / Docker 使用）。
- `quota.py` 内**延迟导入** `jwcrypto`；导入失败时抛出 `QuotaUnavailable("未安装 jwcrypto")`，
  由 `QuotaService` 转成该账号 `status="error"`，**不影响进程其它功能**。
- `requests` **不使用**；HTTP 用标准库 `urllib.request` + `http.cookiejar` + 自定义「不自动跟随重定向」的
  handler（需要手动逐跳解析 `login_challenge` / `code`）。

## 5. 配置变更（`config.py` → `AccountConfig`）

```jsonc
{
  "name": "账号1",
  "api_keys": ["${SENSENOVA_KEY_1}"],
  "rpm_limit": 2,
  "user": "${SENSENOVA_USER_1}",        // 新增，可选，支持 ${ENV}
  "phone": "13800000000",               // 新增，可选，仅展示
  "password": "${SENSENOVA_PASSWORD_1}" // 新增，可选，支持 ${ENV}
}
```

- 三个字段均为 `str = ""`；沿用现有的 `${ENV}` / `${ENV:-默认值}` 展开（`Config.from_dict` 已对字符串标量展开）。
- **查询条件**：仅当 `user` 与 `password` **都非空**时，该账号才参与余量查询；否则 `status="unconfigured"`，
  前端不渲染该账号。`phone` 不参与判定。
- `Config.to_dict()`：**包含** `user` / `phone`（非密钥，便于日志），**绝不包含** `password`。

## 6. 新模块 `st_rotator/quota.py`

### 6.1 常量（对齐参考实现）

```
IAM_BASE   = "https://iam.sensecoreapi.cn"
OIDC_AUTH  = "https://platform.sensenova.cn/oauth2/auth"
OIDC_TOKEN = "https://signin.sensecore.cn/oauth2/token"
JWKS_URL   = "https://signin.sensecore.cn/.well-known/jwks.json"
CLIENT_ID    = "nova"
REDIRECT_URI = "https://platform.sensenova.cn"
SCOPE        = "openid offline offline_access"
JWKS_KID     = "public:hydra.openid.id-token"
USAGE_PATH   = "/lite/console/v1/tokenplan/pool-usage"
USER_AGENT   = "<Chrome UA，见参考实现>"
```

### 6.2 登录流程 `login(user, password) -> TokenBundle`

完全复刻参考实现的五步（`auth_login.py`）：

1. 生成 PKCE（verifier/challenge=S256）+ state；`GET OIDC_AUTH`（带上述参数，不自动跟随），
   手动逐跳跟随重定向（最多 6 跳），直到 URL 含 `login_challenge=`，取出 `login_challenge`。
2. 拉取 `JWKS_URL`，取 `kid == JWKS_KID` 的 RSA 公钥（只取 `n`/`e` 重建，去掉 use/alg 约束）；
   用 JWE 紧凑序列化加密密码：`alg=RSA-OAEP`、`enc=A256GCM`。
3. `POST {IAM_BASE}/iam/authn/v1/auth/nova/login`，JSON
   `{"username":user,"password":<jwe>,"challenge":login_challenge,"is_encrypt":true}`，
   头 `Content-Type: application/json`、`Origin: https://platform.sensenova.cn`、`Referer: https://platform.sensenova.cn/`。
   成功返回含 `redirect`；失败返回含 `message`/`error`。
4. 手动跟随 `redirect` 直到 URL 含 `code=`（也解析响应体里的 meta refresh / JS 跳转），取 `authorization_code`。
5. `POST OIDC_TOKEN`（form：`grant_type=authorization_code`、`code`、`code_verifier`、`client_id`、`redirect_uri`、`scope`），
   取 `access_token` / `refresh_token` / `expires_in`（默认 10800）。

返回 `TokenBundle(access_token: str, refresh_token: str, expires_in: int, acquired_at: float)`。
JWT 的 `exp` 用 base64 解 payload 读取（**不校验签名**，仅用于判断是否临近过期）。

### 6.3 余量获取 `fetch_pool_usage(jwt) -> list[QuotaPool]`

`GET https://platform.sensenova.cn/lite/console/v1/tokenplan/pool-usage`
头：`Authorization: Bearer <jwt>`、`Accept: application/json, text/plain, */*`、
`Referer: https://platform.sensenova.cn/console`、`User-Agent: <Chrome UA>`。

响应 `{"pools": [...]}`，每个 pool 归一化为：

```python
@dataclass(frozen=True)
class QuotaWindow:
    limit: float
    used: float
    remaining: float
    reset_at: int | None     # Unix 秒；缺失/0 -> None

@dataclass(frozen=True)
class QuotaPool:
    name: str
    pool_type: str           # "default" | "dedicated"（其它值原样保留）
    model_ids: list[str]
    window_5h: QuotaWindow | None
    window_7d: QuotaWindow | None
    grant_balance: float
```

- 所有数值是字符串，用 `float()` 解析；`reset_at` 解析为 `int`（`"0"`/空 -> `None`）。
- 解析失败/字段缺失 -> 对应窗口置 `None`，不抛异常。

### 6.4 `QuotaService`

```python
class QuotaService:
    def __init__(self, config: Config, *, ttl: float = 300.0,
                 transport: QuotaTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    def snapshot(self, *, force: bool = False) -> list[AccountQuota]: ...
    def close(self) -> None: ...
```

- 逐账号处理：未配置凭据 -> `status="unconfigured"`（不发起网络请求）。
- 已配置：优先用缓存；缓存过期或 `force=True` 时，确保 JWT 有效（`exp` 临近则重登），
  再拉 `pool-usage`；失败置 `status="error"` 并记录脱敏后的 `error` 文案。
- **401 处理**：清 JWT 重登一次再重试；仍失败则 `error`。
- 缓存键为账号名；缓存值含 `AccountQuota` 与 `fetched_at`。
- 线程安全：内部 `threading.Lock`；`snapshot` 串行化，避免并发重复登录。
- 网络超时（连接/读取）显式设置（默认 10s/20s），单账号失败不影响其它账号。
- `transport` 抽象：定义 `QuotaTransport` 协议（`login(user,password)` / `fetch(jwt)`），
  默认实现走 stdlib HTTP；测试注入 Fake。

```python
@dataclass
class AccountQuota:
    account: str
    user: str
    phone: str
    status: str                 # "ok" | "unconfigured" | "error"
    error: str | None
    pools: list[QuotaPool]
    fetched_at: float | None
```

## 7. 控制台后端（`ui.py` / `proxy.py`）

- `ConsoleState` 新增可选字段 `quota: QuotaService | None`（`cli` 在构造时注入；仅当存在配置了凭据的账号时创建）。
- 新接口 `GET /api/quota`：
  - 鉴权：与其他 `/api/*` 一致（Bearer 或 Cookie；未鉴权 401 JSON）。
  - `?refresh=1` 触发 `snapshot(force=True)`，否则用缓存。
  - 返回 `{"accounts": [ {account,user,phone,status,error,pools,fetched_at} ]}`，
    `pools` 内含归一化窗口；**不含 password / access_token**。
  - `quota is None` 时返回 `{"accounts": []}`。
- `/api/state` **不**内嵌余量（保持轻量），前端单独拉 `/api/quota`。

## 8. 前端面板（`dashboard.py`）

- 新卡片「账号余量」，放在「网关接入信息」附近。
- 每个**已配置**账号一行；账号内每个 pool 显示：`通用/专属` 标签、`5h 余量/上限`、`7d 余量/上限`、
  下次重置时间（本地时区 `MM-DD HH:MM`）、`赠送余额`（`grant_balance`）。
- 未配置凭据的账号**不渲染**。
- 顶部「刷新余量」按钮 -> `GET /api/quota?refresh=1`；页面加载时拉一次（走缓存）。
- 状态：加载中 / 正常 / 该账号 `error`（显示文案）/ 空（没有已配置账号时显示提示）。
- 所有数值用 `esc()` 转义后渲染；无 CDN、沿用现有暗色样式。

## 9. 安全

- 密码、JWT **仅在服务端内存中**；绝不写入日志（日志对 `password`/token 一律脱敏）、绝不返回浏览器。
- `password` 支持 `${ENV}`，推荐不放明文。
- 文档明确：配置了 `user`/`password` 后，网关会**主动向商汤登录端点发起请求**换取 JWT。
- 余量接口与其它控制台接口同等的鉴权；未配置即无任何凭据相关网络行为。

## 10. 错误处理

| 情况 | 表现 |
|---|---|
| 未安装 `jwcrypto` 且账号已配置 | 该账号 `status="error"`，文案「未安装 jwcrypto，无法查询余量」；进程其余功能正常 |
| 登录失败（用户名/密码错、被风控） | 该账号 `status="error"`，透传上游 `message`（脱敏）；网关推理不受影响 |
| 用量接口 401 | 清 JWT 重登一次再试；仍失败 -> `error` |
| 用量接口 403 | `error`「权限不足」 |
| 网络超时/异常 | `error`（含异常类型）；不阻塞控制台其它请求 |
| 上游返回结构异常 | 该账号 `error`，或窗口字段降级为 `None` |

## 11. 测试策略（stdlib `unittest`，不访问真实商汤）

- `tests/test_quota.py`（注入 Fake `QuotaTransport`）：
  - 归一化：字符串数值、`reset_at`、缺失字段、`pool_type`、`grant_balance`。
  - `QuotaService`：未配置账号 `unconfigured` 且不发请求；TTL 内命中缓存不重复请求；`force` 触发刷新；
    JWT 临近过期自动重登；401 -> 重登一次；登录异常 -> `error` 且不影响其它账号。
  - 缺 `jwcrypto`（monkeypatch 导入失败）-> `error` 文案。
- `tests/test_config_account_credentials.py`：三个字段解析、`${ENV}` 展开、`password` 不出现在 `to_dict`。
- 控制台接口：`/api/quota` 未鉴权 401；已鉴权返回结构正确；响应体不含 `password`/`access_token`。
- 前端：`DASHBOARD_HTML` 含「账号余量」面板骨架（存在性断言）。
- 可选 live smoke：`tests/` 之外的脚本，仅当提供真实凭据时运行（默认跳过）。

## 12. Docker / 文档

- `Dockerfile`：在现有基础上增加 `RUN pip install --no-cache-dir -r requirements-quota.txt`
  （使余量功能在镜像内可用；基础源码仍懒加载）。同步更新「零 pip」表述为「核心零依赖，余量功能需 jwcrypto」。
- `.dockerignore`：无需额外改动（`requirements-quota.txt` 需被 COPY）。
- `README.md`：新增「账号余量」小节（配置字段、可选依赖安装、安全说明、Docker 说明）；
  `config.example.json` 增加三个空字段。

## 13. 数据流

```
浏览器 ──GET /api/quota──► proxy(鉴权) ──► ConsoleState.quota.snapshot()
                                               │  缓存命中? 直接返回
                                               │  否则 QuotaService:
                                               │    ensure JWT(exp 临近则 login)
                                               │    GET pool-usage (Bearer JWT)
                                               └──► 归一化 -> AccountQuota[]
浏览器 ◄── {accounts:[...]}（无 password/JWT）──┘
```

## 14. 验收标准（场景契约）

- **S1 未配置**：账号无 `user`/`password` -> `/api/quota` 中该账号 `status="unconfigured"`，前端不显示。
- **S2 正常**：配置凭据 + Fake transport 返回样例 -> `/api/quota` 返回归一化 pools；前端显示 5h/7d 余量与重置时间。
- **S3 登录失败**：transport 抛错 -> 该账号 `status="error"`；网关 `/v1/*`、`/healthz` 正常。
- **S4 缺依赖**：模拟 `jwcrypto` 缺失 -> `status="error"`「未安装 jwcrypto」；其余功能正常。
- **S5 缓存**：TTL 内连续两次 `snapshot` 只发一次网络请求；`force=True` 再发一次。
- **S6 安全**：`/api/quota` 未鉴权 401；已鉴权响应体不含 `password`/`access_token`；日志无明文密码。
- **S7 回归**：不带凭据的配置解析与既有全部测试通过；基础镜像/核心代码零硬依赖。

## 15. 风险与未决

- 上游登录/用量接口无公开文档，是**逆向**得到的；上游若变更（端点、字段、加密算法）会导致余量失败——
  设计上将其隔离在 `quota.py` 且失败只降级，不影响网关核心。
- 手写 JWE 依赖 `jwcrypto` 的正确性；因此不自行实现 AES-GCM。
- 账号级限流风控：频繁登录可能触发上游风控，故默认缓存 + 手动刷新，避免轮询。
