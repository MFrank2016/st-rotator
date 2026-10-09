"""LeakGuardWorker（泄漏守卫）的单元测试。

全部使用 Fake：不访问真实商汤、不发网络。

- 可控时钟：按本地时间构造 epoch，验证「每日 02:00 轮换」的到点判定。
- ``token_usage_since`` / ``credit_changes`` 桩：可配置返回值并记录入参。
- ``rotate`` 桩：记录被轮换的账号，按配置返回 ``RotationOutcome``。
- 真实 ``GuardStore``（tempfile 落盘真实 JSON）。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from st_rotator.autorenew import RotationOutcome
from st_rotator.config import AccountConfig
from st_rotator.guard import GuardStore, LeakGuardWorker


def _epoch_at(year: int, mon: int, day: int, hour: int, minute: int) -> float:
    """按本地时间构造 epoch 秒（``localtime`` 能往返还原该本地时刻）。"""
    return time.mktime((year, mon, day, hour, minute, 0, 0, 0, -1))


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class LeakGuardWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log_lines: list[str] = []

    def _log(self, message: str) -> None:
        self.log_lines.append(message)

    def _worker(
        self,
        *,
        store: GuardStore,
        clock: _Clock,
        accounts=(),
        usage_last: float | None = None,
        changes=(),
        rotate=None,
        **kwargs,
    ) -> LeakGuardWorker:
        self.usage_calls = 0
        self.change_calls: list[tuple[float, float | None]] = []
        self.rotate_calls: list[str] = []

        def token_usage_last() -> float | None:
            self.usage_calls += 1
            return usage_last

        def credit_changes(since: float, idle_since: float | None):
            self.change_calls.append((since, idle_since))
            return list(changes)

        def do_rotate(account: AccountConfig) -> RotationOutcome:
            self.rotate_calls.append(account.name)
            if rotate is not None:
                return rotate(account)
            return RotationOutcome(True, "ok", "")

        return LeakGuardWorker(
            accounts=lambda: list(accounts),
            token_usage_last=token_usage_last,
            credit_changes=credit_changes,
            rotate=do_rotate,
            store=store,
            clock=clock,
            log=self._log,
            **kwargs,
        )

    # ------------------------------------------------------------ S1 记录
    def test_idle_with_credit_changes_flags_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))  # 01:00，未到轮换点
            worker = self._worker(store=store, clock=clock, changes=["A", "B"])
            worker.run_once()
            self.assertEqual(store.pending(), ["A", "B"])
            self.assertEqual(worker.status()["status"], LeakGuardWorker.STATUS_FLAGGED)
            self.assertEqual(worker.status()["pending_count"], 2)
            self.assertEqual(self.rotate_calls, [])

    def test_usage_present_no_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(store=store, clock=clock, usage_last=clock.t, changes=["A"])
            worker.run_once()
            self.assertEqual(store.pending(), [])
            self.assertEqual(worker.status()["status"], LeakGuardWorker.STATUS_IDLE)

    def test_no_changes_no_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(store=store, clock=clock, changes=[])
            worker.run_once()
            self.assertEqual(store.pending(), [])
            self.assertEqual(worker.status()["status"], LeakGuardWorker.STATUS_IDLE)

    def test_window_passed_to_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            now = _epoch_at(2026, 10, 9, 1, 0)
            clock = _Clock(now)
            worker = self._worker(
                store=store, clock=clock, changes=[], window=600.0
            )
            worker.run_once()
            self.assertEqual(self.usage_calls, 1)
            self.assertEqual(self.change_calls, [(now - 600.0, None)])

    def test_idle_since_forwarded_to_credit_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            now = _epoch_at(2026, 10, 9, 1, 0)
            last = now - 3600  # 网关很久没用过
            clock = _Clock(now)
            worker = self._worker(
                store=store, clock=clock, changes=[], usage_last=last, window=600.0
            )
            worker.run_once()
            self.assertEqual(self.change_calls, [(now - 600.0, last)])

    def test_rotate_exception_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            a = AccountConfig(name="A", api_keys=["sk-a"], user="u", password="p")
            b = AccountConfig(name="B", api_keys=["sk-b"], user="u", password="p")
            store.add_pending(["A", "B"])

            def rotate(account: AccountConfig) -> RotationOutcome:
                if account.name == "A":
                    raise RuntimeError("persist boom")
                return RotationOutcome(True, "ok", "")

            clock = _Clock(_epoch_at(2026, 10, 9, 2, 0))
            worker = self._worker(store=store, clock=clock, accounts=[a, b], rotate=rotate)
            worker.run_once()
            self.assertEqual(self.rotate_calls, ["A", "B"])  # A 抛异常不影响 B
            self.assertEqual(store.pending(), [])  # 仍清空
            self.assertEqual(store.last_rotate_date(), "2026-10-09")

    # ------------------------------------------------------------ S3 定时轮换
    def test_rotate_at_two_am_then_idempotent_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            account = AccountConfig(
                name="A", api_keys=["sk-a"], user="u", password="p"
            )
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(
                store=store, clock=clock, accounts=[account], changes=["A"]
            )
            worker.run_once()  # 01:00 记录
            self.assertEqual(store.pending(), ["A"])
            # 到 02:00 → 轮换
            clock.t = _epoch_at(2026, 10, 9, 2, 0)
            worker.run_once()
            self.assertEqual(self.rotate_calls, ["A"])
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.last_rotate_date(), "2026-10-09")
            self.assertEqual(worker.status()["status"], LeakGuardWorker.STATUS_ROTATED)
            # 同日再跑一轮 → 不再轮换
            clock.t = _epoch_at(2026, 10, 9, 3, 0)
            worker.run_once()
            self.assertEqual(self.rotate_calls, ["A"])  # 未新增

    def test_not_due_before_two_am_no_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            account = AccountConfig(
                name="A", api_keys=["sk-a"], user="u", password="p"
            )
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(
                store=store, clock=clock, accounts=[account], changes=["A"]
            )
            worker.run_once()
            worker.run_once()
            self.assertEqual(self.rotate_calls, [])
            self.assertEqual(store.pending(), ["A"])

    def test_catch_up_after_two_am_rotates_immediately(self) -> None:
        """启动时已过 02:00 且当日未轮换：下一轮扫描即补轮换。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            store = GuardStore.load(path)
            store.add_pending(["A"])  # 上一进程遗留
            account = AccountConfig(
                name="A", api_keys=["sk-a"], user="u", password="p"
            )
            clock = _Clock(_epoch_at(2026, 10, 9, 3, 0))  # 03:00
            worker = self._worker(store=store, clock=clock, accounts=[account])
            worker.run_once()
            self.assertEqual(self.rotate_calls, ["A"])
            self.assertEqual(store.pending(), [])

    def test_rotation_without_pending_marks_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 2, 30))
            worker = self._worker(store=store, clock=clock)
            worker.run_once()
            self.assertEqual(store.last_rotate_date(), "2026-10-09")
            self.assertEqual(self.rotate_calls, [])

    # ------------------------------------------------------------ 跳过 / 隔离
    def test_account_missing_from_config_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            store = GuardStore.load(path)
            store.add_pending(["gone"])
            clock = _Clock(_epoch_at(2026, 10, 9, 2, 0))
            worker = self._worker(store=store, clock=clock, accounts=[])
            worker.run_once()
            self.assertEqual(self.rotate_calls, [])
            self.assertEqual(store.pending(), [])  # 结束后清空

    def test_account_without_credentials_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            store = GuardStore.load(path)
            store.add_pending(["A"])
            account = AccountConfig(name="A", api_keys=["sk-a"], user="", password="")
            clock = _Clock(_epoch_at(2026, 10, 9, 2, 0))
            worker = self._worker(store=store, clock=clock, accounts=[account])
            worker.run_once()
            self.assertEqual(self.rotate_calls, [])
            self.assertEqual(store.pending(), [])

    def test_rotation_failure_keeps_going(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            a = AccountConfig(name="A", api_keys=["sk-a"], user="u", password="p")
            b = AccountConfig(name="B", api_keys=["sk-b"], user="u", password="p")
            store.add_pending(["A", "B"])

            def rotate(account: AccountConfig) -> RotationOutcome:
                if account.name == "A":
                    return RotationOutcome(False, "check_error", "boom")
                return RotationOutcome(True, "ok", "")

            clock = _Clock(_epoch_at(2026, 10, 9, 2, 0))
            worker = self._worker(
                store=store, clock=clock, accounts=[a, b], rotate=rotate
            )
            worker.run_once()
            self.assertEqual(self.rotate_calls, ["A", "B"])  # A 失败不影响 B
            self.assertEqual(store.pending(), [])

    # ------------------------------------------------------------ 线程生命周期
    def test_start_stop_daemon_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(store=store, clock=clock, interval=0.05)
            counter = {"n": 0}
            original = worker.run_once

            def counting() -> None:
                counter["n"] += 1
                original()

            worker.run_once = counting  # type: ignore[method-assign]
            worker.start()
            self.assertIsNotNone(worker._thread)
            self.assertTrue(worker._thread.daemon)  # type: ignore[union-attr]
            deadline = time.monotonic() + 2.0
            while counter["n"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()
            self.assertGreaterEqual(counter["n"], 2)
            self.assertFalse(worker._thread.is_alive())  # type: ignore[union-attr]

    def test_next_rotate_at_is_next_two_am(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            clock = _Clock(_epoch_at(2026, 10, 9, 1, 0))
            worker = self._worker(store=store, clock=clock)
            expected = _epoch_at(2026, 10, 9, 2, 0)
            self.assertAlmostEqual(worker.status()["next_rotate_at"], expected, places=0)
            # 过了 02:00 → 下一次是明天 02:00
            clock.t = _epoch_at(2026, 10, 9, 3, 0)
            self.assertAlmostEqual(
                worker.status()["next_rotate_at"], _epoch_at(2026, 10, 10, 2, 0), places=0
            )


if __name__ == "__main__":
    unittest.main()
