"""``rotate_account``（把账号轮换操作抽成模块级函数）的单元测试。

直接测模块级函数 ``rotate_account``，不经 ``AutoRenewWorker``：不访问真实商汤。
Fake 风格与 ``tests/test_autorenew_worker.py`` 一致：

- ``_FakeLogin``：记录 (user, password)，按配置抛错。
- ``_FakeKeys``：KeyTransport 三方法，记录调用（含顺序 ops），按配置返回/抛错。
- ``persist`` 记录 spy：记录 (账号名, 该账号全部 key 列表, 新 key 明文)。
"""

import unittest

from st_rotator.autorenew import (
    DETAIL_LIMIT,
    KeyApiError,
    KeyInfo,
    RotationOutcome,
    rotate_account,
)
from st_rotator.config import AccountConfig
from st_rotator.quota import QuotaAuthError, QuotaUnavailable, TokenBundle

TOKEN = TokenBundle(
    access_token="tok-plain-abc",
    refresh_token="refresh-xyz",
    expires_in=10800,
    acquired_at=0.0,
)


class _FakeLogin:
    """登录 Fake：记录 (user, password)；exc 抛错。"""

    def __init__(self, *, bundle=TOKEN, exc=None):
        self.bundle = bundle
        self.exc = exc
        self.calls = []

    def __call__(self, user: str, password: str) -> TokenBundle:
        self.calls.append((user, password))
        if self.exc is not None:
            raise self.exc
        return self.bundle


class _FakeKeys:
    """KeyTransport 三方法的 Fake：记录调用（含顺序 ops），按配置返回/抛错。"""

    def __init__(self):
        self.ops = []
        self.list_calls = []
        self.create_calls = []
        self.delete_calls = []
        self.list_result = []
        self.created = KeyInfo(
            id="k-new",
            displayname="auto",
            api_key="sk-new-plain",
            key_type="API_KEY_TYPE_TOKEN_PLAN",
            create_time="t-new",
        )
        self.create_exc = None
        self.list_exc = None
        self.delete_exc = None

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        self.ops.append("list")
        self.list_calls.append({"token": access_token})
        if self.list_exc is not None:
            raise self.list_exc
        return list(self.list_result)

    def create_key(self, access_token, *, displayname, key_type):
        self.ops.append("create")
        self.create_calls.append(
            {"token": access_token, "displayname": displayname, "key_type": key_type}
        )
        if self.create_exc is not None:
            raise self.create_exc
        return self.created

    def delete_key(self, access_token, *, key_id):
        self.ops.append("delete")
        self.delete_calls.append({"token": access_token, "key_id": key_id})
        if self.delete_exc is not None:
            raise self.delete_exc


def _key(key_id: str, api_key: str) -> KeyInfo:
    return KeyInfo(
        id=key_id,
        displayname="k",
        api_key=api_key,
        key_type="API_KEY_TYPE_TOKEN_PLAN",
        create_time="t",
    )


class RotateAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_calls = []

    def _persist(self, name, keys_list, new_key) -> None:
        self.persist_calls.append((name, list(keys_list), new_key))

    def _account(self, **kwargs) -> AccountConfig:
        return AccountConfig(
            name=kwargs.pop("name", "账号1"),
            api_keys=kwargs.pop("api_keys", ["sk-a", "sk-b"]),
            user=kwargs.pop("user", "u1"),
            password=kwargs.pop("password", "p1"),
            **kwargs,
        )

    # ------------------------------------------------------------ 1. 成功路径
    def test_ok_path_order_and_args(self):
        """ok：list → delete → create → persist，参数与顺序正确。"""
        account = self._account(api_keys=["sk-a", "sk-b"])
        login = _FakeLogin()
        keys = _FakeKeys()
        keys.list_result = [_key("old-1", "sk-old1")]
        outcome = rotate_account(
            account, login=login, keys=keys, persist=self._persist
        )
        self.assertIsInstance(outcome, RotationOutcome)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.message, "")
        # 登录参数正确
        self.assertEqual(login.calls, [("u1", "p1")])
        # 平台操作顺序：先 list+delete-all 后 create
        self.assertEqual(keys.ops, ["list", "delete", "create"])
        self.assertEqual(
            keys.delete_calls, [{"token": TOKEN.access_token, "key_id": "old-1"}]
        )
        self.assertEqual(
            keys.create_calls,
            [
                {
                    "token": TOKEN.access_token,
                    "displayname": "auto",
                    "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                }
            ],
        )
        # persist 收到 (账号名, 该账号全部 key, 新 key 明文)
        self.assertEqual(
            self.persist_calls,
            [("账号1", ["sk-a", "sk-b"], "sk-new-plain")],
        )

    def test_ok_path_honours_custom_key_name_and_type(self):
        """ok：key_name / key_type 透传给 create_key。"""
        account = self._account(api_keys=["sk-a"])
        keys = _FakeKeys()
        keys.list_result = []
        rotate_account(
            account,
            login=_FakeLogin(),
            keys=keys,
            persist=self._persist,
            key_name="custom",
            key_type="API_KEY_TYPE_METERED",
        )
        self.assertEqual(
            keys.create_calls,
            [
                {
                    "token": TOKEN.access_token,
                    "displayname": "custom",
                    "key_type": "API_KEY_TYPE_METERED",
                }
            ],
        )

    # ------------------------------------------------------------ 2. 密码错误
    def test_password_error_zero_platform_calls_no_persist(self):
        """QuotaAuthError → password_error，零平台调用、不 persist。"""
        account = self._account(api_keys=["sk-bad"])
        login = _FakeLogin(exc=QuotaAuthError("用户名或密码错误"))
        keys = _FakeKeys()
        outcome = rotate_account(
            account, login=login, keys=keys, persist=self._persist
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "password_error")
        self.assertEqual(outcome.message, "用户名或密码错误")
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 3. 缺依赖
    def test_unavailable_silent(self):
        """QuotaUnavailable → unavailable，固定文案，零平台调用、不 persist。"""
        account = self._account(api_keys=["sk-bad"])
        login = _FakeLogin(exc=QuotaUnavailable("未安装 jwcrypto"))
        keys = _FakeKeys()
        outcome = rotate_account(
            account, login=login, keys=keys, persist=self._persist
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.message, "缺 jwcrypto 等依赖，无法登录")
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 4. 登录瞬时失败
    def test_check_error_on_login_runtime_error(self):
        """登录抛 RuntimeError → check_error（非 password_error），零平台调用。"""
        account = self._account(api_keys=["sk-bad"])
        login = _FakeLogin(exc=RuntimeError("boom"))
        keys = _FakeKeys()
        outcome = rotate_account(
            account, login=login, keys=keys, persist=self._persist
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "check_error")
        self.assertEqual(outcome.message, "RuntimeError: boom")
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 5. delete 失败
    def test_check_error_on_delete_failure_no_create_no_persist(self):
        """delete 抛错 → check_error，不再 create、不 persist。"""
        account = self._account(api_keys=["sk-bad"])
        keys = _FakeKeys()
        keys.list_result = [_key("old-1", "sk-old")]
        keys.delete_exc = KeyApiError(500, "server error")
        outcome = rotate_account(
            account, login=_FakeLogin(), keys=keys, persist=self._persist
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "check_error")
        self.assertIn("KeyApiError", outcome.message)
        self.assertEqual(keys.ops, ["list", "delete"])
        self.assertEqual(keys.create_calls, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 6. 多 key 全删
    def test_multiple_keys_all_deleted(self):
        """平台侧多把 key 全部注销后再 create。"""
        account = self._account(api_keys=["sk-a"])
        keys = _FakeKeys()
        keys.list_result = [
            _key("old-1", "sk-1"),
            _key("old-2", "sk-2"),
            _key("old-3", "sk-3"),
        ]
        outcome = rotate_account(
            account, login=_FakeLogin(), keys=keys, persist=self._persist
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(
            keys.ops, ["list", "delete", "delete", "delete", "create"]
        )
        self.assertEqual(
            [c["key_id"] for c in keys.delete_calls], ["old-1", "old-2", "old-3"]
        )
        self.assertEqual(self.persist_calls, [("账号1", ["sk-a"], "sk-new-plain")])

    # ------------------------------------------------------------ 7. 截断
    def test_message_truncated_to_detail_limit(self):
        """超长错误摘要截断到 DETAIL_LIMIT。"""
        account = self._account(api_keys=["sk-bad"])
        login = _FakeLogin(exc=RuntimeError("x" * (DETAIL_LIMIT * 2)))
        keys = _FakeKeys()
        outcome = rotate_account(
            account, login=login, keys=keys, persist=self._persist
        )
        self.assertEqual(outcome.status, "check_error")
        self.assertEqual(len(outcome.message), DETAIL_LIMIT)


if __name__ == "__main__":
    unittest.main()
