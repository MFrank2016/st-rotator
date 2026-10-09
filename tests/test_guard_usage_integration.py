"""泄漏守卫 × 真实用量/积分追踪 的集成测试（闭合「假信号」覆盖缺口）。

用**真实** ``UsageTracker`` 与 ``CreditTracker``（共用一个可控时钟）驱动
``LeakGuardWorker``，验证三条关键路径：

- S1：网关从未记录用量 + 通用池采样有增量 → 记录账号。
- S2：窗口内网关有用量 → 不记录。
- C1：网关用量落在采样覆盖区间内（采样滞后）→ **不**误记（避免轮换健康账号）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from st_rotator.config import AccountConfig
from st_rotator.autorenew import RotationOutcome
from st_rotator.guard import GuardStore, LeakGuardWorker
from st_rotator.quota import CreditTracker
from st_rotator.ui import UsageTracker


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t



class GuardUsageIntegrationTest(unittest.TestCase):
    def _worker(self, *, store: GuardStore, clock: _Clock, usage: UsageTracker, credits: CreditTracker, accounts):
        return LeakGuardWorker(
            accounts=lambda: list(accounts),
            token_usage_last=usage.last_at,
            credit_changes=lambda since, idle_since: credits.accounts_with_delta_since(
                since, idle_since=idle_since
            ),
            rotate=lambda account: RotationOutcome(True, "ok", ""),
            store=store,
            clock=clock,
            window=600.0,
            rotate_hour=23,
            rotate_minute=59,
        )

    def test_s1_true_leak_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(1000.0)
            usage = UsageTracker(clock=clock)
            credits = CreditTracker(clock=clock)
            store = GuardStore.load(Path(tmp) / "guard.json")
            credits.note("A", "general", reset_at=1, used=0.0)      # ts=1000，首采样
            clock.t = 1600.0
            credits.note("A", "general", reset_at=1, used=40.0)     # ts=1600，delta=40
            worker = self._worker(
                store=store, clock=clock, usage=usage, credits=credits,
                accounts=[AccountConfig(name="A", api_keys=["sk-a"], user="u", password="p")],
            )
            clock.t = 1700.0
            worker.run_once()
            self.assertEqual(store.pending(), ["A"])

    def test_s2_usage_in_window_no_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(1000.0)
            usage = UsageTracker(clock=clock)
            credits = CreditTracker(clock=clock)
            store = GuardStore.load(Path(tmp) / "guard.json")
            usage.note(prompt_tokens=5, completion_tokens=5)        # last_at=1000
            credits.note("A", "general", reset_at=1, used=0.0)
            clock.t = 1300.0
            credits.note("A", "general", reset_at=1, used=40.0)     # delta=40
            worker = self._worker(
                store=store, clock=clock, usage=usage, credits=credits,
                accounts=[AccountConfig(name="A", api_keys=["sk-a"], user="u", password="p")],
            )
            clock.t = 1500.0  # since=900；last_at=1000 >= 900 → 窗口内有用量
            worker.run_once()
            self.assertEqual(store.pending(), [])

    def test_c1_sampler_lag_not_misattributed(self) -> None:
        """网关用量在窗口外、但采样覆盖区间内含该用量 → 不误记（C1 回归）。"""
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(1000.0)
            usage = UsageTracker(clock=clock)
            credits = CreditTracker(clock=clock)
            store = GuardStore.load(Path(tmp) / "guard.json")
            usage.note(prompt_tokens=5, completion_tokens=5)        # last_at=1000
            credits.note("A", "general", reset_at=1, used=0.0)      # ts=1000
            clock.t = 1300.0
            credits.note("A", "general", reset_at=1, used=50.0)     # ts=1300, since=1000, delta=50
            worker = self._worker(
                store=store, clock=clock, usage=usage, credits=credits,
                accounts=[AccountConfig(name="A", api_keys=["sk-a"], user="u", password="p")],
            )
            clock.t = 1700.0  # since=1100；last_at=1000 < 1100 → 网关空转
            worker.run_once()
            # 采样覆盖区间 [1000,1300] 含网关用量 → 排除，不误记
            self.assertEqual(store.pending(), [])


if __name__ == "__main__":
    unittest.main()
