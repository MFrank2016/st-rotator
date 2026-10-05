import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.config import AccountConfig, Config, ConfigStore
from st_rotator.errors import ConfigError


def _cfg(**acct):
    base = {"name": "账号1", "api_keys": ["sk-abc12345"], "rpm_limit": 2}
    base.update(acct)
    return {"base_url": "http://127.0.0.1:9/v1", "accounts": [base]}


class AccountCredentialsTest(unittest.TestCase):
    def test_defaults_empty(self):
        acct = Config.from_dict(_cfg()).accounts[0]
        self.assertEqual(acct.user, "")
        self.assertEqual(acct.phone, "")
        self.assertEqual(acct.password, "")

    def test_env_expansion(self):
        with mock.patch.dict(os.environ, {"SN_USER": "u1", "SN_PW": "p1"}):
            acct = Config.from_dict(
                _cfg(user="${SN_USER}", phone="13800000000", password="${SN_PW}")
            ).accounts[0]
        self.assertEqual(acct.user, "u1")
        self.assertEqual(acct.phone, "13800000000")
        self.assertEqual(acct.password, "p1")

    def test_env_missing_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict(_cfg(password="${ULW_MISSING_PW_XYZ}"))

    def test_to_dict_includes_user_phone_not_password(self):
        acct = Config.from_dict(_cfg(user="u1", phone="138", password="secret")).accounts[0]
        d = acct.to_dict()
        self.assertEqual(d["user"], "u1")
        self.assertEqual(d["phone"], "138")
        self.assertNotIn("password", d)

    def test_config_to_dict_includes_user_phone_not_password(self):
        cfg = Config.from_dict(_cfg(user="u1", phone="138", password="secret"))
        d = cfg.to_dict()
        acct = d["accounts"][0]
        self.assertEqual(acct["user"], "u1")
        self.assertEqual(acct["phone"], "138")
        self.assertNotIn("password", json.dumps(d, ensure_ascii=False))


class SetAccountCredentialsTest(unittest.TestCase):
    def _store(self, tmp: str) -> ConfigStore:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(_cfg()), encoding="utf-8")
        return ConfigStore.load(path)

    def test_sets_credentials_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set_account_credentials("账号1", user="u1", phone="138", password="p1")
            store.save()
            reloaded = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
            acct = reloaded["accounts"][0]
            self.assertEqual(acct["user"], "u1")
            self.assertEqual(acct["phone"], "138")
            self.assertEqual(acct["password"], "p1")
            self.assertEqual(store.config.accounts[0].user, "u1")

    def test_unknown_account_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ConfigError):
                store.set_account_credentials("不存在", user="u", phone="", password="p")


if __name__ == "__main__":
    unittest.main()
