"""平台侧 Key 管理 API 的零依赖客户端（HTTP 传输层）。

「定时检测 key 失效→自动轮换」的前半部分：本模块只负责通过平台 OpenAPI
拉取 / 创建 / 删除 API Key（Token Plan 计量 API），不含轮换调度与登录逻辑。

端点契约来自对 platform.sensenova.cn 控制台的逆向（勿改）::

    LIST   GET    {base}/console/v1/metered/api-keys
    CREATE POST   {base}/console/v1/metered/api-keys
    DELETE DELETE {base}/console/v1/metered/api-keys/{id}

请求头与 quota.fetch_pools 一致（Authorization Bearer + 浏览器指纹头）。
只用标准库 urllib.request，风格对齐 quota.HttpQuotaTransport。

失败语义：``KeyApiError``，``status=0`` 表示网络层错误（连接拒绝 / 超时 /
非 JSON 等），其余为 HTTP 状态码；``detail`` 已脱敏（不含 access_token /
api_key 明文），最长 200 字符。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .config import AccountConfig
from .quota import USER_AGENT, QuotaAuthError, QuotaUnavailable, TokenBundle

# ---------------------------------------------------------------- 常量
KEYS_PATH = "/console/v1/metered/api-keys"
MAX_PAGES = 100  # 分页循环上限，防止服务端死循环给 next_page_token
DETAIL_LIMIT = 200  # 错误摘要最长字符数（脱敏上限）


class KeyApiError(RuntimeError):
    """平台 key 管理 API 失败。status=0 表示网络层错误。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class KeyInfo:
    id: str
    displayname: str
    api_key: str
    key_type: str
    create_time: str


