"""ConsoleState.leak_guard_status / CLI build_leak_guard 装配层的单元测试。

全部使用 Fake / 真实 ConfigStore（tempfile），不访问真实商汤：
- snapshot 测试：无 worker 返回安全默认（disabled）；有 worker 返回其 status()。
- build_leak_guard 门控：disabled → None；enabled 但无余量服务 → None；enabled + 余量 → 启动。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

from st_rotator.config import Config, ConfigStore
from st_rotator.quota import QuotaService
from st_rotator.ui import ConsoleState


def _write_config(tmp: str, *, enabled: bool = False, credentials: bool = False) -> Path:
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
                "leak_guard": {"enabled": enabled, "interval_seconds": 3600.0},
            }
        ),
        encoding="utf-8",
    )
    return path


def _load_store(tmp: str, **kw: bool) -> ConfigStore:
    return ConfigStore.load(_write_config(tmp, **kw))


class _FakeWorker:
    """带 status() 的假 worker，用于 snapshot 注入。"""

    def __init__(self, status: dict) -> None:
        self._status = status

    def status(self) -> dict:
        return self._status


class _FakePool:
    def summary(self) -> dict[str, int]:
        return {"total": 0, "healthy": 0, "cooldown": 0, "invalid": 0, "inflight": 0}

    def snapshot(self) -> list[dict[str, int]]:
        return []


class _FakeLimiter:
    def stats(self) -> dict:
        return {
            "mode": "off",
            "rate": 0.0,
            "min_rate": 0.0,
            "max_rate": 0.0,
            "penalties": 0,
            "raises": 0,
        }


class _FakeRotator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool = _FakePool()
        self.limiter = _FakeLimiter()
        self.upstream_attempts = 0

    def probe_key(self, key: str) -> tuple[str, str]:
        return ("ok", "未校验")

    def available_models(self, *, refresh: bool = False, ttl: float = 300.0) -> dict:
        return {"models": []}


class SnapshotLeakGuardTest(unittest.TestCase):
    def test_no_worker_returns_disabled_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            payload = console.snapshot()["leak_guard_status"]
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["status"], "disabled")
            self.assertEqual(payload["pending"], [])

    def test_enabled_without_worker_reports_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, enabled=True)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            payload = console.snapshot()["leak_guard_status"]
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["status"], "not_running")
            self.assertIn("未运行", payload["message"])

    def test_snapshot_includes_worker_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            worker = _FakeWorker(
                {"enabled": True, "status": "flagged", "pending": ["账号1"], "pending_count": 1}
            )
            console = ConsoleState(
                store=store, rotator=rotator, leak_guard=worker  # type: ignore[arg-type]
            )
            self.assertEqual(
                console.snapshot()["leak_guard_status"],
                {
                    "enabled": True,
                    "status": "flagged",
                    "pending": ["账号1"],
                    "pending_count": 1,
                },
            )


class BuildLeakGuardTest(unittest.TestCase):
    def _sink(self) -> tuple[list[str], Callable[[str], None]]:
        lines: list[str] = []

        def sink(message: str) -> None:
            lines.append(message)

        return lines, sink

    def test_disabled_returns_none(self):
        from st_rotator.cli import build_leak_guard

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, credentials=True)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(
                store=store, rotator=rotator, quota=QuotaService(store.config)  # type: ignore[arg-type]
            )
            _lines, sink = self._sink()
            worker = build_leak_guard(
                store.config, store, rotator, console, lock=threading.Lock(), sink=sink  # type: ignore[arg-type]
            )
            self.assertIsNone(worker)

    def test_enabled_without_quota_returns_none(self):
        from st_rotator.cli import build_leak_guard

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, enabled=True)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # quota=None
            lines, sink = self._sink()
            worker = build_leak_guard(
                store.config, store, rotator, console, lock=threading.Lock(), sink=sink  # type: ignore[arg-type]
            )
            self.assertIsNone(worker)
            self.assertTrue(any("未配置账号登录凭据" in line for line in lines))

    def test_enabled_with_quota_starts_worker(self):
        from st_rotator.cli import build_leak_guard

        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp, enabled=True, credentials=True)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(
                store=store, rotator=rotator, quota=QuotaService(store.config)  # type: ignore[arg-type]
            )
            _lines, sink = self._sink()
            worker = build_leak_guard(
                store.config, store, rotator, console, lock=threading.Lock(), sink=sink  # type: ignore[arg-type]
            )
            try:
                if worker is None:
                    raise AssertionError("enabled + 凭据时应返回已 start 的 worker")
                self.assertIsNotNone(worker._thread)  # type: ignore[union-attr]
                self.assertTrue(worker._thread.is_alive())  # type: ignore[union-attr]
                # 允许首轮扫描完成
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and worker.status()["last_scan_at"] is None:
                    time.sleep(0.01)
                self.assertIsNotNone(worker.status()["last_scan_at"])
            finally:
                worker.stop()
            self.assertFalse(worker._thread.is_alive())  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
