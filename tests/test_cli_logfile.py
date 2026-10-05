"""_build_runtime 在 --log-file 父目录无法创建时优雅降级的行为测试。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

from st_rotator.cli import _build_runtime


def _write_config(directory: str) -> str:
    """写一份最小可用配置，返回其路径。"""
    path = os.path.join(directory, "config.json")
    payload = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [
            {
                "name": "a",
                "api_keys": ["k"],
                "rpm_limit": 2,
                "max_concurrency": 4,
                "weight": 1,
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class BuildRuntimeLogFileTest(unittest.TestCase):
    def test_uncreatable_log_parent_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_config(tmp)
            # 父级是一个普通文件 -> mkdir 会抛 OSError/NotADirectoryError
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("not a directory")

            args = argparse.Namespace(
                persist=False,
                config=config_path,
                rate_mode=None,
                qps=0.0,
                log_file=os.path.join(blocker, "sub", "x.log"),
                log_lines=500,
                verbose=False,
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                _store, _config, _sink, rotator, _buffer = _build_runtime(args)

            try:
                # 必须优雅降级：不抛异常，且给出可读警告
                self.assertIn("无法创建日志目录", stderr.getvalue())
            finally:
                rotator.close()


if __name__ == "__main__":
    unittest.main()
