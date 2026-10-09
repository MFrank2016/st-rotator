"""ConsoleState 补号（replenish）装配层的单元测试。

snapshot 测试：Fake rotator + 注入带 account_status() 的假 worker / 假 registry；
set_options 测试：真实 ConfigStore（tempfile 落盘真实 JSON）验证补号参数落盘，
且 sms_token 只写盘、绝不回显。不访问真实易码 / 商汤。
"""

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.config import ConfigStore
from st_rotator.registry import Registry
from st_rotator.ui import ConsoleState


def _write_config(tmp: str, *, replenish: dict | None = None) -> Path:
    """写一份真实配置，可选 replenish 节。"""
    accounts = [
        {
            "name": "账号1",
            "api_keys": ["sk-1"],
            "user": "u1",
            "phone": "138",
            "password": "p1",
        },
        {"name": "账号2", "api_keys": ["sk-2"]},
    ]
    cfg: dict = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": accounts,
    }
    if replenish is not None:
        cfg["replenish"] = replenish
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _load_store(tmp: str, **kw: object) -> ConfigStore:
    return ConfigStore.load(_write_config(tmp, **kw))  # type: ignore[arg-type]


class _FakeWorker:
    """带 account_status() 的假补号 worker，用于 snapshot 注入。"""

    def __init__(self, status: dict) -> None:
        self._status = status

    def account_status(self) -> dict:
        return self._status


class _FakeRegistry:
    """假注册表：spend_state() / used_phones() / count_registrations() / rotation_count()。"""

    def __init__(
        self,
        spend: dict,
        used: list[str],
        *,
        registrations_ok: int = 0,
        rotations: int = 0,
    ) -> None:
        self._spend = dict(spend)
        self._used = frozenset(used)
        self._registrations_ok = registrations_ok
        self._rotations = rotations

    def spend_state(self) -> dict:
        return dict(self._spend)

    def used_phones(self) -> frozenset:
        return self._used

    def count_registrations(self, success: bool | None = None) -> int:
        return self._registrations_ok if success else 0

    def rotation_count(self) -> int:
        return self._rotations


class _FakePool:
    def summary(self) -> dict[str, int]:
        return {"total": 0, "healthy": 0, "cooldown": 0, "invalid": 0, "inflight": 0}

    def snapshot(self) -> list:
        return []


class _FakeLimiter:
    def stats(self) -> dict[str, float | str | int]:
        return {
            "mode": "off",
            "rate": 0.0,
            "min_rate": 0.0,
            "max_rate": 0.0,
            "penalties": 0,
            "raises": 0,
        }


class _FakeRotator:
    """snapshot / set_options 用的假 rotator：不发网络。"""

    def __init__(self, config) -> None:
        self.config = config
        self.pool = _FakePool()
        self.limiter = _FakeLimiter()
        self.upstream_attempts = 0

    def probe_key(self, key: str) -> tuple[str, str]:
        return ("ok", "未校验")

    def available_models(
        self, *, refresh: bool = False, ttl: float = 300.0
    ) -> dict[str, object]:
        return {"models": []}

    def _log(self, message: str) -> None:  # pragma: no cover
        pass


def _console(
    tmp: str,
    *,
    replenish=None,
    registry=None,
    auto_renew=None,
    replenish_cfg: dict | None = None,
) -> ConsoleState:
    store = _load_store(tmp, replenish=replenish_cfg)
    rotator = _FakeRotator(store.config)
    return ConsoleState(  # type: ignore[arg-type]
        store=store,
        rotator=rotator,
        replenish=replenish,
        registry=registry,
        auto_renew=auto_renew,
    )


