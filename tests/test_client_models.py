"""模型清单缓存的单元测试：拉取失败后按 ttl 退避，不再每次轮询都重拉重记。

不访问真实网络：``StRotator.models`` 换成计数 Fake，时钟用 patch 控制。
"""

from __future__ import annotations

import unittest
from unittest import mock

from st_rotator.client import StRotator
from st_rotator.config import AccountConfig, Config, RateControlConfig


class _CountingModels:
    """计数 Fake：每次调用都抛异常，模拟上游不可达。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        raise RuntimeError("上游不可达")


class ModelsCacheTest(unittest.TestCase):
    def test_failure_backs_off_within_ttl(self) -> None:
        logs: list[str] = []
        fake_models = _CountingModels()
        now = [100.0]
        config = Config(
            accounts=[AccountConfig(name="a", api_keys=["k-0123456789abcdef"])],
            rate_control=RateControlConfig(mode="off"),
        )
        with mock.patch("st_rotator.client.time.monotonic", side_effect=lambda: now[0]):
            rotator = StRotator(config, logger=logs.append)
            rotator.models = fake_models  # type: ignore[method-assign]

            result = rotator.available_models(ttl=300.0)
            self.assertEqual(fake_models.calls, 1)
            self.assertEqual(result["models"], [])

            # ttl 内再轮询：不再打上游，也不再追加失败日志
            now[0] += 100.0
            result = rotator.available_models(ttl=300.0)
            self.assertEqual(fake_models.calls, 1)
            self.assertEqual(
                len([line for line in logs if "拉取模型清单失败" in line]), 1
            )

            # ttl 过期后允许再试一次
            now[0] += 300.0
            rotator.available_models(ttl=300.0)
            self.assertEqual(fake_models.calls, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
