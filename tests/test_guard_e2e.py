"""泄漏守卫全链路集成测试：worker + 真实 ConfigStore/StRotator + 生产 persist + rotate_account。

覆盖单元测试之外的「组装后契约」：
- 01:00 扫描：网关空转 + 账号 A 通用池积分变动 → 记入 guard.json 待轮换清单（落盘）。
- 02:00 定时：对 A 执行「登录 → 注销全部 Key → 新建 Key → 落盘」，文件/store/config/pool
  四者收敛到新 Key，凭据保留；guard.json 清空并记下日期。
- status() 序列化后不含明文 Key / Bearer token。

全程不访问真实商汤：登录与 KeyTransport 用 Fake，落盘用真实 ConfigStore（tempfile）。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from st_rotator.autorenew import KeyInfo, rotate_account
from st_rotator.cli import build_auto_renew_persist
from st_rotator.client import StRotator
from st_rotator.config import ConfigStore
from st_rotator.guard import GuardStore, LeakGuardWorker
from st_rotator.quota import TokenBundle

TOKEN = "TOK_guard_e2e_secret"
NEW_KEY = "sk-new-guard-001"


def _write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [
                    {
                        "name": "账号1",
                        "api_keys": ["sk-old-a", "sk-old-b"],
                        "user": "u1",
                        "phone": "138",
                        "password": "p1",
                    }
                ],
                "leak_guard": {"enabled": True, "interval_seconds": 600.0},
            }
        ),
        encoding="utf-8",
    )


def _epoch_at(year: int, mon: int, day: int, hour: int, minute: int) -> float:
    return time.mktime((year, mon, day, hour, minute, 0, 0, 0, -1))


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeLogin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, user: str, password: str) -> TokenBundle:
        self.calls.append((user, password))
        return TokenBundle(
            access_token=TOKEN, refresh_token="r", expires_in=3600, acquired_at=0.0
        )


class _FakeKeys:
    def __init__(self) -> None:
        self.ops: list[str] = []
        self.delete_calls: list[str] = []

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        self.ops.append("list")
        return [
            KeyInfo("k1", "old-a", "sk-old-a", "API_KEY_TYPE_TOKEN_PLAN", "t1"),
            KeyInfo("k2", "old-b", "sk-old-b", "API_KEY_TYPE_TOKEN_PLAN", "t2"),
        ]

    def create_key(self, access_token, *, displayname, key_type) -> KeyInfo:
        self.ops.append("create")
        return KeyInfo("k-new", displayname, NEW_KEY, key_type, "t")

    def delete_key(self, access_token, *, key_id) -> None:
        self.ops.append("delete")
        self.delete_calls.append(key_id)


class GuardE2ETest(unittest.TestCase):
    def test_flag_then_rotate_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path)
            store = ConfigStore.load(path)
            rotator = StRotator(store.config)
            guard_path = Path(tmp) / "guard.json"
            guard_store = GuardStore.load(guard_path)
            fake_login = _FakeLogin()
            fake_keys = _FakeKeys()
            persist = build_auto_renew_persist(store, rotator, lock=threading.Lock())

            def rotate(account):
                return rotate_account(
                    account,
                    login=fake_login,
                    keys=fake_keys,
                    persist=persist,
                    key_name="auto",
                    key_type="API_KEY_TYPE_TOKEN_PLAN",
                )

            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = LeakGuardWorker(
                accounts=lambda: rotator.config.accounts,
                token_usage_last=lambda: None,  # 网关空转
                credit_changes=lambda _since, _idle: ["账号1"],
                rotate=rotate,
                store=guard_store,
                clock=clock,
                interval=3600.0,
                window=600.0,
                rotate_hour=2,
                rotate_minute=0,
            )
            try:
                # 01:00 扫描 → 记录待轮换
                worker.run_once()
                self.assertEqual(guard_store.pending(), ["账号1"])
                self.assertEqual(fake_login.calls, [])
                # 02:00 → 轮换
                clock.t = _epoch_at(2026, 10, 9, 2, 0)
                worker.run_once()
            finally:
                rotator.close()

            # 平台调用顺序：list → delete×2 → create
            self.assertEqual(fake_keys.ops, ["list", "delete", "delete", "create"])
            self.assertEqual(fake_keys.delete_calls, ["k1", "k2"])
            self.assertEqual(fake_login.calls, [("u1", "p1")])
            # 磁盘收敛到新 Key，凭据保留
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["accounts"][0]["api_keys"], [NEW_KEY])
            self.assertEqual(disk["accounts"][0]["user"], "u1")
            self.assertEqual(disk["accounts"][0]["password"], "p1")
            # store / rotator / pool 一致
            self.assertEqual(store.config.accounts[0].api_keys, [NEW_KEY])
            self.assertEqual(rotator.config.accounts[0].api_keys, [NEW_KEY])
            self.assertIsNotNone(rotator.pool.find_key(NEW_KEY))
            self.assertIsNone(rotator.pool.find_key("sk-old-a"))
            # guard.json 清空 + 记日期
            self.assertEqual(guard_store.pending(), [])
            self.assertEqual(guard_store.last_rotate_date(), "2026-10-09")
            self.assertEqual(GuardStore.load(guard_path).last_rotate_date(), "2026-10-09")

    def test_status_never_leaks_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _write_config(path)
            store = ConfigStore.load(path)
            rotator = StRotator(store.config)
            guard_store = GuardStore.load(Path(tmp) / "guard.json")
            guard_store.add_pending(["账号1"])
            fake_keys = _FakeKeys()
            persist = build_auto_renew_persist(store, rotator, lock=threading.Lock())
            clock = _Clock(_epoch_at(2026, 10, 9, 2, 0))
            worker = LeakGuardWorker(
                accounts=lambda: rotator.config.accounts,
                token_usage_last=lambda: None,
                credit_changes=lambda _since, _idle: [],
                rotate=lambda account: rotate_account(
                    account,
                    login=_FakeLogin(),
                    keys=fake_keys,
                    persist=persist,
                ),
                store=guard_store,
                clock=clock,
            )
            try:
                worker.run_once()
                text = json.dumps(worker.status(), ensure_ascii=False)
            finally:
                rotator.close()
            self.assertNotIn("sk-", text)
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(NEW_KEY, text)


if __name__ == "__main__":
    unittest.main()
