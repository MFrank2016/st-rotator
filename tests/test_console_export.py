"""控制台「复制全部账号」导出：导入格式 2（手机--用户名--密码--apikey）。"""

import unittest

from st_rotator.config import Config
from st_rotator.ui import ConsoleState


class _FakeRotator:
    def __init__(self, config):
        self.config = config


def _state(accounts) -> ConsoleState:
    cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1", "accounts": accounts})
    return ConsoleState(store=None, rotator=_FakeRotator(cfg))  # type: ignore[arg-type]


class ExportAccountsTest(unittest.TestCase):
    def test_format_and_plaintext(self):
        st = _state(
            [
                {
                    "name": "账号1",
                    "api_keys": ["sk-1"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                }
            ]
        )
        out = st.export_accounts()
        self.assertEqual(out["text"], "138--u1--p1--sk-1")
        self.assertEqual(out["accounts"], 1)
        self.assertEqual(out["lines"], 1)
        self.assertEqual(out["skipped"], 0)

    def test_multi_key_one_line_each(self):
        st = _state(
            [
                {
                    "name": "账号1",
                    "api_keys": ["sk-1", "sk-2"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                }
            ]
        )
        out = st.export_accounts()
        self.assertEqual(out["text"], "138--u1--p1--sk-1\n138--u1--p1--sk-2")
        self.assertEqual(out["lines"], 2)

    def test_skips_accounts_without_credentials(self):
        st = _state(
            [
                {
                    "name": "有凭据",
                    "api_keys": ["sk-1"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                },
                {"name": "无凭据", "api_keys": ["sk-2"]},
            ]
        )
        out = st.export_accounts()
        self.assertEqual(out["text"], "138--u1--p1--sk-1")
        self.assertEqual(out["accounts"], 1)
        self.assertEqual(out["skipped"], 1)

    def test_empty_when_nothing_exportable(self):
        st = _state([{"name": "无凭据", "api_keys": ["sk-1"]}])
        out = st.export_accounts()
        self.assertEqual(out["text"], "")
        self.assertEqual(out["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
