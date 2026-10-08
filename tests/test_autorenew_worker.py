"""AutoRenewWorker（定时检测 Key 失效并自动轮换）的单元测试。

全部使用 Fake：不访问真实商汤。

- ``_FakeLogin``：按 user 记录调用，可按配置对所有/特定 user 抛错。
- ``_FakeKeys``：KeyTransport 三方法，记录调用（含顺序 ops），按配置返回/抛错。
- ``probe`` 桩：按 key 前缀返回 (verdict, detail)。
- ``persist`` 记录 spy：记录 (账号名, 该账号全部 key 列表, 新 key 明文)。
- ``accounts`` 返回真实 ``AccountConfig``。
"""

import threading
import time
import unittest
from unittest import mock

from st_rotator import autorenew
from st_rotator.autorenew import AutoRenewWorker, KeyInfo
from st_rotator.config import AccountConfig
from st_rotator.quota import QuotaAuthError, QuotaUnavailable, TokenBundle

TOKEN = TokenBundle(
    access_token="tok-plain-abc",
    refresh_token="refresh-xyz",
    expires_in=10800,
    acquired_at=0.0,
)


class _FakeLogin:
    """登录 Fake：记录 (user, password)；exc_for 按 user 抛错，exc 全量抛错。"""

    def __init__(self, *, bundle=TOKEN, exc=None, exc_for=None):
        self.bundle = bundle
        self.exc = exc
        self.exc_for = dict(exc_for or {})
        self.calls = []

    def __call__(self, user: str, password: str) -> TokenBundle:
        self.calls.append((user, password))
        error = self.exc_for.get(user, self.exc)
        if error is not None:
            raise error
        return self.bundle


class _FakeKeys:
    """KeyTransport 三方法的 Fake：记录调用（含顺序 ops），按配置返回/抛错。"""

    def __init__(self):
        self.ops = []
        self.list_calls = []
        self.create_calls = []
        self.delete_calls = []
        self.list_result = []
        self.created = KeyInfo(
            id="k-new",
            displayname="auto",
            api_key="sk-new-plain",
            key_type="API_KEY_TYPE_TOKEN_PLAN",
            create_time="t-new",
        )
        self.create_exc = None
        self.list_exc = None
        self.delete_exc = None

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        self.ops.append("list")
        self.list_calls.append(
            {
                "token": access_token,
                "key_type": key_type,
                "page_size": page_size,
                "page_token": page_token,
            }
        )
        if self.list_exc is not None:
            raise self.list_exc
        return list(self.list_result)

    def create_key(self, access_token, *, displayname, key_type):
        self.ops.append("create")
        self.create_calls.append(
            {"token": access_token, "displayname": displayname, "key_type": key_type}
        )
        if self.create_exc is not None:
            raise self.create_exc
        return self.created

    def delete_key(self, access_token, *, key_id):
        self.ops.append("delete")
        self.delete_calls.append({"token": access_token, "key_id": key_id})
        if self.delete_exc is not None:
            raise self.delete_exc


class AutoRenewWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_calls = []
        self.log_lines = []

    # ------------------------------------------------------------ 辅助
    def _probe_stub(self, verdicts):
        """按 key 前缀返回 verdict 的 probe；记录被探测的 key 列表。"""
        calls = []

        def probe(key: str) -> tuple[str, str]:
            calls.append(key)
            verdict, detail = verdicts.get(key, ("ok", "ok"))
            return verdict, detail

        return probe, calls

    def _logs(self):
        line = []
        self.log_lines.append(line)

        def log(msg: str) -> None:
            line.append(msg)

        return log

    def _worker(
        self, *, accounts, probe=None, login=None, keys=None, persist=None, **kwargs
    ):
        self.log_sink = self._logs()
        return AutoRenewWorker(
            accounts=lambda: list(accounts),
            probe=probe or (lambda _k: ("ok", "ok")),
            login=login or _FakeLogin(),
            keys=keys or _FakeKeys(),
            persist=persist or self._record_persist,
            log=self.log_sink,
            **kwargs,
        )

    def _record_persist(self, name, keys_list, new_key) -> None:
        self.persist_calls.append((name, list(keys_list), new_key))

    # ------------------------------------------------------------ 1. 导入
    def test_module_importable_and_has_run_once(self):
        """RED：ImportError；GREEN：可导入且有 run_once。"""
        from st_rotator.autorenew import AutoRenewWorker as W

        self.assertTrue(callable(W))
        self.assertTrue(hasattr(W, "run_once"))

    # ------------------------------------------------------------ 2. 健康账号
    def test_healthy_account_all_ok_probes_every_key_no_persist(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-a", "sk-b", "sk-c"], user="u1", password="p1"
        )
        probe, probe_calls = self._probe_stub(
            {"sk-a": ("ok", "ok"), "sk-b": ("ok", "ok"), "sk-c": ("ok", "ok")}
        )
        login = _FakeLogin()
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        self.assertEqual(self.persist_calls, [])
        self.assertEqual(login.calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(probe_calls, ["sk-a", "sk-b", "sk-c"])  # 覆盖每一把 key
        self.assertEqual(
            worker.account_status()["账号1"]["status"], AutoRenewWorker.STATUS_OK
        )

    # ------------------------------------------------------------ 3. 遇 invalid 轮换
    def test_invalid_key_triggers_full_rotation_with_correct_args(self):
        account = AccountConfig(
            name="账号1",
            api_keys=["sk-ok", "sk-invalid", "sk-after"],
            user="u1",
            password="p1",
        )
        probe, probe_calls = self._probe_stub(
            {"sk-ok": ("ok", "ok"), "sk-invalid": ("invalid", "401 未授权")}
        )
        login = _FakeLogin()
        keys = _FakeKeys()
        keys.list_result = [
            KeyInfo(
                id="old-1",
                displayname="旧key",
                api_key="sk-old-plain",
                key_type="API_KEY_TYPE_TOKEN_PLAN",
                create_time="t-old",
            )
        ]
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        # probe 在第一个 invalid 处停止，不探后面的 key
        self.assertEqual(probe_calls, ["sk-ok", "sk-invalid"])
        # 登录被调用且参数正确
        self.assertEqual(login.calls, [("u1", "p1")])
        # 平台操作顺序：先 delete-all 后 create（先清空再建唯一一把）
        self.assertEqual(keys.ops, ["list", "delete", "create"])
        self.assertEqual(
            keys.delete_calls, [{"token": TOKEN.access_token, "key_id": "old-1"}]
        )
        self.assertEqual(keys.create_calls[0]["displayname"], "auto")
        self.assertEqual(keys.create_calls[0]["key_type"], "API_KEY_TYPE_TOKEN_PLAN")
        self.assertEqual(keys.create_calls[0]["token"], TOKEN.access_token)
        # persist 收到 (账号名, 该账号全部 key, 新 key)
        self.assertEqual(
            self.persist_calls,
            [("账号1", ["sk-ok", "sk-invalid", "sk-after"], "sk-new-plain")],
        )
        self.assertEqual(
            worker.account_status()["账号1"]["status"], AutoRenewWorker.STATUS_OK
        )
        entry = worker.account_status()["账号1"]
        self.assertNotIn("sk-invalid", entry["message"])
        self.assertNotIn(TOKEN.access_token, entry["message"])

    # ------------------------------------------------------------ 4. QuotaAuthError
    def test_login_auth_error_no_platform_calls_password_error(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        probe, _ = self._probe_stub({"sk-bad": ("invalid", "401")})
        login = _FakeLogin(exc=QuotaAuthError("用户名或密码错误"))
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        entry = worker.account_status()["账号1"]
        self.assertEqual(entry["status"], AutoRenewWorker.STATUS_PASSWORD_ERROR)
        self.assertEqual(entry["message"], "用户名或密码错误")
        self.assertEqual(keys.ops, [])  # 不做任何平台调用
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 5. 其它异常 → check_error
    def test_login_runtime_error_is_check_error_not_password_error(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        probe, _ = self._probe_stub({"sk-bad": ("invalid", "401")})
        login = _FakeLogin(exc=RuntimeError("boom"))
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        entry = worker.account_status()["账号1"]
        self.assertEqual(entry["status"], AutoRenewWorker.STATUS_CHECK_ERROR)
        self.assertNotEqual(entry["status"], AutoRenewWorker.STATUS_PASSWORD_ERROR)
        self.assertIn("RuntimeError", entry["message"])
        self.assertLessEqual(len(entry["message"]), 200)
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 6. QuotaUnavailable → 静默
    def test_login_unavailable_silent(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        probe, _ = self._probe_stub({"sk-bad": ("invalid", "401")})
        login = _FakeLogin(exc=QuotaUnavailable("未安装 jwcrypto"))
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        entry = worker.account_status()["账号1"]
        self.assertEqual(entry["status"], AutoRenewWorker.STATUS_UNAVAILABLE)
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])
        self.assertEqual(self.log_lines[0], [])  # 静默：无任何日志

    # ------------------------------------------------------------ 7. delete 失败 → 不 persist
    def test_delete_key_failure_skips_persist(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        probe, _ = self._probe_stub({"sk-bad": ("invalid", "401")})
        login = _FakeLogin()
        keys = _FakeKeys()
        keys.list_result = [
            KeyInfo(
                id="old-1",
                displayname="old",
                api_key="sk-old",
                key_type="API_KEY_TYPE_TOKEN_PLAN",
                create_time="t1",
            )
        ]
        keys.delete_exc = autorenew.KeyApiError(500, "server error")
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        entry = worker.account_status()["账号1"]
        self.assertEqual(entry["status"], AutoRenewWorker.STATUS_CHECK_ERROR)
        # 删除失败 → 不继续 create，persist 不应被调用
        self.assertEqual(keys.ops, ["list", "delete"])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 8. 全部 unknown
    def test_all_unknown_no_rotation_status_ok(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-x", "sk-y"], user="u1", password="p1"
        )
        probe, probe_calls = self._probe_stub(
            {"sk-x": ("unknown", "429 限流"), "sk-y": ("unknown", "5xx")}
        )
        login = _FakeLogin()
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        self.assertEqual(probe_calls, ["sk-x", "sk-y"])
        self.assertEqual(login.calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])
        self.assertEqual(
            worker.account_status()["账号1"]["status"], AutoRenewWorker.STATUS_OK
        )

    # ------------------------------------------------------------ 9. 无凭据 / 无 key
    def test_no_credentials_or_keys_skips_zero_network(self):
        no_cred = AccountConfig(
            name="无凭据", api_keys=["sk-1"], user="", password="p1"
        )
        no_some = AccountConfig(
            name="无密码", api_keys=["sk-2"], user="u2", password=""
        )
        no_keys = AccountConfig(
            name="无key", api_keys=["sk-3"], user="u3", password="p3"
        )
        no_keys.api_keys = []  # AccountConfig 不允许空 api_keys，构造后手工置空
        probe, probe_calls = self._probe_stub({})
        login = _FakeLogin()
        keys = _FakeKeys()
        worker = self._worker(
            accounts=[no_cred, no_some, no_keys], probe=probe, login=login, keys=keys
        )
        worker.run_once()
        statuses = worker.account_status()
        for name in ("无凭据", "无密码", "无key"):
            self.assertEqual(statuses[name]["status"], AutoRenewWorker.STATUS_NO_CRED)
        self.assertEqual(statuses["无key"]["message"], "账号无 Key")
        self.assertEqual(probe_calls, [])  # 零网络
        self.assertEqual(login.calls, [])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])

    # ------------------------------------------------------------ 10. 账号隔离
    def test_account_a_failure_does_not_block_account_b(self):
        a = AccountConfig(
            name="A", api_keys=["sk-A-ok", "sk-A-bad"], user="uA", password="pA"
        )
        b = AccountConfig(name="B", api_keys=["sk-B-bad"], user="uB", password="pB")
        probe, _ = self._probe_stub(
            {
                "sk-A-ok": ("ok", "ok"),
                "sk-A-bad": ("invalid", "401"),
                "sk-B-bad": ("invalid", "401"),
            }
        )
        login = _FakeLogin(exc_for={"uA": QuotaAuthError("密码错")})
        keys = _FakeKeys()
        worker = self._worker(accounts=[a, b], probe=probe, login=login, keys=keys)
        worker.run_once()
        statuses = worker.account_status()
        self.assertEqual(statuses["A"]["status"], AutoRenewWorker.STATUS_PASSWORD_ERROR)
        self.assertEqual(statuses["B"]["status"], AutoRenewWorker.STATUS_OK)
        # B 轮换成功，A 不轮换
        self.assertEqual(self.persist_calls, [("B", ["sk-B-bad"], "sk-new-plain")])
        self.assertEqual(login.calls, [("uA", "pA"), ("uB", "pB")])
        # account_status 返回深拷贝
        statuses["A"]["status"] = "hacked"
        self.assertEqual(
            worker.account_status()["A"]["status"],
            AutoRenewWorker.STATUS_PASSWORD_ERROR,
        )

    # ------------------------------------------------------------ 11. start/stop 线程
    def test_start_stop_daemon_thread_run_before_wait(self):
        account = AccountConfig(name="空账号", api_keys=["sk-1"], user="", password="")
        worker = self._worker(accounts=[account], interval=0.05)
        counter = {"n": 0}
        original = worker.run_once

        def _counting():
            counter["n"] += 1
            original()

        worker.run_once = _counting
        worker.start()
        self.assertIsNotNone(worker._thread)
        self.assertTrue(worker._thread.daemon)
        deadline = time.monotonic() + 2.0
        while counter["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()
        # _loop 在等待前先 run_once 一次，且 interval 周期循环继续运行
        self.assertGreaterEqual(counter["n"], 2)
        self.assertFalse(worker._thread.is_alive())  # stop() 的 join(timeout=5) 已返回

    # ------------------------------------------------------------ 12. password_error 复清
    def test_password_error_cleared_when_next_round_all_ok(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        verdicts = {"sk-bad": ("invalid", "401 未授权")}
        probe, probe_calls = self._probe_stub(verdicts)
        login = _FakeLogin(exc=QuotaAuthError("密码错"))
        keys = _FakeKeys()
        worker = self._worker(accounts=[account], probe=probe, login=login, keys=keys)
        worker.run_once()
        self.assertEqual(
            worker.account_status()["账号1"]["status"],
            AutoRenewWorker.STATUS_PASSWORD_ERROR,
        )
        # 下一轮 key 全部有效 → 不轮换、状态清回 ok
        verdicts["sk-bad"] = ("ok", "ok")
        worker.run_once()
        self.assertEqual(login.calls, [("u1", "p1")])  # 第二轮未再登录
        self.assertEqual(probe_calls, ["sk-bad", "sk-bad"])
        self.assertEqual(keys.ops, [])
        self.assertEqual(self.persist_calls, [])
        self.assertEqual(
            worker.account_status()["账号1"]["status"], AutoRenewWorker.STATUS_OK
        )


class _PlatformKeys(_FakeKeys):
    """模拟真实平台：list 返回平台当前全部 key（含刚 create 的），delete 从平台移除。"""

    def __init__(self, initial=None):
        super().__init__()
        self.platform = list(initial or [])

    def list_keys(self, access_token, *, key_type=None, page_size=50, page_token=None):
        self.ops.append("list")
        self.list_calls.append(
            {
                "token": access_token,
                "key_type": key_type,
                "page_size": page_size,
                "page_token": page_token,
            }
        )
        return list(self.platform)

    def create_key(self, access_token, *, displayname, key_type):
        self.ops.append("create")
        self.create_calls.append(
            {"token": access_token, "displayname": displayname, "key_type": key_type}
        )
        self.platform.append(self.created)
        return self.created

    def delete_key(self, access_token, *, key_id):
        self.ops.append("delete")
        self.delete_calls.append({"token": access_token, "key_id": key_id})
        self.platform = [k for k in self.platform if k.id != key_id]


class RotationRegressionTest(AutoRenewWorkerTest):
    """回归：真实平台 list 会包含刚 create 的 key，轮换绝不能删掉新建的 key（历史 bug）。"""

    def test_rotation_never_deletes_newly_created_key(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-bad"], user="u1", password="p1"
        )
        probe, _ = self._probe_stub({"sk-bad": ("invalid", "401")})
        keys = _PlatformKeys(
            [
                KeyInfo(
                    id="old-1",
                    displayname="old",
                    api_key="sk-old",
                    key_type="API_KEY_TYPE_TOKEN_PLAN",
                    create_time="t1",
                )
            ]
        )
        worker = self._worker(
            accounts=[account], probe=probe, login=_FakeLogin(), keys=keys
        )
        worker.run_once()
        deleted = [c["key_id"] for c in keys.delete_calls]
        self.assertEqual(deleted, ["old-1"])
        self.assertNotIn("k-new", deleted)
        self.assertEqual([k.id for k in keys.platform], ["k-new"])
        self.assertEqual(keys.ops, ["list", "delete", "create"])
        self.assertEqual(self.persist_calls, [("账号1", ["sk-bad"], "sk-new-plain")])
        self.assertEqual(
            worker.account_status()["账号1"]["status"], AutoRenewWorker.STATUS_OK
        )


class CleanupTest(AutoRenewWorkerTest):
    """30 分钟一轮的「多余 Key」清理：删除非当前配置的 key，保留当前配置的。"""

    def _key(self, key_id, api_key):
        return KeyInfo(
            id=key_id,
            displayname="k",
            api_key=api_key,
            key_type="API_KEY_TYPE_TOKEN_PLAN",
            create_time="t",
        )

    def test_removes_extra_keys_keeps_configured(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-cur"], user="u1", password="p1"
        )
        keys = _FakeKeys()
        keys.list_result = [
            self._key("cur", "sk-cur"),
            self._key("extra1", "sk-extra1"),
            self._key("extra2", "sk-extra2"),
        ]
        worker = self._worker(accounts=[account], keys=keys)
        worker.run_cleanup()
        self.assertEqual([c["key_id"] for c in keys.delete_calls], ["extra1", "extra2"])
        self.assertEqual(keys.ops, ["list", "delete", "delete"])

    def test_no_extra_keys_no_delete(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-cur"], user="u1", password="p1"
        )
        keys = _FakeKeys()
        keys.list_result = [self._key("cur", "sk-cur")]
        worker = self._worker(accounts=[account], keys=keys)
        worker.run_cleanup()
        self.assertEqual(keys.delete_calls, [])
        self.assertEqual(keys.ops, ["list"])

    def test_cleanup_skips_without_credentials_zero_network(self):
        account = AccountConfig(name="无凭据", api_keys=["sk-1"], user="", password="")
        keys = _FakeKeys()
        login = _FakeLogin()
        worker = self._worker(accounts=[account], keys=keys, login=login)
        worker.run_cleanup()
        self.assertEqual(keys.ops, [])
        self.assertEqual(login.calls, [])

    def test_cleanup_auth_error_marks_password_error(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-cur"], user="u1", password="p1"
        )
        keys = _FakeKeys()
        login = _FakeLogin(exc=QuotaAuthError("用户名或密码错误"))
        worker = self._worker(accounts=[account], keys=keys, login=login)
        worker.run_cleanup()
        self.assertEqual(
            worker.account_status()["账号1"]["status"],
            AutoRenewWorker.STATUS_PASSWORD_ERROR,
        )
        self.assertEqual(keys.ops, [])

    def test_cleanup_transient_error_does_not_clobber_status(self):
        account = AccountConfig(
            name="账号1", api_keys=["sk-cur"], user="u1", password="p1"
        )
        worker = self._worker(
            accounts=[account],
            keys=_FakeKeys(),
            login=_FakeLogin(exc=RuntimeError("boom")),
        )
        worker.run_cleanup()
        self.assertNotIn("账号1", worker.account_status())

    def test_loop_runs_cleanup_periodically(self):
        account = AccountConfig(name="空账号", api_keys=["sk-1"], user="", password="")
        worker = self._worker(accounts=[account], interval=0.05, cleanup_interval=0.05)
        calls = {"run": 0, "clean": 0}
        worker.run_once = lambda: calls.__setitem__("run", calls["run"] + 1)
        worker.run_cleanup = lambda: calls.__setitem__("clean", calls["clean"] + 1)
        worker.start()
        deadline = time.monotonic() + 2.0
        while calls["clean"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()
        self.assertGreaterEqual(calls["clean"], 2)


if __name__ == "__main__":
    unittest.main()
