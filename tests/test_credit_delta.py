"""CreditTracker.accounts_with_delta_since：按池筛选、去重、保留出现顺序。"""

from __future__ import annotations

import unittest

from st_rotator.quota import CreditTracker


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class AccountsWithDeltaSinceTest(unittest.TestCase):
    def test_general_delta_after_ts_only(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("a", "general", reset_at=1, used=10.0)   # ts=0，delta=0
        clock.t = 300.0
        tracker.note("a", "general", reset_at=1, used=25.0)   # ts=300，delta=15
        self.assertEqual(tracker.accounts_with_delta_since(300.0), ["a"])
        self.assertEqual(tracker.accounts_with_delta_since(301.0), [])
        # ts 含边界：早于有增量采样的时间点也拿不到
        self.assertEqual(tracker.accounts_with_delta_since(100.0), ["a"])

    def test_pool_filter(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("g", "general", reset_at=1, used=0.0)     # 首次采样 delta=0
        tracker.note("f", "flash_lite", reset_at=1, used=0.0)  # 首次采样 delta=0
        clock.t = 300.0
        tracker.note("g", "general", reset_at=1, used=5.0)     # general delta=5
        tracker.note("f", "flash_lite", reset_at=1, used=7.0)  # flash_lite delta=7
        self.assertEqual(tracker.accounts_with_delta_since(0.0), ["g"])
        self.assertEqual(tracker.accounts_with_delta_since(0.0, pool="flash_lite"), ["f"])

    def test_zero_and_negative_delta_excluded(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("a", "general", reset_at=1, used=10.0)   # 首次采样 delta=0
        clock.t = 300.0
        tracker.note("a", "general", reset_at=1, used=5.0)    # used 变小 → 视为复位，delta=5
        self.assertEqual(tracker.accounts_with_delta_since(0.0), ["a"])
        # 再采一次同值 → delta=0，不应改变结果
        clock.t = 600.0
        tracker.note("a", "general", reset_at=1, used=5.0)
        self.assertEqual(tracker.accounts_with_delta_since(0.0), ["a"])
        # 只要求 ts 落在两次有增量采样之间时也能拿到（去重后仍是 a）
        self.assertEqual(tracker.accounts_with_delta_since(600.0), [])

    def test_dedupe_single_entry(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("a", "general", reset_at=1, used=0.0)    # delta=0
        clock.t = 300.0
        tracker.note("a", "general", reset_at=1, used=10.0)   # delta=10
        clock.t = 600.0
        tracker.note("a", "general", reset_at=1, used=30.0)   # delta=20
        self.assertEqual(tracker.accounts_with_delta_since(0.0), ["a"])

    def test_order_preserved(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("b", "general", reset_at=1, used=0.0)
        tracker.note("a", "general", reset_at=1, used=0.0)
        tracker.note("c", "general", reset_at=1, used=0.0)
        clock.t = 300.0
        tracker.note("b", "general", reset_at=1, used=1.0)    # 首个有增量
        tracker.note("a", "general", reset_at=1, used=1.0)
        tracker.note("c", "general", reset_at=1, used=1.0)
        self.assertEqual(tracker.accounts_with_delta_since(0.0), ["b", "a", "c"])

    def test_sample_records_coverage_start(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("a", "general", reset_at=1, used=10.0)   # ts=0
        clock.t = 300.0
        tracker.note("a", "general", reset_at=1, used=25.0)   # ts=300, since=0
        s = tracker.samples()[-1]
        self.assertEqual(s["since"], 0.0)
        self.assertEqual(s["ts"], 300.0)

    def test_idle_since_excludes_samples_covering_usage(self) -> None:
        clock = _Clock(0.0)
        tracker = CreditTracker(hours=48, clock=clock)
        tracker.note("a", "general", reset_at=1, used=0.0)    # ts=0
        clock.t = 300.0
        tracker.note("a", "general", reset_at=1, used=50.0)   # ts=300, since=0, delta=50
        # 网关在采样覆盖区间内（ts=100）仍有消耗 → 该增量可能来自网关自身，排除
        self.assertEqual(
            tracker.accounts_with_delta_since(0.0, idle_since=100.0), []
        )
        # 网关消耗早于覆盖区间起点（since=0）→ 增量确属外部，计入
        self.assertEqual(
            tracker.accounts_with_delta_since(0.0, idle_since=-10.0), ["a"]
        )
        # idle_since 恰好等于覆盖起点 → 仍排除（边界保守）
        self.assertEqual(
            tracker.accounts_with_delta_since(0.0, idle_since=0.0), []
        )


if __name__ == "__main__":
    unittest.main()
