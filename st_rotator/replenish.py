"""自动补充账号：可用账号数低于目标时，用易码短信自动注册 / 接管新账号。

决策与平台调用集中在 ``ReplenishWorker``；所有外部依赖（sms / authn / keys /
persist / registry）都通过注入的协议与回调提供，因此可无网络地整体拉通测试
（与 autorenew.AutoRenewWorker 同风格）。

一轮 ``run_once`` 的流程：
1. ``count_available``（只有密码错误才算不可用）对比目标；够 → idle。
2. 不够 → 循环补齐：
   a. spend gate：余额差口径算当日消费，超上限 → blocked_cap 即停。
   b. 取号 + 去重（已用号码跳过，上限 20 次）→ claim。
   c. 发短信验证码；返回 None → 需要滑块（captcha_required，不花钱），
      除非注入了 captcha_solver（先 getCaptcha+checkCaptcha 再带 code_key 重发）。
   d. 等短信；超时 → 换新号，重跑 spend gate。
   e. mint login challenge（challenge_expired 时重 mint 一次）。
   f. 短信校验（smsLogin）：服务端要求 register 前 token_code 先经一次验证码校验，
      否则 register 报 incorrectSmsCode。tenant_list 非空 → 号码已注册 → S2 接管；
      空 → 未注册 → S1 注册。
   g. S1 register → exchange → create key → persist；
      username_taken → 重造用户名重试一次，仍失败 → check_error。
      S2 接管：吊销全部 key → 建新 key → 二次短信 → 改密 → get_user_info → persist。
   h. 其它异常 → check_error，结束本轮。

纯函数 ``generate_credentials`` 生成的用户名 / 密码字符集都排除 ``$`` ``{``
``}``，避免被 ConfigStore 的 ``${ENV}`` 占位符展开误伤。

说明：本模块行数超出通用 250 行上限，按 single-responsibility 不可再拆 ——
接口由 .omo/plans/replenish.md T5 钉死为单模块（6 个纯函数 + 完整 worker 周期），
仓库同类模块亦为大文件（quota.py 634 行 / autorenew.py 404 行）。特此声明，非漏检。
"""

from __future__ import annotations

import secrets
import string
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .authn import (
    AuthnError,
    AuthnTransport,
    jwt_sub,
    sms_login_redirect,
    sms_login_tenants,
)
from .autorenew import KeyTransport
from .config import AccountConfig
from .registry import Registry
from .sms import SmsTransport, wait_sms_code

# 错误摘要最长字符数（脱敏上限，同 autorenew.DETAIL_LIMIT）
DETAIL_LIMIT = 200
# 取号去重循环上限：连续拿到已用号码超过此数即放弃本轮
DEDUPE_MAX = 20
# 软重试上限：短信持续超时（need 不递减）时，连续超过此数即放弃本轮，避免死循环
SOFT_RETRY_MAX = 10

# 用户名 / 密码字符集：**刻意排除 $ { }** —— 避免 ConfigStore 的 ${ENV}
# 占位符展开误伤生成的凭据。
_USER_CHARSET = string.ascii_letters + string.digits
_SPECIALS = "~!@#%^&*?_+.,;:-"
_PASSWORD_CLASSES = (
    string.ascii_lowercase,
    string.ascii_uppercase,
    string.digits,
    _SPECIALS,
)
_USER_MIN, _USER_MAX = 6, 24
_PW_MIN, _PW_MAX = 8, 32


class _StopCycle(Exception):
    """硬性终止本轮补充（blocked_cap / captcha / check_error / 去重耗尽）。"""


def count_unavailable(
    accounts: Sequence[AccountConfig], statuses: Mapping[str, Mapping[str, str]]
) -> int:
    """不可用账号数 = 密码错误（password_error）账号数。

    只有 ``auto_renew.account_status()`` 标为密码错误的账号才算「不可用」；
    其它状态（ok / check_error / unavailable / 无记录）都算可用。
    """
    unavailable = 0
    for account in accounts:
        entry = statuses.get(account.name)
        if entry is not None and entry.get("status") == "password_error":
            unavailable += 1
    return unavailable


def count_available(
    accounts: Sequence[AccountConfig], statuses: Mapping[str, Mapping[str, str]]
) -> int:
    """可用账号数 = 账号总数 - 密码错误（password_error）账号数。"""
    return len(accounts) - count_unavailable(accounts, statuses)


