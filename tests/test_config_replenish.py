import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.config import Config, ConfigStore, ReplenishConfig
from st_rotator.errors import ConfigError


def _cfg(**extra) -> dict:
    data = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [{"name": "账号1", "api_keys": ["sk-abc12345"]}],
    }
    data.update(extra)
    return data


class ReplenishParseTest(unittest.TestCase):
    def test_missing_replenish_uses_defaults(self):
        cfg = Config.from_dict(_cfg())
        self.assertIsInstance(cfg.replenish, ReplenishConfig)
        self.assertEqual(cfg.replenish.enabled, False)
        self.assertEqual(cfg.replenish.target_count, 0)
        self.assertEqual(cfg.replenish.interval_seconds, 3600.0)
        self.assertEqual(cfg.replenish.sms_token, "")
        self.assertEqual(cfg.replenish.keyword, "商汤")
        self.assertEqual(cfg.replenish.daily_spend_cap, 5.0)
        self.assertEqual(cfg.replenish.sms_poll_interval, 5.0)
        self.assertEqual(cfg.replenish.sms_poll_timeout, 60.0)
        self.assertEqual(cfg.replenish.key_name, "auto")
        self.assertEqual(cfg.replenish.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_parses_replenish_and_fills_defaults(self):
        cfg = Config.from_dict(
            _cfg(replenish={"enabled": True, "target_count": 3, "keyword": "商汤2"})
        )
        self.assertEqual(cfg.replenish.enabled, True)
        self.assertEqual(cfg.replenish.target_count, 3)
        self.assertEqual(cfg.replenish.keyword, "商汤2")
        self.assertEqual(cfg.replenish.interval_seconds, 3600.0)
        self.assertEqual(cfg.replenish.sms_token, "")
        self.assertEqual(cfg.replenish.daily_spend_cap, 5.0)
        self.assertEqual(cfg.replenish.sms_poll_interval, 5.0)
        self.assertEqual(cfg.replenish.sms_poll_timeout, 60.0)
        self.assertEqual(cfg.replenish.key_name, "auto")
        self.assertEqual(cfg.replenish.key_type, "API_KEY_TYPE_TOKEN_PLAN")

    def test_from_dict_none_uses_defaults(self):
        cfg = ReplenishConfig.from_dict(None)
        self.assertEqual(cfg, ReplenishConfig())

    def test_unknown_subfield_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(replenish={"unknown_field": 1}))
        with self.assertRaises(ConfigError):
            ReplenishConfig.from_dict({"unknown_field": 1})

    def test_invalid_key_type_raises(self):
        with self.assertRaises(ConfigError):
            ReplenishConfig(key_type="API_KEY_TYPE_FOO")

    def test_invalid_key_name_raises(self):
        with self.assertRaises(ConfigError):
            ReplenishConfig(key_name="")
        with self.assertRaises(ConfigError):
            ReplenishConfig(key_name="x" * 65)
        with self.assertRaises(ConfigError):
            ReplenishConfig(key_name="sk_key_123")  # 下划线不在允许字符集

    def test_target_count_validation(self):
        with self.assertRaises(ConfigError):
            ReplenishConfig(target_count=-1)
        with self.assertRaises(ConfigError):
            ReplenishConfig(target_count=1.5)

    def test_timing_must_be_positive(self):
        for kwargs in (
            {"interval_seconds": 0},
            {"interval_seconds": -1.0},
            {"sms_poll_interval": 0},
            {"sms_poll_timeout": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    ReplenishConfig(**kwargs)

    def test_spend_cap_nonnegative(self):
        ReplenishConfig(daily_spend_cap=0)  # 0 = 不限制
        with self.assertRaises(ConfigError):
            ReplenishConfig(daily_spend_cap=-1)

    def test_sms_token_and_keyword_env_expansion(self):
        with mock.patch.dict(
            os.environ, {"EJ_TOKEN": "tok-1", "EJ_KEYWORD": "商汤-prod"}
        ):
            cfg = Config.from_dict(
                _cfg(replenish={"sms_token": "${EJ_TOKEN}", "keyword": "${EJ_KEYWORD}"})
            )
        self.assertEqual(cfg.replenish.sms_token, "tok-1")
        self.assertEqual(cfg.replenish.keyword, "商汤-prod")


class ReplenishToDictTest(unittest.TestCase):
    def test_to_dict_omits_sms_token(self):
        with mock.patch.dict(os.environ, {"EJ_TOKEN": "tok-secret"}):
            cfg = Config.from_dict(
                _cfg(
                    replenish={
                        "enabled": True,
                        "sms_token": "${EJ_TOKEN}",
                        "keyword": "商汤",
                    }
                )
            )
        d = cfg.replenish.to_dict()
        self.assertNotIn("sms_token", d)
        self.assertEqual(d["keyword"], "商汤")
        self.assertEqual(d["enabled"], True)

        full = cfg.to_dict()
        self.assertIn("replenish", full)
        self.assertNotIn("sms_token", full["replenish"])
        self.assertNotIn("tok-secret", json.dumps(full, ensure_ascii=False))


class AddAccountTest(unittest.TestCase):
    def _store(self, tmp: str) -> ConfigStore:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(_cfg()), encoding="utf-8")
        return ConfigStore.load(path)

    def test_add_account_appends_with_credentials_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            entry = store.add_account(
                "账号2",
                user="u2",
                phone="13900000000",
                password="p2",
                api_keys=["sk-new"],
            )
            self.assertEqual(entry["name"], "账号2")
            self.assertEqual(entry["api_keys"], ["sk-new"])
            self.assertEqual(entry["user"], "u2")
            self.assertEqual(entry["phone"], "13900000000")
            self.assertEqual(entry["password"], "p2")
            self.assertEqual(entry["rpm_limit"], None)
            self.assertEqual(entry["max_concurrency"], 4)
            self.assertEqual(entry["weight"], 1.0)
            self.assertEqual(len(store.config.accounts), 2)  # reload 已生效
            self.assertEqual(store.config.accounts[1].user, "u2")
            self.assertEqual(store.config.accounts[1].api_keys, ["sk-new"])

    def test_add_account_duplicate_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ConfigError):
                store.add_account(
                    "账号1", user="u", phone="", password="p", api_keys=["sk-new"]
                )

    def test_add_account_empty_name_or_keys_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ConfigError):
                store.add_account(
                    "", user="u", phone="", password="p", api_keys=["sk-new"]
                )
            with self.assertRaises(ConfigError):
                store.add_account(
                    "账号2", user="u", phone="", password="p", api_keys=[]
                )


def test_add_account_survives_save_and_reload(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg()), encoding="utf-8")
    store = ConfigStore.load(path)
    store.add_account(
        "账号2", user="u2", phone="13900000000", password="p2", api_keys=["sk-new"]
    )
    store.save()

    reloaded = ConfigStore.load(path)
    assert reloaded.account_names() == ["账号1", "账号2"]
    acct = reloaded.config.accounts[1]
    assert acct.name == "账号2"
    assert acct.user == "u2"
    assert acct.phone == "13900000000"
    assert acct.password == "p2"
    assert acct.api_keys == ["sk-new"]
    assert acct.rpm_limit is None
    assert acct.max_concurrency == 4
    assert acct.weight == 1.0

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["accounts"][1]["name"] == "账号2"
    assert raw["accounts"][1]["password"] == "p2"
    assert raw["accounts"][1]["api_keys"] == ["sk-new"]


if __name__ == "__main__":
    unittest.main()
