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

    def test_quota_refresh_decoupled_from_state_poll(self):
        # C1：余量不再挂在 2s 的 refreshState 上，而是独立的 ~60s 节奏。
        self.assertIn(
            "S.timerQuota = setInterval(function () { refreshQuota(false); }, 60000);", DASHBOARD_HTML
        )
        self.assertIn("refreshState().then(function () { refreshQuota(false); refreshUsage(); refreshSamples(); });", DASHBOARD_HTML)

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

    def test_manual_refresh_quota_button_present_and_wired(self):
        # spec §8.3 手动「刷新余量」按钮存在，且强制刷新（?refresh=1）。
        self.assertIn('id="btn-refresh-quota"', DASHBOARD_HTML)
        self.assertIn("刷新余量", DASHBOARD_HTML)
        self.assertIn("fetchQuota(true)", DASHBOARD_HTML)


class DashboardImportTest(unittest.TestCase):
    def test_import_ui_present(self):
        for text in ["批量新增", 'id="import-dialog"', "/api/keys/import", "手机--用户名--密码--apikey"]:
            self.assertIn(text, DASHBOARD_HTML)


class DashboardUsageTest(unittest.TestCase):
    def test_usage_chart_present(self):
        for text in ["Token 消耗（近 24 小时）", 'id="usage-chart"', "/api/usage?hours=24", "function renderUsage"]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_kpi_values_rounded(self):
        # 聚合卡片与余量列取整，不显示小数。
        self.assertIn("fmtInt(Math.round(agg[key]))", DASHBOARD_HTML)
        self.assertIn("fmtInt(Math.round(remaining))", DASHBOARD_HTML)


class DashboardCreditsTest(unittest.TestCase):
    def test_consumption_cards_present(self):
        for text in [
            "近1h 通用积分消耗", "近5h 通用积分消耗", "近24h 通用积分消耗", "近7d 通用积分消耗", "近30d 通用积分消耗",
            "近1h 专属积分消耗", "近5h 专属积分消耗", "近24h 专属积分消耗", "近7d 专属积分消耗", "近30d 专属积分消耗",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_chart_beautified(self):
        # 输入=蓝、输出=橙；自定义悬停 tooltip；横坐标小时标签。
        for text in ['fill="#5b93ff"', 'fill="#f0a429"', 'id="usage-tip"', 'closest("g[data-tip]")',
                     '"#34d399"', '"#a78bfa"', "function tipText"]:
            self.assertIn(text, DASHBOARD_HTML)


class DashboardSamplesTest(unittest.TestCase):
    def test_samples_panel_present(self):
        for text in ["积分采样明细（仅有差异）", 'id="samples"', "/api/credits/samples?nonzero=1", "function renderSamples"]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_reset_countdown_ignores_past(self):
        # 倒计时只统计“未来”的 reset_at，避免个别账号窗口刚过期时整张卡显示成 —。
        self.assertIn("function minReset(a, b)", DASHBOARD_HTML)
        self.assertIn("b <= now", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
