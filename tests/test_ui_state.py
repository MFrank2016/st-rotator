"""ConsoleState.snapshot() 的单元测试。

Fake rotator（不发网络）+ 真实 ConfigStore（tempfile 落盘真实 JSON），
base_url 指向死端口，避免任何网络行为。
"""

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState


def _write_config(tmp: str) -> Path:
    """写一份含两个账号的真实配置，顺序即配置里的顺序。"""
    path = Path(tmp) / "config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [
                    {"name": "账号A", "api_keys": ["sk-a1"]},
                    {"name": "账号B", "api_keys": ["sk-b1", "sk-b2"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


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
    """snapshot 用的假 rotator：不发网络。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool = _FakePool()
        self.limiter = _FakeLimiter()
        self.upstream_attempts = 0

    def available_models(
        self, *, refresh: bool = False, ttl: float = 300.0
    ) -> dict[str, object]:
        return {"models": []}


class SnapshotAccountNamesTest(unittest.TestCase):
    """RED：snapshot() 当前没有 account_names 键；GREEN：等于配置账号名的有序列表。"""

    def test_snapshot_includes_account_names_in_config_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore.load(_write_config(tmp))
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            self.assertEqual(console.snapshot()["account_names"], ["账号A", "账号B"])


if __name__ == "__main__":
    unittest.main()
