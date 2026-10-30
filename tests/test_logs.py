"""build_logger 在不可写路径下优雅降级的行为测试。"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

from st_rotator.logs import (
    LogBuffer,
    build_logger,
    make_log_sink,
    read_log_tail,
)


def _has_file_handler(logger: logging.Logger) -> bool:
    return any(isinstance(h, RotatingFileHandler) for h in logger.handlers)


class BuildLoggerTest(unittest.TestCase):
    def test_unwritable_path_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "f")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("not a directory")

            # 路径的父级是一个普通文件 -> mkdir 会抛 OSError
            logger = build_logger(os.path.join(blocker, "sub", "x.log"))

            self.assertIsInstance(logger, logging.Logger)
            self.assertFalse(_has_file_handler(logger))

    def test_valid_path_attaches_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "r.log")
            logger = build_logger(log_path)

            self.assertTrue(_has_file_handler(logger))

            sink = make_log_sink(logger)
            sink("hello from test")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(os.path.isfile(log_path))
            with open(log_path, "r", encoding="utf-8") as handle:
                self.assertIn("hello from test", handle.read())

    def test_none_path_no_file_handler(self) -> None:
        logger = build_logger(None)
        self.assertIsInstance(logger, logging.Logger)
        self.assertFalse(_has_file_handler(logger))


class LogTailTest(unittest.TestCase):
    def test_tail_zero_returns_empty(self) -> None:
        buf = LogBuffer()
        buf.append("a")
        self.assertEqual(buf.tail(0), [])
        self.assertEqual(buf.tail(-5), [])

    def test_read_log_tail_returns_last_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.log")
            with open(path, "w", encoding="utf-8") as handle:
                for i in range(1000):
                    handle.write(f"line-{i}\n")
            tail = read_log_tail(path, 3)
            self.assertEqual([line.strip() for line in tail], ["line-997", "line-998", "line-999"])

    def test_read_log_tail_does_not_scan_whole_file(self) -> None:
        class _Counting:
            def __init__(self, handle) -> None:
                self._handle = handle
                self.bytes_read = 0

            def seek(self, *args):
                return self._handle.seek(*args)

            def tell(self):
                return self._handle.tell()

            def read(self, size=-1):
                data = self._handle.read(size)
                self.bytes_read += len(data)
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._handle.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.log")
            with open(path, "w", encoding="utf-8") as handle:
                for i in range(4000):
                    handle.write(f"line-{i:05d} padding padding padding\n")
            size = os.path.getsize(path)
            counter = {}

            real_open = Path.open

            def fake_open(self, *args, **kwargs):
                handle = _Counting(real_open(self, *args, **kwargs))
                counter["handle"] = handle
                return handle

            with mock.patch.object(Path, "open", fake_open):
                tail = read_log_tail(path, 2)
            self.assertEqual(len(tail), 2)
            self.assertLess(counter["handle"].bytes_read, size)

    def test_read_log_tail_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_log_tail(os.path.join(tmp, "nope.log"), 10), [])

    def test_read_log_tail_handles_rotation_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.log")
            Path(path).write_text("x\n", encoding="utf-8")
            with mock.patch.object(Path, "open", side_effect=FileNotFoundError):
                self.assertEqual(read_log_tail(path, 10), [])


class LogBufferTimestampTest(unittest.TestCase):
    _TS = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"

    def test_since_entries_carry_datetime(self) -> None:
        buf = LogBuffer()
        buf.append("hello")
        _cursor, items = buf.since(0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "hello")
        self.assertRegex(items[0]["time"], self._TS)

    def test_tail_entries_carry_datetime(self) -> None:
        buf = LogBuffer()
        buf.append("world")
        self.assertRegex(buf.tail(10)[0]["time"], self._TS)

if __name__ == "__main__":
    unittest.main()
