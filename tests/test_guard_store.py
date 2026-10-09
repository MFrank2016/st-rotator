"""GuardStore：待轮换清单 + 最近轮换日期的原子 JSON 持久化。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.guard import GuardError, GuardStore


class GuardStoreTest(unittest.TestCase):
    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.last_rotate_date(), "")

    def test_add_pending_dedupes_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            store = GuardStore.load(path)
            added = store.add_pending(["A", "B", "A", ""])
            self.assertEqual(added, ["A", "B"])
            self.assertEqual(store.pending(), ["A", "B"])
            # 再次加入已存在的账号 → 不新增
            self.assertEqual(store.add_pending(["B", "C"]), ["C"])
            self.assertEqual(store.pending(), ["A", "B", "C"])
            # 落盘后可重读
            self.assertEqual(GuardStore.load(path).pending(), ["A", "B", "C"])

    def test_finish_rotation_clears_and_sets_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            store = GuardStore.load(path)
            store.add_pending(["A", "B"])
            store.finish_rotation("2026-10-09")
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.last_rotate_date(), "2026-10-09")
            reloaded = GuardStore.load(path)
            self.assertEqual(reloaded.pending(), [])
            self.assertEqual(reloaded.last_rotate_date(), "2026-10-09")

    def test_corrupt_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(GuardError):
                GuardStore.load(path)

    def test_wrong_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            path.write_text(json.dumps({"version": 2, "pending": []}), encoding="utf-8")
            with self.assertRaises(GuardError):
                GuardStore.load(path)

    def test_bad_pending_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.json"
            path.write_text(
                json.dumps({"version": 1, "pending": "A"}), encoding="utf-8"
            )
            with self.assertRaises(GuardError):
                GuardStore.load(path)


if __name__ == "__main__":
    unittest.main()
