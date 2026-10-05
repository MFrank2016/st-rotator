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

    def test_countdown_formats_match_window(self):
        # 5h 窗口用短格式（X h Y m），7d 窗口用长格式（X d Y h Z m）。
        self.assertIn("fmtCountdown(agg.reset5, false)", DASHBOARD_HTML)
        self.assertIn("fmtCountdown(agg.reset7, true)", DASHBOARD_HTML)
        # Key 池：5h 重置列短格式，7d 重置列长格式。
        self.assertIn('countdownCell(quotaResetAt(general, "h5"))', DASHBOARD_HTML)
        self.assertIn('countdownCell(quotaResetAt(general, "d7"), true)', DASHBOARD_HTML)

    def test_aggregate_cards_use_guard(self):
        # 无可用账号时聚合卡片显示 —，走 aggText 守卫。
        for key in ("g5", "g7", "f5", "f7"):
            self.assertIn('aggText(agg, "%s")' % key, DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
