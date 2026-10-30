"""trayicons：缓存键覆盖全部渲染参数、图标文件原子写入。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from st_rotator.trayicons import ensure_icons


class CacheKeyTest(unittest.TestCase):
    def _names(self, directory: Path) -> dict[str, str]:
        with mock.patch("st_rotator.trayicons.build_ico", return_value=b"ico"):
            return {state: path.name for state, path in ensure_icons(directory).items()}

    def test_same_params_reuse_the_same_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = self._names(directory)
            second = self._names(directory)
            self.assertEqual(first, second)
            self.assertEqual(len(list(directory.glob("tray-*.ico"))), len(first))

    def test_darken_factor_changes_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before = self._names(directory)
            with mock.patch("st_rotator.trayicons._DARKEN_FACTOR", 0.5):
                after = self._names(directory)
            self.assertNotEqual(before["healthy"], after["healthy"])

    def test_renderer_version_changes_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before = self._names(directory)
            with mock.patch("st_rotator.trayicons._ICON_RENDER_VERSION", 99):
                after = self._names(directory)
            self.assertNotEqual(before["healthy"], after["healthy"])


class AtomicWriteTest(unittest.TestCase):
    def test_icons_are_written_via_tmp_then_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            seen: list[Path] = []
            real_replace = os.replace

            def spy(src, dst):
                seen.append(Path(src))
                real_replace(src, dst)

            with mock.patch("st_rotator.trayicons.os.replace", side_effect=spy):
                icons = ensure_icons(directory)
            self.assertTrue(seen)
            self.assertTrue(all(path.suffix == ".tmp" for path in seen))
            self.assertTrue(all(not path.exists() for path in seen))  # tmp 已被替换掉
            self.assertTrue(all(path.exists() for path in icons.values()))

    def test_failed_replace_leaves_no_partial_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch(
                "st_rotator.trayicons.os.replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    ensure_icons(directory)
            self.assertEqual(list(directory.glob("tray-*.ico")), [])
            self.assertEqual(list(directory.glob(".tray-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
