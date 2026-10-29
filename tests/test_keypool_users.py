"""用户维度：上下文登记表与 Key 归属记录（不改变调度行为，只做展示数据）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from st_rotator.client import StRotator
from st_rotator.config import AccountConfig, ConfigStore
from st_rotator.keypool import (
    KeyPool,
    current_user,
    trying_users,
    user_context,
)
from st_rotator.ui import ConsoleState


def _pool(max_concurrency: int = 4) -> KeyPool:
    return KeyPool(
        [
            AccountConfig(
                name="a",
                api_keys=["sk-a1", "sk-a2"],
                max_concurrency=max_concurrency,
            )
        ]
    )


class UserContextTest(unittest.TestCase):
    def test_enter_and_exit_register_and_clear(self) -> None:
        self.assertEqual(current_user(), "")
        self.assertEqual(trying_users(), [])
        with user_context("alice"):
            self.assertEqual(current_user(), "alice")
            self.assertEqual(trying_users(), ["alice"])
        self.assertEqual(current_user(), "")
        self.assertEqual(trying_users(), [])

    def test_exception_still_clears(self) -> None:
        with self.assertRaises(RuntimeError):
            with user_context("bob"):
                raise RuntimeError("boom")
        self.assertEqual(current_user(), "")
        self.assertEqual(trying_users(), [])

    def test_empty_label_is_not_registered(self) -> None:
        with user_context(""):
            self.assertEqual(current_user(), "")
            self.assertEqual(trying_users(), [])
        self.assertEqual(trying_users(), [])

    def test_trying_users_follows_first_entry_order(self) -> None:
        with user_context("alice"):
            with user_context("bob"):
                self.assertEqual(current_user(), "bob")
                self.assertEqual(trying_users(), ["alice", "bob"])
            self.assertEqual(current_user(), "alice")
        self.assertEqual(trying_users(), [])


class InflightUsersTest(unittest.TestCase):
    def test_reserve_and_release_keep_lists_aligned(self) -> None:
        pool = _pool()
        with user_context("alice"):
            key = pool.acquire()
            self.assertEqual(key.inflight, 1)
            self.assertEqual(key.inflight_users, ["alice"])
            self.assertEqual(key.last_user, "alice")
            item = key.to_dict(0.0, include_users=True)
            self.assertEqual(item["inflight_users"], ["alice"])
            self.assertEqual(item["last_user"], "alice")
            pool.release(key)
        self.assertEqual(key.inflight, 0)
        self.assertEqual(key.inflight_users, [])
        # last_user 保留最近一次非空用户
        self.assertEqual(key.last_user, "alice")

    def test_empty_label_still_appends_for_alignment(self) -> None:
        pool = _pool()
        key = pool.acquire()  # 无上下文
        self.assertEqual(key.inflight, 1)
        self.assertEqual(key.inflight_users, [""])
        pool.release(key)
        self.assertEqual(key.inflight_users, [])

    def test_concurrent_acquire_and_release_stay_aligned(self) -> None:
        pool = _pool(max_concurrency=8)
        barrier = threading.Barrier(6)
        errors: list[str] = []

        def worker(name: str) -> None:
            try:
                with user_context(name):
                    key = pool.acquire()
                    try:
                        barrier.wait(timeout=5)
                    finally:
                        pool.release(key)
            except Exception as exc:  # pragma: no cover - 失败信息在断言里看
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(f"u{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        for key in pool.keys:
            self.assertEqual(key.inflight, 0)
            self.assertEqual(key.inflight_users, [])
            self.assertEqual(len(key.inflight_users), key.inflight)
        self.assertEqual(trying_users(), [])

    def test_release_with_empty_or_short_list_does_not_raise(self) -> None:
        pool = _pool()
        key = pool.find_key("sk-a1")
        pool.release(key)  # inflight 已经是 0，不该抛
        key.inflight = 1  # 列表异常为空时也不能抛
        key.inflight_users.clear()
        pool.release(key)
        self.assertEqual(key.inflight, 0)
        self.assertEqual(key.inflight_users, [])

    def test_empty_label_release_removes_own_entry_not_others(self) -> None:
        pool = _pool()
        key = pool.find_key("sk-a1")
        key.inflight = 2
        key.inflight_users[:] = ["alice", ""]
        pool.release(key)  # 无用户上下文归还
        self.assertEqual(key.inflight, 1)
        self.assertEqual(key.inflight_users, ["alice"])

    def test_summary_counts_users_but_never_names(self) -> None:
        pool = _pool()
        with user_context("alice"):
            key = pool.acquire()
            summary = pool.summary()
        pool.release(key)
        self.assertEqual(summary["active_users"], 1)
        self.assertEqual(summary["trying_users"], 1)
        self.assertNotIn("alice", str(summary))
        self.assertEqual(
            set(summary),
            {
                "total",
                "healthy",
                "cooldown",
                "invalid",
                "inflight",
                "active_users",
                "trying_users",
            },
        )


class UserFieldVisibilityTest(unittest.TestCase):
    """用户标签只能出现在需鉴权的控制台，公开的 /stats 里必须没有。"""

    def _console(self, tmp: str) -> ConsoleState:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:9/v1",
                    "accounts": [{"name": "a", "api_keys": ["sk-a1"]}],
                }
            ),
            encoding="utf-8",
        )
        store = ConfigStore.load(path)
        rotator = StRotator(store.config)
        return ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]

    def test_pool_snapshot_hides_user_fields_by_default(self) -> None:
        pool = _pool()
        with user_context("alice"):
            key = pool.acquire()
            item = pool.snapshot()[0]
        pool.release(key)
        self.assertNotIn("inflight_users", item)
        self.assertNotIn("last_user", item)

    def test_console_snapshot_has_users_but_stats_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            with user_context("alice"):
                key = console.rotator.pool.acquire()  # type: ignore[union-attr]
                try:
                    state = console.snapshot()
                    stats = console.rotator.status()  # type: ignore[union-attr]
                finally:
                    console.rotator.pool.release(key)  # type: ignore[union-attr]
        item = state["keys"][0]
        self.assertEqual(item["inflight_users"], ["alice"])
        self.assertEqual(item["last_user"], "alice")
        self.assertEqual(state["trying_users"], ["alice"])
        self.assertNotIn("inflight_users", stats["keys"][0])
        self.assertNotIn("last_user", stats["keys"][0])
        self.assertNotIn("alice", json.dumps(stats, ensure_ascii=False))


class NewAccountDefaultsTest(unittest.TestCase):
    """新建账号 / Key 的默认并发上限与 AccountConfig 的默认值（6）保持一致。"""

    def test_add_account_writes_default_max_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {"base_url": "http://127.0.0.1:9/v1",
                     "accounts": [{"name": "旧账号", "api_keys": ["sk-old"]}]}
                ),
                encoding="utf-8",
            )
            store = ConfigStore.load(path)
            entry = store.add_account(
                "新账号", user="u", phone="13800000000", password="p",
                api_keys=["sk-new"],
            )
            self.assertEqual(entry["max_concurrency"], 6)
            self.assertEqual(store.config.accounts[1].max_concurrency, 6)

    def test_add_key_default_max_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {"base_url": "http://127.0.0.1:9/v1",
                     "accounts": [{"name": "旧账号", "api_keys": ["sk-old"]}]}
                ),
                encoding="utf-8",
            )
            store = ConfigStore.load(path)
            entry = store.add_key("sk-new", account="新账号")
            self.assertEqual(entry["max_concurrency"], 6)
            store.reload()  # add_key 只落盘，内存配置要显式重载
            self.assertEqual(store.config.accounts[1].max_concurrency, 6)


if __name__ == "__main__":
    unittest.main()
