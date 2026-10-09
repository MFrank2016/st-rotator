"""Key 池的数字 id：按池内顺序递增、稳定不复用，且可作为操作标识。"""

from __future__ import annotations

import unittest

from st_rotator.config import AccountConfig
from st_rotator.keypool import KeyPool


def _pool() -> KeyPool:
    return KeyPool(
        [
            AccountConfig(name="a", api_keys=["sk-a1", "sk-a2"]),
            AccountConfig(name="b", api_keys=["sk-b1"]),
        ]
    )


class KeyPoolNumericIdTest(unittest.TestCase):
    def test_snapshot_ids_are_sequential_ints(self) -> None:
        pool = _pool()
        ids = [k["id"] for k in pool.snapshot()]
        self.assertEqual(ids, [1, 2, 3])
        self.assertTrue(all(isinstance(i, int) for i in ids))

    def test_add_key_gets_next_id(self) -> None:
        pool = _pool()
        item = pool.add_key("sk-c1", account="c")
        self.assertEqual(item.id, 4)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 2, 3, 4])

    def test_find_by_id_accepts_int_str_and_plaintext(self) -> None:
        pool = _pool()
        self.assertEqual(pool.find_by_id(2).key, "sk-a2")
        self.assertEqual(pool.find_by_id("2").key, "sk-a2")
        self.assertEqual(pool.find_by_id("sk-a2").key, "sk-a2")
        self.assertIsNone(pool.find_by_id(999))

    def test_remove_by_numeric_id_and_no_reuse(self) -> None:
        pool = _pool()
        removed = pool.remove_key(2)
        self.assertIsNotNone(removed)
        self.assertEqual(removed.key, "sk-a2")
        # 不重排、不复用：剩余 id 保留原值（出现空位）
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 3])
        # 新增拿下一个号，不复用已删除的 2
        item = pool.add_key("sk-c1", account="c")
        self.assertEqual(item.id, 4)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 3, 4])

    def test_remove_by_plaintext_still_works(self) -> None:
        pool = _pool()
        removed = pool.remove_key("sk-b1")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.id, 3)
        self.assertEqual([k["id"] for k in pool.snapshot()], [1, 2])


if __name__ == "__main__":
    unittest.main()
