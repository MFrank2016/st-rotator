"""cmd_tray 端口 bind 失败路径：已 start() 的 worker 必须停掉，rotator 关闭。"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from st_rotator import cli


class _Worker:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Rotator:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeSubKeyManager:
    def __init__(self, *args, **kwargs) -> None:
        pass


class TrayBindFailureTest(unittest.TestCase):
    def test_bind_failure_stops_started_workers(self) -> None:
        auto, replenish, leak = _Worker(), _Worker(), _Worker()
        rotator = _Rotator()
        config = SimpleNamespace(console_token="")

        def fake_build_runtime(_args, *, default_log_file=None):
            # store 非 None 才会走 _wire_replenish / build_leak_guard 分支
            return (SimpleNamespace(), config, lambda _m: None, rotator, None)

        def fake_wire(console, *_args, **_kwargs):
            console.replenish = replenish

        tray_mod = types.ModuleType("st_rotator.tray")
        tray_mod.acquire_single_instance = lambda: True  # type: ignore[attr-defined]
        tray_mod.TrayApp = object  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(
                config=str(cfg),
                token="t" * 16,
                host="127.0.0.1",
                port=8899,
                verbose=False,
                log_file=None,
                browser=None,
                window_size=None,
            )
            with (
                mock.patch.dict(sys.modules, {"st_rotator.tray": tray_mod}),
                mock.patch.object(cli, "_build_runtime", fake_build_runtime),
                mock.patch.object(cli, "build_quota_service", lambda _cfg: None),
                mock.patch.object(cli, "_load_registry_for", lambda *a, **k: None),
                mock.patch.object(cli, "build_auto_renew", lambda *a, **k: auto),
                mock.patch.object(cli, "_wire_replenish", fake_wire),
                mock.patch.object(cli, "build_leak_guard", lambda *a, **k: leak),
                mock.patch("st_rotator.subkeys.SubKeyManager", _FakeSubKeyManager),
                mock.patch(
                    "st_rotator.proxy.create_server", side_effect=OSError("port busy")
                ),
            ):
                rc = cli.cmd_tray(args)

        self.assertEqual(rc, 1)
        self.assertTrue(auto.stopped)
        self.assertTrue(replenish.stopped)
        self.assertTrue(leak.stopped)
        self.assertTrue(rotator.closed)


if __name__ == "__main__":
    unittest.main()
