"""控制台 Token 解析与最小长度校验的行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from st_rotator.cli import (
    _check_token_length,
    _load_or_create_token,
    resolve_console_token,
)


class ResolveTokenTest(unittest.TestCase):
    def test_explicit_overrides_config(self) -> None:
        self.assertEqual(resolve_console_token("cli", "cfg"), "cli")

    def test_config_used_when_no_explicit(self) -> None:
        self.assertEqual(resolve_console_token(None, "cfg"), "cfg")

    def test_none_when_both_empty(self) -> None:
        self.assertIsNone(resolve_console_token(None, ""))

    def test_tray_fallback_used_when_empty(self) -> None:
        self.assertEqual(resolve_console_token(None, "", fallback="fb"), "fb")


class TokenLengthTest(unittest.TestCase):
    def test_short_token_rejected(self) -> None:
        messages: list[str] = []
        self.assertIs(_check_token_length("short", messages.append), False)
        self.assertEqual(len(messages), 1)
        self.assertIn("控制台 Token", messages[0])
        self.assertTrue("16" in messages[0] or "长度" in messages[0])

    def test_min_length_accepted(self) -> None:
        messages: list[str] = []
        self.assertIs(_check_token_length("0123456789abcdef", messages.append), True)
        self.assertEqual(messages, [])

    def test_long_token_accepted(self) -> None:
        messages: list[str] = []
        self.assertIs(
            _check_token_length("0123456789abcdefghijklmn", messages.append), True
        )
        self.assertEqual(messages, [])

    def test_none_and_empty_accepted(self) -> None:
        messages: list[str] = []
        self.assertIs(_check_token_length(None, messages.append), True)
        self.assertIs(_check_token_length("", messages.append), True)
        self.assertEqual(messages, [])


class TrayTokenFileTest(unittest.TestCase):
    """``.tray-token`` 必须仅属主可读写，否则同机其他用户可直接拿它访问控制台。"""

    def test_created_file_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = _load_or_create_token(Path(tmp))
            path = Path(tmp) / ".tray-token"
            self.assertEqual(path.read_text(encoding="utf-8"), token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_loose_file_is_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".tray-token"
            path.write_text("existing-token", encoding="utf-8")
            path.chmod(0o644)
            self.assertEqual(_load_or_create_token(Path(tmp)), "existing-token")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
