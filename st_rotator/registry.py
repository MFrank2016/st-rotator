"""已用号码 / 每日花销 / 注册审计 / 轮换计数的原子 JSON 注册表。

持久化结构：``{"version":1,"used_phones":[...],"spend":{...},"registrations":[...],
"rotations":N}``。写入沿用 ``ConfigStore.save`` 的「临时文件 + 原子替换」模式，
避免写坏原文件。``rotations`` 为 auto_renew 累计轮换成功次数（跨重启持久化）。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RegistryError(RuntimeError):
    """注册表文件读取 / 结构校验 / 写入失败。"""


@dataclass
class SpendState:
    """每日花销状态；``date`` 换天后由 ``Registry.note_balance`` 重置基线。"""

    date: str = ""
    start_balance: float | None = None
    last_balance: float | None = None


def _as_count(value: Any) -> int:
    """把 JSON 里的计数规范成非负 ``int``，不合法即抛 RegistryError。"""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegistryError(f"计数必须是非负整数，当前为 {value!r}")
    return value


def _as_balance(value: Any) -> float | None:
    """把 JSON 里的余额字段规范成 ``float | None``，不合法即抛 RegistryError。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegistryError(f"spend 字段必须是数值或 null，当前为 {value!r}")
    return float(value)


class Registry:
    """已用号码与花销的 JSON 持久化注册表。

    ``load`` 在文件缺失时返回全新空状态；文件损坏或结构不对抛 ``RegistryError``。
    ``save`` 先写 ``path + \".tmp\"`` 再 ``os.replace`` 原子替换，落盘后无 .tmp 残留。
    所有读取与修改都受 ``self._lock`` 保护（未注入锁时使用私有 ``RLock``）。
    """

    def __init__(
        self, path: str | os.PathLike[str], *, lock: threading.Lock | None = None
    ) -> None:
        self.path: Path = Path(path)
        self._lock: threading.Lock | threading.RLock = (
            lock if lock is not None else threading.RLock()
        )
        self._used: set[str] = set()
        self._spend: SpendState = SpendState()
        self._registrations: list[dict[str, Any]] = []
        self._rotations: int = 0

    # ------------------------------------------------------------ 加载 / 保存

    @classmethod
    def load(
        cls, path: str | os.PathLike[str], *, lock: threading.Lock | None = None
    ) -> "Registry":
        """从磁盘加载注册表；文件缺失 -> 全新空状态，损坏 / 结构不对 -> RegistryError。"""
        p = Path(path)
        if not p.is_file():
            return cls(p, lock=lock)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"replenish.json 不是合法 JSON: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise RegistryError("replenish.json 根节点必须是对象")
        if raw.get("version") != 1:
            raise RegistryError(f"replenish.json 版本不支持: {raw.get('version')!r}")
        used_raw = raw.get("used_phones")
        if not isinstance(used_raw, list) or not all(
            isinstance(p, str) for p in used_raw
        ):
            raise RegistryError("replenish.json used_phones 必须是字符串列表")
        spend_raw = raw.get("spend")
        if not isinstance(spend_raw, Mapping):
            raise RegistryError("replenish.json spend 必须是对象")
        regs_raw = raw.get("registrations")
        if not isinstance(regs_raw, list) or not all(
            isinstance(e, Mapping) for e in regs_raw
        ):
            raise RegistryError("replenish.json registrations 必须是对象列表")
        reg = cls(p, lock=lock)
        reg._used = set(used_raw)
        reg._spend = SpendState(
            date=str(spend_raw.get("date") or ""),
            start_balance=_as_balance(spend_raw.get("start_balance")),
            last_balance=_as_balance(spend_raw.get("last_balance")),
        )
        reg._registrations = [dict(e) for e in regs_raw]
        reg._rotations = _as_count(raw.get("rotations"))
        return reg

    def save(self) -> None:
        """原子落盘（先写临时文件再替换，避免写坏原文件）。"""
        with self._lock:
            self._save()

    def _save(self) -> None:
        payload = {
            "version": 1,
            "used_phones": sorted(self._used),
            "spend": self._spend_dict(),
            "registrations": self._registrations,
            "rotations": self._rotations,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 已用号码

    def is_phone_used(self, phone: str) -> bool:
        """该号码是否已被注册使用过。"""
        with self._lock:
            return phone in self._used

    def claim_phone(self, phone: str) -> None:
        """把号码标记为已用并落盘。"""
        with self._lock:
            self._used.add(phone)
            self._save()

    def used_phones(self) -> frozenset[str]:
        """全部已用号码的不可变视图。"""
        with self._lock:
            return frozenset(self._used)

    # ------------------------------------------------------------ 每日花销

    def note_balance(self, date: str, balance: float) -> None:
        """记录一次余额查询。

        当天首次读取 -> 同时作为基线；之后只更新 ``last_balance``；
        换天后重置基线与 last（花销按当日基线差值计算）。
        """
        with self._lock:
            if self._spend.date != date:
                self._spend.date = date
                self._spend.start_balance = balance
                self._spend.last_balance = balance
            elif self._spend.start_balance is None:
                self._spend.start_balance = balance
                self._spend.last_balance = balance
            else:
                self._spend.last_balance = balance
            self._save()

    def spend_state(self) -> dict[str, Any]:
        """当前花销状态 ``{"date", "start_balance", "last_balance"}``。"""
        with self._lock:
            return self._spend_dict()

    def _spend_dict(self) -> dict[str, Any]:
        return {
            "date": self._spend.date,
            "start_balance": self._spend.start_balance,
            "last_balance": self._spend.last_balance,
        }

    # ------------------------------------------------------------ 注册审计

    def record_registration(
        self, *, name: str, phone: str, outcome: str, created_at: float
    ) -> None:
        """追加一条注册审计记录并落盘。"""
        with self._lock:
            self._registrations.append(
                {
                    "name": name,
                    "phone": phone,
                    "outcome": outcome,
                    "created_at": created_at,
                }
            )
            self._save()

    def registrations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """注册审计记录，最新在前，最多 ``limit`` 条。"""
        with self._lock:
            newest_first = list(reversed(self._registrations))
        return newest_first[:limit]

    def count_registrations(self, outcome: str | None = None) -> int:
        """注册审计记录数；``outcome`` 非空时只计该结果的条数。"""
        with self._lock:
            if outcome is None:
                return len(self._registrations)
            return sum(
                1 for e in self._registrations if e.get("outcome") == outcome
            )

    # ------------------------------------------------------------ 轮换计数

    def note_rotation(self) -> None:
        """累计一次 auto_renew 轮换成功并落盘（跨重启持久化）。"""
        with self._lock:
            self._rotations += 1
            self._save()

    def rotation_count(self) -> int:
        """累计轮换成功次数。"""
        with self._lock:
            return self._rotations

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        """注册表全量快照：已用号码（排序）、花销状态、审计记录（最新在前）、轮换计数。"""
        with self._lock:
            used = sorted(self._used)
            spend = self._spend_dict()
            regs = list(reversed(self._registrations))
            rotations = self._rotations
        return {
            "used_phones": used,
            "spend": spend,
            "registrations": regs,
            "rotations": rotations,
        }