class SnapshotReplenishTest(unittest.TestCase):
    """RED：snapshot() 当前没有 replenish_* 键；GREEN：无 worker 返回 {} / 安全默认。"""

    def test_no_worker_returns_empty_replenish_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp)
            self.assertEqual(console.snapshot()["replenish_status"], {})

    def test_snapshot_includes_worker_replenish_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker({"_replenish": {"status": "ok", "message": ""}})
            console = _console(tmp, replenish=worker)
            self.assertEqual(
                console.snapshot()["replenish_status"],
                {"_replenish": {"status": "ok", "message": ""}},
            )

    def test_snapshot_options_omit_sms_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(
                tmp,
                replenish_cfg={
                    "sms_token": "sekret",
                    "enabled": True,
                    "keyword": "商汤",
                },
            )
            opts = console.snapshot()["options"]["replenish"]
            self.assertNotIn("sms_token", opts)
            self.assertTrue(opts["enabled"])
            self.assertEqual(opts["keyword"], "商汤")
            self.assertEqual(opts["target_count"], 0)
            self.assertEqual(opts["key_type"], "API_KEY_TYPE_TOKEN_PLAN")

    def test_snapshot_replenish_state_computes_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            auto_renew = _FakeWorker(
                {"账号1": {"status": "password_error", "message": ""}}
            )
            registry = _FakeRegistry(
                {"date": "2026-01-01", "start_balance": 10.0, "last_balance": 8.0},
                ["138", "139"],
            )
            console = _console(
                tmp,
                replenish=_FakeWorker({}),
                auto_renew=auto_renew,
                registry=registry,
                replenish_cfg={"target_count": 3},
            )
            state = console.snapshot()["replenish_state"]
            self.assertEqual(state["available"], 1)  # 2 账号 - 1 密码错误
            self.assertEqual(state["target"], 3)
            self.assertEqual(
                state["spend"],
                {"date": "2026-01-01", "start_balance": 10.0, "last_balance": 8.0},
            )
            self.assertEqual(state["used_phones"], 2)

    def test_snapshot_replenish_state_reports_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            auto_renew = _FakeWorker(
                {
                    "账号1": {"status": "password_error", "message": ""},
                    "账号2": {"status": "ok", "message": ""},
                }
            )
            registry = _FakeRegistry({}, [], registrations_ok=7, rotations=3)
            console = _console(
                tmp,
                replenish=_FakeWorker({}),
                auto_renew=auto_renew,
                registry=registry,
                replenish_cfg={"target_count": 3},
            )
            state = console.snapshot()["replenish_state"]
            self.assertEqual(state["invalid_accounts"], 1)
            self.assertEqual(state["registrations_ok"], 7)
            self.assertEqual(state["rotations"], 3)

    def test_snapshot_replenish_state_defaults_without_worker_and_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp, replenish_cfg={"target_count": 4})
            state = console.snapshot()["replenish_state"]
            self.assertEqual(state["available"], 2)  # 无状态，全部可用
            self.assertEqual(state["target"], 4)
            self.assertEqual(state["spend"], {})
            self.assertEqual(state["used_phones"], 0)
            self.assertEqual(state["invalid_accounts"], 0)
            self.assertEqual(state["registrations_ok"], 0)
            self.assertEqual(state["rotations"], 0)


class SetOptionsReplenishTest(unittest.TestCase):
    """set_options(payload["replenish"])：镜像 flash_lite，校验后落盘，sms_token 不回显。"""

    def test_set_options_updates_replenish_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            store = ConfigStore.load(path)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            resp = console.set_options(
                {
                    "replenish": {
                        "enabled": True,
                        "target_count": 5,
                        "daily_spend_cap": 8.5,
                        "keyword": "x",
                        "sms_token": "tok123",
                    }
                }
            )
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.payload["ok"])
            cfg = console.config.replenish
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.target_count, 5)
            self.assertAlmostEqual(cfg.daily_spend_cap, 8.5)
            self.assertEqual(cfg.keyword, "x")
            self.assertEqual(cfg.sms_token, "tok123")
            # 落盘
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["replenish"]["target_count"], 5)
            self.assertEqual(reloaded["replenish"]["sms_token"], "tok123")
            # 不回显
            self.assertNotIn("sms_token", console.snapshot()["options"]["replenish"])

    def test_set_options_does_not_echo_sms_token_in_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            resp = console.set_options(
                {"replenish": {"enabled": True, "sms_token": "super-secret-token"}}
            )
            self.assertEqual(resp.status, 200)
            self.assertNotIn("super-secret-token", json.dumps(resp.payload))

    def test_set_options_rejects_invalid_replenish(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            resp = console.set_options({"replenish": {"target_count": -1}})
            self.assertEqual(resp.status, 400)
            self.assertFalse(resp.payload["ok"])
            self.assertEqual(console.config.replenish.target_count, 0)

    def test_set_options_ignores_empty_sms_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(tmp)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            resp = console.set_options(
                {"replenish": {"enabled": True, "sms_token": ""}}
            )
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.payload["ok"])
            self.assertTrue(console.config.replenish.enabled)
            self.assertEqual(console.config.replenish.sms_token, "")


class _StoppableWorker:
    """可停止的假 worker：用于验证 _sync_replenish 的停机 / 重建 / 热更新分支。"""

    def __init__(self) -> None:
        self.stopped = False

    def account_status(self) -> dict:
        return {}

    def stop(self) -> None:
        self.stopped = True


