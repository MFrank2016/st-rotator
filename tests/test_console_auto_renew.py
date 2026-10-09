"""ConsoleState.account_status / CLI build_auto_renew 装配层的单元测试。

全部使用 Fake 与真实 ConfigStore（tempfile 落盘真实 JSON），不访问真实商汤：

- snapshot 测试：Fake rotator（probe 不发网络）+ 注入带 account_status() 的假 worker。
- build_auto_renew 门控测试：Fake rotator + 真实 ConfigStore，probe 全部返回 ok，
  不会触发登录 / 平台调用。
- persist 端到端：真实 StRotator + 真实 ConfigStore，只调 persist 闭包（不触发 probe），
  逐一断言磁盘 / store / rotator 内存 / 池四者一致，且 user/password 原样保留。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

from st_rotator.client import StRotator
from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState


def _write_config(
    tmp: str, *, enabled: bool = False, credentials: bool = False
) -> Path:
    """写一份真实配置：可选 auto_renew.enabled 与 user/password 凭据。"""
    accounts = [{"name": "账号1", "api_keys": ["sk-old1", "sk-old2"]}]
    if credentials:
        accounts[0]["user"] = "u1"
        accounts[0]["password"] = "p1"
    path = Path(tmp) / "config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": accounts,
                "auto_renew": {"enabled": enabled, "interval_seconds": 3600.0},
            }
        ),
        encoding="utf-8",
    )
    return path


def _load_store(tmp: str, **kw: bool) -> ConfigStore:
    return ConfigStore.load(_write_config(tmp, **kw))


class _FakeWorker:
    """带 account_status() 的假 worker，用于 snapshot 注入。"""

    def __init__(self, status: dict[str, dict[str, str]]) -> None:
        self._status = status

    def account_status(self) -> dict[str, dict[str, str]]:
        return self._status


class _FakePool:
    def summary(self) -> dict[str, int]:
        return {"total": 0, "healthy": 0, "cooldown": 0, "invalid": 0, "inflight": 0}

    def snapshot(self) -> list[dict[str, int]]:
        return []


class _FakeLimiter:
    def stats(self) -> dict[str, float | str | int]:
        return {
            "mode": "off",
            "rate": 0.0,
            "min_rate": 0.0,
            "max_rate": 0.0,
            "penalties": 0,
            "raises": 0,
        }


class _FakeRotator:
    """snapshot / build_auto_renew 用的假 rotator：probe 记录调用、返回 ok，不发网络。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool = _FakePool()
        self.limiter = _FakeLimiter()
        self.upstream_attempts = 0
        self.probed: list[str] = []

    def probe_key(self, key: str) -> tuple[str, str]:
        self.probed.append(key)
        return ("ok", "未校验")

    def available_models(
        self, *, refresh: bool = False, ttl: float = 300.0
    ) -> dict[str, object]:
        return {"models": []}


class SnapshotAccountStatusTest(unittest.TestCase):
    """RED：snapshot() 当前没有 account_status 键；GREEN：无 worker 返回 {}，有 worker 返回其 account_status()。"""

    def test_no_worker_returns_empty_account_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            self.assertEqual(console.snapshot()["account_status"], {})

    def test_snapshot_includes_worker_account_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            worker = _FakeWorker({"账号1": {"status": "ok", "message": ""}})
            console = ConsoleState(store=store, rotator=rotator, auto_renew=worker)  # type: ignore[arg-type]
            self.assertEqual(
                console.snapshot()["account_status"],
                {"账号1": {"status": "ok", "message": ""}},
            )


