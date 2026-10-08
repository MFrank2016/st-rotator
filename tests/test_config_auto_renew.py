import unittest

from st_rotator.config import AutoRenewConfig, Config
from st_rotator.errors import ConfigError


def _cfg(**extra) -> dict:
    data = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [{"name": "账号1", "api_keys": ["sk-abc12345"]}],
    }
    data.update(extra)
    return data


class AutoRenewParseTest(unittest.TestCase):
    def test_parses_auto_renew_and_fills_defaults(self):
        cfg = Config.from_dict(
            _cfg(auto_renew={"enabled": True, "interval_seconds": 7200})
        )
        self.assertEqual(cfg.auto_renew.enabled, True)
        self.assertEqual(cfg.auto_renew.interval_seconds, 7200.0)
        self.assertEqual(cfg.auto_renew.key_name, "auto")
        self.assertEqual(cfg.auto_renew.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_missing_auto_renew_uses_defaults(self):
        cfg = Config.from_dict(_cfg())
        self.assertIsInstance(cfg.auto_renew, AutoRenewConfig)
        self.assertEqual(cfg.auto_renew.enabled, False)
        self.assertEqual(cfg.auto_renew.interval_seconds, 180.0)
        self.assertEqual(cfg.auto_renew.cleanup_interval_seconds, 1800.0)
        self.assertEqual(cfg.auto_renew.key_name, "auto")
        self.assertEqual(cfg.auto_renew.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_from_dict_none_uses_defaults(self):
        cfg = AutoRenewConfig.from_dict(None)
        self.assertEqual(cfg, AutoRenewConfig())

    def test_unknown_subfield_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(auto_renew={"unknown_field": 1}))

    def test_invalid_key_type_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(key_type="API_KEY_TYPE_FOO")

    def test_invalid_key_name_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(key_name="")
        with self.assertRaises(ConfigError):
            AutoRenewConfig(key_name="x" * 65)
        with self.assertRaises(ConfigError):
            AutoRenewConfig(key_name="sk_key_123")  # 下划线不在允许字符集

    def test_interval_nonpositive_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(interval_seconds=0)
        with self.assertRaises(ConfigError):
            AutoRenewConfig(interval_seconds=-1.0)

    def test_interval_non_numeric_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(interval_seconds="3600")

    def test_cleanup_interval_invalid_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(cleanup_interval_seconds=0)
        with self.assertRaises(ConfigError):
            AutoRenewConfig(cleanup_interval_seconds=-1.0)
        with self.assertRaises(ConfigError):
            AutoRenewConfig(cleanup_interval_seconds="1800")

    def test_key_name_non_string_raises(self):
        with self.assertRaises(ConfigError):
            AutoRenewConfig(key_name=123)


class AutoRenewToDictTest(unittest.TestCase):
    def test_to_dict_roundtrip_includes_auto_renew(self):
        cfg = Config.from_dict(
            _cfg(
                auto_renew={
                    "enabled": True,
                    "interval_seconds": 120,
                    "cleanup_interval_seconds": 900,
                    "key_name": "手动",
                    "key_type": "API_KEY_TYPE_METERED",
                }
            )
        )
        d = cfg.to_dict()
        self.assertIn("auto_renew", d)
        self.assertEqual(d["auto_renew"]["enabled"], True)
        self.assertEqual(d["auto_renew"]["interval_seconds"], 120)
        self.assertEqual(d["auto_renew"]["cleanup_interval_seconds"], 900)
        self.assertEqual(d["auto_renew"]["key_name"], "手动")
        self.assertEqual(d["auto_renew"]["key_type"], "API_KEY_TYPE_METERED")


class AutoRenewEnabledStrictTest(unittest.TestCase):
    def test_enabled_accepts_bool_only(self):
        cfg = Config.from_dict(_cfg(auto_renew={"enabled": True}))
        self.assertEqual(cfg.auto_renew.enabled, True)
        cfg = Config.from_dict(_cfg(auto_renew={"enabled": False}))
        self.assertEqual(cfg.auto_renew.enabled, False)

    def test_enabled_rejects_non_bool(self):
        for bad in ("true", "false", 1, 0, "yes"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    Config.from_dict(_cfg(auto_renew={"enabled": bad}))


if __name__ == "__main__":
    unittest.main()
