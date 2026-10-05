# 设计文档：控制台增强 —— 账号余量展示 + 批量新增 Key

- 日期：2026-10-05
- 状态：待评审
- 参考实现：`shaobingtongzhi/sensenova-usage-dashboard`（`auth_login.py` / `dashboard.py`，commit e6b05f1）

## 1. 背景与目标

st-rotator 目前把多个账号的多把 API Key 池化成一个本地端点，控制台能看每把 Key 的健康度，
但**看不到各账号在上游还剩多少额度**。上游（商汤日日新）按账号的 5 小时 / 7 天滚动窗口限流，
运营者需要一个直观的「余量」视图来判断该不该加 Key、什么时候会被限。

目标：
1. 在控制台展示每个**配置了凭据**的账号的剩余额度（KPI 卡片 + Key 池字段，见 §8）。
2. 在 Key 池面板新增「批量新增 Key」弹窗，支持两种粘贴格式并逐行校验（见 §16）。

> 本文档同时覆盖两个控制台增强（余量展示 + 批量导入）；两者共享「账号凭据」模型，故合并为一份 spec。

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
头：`Authorization: Bearer <jwt>`、`Accept: application/json, text/plain, */*`、`Accept-Language: zh-CN`、
`Referer: https://platform.sensenova.cn/console`、`User-Agent: <Chrome UA>`。
（浏览器 curl 里还带 `oauth2_*` Cookie，但**只需 Bearer JWT**即可；参考实现仅用 Bearer 就能成功。）

响应含顶层 `plan`（本次不用）与 `pools: [...]`；每个 pool 归一化为：

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
@dataclass(frozen=True)
class WindowPair:
    h5: QuotaWindow | None
    d7: QuotaWindow | None

@dataclass
class AccountQuota:
    account: str
    user: str
    phone: str
    status: str                 # "ok" | "unconfigured" | "error"
    error: str | None
    general: WindowPair | None      # 通用积分池（pool_type=default）
    flash_lite: WindowPair | None   # Flash-Lite 专属积分池
    fetched_at: float | None