class KeyTransport(Protocol):
    """Key 管理 API 的传输抽象（供上层 worker / 测试 Fake 实现）。"""

    def list_keys(
        self,
        access_token: str,
        *,
        key_type: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[KeyInfo]: ...
    def create_key(
        self, access_token: str, *, displayname: str, key_type: str
    ) -> KeyInfo: ...
    def delete_key(self, access_token: str, *, key_id: str) -> None: ...


def _parse_key(raw: Any) -> KeyInfo | None:
    """把单条 key 对象解析为 KeyInfo；结构不合法返回 None（调用方容错跳过）。"""
    if not isinstance(raw, Mapping):
        return None
    return KeyInfo(
        id=str(raw.get("id", "")),
        displayname=str(raw.get("displayname", "")),
        api_key=str(raw.get("api_key", "")),
        key_type=str(raw.get("key_type", "")),
        create_time=str(raw.get("create_time", "")),
    )


class HttpKeyManager:
    """默认实现：stdlib urllib.request，与 quota.HttpQuotaTransport 同风格。"""

    BASE = "https://platform.sensenova.cn/lite"

    def __init__(self, *, base: str = BASE, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    # --- 低层 HTTP ---
    def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发一次请求并解析 JSON；响应为空 / 非 JSON 容忍为 {}。

        HTTP 状态非 2xx → KeyApiError(状态码, 脱敏文本)；
        网络层失败 / 超时 → KeyApiError(0, 摘要)。
        """
        url = self.base + path
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Authorization": f"Bearer {access_token}",
            "Referer": "https://platform.sensenova.cn/console",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise KeyApiError(exc.code, self._http_error_detail(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            # socket.timeout / TimeoutError 均为 OSError 子类，一并归为网络层
            raise KeyApiError(0, self._network_error_detail(exc)) from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        """读取错误响应体，脱敏为 ≤200 字符的摘要（只留状态码 + 服务端文本）。"""
        text = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读不到响应体也不阻塞错误上报
            text = ""
        finally:
            exc.close()
        text = " ".join(text.split())
        return f"HTTP {exc.code}: {text}"[:DETAIL_LIMIT]

    @staticmethod
    def _network_error_detail(exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]

    # --- 业务方法 ---
    def list_keys(
        self,
        access_token: str,
        *,
        key_type: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[KeyInfo]:
        """分页拉取全部 key：追 next_page_token（上限 MAX_PAGES 页），按 id 去重。"""
        params: dict[str, Any] = {"page_size": str(int(page_size))}
        if key_type is not None:
            params["key_type"] = key_type
        if page_token is not None:
            params["page_token"] = page_token
        seen: dict[str, KeyInfo] = {}
        token = page_token
        for _ in range(MAX_PAGES):
            if token is not None:
                params["page_token"] = token
            data = self._request("GET", KEYS_PATH, access_token, params=params)
            for raw in data.get("api_keys") or []:
                key = _parse_key(raw)
                if key is not None and key.id:
                    seen.setdefault(key.id, key)
            token = data.get("next_page_token")
            if not token:
                break
        return list(seen.values())

    def create_key(
        self, access_token: str, *, displayname: str, key_type: str
    ) -> KeyInfo:
        """创建一个新 key，返回含完整明文 api_key 的 KeyInfo。

        响应形如 ``{"api_key": {…脱敏对象…}, "api_key_plain": "sk-…"}``：明文在
        ``api_key_plain``（仅创建时返回一次），``api_key`` 里是与 list 同形的脱敏对象。
        """
        data = self._request(
            "POST",
            KEYS_PATH,
            access_token,
            body={"displayname": displayname, "key_type": key_type},
        )
        raw = data.get("api_key")
        key = _parse_key(raw if isinstance(raw, Mapping) else data)
        if key is None:
            raise KeyApiError(0, "create_key 响应缺少 key 字段")
        plain = data.get("api_key_plain")
        if isinstance(plain, str) and plain.strip():
            return KeyInfo(
                id=key.id,
                displayname=key.displayname,
                api_key=plain.strip(),
                key_type=key.key_type,
                create_time=key.create_time,
            )
        return key

    def delete_key(self, access_token: str, *, key_id: str) -> None:
        """按 id 删除 key；服务端空响应 / {} 均视为成功。"""
        self._request(
            "DELETE", f"{KEYS_PATH}/{urllib.parse.quote(key_id, safe='')}", access_token
        )


class AutoRenewWorker:
    """定时检测 Key 失效并自动轮换。

    职责：定时探测 key → 失效则登录 → 平台注销全部 key → 新建 key → persist 回调。
    本类只做「决策 + 平台调用」；所有「落池 / 落盘」都交给注入的 ``persist``，
    以便在测试里完全用 Fake 替换平台，也方便接入方自定义落盘方式。

    状态语义（``account_status()`` 里的 ``status``）：

    * ``ok``             —— 全部 key 有效（或轮换成功）。
    * ``no_credentials`` —— 账号未配置 user/password（或没有 key），跳过。
    * ``password_error`` —— 登录失败且判定为凭据 / 密码问题。
    * ``check_error``    —— 登录或平台操作瞬时失败（网络 / 风控），下次再试。
    * ``unavailable``    —— 缺 jwcrypto 等依赖，无法登录。

    平台操作顺序固定为「先 delete-all 后 create」：先把该账号平台侧的全部 key 注销，
    再新建唯一一把 key。若反过来（先 create 后 delete-all），list 会把刚建的 key 也算进去
    并把它一并删掉，导致账号零 key、新 key 立即失效、无限轮换（历史 bug）。
    轮换成功后 persist 新 key；delete 中途失败则不 persist，下一轮会重新收敛。
    """

    STATUS_OK = "ok"  # 全部 key 有效（或轮换成功）
    STATUS_NO_CRED = "no_credentials"  # 账号未配置 user/password（或没有 key）
    STATUS_PASSWORD_ERROR = "password_error"  # 登录失败且判定为凭据/密码问题
    STATUS_CHECK_ERROR = "check_error"  # 登录或平台操作瞬时失败（网络/风控），下次再试
    STATUS_UNAVAILABLE = "unavailable"  # 缺 jwcrypto 等依赖，无法登录

    def __init__(
        self,
        *,
        accounts: Callable[[], Sequence[AccountConfig]],
        probe: Callable[[str], tuple[str, str]],
        login: Callable[[str, str], TokenBundle],
        keys: KeyTransport,
        persist: Callable[[str, Sequence[str], str], None],
        key_name: str = "auto",
        key_type: str = "API_KEY_TYPE_TOKEN_PLAN",
        interval: float = 180.0,
        cleanup_interval: float = 1800.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._accounts = accounts
        self._probe = probe
        self._login = login
        self._keys = keys
        self._persist = persist
        self._key_name = key_name
        self._key_type = key_type
        self._interval = float(interval)
        self._cleanup_interval = float(cleanup_interval)
        self._log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, dict[str, str]] = {}
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------ 线程生命周期
    def start(self) -> None:
        """以后台 daemon 线程启动自动轮换循环。"""
        self._thread = threading.Thread(
            target=self._loop, name="auto-renew", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止并 join 线程（最多等 5 秒）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def account_status(self) -> dict[str, dict[str, str]]:
        """返回 {账号名: {"status":..., "message":...}} 的深拷贝（线程安全）。"""
        with self._status_lock:
            return {name: dict(entry) for name, entry in self._status.items()}

    # ------------------------------------------------------------ 核心
    def run_once(self) -> None:
        """跑一轮检测：逐账号探测→轮换。单账号异常用 try/except 隔离，互不影响。"""
        for account in list(self._accounts()):  # 先快照，防迭代中变更
            try:
                self._check_account(account)
            except Exception as exc:  # noqa: BLE001 - 单账号异常不影响其它账号
                self._set_status(
                    account.name,
                    self.STATUS_CHECK_ERROR,
                    f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
                )

    def run_cleanup(self) -> None:
        """跑一轮「多余 Key」清理：删除账号在平台侧、但不在当前配置 api_keys 里的 key。"""
        for account in list(self._accounts()):
            try:
                self._cleanup_account(account)
            except Exception as exc:  # noqa: BLE001 - 单账号异常不影响其它账号
                self._log(
                    f"账号 {account.name}：清理多余 Key 失败：{type(exc).__name__}: {exc}"[
                        :DETAIL_LIMIT
                    ]
                )

    def _cleanup_account(self, account: AccountConfig) -> None:
        """删除平台侧多余 key（保留当前配置的）；无凭据/无 key 直接跳过，零网络。"""
        if not account.user or not account.password or not account.api_keys:
            return
        configured = set(account.api_keys)
        try:
            bundle = self._login(account.user, account.password)
        except QuotaAuthError as exc:
            self._set_status(
                account.name,
                self.STATUS_PASSWORD_ERROR,
                str(exc),
                log_line=f"账号 {account.name}：密码错误",
            )
            return
        except QuotaUnavailable:
            return
        except Exception as exc:  # noqa: BLE001 - 清理是后台兜底，瞬时失败仅记日志
            self._log(
                f"账号 {account.name}：清理多余 Key 登录失败：{type(exc).__name__}: {exc}"[
                    :DETAIL_LIMIT
                ]
            )
            return
        try:
            extras = [
                item
                for item in self._keys.list_keys(bundle.access_token)
                if item.api_key not in configured
            ]
            for item in extras:
                self._keys.delete_key(bundle.access_token, key_id=item.id)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"账号 {account.name}：清理多余 Key 失败：{type(exc).__name__}: {exc}"[
                    :DETAIL_LIMIT
                ]
            )
            return
        if extras:
            self._log(f"账号 {account.name}：已清理 {len(extras)} 把多余 Key")

    def _loop(self) -> None:
        """先立即跑一轮检测与清理，再按 interval 周期检测、按 cleanup_interval 周期清理。"""
        self.run_once()
        self.run_cleanup()
        next_cleanup = time.monotonic() + self._cleanup_interval
        while not self._stop.wait(self._interval):
            self.run_once()
            if time.monotonic() >= next_cleanup:
                self.run_cleanup()
                next_cleanup = time.monotonic() + self._cleanup_interval

    # ------------------------------------------------------------ 内部
    def _check_account(self, account: AccountConfig) -> None:
        """单个账号：无凭据/无 key 跳过；探测全部 key，遇首个 invalid 才轮换。"""
        if not account.user or not account.password:
            self._set_status(
                account.name,
                self.STATUS_NO_CRED,
                "",
                log_line=f"账号 {account.name}：未配置登录凭据，跳过",
            )
            return
        if not account.api_keys:
            self._set_status(
                account.name,
                self.STATUS_NO_CRED,
                "账号无 Key",
                log_line=f"账号 {account.name}：账号无 Key，跳过",
            )
            return
        needs_rotate = False
        for key in account.api_keys:
            verdict, _detail = self._probe(key)
            if verdict == "invalid":  # 遇到首个 invalid 即停，节省探测
                needs_rotate = True
                break
        if not needs_rotate:
            # 全部有效（或暂不可判）→ 恢复 ok，顺带清除此前的 password_error
            self._set_status(
                account.name,
                self.STATUS_OK,
                "",
                log_line=f"账号 {account.name}：Key 全部有效",
            )
            return
        self._rotate(account)

    def _rotate(self, account: AccountConfig) -> None:
        """登录 → 先 delete-all 后 create → persist；异常分类到对应状态。"""
        try:
            bundle = self._login(account.user, account.password)
        except QuotaAuthError as exc:
            # 凭据/密码问题：不调用任何平台操作，状态记 password_error
            self._set_status(
                account.name,
                self.STATUS_PASSWORD_ERROR,
                str(exc),
                log_line=f"账号 {account.name}：密码错误",
            )
            return
        except QuotaUnavailable:
            # 缺依赖：静默跳过，不记日志
            self._set_status(
                account.name, self.STATUS_UNAVAILABLE, "缺 jwcrypto 等依赖，无法登录"
            )
            return
        except Exception as exc:  # noqa: BLE001 - 瞬时失败（网络/风控），下次再试
            self._set_status(
                account.name,
                self.STATUS_CHECK_ERROR,
                f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
            )
            return
        # 登录成功后日志「开始轮换」：登录失败（含 unavailable）时保持静默
        self._log(f"账号 {account.name}：key 失效，开始轮换")
        try:
            for item in self._keys.list_keys(bundle.access_token):
                self._keys.delete_key(bundle.access_token, key_id=item.id)
            created = self._keys.create_key(
                bundle.access_token,
                displayname=self._key_name,
                key_type=self._key_type,
            )
        except Exception as exc:  # noqa: BLE001 - 平台瞬时失败
            self._set_status(
                account.name,
                self.STATUS_CHECK_ERROR,
                f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
            )
            return
        self._persist(account.name, list(account.api_keys), created.api_key)
        self._set_status(
            account.name,
            self.STATUS_OK,
            "",
            log_line=f"账号 {account.name}：轮换成功，Key 已更新",
        )

    def _set_status(
        self, name: str, status: str, message: str, *, log_line: str | None = None
    ) -> None:
        """写入状态；仅在状态发生迁移时输出日志（log_line 非空且确实变化）。"""
        with self._status_lock:
            current = self._status.get(name)
            changed = (
                current is None
                or current.get("status") != status
                or current.get("message") != message
            )
            if changed:
                self._status[name] = {"status": status, "message": message}
        if changed and log_line is not None:
            self._log(log_line)
