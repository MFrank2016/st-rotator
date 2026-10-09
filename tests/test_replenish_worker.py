"""ReplenishWorker（自动补充账号）的单元测试。

全部使用 Fake sms / authn / keys / registry / persist：绝不访问真实
ejiema / SenseNova。

Fake 结构（与 test_autorenew_worker 同风格）：
- ``_FakeSms``：SmsTransport 三方法；``get_msg`` 支持「队列 + 兜底」，用来模拟
  首次超时后成功（S3）。
- ``_FakeAuthn``：AuthnTransport 全方法；``register`` 按异常队列抛 AuthnError；
  ``exchange_code`` 返回带真实 ``sub`` 的 JWT，供 ``authn.jwt_sub`` 解码出
  user_id（S2 接管路径必需）。
- ``_FakeKeys``：KeyTransport 三方法，记录调用与顺序 ops。
- registry 用真实 ``Registry``（tmp 文件，零网络）。
- persist 为记录 spy。
"""

import base64
import json
import random
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from st_rotator import replenish
from st_rotator.authn import AuthnError
from st_rotator.autorenew import KeyInfo
from st_rotator.config import AccountConfig
from st_rotator.quota import TokenBundle
from st_rotator.registry import Registry
from st_rotator.replenish import (
    ReplenishWorker,
    count_available,
    count_unavailable,
    generate_credentials,
    spend_ok,
    today_str,
)

# 生成器参数校验用的字符集正则（与 replenish.py 的字符集一致，排除 $ { }）
_USER_RE = re.compile(r"^[A-Za-z0-9]{6,24}$")
_PW_RE = re.compile(r"^[A-Za-z0-9~!@#%^&*?_+.,;:-]{8,32}$")
_CLASS_PATS = (r"[a-z]", r"[A-Z]", r"[0-9]", r"[~!@#%^&*?_+.,;:\-]")

FIXED_EPOCH = 1700000000.0  # 固定时钟：today_str 结果稳定可复算


def _jwt(sub: str) -> str:
    """构造一个只带 sub 的未签名 JWT，供 authn.jwt_sub 解码。"""

    def _enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_enc({'alg': 'none', 'typ': 'JWT'})}.{_enc({'sub': sub})}."


BUNDLE = TokenBundle(
    access_token=_jwt("user-42"),
    refresh_token="refresh-xyz",
    expires_in=10800,
    acquired_at=0.0,
)


def _password_classes(password: str) -> int:
    """统计密码命中的字符类别数（lower/upper/digit/special）。"""
    return sum(1 for pat in _CLASS_PATS if re.search(pat, password))


class _JumpClock:
    """前两次返回 0.0，之后一直返回 999.0。

    第一次调用被 ``today_str`` 消耗，第二次是 ``wait_sms_code`` 的 start——
    第三次起 (999-0) >= timeout，wait_sms_code 首次轮询即判超时。
    """

    def __init__(self) -> None:
        self._calls = 0

    def __call__(self) -> float:
        self._calls += 1
        if self._calls < 3:
            return 0.0
        return 999.0


class _FakeSms:
    """SmsTransport Fake：get_phone 从 phones 逐个弹出；get_msg 走队列，空则兜底。"""

    def __init__(
        self, *, balance: float = 10.0, phones=(), msg: str = "【商汤】验证码 123456"
    ):
        self.balance = balance
        self.phones = list(phones)
        self.msg = msg
        self.msg_queue: list[str] = []
        self.left_calls: list[int] = []
        self.phone_calls: list[str] = []
        self.msg_calls: list[tuple[str, str]] = []

    def left_amount(self) -> float:
        self.left_calls.append(1)
        return self.balance

    def get_phone(self, *, keyword: str) -> str:
        self.phone_calls.append(keyword)
        return self.phones.pop(0)

    def get_msg(self, phone: str, *, keyword: str) -> str:
        self.msg_calls.append((phone, keyword))
        if self.msg_queue:
            return self.msg_queue.pop(0)
        return self.msg


