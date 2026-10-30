"""控制台子 Key 接口：/api/state 只下发掩码，明文走 /api/subkeys/reveal 按需取回。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from st_rotator.config import ConfigStore
from st_rotator.subkeys import SubKeyManager
from st_rotator.ui import ConsoleState


class _FakeRotator:
    def __init__(self, config) -> None:
        self.config = config


class ConsoleSubkeyRevealTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:9/v1",
                    "accounts": [{"name": "a", "api_keys": ["sk-a"]}],
                }
            ),
            encoding="utf-8",
        )
        store = ConfigStore.load(cfg_path)
        self.manager = SubKeyManager(
            Path(self._tmp.name) / "subkeys.json", autosave_interval=0
        )
        self.addCleanup(self.manager.close)
        self.sub = self.manager.create("app-a")
        self.console = ConsoleState(  # type: ignore[arg-type]
            store=store,
            rotator=_FakeRotator(store.config),
            subkeys=self.manager,
        )

    def test_state_payload_only_has_masked_key(self) -> None:
        keys = self.console._subkeys_snapshot()["keys"]
        self.assertNotIn(self.sub.key, json.dumps(keys))
        self.assertEqual(keys[0]["masked"], self.sub.masked)

    def test_reveal_endpoint_returns_plaintext(self) -> None:
        result = self.console.handle(
            "POST", "/api/subkeys/reveal", body={"id": self.sub.id}
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload["key"], self.sub.key)

    def test_reveal_unknown_id_returns_404(self) -> None:
        result = self.console.handle(
            "POST", "/api/subkeys/reveal", body={"id": "no-such-key"}
        )
        self.assertEqual(result.status, 404)


if __name__ == "__main__":
    unittest.main()
