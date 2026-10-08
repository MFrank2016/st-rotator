"""ejiema (易接码) SMS 客户端与验证码轮询的本地 HTTP 测试。

测试模式仿 test_autorenew_transport.py：ThreadingHTTPServer 起在线程里，
base 注入本地端口，由 server 罐头化纯文本响应并记录收到的请求路径。
绝不打真实 ejiema API。
"""

import http.server
import threading
import unittest
import urllib.parse
from typing import Callable

from st_rotator import sms


# ---------------------------------------------------------------- 本地 fake server

Responder = Callable[[str], tuple[int, bytes | None]]


def _handler_class(recorded: list[str], responder: Responder):
    """记录请求路径，按 responder 出罐头纯文本响应。"""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            recorded.append(self.path)
            status, payload = responder(self.path)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if payload is not None:
                self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return H


class _Server:
    """本地罐头 HTTP server：responder(path) -> (status, payload_bytes)。"""

    _server: http.server.ThreadingHTTPServer
    _thread: threading.Thread
    port: int
    recorded: list[str]

    def __init__(self, responder: Responder) -> None:
        self.recorded = []
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_class(self.recorded, responder)
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------- fake 时钟/睡眠


class _FakeClock:
    """单调递增假时钟：sleep 推进它，避免任何真实 sleep。"""

    t: float
    calls: list[float]

    def __init__(self) -> None:
        self.t = 0.0
        self.calls = []

    def __call__(self) -> float:
        self.calls.append(self.t)
        return self.t


class _RecordingSleep:
    _clock: _FakeClock
    calls: list[float]

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self._clock.t += seconds
        self.calls.append(seconds)


class _PendingSms:
    """get_msg 永远返回 [尚未收到]，只计数。其余 Protocol 方法提供最小实现。"""

    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def left_amount(self) -> float:
        return 0.0

    def get_phone(self, *, keyword: str) -> str:
        return "13800138000"

    def get_msg(self, phone: str, *, keyword: str) -> str:
        del phone, keyword  # 与 SmsTransport 签名一致；本 fake 只计数
        self.calls += 1
        return "[尚未收到]"


class _QueuedSms:
    """get_msg 依次弹罐头响应；用尽后永远 [尚未收到]。"""

    _responses: list[str]
    calls: list[tuple[str, str]]

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = []

    def left_amount(self) -> float:
        return 0.0

    def get_phone(self, *, keyword: str) -> str:
        return "13800138000"

    def get_msg(self, phone: str, *, keyword: str) -> str:
        self.calls.append((phone, keyword))
        return self._responses.pop(0) if self._responses else "[尚未收到]"


# ---------------------------------------------------------------- 测试


