"""控制台已暴露 Anthropic Messages 接入信息（/anthropic 独立前缀）。

只查 ``ConsoleState.gateway_info()`` 的字段与控制台 HTML 的文案，不起服务、不联网。
"""

import unittest

from st_rotator.config import Config
from st_rotator.dashboard import DASHBOARD_HTML
from st_rotator.ui import ConsoleState


class _FakeRotator:
    """gateway_info 只读 config，不发网络。"""

    def __init__(self, config: Config) -> None:
        self.config = config


def _console() -> ConsoleState:
    cfg = Config.from_dict(
        {
            "base_url": "http://127.0.0.1:9/v1",
            "accounts": [{"name": "账号A", "api_keys": ["k1"]}],
        }
    )
    return ConsoleState(  # type: ignore[arg-type]
        store=None,
        rotator=_FakeRotator(cfg),
        host="127.0.0.1",
        port=8899,
        token="tk",
        log_file="",
    )


class GatewayInfoAnthropicTest(unittest.TestCase):
    def test_anthropic_fields_use_own_prefix(self) -> None:
        info = _console().gateway_info()
        self.assertEqual(info["anthropic_base_url"], "http://127.0.0.1:8899/anthropic")
        self.assertEqual(
            info["anthropic_messages_endpoint"],
            "http://127.0.0.1:8899/anthropic/v1/messages",
        )
        self.assertEqual(
            info["anthropic_models_endpoint"],
            "http://127.0.0.1:8899/anthropic/v1/models",
        )

    def test_anthropic_fields_follow_request_base(self) -> None:
        """对外反代地址优先，和 base_url 用同一个 base。"""
        info = _console().gateway_info(request_base="https://gw.example")
        self.assertEqual(info["base_url"], "https://gw.example/v1")
        self.assertEqual(info["anthropic_base_url"], "https://gw.example/anthropic")
        self.assertEqual(
            info["anthropic_messages_endpoint"],
            "https://gw.example/anthropic/v1/messages",
        )


class DashboardAnthropicTest(unittest.TestCase):
    def test_gateway_block_shows_anthropic_rows(self) -> None:
        self.assertIn("Anthropic Base URL", DASHBOARD_HTML)
        self.assertIn("Anthropic 端点", DASHBOARD_HTML)
        self.assertIn("g.anthropic_base_url", DASHBOARD_HTML)
        self.assertIn("g.anthropic_messages_endpoint", DASHBOARD_HTML)

    def test_snippet_tab_has_anthropic_branch(self) -> None:
        self.assertIn('["anthropic", "Anthropic / Claude"]', DASHBOARD_HTML)
        self.assertIn("ANTHROPIC_BASE_URL", DASHBOARD_HTML)
        self.assertIn("ANTHROPIC_API_KEY", DASHBOARD_HTML)
        self.assertIn("from anthropic import Anthropic", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
