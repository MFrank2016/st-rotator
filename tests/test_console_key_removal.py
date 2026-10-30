"""ConsoleState.remove_one：「最后一把 Key」的判断必须与删除在同一临界区。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from st_rotator.config import ConfigStore
from st_rotator.ui import ConsoleState


class _Key:
    def __init__(self, plain: str, account: str = "账号1") -> None:
        self.key = plain
        self.account = account
        self.masked = plain[:6] + "..." + plain[-4:]


class _Pool:
    def __init__(self, keys: list[str]) -> None:
        self.keys = [_Key(k) for k in keys]
        self.on_find = None

    def find_by_id(self, identifier: str):
        if self.on_find is not None:
            self.on_find()
        for key in self.keys:
            if key.key == identifier:
                return key
        return None

    def __len__(self) -> int:
        return len(self.keys)


class _Rotator:
    def __init__(self, config, keys: list[str]) -> None:
        self.config = config
        self.pool = _Pool(keys)

    def remove_key(self, plain: str) -> bool:
        for key in list(self.pool.keys):
            if key.key == plain:
                self.pool.keys.remove(key)
                return True
        return False


def _console(tmp: str, keys: list[str]) -> ConsoleState:
    path = Path(tmp) / "config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9/v1",
                "accounts": [{"name": "账号1", "api_keys": keys}],
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore.load(path)
    rotator = _Rotator(store.config, keys)
    return ConsoleState(store=store, rotator=rotator)  # type: ignore[arg-type]


def _locked(lock) -> bool:
    """非阻塞探测：锁被当前线程持有时返回 True。"""
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


class RemoveOneAtomicityTest(unittest.TestCase):
    def test_lookup_happens_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp, ["sk-aaaa1111", "sk-bbbb2222"])
            seen: list[bool] = []
            console.rotator.pool.on_find = lambda: seen.append(_locked(console.lock))
            resp = console.remove_one("sk-aaaa1111")
            self.assertEqual(resp.status, 200)
            self.assertEqual(seen, [True])

    def test_concurrent_removals_cannot_delete_all_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = _console(tmp, ["sk-aaaa1111", "sk-bbbb2222"])
            barrier = threading.Barrier(2)
            results: list[bool] = []
            guard = threading.Lock()

            def worker(identifier: str) -> None:
                barrier.wait()
                resp = console.remove_one(identifier)
                with guard:
                    results.append(bool(resp.payload.get("ok")))

            threads = [
                threading.Thread(target=worker, args=(key,))
                for key in ("sk-aaaa1111", "sk-bbbb2222")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(1 for ok in results if ok), 1)  # 只允许一个成功
            self.assertEqual(len(console.rotator.pool), 1)  # 不会删光
            self.assertEqual(len(console.config.accounts[0].api_keys), 1)


if __name__ == "__main__":
    unittest.main()
