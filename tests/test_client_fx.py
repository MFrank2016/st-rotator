"""一换一（flash-lite 烧点）的全局并发额度：检查与占用必须原子。

模块级 ``_FX_ACTIVE`` 是进程级额度，测试之间要手工复位。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from st_rotator import client as client_module
from st_rotator.client import StRotator
from st_rotator.config import (
    AccountConfig,
    Config,
    FlashLiteExchangeConfig,
    RateControlConfig,
)


class _InlineThread:
    """把 ``threading.Thread`` 换成立即同步执行，去掉时序不确定性。"""

    def __init__(self, target=None, args=(), daemon=None, name=None) -> None:
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)


class _BoomThread:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("can't start new thread")


def _rotator(max_workers: int) -> StRotator:
    config = Config(
        accounts=[
            AccountConfig(name=f"acct-{i}", api_keys=[f"sk-{i}"]) for i in range(5)
        ],
        rate_control=RateControlConfig(mode="off"),
        flash_lite_exchange=FlashLiteExchangeConfig(
            enabled=True, max_workers=max_workers, min_interval_s=0.0
        ),
    )
    return StRotator(config, logger=lambda _msg: None)


class FlashLiteConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        client_module._FX_ACTIVE = 0
        client_module._FX_LAST.clear()
        self.addCleanup(setattr, client_module, "_FX_ACTIVE", 0)
        self.addCleanup(client_module._FX_LAST.clear)

    def _keys(self, rotator: StRotator):
        return list(rotator.pool.keys)

    def test_slot_is_taken_atomically_never_exceeds_max_workers(self) -> None:
        rotator = _rotator(max_workers=2)
        calls: list[str] = []
        rotator._flash_lite_burn = lambda key, fx: calls.append(key.account)  # type: ignore[method-assign]
        with mock.patch.object(client_module, "available_mb", return_value=None):
            with mock.patch.object(client_module.threading, "Thread", _InlineThread):
                for key in self._keys(rotator):
                    rotator._maybe_flash_lite_exchange(key, "quota exhausted")
        # 上限 2：只有前两个拿到额度；旧实现把自增放在线程里，5 个都会通过检查
        self.assertEqual(len(calls), 2)
        self.assertEqual(client_module._FX_ACTIVE, 2)

    def test_thread_start_failure_releases_slot(self) -> None:
        rotator = _rotator(max_workers=2)
        rotator._flash_lite_burn = lambda key, fx: None  # type: ignore[method-assign]
        with mock.patch.object(client_module, "available_mb", return_value=None):
            with mock.patch.object(client_module.threading, "Thread", _BoomThread):
                rotator._maybe_flash_lite_exchange(
                    self._keys(rotator)[0], "quota exhausted"
                )
        self.assertEqual(client_module._FX_ACTIVE, 0)

    def test_released_slot_can_be_reused(self) -> None:
        rotator = _rotator(max_workers=1)
        calls: list[str] = []
        rotator._flash_lite_burn = lambda key, fx: calls.append(key.account)  # type: ignore[method-assign]
        keys = self._keys(rotator)
        with mock.patch.object(client_module, "available_mb", return_value=None):
            with mock.patch.object(client_module.threading, "Thread", _InlineThread):
                rotator._maybe_flash_lite_exchange(keys[0], "quota exhausted")
                rotator._maybe_flash_lite_exchange(keys[1], "quota exhausted")
                self.assertEqual(len(calls), 1)  # 额度被占满
                with client_module._FX_LOCK:  # 模拟烧点线程结束归还
                    client_module._FX_ACTIVE -= 1
                rotator._maybe_flash_lite_exchange(keys[1], "quota exhausted")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
