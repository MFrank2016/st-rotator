import json
import os
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.config import Config, LeakGuardConfig
from st_rotator.errors import ConfigError

_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def _cfg(**extra) -> dict:
    data = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [{"name": "账号1", "api_keys": ["sk-abc12345"]}],
    }
    data.update(extra)
    return data


class LeakGuardDefaultsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = LeakGuardConfig()
        self.assertEqual(cfg.enabled, False)
        self.assertEqual(cfg.interval_seconds, 600.0)
        self.assertEqual(cfg.window_seconds, 600.0)
        self.assertEqual(cfg.rotate_hour, 2)
        self.assertEqual(cfg.rotate_minute, 0)
        self.assertEqual(cfg.key_name, "auto")
        self.assertEqual(cfg.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_from_dict_none_uses_defaults(self):
        self.assertEqual(LeakGuardConfig.from_dict(None), LeakGuardConfig())

    def test_config_missing_leak_guard_uses_defaults(self):
        cfg = Config.from_dict(_cfg())
        self.assertIsInstance(cfg.leak_guard, LeakGuardConfig)
        self.assertEqual(cfg.leak_guard.enabled, False)
        self.assertEqual(cfg.leak_guard.interval_seconds, 600.0)


class LeakGuardParseTest(unittest.TestCase):
    def test_parses_leak_guard_and_fills_defaults(self):
        cfg = Config.from_dict(
            _cfg(leak_guard={"enabled": True, "interval_seconds": 30, "rotate_hour": 5})
        )
        self.assertEqual(cfg.leak_guard.enabled, True)
        self.assertEqual(cfg.leak_guard.interval_seconds, 30.0)
        self.assertEqual(cfg.leak_guard.rotate_hour, 5)
        self.assertEqual(cfg.leak_guard.window_seconds, 600.0)
        self.assertEqual(cfg.leak_guard.key_name, "auto")
        self.assertEqual(cfg.leak_guard.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_unknown_subfield_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(leak_guard={"unknown_field": 1}))
        with self.assertRaises(ConfigError):
            LeakGuardConfig.from_dict({"unknown_field": 1})


class LeakGuardValidationTest(unittest.TestCase):
    def test_enabled_rejects_non_bool(self):
        for bad in ("true", "false", 1, 0, "yes"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    LeakGuardConfig(enabled=bad)

    def test_interval_must_be_positive(self):
        for bad in (0, -1.0, "600"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    LeakGuardConfig(interval_seconds=bad)

    def test_window_must_be_positive(self):
        for bad in (0, -1.0, "600"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    LeakGuardConfig(window_seconds=bad)

    def test_rotate_hour_range(self):
        LeakGuardConfig(rotate_hour=0)
        LeakGuardConfig(rotate_hour=23)
        for bad in (24, -1, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    LeakGuardConfig(rotate_hour=bad)

    def test_rotate_minute_range(self):
        LeakGuardConfig(rotate_minute=0)
        LeakGuardConfig(rotate_minute=59)
        for bad in (60, -1, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    LeakGuardConfig(rotate_minute=bad)

    def test_key_name_validation(self):
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_name="")
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_name="   ")
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_name="x" * 65)
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_name="a b")  # 空格不在允许字符集
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_name=123)

    def test_key_name_stripped(self):
        self.assertEqual(LeakGuardConfig(key_name="  auto  ").key_name, "auto")

    def test_key_type_validation(self):
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_type="API_KEY_TYPE_FOO")
        with self.assertRaises(ConfigError):
            LeakGuardConfig(key_type=123)


class LeakGuardToDictTest(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        cfg = LeakGuardConfig(
            enabled=True,
            interval_seconds=30,
            window_seconds=120,
            rotate_hour=3,
            rotate_minute=15,
            key_name="手动",
            key_type="API_KEY_TYPE_METERED",
        )
        self.assertEqual(
            cfg.to_dict(),
            {
                "enabled": True,
                "interval_seconds": 30,
                "window_seconds": 120,
                "rotate_hour": 3,
                "rotate_minute": 15,
                "key_name": "手动",
                "key_type": "API_KEY_TYPE_METERED",
            },
        )

    def test_config_to_dict_includes_leak_guard(self):
        cfg = Config.from_dict(_cfg(leak_guard={"enabled": True, "rotate_hour": 5}))
        d = cfg.to_dict()
        self.assertIn("leak_guard", d)
        self.assertEqual(d["leak_guard"]["enabled"], True)
        self.assertEqual(d["leak_guard"]["rotate_hour"], 5)
        self.assertEqual(d["leak_guard"]["key_name"], "auto")


class ExampleConfigTest(unittest.TestCase):
    def test_example_file_parses(self):
        data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn("leak_guard", data)
        with mock.patch.dict(
            os.environ,
            {
                "SENSENOVA_KEY_1": "sk-1",
                "SENSENOVA_KEY_2": "sk-2",
                "SENSENOVA_KEY_3": "sk-3",
            },
        ):
            cfg = Config.from_dict(data)
        self.assertEqual(cfg.leak_guard.enabled, False)
        self.assertEqual(cfg.leak_guard, LeakGuardConfig())


if __name__ == "__main__":
    unittest.main()
