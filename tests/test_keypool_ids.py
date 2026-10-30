"""Key 池的数字 id：按池内顺序递增、稳定不复用，且可作为操作标识。"""

from __future__ import annotations

import threading
import time
import unittest

from st_rotator.config import AccountConfig
from st_rotator.errors import NoAvailableKey
from st_rotator.keypool import KeyPool, KeyStatus


def _pool() -> KeyPool:
    return KeyPool(
        [
            AccountConfig(name="a", api_keys=["sk-a1", "sk-a2"]),
            AccountConfig(name="b", api_keys=["sk-b1"]),
        ]
    )


class KeyPoolNumericIdTest(unittest.TestCase):
    def test_snapshot_ids_are_sequential_ints(self) -> None:
        pool = _pool()
        ids = [k["id"] for k in pool.snapshot()]
        self.assertEqual(ids, [1, 2, 3])
        self.assertTrue(all(isinstance(i, int) for i in ids))

    def test_add_key_gets_next_id(self) -> None:
        pool = _pool()
        item = pool.add_key("sk-c1", account="c")
        self.assertEqual(item.id, 4)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 2, 3, 4])

    def test_find_by_id_accepts_int_str_and_plaintext(self) -> None:
        pool = _pool()
        self.assertEqual(pool.find_by_id(2).key, "sk-a2")
        self.assertEqual(pool.find_by_id("2").key, "sk-a2")
        self.assertEqual(pool.find_by_id("sk-a2").key, "sk-a2")
        self.assertIsNone(pool.find_by_id(999))

    def test_remove_by_numeric_id_and_no_reuse(self) -> None:
        pool = _pool()
        removed = pool.remove_key(2)
        self.assertIsNotNone(removed)
        self.assertEqual(removed.key, "sk-a2")
        # 不重排、不复用：剩余 id 保留原值（出现空位）
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 3])
        # 新增拿下一个号，不复用已删除的 2
        item = pool.add_key("sk-c1", account="c")
        self.assertEqual(item.id, 4)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 3, 4])

    def test_remove_by_plaintext_still_works(self) -> None:
        pool = _pool()
        removed = pool.remove_key("sk-b1")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.id, 3)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 2])


class KeyPoolAcquireNoCandidateTest(unittest.TestCase):
    """全被排除（没有可等的 Key）要立即报错，而不是死循环；并发打满仍要等。"""

    def test_all_excluded_raises_immediately(self) -> None:
        pool = KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a1", "sk-a2"])]
        )
        started = time.monotonic()
        with self.assertRaises(NoAvailableKey) as ctx:
            pool.acquire(
                exclude=["sk-a1", "sk-a2"], timeout=1.0
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("没有可用", str(ctx.exception))

    def test_concurrency_full_waits_then_times_out(self) -> None:
        pool = KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a"], max_concurrency=1)]
        )
        pool.acquire(timeout=1.0)  # 占满并发
        with self.assertRaises(NoAvailableKey) as ctx:
            pool.acquire(timeout=0.2)
        self.assertIn("超时", str(ctx.exception))

    def test_acquire_succeeds_after_release(self) -> None:
        pool = KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a"], max_concurrency=1)]
        )
        held = pool.acquire(timeout=1.0)
        got: list[object] = []
        errors: list[BaseException] = []

        def waiter() -> None:
            try:
                got.append(pool.acquire(timeout=2.0))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        pool.release(held)
        thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 1)
        self.assertIs(got[0], held)


