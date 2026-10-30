"""Phase 4 新增测试：B/C 组缺陷回归。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from st_rotator import quota, sms
from st_rotator.config import AccountConfig, ConfigError, CooldownConfig
from st_rotator.demo import FakeUpstream
from st_rotator.guard import GuardStore, LeakGuardWorker
from st_rotator.keypool import KeyPool
from st_rotator.limiter import AdaptiveRateLimiter
from st_rotator.registry import Registry, RegistryError
from st_rotator.transport import HttpClient, Request


# ---------------------------------------------------------------- B6 demo
class FakeUpstreamBodyTest(unittest.TestCase):
    """非 UTF-8 / 非对象 body 不能让假上游抛异常。"""

    def _handle(self, body):
        upstream = FakeUpstream(flaky_rate=0.0, latency=(0.0, 0.0))
        return upstream.handle(Request("POST", "/chat/completions", {}, body))

    def test_non_utf8_body_falls_back_to_empty_payload(self) -> None:
        resp = self._handle(b"\xff\xfe\x00")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"choices", resp.content)

    def test_non_object_json_body_falls_back(self) -> None:
        resp = self._handle(b"[1,2,3]")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"choices", resp.content)

    def test_stream_flag_still_works(self) -> None:
        resp = self._handle(b'{"stream": true}')
        self.assertIn(b"[DONE]", resp.content)


# ---------------------------------------------------------------- B7 / B8 config
class ConfigTypeValidationTest(unittest.TestCase):
    def test_null_api_key_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            AccountConfig.from_dict({"name": "a", "api_keys": [None]})

    def test_numeric_api_keys_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            AccountConfig.from_dict({"name": "a", "api_keys": 123})

    def test_string_api_key_still_accepted(self) -> None:
        cfg = AccountConfig.from_dict({"name": "a", "api_keys": "sk-1"})
        self.assertEqual(cfg.api_keys, ["sk-1"])

    def test_cooldown_string_value_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            CooldownConfig.from_dict({"base": "60"})

    def test_cooldown_bool_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            CooldownConfig.from_dict({"jitter": True})

    def test_cooldown_int_accepted(self) -> None:
        self.assertEqual(CooldownConfig.from_dict({"base": 30}).base, 30)


# ---------------------------------------------------------------- B9 / B10 registry
class RegistryShapeTest(unittest.TestCase):
    def _write(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / "replenish.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_bool_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"version": True, "used_phones": []})
            with self.assertRaises(RegistryError):
                Registry.load(path)

    def test_float_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"version": 1.0, "used_phones": []})
            with self.assertRaises(RegistryError):
                Registry.load(path)

    def test_partial_new_record_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "version": 1,
                    "used_phones": [],
                    "spend": {"date": "", "start_balance": None, "last_balance": None},
                    "registrations": [{"success": True, "phone": "130"}],
                },
            )
            with self.assertRaises(RegistryError):
                Registry.load(path)

    def test_full_new_record_accepted(self) -> None:
        record = {
            "created_at": 1.0,
            "phone": "130",
            "username": "u",
            "password": "p",
            "is_new": True,
            "password_reset": False,
            "success": True,
            "reason": "",
            "detail": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "version": 1,
                    "used_phones": [],
                    "spend": {"date": "", "start_balance": None, "last_balance": None},
                    "registrations": [record],
                },
            )
            self.assertEqual(Registry.load(path).registrations()[0]["phone"], "130")


# ---------------------------------------------------------------- B11 quota JWKS
class JwksTest(unittest.TestCase):
    def _public_key(self, body: bytes):
        transport = quota.HttpQuotaTransport()

        class _Resp:
            def read(self) -> bytes:
                return body

        with (
            mock.patch.object(transport, "_open", lambda *a, **k: _Resp()),
            mock.patch.object(
                quota, "_jwcrypto", lambda: (object(), object(), object())
            ),
        ):
            return transport._public_key()

    def test_missing_kid_raises_quota_http_error(self) -> None:
        body = json.dumps({"keys": [{"kid": "other", "n": "x", "e": "AQAB"}]}).encode()
        with self.assertRaises(quota.QuotaHTTPError) as ctx:
            self._public_key(body)
        self.assertIn("JWKS", str(ctx.exception))

    def test_missing_keys_field_raises_quota_http_error(self) -> None:
        with self.assertRaises(quota.QuotaHTTPError):
            self._public_key(b'{"nokeys": []}')

    def test_invalid_json_raises_quota_http_error(self) -> None:
        with self.assertRaises(quota.QuotaHTTPError):
            self._public_key(b"not json")


# ---------------------------------------------------------------- B14 trayicons
class TrayIconsCleanupTest(unittest.TestCase):
    def test_only_generated_icons_are_cleaned(self) -> None:
        from st_rotator.trayicons import ensure_icons

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            user_file = directory / "tray-custom.ico"
            user_file.write_bytes(b"user")
            stale = directory / "tray-healthy-0000aaaa.ico"
            stale.write_bytes(b"old")
            icons = ensure_icons(directory)
            self.assertTrue(user_file.exists())  # 调用方自己的文件不动
            self.assertFalse(stale.exists())  # 本函数生成的旧图标被清掉
            self.assertTrue(all(path.exists() for path in icons.values()))


# ---------------------------------------------------------------- B16 / B17 sms
class SmsParsingTest(unittest.TestCase):
    def test_extract_code_ignores_fullwidth_digits(self) -> None:
        self.assertIsNone(sms.extract_code("验证码：１２３４５６"))
        self.assertEqual(sms.extract_code("验证码 123456"), "123456")

    def test_get_phone_is_stripped(self) -> None:
        class _Sms(sms.EjiemaSms):
            def _request(self, action, **params):
                return " 13800001111 \r\n"

        self.assertEqual(_Sms("tok").get_phone(), "13800001111")


# ---------------------------------------------------------------- B18 keypool
class AccountWindowPruneTest(unittest.TestCase):
    def test_to_dict_prunes_account_window(self) -> None:
        clock = [100.0]
        pool = KeyPool(
            [AccountConfig(name="a", api_keys=["sk-a"], rpm_limit=5)],
            clock=lambda: clock[0],
        )
        key = pool.find_key("sk-a")
        assert key is not None
        key.account_state._window.extend([100.0, 100.0])
        clock[0] = 400.0  # 远超 60s 窗口
        data = key.to_dict(clock[0])
        self.assertEqual(data["rpm_window"], "0/5")


# ---------------------------------------------------------------- B19 transport
class _RawWithConnection:
    status = 200

    def __init__(self, connection: str) -> None:
        self._connection = connection

    def getheaders(self):
        return []

    def getheader(self, name, default=None):
        return self._connection if name.lower() == "connection" else default

    def read(self) -> bytes:
        return b"ok"


class _Conn:
    def __init__(self, raw) -> None:
        self._raw = raw
        self.sock = object()
        self.closed = False

    def request(self, method, url, body=None, headers=None) -> None:
        pass

    def getresponse(self):
        return self._raw

    def close(self) -> None:
        self.closed = True


class ConnectionTokenTest(unittest.TestCase):
    def _run(self, connection_header: str):
        client = HttpClient("http://127.0.0.1:9/v1", timeout=1.0, connect_timeout=1.0)
        conn = _Conn(_RawWithConnection(connection_header))
        client._checkout = lambda: (conn, False)  # type: ignore[method-assign]
        resp = client.request("GET", "/models")
        self.assertEqual(resp.status, 200)
        return client, conn

    def test_keepalive_token_list_with_close_is_not_healthy(self) -> None:
        client, conn = self._run("keep-alive, close")
        self.assertTrue(conn.closed)  # 不放回连接池
        self.assertEqual(client._idle, [])

    def test_plain_keepalive_is_healthy(self) -> None:
        client, conn = self._run("keep-alive")
        self.assertFalse(conn.closed)
        self.assertEqual(len(client._idle), 1)


# ---------------------------------------------------------------- B20 ui profile dir
class ConsoleWindowProfileTest(unittest.TestCase):
    def test_profile_mkdir_failure_returns_reason(self) -> None:
        from st_rotator.ui import open_console_window

        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch("st_rotator.ui.find_app_browser", return_value="/bin/echo"):
                ok, note = open_console_window(
                    "http://127.0.0.1:1/", profile_dir=blocker
                )
        self.assertFalse(ok)
        self.assertIn("无法创建独立配置目录", note)


# ---------------------------------------------------------------- C21 guard
class NextRotateAtTest(unittest.TestCase):
    def _worker(self, now: float) -> LeakGuardWorker:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuardStore.load(Path(tmp) / "guard.json")
        return LeakGuardWorker(
            accounts=lambda: [],
            token_usage_last=lambda: None,
            credit_changes=lambda since, idle: [],
            rotate=lambda account: None,
            store=store,
            clock=lambda: now,
            rotate_hour=2,
            rotate_minute=30,
        )

    def test_next_rotate_is_today_when_not_yet_due(self) -> None:
        now = time.mktime((2026, 10, 9, 1, 0, 0, 0, 0, -1))
        expected = time.mktime((2026, 10, 9, 2, 30, 0, 0, 0, -1))
        self.assertEqual(self._worker(now)._next_rotate_at(), expected)

    def test_next_rotate_is_tomorrow_when_passed(self) -> None:
        now = time.mktime((2026, 10, 9, 15, 0, 0, 0, 0, -1))
        expected = time.mktime((2026, 10, 10, 2, 30, 0, 0, 0, -1))
        self.assertEqual(self._worker(now)._next_rotate_at(), expected)

    @unittest.skipUnless(hasattr(time, "tzset"), "需要 POSIX tzset")
    def test_next_rotate_uses_calendar_day_across_dst(self) -> None:
        """跨夏令时那天只有 23 小时：必须按日历加一天，而不是 +86400 秒。"""
        old_tz = os.environ.get("TZ")
        self.addCleanup(lambda: self._restore_tz(old_tz))
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        # 2026-03-28 13:00（次日 02:00 起进入夏令时）；轮换设在 12:00
        now = time.mktime((2026, 3, 28, 13, 0, 0, 0, 0, -1))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = LeakGuardWorker(
            accounts=lambda: [],
            token_usage_last=lambda: None,
            credit_changes=lambda since, idle: [],
            rotate=lambda account: None,
            store=GuardStore.load(Path(tmp.name) / "guard.json"),
            clock=lambda: now,
            rotate_hour=12,
            rotate_minute=0,
        )
        expected = time.mktime((2026, 3, 29, 12, 0, 0, 0, 0, -1))
        self.assertEqual(worker._next_rotate_at(), expected)

    @staticmethod
    def _restore_tz(old: str | None) -> None:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ---------------------------------------------------------------- C23 limiter
class LimiterHistoryTest(unittest.TestCase):
    def test_history_records_wall_clock(self) -> None:
        limiter = AdaptiveRateLimiter(1.0, clock=lambda: 123.0)
        before = time.time()
        limiter.on_rate_limited()
        after = time.time()
        monotonic_at, rate, wall = limiter.stats()["history"][-1]
        self.assertEqual(monotonic_at, 123.0)
        # round(...,3) 可能把墙钟向上取整越过 after，给 2s 容差
        self.assertAlmostEqual(wall, before, delta=2.0)
        self.assertLessEqual(wall, after + 2.0)
        self.assertGreater(rate, 0.0)


if __name__ == "__main__":
    unittest.main()
