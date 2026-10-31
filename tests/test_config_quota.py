"""quota 配置节：缺省启用、布尔校验、落盘后 reload 保持。"""

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.config import Config, ConfigStore, QuotaConfig
from st_rotator.errors import ConfigError


def _cfg(**extra) -> dict:
    data = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [{"name": "账号1", "api_keys": ["sk-abc12345"]}],
    }
    data.update(extra)
    return data


class QuotaDefaultsTest(unittest.TestCase):
    def test_default_enabled(self):
        self.assertEqual(QuotaConfig().enabled, True)

    def test_config_missing_quota_enabled(self):
        cfg = Config.from_dict(_cfg())
        self.assertIsInstance(cfg.quota, QuotaConfig)
        self.assertIs(cfg.quota.enabled, True)


class QuotaParseTest(unittest.TestCase):
    def test_parses_enabled_false(self):
        cfg = Config.from_dict(_cfg(quota={"enabled": False}))
        self.assertIs(cfg.quota.enabled, False)

    def test_enabled_rejects_non_bool(self):
        for bad in ("no", "true", 1, 0):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    Config.from_dict(_cfg(quota={"enabled": bad}))
        with self.assertRaises(ConfigError):
            QuotaConfig(enabled="no")

    def test_unknown_subfield_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(quota={"unknown_field": 1}))


class QuotaToDictTest(unittest.TestCase):
    def test_config_to_dict_includes_quota(self):
        cfg = Config.from_dict(_cfg(quota={"enabled": False}))
        d = cfg.to_dict()
        self.assertEqual(d["quota"], {"enabled": False})


class QuotaStoreReloadTest(unittest.TestCase):
    def test_save_then_reload_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(_cfg()), encoding="utf-8")
            store = ConfigStore.load(path)
            self.assertIs(store.config.quota.enabled, True)
            store.set_section("quota", {"enabled": False})
            store.save()
            reloaded = ConfigStore.load(path)
            self.assertIs(reloaded.config.quota.enabled, False)


if __name__ == "__main__":
    unittest.main()