class KeyPoolStatusMonotonicTest(unittest.TestCase):
    """状态只升不降：INVALID 不能被 429 / 5xx / 模型不可用降级成 COOLDOWN。"""

    def _pool(self) -> tuple[KeyPool, _Clock]:
        clock = _Clock(100.0)
        pool = KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a1", "sk-a2"])], clock=clock
        )
        return pool, clock

    def _invalid(self, pool: KeyPool):
        key = pool.find_key("sk-a1")
        pool.report_invalid(key, "401")
        self.assertIs(key.status, KeyStatus.INVALID)
        return key, key.cooldown_until

    def test_rate_limit_does_not_downgrade_invalid(self) -> None:
        pool, clock = self._pool()
        key, until = self._invalid(pool)
        clock.t = 101.0
        pool.report_rate_limit(key)
        self.assertIs(key.status, KeyStatus.INVALID)
        self.assertEqual(key.cooldown_until, until)
        # 同账号的兄弟 Key 也不该因为一把失效 Key 的 429 被冷却
        self.assertIs(pool.find_key("sk-a2").status, KeyStatus.HEALTHY)

    def test_server_error_does_not_downgrade_invalid(self) -> None:
        pool, clock = self._pool()
        key, until = self._invalid(pool)
        clock.t = 101.0
        pool.report_server_error(key)
        self.assertIs(key.status, KeyStatus.INVALID)
        self.assertEqual(key.cooldown_until, until)

    def test_model_unavailable_does_not_downgrade_invalid(self) -> None:
        pool, clock = self._pool()
        key, until = self._invalid(pool)
        clock.t = 101.0
        pool.report_model_unavailable(key)
        self.assertIs(key.status, KeyStatus.INVALID)
        self.assertEqual(key.cooldown_until, until)

    def test_cooldown_key_can_still_be_marked_invalid(self) -> None:
        pool, clock = self._pool()
        key = pool.find_key("sk-a1")
        clock.t = 101.0
        pool.report_rate_limit(key)
        self.assertIs(key.status, KeyStatus.COOLDOWN)
        pool.report_invalid(key, "403")
        self.assertIs(key.status, KeyStatus.INVALID)


class _Clock:
    """可手动推进的假时钟。"""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class KeyPoolReportSuccessTest(unittest.TestCase):
    """晚到的成功不能抹掉刚判下的冷却。"""

    def _pool(self, clock: _Clock) -> KeyPool:
        return KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a1", "sk-a2"])],
            clock=clock,
        )

    def test_late_success_keeps_account_cooldown(self) -> None:
        clock = _Clock(100.0)
        pool = self._pool(clock)
        key = pool.acquire()  # last_used = 100
        clock.t = 101.0
        pool.report_rate_limit(key)  # 冷却判定时刻 = 101
        acct = key.account_state
        until, failures = acct.cooldown_until, acct.consecutive_failures
        clock.t = 102.0
        pool.report_success(key)  # 本次请求在判定之前就已开始
        self.assertEqual(acct.cooldown_until, until)
        self.assertEqual(acct.consecutive_failures, failures)
        self.assertIs(key.status, KeyStatus.COOLDOWN)
        sibling = pool.find_key("sk-a2")
        self.assertIs(sibling.status, KeyStatus.COOLDOWN)
        self.assertGreater(sibling.cooldown_until, 102.0)

    def test_success_started_after_judgement_clears_account_cooldown(self) -> None:
        clock = _Clock(100.0)
        pool = self._pool(clock)
        key = pool.acquire()
        clock.t = 101.0
        pool.report_rate_limit(key)
        acct = key.account_state
        key.last_used = 105.0  # 模拟冷却判定之后才发起的请求
        clock.t = 106.0
        pool.report_success(key)
        self.assertEqual(acct.cooldown_until, 0.0)
        self.assertEqual(acct.consecutive_failures, 0)

    def test_late_success_keeps_own_key_cooldown(self) -> None:
        clock = _Clock(100.0)
        pool = self._pool(clock)
        key = pool.acquire()
        clock.t = 101.0
        pool.report_server_error(key)  # 只冷却该 Key 自身
        until = key.cooldown_until
        clock.t = 102.0
        pool.report_success(key)
        self.assertEqual(key.cooldown_until, until)
        self.assertIs(key.status, KeyStatus.COOLDOWN)

    def test_force_success_clears_cooldown(self) -> None:
        clock = _Clock(100.0)
        pool = self._pool(clock)
        key = pool.acquire()
        clock.t = 101.0
        pool.report_server_error(key)  # last_used(100) <= cooldown_set_at(101)，会被守卫挡住
        pool.report_success(key, force=True)
        self.assertEqual(key.cooldown_until, 0.0)
        self.assertIs(key.status, KeyStatus.HEALTHY)
        self.assertEqual(key.cooldown_set_at, 0.0)

    def test_plain_success_still_recovers_key(self) -> None:
        clock = _Clock(100.0)
        pool = self._pool(clock)
        key = pool.find_key("sk-a1")
        key.last_error = "boom"
        key.consecutive_failures = 3
        before = key.stats.successes
        pool.report_success(key, latency=0.5)
        self.assertEqual(key.stats.successes, before + 1)
        self.assertIs(key.status, KeyStatus.HEALTHY)
        self.assertEqual(key.last_error, "")
        self.assertEqual(key.consecutive_failures, 0)
        self.assertEqual(key.cooldown_until, 0.0)
        self.assertAlmostEqual(key.stats.total_latency, 0.5)


if __name__ == "__main__":
    unittest.main()
