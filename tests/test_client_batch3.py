"""批次 3（client.py）：soft429 降速、模型清单锁、时钟统一、烧点超时、探测分类、非 JSON 响应。"""

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
from st_rotator.errors import DeadlineExceeded, RotatorError
from st_rotator.transport import Response


def _config(**kwargs) -> Config:
    return Config(
        accounts=[AccountConfig(name="a", api_keys=["k-0123456789abcdef"])],
        rate_control=RateControlConfig(mode="off"),
        max_attempts=kwargs.pop("max_attempts", 1),
        **kwargs,
    )


def _rotator(**kwargs) -> StRotator:
    return StRotator(_config(**kwargs), logger=lambda _m: None)


# ---------------------------------------------------------------- 1. soft429 降速
class _FxHttp:
    """第一次回 429（非 entitlement），之后回 200（给 probe_recovered 用）。"""

    def __init__(self, first_status: int = 429) -> None:
        self.first_status = first_status
        self.calls = 0

    def request(self, method, path, json_body=None, headers=None, deadline=None, **kw):
        self.calls += 1
        if self.calls == 1:
            body = b'{"error":{"message":"Requests rate limit exceeded"}}'
            return Response(self.first_status, {}, body)
        return Response(200, {}, b'{"usage":{"total_tokens":1}}')


def _fx(
    *, requests_per_trigger: int = 5, concurrency: int = 1, image_enabled: bool = False
) -> FlashLiteExchangeConfig:
    return FlashLiteExchangeConfig(
        enabled=True,
        requests_per_trigger=requests_per_trigger,
        concurrency=concurrency,
        image_enabled=image_enabled,
        yield_to_serve=False,
    )


class FlashLiteSoft429Test(unittest.TestCase):
    def setUp(self) -> None:
        self._old_active = client_module._FX_ACTIVE
        client_module._FX_ACTIVE = 0
        self.addCleanup(setattr, client_module, "_FX_ACTIVE", self._old_active)

    def _burn(self, http, fx):
        rotator = StRotator(_config(), client=http, logger=lambda _m: None)
        key = list(rotator.pool.keys)[0]
        with (
            mock.patch.object(client_module, "available_mb", return_value=None),
            mock.patch.object(client_module, "_fx_soft429_pause") as pause,
        ):
            rotator._flash_lite_burn(key, fx)
        return pause

    def test_soft429_triggers_pause(self) -> None:
        pause = self._burn(_FxHttp(first_status=429), _fx())
        pause.assert_called_once()

    def test_success_does_not_pause(self) -> None:
        pause = self._burn(
            _FxHttp(first_status=200), _fx(requests_per_trigger=1)
        )
        pause.assert_not_called()


# ---------------------------------------------------------------- 4. 烧点超时
class FlashLiteTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_active = client_module._FX_ACTIVE
        client_module._FX_ACTIVE = 0
        self.addCleanup(setattr, client_module, "_FX_ACTIVE", self._old_active)

    def test_image_slot_timeout_skips_request_without_leaking(self) -> None:
        http = _FxHttp(first_status=200)
        rotator = StRotator(_config(), client=http, logger=lambda _m: None)
        key = list(rotator.pool.keys)[0]
        fx = FlashLiteExchangeConfig(
            enabled=True,
            requests_per_trigger=5,
            concurrency=2,  # 批内 i=0 文本、i=1 图片
            image_enabled=True,
            multi_image_count=1,
            image_size=256,
            yield_to_serve=False,
        )
        sem = threading.Semaphore(0)  # 永远拿不到额度
        logs: list[str] = []
        rotator._log = logs.append  # type: ignore[method-assign]
        with (
            mock.patch.object(client_module, "available_mb", return_value=None),
            mock.patch.object(client_module, "_FX_IMG_SEM", sem),
            mock.patch.object(client_module, "_FX_IMG_WAIT", 0.05),  # 别真等 30s
            mock.patch.object(client_module, "_noise_png_b64", return_value="x"),
            mock.patch.object(client_module, "_fx_soft429_pause"),
        ):
            rotator._flash_lite_burn(key, fx)
        self.assertTrue(any("大图额度排队超时" in line for line in logs))
        # 只有文本那发 + probe 真正打到上游；图片那发被跳过
        self.assertEqual(http.calls, 2)
        self.assertEqual(sem._value, 0)  # 没拿到额度也不会误 release

    def test_stuck_burn_thread_ends_round(self) -> None:
        class _StuckThread:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                pass  # 不真正执行

            def join(self, timeout=None) -> None:
                pass

            def is_alive(self) -> bool:
                return True

        http = _FxHttp(first_status=200)
        rotator = StRotator(_config(), client=http, logger=lambda _m: None)
        key = list(rotator.pool.keys)[0]
        logs: list[str] = []
        rotator._log = logs.append  # type: ignore[method-assign]
        with (
            mock.patch.object(client_module, "available_mb", return_value=None),
            mock.patch.object(client_module.threading, "Thread", _StuckThread),
        ):
            rotator._flash_lite_burn(key, _fx())
        self.assertTrue(any("烧点线程未在" in line for line in logs))
        self.assertEqual(http.calls, 0)  # 没有继续打上游


