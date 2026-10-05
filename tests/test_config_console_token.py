"""Config.console_token 字段的行为测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from st_rotator.config import Config
from st_rotator.errors import ConfigError


def _minimal_config(**overrides: object) -> dict[str, object]:
    """构造一份最小可用的配置 dict，允许覆盖顶层字段。"""
    config: dict[str, object] = {
        "base_url": "http://127.0.0.1:8080/v1",
        "accounts": [
            {
                "name": "a",
                "api_keys": ["k"],
                "rpm_limit": 2,
                "max_concurrency": 4,
                "weight": 1,
            }
        ],
    }
    config.update(overrides)
    return config


class ConfigConsoleTokenTest(unittest.TestCase):
    def test_field_defaults_empty(self) -> None:
        cfg = _minimal_config()
        self.assertEqual(Config.from_dict(cfg).console_token, "")

    def test_env_expansion(self) -> None:
        cfg = _minimal_config(console_token="${CONSOLE_TOKEN}")
        with patch.dict(os.environ, {"CONSOLE_TOKEN": "sekret"}):
            self.assertEqual(Config.from_dict(cfg).console_token, "sekret")

    def test_env_missing_raises(self) -> None:
        cfg = _minimal_config(console_token="${ULW_MISSING_TOKEN_VAR_XYZ}")
        with self.assertRaises(ConfigError):
            Config.from_dict(cfg)

    def test_unknown_field_still_rejected(self) -> None:
        cfg = _minimal_config(bogus=1)
        with self.assertRaises(ConfigError):
            Config.from_dict(cfg)

    def test_to_dict_omits_token(self) -> None:
        cfg = _minimal_config(console_token="sekret")
        self.assertNotIn("console_token", Config.from_dict(cfg).to_dict())


if __name__ == "__main__":
    unittest.main()