def _randbelow(rng: Any, n: int) -> int:
    """统一的 [0, n) 随机整数：兼容 secrets / random.Random / random 模块。"""
    if hasattr(rng, "randbelow"):
        return rng.randbelow(n)
    return rng.randint(0, n - 1)


def _sample_without_replacement(rng: Any, seq: Sequence[Any], k: int) -> list[Any]:
    """从 seq 里不重复地取 k 个（用小算法实现，避免依赖 rng.sample）。"""
    pool = list(seq)
    out: list[Any] = []
    for _ in range(k):
        out.append(pool.pop(_randbelow(rng, len(pool))))
    return out


def generate_credentials(
    rng: Any = None, *, user_length: int = 10, password_length: int = 16
) -> tuple[str, str]:
    """生成一对新账号凭据 ``(user, password)``。

    - user: ``[A-Za-z0-9]``，长度 6..24（默认 10）。
    - password: 必须同时含小写 / 大写 / 数字 / 特殊字符四类（商汤规则：
      「8–32 位，须含大写、小写、数字与特殊字符」），长度 8..32（默认 16）。
    - 两个字符集都排除 ``$`` ``{`` ``}``，保证 ${ENV} 展开永不误伤。

    ``rng`` 可注入（``secrets`` / ``random.Random(seed)``）；缺省用 ``secrets``
    （密码学安全），测试可注入 ``random.Random(seed)`` 固定种子。
    """
    if rng is None:
        rng = secrets
    ulen = max(_USER_MIN, min(_USER_MAX, int(user_length)))
    plen = max(_PW_MIN, min(_PW_MAX, int(password_length)))

    user = "".join(rng.choice(_USER_CHARSET) for _ in range(ulen))

    # 商汤密码规则要求四类齐全（见上方 docstring），故固定取全部类别，每类至少 1 字符
    picked = _sample_without_replacement(rng, _PASSWORD_CLASSES, len(_PASSWORD_CLASSES))
    pool = "".join(picked)
    chars = [rng.choice(cls) for cls in picked]
    chars += [rng.choice(pool) for _ in range(plen - len(chars))]
    # 就地洗牌，让「每类至少 1 字符」的约束不暴露在头部
    for i in range(len(chars) - 1, 0, -1):
        j = _randbelow(rng, i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return user, "".join(chars)


def today_str(clock: Callable[[], float] = time.time) -> str:
    """本地日期 ``YYYY-MM-DD``。"""
    return time.strftime("%Y-%m-%d", time.localtime(clock()))


def pick_tenant(tenants: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """多租户短信登录选一个租户：优先最近登录（``is_last_login``），否则第一个。"""
    for tenant in tenants:
        if tenant.get("is_last_login"):
            return tenant
    return tenants[0] if tenants else None


def spend_ok(cap: float, state: Mapping[str, Any], today: str) -> tuple[bool, float]:
    """当日花销闸门：返回 ``(allowed, consumed)``。

    - ``state["date"] != today`` → 换天重置：consumed=0，allowed=True。
    - ``start_balance`` 为 None（当天首次读，基线在别处建立）→ allowed=True，consumed=0。
    - 否则 consumed = max(0, start_balance - last_balance)，allowed = consumed < cap
      （cap <= 0 表示不限制）。
    """
    if state.get("date") != today:
        return True, 0.0
    start = state.get("start_balance")
    if start is None:
        return True, 0.0
    last = state.get("last_balance")
    try:
        consumed = max(0.0, float(start) - float(last if last is not None else start))
    except (TypeError, ValueError):
        consumed = 0.0
    try:
        cap_value = float(cap)
    except (TypeError, ValueError):
        cap_value = 0.0
    return (cap_value <= 0 or consumed < cap_value), consumed


class ReplenishWorker:
    """定时检测可用账号数，不足时自动注册 / 接管新账号。

    与 AutoRenewWorker 同构：``start`` 先立即跑一轮再按 interval 周期循环；
    任何异常都收敛为状态而不抛出，线程循环永不中断。所有状态写入
    ``account_status()["_replenish"]``。
    """

    STATUS_IDLE = "idle"  # 可用账号数已达标
    STATUS_OK = "ok"  # 本轮补充成功
    STATUS_BLOCKED_CAP = "blocked_cap"  # 今日短信消费已达上限
    STATUS_CAPTCHA = "captcha_required"  # 需要滑块验证码且未配置 solver
    STATUS_CHECK_ERROR = "check_error"  # 平台操作瞬时失败 / 参数问题，下次再试
    STATUS_UNAVAILABLE = "unavailable"  # 保留位（与 autorenew 状态集合对齐）

    def __init__(
        self,
        *,
        accounts: Callable[[], Sequence[AccountConfig]],
        status_source: Callable[[], Mapping[str, Mapping[str, str]]],
        target: int,
        sms: SmsTransport,
        authn: AuthnTransport,
        keys: KeyTransport,
        captcha_solver: Callable[[], str] | None = None,
        persist: Callable[..., None],
        registry: Registry,
        clock: Callable[[], float] = time.time,
        key_name: str = "auto",
        key_type: str = "API_KEY_TYPE_TOKEN_PLAN",
        keyword: str = "商汤",
        sms_poll_interval: float = 5.0,
        sms_poll_timeout: float = 120.0,
        daily_spend_cap: float = 5.0,
        interval: float = 3600.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._accounts = accounts
        self._status_source = status_source
        self._target = int(target)
        self._sms = sms
        self._authn = authn
        self._keys = keys
        self._captcha_solver = captcha_solver
        self._persist = persist
        self._registry = registry
        self._clock = clock
        self._key_name = key_name
        self._key_type = key_type
        self._keyword = keyword
        self._sms_poll_interval = float(sms_poll_interval)
        self._sms_poll_timeout = float(sms_poll_timeout)
        self._daily_spend_cap = float(daily_spend_cap)
        self._interval = float(interval)
        self._log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, dict[str, str]] = {}
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------ 线程生命周期
    def start(self) -> None:
        """以后台 daemon 线程启动补充循环。"""
        self._thread = threading.Thread(
            target=self._loop, name="replenish", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止并 join 线程（最多等 5 秒）。"""
        self._stop.set()
        self._wake.set()  # 立刻打断 interval 等待，缩短 join
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reconfigure(
        self,
        *,
        target: int | None = None,
        interval: float | None = None,
        keyword: str | None = None,
        sms_poll_interval: float | None = None,
        sms_poll_timeout: float | None = None,
        daily_spend_cap: float | None = None,
        key_name: str | None = None,
        key_type: str | None = None,
    ) -> None:
        """热更新运行参数，无需重启进程（控制台保存补号配置后调用）。

        只更新标量运行参数；``sms`` / ``authn`` / ``keys`` 传输层不在此热换
        （``SmsTransport`` 在构造时固定 token）——token 变更需由调用方重建 worker。
        更新后唤醒循环，使「更短的检查间隔」等参数立即生效，而不必等完旧间隔。
        """
        if target is not None:
            self._target = int(target)
        if interval is not None:
            self._interval = float(interval)
        if keyword is not None:
            self._keyword = str(keyword)
        if sms_poll_interval is not None:
            self._sms_poll_interval = float(sms_poll_interval)
        if sms_poll_timeout is not None:
            self._sms_poll_timeout = float(sms_poll_timeout)
        if daily_spend_cap is not None:
            self._daily_spend_cap = float(daily_spend_cap)
        if key_name is not None:
            self._key_name = str(key_name)
        if key_type is not None:
            self._key_type = str(key_type)
        self._wake.set()

    def account_status(self) -> dict[str, dict[str, str]]:
        """返回 ``{"_replenish": {"status", "message"}}`` 的深拷贝（线程安全）。"""
        with self._status_lock:
            return {name: dict(entry) for name, entry in self._status.items()}

    # ------------------------------------------------------------ 核心
    def run_once(self) -> None:
        """跑一轮补充；任何异常收敛为 check_error，绝不抛出（线程循环永续）。"""
        try:
            self._cycle()
        except Exception as exc:  # noqa: BLE001 - 兜底：循环线程永不中断
            self._set_status(
                self.STATUS_CHECK_ERROR,
                f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
                log_line=f"[补号] 本轮异常：{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
            )

    def _loop(self) -> None:
        """先立即跑一轮，再按 interval 周期循环。

        ``reconfigure()`` / ``stop()`` 置位 ``_wake`` 可提前打断等待：改短检查间隔
        或停止时都能立即响应，而不必等完当前 interval。
        """
        self.run_once()
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.run_once()

    def _cycle(self) -> None:
        """统计可用数 vs 目标；不足则逐个补齐，单次失败即停本轮。"""
        accounts = list(self._accounts())
        available = count_available(accounts, self._status_source())
        if available >= self._target:
            self._set_status(
                self.STATUS_IDLE,
                "",
                log_line=f"[补号] 可用账号 {available} >= 目标 {self._target}，无需补充",
            )
            return
        need = self._target - available
        self._log(f"[补号] 可用账号 {available} < 目标 {self._target}，需补充 {need} 个")
        soft_retries = 0
        while need > 0:
            try:
                if self._replenish_one():
                    need -= 1
                    soft_retries = 0
                    continue
                soft_retries += 1
                if soft_retries >= SOFT_RETRY_MAX:
                    self._set_status(
                        self.STATUS_CHECK_ERROR,
                        f"连续 {SOFT_RETRY_MAX} 次短信超时，本轮放弃",
                        log_line=f"[补号] 连续 {SOFT_RETRY_MAX} 次短信超时，本轮放弃",
                    )
                    return
            except _StopCycle:
                return
            except Exception as exc:  # noqa: BLE001 - 单次迭代失败不拖垮循环
                self._set_status(
                    self.STATUS_CHECK_ERROR,
                    f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
                    log_line=f"[补号] 本轮补充出错：{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
                )
                return

    # ------------------------------------------------------------ 单次补齐
    def _replenish_one(self) -> bool:
        """尝试补齐一个账号。

        返回 True = 成功建号（need 减一）；False = 软重试（短信超时，换新号）。
        硬性终止（blocked_cap / captcha / check_error / 去重耗尽）先写状态再抛
        ``_StopCycle`` 结束本轮。
        """
        # a. spend gate：余额差口径，超当日上限即停。先记入本次读数再判定，
        #    使闸门用的是「刚读到的余额」而不是上一次的读数（避免差一拍超冲）。
        today = today_str(self._clock)
        balance = self._sms.left_amount()
        self._registry.note_balance(today, balance)
        allowed, consumed = spend_ok(
            self._daily_spend_cap, self._registry.spend_state(), today
        )
        if not allowed:
            self._set_status(
                self.STATUS_BLOCKED_CAP,
                "今日短信消费已达上限",
                log_line=f"[补号] 今日短信消费已达上限（已用 ¥{consumed:.2f}），本轮停止",
            )
            raise _StopCycle()

        # b. 取号 + 去重（已用号码跳过，不 claim；连续 20 个已用即放弃）
        phone = self._fetch_fresh_phone()
        self._registry.claim_phone(phone)
        self._log(f"[补号] 已取号 {phone}")

        # c. 发短信验证码；None -> 需要滑块（不花钱），除非有 solver 带 code_key 重发
        token_code = self._authn.send_sms_code(phone)
        if token_code is None:
            if self._captcha_solver is None:
                self._set_status(
                    self.STATUS_CAPTCHA,
                    "需要滑块验证码（未配置 solver）",
                    log_line=f"[补号] 号码 {phone} 需要滑块验证码（未配置 solver），本轮停止",
                )
                raise _StopCycle()
            code_key = self._captcha_solver()
            token_code = self._authn.send_sms_code(phone, code_key=code_key)
            if token_code is None:
                self._set_status(
                    self.STATUS_CAPTCHA,
                    "滑块验证后仍未下发验证码",
                    log_line=f"[补号] 号码 {phone} 滑块验证后仍未下发验证码，本轮停止",
                )
                raise _StopCycle()
        self._log(f"[补号] 已向 {phone} 发送短信验证码，等待接收…")

        # d. 等短信；超时 -> 软重试（换新号，重跑 spend gate）
        code = wait_sms_code(
            self._sms,
            phone,
            keyword=self._keyword,
            interval=self._sms_poll_interval,
            timeout=self._sms_poll_timeout,
            clock=self._clock,
        )
        if code is None:
            self._log(
                f"[补号] 号码 {phone} 短信超时（{self._sms_poll_timeout:g}s），换号重试"
            )
            return False

        # e. mint login challenge（challenge_expired 重 mint 一次）
        challenge, verifier = self._mint_challenge()

        # f. 先短信校验（smsLogin）：服务端要求 register 前 token_code 必须先经一次
        #    验证码校验，否则 register 报 incorrectSmsCode。smsLogin 同时判定号码归属。
        try:
            login_data = self._authn.sms_login(
                token_code=token_code, verify_code=code, challenge=challenge
            )
        except AuthnError as exc:
            if exc.reason == "challenge_expired":
                challenge, verifier = self._mint_challenge()
                login_data = self._authn.sms_login(
                    token_code=token_code, verify_code=code, challenge=challenge
                )
            elif exc.reason == "incorrect_sms_code":
                return False
            else:
                raise

        # 手机号已被注册（tenant_list 非空）-> S2 接管
        if sms_login_tenants(login_data):
            self._log(f"[补号] 号码 {phone} 已注册，走接管流程")
            return self._takeover(login_data, challenge, verifier, phone)

        # g. S1 新注册；username_taken -> 重试一次
        self._log(f"[补号] 号码 {phone} 未注册，走注册流程")
        user, password = generate_credentials()
        try:
            redirect = self._authn.register(
                token_code=token_code,
                user_name=user,
                password=password,
                challenge=challenge,
            )
        except AuthnError as exc:
            if exc.reason == "username_taken":
                user, redirect = self._retry_register(token_code, password, challenge)
            else:
                raise

        bundle = self._authn.exchange_code(redirect, verifier)
        created = self._keys.create_key(
            bundle.access_token, displayname=self._key_name, key_type=self._key_type
        )
        self._persist(user, phone, password, created.api_key)
        self._set_status(
            self.STATUS_OK,
            "",
            log_line=f"[补号] 已注册新账号 {user}（{phone}）",
        )
        return True

    def _fetch_fresh_phone(self) -> str:
        """连续取号直到拿到一个未用过的号码；去重耗尽 -> check_error。"""
        for _ in range(DEDUPE_MAX):
            phone = self._sms.get_phone(keyword=self._keyword)
            if not self._registry.is_phone_used(phone):
                return phone
        self._set_status(
            self.STATUS_CHECK_ERROR,
            f"取号去重超过 {DEDUPE_MAX} 次",
            log_line=f"[补号] 取号去重超过 {DEDUPE_MAX} 次，本轮放弃",
        )
        raise _StopCycle()

    def _mint_challenge(self) -> tuple[str, str]:
        """拿 login challenge；challenge_expired 时重 mint 一次。"""
        try:
            return self._authn.mint_login_challenge(intent="register")
        except AuthnError as exc:
            if exc.reason == "challenge_expired":
                return self._authn.mint_login_challenge(intent="register")
            raise

    def _retry_register(
        self, token_code: str, password: str, challenge: str
    ) -> tuple[str, str]:
        """username_taken：重造用户名重试；仍失败 -> check_error。返回 (user, redirect)。"""
        new_user, _ = generate_credentials()
        try:
            redirect = self._authn.register(
                token_code=token_code,
                user_name=new_user,
                password=password,
                challenge=challenge,
            )
        except AuthnError as exc:
            self._set_status(
                self.STATUS_CHECK_ERROR,
                f"用户名重试仍失败: {exc.detail}"[:DETAIL_LIMIT],
                log_line=f"[补号] 用户名重试仍失败：{exc.detail}"[:DETAIL_LIMIT],
            )
            raise _StopCycle() from exc
        return new_user, redirect

    # ------------------------------------------------------------ S2 接管
    def _takeover(
        self,
        login_data: Mapping[str, Any],
        challenge: str,
        verifier: str,
        phone: str,
    ) -> bool:
        """手机号已被他人注册：短信登录 -> 吊销全部 key -> 建新 key -> 二次短信改密。

        ``login_data`` 为 smsLogin 原始响应（验证码已在 _replenish_one 校验通过），
        其中含可直接换取 code 的 redirect。
        """
        redirect = sms_login_redirect(login_data)
        if not redirect:
            tenant = pick_tenant(sms_login_tenants(login_data))
            if tenant is None:
                self._set_status(
                    self.STATUS_CHECK_ERROR,
                    "接管：smsLogin 无 redirect 且无租户",
                    log_line=f"[补号] 号码 {phone} 接管失败：smsLogin 无 redirect 且无租户",
                )
                raise _StopCycle()
            nxt = self._authn.login_next(
                challenge=challenge,
                username=str(tenant.get("username") or ""),
                user_id=str(tenant.get("user_id") or ""),
                sign=str(tenant.get("sign") or ""),
            )
            redirect = sms_login_redirect(nxt)
        if not redirect:
            self._set_status(
                self.STATUS_CHECK_ERROR,
                "接管：登录未返回 redirect",
                log_line=f"[补号] 号码 {phone} 接管失败：登录未返回 redirect",
            )
            raise _StopCycle()
        bundle = self._authn.exchange_code(redirect, verifier)
        user_id = jwt_sub(bundle.access_token)
        if not user_id:
            self._set_status(
                self.STATUS_CHECK_ERROR,
                "接管后无法解析 user_id",
                log_line=f"[补号] 号码 {phone} 接管失败：无法解析 user_id",
            )
            raise _StopCycle()
        # 先吊销全部旧 key（该手机号归我们所有，旧凭据一律作废）。
        for item in self._keys.list_keys(bundle.access_token):
            self._keys.delete_key(bundle.access_token, key_id=item.id)
        created = self._keys.create_key(
            bundle.access_token, displayname=self._key_name, key_type=self._key_type
        )
        self._log(f"[补号] 号码 {phone} 接管中：已吊销旧 Key 并新建，等待改密短信…")
        new_pw = generate_credentials()[1]
        tok2 = self._authn.request_change_password_code(bundle.access_token, user_id)
        code2 = wait_sms_code(
            self._sms,
            phone,
            keyword=self._keyword,
            interval=self._sms_poll_interval,
            timeout=self._sms_poll_timeout,
            clock=self._clock,
        )
        if code2 is None:
            # 二次短信超时：账号已在平台接管成功（新 key 已建），但改密未完成、真实密码未知。
            # 兜底：
            # 1) 用已有 access_token 取回用户名，避免 user 落空（否则余量 / 自动续期彻底用不了）；
            # 2) 密码落空串 —— new_pw 从未被服务端接受，写进去只会让余量面板误报「登录失败」，
            #    空密码则如实记为「未配置凭据」，等人工重发改密短信补录即可恢复。
            username = self._recover_username(bundle.access_token, user_id)
            self._persist(
                username, phone, "", created.api_key, outcome="takeover_password_unset"
            )
            self._set_status(
                self.STATUS_CHECK_ERROR,
                "改密短信超时，账号已接管但未改密",
                log_line=(
                    f"[补号] 号码 {phone} 改密短信超时：已接管并保留 Key，"
                    f"用户名={username or '未取到'}（密码未改，需人工补录）"
                ),
            )
            raise _StopCycle()
        self._authn.change_password(
            bundle.access_token,
            user_id,
            token_code=tok2,
            verify_code=code2,
            password=new_pw,
        )
        username = self._recover_username(bundle.access_token, user_id)
        self._persist(username, phone, new_pw, created.api_key)
        self._set_status(
            self.STATUS_OK,
            "",
            log_line=f"[补号] 已接管账号 {username or phone}（{phone}）并改密",
        )
        return True

    def _recover_username(self, access_token: str, user_id: str) -> str:
        """尽力取回用户名；失败返回空串（不抛错，仅用于落账）。"""
        try:
            profile = self._authn.get_user_info(access_token, user_id)
        except Exception:  # noqa: BLE001 - 用户名可缺省，不影响落账
            return ""
        return str(profile.get("user_name") or profile.get("username") or "")

    # ------------------------------------------------------------ 状态
    def _set_status(
        self, status: str, message: str, *, log_line: str | None = None
    ) -> None:
        """写入 ``_replenish`` 状态；仅在状态迁移时输出日志。"""
        with self._status_lock:
            current = self._status.get("_replenish")
            changed = (
                current is None
                or current.get("status") != status
                or current.get("message") != message
            )
            if changed:
                self._status["_replenish"] = {"status": status, "message": message}
        if changed and log_line is not None:
            self._log(log_line)