class _ReconfigWorker(_StoppableWorker):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


class SyncReplenishTest(unittest.TestCase):
    """set_options 落盘后调用 _sync_replenish，让运行中的 worker 实时采用新配置。"""

    def test_sync_stops_worker_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _StoppableWorker()
            console = _console(
                tmp,
                replenish=worker,
                replenish_cfg={"enabled": False, "target_count": 3, "sms_token": "t"},
            )
            console._sync_replenish()
            self.assertTrue(worker.stopped)
            self.assertIsNone(console.replenish)

    def test_sync_reconfigures_running_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _ReconfigWorker()
            console = _console(
                tmp,
                replenish=worker,
                replenish_cfg={
                    "enabled": True,
                    "target_count": 4,
                    "sms_token": "t",
                    "keyword": "商汤",
                    "daily_spend_cap": 1.0,
                    "sms_poll_interval": 5,
                    "sms_poll_timeout": 60,
                    "interval_seconds": 600,
                    "key_name": "auto",
                    "key_type": "API_KEY_TYPE_TOKEN_PLAN",
                },
            )
            console._sync_replenish()
            self.assertIs(console.replenish, worker)
            self.assertEqual(worker.calls[-1]["target"], 4)
            self.assertEqual(worker.calls[-1]["daily_spend_cap"], 1.0)
            self.assertEqual(worker.calls[-1]["keyword"], "商汤")

    def test_sync_rebuilds_running_worker_on_token_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = _StoppableWorker()
            rebuilt = _StoppableWorker()
            console = _console(
                tmp,
                replenish=old,
                replenish_cfg={"enabled": True, "target_count": 3, "sms_token": "new"},
            )
            console.replenish_factory = lambda: rebuilt
            console._sync_replenish(rebuild=True)
            self.assertTrue(old.stopped)
            self.assertIs(console.replenish, rebuilt)

    def test_sync_builds_worker_when_missing_and_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = _StoppableWorker()
            console = _console(
                tmp,
                replenish_cfg={"enabled": True, "target_count": 3, "sms_token": "t"},
            )
            console.replenish_factory = lambda: built
            console._sync_replenish()
            self.assertIs(console.replenish, built)

    def test_set_options_replenish_triggers_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _ReconfigWorker()
            console = _console(
                tmp,
                replenish=worker,
                replenish_cfg={"enabled": True, "target_count": 1, "sms_token": "t"},
            )
            resp = console.set_options({"replenish": {"target_count": 6}})
            self.assertEqual(resp.status, 200)
            self.assertEqual(worker.calls[-1]["target"], 6)


class RegistrationsApiTest(unittest.TestCase):
    """GET /api/replenish/registrations：分页 + 过滤 + 缺 registry 时的安全空值。"""

    def test_route_returns_page_from_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry.load(Path(tmp) / "state.json")
            reg.record_registration(
                created_at=1.0,
                phone="13800000001",
                username="a1",
                password="p",
                is_new=True,
                password_reset=False,
                success=True,
                reason="",
                detail="",
            )
            reg.record_registration(
                created_at=2.0,
                phone="13800000002",
                username="a2",
                password="p",
                is_new=False,
                password_reset=True,
                success=True,
                reason="",
                detail="",
            )
            reg.record_registration(
                created_at=3.0,
                phone="13800000003",
                username="a3",
                password="p",
                is_new=True,
                password_reset=False,
                success=False,
                reason="captcha_required",
                detail="需要滑块",
            )
            console = _console(tmp, registry=reg)
            resp = console.handle(
                "GET",
                "/api/replenish/registrations",
                query={"page": ["1"], "size": ["2"], "status": ["ok"], "q": ["138"]},
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.payload["total"], 2)
            self.assertEqual(len(resp.payload["items"]), 2)
            self.assertEqual(resp.payload["items"][0]["phone"], "13800000002")
            self.assertEqual(resp.payload["page"], 1)
            self.assertEqual(resp.payload["size"], 2)

    def test_route_without_registry_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp)
            resp = console.handle("GET", "/api/replenish/registrations")
            self.assertEqual(resp.status, 200)
            self.assertEqual(
                resp.payload, {"items": [], "total": 0, "page": 1, "size": 20}
            )


