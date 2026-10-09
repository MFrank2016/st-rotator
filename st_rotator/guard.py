"""泄漏守卫：检测「网关空转但账号积分仍在消耗」并定时统一轮换疑似泄漏的账号。

背景
----
本工具把多把 Key 池化后，网关是这些 Key 的**唯一合法使用者**。若在某个窗口内
网关**完全没有** token 消耗记录（空转），却有账号的**通用池积分**采样出现消耗增量，
说明该账号的 Key 很可能被网关之外的第三方使用（泄漏 / 被共享）。

处置
----
把这类账号记入「待轮换」清单（跨重启持久化到 ``guard.json``），每天到
``rotate_hour:rotate_minute``（默认 02:00，**本地时间**）统一执行一次
「重新登录 → 注销该账号全部 Key → 新建一把 Key → 更新配置并落盘」，随后清空清单。

判定口径（与需求一致）
----------------------
* 「没有 token 消耗记录」= **全局口径**：窗口内网关整体没有任何 token 用量记录。
* 「积分采样变动」= 该账号**通用池（general）**的采样增量 ``delta > 0``。
  只看通用池，是为了规避「一换一烧点」只消耗专属池（flash_lite）却绕过 HTTP 代理、
  不被 token 统计记录而造成的误判（模型测试同理，属手动操作，文档已说明）。

时钟语义
--------
本模块的 ``clock`` 默认 ``time.time``（墙上时间），与 ``UsageTracker`` /
``CreditTracker`` 的采样时间一致；轮换时刻用 ``time.localtime`` 判定，即本地时区。

说明：本模块行数接近仓库同类 worker（autorenew 480 行 / replenish 849 行），
接口单一（一个持久化 store + 一个 worker），不拆分。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from .autorenew import DETAIL_LIMIT, RotationOutcome
from .config import AccountConfig


class GuardError(RuntimeError):
    """``guard.json`` 读取 / 结构校验 / 写入失败。"""


def local_date(ts: float) -> str:
    """把 epoch 秒转成本地日期 ``YYYY-MM-DD``。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class GuardStore:
    """待轮换账号清单 + 最近一次轮换日期的原子 JSON 持久化。

    持久化结构：``{"version":1,"pending":[...],"last_rotate_date":"YYYY-MM-DD"}``。
    写入沿用 ``ConfigStore`` / ``Registry`` 的「临时文件 + 原子替换」模式，
    避免写坏原文件。所有读取与修改都受 ``self._lock`` 保护（未注入时用私有 RLock）。
    """

    VERSION = 1

    def __init__(
        self, path: str | os.PathLike[str], *, lock: threading.Lock | None = None
    ) -> None:
        self.path: Path = Path(path)
        self._lock: threading.Lock | threading.RLock = (
            lock if lock is not None else threading.RLock()
        )
        self._pending: list[str] = []
        self._last_rotate_date: str = ""

    # ------------------------------------------------------------ 加载 / 保存

    @classmethod
    def load(
        cls, path: str | os.PathLike[str], *, lock: threading.Lock | None = None
    ) -> "GuardStore":
        """从磁盘加载；文件缺失 → 全新空状态，损坏 / 结构不对 → ``GuardError``。"""
        p = Path(path)
        if not p.is_file():
            return cls(p, lock=lock)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GuardError(f"guard.json 不是合法 JSON: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise GuardError("guard.json 根节点必须是对象")
        if raw.get("version") != cls.VERSION:
            raise GuardError(f"guard.json 版本不支持: {raw.get('version')!r}")
        pending_raw = raw.get("pending")
        if not isinstance(pending_raw, list) or not all(
            isinstance(name, str) for name in pending_raw
        ):
            raise GuardError("guard.json pending 必须是字符串列表")
        date_raw = raw.get("last_rotate_date")
        if date_raw is not None and not isinstance(date_raw, str):
            raise GuardError("guard.json last_rotate_date 必须是字符串")
        store = cls(p, lock=lock)
        seen: list[str] = []
        for name in pending_raw:
            if name and name not in seen:  # 去重但保持书写顺序
                seen.append(name)
        store._pending = seen
        store._last_rotate_date = str(date_raw or "")
        return store

    def save(self) -> None:
        """原子落盘（先写临时文件再替换）。"""
        with self._lock:
            self._save()

    def _save(self) -> None:
        payload = {
            "version": self.VERSION,
            "pending": list(self._pending),
            "last_rotate_date": self._last_rotate_date,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 待轮换清单

    def pending(self) -> list[str]:
        """当前待轮换账号（拷贝）。"""
        with self._lock:
            return list(self._pending)

    def add_pending(self, names: Sequence[str]) -> list[str]:
        """把账号并入待轮换清单并落盘；返回本次**新增**（此前不在清单里）的账号。"""
        with self._lock:
            added: list[str] = []
            for name in names:
                if name and name not in self._pending and name not in added:
                    added.append(name)
            if added:
                self._pending.extend(added)
                self._save()
            return added

    def finish_rotation(self, date: str) -> None:
        """记录一次轮换完成：清空待轮换清单 + 记下日期，一次性落盘。"""
        with self._lock:
            self._pending = []
            self._last_rotate_date = str(date)
            self._save()

    def last_rotate_date(self) -> str:
        with self._lock:
            return self._last_rotate_date


class LeakGuardWorker:
    """定时扫描「网关空转却有通用池积分消耗」的账号，并在每日固定时刻统一轮换。

    与 ``AutoRenewWorker`` / ``ReplenishWorker`` 同构：``start`` 先立即跑一轮再按
    ``interval`` 周期循环；任何异常都收敛为状态而不抛出，线程循环永不中断。
    所有外部依赖（accounts / token_usage_last / credit_changes / rotate / store）
    都通过注入提供，因此可无网络地整体拉通测试。
    """

    STATUS_IDLE = "idle"  # 窗口内网关有消耗，或空转但无积分变动
    STATUS_FLAGGED = "flagged"  # 本轮新增了待轮换账号
    STATUS_ROTATED = "rotated"  # 完成了一轮定时轮换
    STATUS_CHECK_ERROR = "check_error"  # 本轮扫描 / 轮换异常，下轮再试

    def __init__(
        self,
        *,
        accounts: Callable[[], Sequence[AccountConfig]],
        token_usage_last: Callable[[], float | None],
        credit_changes: Callable[[float, float | None], Sequence[str]],
        rotate: Callable[[AccountConfig], RotationOutcome],
        store: GuardStore,
        clock: Callable[[], float] = time.time,
        interval: float = 3600.0,
        window: float = 3600.0,
        rotate_hour: int = 2,
        rotate_minute: int = 0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._accounts = accounts
        self._token_usage_last = token_usage_last
        self._credit_changes = credit_changes
        self._rotate = rotate
        self._store = store
        self._clock = clock
        self._interval = float(interval)
        self._window = float(window)
        self._rotate_hour = int(rotate_hour)
        self._rotate_minute = int(rotate_minute)
        self._log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()  # 扫描 / 轮换串行，同一时刻只允许一轮
        self._status: dict[str, str] = {"status": self.STATUS_IDLE, "message": ""}
        self._status_lock = threading.Lock()
        self._last_scan_at: float | None = None
        self._last_rotate_at: float | None = None

    # ------------------------------------------------------------ 线程生命周期

    def start(self) -> None:
        """以后台 daemon 线程启动守卫循环。"""
        self._thread = threading.Thread(target=self._loop, name="leak-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止并 join 线程（最多等 5 秒）。"""
        self._stop.set()
        self._wake.set()  # 立刻打断 interval 等待，缩短 join
        if self._thread is not None:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        """唤醒后台循环立即跑一轮（在 worker 自己的线程里跑）。"""
        self._wake.set()

    def _loop(self) -> None:
        """先立即跑一轮，再按 interval 周期循环。"""
        self.run_once()
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.run_once()

    # ------------------------------------------------------------ 核心

    def run_once(self) -> None:
        """跑一轮：先扫描记录，再（若到点）统一轮换。异常收敛为 check_error。"""
        if not self._cycle_lock.acquire(blocking=False):
            self._log("[泄漏守卫] 上一轮仍在进行，跳过本轮")
            return
        try:
            self._cycle()
        except Exception as exc:  # noqa: BLE001 - 兜底：循环线程永不中断
            self._set_status(
                self.STATUS_CHECK_ERROR,
                f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
                log_line=f"[泄漏守卫] 本轮异常：{type(exc).__name__}: {exc}"[:DETAIL_LIMIT],
            )
        finally:
            self._cycle_lock.release()

    def _cycle(self) -> None:
        now = self._clock()
        self._last_scan_at = now
        self._scan(now)
        if self._rotation_due(now):
            self._rotate_pending(now)

    def _scan(self, now: float) -> None:
        """窗口内网关空转却有通用池积分消耗 → 把相关账号并入待轮换清单。

        「空转」= 网关最近一次 token 消耗时刻早于窗口起点；积分增量还要求其
        **覆盖区间整体晚于**该时刻（采样最长有一个采样周期的滞后），避免把网关
        自身刚发生的请求误判为外部盗用。
        """
        since = now - self._window
        last_usage = self._token_usage_last()
        if last_usage is not None and last_usage >= since:
            # 网关在窗口内有 token 消耗 → 正常，不记录（避免把正常用量误判为泄漏）
            if not self._store.pending():
                self._set_status(self.STATUS_IDLE, "")
            return
        changed = [
            name for name in self._credit_changes(since, last_usage) if name
        ]
        if not changed:
            if not self._store.pending():
                self._set_status(self.STATUS_IDLE, "")
            return
        added = self._store.add_pending(changed)
        if added:
            self._set_status(
                self.STATUS_FLAGGED,
                f"新增待轮换账号 {len(added)} 个",
                log_line=(
                    f"[泄漏守卫] 网关空转却有积分消耗，新增待轮换账号 "
                    f"{len(added)} 个：{'、'.join(added)}"
                ),
            )

    def _rotation_due(self, now: float) -> bool:
        """是否到了每日轮换时刻（本地时间）且今天还没轮换过。"""
        lt = time.localtime(now)
        if self._store.last_rotate_date() == local_date(now):
            return False
        return (lt.tm_hour, lt.tm_min) >= (self._rotate_hour, self._rotate_minute)

    def _rotate_pending(self, now: float) -> None:
        """对待轮换清单里的每个账号执行一次轮换；随后清空清单并记下日期。"""
        date = local_date(now)
        names = self._store.pending()
        if not names:
            self._store.finish_rotation(date)
            self._last_rotate_at = now
            self._set_status(
                self.STATUS_IDLE,
                "无待轮换账号",
                log_line=f"[泄漏守卫] {date} 定时轮换：无待轮换账号",
            )
            return
        by_name = {account.name: account for account in self._accounts()}
        ok = 0
        failed = 0
        for name in names:
            account = by_name.get(name)
            if account is None:
                failed += 1
                self._log(f"[泄漏守卫] 账号 {name} 已不在配置中，跳过轮换")
                continue
            if not account.user or not account.password:
                failed += 1
                self._log(f"[泄漏守卫] 账号 {name} 未配置登录凭据，无法轮换")
                continue
            try:
                outcome = self._rotate(account)
            except Exception as exc:  # noqa: BLE001 - 单账号异常不影响其它账号
                failed += 1
                self._log(
                    f"[泄漏守卫] 账号 {name} 轮换异常：{type(exc).__name__}: {exc}"[
                        :DETAIL_LIMIT
                    ]
                )
                continue
            if outcome.ok:
                ok += 1
                self._log(f"[泄漏守卫] 账号 {name} 轮换成功，Key 已更新")
            else:
                failed += 1
                self._log(
                    f"[泄漏守卫] 账号 {name} 轮换失败（{outcome.status}）：{outcome.message}"
                )
        self._store.finish_rotation(date)
        self._last_rotate_at = now
        self._set_status(
            self.STATUS_ROTATED,
            f"轮换 {ok} 成功 / {failed} 失败",
            log_line=f"[泄漏守卫] {date} 定时轮换完成：成功 {ok}，失败 {failed}",
        )

    # ------------------------------------------------------------ 状态

    def _set_status(self, status: str, message: str, *, log_line: str | None = None) -> None:
        """写入状态；仅在状态发生迁移时输出日志（log_line 非空且确实变化）。"""
        with self._status_lock:
            changed = (
                self._status.get("status") != status
                or self._status.get("message") != message
            )
            if changed:
                self._status = {"status": status, "message": message}
        if changed and log_line is not None:
            self._log(log_line)

    def status(self) -> dict[str, Any]:
        """给控制台展示的状态快照（线程安全）。"""
        with self._status_lock:
            status = self._status.get("status", self.STATUS_IDLE)
            message = self._status.get("message", "")
            last_scan = self._last_scan_at
            last_rotate = self._last_rotate_at
        pending = self._store.pending()
        return {
            "enabled": True,
            "status": status,
            "message": message,
            "pending": pending,
            "pending_count": len(pending),
            "last_scan_at": last_scan,
            "last_rotate_at": last_rotate,
            "last_rotate_date": self._store.last_rotate_date(),
            "next_rotate_at": self._next_rotate_at(),
            "window_seconds": self._window,
            "rotate_hour": self._rotate_hour,
            "rotate_minute": self._rotate_minute,
        }

    def _next_rotate_at(self) -> float:
        """下一次轮换时刻（本地时间）的 epoch 秒。"""
        now = self._clock()
        lt = time.localtime(now)
        candidate = time.mktime(
            (
                lt.tm_year,
                lt.tm_mon,
                lt.tm_mday,
                self._rotate_hour,
                self._rotate_minute,
                0,
                0,
                0,
                -1,
            )
        )
        if candidate <= now:
            candidate += 86400
        return candidate
