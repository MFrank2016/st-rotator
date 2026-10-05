"""UsageTracker：按小时分桶的 token 用量统计。"""

from __future__ import annotations

import unittest

from st_rotator.ui import UsageTracker


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class UsageTrackerTest(unittest.TestCase):
    def test_bucket_and_series(self) -> None:
        clock = _Clock(0.0)
        tracker = UsageTracker(hours=48, clock=clock)
        tracker.note(prompt_tokens=10, completion_tokens=5)
        tracker.note(prompt_tokens=1, completion_tokens=2)  # 同一小时
        series = tracker.series(hours=2)
        self.assertEqual(len(series), 2)
        self.assertEqual(series[-1]["prompt"], 11)
        self.assertEqual(series[-1]["completion"], 7)
        self.assertEqual(series[-1]["total"], 18)
        self.assertEqual(series[-1]["requests"], 2)
        self.assertEqual(series[0]["total"], 0)  # 空小时补 0

    def test_prunes_old_buckets(self) -> None:
        clock = _Clock(0.0)
        tracker = UsageTracker(hours=2, clock=clock)
        tracker.note(prompt_tokens=5)  # 小时 0
        clock.t = 3 * 3600  # 跳到小时 3，小时 0 应被清理
        tracker.note(prompt_tokens=7)
        series = tracker.series(hours=4)  # 被截断到 self._hours=2
        self.assertEqual([b["hour"] for b in series], [7200, 10800])
        self.assertEqual(series[0]["total"], 0)
        self.assertEqual(series[1]["total"], 7)

    def test_series_empty_when_no_data(self) -> None:
        tracker = UsageTracker(hours=24, clock=_Clock(0.0))
        series = tracker.series(hours=3)
        self.assertEqual([b["total"] for b in series], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
