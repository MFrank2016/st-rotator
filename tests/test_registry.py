"""T4 registry.py 测试：原子 JSON 注册表（已用号码 / 每日花销 / 注册审计）。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

from st_rotator.registry import Registry, RegistryError, SpendState


def test_load_missing_file_returns_fresh_state(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    assert reg.used_phones() == frozenset()
    assert reg.spend_state() == {
        "date": "",
        "start_balance": None,
        "last_balance": None,
    }
    assert reg.registrations() == []
    assert reg.rotation_count() == 0


def test_spend_state_defaults():
    state = SpendState()
    assert state.date == ""
    assert state.start_balance is None
    assert state.last_balance is None


def test_claim_phone_marked_and_persists_across_reload(tmp_path):
    path = tmp_path / "state.json"
    reg = Registry.load(path)
    assert not reg.is_phone_used("13800000000")
    reg.claim_phone("13800000000")
    assert reg.is_phone_used("13800000000")
    reloaded = Registry.load(path)
    assert reloaded.is_phone_used("13800000000")
    assert reloaded.used_phones() == frozenset({"13800000000"})


def test_save_is_atomic_no_tmp_residue(tmp_path):
    path = tmp_path / "state.json"
    reg = Registry.load(path)
    reg.claim_phone("13900000000")
    reg.save()
    assert path.is_file()
    assert not (tmp_path / "state.json.tmp").exists()


def test_invalid_json_raises_registry_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError):
        Registry.load(path)


def test_invalid_shape_raises_registry_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "used_phones": []}), encoding="utf-8")
    with pytest.raises(RegistryError):
        Registry.load(path)


def test_note_balance_sets_baseline_on_first_read_of_day(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.note_balance("2026-10-09", 100.0)
    state = reg.spend_state()
    assert state["date"] == "2026-10-09"
    assert state["start_balance"] == 100.0
    assert state["last_balance"] == 100.0


def test_spend_state_updates_last_balance(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.note_balance("2026-10-09", 100.0)
    reg.note_balance("2026-10-09", 95.5)
    state = reg.spend_state()
    assert state["date"] == "2026-10-09"
    assert state["start_balance"] == 100.0
    assert state["last_balance"] == 95.5


def test_spend_persists_across_reload(tmp_path):
    path = tmp_path / "state.json"
    reg = Registry.load(path)
    reg.note_balance("2026-10-09", 100.0)
    reg.note_balance("2026-10-09", 95.0)
    reloaded = Registry.load(path)
    assert reloaded.spend_state() == {
        "date": "2026-10-09",
        "start_balance": 100.0,
        "last_balance": 95.0,
    }


def test_date_rollover_resets_spend(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.note_balance("2026-10-09", 100.0)
    reg.note_balance("2026-10-09", 95.0)
    reg.note_balance("2026-10-10", 80.0)
    state = reg.spend_state()
    assert state["date"] == "2026-10-10"
    assert state["start_balance"] == 80.0
    assert state["last_balance"] == 80.0


def test_record_registration_audit(tmp_path):
    path = tmp_path / "state.json"
    reg = Registry.load(path)
    reg.record_registration(
        created_at=0.0,
        phone="13000000000",
        username="u0",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    reg.record_registration(
        created_at=1.0,
        phone="13100000000",
        username="u1",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    reg.record_registration(
        created_at=2.0,
        phone="13200000000",
        username="u2",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    entries = reg.registrations()
    assert [e["phone"] for e in entries] == [
        "13200000000",
        "13100000000",
        "13000000000",
    ]
    # 审计日志同样持久化
    reloaded = Registry.load(path)
    assert [e["phone"] for e in reloaded.registrations()] == [
        "13200000000",
        "13100000000",
        "13000000000",
    ]


def test_registrations_capped_at_default_limit(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    for i in range(60):
        reg.record_registration(
            created_at=float(i),
            phone=f"13{i % 10:08d}",
            username="u",
            password="p",
            is_new=True,
            password_reset=False,
            success=True,
            reason="",
            detail="",
        )
    assert len(reg.registrations()) == 50
    assert len(reg.registrations(limit=60)) == 60
    assert reg.registrations(limit=2)[0]["phone"] == "1300000009"


def test_injected_lock_is_used(tmp_path):
    lock = threading.Lock()
    reg = Registry.load(tmp_path / "state.json", lock=lock)
    reg.claim_phone("13700000000")
    assert reg.is_phone_used("13700000000")


def test_note_rotation_increments_and_persists(tmp_path):
    path = tmp_path / "state.json"
    reg = Registry.load(path)
    assert reg.rotation_count() == 0
    reg.note_rotation()
    reg.note_rotation()
    assert reg.rotation_count() == 2
    assert Registry.load(path).rotation_count() == 2


def test_count_registrations_filters_by_success(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.record_registration(
        created_at=1.0,
        phone="130",
        username="a",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    reg.record_registration(
        created_at=2.0,
        phone="131",
        username="b",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    reg.record_registration(
        created_at=3.0,
        phone="132",
        username="c",
        password="p",
        is_new=False,
        password_reset=True,
        success=False,
        reason="sms_timeout",
        detail="",
    )
    assert reg.count_registrations(True) == 2
    assert reg.count_registrations(False) == 1
    assert reg.count_registrations() == 3


def test_snapshot_shape(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.claim_phone("13900000000")
    reg.claim_phone("13800000000")
    reg.note_balance("2026-10-09", 100.0)
    reg.note_rotation()
    reg.record_registration(
        created_at=1.0,
        phone="13800000000",
        username="u1",
        password="p",
        is_new=True,
        password_reset=False,
        success=True,
        reason="",
        detail="",
    )
    snap = reg.snapshot()
    assert set(snap) == {"used_phones", "spend", "registrations", "rotations"}
    assert snap["used_phones"] == ["13800000000", "13900000000"]
    assert snap["spend"] == {
        "date": "2026-10-09",
        "start_balance": 100.0,
        "last_balance": 100.0,
    }
    assert snap["rotations"] == 1
    assert snap["registrations"] == [
        {
            "created_at": 1.0,
            "phone": "13800000000",
            "username": "u1",
            "password": "p",
            "is_new": True,
            "password_reset": False,
            "success": True,
            "reason": "",
            "detail": "",
        }
    ]


def _record(
    reg,
    *,
    phone="13000000000",
    username="",
    is_new=True,
    success=True,
    created_at=0.0,
    reason="",
    detail="",
    password_reset=False,
) -> None:
    reg.record_registration(
        created_at=created_at,
        phone=phone,
        username=username,
        password="pw",
        is_new=is_new,
        password_reset=password_reset,
        success=success,
        reason=reason,
        detail=detail,
    )


class RegistrationAuditTest(unittest.TestCase):
    """新记录契约：record_registration 关键字参数 + count_registrations(success) + 分页。"""

    def _registry(self) -> Registry:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Registry.load(Path(tmp.name) / "state.json")

    def test_record_registration_stores_exact_keys(self):
        reg = self._registry()
        reg.record_registration(
            created_at=5.0,
            phone="13800000001",
            username="abcd1234",
            password="s3cret!A",
            is_new=True,
            password_reset=False,
            success=True,
            reason="",
            detail="",
        )
        (entry,) = reg.registrations()
        self.assertEqual(
            set(entry),
            {
                "created_at",
                "phone",
                "username",
                "password",
                "is_new",
                "password_reset",
                "success",
                "reason",
                "detail",
            },
        )
        self.assertEqual(entry["created_at"], 5.0)
        self.assertEqual(entry["username"], "abcd1234")
        self.assertEqual(entry["is_new"], True)
        self.assertEqual(entry["success"], True)

    def test_count_registrations_by_success(self):
        reg = self._registry()
        _record(reg, phone="130", created_at=1.0, success=True)
        _record(reg, phone="131", created_at=2.0, success=True)
        _record(reg, phone="132", created_at=3.0, success=False, reason="sms_timeout")
        self.assertEqual(reg.count_registrations(), 3)
        self.assertEqual(reg.count_registrations(True), 2)
        self.assertEqual(reg.count_registrations(False), 1)

    def test_registrations_page_newest_first_paging(self):
        reg = self._registry()
        for i in range(5):
            _record(reg, phone=f"13{i}", created_at=float(i))
        page = reg.registrations_page(page=1, size=2)
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["size"], 2)
        self.assertEqual([e["phone"] for e in page["items"]], ["134", "133"])
        page2 = reg.registrations_page(page=3, size=2)
        self.assertEqual([e["phone"] for e in page2["items"]], ["130"])

    def test_registrations_page_filters_q_status_kind(self):
        reg = self._registry()
        _record(
            reg,
            phone="13800000001",
            username="userAbc",
            created_at=1.0,
            is_new=True,
            success=True,
        )
        _record(
            reg,
            phone="13800000002",
            username="other",
            created_at=2.0,
            is_new=False,
            success=True,
            password_reset=True,
        )
        _record(
            reg,
            phone="13800000003",
            username="third",
            created_at=3.0,
            is_new=True,
            success=False,
            reason="captcha_required",
        )
        self.assertEqual(reg.registrations_page(q="USERABC")["total"], 1)
        self.assertEqual(reg.registrations_page(q="13800000002")["total"], 1)
        self.assertEqual(reg.registrations_page(status="ok")["total"], 2)
        self.assertEqual(reg.registrations_page(status="fail")["total"], 1)
        self.assertEqual(reg.registrations_page(kind="new")["total"], 2)
        self.assertEqual(reg.registrations_page(kind="takeover")["total"], 1)
        both = reg.registrations_page(status="ok", kind="new")
        self.assertEqual(both["total"], 1)
        self.assertEqual(both["items"][0]["phone"], "13800000001")

    def test_registrations_page_clamps_page_and_size(self):
        reg = self._registry()
        _record(reg, phone="130", created_at=1.0)
        page = reg.registrations_page(page=0, size=9999)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["size"], 200)
        page = reg.registrations_page(page=-5, size=0)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["size"], 1)

    def test_legacy_records_are_migrated_to_new_contract(self):
        # 旧版记录（outcome / name）加载时迁移成新 9 键契约：success 由 outcome 派生，
        # 并落盘（一次性 schema 迁移），不再被误当作失败。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "used_phones": [],
                    "spend": {"date": "", "start_balance": None, "last_balance": None},
                    "registrations": [
                        {"name": "账号0", "phone": "130", "outcome": "ok", "created_at": 1.0},
                        {
                            "name": "账号1",
                            "phone": "131",
                            "outcome": "takeover_password_unset",
                            "created_at": 2.0,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        reg = Registry.load(path)
        items = reg.registrations()  # 最新在前
        self.assertEqual(len(items), 2)
        for entry in items:
            self.assertEqual(
                set(entry),
                {
                    "created_at",
                    "phone",
                    "username",
                    "password",
                    "is_new",
                    "password_reset",
                    "success",
                    "reason",
                    "detail",
                },
            )
        # success 由 outcome 派生：ok -> True，takeover_password_unset -> False
        self.assertEqual(reg.count_registrations(True), 1)
        self.assertEqual(reg.count_registrations(False), 1)
        self.assertEqual(items[0]["reason"], "takeover_password_unset")
        self.assertFalse(items[0]["success"])
        self.assertTrue(items[1]["success"])
        self.assertEqual(items[1]["phone"], "130")
        # 迁移已落盘：旧键（outcome/name）消失，新键（success）就位
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(all("success" in e for e in reloaded["registrations"]))
        self.assertTrue(all("outcome" not in e for e in reloaded["registrations"]))
