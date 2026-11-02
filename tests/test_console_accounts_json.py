"""控制台账号 JSON：无损导出与按账号名合并导入。"""

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState


class _PoolItem:
    def __init__(self, key: str, account: str) -> None:
        self.key = key
        self.account = account


class _FakePool:
    def __init__(self) -> None:
        self.items: list[_PoolItem] = []

    def find_key(self, key: str):
        return next((item for item in self.items if item.key == key), None)

    def add_key(self, key: str, account: str, **kwargs) -> _PoolItem:
        item = _PoolItem(key, account)
        self.items.append(item)
        return item

    @property
    def keys(self):
        # 与真实 KeyPool 一致：keys 是属性而非方法
        return tuple(self.items)

    def remove_key(self, key: str):
        item = self.find_key(key)
        if item is not None:
            self.items.remove(item)
        return item


class _FakeRotator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool = _FakePool()


def _export_state(accounts) -> ConsoleState:
    cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1", "accounts": accounts})
    return ConsoleState(store=None, rotator=_FakeRotator(cfg))  # type: ignore[arg-type]


def _store_state(tmp: str, accounts) -> tuple[ConsoleState, Path]:
    path = Path(tmp) / "config.json"
    path.write_text(
        json.dumps(
            {"base_url": "http://127.0.0.1:9/v1", "accounts": accounts},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ConfigStore.load(path)
    rotator = _FakeRotator(store.config)
    return ConsoleState(store=store, rotator=rotator), path  # type: ignore[arg-type]


class ExportAccountsJsonTest(unittest.TestCase):
    def test_includes_accounts_without_credentials(self):
        st = _export_state(
            [
                {
                    "name": "有凭据",
                    "api_keys": ["sk-1"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                },
                {"name": "无凭据", "api_keys": ["sk-2"]},
            ]
        )
        out = st.export_accounts_json()
        self.assertEqual([a["name"] for a in out["accounts"]], ["有凭据", "无凭据"])
        self.assertEqual(out["accounts"][1]["user"], "")
        self.assertEqual(out["accounts"][1]["password"], "")

    def test_fields_complete_with_tags_and_plaintext_password(self):
        st = _export_state(
            [
                {
                    "name": "账号1",
                    "api_keys": ["sk-1", "sk-2"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                    "rpm_limit": 30,
                    "max_concurrency": 4,
                    "weight": 2.0,
                    "tags": ["vip", "备用"],
                    "updated_at": 123.5,
                }
            ]
        )
        out = st.export_accounts_json()
        self.assertEqual(out["format"], "st-rotator/accounts")
        self.assertEqual(out["version"], 1)
        account = out["accounts"][0]
        self.assertEqual(
            set(account),
            {
                "name",
                "api_keys",
                "user",
                "phone",
                "password",
                "rpm_limit",
                "max_concurrency",
                "weight",
                "tags",
                "updated_at",
            },
        )
        self.assertEqual(account["password"], "p1")
        self.assertEqual(account["tags"], ["vip", "备用"])
        self.assertEqual(account["api_keys"], ["sk-1", "sk-2"])
        self.assertEqual(account["updated_at"], 123.5)

    def test_legacy_export_route_unchanged(self):
        st = _export_state(
            [
                {
                    "name": "账号1",
                    "api_keys": ["sk-1"],
                    "user": "u1",
                    "phone": "138",
                    "password": "p1",
                },
                {"name": "无凭据", "api_keys": ["sk-2"]},
            ]
        )
        legacy = st.handle("GET", "/api/accounts/export")
        self.assertEqual(legacy.payload, st.export_accounts())
        self.assertEqual(legacy.payload["text"], "138--u1--p1--sk-1")
        self.assertEqual(legacy.payload["skipped"], 1)

        modern = st.handle("GET", "/api/accounts/export", query={"format": ["json"]})
        self.assertNotIn("text", modern.payload)
        self.assertEqual(len(modern.payload["accounts"]), 2)


class ImportAccountsJsonTest(unittest.TestCase):
    def test_merge_by_name_and_append_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, path = _store_state(
                tmp,
                [
                    {
                        "name": "账号A",
                        "api_keys": ["sk-old"],
                        "user": "old",
                        "password": "oldpwd",
                    }
                ],
            )
            payload = {
                "format": "st-rotator/accounts",
                "version": 1,
                "accounts": [
                    {
                        "name": "账号A",
                        "api_keys": ["sk-new1", "sk-new2"],
                        "user": "new",
                        "phone": "139",
                        "password": "newpwd",
                        "rpm_limit": 30,
                        "max_concurrency": 4,
                        "weight": 2.0,
                        "tags": ["vip"],
                    },
                    {
                        "name": "账号B",
                        "api_keys": ["sk-b"],
                        "user": "ub",
                        "phone": "137",
                        "password": "pb",
                    },
                ],
            }
            resp = st.import_accounts_json(payload)
            self.assertEqual(resp.payload["imported"], 2)
            self.assertEqual(resp.payload["failed"], 0)

            # 落盘后重新加载，确认重启仍在
            reloaded = ConfigStore.load(path)
            by_name = {a.name: a for a in reloaded.config.accounts}
            self.assertEqual(set(by_name), {"账号A", "账号B"})
            self.assertEqual(by_name["账号A"].api_keys, ["sk-new1", "sk-new2"])
            self.assertEqual(by_name["账号A"].user, "new")
            self.assertEqual(by_name["账号A"].password, "newpwd")
            self.assertEqual(by_name["账号A"].tags, ["vip"])
            self.assertEqual(by_name["账号B"].api_keys, ["sk-b"])
            self.assertEqual(by_name["账号B"].phone, "137")
            # 内存池同步：旧 Key 被剔除，新 Key 可用
            pool_keys = {item.key for item in st.rotator.pool.keys}
            self.assertEqual(pool_keys, {"sk-new1", "sk-new2", "sk-b"})

    def test_null_updated_at_survives_round_trip(self):
        """导出 None 的 updated_at，导入后必须仍是 None（不能被补成当前时间）。"""
        with tempfile.TemporaryDirectory() as tmp:
            st, path = _store_state(
                tmp, [{"name": "老账号", "api_keys": ["sk-1"], "updated_at": None}]
            )
            exported = st.export_accounts_json()
            self.assertIsNone(exported["accounts"][0]["updated_at"])

            resp = st.import_accounts_json(exported)
            self.assertEqual(resp.payload["imported"], 1)

            reloaded = ConfigStore.load(path)
            self.assertIsNone(reloaded.config.accounts[0].updated_at)

    def test_bad_entry_reported_without_failing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, path = _store_state(tmp, [{"name": "种子", "api_keys": ["sk-seed"]}])
            resp = st.import_accounts_json(
                {
                    "accounts": [
                        {"name": "好1", "api_keys": ["sk-1"]},
                        {"name": "坏", "api_keys": []},
                        {"name": "好2", "api_keys": ["sk-2"]},
                    ]
                }
            )
            self.assertEqual(resp.payload["imported"], 2)
            self.assertEqual(resp.payload["failed"], 1)
            self.assertEqual(
                [r["status"] for r in resp.payload["results"]],
                ["ok", "error", "ok"],
            )
            reloaded = ConfigStore.load(path)
            self.assertEqual(
                [a.name for a in reloaded.config.accounts], ["种子", "好1", "好2"]
            )


if __name__ == "__main__":
    unittest.main()