# ---------------------------------------------------------------- 2. 模型清单锁
class ModelsLockTest(unittest.TestCase):
    def test_network_call_runs_outside_lock(self) -> None:
        rotator = _rotator()
        seen: list[bool] = []

        def fake_models() -> dict:
            acquired = rotator._models_lock.acquire(blocking=False)
            if acquired:
                rotator._models_lock.release()
            seen.append(acquired)
            return {"data": [{"id": "m1"}]}

        rotator.models = fake_models  # type: ignore[method-assign]
        out = rotator.available_models()
        self.assertEqual(seen, [True])  # 拉取时锁是空闲的
        self.assertEqual([m["id"] for m in out["models"]], ["m1"])
        self.assertFalse(out["cached"])
        # 缓存命中：不再调网络
        out2 = rotator.available_models()
        self.assertTrue(out2["cached"])
        self.assertEqual(len(seen), 1)


# ---------------------------------------------------------------- 3. 时钟统一
class DeadlineClockTest(unittest.TestCase):
    def test_check_deadline_uses_injected_clock(self) -> None:
        clock = [100.0]
        rotator = StRotator(
            _config(), clock=lambda: clock[0], logger=lambda _m: None
        )
        rotator._check_deadline(100.5)  # 未到点，不能抛
        clock[0] = 101.0
        with self.assertRaises(DeadlineExceeded):
            rotator._check_deadline(100.5)

    def test_clamp_deadline_uses_injected_clock(self) -> None:
        clock = [100.0]
        rotator = StRotator(
            _config(), clock=lambda: clock[0], logger=lambda _m: None
        )
        self.assertAlmostEqual(rotator._clamp_deadline(None, 100.5), 0.5)
        self.assertAlmostEqual(rotator._clamp_deadline(0.2, 100.5), 0.2)


# ---------------------------------------------------------------- 5. 探测分类
class _ProbeHttp:
    def __init__(self, get_status: int, post_status: int) -> None:
        self.get_status = get_status
        self.post_status = post_status

    def get(self, path, headers=None):
        return Response(self.get_status, {}, b"")

    def post(self, path, headers=None, json_body=None):
        return Response(self.post_status, {}, b'{"error":{"message":"x"}}')


class ProbeClassificationTest(unittest.TestCase):
    def _probe(self, get_status: int, post_status: int):
        rotator = StRotator(
            _config(), client=_ProbeHttp(get_status, post_status), logger=lambda _m: None
        )
        key = list(rotator.pool.keys)[0]
        return rotator.probe_key(key)

    def test_bad_request_on_fallback_is_not_invalid(self) -> None:
        verdict, _detail = self._probe(404, 400)
        self.assertEqual(verdict, "unknown")

    def test_auth_failure_on_fallback_is_invalid(self) -> None:
        verdict, _detail = self._probe(404, 401)
        self.assertEqual(verdict, "invalid")

    def test_success_on_fallback_is_ok(self) -> None:
        verdict, _detail = self._probe(404, 200)
        self.assertEqual(verdict, "ok")


# ---------------------------------------------------------------- 6. 非 JSON 2xx
class NonJsonOkResponseTest(unittest.TestCase):
    def test_non_json_2xx_wrapped_as_rotator_error(self) -> None:
        class _HtmlHttp:
            def request(self, method, path, json_body=None, headers=None, deadline=None, **kw):
                return Response(200, {}, b"<html>bad gateway</html>")

        rotator = StRotator(
            _config(max_attempts=1), client=_HtmlHttp(), logger=lambda _m: None
        )
        with self.assertRaises(RotatorError) as ctx:
            rotator.chat([{"role": "user", "content": "hi"}])
        self.assertIn("非 JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
