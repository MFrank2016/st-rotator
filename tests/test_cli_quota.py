import unittest

from st_rotator.cli import build_quota_service
from st_rotator.config import Config


class BuildQuotaServiceTest(unittest.TestCase):
    def test_none_when_no_credentials(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1",
                                "accounts": [{"name": "a", "api_keys": ["k"]}]})
        self.assertIsNone(build_quota_service(cfg))

    def test_returns_service_when_credentials_present(self):
        cfg = Config.from_dict({"base_url": "http://127.0.0.1:9/v1",
                                "accounts": [{"name": "a", "api_keys": ["k"], "user": "u", "password": "p"}]})
        svc = build_quota_service(cfg)
        self.assertIsNotNone(svc)
        svc.close()


if __name__ == "__main__":
    unittest.main()