```

### 6.5 池映射（通用 / Flash-Lite 专属）

- **通用池** = `pool_type == "default"` 的池（实测名「通用积分池」）；若无 default 池，则取第一个「不匹配 flash-lite」的池。
- **Flash-Lite 专属池** = `pool_type != "default"` 且其 `name` 或任一 `model_ids` 匹配正则 `flash[-_ ]?lite`（忽略大小写）。
  实测该池 `name="Flash-Lite积分池"`、`pool_type="dedicated"`、`model_ids=["sensenova-6.7-flash-lite","sensenova-6.8-flash-lite"]`。
  ⚠️ 通用池的 `model_ids` **也**包含 flash-lite 模型，所以**必须**先排除 `default` 再匹配，不能只按 `model_ids` 匹配。
- 若无匹配但恰好只有一个非 default 池，则取该池（兜底）。
- 每个池映射为 `WindowPair(h5=window_5h, d7=window_7d)`；窗口缺失 -> `None`。
- 无法确定某类池 -> 对应字段为 `None`，前端显示 `—`。

## 7. 控制台后端（`ui.py` / `proxy.py`）

- `ConsoleState` 新增可选字段 `quota: QuotaService | None`（`cli` 在构造时注入；仅当存在配置了凭据的账号时创建）。
- 新接口 `GET /api/quota`：
  - 鉴权：与其他 `/api/*` 一致（Bearer 或 Cookie；未鉴权 401 JSON）。
  - `?refresh=1` 触发 `snapshot(force=True)`，否则用缓存。
  - 返回 `{"accounts": [ {account,user,phone,status,error,general,flash_lite,fetched_at} ]}`；
    `general` / `flash_lite` 各为 `{"h5":{"remaining":<num>,"reset_at":<int|null>}, "d7":{...}}` 或 `null`。
    **不含 password / access_token**。
  - `quota is None` 时返回 `{"accounts": []}`。
- `/api/state` **不**内嵌余量（保持轻量），前端单独拉 `/api/quota`。
- 新接口 `POST /api/keys/import`（批量导入，见 §16）：鉴权同 `/api/*`；body `{lines, account?, max_concurrency?}`；返回逐行结果。

## 8. 前端呈现（`dashboard.py`）

不新增独立面板；余量信息**并入现有 KPI 卡片与 Key 池表格**。前端从 `GET /api/quota` 拿到按账号的数据，
按账号名与 `state.keys[].account` 关联。

### 8.1 顶部 KPI 卡片（在现有 6 张后追加 6 张）

仅统计 `status="ok"` 的账号；聚合口径：

| 卡片 | 值 |
|---|---|
| 通用积分 5h 累计余量 | Σ `general.h5.remaining` |
| 通用积分 7d 累计余量 | Σ `general.d7.remaining` |
| Flash-Lite 专属积分 5h 累计余量 | Σ `flash_lite.h5.remaining` |
| Flash-Lite 专属积分 7d 累计余量 | Σ `flash_lite.d7.remaining` |
| 5h 重置倒计时 | 所有账号 `h5.reset_at` 的**最小值** -> 倒计时 |
| 7d 重置倒计时 | 所有账号 `d7.reset_at` 的**最小值** -> 倒计时 |

- 倒计时格式：5h -> `xx h xx m`；7d -> `xx d xx h xx m`（过期/缺失显示 `—`）。
- 数值用千分位；无任何已配置账号时这些卡片显示 `—`。

### 8.2 Key 池表格（新增 6 列）

在现有「账号 | Key | 状态 | 冷却 | RPM | 成功/失败 | 429 | 延迟 | 操作」后追加：

`通用 5h 余量 | 5h 重置倒计时 | 通用 7d 余量 | 7d 重置倒计时 | FL 专属 5h 余量 | FL 专属 7d 余量`

- 每行按该 Key 所属账号取余量（**按 Key 维度展示**，同一账号的多把 Key 显示相同值）。
- 未配置凭据的账号：这 6 列显示 `—`（不隐藏整行）。
- 账号 `status="error"`：对应列显示 `!` + tooltip（错误文案）。
- 列标题用缩写，`title` 属性给全称（`通用积分 5h 余量` 等）。
- 「余量」数值按四舍五入取整 + 千分位展示（如 `40,979`）。
- Key 池面板标题旁新增「**批量新增**」按钮 -> 打开导入弹窗（见 §16）；不替换现有单行「添加」入口。

### 8.3 刷新与倒计时

- 顶部「刷新余量」按钮 -> `GET /api/quota?refresh=1`；页面加载时拉一次（走缓存）。
- 倒计时用一个 1s 的前端定时器就地更新（元素带 `data-reset-at` 属性，避免整页重渲染）。
- 所有数值经 `esc()` 转义；无 CDN、沿用现有暗色样式。

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
  - 归一化：字符串数值、`reset_at`、缺失字段、`grant_balance`。
  - 池映射（§6.5）：`default` -> 通用；`model_ids`/`name` 含 flash-lite -> 专属；单一非 default 池的兜底。
  - `QuotaService`：未配置账号 `unconfigured` 且不发请求；TTL 内命中缓存不重复请求；`force` 触发刷新；
    JWT 临近过期自动重登；401 -> 重登一次；登录异常 -> `error` 且不影响其它账号。
  - 缺 `jwcrypto`（monkeypatch 导入失败）-> `error` 文案。
- `tests/test_config_account_credentials.py`：三个字段解析、`${ENV}` 展开、`password` 不出现在 `to_dict`。
- 控制台接口：`/api/quota` 未鉴权 401；已鉴权返回结构正确（含 `general`/`flash_lite`）；响应体不含 `password`/`access_token`。
- 前端：`DASHBOARD_HTML` 含新增 KPI 卡片与 Key 池列的表头文案（存在性断言）。
- 可选 live smoke：仅当提供真实凭据时运行（默认跳过）。
- `tests/test_keys_import.py`：两种格式解析（`--` 4 段 / 单 key / 段数非法）、逐行校验（凭据登录 + key 探测，注入 Fake）、去重、凭据写入账号、异常行原因、密码不回显。

## 12. Docker / 文档

- `Dockerfile`：在现有基础上增加 `RUN pip install --no-cache-dir -r requirements-quota.txt`
  （使余量功能在镜像内可用；基础源码仍懒加载）。同步更新「零 pip」表述为「核心零依赖，余量功能需 jwcrypto」。
- `.dockerignore`：无需额外改动（`requirements-quota.txt` 需被 COPY）。
- `README.md`：新增「账号余量」小节（配置字段、可选依赖安装、安全说明、Docker 说明）；
  `config.example.json` 增加三个空字段。
- README：补充「批量新增 Key」的两种格式与校验说明。

## 13. 数据流

```
浏览器 ──GET /api/quota──► proxy(鉴权) ──► ConsoleState.quota.snapshot()
                                               │  缓存命中? 直接返回
                                               │  否则 QuotaService:
                                               │    ensure JWT(exp 临近则 login)
                                               │    GET pool-usage (Bearer JWT)
                                               └──► 归一化 + 池映射 -> AccountQuota[]{general, flash_lite}
浏览器 ◄── {accounts:[...]}（无 password/JWT）──► 前端: KPI 卡片聚合 + Key 池表格列 + 倒计时
```

## 14. 验收标准（场景契约）

- **S1 未配置**：账号无 `user`/`password` -> `/api/quota` 中该账号 `status="unconfigured"`；Key 池该行 6 列显示 `—`；不发起任何凭据网络请求。
- **S2 正常**：配置凭据 + Fake transport 返回含 default 与 flash-lite 两池的样例 -> `/api/quota` 返回 `general` 与 `flash_lite`；KPI 卡片显示累计余量与倒计时；Key 池表格对应列显示余量。
- **S3 登录失败**：transport 抛错 -> 该账号 `status="error"`；Key 池对应列显示错误标记；网关 `/v1/*`、`/healthz` 正常。
- **S4 缺依赖**：模拟 `jwcrypto` 缺失 -> `status="error"`「未安装 jwcrypto」；其余功能正常。
- **S5 缓存**：TTL 内连续两次 `snapshot` 只发一次网络请求；`force=True` 再发一次。
- **S6 安全**：`/api/quota` 未鉴权 401；已鉴权响应体不含 `password`/`access_token`；日志无明文密码。
- **S7 回归**：不带凭据的配置解析与既有全部测试通过；核心代码零硬依赖。
- **S8 呈现**：无任何已配置账号时，新增 KPI 卡片显示 `—`、Key 池 6 列显示 `—`；页面不报错。
- **S9 批量-格式1**：粘贴纯 key 列表 -> 逐行探测；有效写入，无效行明确原因；响应/日志不含明文 key。
- **S10 批量-格式2**：粘贴 `手机--用户名--密码--apikey` -> 先用用户名密码登录校验，再探测 key；成功行按用户名归入账号并写入 `user/phone/password`；登录失败/key 无效/段数非法的行分别给出明确提示。

## 15. 风险与未决

- 上游登录/用量接口无公开文档，是**逆向**得到的；上游若变更（端点、字段、加密算法）会导致余量失败——
  设计上将其隔离在 `quota.py` 且失败只降级，不影响网关核心。
- 手写 JWE 依赖 `jwcrypto` 的正确性；因此不自行实现 AES-GCM。
- 账号级限流风控：频繁登录可能触发上游风控，故默认缓存 + 手动刷新，避免轮询。
- **池映射（已确认）**：通用池 = `pool_type=="default"`（「通用积分池」）；Flash-Lite 专属池 = `pool_type=="dedicated"` 且名/`model_ids` 含 `flash-lite`（「Flash-Lite积分池」）。
- **展示粒度（已确认）**：Key 池表格按每把 Key 一行展示其账号余量（不合并）。


## 16. 批量新增 Key（弹窗）

### 16.1 入口与弹窗
- Key 池面板标题旁新增「批量新增」按钮（保留现有单行「添加」入口）。
- 点击打开模态框（原生 `<dialog>`，无 CDN、沿用暗色样式）：一个 `textarea`（每行一条）、格式说明、可选的「归属账号」（仅格式 1 用）、「导入」按钮与结果区。
- 弹窗内展示逐行结果表：`行号 | 输入(脱敏) | 判定 | 原因`。

### 16.2 每行解析（逐行独立判定）
- 去首尾空白；空行跳过。
- 按分隔符 `--` 分割：
  - **1 段** -> 格式 1：整行是一个 apikey。
  - **4 段** -> 格式 2：`手机 | 用户名 | 密码 | apikey`（各段 `strip`）。
  - **其它段数** -> 该行标 `格式错误`（提示应为 1 段或 4 段）。
- 分隔符固定为 `--`；apikey 不含 `--`，故不歧义。

### 16.3 校验（导入前）
- 格式 1：对每把 key 调 `probe_key`（现有语义：401/403 -> 无效；429 -> 有效；unknown -> 警告放行）。
- 格式 2：
  1. 先用 `用户名 + 密码` 调 `quota.login(...)` 做**真实登录校验**（复用 §6.2）；登录失败 -> 该行 `凭据无效：<脱敏原因>`（**绝不回显密码**）。
  2. 再对该行 apikey 调 `probe_key` 校验 key；无效 -> 该行 `Key 无效：<原因>`。
  3. 两者都通过才导入。
- 未安装 `jwcrypto` 时格式 2 无法登录 -> 该行 `未安装 jwcrypto，无法校验凭据`。
- 去重：同一批内重复的 apikey、或已在池中的 apikey -> 该行 `重复，已跳过`。

### 16.4 写入
- 格式 1：归入弹窗指定的「归属账号」（留空则自动命名），沿用 `add_keys` 的账号语义。
- 格式 2：按 `用户名` 分组，每组一个账号：
  - 账号名 = 用户名（用户名为空则用手机号）。
  - 若已存在同 `user` 的账号则复用；否则新建。
  - 写入该账号的 `user`/`phone`/`password`（经 `ConfigStore` 落盘）及该组全部 apikey。
- 逐行写入失败（如 key 已存在）只影响该行，其它行照常。

### 16.5 结果与提示
- 返回 `{added:[...], results:[{line,input_masked,format,status,reason}], summary:{ok,error,skipped}}`。
- 前端在弹窗内以表格渲染；**异常行逐条给出明确原因**；汇总计数 toast。
- 单次最多 `MAX_IMPORT_LINES = 50` 行，超出直接报错。

### 16.6 安全
- 密码与明文 key **不写入日志、不回显**；结果里 key 一律脱敏；格式 2 的凭据仅写入 `config.json`。
- 导入触发的登录/探测为真实网络请求，弹窗内提示「校验中…」；失败只影响对应行。

### 16.7 依赖与测试
- 依赖 §6 的 `quota.login`（即 `jwcrypto`）仅用于格式 2 的凭据校验；格式 1 不需要。
- 新增 `tests/test_keys_import.py`（见 §11）：解析、逐行校验（注入 Fake transport/probe）、分组写入、异常提示、脱敏。