"""T4 registry.py 测试：原子 JSON 注册表（已用号码 / 每日花销 / 注册审计）。"""

import json
import threading

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
        name="账号0", phone="13000000000", outcome="ok", created_at=0.0
    )
    reg.record_registration(
        name="账号1", phone="13100000000", outcome="ok", created_at=1.0
    )
    reg.record_registration(
        name="账号2", phone="13200000000", outcome="ok", created_at=2.0
    )
    entries = reg.registrations()
    assert [e["name"] for e in entries] == ["账号2", "账号1", "账号0"]
    assert [e["phone"] for e in entries] == [
        "13200000000",
        "13100000000",
        "13000000000",
    ]
    # 审计日志同样持久化
    reloaded = Registry.load(path)
    assert [e["name"] for e in reloaded.registrations()] == ["账号2", "账号1", "账号0"]


def test_registrations_capped_at_default_limit(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    for i in range(60):
        reg.record_registration(
            name=f"账号{i}", phone=f"13{i % 10:08d}", outcome="ok", created_at=float(i)
        )
    assert len(reg.registrations()) == 50
    assert len(reg.registrations(limit=60)) == 60
    assert reg.registrations(limit=2)[0]["name"] == "账号59"


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


def test_count_registrations_filters_by_outcome(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.record_registration(name="a", phone="130", outcome="ok", created_at=1.0)
    reg.record_registration(name="b", phone="131", outcome="ok", created_at=2.0)
    reg.record_registration(
        name="c", phone="132", outcome="takeover_password_unset", created_at=3.0
    )
    assert reg.count_registrations("ok") == 2
    assert reg.count_registrations("takeover_password_unset") == 1
    assert reg.count_registrations() == 3
    assert reg.count_registrations("nope") == 0


def test_snapshot_shape(tmp_path):
    reg = Registry.load(tmp_path / "state.json")
    reg.claim_phone("13900000000")
    reg.claim_phone("13800000000")
    reg.note_balance("2026-10-09", 100.0)
    reg.note_rotation()
    reg.record_registration(
        name="账号1", phone="13800000000", outcome="ok", created_at=1.0
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
        {"name": "账号1", "phone": "13800000000", "outcome": "ok", "created_at": 1.0}
    ]
