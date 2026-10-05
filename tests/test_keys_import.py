import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from st_rotator import quota
from st_rotator.config import Config, ConfigStore
from st_rotator.errors import ConfigError
from st_rotator.ui import ConsoleState, parse_import_lines


class _FakePool:
    def __init__(self): self.keys = set()
    def find_key(self, key): return object() if key in self.keys else None


class _FakeRotator:
    def __init__(self, config):
        self.config = config
        self.pool = _FakePool()
        self.probed = []
    def probe_key(self, key):
        self.probed.append(key)
        return ("ok", "未校验") if key.startswith("sk-good") else ("invalid", "401")
    def add_key(self, key, *, account, max_concurrency=4, rpm_limit=None):
        self.pool.keys.add(key)
        class _Item:
            key_id = "id-" + key[-4:]
            masked = key[:6] + "..." + key[-4:]
            account_ = account
        it = _Item(); it.account = account
        return it


class _FakeTransport:
    def login(self, user, password):
        if password == "right":
            return quota.TokenBundle("jwt", "", 10800, 0.0)
        raise quota.QuotaAuthError("用户名或密码错误")
    def fetch_pools(self, token):
        return []
class _RejectingRotator(_FakeRotator):
    """内存池总是拒绝 add_key，用于验证 import_keys 的 store 回滚。"""

    def add_key(self, key, *, account, max_concurrency=4, rpm_limit=None):
        raise ConfigError("内存池拒绝")




def _store(tmp):
    return _store_with(tmp, [{"name": "账号1", "api_keys": ["sk-seed1234"]}])


def _store_with(tmp, accounts):
    p = Path(tmp) / "config.json"
    p.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "accounts": accounts}), encoding="utf-8")
    return ConfigStore.load(p)


class ParseImportTest(unittest.TestCase):
    def test_parses_forms_and_skips_blank(self):
        raw = "sk-aaa\n\n138--u1--p1--sk-bbb\nbad--seg"
        rows = parse_import_lines(raw)
        self.assertEqual([r[0] for r in rows], [1, 3, 4])
        self.assertEqual(rows[0][2], ("sk-aaa",))
        self.assertEqual(rows[1][2], ("138", "u1", "p1", "sk-bbb"))
        self.assertEqual(rows[2][2], ("bad", "seg"))


class ImportKeysTest(unittest.TestCase):
    def _state(self, tmp):
        store = _store(tmp)
        fake = _FakeRotator(store.config)
        svc = quota.QuotaService(store.config, transport=_FakeTransport())
        return ConsoleState(store=store, rotator=fake, quota=svc)  # type: ignore[arg-type]

    def test_format1_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("sk-good1\nsk-bad1", account=None, max_concurrency=4)
            body = resp.payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(body["summary"]["error"], 1)
            self.assertTrue(any("无效" in r["reason"] for r in body["results"] if r["status"] == "error"))

    def test_format2_login_and_key_and_credentials_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("138--u1--right--sk-good2", account=None, max_concurrency=4)
            self.assertEqual(resp.payload["summary"]["ok"], 1)
            acct = next(a for a in st.store.config.accounts if a.name == "u1")
            self.assertEqual(acct.user, "u1")
            self.assertEqual(acct.phone, "138")
            self.assertEqual(acct.password, "right")

    def test_format2_bad_login_rejected_no_password_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            resp = st.import_keys("138--u1--WRONGPW--sk-good3", account=None, max_concurrency=4)
            body = resp.payload
            self.assertEqual(body["summary"]["ok"], 0)
            self.assertNotIn("WRONGPW", json.dumps(body, ensure_ascii=False))

    def test_duplicate_within_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            body = st.import_keys("sk-good9\nsk-good9", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(body["summary"]["skipped"], 1)

    def test_bad_segment_count_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            body = st.import_keys("a--b--c", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["error"], 1)
            self.assertIn("格式", body["results"][0]["reason"])
    def test_format1_batch_shares_one_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            body = st.import_keys("sk-goodA\nsk-goodB", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 2)
            accounts = [r["account"] for r in body["added"]]
            self.assertEqual(len(set(accounts)), 1)
            created = [a for a in st.store.config.accounts if a.name not in ("账号1",)]
            self.assertEqual(len(created), 1)

    def test_format2_reuses_existing_account_by_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with(tmp, [
                {"name": "老账号", "api_keys": ["sk-seed1234"], "user": "u1", "password": "old"}
            ])
            fake = _FakeRotator(store.config)
            st = ConsoleState(store=store, rotator=fake, quota=quota.QuotaService(store.config, transport=_FakeTransport()))  # type: ignore[arg-type]
            body = st.import_keys("138--u1--right--sk-good7", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(body["added"][0]["account"], "老账号")
            self.assertFalse(any(a.name == "u1" for a in st.store.config.accounts))

    def test_format2_lazily_creates_service_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            fake = _FakeRotator(store.config)
            st = ConsoleState(store=store, rotator=fake, quota=None)  # type: ignore[arg-type]
            made = []

            def factory(config):
                svc = quota.QuotaService(config, transport=_FakeTransport())
                made.append(svc)
                return svc

            with mock.patch("st_rotator.ui.QuotaService", side_effect=factory):
                body = st.import_keys("138--u1--right--sk-good2", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 1)
            self.assertEqual(st.quota, made[0])

    def test_format2_without_service_reports_jwcrypto_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            fake = _FakeRotator(store.config)
            st = ConsoleState(store=store, rotator=fake, quota=None)  # type: ignore[arg-type]
            with mock.patch.dict("sys.modules", {"jwcrypto": None}):
                body = st.import_keys("138--u1--right--sk-good8", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["ok"], 0)
            reason = body["results"][0]["reason"]
            self.assertIn("jwcrypto", reason)
            self.assertNotIn("凭据无效", reason)

    def test_import_rolls_back_store_on_pool_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            fake = _RejectingRotator(store.config)
            st = ConsoleState(store=store, rotator=fake, quota=quota.QuotaService(store.config, transport=_FakeTransport()))  # type: ignore[arg-type]
            body = st.import_keys("sk-good5", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["error"], 1)
            self.assertNotIn("sk-good5", [k for a in st.store.config.accounts for k in a.api_keys])

    def test_format2_reused_account_credentials_not_overwritten_on_pool_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with(tmp, [
                {"name": "老账号", "api_keys": ["sk-seed1234"],
                 "user": "u1", "phone": "138", "password": "old"}
            ])
            fake = _RejectingRotator(store.config)
            st = ConsoleState(store=store, rotator=fake, quota=quota.QuotaService(store.config, transport=_FakeTransport()))  # type: ignore[arg-type]
            body = st.import_keys("139--u1--right--sk-good6", account=None, max_concurrency=4).payload
            self.assertEqual(body["summary"]["error"], 1)
            acct = next(a for a in st.store.config.accounts if a.name == "老账号")
            self.assertEqual(acct.password, "old")
            self.assertEqual(acct.phone, "138")




if __name__ == "__main__":
    unittest.main()
