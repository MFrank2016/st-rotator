"""SubKeyManager：状态快照只含掩码，明文只能按 id 单条取回。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from st_rotator.subkeys import SubKeyManager


class SnapshotMaskingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = SubKeyManager(
            Path(self._tmp.name) / "subkeys.json", autosave_interval=0
        )
        self.addCleanup(self.manager.close)
        self.sub = self.manager.create("app-a", note="")

    def test_snapshot_has_no_plaintext_key(self) -> None:
        item = self.manager.snapshot()[0]
        self.assertNotIn("key", item)
        self.assertEqual(item["masked"], self.sub.masked)
        self.assertNotIn(self.sub.key, json.dumps(item))

    def test_reveal_returns_plaintext_by_id(self) -> None:
        self.assertEqual(self.manager.reveal(self.sub.id), self.sub.key)

    def test_reveal_unknown_id_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.manager.reveal("no-such-key")

    def test_mutations_look_up_under_lock(self) -> None:
        """查找与修改必须同临界区：_require 被调用时锁必须已持有。"""
        seen: list[bool] = []
        original = self.manager._require

        def spy(identifier: str):
            owned = getattr(self.manager._lock, "_is_owned", None)
            if owned is not None:
                seen.append(bool(owned()))
            return original(identifier)

        self.manager._require = spy  # type: ignore[method-assign]
        self.manager.update(self.sub.id, name="renamed")
        self.manager.set_enabled(self.sub.id, False)
        self.manager.reset_usage(self.sub.id)
        self.manager.reveal(self.sub.id)
        self.assertTrue(seen)
        self.assertTrue(all(seen), seen)

    def test_update_after_revoke_raises_key_error(self) -> None:
        self.manager.revoke(self.sub.id)
        with self.assertRaises(KeyError):
            self.manager.update(self.sub.id, name="x")

    def test_concurrent_revoke_never_raises_value_error(self) -> None:
        """并发撤销同一把 Key：只允许 KeyError（ui 捕获它），绝不能是 ValueError。"""
        sub = self.manager.create("dup", note="")
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def worker() -> None:
            barrier.wait()
            try:
                self.manager.revoke(sub.id)
            except KeyError:
                pass
            except BaseException as exc:  # noqa: BLE001 - 只关心意外类型
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIsNone(self.manager.find(sub.id))


if __name__ == "__main__":
    unittest.main()