class SnapshotSmsTokenFlagsTest(unittest.TestCase):
    """snapshot options.replenish：sms_token_configured / sms_token_ok。"""

    def test_snapshot_exposes_token_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp, replenish_cfg={"sms_token": "tok", "enabled": True})
            console.sms_token_ok = False
            opts = console.snapshot()["options"]["replenish"]
            self.assertTrue(opts["sms_token_configured"])
            self.assertFalse(opts["sms_token_ok"])

    def test_snapshot_token_ok_defaults_none_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp)
            opts = console.snapshot()["options"]["replenish"]
            self.assertFalse(opts["sms_token_configured"])
            self.assertIsNone(opts["sms_token_ok"])


class SmsCheckRunner:
    """set_options 保存时用的假易码传输：``left_amount`` 可控成功 / 抛错。"""

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def left_amount(self) -> float:
        self.calls.append("left")
        if not self.ok:
            raise RuntimeError("token 无效")
        return 99.0


class _WakeWorker(_StoppableWorker):
    """记录 wake / run_once 的假 worker：验证对账只唤醒、不在请求线程里跑补号。"""

    def __init__(self) -> None:
        super().__init__()
        self.woke = 0
        self.ran = 0

    def wake(self) -> None:
        self.woke += 1

    def run_once(self) -> None:
        self.ran += 1

class SaveTimeReplenishTest(unittest.TestCase):
    """保存补号配置后的 token 校验与立即对账（失败不破坏落盘）。"""

    def test_save_with_valid_token_marks_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(
                tmp,
                replenish_cfg={"enabled": True, "target_count": 0, "sms_token": "old"},
            )
            console.sms_factory = lambda token: SmsCheckRunner(ok=True)
            resp = console.set_options({"replenish": {"sms_token": "newtok"}})
            self.assertEqual(resp.status, 200)
            self.assertTrue(console.sms_token_ok)
            self.assertEqual(console.config.replenish.sms_token, "newtok")
            self.assertNotIn("易码 Token 校验失败", resp.payload["message"])

    def test_save_with_invalid_token_keeps_save_marks_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            store = ConfigStore.load(path)
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            console.sms_factory = lambda token: SmsCheckRunner(ok=False)
            resp = console.set_options(
                {"replenish": {"enabled": True, "target_count": 5, "sms_token": "bad"}}
            )
            self.assertEqual(resp.status, 200)
            self.assertFalse(console.sms_token_ok)
            self.assertIn("易码 Token 校验失败", resp.payload["message"])
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["replenish"]["sms_token"], "bad")

    def test_save_with_valid_token_marks_unavailable_using_existing_token(self):
        # 未提交新 token 时，用已存 token 校验
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(
                tmp,
                replenish_cfg={
                    "enabled": True,
                    "target_count": 0,
                    "sms_token": "stored",
                },
            )
            console.sms_factory = lambda token: SmsCheckRunner(ok=False)
            resp = console.set_options({"replenish": {"enabled": True}})
            self.assertEqual(resp.status, 200)
            self.assertFalse(console.sms_token_ok)

    def test_save_with_no_token_sets_ok_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(
                tmp, replenish_cfg={"enabled": True, "target_count": 0, "sms_token": ""}
            )
            console.sms_factory = lambda token: SmsCheckRunner(ok=True)
            resp = console.set_options({"replenish": {"enabled": True}})
            self.assertEqual(resp.status, 200)
            self.assertIsNone(console.sms_token_ok)

    def test_save_triggers_immediate_reconcile_when_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _WakeWorker()
            logs: list[str] = []
            store = _load_store(
                tmp,
                replenish={"enabled": True, "target_count": 3, "sms_token": "tok"},
            )
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            console.replenish = worker
            console._log = logs.append
            resp = console.set_options({"replenish": {"target_count": 5}})
            self.assertEqual(resp.status, 200)
            self.assertTrue(any("已开启自动补号" in line for line in logs), logs)
            self.assertEqual(worker.woke, 1)  # 唤醒 worker
            self.assertEqual(worker.ran, 0)  # 不在请求线程里跑（避免与 worker 并发）

    def test_reconcile_failure_never_breaks_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _load_store(
                tmp,
                replenish={
                    "enabled": True,
                    "target_count": 5,
                    "sms_token": "tok",
                },
            )
            rotator = _FakeRotator(store.config)
            console = ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]
            console.replenish = object()  # 无 run_once -> 对账失败必须被吞掉
            resp = console.set_options({"replenish": {"target_count": 6}})
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.payload["ok"])


if __name__ == "__main__":
    unittest.main()