class _FakeAuthn:
    """AuthnTransport Fake：register / mint 的异常走「逐个消费」队列。"""

    def __init__(
        self,
        *,
        bundle: TokenBundle = BUNDLE,
        register_excs=(),
        send_result: str | None = "tok-sms",
        send_queue=(),
        mint_excs=(),
        user_info=None,
        sms_login_result=None,
        sms_login_excs=(),
        login_next_result=None,
    ):
        self.bundle = bundle
        self.register_excs = list(register_excs)
        self.send_result = send_result
        self.send_queue = list(send_queue)
        self.mint_excs = list(mint_excs)
        self.user_info = user_info
        self.sms_login_result = (
            {"tenant_list": []} if sms_login_result is None else sms_login_result
        )
        self.sms_login_excs = list(sms_login_excs)
        self.login_next_result = login_next_result or {
            "redirect": "http://redirect/login?code=def"
        }
        self.login_next_calls: list[tuple[object, object, object, object]] = []
        self.mint_calls: list[str | None] = []
        self.exchange_calls: list[tuple[object, object]] = []
        self.send_calls: list[tuple[str, str | None]] = []
        self.register_calls: list[tuple[object, object, object, object]] = []
        self.sms_login_calls: list[tuple[object, object, object]] = []
        self.pw_code_calls: list[tuple[object, object]] = []
        self.change_pw_calls: list[tuple[str, str, str, str, str]] = []
        self.user_info_calls: list[tuple[object, object]] = []

    def mint_login_challenge(self, *, intent: str | None = None) -> tuple[str, str]:
        self.mint_calls.append(intent)
        if self.mint_excs:
            exc = self.mint_excs.pop(0)
            if exc is not None:
                raise exc
        return ("challenge-1", "verifier-1")

    def exchange_code(
        self, redirect_url: str | None, code_verifier: str
    ) -> TokenBundle:
        self.exchange_calls.append((redirect_url, code_verifier))
        return self.bundle

    def send_sms_code(self, phone: str, *, code_key: str | None = None) -> str | None:
        self.send_calls.append((phone, code_key))
        if self.send_queue:
            return self.send_queue.pop(0)
        return self.send_result

    def register(
        self, *, token_code: str, user_name: str, password: str, challenge: str
    ) -> str:
        self.register_calls.append((token_code, user_name, password, challenge))
        if self.register_excs:
            exc = self.register_excs.pop(0)
            if exc is not None:
                raise exc
        return "http://redirect/register?code=abc"

    def sms_login(self, *, token_code: str, verify_code: str, challenge: str):
        self.sms_login_calls.append((token_code, verify_code, challenge))
        if self.sms_login_excs:
            exc = self.sms_login_excs.pop(0)
            if exc is not None:
                raise exc
        return self.sms_login_result

    def login_next(self, *, challenge: str, username: str, user_id: str, sign: str):
        self.login_next_calls.append((challenge, username, user_id, sign))
        return self.login_next_result

    def request_change_password_code(self, access_token: str, user_id: str) -> str:
        self.pw_code_calls.append((access_token, user_id))
        return "tok-pw-2"

    def change_password(
        self,
        access_token: str,
        user_id: str,
        *,
        token_code: str,
        verify_code: str,
        password: str,
    ) -> None:
        self.change_pw_calls.append(
            (access_token, user_id, token_code, verify_code, password)
        )

    def get_user_info(self, access_token: str, user_id: str):
        self.user_info_calls.append((access_token, user_id))
        if isinstance(self.user_info, Exception):
            raise self.user_info
        if self.user_info is not None:
            return self.user_info
        return {"user_name": "接管实名"}


class _FakeKeys:
    """KeyTransport Fake：记录调用与顺序 ops（同 test_autorenew_worker）。"""

    def __init__(self, *, list_result=()):
        self.ops: list[str] = []
        self.list_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.list_result = list(list_result)
        self.created = KeyInfo(
            id="k-new",
            displayname="auto",
            api_key="sk-new-plain",
            key_type="API_KEY_TYPE_TOKEN_PLAN",
            create_time="t-new",
        )

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        self.ops.append("list")
        self.list_calls.append(
            {
                "token": access_token,
                "key_type": key_type,
                "page_size": page_size,
                "page_token": page_token,
            }
        )
        return list(self.list_result)

    def create_key(self, access_token, *, displayname, key_type):
        self.ops.append("create")
        self.create_calls.append(
            {"token": access_token, "displayname": displayname, "key_type": key_type}
        )
        return self.created

    def delete_key(self, access_token, *, key_id):
        self.ops.append("delete")
        self.delete_calls.append({"token": access_token, "key_id": key_id})


