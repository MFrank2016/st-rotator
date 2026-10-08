# 设计文档：定时检测 Key 失效 + 自动轮换（auto_renew）

- 日期：2026-10-08
- 状态：待评审
- 关联：`2026-10-05-account-quota-design.md`（复用其登录 transport 与账号凭据模型）

## 1. 背景与目标

st-rotator 把多把商汤日日新 API Key 池化。Key 会因上游侧原因失效（停用、欠费、被风控），
当前只能靠运行期 401/403 把它标 `invalid` 并通过控制台手动更换，或人工跑 `check` 命令体检。

目标：增加**定时检测 + 自愈轮换**能力——周期探测各账号 Key，发现失效时用账号密码登录，
在平台侧注销该账号**全部** Key、新建一把名为 `auto` 的 Key，把网关、配置、磁盘文件同步到
新 Key；若登录失败且判定为密码/凭据问题，在管理端（控制台）对该账号打「密码错误」标志。

> 本文档只覆盖「新增的 auto_renew 特性」；网关既有推理路径、限流、冷却一律不动。

## 2. 非目标（YAGNI）

- 不做平台侧 Key 之外的资源管理（模型、套餐等）。
- 不做告警/通知；控制台「密码错误」标志只展示运行期状态，不持久化。
- 不支持「多把新 Key」或自定义轮换数量——按需求固定为「注销全部 → 新建 1 把名为 auto 的 Key」。
- 不改动 `cmd serve`（无配置文件写回与控制台面，不装配本特性）。

## 3. 已批准的关键决策

1. **配置 opt-in**：新增 `auto_renew` 配置节，`enabled=false` 默认关闭；仅 `ui` / `tray`
   两种带控制台与落盘能力的模式装配（`cmd serve` 不装配）。
2. **登录复用**：直接复用 `quota.HttpQuotaTransport.login()`（OIDC + JWE 密码加密）得到的
   `access_token` 作为平台 Key 管理 API 的 Bearer；不新写登录。
3. **平台 Key 管理 API 为逆向契约**：`/lite/console/v1/metered/api-keys` 的
   list / create / delete（见 §6），隔离在 `autorenew.HttpKeyManager`，失败只降级。
4. **轮换只改选定账号**：一经探测到某账号任一把 Key `invalid`，只轮换该账号；
   单账号失败不影响其它账号（逐账号 try/except 隔离）。
5. **平台操作顺序 create 先于 delete-all**：避免中途失败时账号在平台侧变成零 Key。
6. **`persist` 回调只在平台操作全成功后调用**：所有落池/落盘（pool + ConfigStore + 内存 config
   同步）封装成一个注入的 `persist(account, old_keys, new_key)`，测试可用 spy 替换。
7. **状态词汇**（`AutoRenewWorker.account_status()`，仅运行期，含 message）：
   `ok` / `no_credentials` / `password_error` / `check_error` / `unavailable`。
8. **`password_error` 生命周期**：登录抛 `QuotaAuthError` 时置位；后续周期探测全 ok 或轮换成功时清除。

## 4. 配置变更（`config.py` → 新 `AutoRenewConfig`）

```jsonc
"auto_renew": {
  "enabled": false,                 // 是否启用定时检测（默认关闭）
  "interval_seconds": 3600,         // 两次检测间隔（秒，>0）
  "key_name": "auto",               // 轮换新建的 Key 名称（≤64，仅中文/字母/数字/连字符）
  "key_type": "API_KEY_TYPE_TOKEN_PLAN"   // 仅 TOKEN_PLAN / METERED
}
```

- 新 `AutoRenewConfig` dataclass（`enabled=False`、`interval_seconds=3600.0`、
  `key_name="auto"`、`key_type="API_KEY_TYPE_TOKEN_PLAN"`），`__post_init__` 校验、
  `from_dict`（未知字段报错）、`to_dict`；`Config` 增同名字段，`from_dict`/`to_dict` 同步接线。
- `config.example.json` 增加上述节。
- `ConfigStore` 新增 `replace_account_keys(name, keys)`：整体替换某账号 `api_keys`
  （凭据/占位符/其余字段原样保留）后 `reload()`；空列表或账号不存在抛 `ConfigError`。

## 5. 新模块 `st_rotator/autorenew.py`

### 5.1 平台 Key 管理 transport（HTTP 传输层）

