"""限速器：rate 可写且立即生效、refund 用预订时的间隔、无预订时不退。"""

from __future__ import annotations

import threading
import unittest

from st_rotator.limiter import AdaptiveRateLimiter, RateLimiter


class RateLimiterRateTest(unittest.TestCase):
    def _limiter(self, rate: float = 1.0) -> RateLimiter:
        return RateLimiter(rate, clock=lambda: 0.0, sleeper=lambda _s: None)

    def test_rate_setter_updates_interval(self) -> None:
        limiter = self._limiter(1.0)
        self.assertEqual(limiter._interval, 1.0)
        limiter.rate = 10.0
        self.assertEqual(limiter.rate, 10.0)
        self.assertAlmostEqual(limiter._interval, 0.1)
        limiter.acquire()
        self.assertAlmostEqual(limiter._next, 0.1)  # 新速率立刻生效

    def test_rate_zero_disables(self) -> None:
        limiter = self._limiter(1.0)
        limiter.rate = 0.0
        self.assertFalse(limiter.enabled)
        self.assertEqual(limiter.acquire(), 0.0)


class RateLimiterRefundTest(unittest.TestCase):
    def _limiter(self, rate: float = 1.0) -> RateLimiter:
        return RateLimiter(rate, clock=lambda: 0.0, sleeper=lambda _s: None)

    def test_refund_uses_reserved_interval(self) -> None:
        limiter = self._limiter(1.0)
        limiter.acquire()  # 预订 1.0s
        limiter.rate = 100.0  # 期间速率暴涨
        limiter.refund()
        # 退的是预订时的 1.0s；若用当前速率只会退 0.01s
        self.assertEqual(limiter._next, 0.0)

    def test_double_refund_is_noop(self) -> None:
        limiter = self._limiter(1.0)
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(limiter._next, 2.0)
        limiter.refund()
        self.assertEqual(limiter._next, 1.0)
        limiter.refund()  # 已经没有未退的预订
        self.assertEqual(limiter._next, 1.0)

    def test_refund_without_acquire_is_noop(self) -> None:
        limiter = self._limiter(1.0)
        limiter.refund()
        self.assertEqual(limiter._next, 0.0)

    def test_refund_in_another_thread_does_not_steal_reservation(self) -> None:
        limiter = self._limiter(1.0)
        limiter.acquire()  # 主线程的预订
        seen: list[float] = []

        def other() -> None:
            limiter.refund()
            seen.append(limiter._next)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        self.assertEqual(seen, [1.0])  # 别的线程退不掉这次预订


class AdaptiveRefundTest(unittest.TestCase):
    def _limiter(self) -> AdaptiveRateLimiter:
        return AdaptiveRateLimiter(1.0, clock=lambda: 0.0, sleeper=lambda _s: None)

    def test_refund_uses_reserved_interval(self) -> None:
        limiter = self._limiter()
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(limiter._next, 2.0)
        limiter.on_rate_limited()  # 速率被 AIMD 调到 0.85
        limiter.refund()
        # 退的是预订时的 1.0s；用当前速率会退 1/0.85 ≈ 1.176
        self.assertAlmostEqual(limiter._next, 1.0)

    def test_refund_without_reservation_is_noop(self) -> None:
        limiter = self._limiter()
        limiter.refund()
        self.assertEqual(limiter._next, 0.0)

    def test_double_refund_is_noop(self) -> None:
        limiter = self._limiter()
        limiter.acquire()
        limiter.refund()
        limiter.refund()
        self.assertEqual(limiter._next, 0.0)


if __name__ == "__main__":
    unittest.main()
