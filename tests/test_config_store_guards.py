"""config / errors 的中危修复回归（环境变量展开、密码不 strip、set_section 校验、reload 回滚、pickle）。"""

from __future__ import annotations

import copy
import json
import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.config import (
    AccountConfig,
    Config,
    ConfigError,
    ConfigStore,
    expand_env,
)
from st_rotator.errors import ApiError, RotationExhausted


def _write_config(path: Path, accounts: list[dict] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": accounts or [{"name": "账号1", "api_keys": ["sk-a"]}],
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 7. expand_env
class ExpandEnvTest(unittest.TestCase):
    def test_unset_without_default_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SR_MISSING_XYZ", None)
            with self.assertRaises(ConfigError):
                expand_env("${SR_MISSING_XYZ}")

    def test_explicit_empty_is_not_unset(self) -> None:
        """显式设成空串：${VAR} 应展开成空串，而不是被当成未设置。"""
        with mock.patch.dict(os.environ, {"SR_EMPTY_XYZ": ""}):
            self.assertEqual(expand_env("a${SR_EMPTY_XYZ}b"), "ab")

    def test_empty_with_default_uses_default(self) -> None:
        """${VAR:-default} 的 POSIX 语义：未设置或为空都取默认值。"""
        with mock.patch.dict(os.environ, {"SR_EMPTY_XYZ": ""}):
            self.assertEqual(expand_env("${SR_EMPTY_XYZ:-fb}"), "fb")

    def test_non_empty_wins_over_default(self) -> None:
        with mock.patch.dict(os.environ, {"SR_SET_XYZ": "v"}):
            self.assertEqual(expand_env("${SR_SET_XYZ:-fb}"), "v")


# ---------------------------------------------------------------- 8. 密码不 strip
class PasswordStripTest(unittest.TestCase):
    def test_password_keeps_whitespace(self) -> None:
        acct = AccountConfig.from_dict(
            {
                "name": "a",
                "api_keys": ["sk-1"],
                "user": " u ",
                "phone": " 138 ",
                "password": " p w ",
            }
        )
        self.assertEqual(acct.user, "u")  # user / phone 仍然 strip
        self.assertEqual(acct.phone, "138")
        self.assertEqual(acct.password, " p w ")  # 密码原样保留

    def test_password_env_placeholder_not_stripped(self) -> None:
        with mock.patch.dict(os.environ, {"SR_PW_XYZ": " pw "}):
            acct = AccountConfig.from_dict(
                {"name": "a", "api_keys": ["sk-1"], "password": "${SR_PW_XYZ}"}
            )
        self.assertEqual(acct.password, " pw ")


# ---------------------------------------------------------------- 9. set_section
class SetSectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"
        _write_config(self.path)
        self.store = ConfigStore.load(self.path)

    def test_rejects_empty_name(self) -> None:
        with self.assertRaises(ConfigError):
            self.store.set_section("", {"a": 1})
        with self.assertRaises(ConfigError):
            self.store.set_section(123, {"a": 1})  # type: ignore[arg-type]

    def test_rejects_non_dict_existing_section(self) -> None:
        self.store._raw["flash_lite_exchange"] = 42
        with self.assertRaises(ConfigError):
            self.store.set_section("flash_lite_exchange", {"enabled": True})
        self.assertEqual(self.store._raw["flash_lite_exchange"], 42)  # 没被覆盖

    def test_creates_and_updates_section(self) -> None:
        self.assertEqual(
            self.store.set_section("flash_lite_exchange", {"enabled": True}),
            {"enabled": True},
        )
        self.assertEqual(
            self.store.set_section("flash_lite_exchange", {"max_workers": 2}),
            {"enabled": True, "max_workers": 2},
        )


# ---------------------------------------------------------------- 10. reload 回滚
class ReloadRollbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"
        _write_config(self.path)
        self.store = ConfigStore.load(self.path)

    def _assert_rolled_back(self, action) -> None:
        before = copy.deepcopy(self.store._raw)
        with mock.patch.object(
            self.store, "reload", side_effect=ConfigError("校验失败")
        ):
            with self.assertRaises(ConfigError):
                action()
        self.assertEqual(self.store._raw, before)  # 内存里没留下未经验证的改动

    def test_add_account_rolls_back(self) -> None:
        self._assert_rolled_back(
            lambda: self.store.add_account(
                "账号2", user="u", phone="p", password="pw", api_keys=["sk-b"]
            )
        )

    def test_set_credentials_rolls_back(self) -> None:
        self._assert_rolled_back(
            lambda: self.store.set_account_credentials(
                "账号1", user="u", phone="p", password="pw"
            )
        )

    def test_replace_keys_rolls_back(self) -> None:
        self._assert_rolled_back(
            lambda: self.store.replace_account_keys("账号1", ["sk-new"])
        )

    def test_successful_mutation_still_reloads(self) -> None:
        self.store.add_account(
            "账号2", user="u", phone="p", password="pw", api_keys=["sk-b"]
        )
        self.assertEqual(
            [a.name for a in self.store.config.accounts], ["账号1", "账号2"]
        )


# ---------------------------------------------------------------- 11. pickle
class PickleTest(unittest.TestCase):
    def test_rotation_exhausted_round_trip(self) -> None:
        exc = RotationExhausted(
            "全部失败", attempts=3, last_status=429, last_body='{"e":1}'
        )
        clone = pickle.loads(pickle.dumps(exc))
        self.assertIsInstance(clone, RotationExhausted)
        self.assertEqual(str(clone), "全部失败")
        self.assertEqual(clone.attempts, 3)
        self.assertEqual(clone.last_status, 429)
        self.assertEqual(clone.last_body, '{"e":1}')

    def test_api_error_round_trip(self) -> None:
        exc = ApiError(400, '{"error":"bad"}', "上游参数错误")
        clone = pickle.loads(pickle.dumps(exc))
        self.assertIsInstance(clone, ApiError)
        self.assertEqual(str(clone), "上游参数错误")
        self.assertEqual(clone.status, 400)
        self.assertEqual(clone.body, '{"error":"bad"}')

    def test_api_error_default_message_survives(self) -> None:
        exc = ApiError(404, "x" * 600)
        clone = pickle.loads(pickle.dumps(exc))
        self.assertEqual(str(clone), str(exc))
        self.assertEqual(clone.status, 404)


if __name__ == "__main__":
    unittest.main()