```python
class KeyApiError(RuntimeError):    # status=0 表示网络层错误；detail 脱敏 ≤200 字符
class KeyInfo:                      # frozen dataclass: id/displayname/api_key/key_type/create_time
class KeyTransport(Protocol):
    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None) -> tuple[list[KeyInfo], str]: ...
    def create_key(self, access_token, *, displayname, key_type) -> KeyInfo: ...
    def delete_key(self, access_token, *, key_id) -> None: ...
class HttpKeyManager:               # 默认实现，stdlib urllib.request，base 可注入（便于测试）
    BASE = "https://platform.sensenova.cn/lite"
```

- 请求头与 `quota.fetch_pools` 一致：`Authorization: Bearer <access_token>`、
  `Accept: application/json, text/plain, */*`、`Accept-Language: zh-CN`、
  `Referer: https://platform.sensenova.cn/console`、Chrome UA。
- `list_keys` 分页（追 `next_page_token`，上限 100 页，按 id 去重）；`create_key` POST
  `{"displayname":..., "key_type":...}`；`delete_key` DELETE `/console/v1/metered/api-keys/{id}`。
- 空/非 JSON 响应体容忍为 `{}`；非 2xx → `KeyApiError(code, 文本≤200)`；
  网络层（URLError/OSError/超时）→ `KeyApiError(0, 摘要)`。

### 5.2 `AutoRenewWorker`（定时检测 + 轮换状态机）

```python
class AutoRenewWorker:
    def __init__(self, *, accounts, probe, login, keys, persist,
                 key_name="auto", key_type="API_KEY_TYPE_TOKEN_PLAN",
                 interval=3600.0, log=None): ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def run_once(self) -> None: ...
    def account_status(self) -> dict[str, dict[str, str]]: ...
```

- 注入：`accounts()`（取当前账号序列）、`probe(key)`（"ok"/"invalid"/"unknown" 三态）、
  `login(user,pass)`（→TokenBundle，可抛 `QuotaAuthError`/`QuotaUnavailable`/其它）、
  `keys: KeyTransport`、`persist(account, old_keys, new_key)`、`log` sink。
- `run_once()` 逐账号：
  1. 无 user/password 或 api_keys 为空 → `no_credentials`，零网络。
  2. 逐个 `probe`；首个 `invalid` 即停；无一 invalid → `ok`（清除既有 password_error）。
  3. 轮换：`login` → 异常分类（`QuotaAuthError`→`password_error`；`QuotaUnavailable`→`unavailable`；
     其它→`check_error`，均不做平台调用）→ `create_key` → `list_keys` 逐个 `delete_key`
     → 全成功才 `persist`；平台任一步失败→`check_error`（不 persist）。
  4. 成功 → `ok`。
- daemon 线程（`name="auto-renew"`），先 `run_once()` 一次再 `while not stop.wait(interval)`；
  `stop()` 置 Event + `join(timeout=5)`。
- `account_status()` 线程安全深拷贝；log 只记状态迁移，绝不出现明文 key / access_token。

## 6. 平台端点契约（逆向，勿改）

| 操作 | 方法 & 路径                                                                 | 说明                                                                                                       |
| ---- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 列表 | `GET {base}/console/v1/metered/api-keys?key_type=&page_size=&page_token=`   | 返回 `{api_keys:[{id,displayname,api_key,key_type,create_time}], next_page_token}`；**api_key 为完整明文** |
| 创建 | `POST {base}/console/v1/metered/api-keys` body `{"displayname","key_type"}` | 返回含 `api_key`（完整明文）的 key 对象                                                                    |
| 删除 | `DELETE {base}/console/v1/metered/api-keys/{id}`                            | 空响应视为成功                                                                                             |

- `base = https://platform.sensenova.cn/lite`；鉴权 `Authorization: Bearer <access_token>`。
- `key_type ∈ {API_KEY_TYPE_TOKEN_PLAN, API_KEY_TYPE_METERED}`。
- 跨账号含义：**注销「全部」= 平台侧该账号名下的全部 key 都删除**（管理侧视为权威清单，
  本地 config 之外的平台 key 也会被注销；文档明示此语义）。

## 7. 控制台（`ui.py` / `dashboard.py`）

- `ConsoleState` 增可选字段 `auto_renew: AutoRenewWorker | None = None`；
  `snapshot()` 的 `/api/state` 增 `account_status`（worker 不存在时为 `{}`）。
