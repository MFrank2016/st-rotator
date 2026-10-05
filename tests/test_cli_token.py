"""控制台 Token 解析与最小长度校验的行为测试。"""

from __future__ import annotations

import unittest

from st_rotator.cli import _check_token_length, resolve_console_token


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
        self.assertIs(_check_token_length("0123456789abcdefghijklmn", messages.append), True)
        self.assertEqual(messages, [])

    def test_none_and_empty_accepted(self) -> None:
        messages: list[str] = []
        self.assertIs(_check_token_length(None, messages.append), True)
        self.assertIs(_check_token_length("", messages.append), True)
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
