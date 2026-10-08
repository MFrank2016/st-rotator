"""auto_renew 全链路集成测试：worker + 真实 ConfigStore/StRotator + 生产 persist 一起运转。

覆盖 T5/T6 单元测试之外的「组装后契约」：
- 失效探测 → 登录 → 平台 list/create/delete → 生产 persist 落池落盘，最终
  文件 / store.config / rotator.config / pool 四者收敛到同一把新 Key，凭据保留。
- 密码错误路径：worker 标记 password_error 且不触发任何落盘/平台操作。
- account_status 与快照面绝不包含明文 Key / Bearer token。

测试全程不访问真实商汤：登录与 KeyTransport 用 Fake，落盘用真实 ConfigStore（tempfile）。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.autorenew import AutoRenewWorker, KeyInfo
from st_rotator.cli import build_auto_renew_persist
from st_rotator.client import StRotator
from st_rotator.config import ConfigStore
from st_rotator.quota import QuotaAuthError, TokenBundle

TOKEN = "TOK_e2e_secret_123"
NEW_KEY = "sk-new-plain-001"


def _write_config(path: Path, *, api_keys: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [
                    {
                        "name": "账号1",
                        "api_keys": api_keys,
                        "user": "u1",
                        "phone": "138",
                        "password": "p1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class _FakeLogin:
    """登录 Fake：可配置抛错（QuotaAuthError 模拟密码失效）。"""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.exc = exc

    def __call__(self, user: str, password: str) -> TokenBundle:
        self.calls.append((user, password))
        if self.exc is not None:
            raise self.exc
        return TokenBundle(
            access_token=TOKEN, refresh_token="r", expires_in=3600, acquired_at=0.0
        )


class _FakeKeys:
    """KeyTransport Fake：记录调用，返回平台侧既有 Key 清单 / 新建 Key。"""

    def __init__(self, *, remote: list[KeyInfo]) -> None:
        self.remote = remote
        self.create_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        return list(self.remote)

    def create_key(self, access_token, *, displayname, key_type) -> KeyInfo:
        self.create_calls.append(
            {"token": access_token, "displayname": displayname, "key_type": key_type}
        )
        return KeyInfo(
            id="k-new",
            displayname=displayname,
            api_key=NEW_KEY,
            key_type=key_type,
            create_time="t",
        )

    def delete_key(self, access_token, *, key_id) -> None:
        self.delete_calls.append({"token": access_token, "key_id": key_id})


def _probe_invalid(key: str):
    return ("invalid", "401 unauthorized")


class AutoRenewE2ETest(unittest.TestCase):
    def _build(self, tmp: str, *, login_exc: Exception | None = None) -> tuple:
        path = Path(tmp) / "config.json"
        _write_config(path, api_keys=["sk-old-a-001", "sk-old-b-002"])
        store = ConfigStore.load(path)
        rotator = StRotator(store.config)
        remote = [
            KeyInfo("k1", "old-a", "sk-old-a-001", "API_KEY_TYPE_TOKEN_PLAN", "t1"),
            KeyInfo("k2", "old-b", "sk-old-b-002", "API_KEY_TYPE_TOKEN_PLAN", "t2"),
        ]
        fake_login = _FakeLogin(exc=login_exc)
        fake_keys = _FakeKeys(remote=remote)
        persist = build_auto_renew_persist(store, rotator, lock=threading.Lock())
        worker = AutoRenewWorker(
            accounts=lambda: rotator.config.accounts,
            probe=_probe_invalid,
            login=fake_login,
            keys=fake_keys,
            persist=persist,
            key_name="auto",
            key_type="API_KEY_TYPE_TOKEN_PLAN",
            interval=3600.0,
            log=None,
        )
        return store, rotator, fake_login, fake_keys, worker, path

    def test_rotation_converges_file_store_config_pool(self):
        """失效探测 → 登录 → 平台 list/create/delete-all → 生产 persist，四者收敛到新 Key。"""
        with tempfile.TemporaryDirectory() as tmp:
            store, rotator, fake_login, fake_keys, worker, path = self._build(tmp)
            worker.run_once()

            # 平台调用
            self.assertEqual(fake_login.calls, [("u1", "p1")])
            self.assertEqual(
                fake_keys.create_calls,
                [
                    {
                        "token": TOKEN,
                        "displayname": "auto",
                        "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                    }
                ],
            )
            self.assertEqual(
                [c["key_id"] for c in fake_keys.delete_calls], ["k1", "k2"]
            )
            # 磁盘
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["accounts"][0]["api_keys"], [NEW_KEY])
            self.assertEqual(disk["accounts"][0]["user"], "u1")
            self.assertEqual(disk["accounts"][0]["password"], "p1")
            # store.config / rotator.config / pool
            self.assertEqual(store.config.accounts[0].api_keys, [NEW_KEY])
            self.assertEqual(rotator.config.accounts[0].api_keys, [NEW_KEY])
            self.assertIsNotNone(rotator.pool.find_key(NEW_KEY))
            self.assertIsNone(rotator.pool.find_key("sk-old-a-001"))
            self.assertIsNone(rotator.pool.find_key("sk-old-b-002"))
            # 状态
            self.assertEqual(worker.account_status()["账号1"]["status"], "ok")

    def test_password_error_flags_and_does_not_touch_state(self):
        """登录抛 QuotaAuthError → password_error；不调用平台、不落盘、池不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            store, rotator, fake_login, fake_keys, worker, path = self._build(
                tmp, login_exc=QuotaAuthError("用户名或密码错误")
            )
            before_disk = path.read_text(encoding="utf-8")
            worker.run_once()

            self.assertEqual(
                worker.account_status()["账号1"]["status"], "password_error"
            )
            self.assertEqual(
                worker.account_status()["账号1"]["message"], "用户名或密码错误"
            )
            self.assertEqual(fake_login.calls, [("u1", "p1")])
            self.assertEqual(fake_keys.create_calls, [])
            self.assertEqual(fake_keys.delete_calls, [])
            # 磁盘与池原样
            self.assertEqual(path.read_text(encoding="utf-8"), before_disk)
            self.assertIsNotNone(rotator.pool.find_key("sk-old-a-001"))
            self.assertIsNone(rotator.pool.find_key(NEW_KEY))

    def test_account_status_never_leaks_secrets(self):
        """account_status 序列化后不含明文 Key / Bearer token。"""
        with tempfile.TemporaryDirectory() as tmp:
            store, rotator, fake_login, fake_keys, worker, path = self._build(
                tmp, login_exc=QuotaAuthError("密码错误")
            )
            worker.run_once()
            text = json.dumps(worker.account_status(), ensure_ascii=False)
            self.assertNotIn("sk-", text)
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("Bearer", text)
            self.assertNotIn(NEW_KEY, text)

    def test_healthy_account_probe_all_ok_keeps_status_ok(self):
        """全部 Key 有效 → 不登录、不轮换，状态 ok（探活覆盖每一把）。"""
        probed: list[str] = []

        def probe(key: str):
            probed.append(key)
            return ("ok", "ok")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path, api_keys=["sk-good-a-001", "sk-good-b-002"])
            store = ConfigStore.load(path)
            rotator = StRotator(store.config)
            fake_login = _FakeLogin()
            fake_keys = _FakeKeys(remote=[])
            worker = AutoRenewWorker(
                accounts=lambda: rotator.config.accounts,
                probe=probe,
                login=fake_login,
                keys=fake_keys,
                persist=build_auto_renew_persist(store, rotator, lock=threading.Lock()),
                key_name="auto",
                key_type="API_KEY_TYPE_TOKEN_PLAN",
                interval=3600.0,
                log=None,
            )
            worker.run_once()

            self.assertEqual(sorted(probed), ["sk-good-a-001", "sk-good-b-002"])
            self.assertEqual(fake_login.calls, [])
            self.assertEqual(fake_keys.create_calls, [])
            self.assertEqual(worker.account_status()["账号1"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
