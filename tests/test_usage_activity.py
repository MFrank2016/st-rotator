"""UsageTracker / GatewayMetrics：最近一次用量活动的墙钟时间戳。"""

from __future__ import annotations

import time
import unittest

from st_rotator.ui import GatewayMetrics, UsageTracker


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class UsageTrackerActivityTest(unittest.TestCase):
    def test_last_at_none_before_any_note(self) -> None:
        tracker = UsageTracker(clock=_Clock(0.0))
        self.assertIsNone(tracker.last_at())

    def test_has_usage_since_false_before_any_note(self) -> None:
        tracker = UsageTracker(clock=_Clock(0.0))
        self.assertFalse(tracker.has_usage_since(0.0))
        self.assertFalse(tracker.has_usage_since(-100.0))

    def test_last_at_equals_clock_after_note(self) -> None:
        clock = _Clock(1000.0)
        tracker = UsageTracker(clock=clock)
        tracker.note(prompt_tokens=1)
        self.assertEqual(tracker.last_at(), 1000.0)

    def test_last_at_advances_with_clock(self) -> None:
        clock = _Clock(1000.0)
        tracker = UsageTracker(clock=clock)
        tracker.note()
        clock.t = 2000.0
        tracker.note()
        self.assertEqual(tracker.last_at(), 2000.0)

    def test_has_usage_since_boundary(self) -> None:
        clock = _Clock(500.0)
        tracker = UsageTracker(clock=clock)
        tracker.note()
        # ts <= last_at → True（含相等边界）
        self.assertTrue(tracker.has_usage_since(500.0))
        self.assertTrue(tracker.has_usage_since(499.999))
        self.assertTrue(tracker.has_usage_since(-1.0))
        # ts > last_at → False
        self.assertFalse(tracker.has_usage_since(500.001))
        self.assertFalse(tracker.has_usage_since(1000.0))


class GatewayMetricsActivityTest(unittest.TestCase):
    def test_delegates_has_usage_since(self) -> None:
        metrics = GatewayMetrics()
        self.assertFalse(metrics.has_usage_since(time.time()))
        metrics.note_usage(prompt_tokens=3, completion_tokens=4)
        # 刚记录过 → 早于此刻的 ts 为 True，未来时刻为 False
        self.assertTrue(metrics.has_usage_since(time.time() - 1.0))
        self.assertFalse(metrics.has_usage_since(time.time() + 1000.0))
        self.assertIsNotNone(metrics.usage.last_at())


if __name__ == "__main__":
    unittest.main()
