"""控制台 Token 解析与短 Token 警告的行为测试。"""

from __future__ import annotations

import unittest

from st_rotator.cli import _warn_short_token, resolve_console_token


class ResolveTokenTest(unittest.TestCase):
    def test_explicit_overrides_config(self) -> None:
        self.assertEqual(resolve_console_token("cli", "cfg"), "cli")

    def test_config_used_when_no_explicit(self) -> None:
        self.assertEqual(resolve_console_token(None, "cfg"), "cfg")

    def test_none_when_both_empty(self) -> None:
        self.assertIsNone(resolve_console_token(None, ""))

    def test_tray_fallback_used_when_empty(self) -> None:
        self.assertEqual(resolve_console_token(None, "", fallback="fb"), "fb")


class TokenWarningTest(unittest.TestCase):
    def test_short_token_warns(self) -> None:
        messages: list[str] = []
        _warn_short_token("short", messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn("控制台 Token 长度不足", messages[0])

    def test_long_token_silent(self) -> None:
        messages: list[str] = []
        _warn_short_token("0123456789abcdef", messages.append)
        self.assertEqual(messages, [])

    def test_none_token_silent(self) -> None:
        messages: list[str] = []
        _warn_short_token(None, messages.append)
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