- `cli.py` 新增 `build_auto_renew(config, store, rotator, *, lock, sink)`：`enabled=False`
  或无凭据账号 → 返回 None；否则构造真实 `HttpQuotaTransport`/`HttpKeyManager` 与生产
  `persist`（pool.add_key→pool.remove_key→store.replace_account_keys→reload→
  rotator.config.accounts 同步→store.save()，全程持 lock），`start()` 后注入 ConsoleState；
  在 `cmd_ui` / `cmd_tray` 装配，并在 finally `stop()`。
- `dashboard.py` Key 池表格账号列追加红色「密码错误」徽标（复用 `.pill.invalid`），
  当该账号 `account_status.status == "password_error"` 时显示，`title` 为 message。

## 8. 安全

- `password`、`access_token`、`KeyInfo.api_key` **绝不**进入日志、`account_status`、文档；
  平台错误 detail 脱敏 ≤200 字符。
- 密码/明文 key 不写入任何测试文件（测试全 Fake / 本地 server）。
- 探测会产生真实网络请求（每 interval 每账号 N 把 key 各一次 `/models`），
  默认关闭 + 3600s 间隔降低暴露面；`unknown`（如 429）**绝不**触发轮换。

## 9. 错误处理与收敛

| 情况                        | 表现                                                        |
| --------------------------- | ----------------------------------------------------------- |
| 登录失败（密码错）          | `password_error`；不平台操作；下周期若全 ok 自动清除        |
| 登录失败（网络/风控）       | `check_error`；下周期重试；不标记密码错                     |
| 缺 jwcrypto                 | `unavailable`；静默跳过该账号                               |
| 平台 create/delete 中途失败 | `check_error`；不 persist；可能残留一把平台 key，下周期收敛 |
| 单账号失败                  | 仅该账号置状态；其它账号照常                                |
| 探测 unknown                | 不轮换（限流≠失效）                                         |

## 10. 测试策略（stdlib `unittest`，不访问真实商汤）

- `tests/test_config_auto_renew.py`：配置解析/校验/往返；未知字段报错。
- `tests/test_config_account_credentials.py`（扩展 `ReplaceAccountKeysTest`）：落盘重读、
  占位符保留、凭据保留、空列表/缺账号报错。
- `tests/test_autorenew_transport.py`：本地 `ThreadingHTTPServer` 罐头化验证
  分页/参数/请求体/方法路径/鉴权头/非 2xx/网络错误/空响应体。
- `tests/test_autorenew_worker.py`：Fake 注入的状态机矩阵（健康/失效轮换/密码错/瞬时错/
  依赖缺失/unknown/未配置/单账号隔离/线程生命周期/password_error 清除）。
- `tests/test_console_auto_renew.py`：`ConsoleState.snapshot()` 含 `account_status`；
  `build_auto_renew` 门控；生产 `persist` 端到端一致性（文件==store==rotator.config==pool，
  凭据保留）。
- `tests/test_dashboard_auto_renew.py`：`DASHBOARD_HTML` 字符串断言（「密码错误」、
  `state.account_status`、`status === "password_error"`）。

## 11. 验收标准（场景契约）

- **S1 配置**：auto_renew 解析/校验/往返；未知字段报错；example 含节。
- **S2 传输**：HttpKeyManager list(分页/去重/参数)/create(请求体)/delete(路径+Bearer 头)；
  非 2xx→KeyApiError(code)；网络→KeyApiError(0)；空响应体容忍。
- **S3 轮换成功**：invalid→login→create→delete-all→persist；文件/池/配置一致；凭据保留。
- **S4 密码错**：QuotaAuthError→password_error；零平台调用。
- **S5 瞬时错**：其它异常→check_error；不标密码错。
- **S6 健康**：全 ok→无轮换；清 password_error。
- **S7 未配置**：无凭据/无 key 账号跳过，零网络。
- **S8 unknown**：不轮换。
- **S9 隔离**：账号 A 失败不影响账号 B 轮换。
- **S10 UI 面**：`/api/state.account_status`；dashboard「密码错误」徽标。
- **S11 安全**：account_status / 日志 / 文档无明文 key/token。
- **S12 回归**：既有测试全绿（148 项基线）。

## 12. 风险与未决

- 平台 Key 管理 API 为逆向结果，上游变更会导致轮换失败——隔离在 `autorenew.py`，
  失败仅降级（`check_error`），不影响网关推理。
- 频繁登录/探测可能触发上游风控：默认 opt-in + 3600s 间隔；探测遇首个 invalid 即停。
- 「注销全部」会删除平台侧该账号全部 key（含本地未配置的）：文档明示为预期语义。
