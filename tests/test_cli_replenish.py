"""cli.py 的自动补充账号（replenish）装配测试。

- ``build_replenish`` 的启用闸门：enabled + target_count>0 + sms_token 三者同时满足
  才创建 worker，否则返回 None；启用路径注入 Fake sms/authn/keys，零真实网络。
- ``build_replenish_persist`` 落账契约：真实 ConfigStore + 真实 StRotator +
  tempfile Registry 一起运转（同 PersistEndToEndTest 风格），断言
  config.json / store.config / rotator.config / pool / registry 五者一致，
  且密码以明文原样保留在磁盘。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from st_rotator.cli import build_replenish, build_replenish_persist  # type: ignore[misc]
from st_rotator.client import StRotator
from st_rotator.config import ConfigStore
from st_rotator.registry import Registry
from st_rotator.replenish import ReplenishWorker

NEW_KEY = "sk-new-plain-001"


class _Guard:
    """任何被调用的协议方法都立刻炸穿——证明启用路径没打真实网络。"""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError(f"{self._label}.{name} 不应被调用（无网络）")

        return _boom


def _make_runtime(
    tmp: str, *, replenish: dict[str, Any] | None = None
) -> tuple[Path, ConfigStore, StRotator, Registry]:
    path = Path(tmp) / "config.json"
    data: dict[str, Any] = {
        "base_url": "http://127.0.0.1:9/v1",
        "accounts": [
            {
                "name": "账号1",
                "api_keys": ["sk-existing-001"],
                "user": "u1",
                "phone": "13800000000",
                "password": "p1",
            }
        ],
    }
    if replenish is not None:
        data["replenish"] = replenish
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore.load(path)
    rotator = StRotator(store.config)
    registry = Registry(path.parent / "state.json", lock=threading.Lock())
    return path, store, rotator, registry


class BuildReplenishGateTest(unittest.TestCase):
    """启用闸门：enabled / target_count / sms_token 缺一即返回 None。"""

    def test_disabled_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": False, "target_count": 2, "sms_token": "tok"}
            )
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=lambda _m: None,
            )
            self.assertIsNone(worker)

    def test_enabled_without_token_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": True, "target_count": 2, "sms_token": ""}
            )
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=lambda _m: None,
            )
            self.assertIsNone(worker)

    def test_enabled_without_token_logs_startup_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": True, "target_count": 2, "sms_token": ""}
            )
            logs: list[str] = []
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=logs.append,
            )
            self.assertIsNone(worker)
            self.assertEqual(logs, ["[补号] 未配置易码 Token，补号未运行"])

    def test_disabled_does_not_log_missing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": False, "target_count": 2, "sms_token": ""}
            )
            logs: list[str] = []
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=logs.append,
            )
            self.assertIsNone(worker)
            self.assertEqual(logs, [])

    def test_enabled_without_target_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": True, "target_count": 0, "sms_token": "tok"}
            )
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=lambda _m: None,
            )
            self.assertIsNone(worker)

    def test_enabled_starts_worker(self):
        # target 等于现有可用账号数（1）→ 首轮即 idle，绝不会触碰 sms/authn/keys 假依赖
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(
                tmp, replenish={"enabled": True, "target_count": 1, "sms_token": "tok"}
            )
            logs: list[str] = []
            worker = build_replenish(
                store.config,
                store,
                rotator,
                registry,
                lock=threading.Lock(),
                sink=logs.append,
                status_source=lambda: {},
                sms=_Guard("sms"),
                authn=_Guard("authn"),
                keys=_Guard("keys"),
            )
            self.assertIsNotNone(worker)
            assert worker is not None
            self.assertIsInstance(worker, ReplenishWorker)
            self.assertIsNotNone(worker._thread)
            self.assertTrue(worker._thread.daemon)
            self.assertTrue(worker._thread.is_alive())
            worker.stop()
            self.assertFalse(worker._thread.is_alive())


class BuildReplenishPersistTest(unittest.TestCase):
    """落账契约：config.json / store / rotator / pool / registry 五者一致。"""

    def test_persist_creates_account_consistently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, rotator, registry = _make_runtime(tmp)
            persist = build_replenish_persist(store, rotator, lock=threading.Lock())
            persist("user_new", "13900000001", "pw_new", NEW_KEY)

            new_acct = store.config.accounts[-1]
            self.assertEqual(new_acct.name, "账号2")
            self.assertEqual(new_acct.user, "user_new")
            self.assertEqual(new_acct.phone, "13900000001")
            self.assertEqual(new_acct.password, "pw_new")
            self.assertEqual(new_acct.api_keys, [NEW_KEY])

            # 池里带着新 Key（pool first 铁律）
            self.assertIsNotNone(rotator.pool.find_key(NEW_KEY))
            # rotator.config.accounts 与 store 同步
            self.assertEqual(rotator.config.accounts[-1].name, "账号2")
            self.assertEqual(rotator.config.accounts[-1].api_keys, [NEW_KEY])

            # 磁盘 config.json：新账号字段全齐，明文密码保留
            disk = json.loads(path.read_text(encoding="utf-8"))
            raw_acct = disk["accounts"][-1]
            self.assertEqual(raw_acct["name"], "账号2")
            self.assertEqual(raw_acct["user"], "user_new")
            self.assertEqual(raw_acct["phone"], "13900000001")
            self.assertEqual(raw_acct["password"], "pw_new")
            self.assertEqual(raw_acct["api_keys"], [NEW_KEY])

            # 注册审计由 worker 统一登记，persist 不再写入 registry
            self.assertEqual(registry.registrations(), [])


class LoadRegistryForTest(unittest.TestCase):
    """``_load_registry_for`` 的文件名与容错契约。"""

    def test_reads_own_replenish_json(self) -> None:
        from st_rotator.cli import _load_registry_for

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            (Path(tmp) / "replenish.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "used_phones": ["13800000000"],
                        "spend": {
                            "date": "",
                            "start_balance": None,
                            "last_balance": None,
                        },
                        "registrations": [],
                    }
                ),
                encoding="utf-8",
            )
            reg = _load_registry_for(cfg)
            self.assertIn("13800000000", reg.used_phones())

    def test_foreign_file_degrades_instead_of_crashing(self) -> None:
        from st_rotator.cli import _load_registry_for

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            # 模拟同名外来文件（如别的工具写的 state.json 内容）
            (Path(tmp) / "replenish.json").write_text(
                json.dumps({"version": "1.3.0", "gateway": {}}), encoding="utf-8"
            )
            msgs: list[str] = []
            reg = _load_registry_for(cfg, sink=msgs.append)
            self.assertEqual(reg.used_phones(), frozenset())
            self.assertTrue(msgs, "应告警而非崩溃")

    def test_uses_distinct_filename_not_state_json(self) -> None:
        from st_rotator.cli import _load_registry_for

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            reg = _load_registry_for(cfg)
            reg.claim_phone("13900000002")
            self.assertTrue((Path(tmp) / "replenish.json").is_file())
            self.assertFalse((Path(tmp) / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
