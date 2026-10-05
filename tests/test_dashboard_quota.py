import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardQuotaTest(unittest.TestCase):
    def test_kpi_and_columns_present(self):
        for text in [
            "通用积分 5h 累计余量", "通用积分 7d 累计余量",
            "Flash-Lite 专属积分 5h 累计余量", "Flash-Lite 专属积分 7d 累计余量",
            "5h 重置倒计时", "7d 重置倒计时",
            "通用 5h 余量", "FL 专属 5h 余量",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_quota_fetch_present(self):
        self.assertIn("/api/quota", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
