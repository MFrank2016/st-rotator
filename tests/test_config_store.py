"""ConfigStore.save：实例锁串行化、临时文件名唯一、fsync 后再原子替换。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.config import ConfigStore

RAW = {
    "base_url": "http://127.0.0.1:9/v1",
    "accounts": [{"name": "a", "api_keys": ["sk-a"]}],
}


class ConfigStoreSaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cfg = self.dir / "config.json"
        self.cfg.write_text(json.dumps(RAW), encoding="utf-8")
        self.store = ConfigStore.load(self.cfg)

    def test_save_is_atomic_and_leaves_no_tmp(self) -> None:
        self.store.save()
        self.assertEqual(json.loads(self.cfg.read_text(encoding="utf-8")), RAW)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        self.assertEqual(list(self.dir.glob(".config.json-*")), [])

    def test_save_fsyncs_before_replace(self) -> None:
        fsynced: list[int] = []

        def spy(fd: int) -> None:
            fsynced.append(fd)

        with mock.patch("st_rotator.config.os.fsync", side_effect=spy):
            self.store.save()
        self.assertEqual(len(fsynced), 1)

    @unittest.skipIf(sys.platform == "win32", "Windows 不支持 POSIX 权限位")
    def test_save_preserves_existing_file_mode(self) -> None:
        """mkstemp 默认 0600：保存不能悄悄改变原文件的可读范围。"""
        self.cfg.chmod(0o640)
        self.store.save()
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o640)

    def test_tmp_name_is_unique_per_save(self) -> None:
        seen: list[str] = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(str(src))
            real_replace(src, dst)

        with mock.patch("st_rotator.config.os.replace", side_effect=spy):
            self.store.save()
            self.store.save()
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(set(seen)), 2)
        self.assertNotIn(str(self.dir / "config.json.tmp"), seen)

    def test_concurrent_saves_are_serialized(self) -> None:
        real_replace = os.replace
        guard = threading.Lock()
        state = {"inflight": 0, "max": 0}

        def slow_replace(src, dst):
            with guard:
                state["inflight"] += 1
                state["max"] = max(state["max"], state["inflight"])
            try:
                time.sleep(0.01)  # 放大竞争窗口：无锁时 max 会 > 1
                real_replace(src, dst)
            finally:
                with guard:
                    state["inflight"] -= 1

        with mock.patch("st_rotator.config.os.replace", side_effect=slow_replace):
            threads = [threading.Thread(target=self.store.save) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(state["max"], 1)
        self.assertEqual(json.loads(self.cfg.read_text(encoding="utf-8")), RAW)


if __name__ == "__main__":
    unittest.main()
