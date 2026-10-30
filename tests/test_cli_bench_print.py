"""bench / demo 的空请求守卫：``-n 0`` 不能因除零炸掉。"""

from __future__ import annotations

import contextlib
import io
import unittest

from st_rotator.cli import _print_bench, _run_bench


class PrintBenchZeroTest(unittest.TestCase):
    def test_zero_requests_prints_friendly_line(self) -> None:
        stats = _run_bench(object(), 0, 1)  # total=0 时不会真的发请求
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_bench(stats)  # 旧实现这里 ZeroDivisionError
        out = buf.getvalue()
        self.assertIn("请求总数", out)
        self.assertIn("未发起请求", out)

    def test_nonzero_total_keeps_percentage(self) -> None:
        stats = {
            "total": 4,
            "parallel": 2,
            "max_tokens": 64,
            "wall": 1.0,
            "qps": 4.0,
            "latencies": [0.1, 0.2],
            "success": 3,
            "rate_limited": 1,
            "invalid": 0,
            "failed": 0,
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_bench(stats)
        self.assertIn("75.0%", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