def _key(key_id: str) -> KeyInfo:
    return KeyInfo(
        id=key_id,
        displayname="旧key",
        api_key=f"sk-old-{key_id}",
        key_type="API_KEY_TYPE_TOKEN_PLAN",
        create_time="t-old",
    )


class ReplenishWorkerTest(unittest.TestCase):
    """全部断言都跑在 Fake 之上；registry 用真实 Registry（tmp 文件）。"""

    persist_calls: list[
        tuple[str, str, str, str]
    ] = []  # 类级占位；setUp/_worker 每次重建
    persist_outcomes: list[str] = []
    log_lines: list[str] = []

    # ------------------------------------------------------------ 辅助
    def setUp(self) -> None:
        self.persist_calls = []
        self.persist_outcomes = []
        self.log_lines = []

    def _registry(self) -> Registry:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Registry.load(Path(tmp.name) / "state.json")

    def _worker(
        self,
        *,
        sms,
        authn,
        keys,
        registry,
        accounts=(),
        statuses=None,
        target: int = 1,
        clock: Callable[[], float] | None = None,
        persist=None,
        **kwargs,
    ) -> ReplenishWorker:
        self.persist_calls = []
        self.persist_outcomes = []
        self.log_lines = []

        def record(
            user: str, phone: str, password: str, api_key: str, outcome: str = "ok"
        ) -> None:
            self.persist_calls.append((user, phone, password, api_key))
            self.persist_outcomes.append(outcome)

        return ReplenishWorker(
            accounts=lambda: list(accounts),
            status_source=lambda: dict(statuses or {}),
            target=target,
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            persist=persist or record,
            clock=clock if clock is not None else (lambda: FIXED_EPOCH),
            log=lambda msg: self.log_lines.append(msg),
            **kwargs,
        )

    def _status(self, worker: ReplenishWorker) -> str:
        return worker.account_status()["_replenish"]["status"]

    # ------------------------------------------------------------ 1. 纯函数：count_available
    def test_count_available_counts_only_password_error(self):
        accounts = [
            AccountConfig(name="a", api_keys=["sk-a"]),
            AccountConfig(name="b", api_keys=["sk-b"]),
            AccountConfig(name="c", api_keys=["sk-c"]),
        ]
        statuses = {
            "a": {"status": "password_error", "message": "密码错"},
            "b": {"status": "ok", "message": ""},
            # c 无状态记录 -> 视为可用
        }
        self.assertEqual(count_available(accounts, statuses), 2)
        self.assertEqual(count_unavailable(accounts, statuses), 1)

    def test_count_unavailable_only_password_error(self):
        accounts = [
            AccountConfig(name="a", api_keys=["sk-a"]),
            AccountConfig(name="b", api_keys=["sk-b"]),
        ]
        statuses = {
            "a": {"status": "check_error", "message": "瞬时"},
            "b": {"status": "password_error", "message": "密码错"},
        }
        self.assertEqual(count_unavailable(accounts, statuses), 1)

    # ------------------------------------------------------------ 2. 纯函数：generate_credentials
    def test_generate_credentials_charset_and_classes(self):
        rng = random.Random(7)
        for _ in range(50):
            user, password = generate_credentials(rng)
            self.assertRegex(user, _USER_RE)
            self.assertEqual(len(password), 16)  # 默认长度
            self.assertRegex(password, _PW_RE)
            self.assertGreaterEqual(_password_classes(password), 3)

    def test_generate_credentials_excludes_dollar_brace(self):
        rng = random.Random(11)
        for _ in range(300):
            user, password = generate_credentials(
                rng, user_length=24, password_length=32
            )
            self.assertEqual(len(user), 24)
            self.assertEqual(len(password), 32)
            for forbidden in ("$", "{", "}"):
                self.assertNotIn(forbidden, user)
                self.assertNotIn(forbidden, password)

    # ------------------------------------------------------------ 3. 纯函数：spend_ok
    def test_spend_ok_first_read_allowed(self):
        today = today_str(lambda: FIXED_EPOCH)
        # 新一天（date 为空或不同）-> 重置：consumed=0，allowed
        self.assertEqual(
            spend_ok(
                5.0, {"date": "", "start_balance": None, "last_balance": None}, today
            ),
            (True, 0.0),
        )
        self.assertEqual(
            spend_ok(
                5.0,
                {"date": "2025-12-31", "start_balance": 10.0, "last_balance": 4.0},
                today,
            ),
            (True, 0.0),
        )
        # start_balance 为 None（首读基线未定）-> allowed
        self.assertEqual(
            spend_ok(
                5.0,
                {"date": today, "start_balance": None, "last_balance": 1.0},
                today,
            ),
            (True, 0.0),
        )

    def test_spend_ok_cap_stops(self):
        today = today_str(lambda: FIXED_EPOCH)
        state = {"date": today, "start_balance": 10.0, "last_balance": 4.0}
        self.assertEqual(spend_ok(5.0, state, today), (False, 6.0))
        self.assertEqual(spend_ok(10.0, state, today), (True, 6.0))
        # 余额不降反升（充值）-> consumed 截断为 0
        self.assertEqual(
            spend_ok(
                5.0, {"date": today, "start_balance": 10.0, "last_balance": 12.0}, today
            ),
            (True, 0.0),
        )

    def test_spend_ok_new_day_resets(self):
        yesterday = {"date": "2025-12-31", "start_balance": 10.0, "last_balance": 4.0}
        today = today_str(lambda: FIXED_EPOCH)
        self.assertNotEqual(yesterday["date"], today)
        self.assertEqual(spend_ok(5.0, yesterday, today), (True, 0.0))

    def test_spend_ok_zero_or_negative_cap_means_unlimited(self):
        today = today_str(lambda: FIXED_EPOCH)
        state = {"date": today, "start_balance": 10.0, "last_balance": 1.0}
        self.assertEqual(spend_ok(0, state, today), (True, 9.0))
        self.assertEqual(spend_ok(0.0, state, today), (True, 9.0))
        self.assertEqual(spend_ok(-1, state, today), (True, 9.0))

    # ------------------------------------------------------------ 4. S1 新注册
    def test_register_path_persists_new_account_no_revoke(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000001"])
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        # 新注册路径：只 create，绝不 list/delete（无 revoke）
        self.assertEqual(keys.ops, ["create"])
        self.assertEqual(keys.delete_calls, [])
        # 流程参数
        self.assertEqual(authn.send_calls, [("13800000001", None)])
        self.assertEqual(authn.register_calls[0][0], "tok-sms")  # token_code
        self.assertEqual(authn.register_calls[0][3], "challenge-1")  # challenge
        self.assertEqual(
            authn.exchange_calls, [("http://redirect/register?code=abc", "verifier-1")]
        )
        self.assertEqual(keys.create_calls[0]["displayname"], "auto")
        self.assertEqual(keys.create_calls[0]["key_type"], "API_KEY_TYPE_TOKEN_PLAN")
        # persist(user, phone, password, api_key) —— 用户名/密码是生成器产出
        self.assertEqual(len(self.persist_calls), 1)
        user, phone, password, api_key = self.persist_calls[0]
        self.assertRegex(user, _USER_RE)
        self.assertEqual(phone, "13800000001")
        self.assertRegex(password, _PW_RE)
        self.assertGreaterEqual(_password_classes(password), 3)
        self.assertEqual(api_key, "sk-new-plain")
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)
        # 号码已被占用登记
        self.assertTrue(registry.is_phone_used("13800000001"))

    def test_register_path_emits_prefixed_logs(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000001"])
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()
        joined = "\n".join(self.log_lines)
        self.assertTrue(self.log_lines)
        self.assertTrue(all(line.startswith("[补号]") for line in self.log_lines))
        self.assertIn("已取号 13800000001", joined)
        self.assertIn("未注册", joined)
        self.assertIn("已注册新账号", joined)

    # ------------------------------------------------------------ 5. S2 接管
    def test_takeover_path_revokes_all_creates_key_and_changes_password(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000002"], msg="【商汤】验证码 888888")
        authn = _FakeAuthn(
            sms_login_result={
                "tenant_list": [{"user_id": "user-42"}],
                "redirect": "http://redirect/login?code=def",
            }
        )
        keys = _FakeKeys(list_result=[_key("old-1"), _key("old-2")])
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        # smsLogin 返回非空 tenant_list -> 手机号已注册 -> 走短信登录接管（不 register）
        self.assertEqual(authn.register_calls, [])
        self.assertEqual(len(authn.sms_login_calls), 1)
        self.assertEqual(authn.sms_login_calls[0][0], "tok-sms")  # token_code
        self.assertEqual(authn.sms_login_calls[0][1], "888888")  # verify_code
        # 吊销全部 key 后建新 key
        self.assertEqual(keys.ops, ["list", "delete", "delete", "create"])
        self.assertEqual([c["key_id"] for c in keys.delete_calls], ["old-1", "old-2"])
        self.assertEqual(keys.create_calls[0]["displayname"], "auto")
        # jwt_sub 解出 user_id；二次验证码走改密
        self.assertEqual(authn.pw_code_calls, [(BUNDLE.access_token, "user-42")])
        self.assertEqual(len(authn.change_pw_calls), 1)
        _tok, _uid, tok2, code2, new_pw = authn.change_pw_calls[0]
        self.assertEqual(tok2, "tok-pw-2")
        self.assertEqual(code2, "888888")
        self.assertRegex(new_pw, _PW_RE)
        self.assertGreaterEqual(_password_classes(new_pw), 3)
        # 用户名来自 get_user_info
        self.assertEqual(authn.user_info_calls, [(BUNDLE.access_token, "user-42")])
        user, phone, password, api_key = self.persist_calls[0]
        self.assertEqual(user, "接管实名")
        self.assertEqual(phone, "13800000002")
        self.assertEqual(api_key, "sk-new-plain")
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    def test_takeover_multi_tenant_uses_login_next(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000003"], msg="【商汤】验证码 888888")
        authn = _FakeAuthn(
            sms_login_result={
                "tenant_list": [
                    {"user_id": "u1", "username": "a", "sign": "s1"},
                    {
                        "user_id": "u2",
                        "username": "b",
                        "sign": "s2",
                        "is_last_login": True,
                    },
                ]
            }
        )
        keys = _FakeKeys(list_result=[_key("old-1")])
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        # 多租户：smsLogin 无 redirect -> loginNext 选中最近登录的 u2
        self.assertEqual(authn.register_calls, [])
        self.assertEqual(len(authn.login_next_calls), 1)
        _challenge, username, user_id, sign = authn.login_next_calls[0]
        self.assertEqual((username, user_id, sign), ("b", "u2", "s2"))
        self.assertEqual(keys.ops, ["list", "delete", "create"])
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    def test_takeover_password_sms_timeout_persists_with_partial_outcome(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000013"])
        sms.msg_queue = ["【商汤】验证码 888888", "[尚未收到]", "[尚未收到]"]
        authn = _FakeAuthn(
            sms_login_result={
                "tenant_list": [{"user_id": "user-42"}],
                "redirect": "http://redirect/login?code=def",
            }
        )
        keys = _FakeKeys(list_result=[_key("old-1")])
        calls = {"n": 0}

        def clock() -> float:
            calls["n"] += 1
            return 0.0 if calls["n"] <= 3 else 999.0

        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1, clock=clock
        )
        worker.run_once()

        # 改密二次短信超时：账号已接管（新 key 已建）-> 仍落账；兜底取回用户名、密码留空
        self.assertEqual(keys.ops, ["list", "delete", "create"])
        self.assertEqual(len(self.persist_calls), 1)
        user, phone, password, api_key = self.persist_calls[0]
        self.assertEqual(user, "接管实名")  # 兜底用已有 access_token 取回用户名
        self.assertEqual(password, "")  # 改密未生效，不落一个假密码
        self.assertEqual(phone, "13800000013")
        self.assertEqual(api_key, "sk-new-plain")
        self.assertEqual(self.persist_outcomes, ["takeover_password_unset"])
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_CHECK_ERROR)

    # ------------------------------------------------------------ 6. S3 短信超时换新号
    def test_sms_timeout_fetches_new_number_and_retries(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000003", "13800000004"])
        sms.msg_queue = ["[尚未收到]", "【商汤】验证码 111222"]
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            target=1,
            clock=_JumpClock(),
        )
        worker.run_once()

        # 首次 wait 超时 -> 换新号重跑 spend gate，第二次成功
        self.assertEqual(len(sms.phone_calls), 2)
        self.assertEqual(len(authn.send_calls), 2)
        self.assertEqual(len(sms.msg_calls), 2)
        self.assertEqual(len(self.persist_calls), 1)
        self.assertEqual(self.persist_calls[0][1], "13800000004")
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    # ------------------------------------------------------------ 7. S4 已用号码跳过
    def test_used_phone_skipped_different_phone_fetched(self):
        registry = self._registry()
        registry.claim_phone("13800000005")  # 预置一个已用号码
        sms = _FakeSms(phones=["13800000005", "13800000006"])
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        # 已用号被跳过（未 claim 第二次），取到新号并注册
        self.assertEqual(len(sms.phone_calls), 2)
        self.assertEqual(len(self.persist_calls), 1)
        self.assertEqual(self.persist_calls[0][1], "13800000006")
        self.assertTrue(registry.is_phone_used("13800000006"))
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    # ------------------------------------------------------------ 8. S5 每日额度耗尽
    def test_daily_cap_blocks_cycle_no_spend(self):
        registry = self._registry()
        today = today_str(lambda: FIXED_EPOCH)
        registry.note_balance(today, 10.0)
        registry.note_balance(today, 4.0)  # 已消费 6 元
        sms = _FakeSms(balance=1.0, phones=["13800000007"])
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            target=1,
            daily_spend_cap=5.0,
        )
        worker.run_once()

        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_BLOCKED_CAP)
        self.assertEqual(sms.phone_calls, [])  # 未取号
        self.assertEqual(authn.send_calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    def test_daily_cap_gate_uses_fresh_balance_not_stale(self):
        # 记录口径消费为 0，但平台实时余额已超上限：闸门必须用「刚读到的余额」
        # 判定，不能沿用上一次的余额（否则会多取一个号、白花一条短信钱）。
        registry = self._registry()
        today = today_str(lambda: FIXED_EPOCH)
        registry.note_balance(today, 10.0)  # 基线
        registry.note_balance(today, 10.0)  # 上次读数 = 基线（记录口径消费 0）
        sms = _FakeSms(balance=9.2, phones=["13800000001"])  # 实时：已消费 0.8
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            target=1,
            daily_spend_cap=0.5,
        )
        worker.run_once()

        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_BLOCKED_CAP)
        self.assertEqual(sms.phone_calls, [])  # 未取号

    # ------------------------------------------------------------ 9. S6 需要滑块
    def test_captcha_required_aborts_cycle_no_spend(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000008"])
        authn = _FakeAuthn(send_result=None)  # sendSmsCode 一直返回 None
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_CAPTCHA)
        self.assertEqual(authn.register_calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    def test_captcha_solver_injected_retries(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000009"])
        authn = _FakeAuthn(send_queue=[None, "tok-after-captcha"])
        keys = _FakeKeys()
        solver_calls: list[int] = []

        def solver() -> str:
            solver_calls.append(1)
            return "ck-123"

        worker = self._worker(
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            target=1,
            captcha_solver=solver,
        )
        worker.run_once()

        # 无 code_key 返回 None -> solver 出 code_key -> 重发成功
        self.assertEqual(
            authn.send_calls, [("13800000009", None), ("13800000009", "ck-123")]
        )
        self.assertEqual(solver_calls, [1])
        self.assertEqual(authn.register_calls[0][0], "tok-after-captcha")
        self.assertEqual(len(self.persist_calls), 1)
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    # ------------------------------------------------------------ 10. 目标已达成
    def test_target_met_is_idle_no_spend(self):
        registry = self._registry()
        account = AccountConfig(name="a", api_keys=["sk-a"])
        sms = _FakeSms(phones=[], balance=1.0)
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms,
            authn=authn,
            keys=keys,
            registry=registry,
            accounts=[account],
            target=1,
        )
        worker.run_once()

        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_IDLE)
        self.assertEqual(sms.left_calls, [])
        self.assertEqual(sms.phone_calls, [])
        self.assertEqual(authn.send_calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 11. challenge 过期重 mint
    def test_challenge_expired_re_mints_once(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000010"])
        authn = _FakeAuthn(
            mint_excs=[AuthnError(400, "challenge_expired", "challenge expired")]
        )
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        self.assertEqual(authn.mint_calls, ["register", "register"])  # 重 mint 一次
        self.assertEqual(
            authn.register_calls[0][3], "challenge-1"
        )  # 用第二次的 challenge
        self.assertEqual(len(self.persist_calls), 1)
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    # ------------------------------------------------------------ 12. username_taken 重试
    def test_username_taken_regenerates_and_retries_once(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000012"])
        authn = _FakeAuthn(
            register_excs=[AuthnError(400, "username_taken", "用户名已存在")]
        )
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, target=1
        )
        worker.run_once()

        self.assertEqual(len(authn.register_calls), 2)
        self.assertNotEqual(
            authn.register_calls[0][1], authn.register_calls[1][1]
        )  # 换了用户名
        self.assertEqual(
            authn.register_calls[1][2], authn.register_calls[0][2]
        )  # 密码不变
        self.assertEqual(len(self.persist_calls), 1)
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)

    # ------------------------------------------------------------ 13. 线程生命周期
    def test_start_stop_daemon_thread(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000011"])
        authn = _FakeAuthn()
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms, authn=authn, keys=keys, registry=registry, interval=0.05
        )
        counter = {"n": 0}
        original = worker.run_once

        def _counting():
            counter["n"] += 1
            original()

        worker.run_once = _counting
        worker.start()
        thread = worker._thread
        assert thread is not None
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "replenish")
        deadline = time.monotonic() + 2.0
        while counter["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()
        self.assertGreaterEqual(counter["n"], 2)  # 先立即跑一轮，再周期循环
        self.assertFalse(thread.is_alive())

    # ------------------------------------------------------------ 14. 热更新
    def test_reconfigure_updates_live_params_and_wakes(self):
        registry = self._registry()
        worker = self._worker(
            sms=_FakeSms(phones=[]),
            authn=_FakeAuthn(),
            keys=_FakeKeys(),
            registry=registry,
            target=1,
            interval=3600.0,
        )
        worker.reconfigure(
            target=3,
            interval=30.0,
            keyword="x",
            sms_poll_interval=2.0,
            sms_poll_timeout=20.0,
            daily_spend_cap=9.0,
            key_name="k",
            key_type="API_KEY_TYPE_METERED",
        )
        self.assertEqual(worker._target, 3)
        self.assertEqual(worker._interval, 30.0)
        self.assertEqual(worker._keyword, "x")
        self.assertEqual(worker._sms_poll_interval, 2.0)
        self.assertEqual(worker._sms_poll_timeout, 20.0)
        self.assertEqual(worker._daily_spend_cap, 9.0)
        self.assertEqual(worker._key_name, "k")
        self.assertEqual(worker._key_type, "API_KEY_TYPE_METERED")
        self.assertTrue(worker._wake.is_set())  # 唤醒循环，缩短间隔立即生效

    def test_reconfigure_raises_target_and_next_cycle_replenishes(self):
        registry = self._registry()
        sms = _FakeSms(phones=["13800000014"])
        keys = _FakeKeys()
        worker = self._worker(
            sms=sms,
            authn=_FakeAuthn(),
            keys=keys,
            registry=registry,
            accounts=[AccountConfig(name="a", api_keys=["sk-a"])],
            target=1,
        )
        worker.run_once()  # 1 可用 >= 目标 1 -> 空闲，不取号
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_IDLE)
        self.assertEqual(sms.phone_calls, [])

        worker.reconfigure(target=2)  # 目标抬到 2 -> 下一轮立即补 1 个
        worker.run_once()
        self.assertEqual(self._status(worker), ReplenishWorker.STATUS_OK)
        self.assertEqual(self.persist_calls[0][1], "13800000014")


if __name__ == "__main__":
    unittest.main()
