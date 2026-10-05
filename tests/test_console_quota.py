import unittest

from st_rotator import quota
from st_rotator.config import Config, ConfigStore
from st_rotator.ui import ConsoleState

SAMPLE = {
    "pools": [
        {"name": "通用积分池", "pool_type": "default", "model_ids": ["m"],
         "window_5h": {"remaining": "40979", "reset_at": "1791206310"},
         "window_7d": {"remaining": "45196", "reset_at": "1791321510"}},
        {"name": "Flash-Lite积分池", "pool_type": "dedicated", "model_ids": ["sensenova-6.8-flash-lite"],
         "window_5h": {"remaining": "60000", "reset_at": "1791206310"},
         "window_7d": {"remaining": "600000", "reset_at": "1791321510"}},
    ]
}


class _FakeTransport:
    def login(self, user, password):
        return quota.TokenBundle("jwt", "", 10800, 0.0)

    def fetch_pools(self, access_token):
        return quota.normalize_pools(SAMPLE)


class _FakeRotator:
    def __init__(self, config):
        self.config = config


class ConsoleQuotaTest(unittest.TestCase):
    def _state(self):
        cfg = Config.from_dict({
            "base_url": "http://127.0.0.1:9/v1",
            "accounts": [{"name": "账号1", "api_keys": ["k1"], "user": "u1", "phone": "138", "password": "p1"}],
        })
        svc = quota.QuotaService(cfg, transport=_FakeTransport())
        return ConsoleState(store=None, rotator=_FakeRotator(cfg), quota=svc)  # type: ignore[arg-type]

    def test_quota_payload_shape_and_no_secret(self):
        payload = self._state().quota_payload(force=False)
        acct = payload["accounts"][0]
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["user"], "u1")
        self.assertAlmostEqual(acct["general"]["h5"]["remaining"], 40979.0)
        self.assertAlmostEqual(acct["flash_lite"]["d7"]["remaining"], 600000.0)
        self.assertNotIn("password", str(payload))
        self.assertIn("consumption", payload)

    def test_no_service_returns_empty(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1", "accounts": [{"name": "a", "api_keys": ["k"]}]})
        st = ConsoleState(store=None, rotator=_FakeRotator(cfg), quota=None)  # type: ignore[arg-type]
        self.assertEqual(st.quota_payload(force=False)["accounts"], [])


if __name__ == "__main__":
    unittest.main()
