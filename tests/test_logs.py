"""build_logger 在不可写路径下优雅降级的行为测试。"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler

from st_rotator.logs import LogBuffer, build_logger, make_log_sink


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
