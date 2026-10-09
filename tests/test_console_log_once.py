"""控制台日志去重：一次操作只应产生一行日志。

回归用例：``ConsoleState._log`` 曾同时 ``self.buffer.append`` 又调用
``rotator._log``（= 同一个日志出口 sink，而 sink 也写同一个环形缓冲），
导致同一条日志在控制台出现两遍（例如保存运行参数时的
``[控制台] 运行参数已更新：…``）。
"""

import unittest

from st_rotator.config import Config
from st_rotator.logs import LogBuffer, make_log_sink
from st_rotator.ui import ConsoleState


class _SinkRotator:
    """最小 rotator：``_log`` 指向真实 sink，与生产接线一致。"""

    def __init__(self, config, sink):
        self.config = config
        self._log = sink


class _Store:
    def save(self) -> None:  # pragma: no cover - 只占位
        pass


class ConsoleLogOnceTest(unittest.TestCase):
    def _state(self, buf: LogBuffer) -> ConsoleState:
        cfg = Config.from_dict(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [{"name": "a", "api_keys": ["sk-1"]}],
            }
        )
        sink = make_log_sink(None, buf, None)
        return ConsoleState(  # type: ignore[arg-type]
            store=_Store(), rotator=_SinkRotator(cfg, sink), buffer=buf
        )

    def test_save_and_log_emits_single_line(self) -> None:
        buf = LogBuffer()
        state = self._state(buf)
        state._save_and_log("测试事件")  # noqa: SLF001
        hits = [it["text"] for it in buf.since(0)[1] if "测试事件" in it["text"]]
        self.assertEqual(len(hits), 1, f"日志重复：{hits}")

    def test_log_without_sink_still_reaches_buffer(self) -> None:
        """出口缺失（无 _log）时仍要能落到环形缓冲。"""
        buf = LogBuffer()
        cfg = Config.from_dict(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [{"name": "a", "api_keys": ["sk-1"]}],
            }
        )

        class _NoSinkRotator:
            pass

        state = ConsoleState(  # type: ignore[arg-type]
            store=_Store(), rotator=_NoSinkRotator(), buffer=buf
        )
        state._log("孤立事件")  # noqa: SLF001
        hits = [it["text"] for it in buf.since(0)[1] if "孤立事件" in it["text"]]
        self.assertEqual(len(hits), 1, f"日志缺失：{hits}")


if __name__ == "__main__":
    unittest.main()