class BuildAutoRenewTest(unittest.TestCase):
    """RED：build_auto_renew 尚不存在（ImportError）；GREEN：按 enabled + 凭据 门控返回 None / 已 start 的 worker。"""

    def _sink(self) -> tuple[list[str], Callable[[str], None]]:
        lines: list[str] = []

        def sink(message: str) -> None:
            lines.append(message)

        return lines, sink

    def test_disabled_returns_none(self):
        from st_rotator.cli import build_auto_renew

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, credentials=True)
            rotator = _FakeRotator(store.config)
            _lines, sink = self._sink()
            worker = build_auto_renew(  # type: ignore[arg-type]
                store.config, store, rotator, lock=threading.Lock(), sink=sink
            )
            self.assertIsNone(worker)

    def test_enabled_without_credentials_returns_none(self):
        from st_rotator.cli import build_auto_renew

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, enabled=True)
            rotator = _FakeRotator(store.config)
            _lines, sink = self._sink()
            worker = build_auto_renew(  # type: ignore[arg-type]
                store.config, store, rotator, lock=threading.Lock(), sink=sink
            )
            self.assertIsNone(worker)

    def test_enabled_with_credentials_starts_worker(self):
        from st_rotator.cli import build_auto_renew

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, enabled=True, credentials=True)
            rotator = _FakeRotator(store.config)
            _lines, sink = self._sink()
            worker = build_auto_renew(  # type: ignore[arg-type]
                store.config, store, rotator, lock=threading.Lock(), sink=sink
            )
            try:
                if worker is None:
                    raise AssertionError("enabled + 凭据时应返回已 start 的 worker")
                self.assertIsNotNone(worker._thread)  # type: ignore[union-attr]
                self.assertTrue(worker._thread.is_alive())  # type: ignore[union-attr]
                # start() 的线程会立即跑一轮 run_once：probe 全部 ok，无登录 / 平台调用
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and len(rotator.probed) < 2:
                    time.sleep(0.01)
                self.assertEqual(rotator.probed, ["sk-old1", "sk-old2"])
                self.assertEqual(worker.account_status()["账号1"]["status"], "ok")
            finally:
                worker.stop()
            self.assertFalse(worker._thread.is_alive())  # type: ignore[union-attr]


class PersistEndToEndTest(unittest.TestCase):
    """最关键场景：真实 ConfigStore + 真实 StRotator + console.lock，直接调生产 persist 闭包。"""

    def test_persist_replaces_keys_pool_and_disk_consistently(self):
        from st_rotator.cli import build_auto_renew_persist

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, credentials=True)
            store = ConfigStore.load(path)
            rotator = StRotator(store.config)
            persist = build_auto_renew_persist(store, rotator, lock=threading.Lock())
            try:
                persist("账号1", ["sk-old1", "sk-old2"], "sk-new")

                # 磁盘文件：账号 1 的 key 被整体替换，user/password 原样保留
                reloaded = json.loads(path.read_text(encoding="utf-8"))
                acct = reloaded["accounts"][0]
                self.assertEqual(acct["api_keys"], ["sk-new"])
                self.assertEqual(acct["user"], "u1")
                self.assertEqual(acct["password"], "p1")

                # store 内存配置同步
                self.assertEqual(store.config.accounts[0].api_keys, ["sk-new"])
                self.assertEqual(store.config.accounts[0].user, "u1")
                self.assertEqual(store.config.accounts[0].password, "p1")

                # rotator 内存配置：reload 后拷回
                self.assertEqual(rotator.config.accounts[0].api_keys, ["sk-new"])

                # 池：含新 key、不含旧 key
                self.assertIsNotNone(rotator.pool.find_key("sk-new"))
                self.assertIsNone(rotator.pool.find_key("sk-old1"))
                self.assertIsNone(rotator.pool.find_key("sk-old2"))
            finally:
                rotator.close()

    def test_persist_increments_registry_rotation_count(self):
        from st_rotator.cli import build_auto_renew_persist
        from st_rotator.registry import Registry

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, credentials=True)
            store = ConfigStore.load(path)
            rotator = StRotator(store.config)
            registry = Registry.load(Path(tmp) / "replenish.json")
            persist = build_auto_renew_persist(
                store, rotator, registry=registry, lock=threading.Lock()
            )
            try:
                persist("账号1", ["sk-old1", "sk-old2"], "sk-new")
                self.assertEqual(registry.rotation_count(), 1)
                self.assertEqual(
                    Registry.load(Path(tmp) / "replenish.json").rotation_count(), 1
                )
            finally:
                rotator.close()


if __name__ == "__main__":
    unittest.main()
