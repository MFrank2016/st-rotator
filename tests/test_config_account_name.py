"""config.py 账号命名与迁移辅助函数的单元测试。

- ``account_name_for``：新账号命名（用户名优先、回退自动编号）。
- ``migrate_account_names``：把自动编号账号（``账号N``）迁移成用户名。
- ``next_account_name``：自动编号的既有语义（防回归）。
"""

import unittest

from st_rotator.config import (
    account_name_for,
    migrate_account_names,
    next_account_name,
)


class AccountNameForTest(unittest.TestCase):
    def test_prefers_username(self) -> None:
        self.assertEqual(account_name_for("snT7Ajpzmbj01", ["账号1"]), "snT7Ajpzmbj01")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(account_name_for("  abc  ", []), "abc")

    def test_empty_user_falls_back_to_auto(self) -> None:
        self.assertEqual(account_name_for("", ["账号1"]), "账号2")
        self.assertEqual(account_name_for("   ", ["账号1"]), "账号2")

    def test_collision_falls_back_to_auto(self) -> None:
        self.assertEqual(account_name_for("账号1", ["账号1"]), "账号2")

    def test_collision_with_username_falls_back(self) -> None:
        self.assertEqual(account_name_for("bob", ["bob"]), "账号2")


class MigrateAccountNamesTest(unittest.TestCase):
    def test_renames_auto_named_accounts_to_username(self) -> None:
        raw = {
            "accounts": [
                {"name": "账号1", "user": "bob", "api_keys": ["k"]},
                {"name": "账号2", "user": "alice", "api_keys": ["k"]},
            ]
        }
        self.assertTrue(migrate_account_names(raw))
        self.assertEqual([a["name"] for a in raw["accounts"]], ["bob", "alice"])

    def test_keeps_custom_names(self) -> None:
        raw = {"accounts": [{"name": "prod", "user": "bob"}]}
        self.assertFalse(migrate_account_names(raw))
        self.assertEqual(raw["accounts"][0]["name"], "prod")

    def test_skips_empty_user(self) -> None:
        raw = {"accounts": [{"name": "账号1", "user": "", "api_keys": ["k"]}]}
        self.assertFalse(migrate_account_names(raw))
        self.assertEqual(raw["accounts"][0]["name"], "账号1")

    def test_skips_collision(self) -> None:
        raw = {
            "accounts": [
                {"name": "账号1", "user": "bob"},
                {"name": "bob", "user": "alice"},
            ]
        }
        self.assertFalse(migrate_account_names(raw))
        self.assertEqual([a["name"] for a in raw["accounts"]], ["账号1", "bob"])

    def test_two_auto_accounts_sharing_user_only_first_renamed(self) -> None:
        raw = {
            "accounts": [
                {"name": "账号1", "user": "bob"},
                {"name": "账号2", "user": "bob"},
            ]
        }
        self.assertTrue(migrate_account_names(raw))
        self.assertEqual([a["name"] for a in raw["accounts"]], ["bob", "账号2"])

    def test_non_list_accounts_is_noop(self) -> None:
        self.assertFalse(migrate_account_names({}))
        self.assertFalse(migrate_account_names({"accounts": "x"}))


class NextAccountNameTest(unittest.TestCase):
    def test_appends_at_end(self) -> None:
        self.assertEqual(next_account_name(["账号1", "账号3"]), "账号4")

    def test_empty_gives_first(self) -> None:
        self.assertEqual(next_account_name([]), "账号1")


if __name__ == "__main__":
    unittest.main()