class EjiemaSmsTest(unittest.TestCase):
    _server: _Server | None = None

    def setUp(self) -> None:
        self._server = None

    def tearDown(self) -> None:
        if self._server is not None:
            self._server.close()

    def _sms(self, responder: Responder) -> sms.EjiemaSms:
        self._server = _Server(responder)
        return sms.EjiemaSms("tok", base=self._server.base)

    def _qs(self, index: int = 0) -> dict[str, list[str]]:
        assert self._server is not None
        path = self._server.recorded[index]
        return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)

    # 1. 纯函数：取正文里最后一个 6-8 位连续数字串
    def test_extract_code_returns_last_digits(self):
        self.assertEqual(sms.extract_code("【商汤】验证码123456"), "123456")
        self.assertEqual(
            sms.extract_code("验证码 123456，请勿泄露。您的验证码为 654321。"),
            "654321",
        )
        self.assertEqual(sms.extract_code("验证码 654321；备用 12345678"), "12345678")
        self.assertIsNone(sms.extract_code("未收到短信"))
        self.assertIsNone(sms.extract_code("验证码12345"))  # 不足 6 位
        self.assertIsNone(sms.extract_code("验证码1234567890"))  # 超过 8 位

    # 2. left_amount：解析纯文本余额（容忍尾随换行）；不可解析 → SmsError
    def test_left_amount_parses(self):
        def responder(path: str) -> tuple[int, bytes | None]:
            return 200, "12.34\n".encode()

        client = self._sms(responder)
        self.assertEqual(client.left_amount(), 12.34)
        self.assertEqual(self._qs()["code"], ["leftAmount"])
        self.assertEqual(self._qs()["token"], ["tok"])

        def responder_bad(path: str) -> tuple[int, bytes | None]:
            return 200, "abc".encode()

        bad = self._sms(responder_bad)
        with self.assertRaises(sms.SmsError):
            bad.left_amount()

    # 3. 任意带 "ERROR:" 的响应体（含非开头子串）→ SmsError(detail=原文)
    def test_error_prefix_raises_sms_error(self):
        def responder(path: str) -> tuple[int, bytes | None]:
            if "leftAmount" in path:
                return 200, "ERROR:缺少参数：token".encode()
            return 200, "balance ERROR: 余额不足".encode()

        client = self._sms(responder)
        with self.assertRaises(sms.SmsError) as cm:
            client.left_amount()
        self.assertEqual(cm.exception.detail, "ERROR:缺少参数：token")
        with self.assertRaises(sms.SmsError) as cm:
            client.get_phone(keyword="商汤")
        self.assertEqual(cm.exception.detail, "balance ERROR: 余额不足")

    # 4. getPhone：keyWord 必须百分号编码后进 query（服务端看到 %E5%95%86%E6%B1%A4）
    def test_get_phone_urlencodes_keyword(self):
        def responder(path: str) -> tuple[int, bytes | None]:
            return 200, "13800138000".encode()

        client = self._sms(responder)
        self.assertEqual(client.get_phone(keyword="商汤"), "13800138000")
        assert self._server is not None
        path = self._server.recorded[0]
        self.assertIn("code=getPhone", path)
        self.assertIn("token=tok", path)
        self.assertIn(urllib.parse.quote("商汤"), path)  # 百分号编码形式
        self.assertNotIn("商汤", path)  # 不允许裸中文进 query
        self.assertEqual(self._qs()["keyWord"], ["商汤"])

    # 5. getMsg：返回原文（含 [尚未收到] 标记也原样透传），code/phone/keyWord 正确
    def test_get_msg_returns_raw_text(self):
        raw = "【商汤】验证码 123456，5分钟内有效。[尚未收到]"

        def responder(path: str) -> tuple[int, bytes | None]:
            return 200, raw.encode()

        client = self._sms(responder)
        got = client.get_msg("13800138000", keyword="商汤")
        self.assertEqual(got, raw)  # 原样返回，不剥标记
        self.assertEqual(self._qs()["code"], ["getMsg"])
        self.assertEqual(self._qs()["phone"], ["13800138000"])
        self.assertEqual(self._qs()["keyWord"], ["商汤"])


class WaitSmsCodeTest(unittest.TestCase):
    # 6. 轮询：首次 [尚未收到] → 二次出现验证码 → 返回最后一个 6-8 位数字串
    def test_wait_sms_code_polls_and_returns(self):
        queued = _QueuedSms(["[尚未收到]", "【商汤】验证码 654321，请勿泄露。"])
        clock = _FakeClock()
        sleeper = _RecordingSleep(clock)
        got = sms.wait_sms_code(
            queued,
            "13800138000",
            keyword="商汤",
            interval=5.0,
            timeout=60.0,
            clock=clock,
            sleep=sleeper,
        )
        self.assertEqual(got, "654321")
        self.assertEqual(queued.calls, [("13800138000", "商汤")] * 2)
        self.assertEqual(sleeper.calls, [5.0])
        self.assertEqual(clock.t, 5.0)

    # 7. 超时：按注入时钟 elapsed >= timeout → None，轮询有界不挂死
    def test_wait_sms_code_times_out_returns_none(self):
        pending = _PendingSms()
        clock = _FakeClock()
        sleeper = _RecordingSleep(clock)
        got = sms.wait_sms_code(
            pending,
            "13800138000",
            keyword="商汤",
            interval=5.0,
            timeout=12.0,
            clock=clock,
            sleep=sleeper,
        )
        self.assertIsNone(got)
        # t 推进到 15.0 后才触发超时：轮询 4 次、睡眠 3 次，绝不真实 sleep
        self.assertEqual(pending.calls, 4)
        self.assertEqual(sleeper.calls, [5.0, 5.0, 5.0])
        self.assertEqual(clock.t, 15.0)

    # 8. 注入的 clock/sleep 被真实使用：睡眠间隔、时钟推进全部来自注入对象
    def test_wait_sms_code_uses_injected_clock_and_sleep(self):
        pending = _PendingSms()
        clock = _FakeClock()
        sleeper = _RecordingSleep(clock)
        sms.wait_sms_code(
            pending,
            "13800138000",
            keyword="商汤",
            interval=5.0,
            timeout=12.0,
            clock=clock,
            sleep=sleeper,
        )
        self.assertEqual(clock.calls[0], 0.0)  # 起始读数来自注入时钟
        self.assertEqual(sleeper.calls, [5.0, 5.0, 5.0])  # 每次 sleep 用 interval
        self.assertEqual(clock.calls[-1], 15.0)  # 超时判断用 clock 增量
        self.assertGreater(len(clock.calls), 2)


if __name__ == "__main__":
    unittest.main()
