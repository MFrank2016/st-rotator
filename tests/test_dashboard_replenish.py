import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardReplenishTest(unittest.TestCase):
    def test_replenish_section_markup_present(self):
        # 运行参数卡片内新增自动补号设置块：opt-rp-* 输入齐全，Key 类型两种枚举可选。
        for text in [
            'id="opt-rp-enabled"',
            'id="opt-rp-target"',
            'id="opt-rp-interval"',
            'id="opt-rp-keyword"',
            'id="opt-rp-cap"',
            'id="opt-rp-sms-interval"',
            'id="opt-rp-sms-timeout"',
            'id="opt-rp-keyname"',
            'id="opt-rp-keytype"',
            'id="opt-rp-sms-token"',
            "自动补号",
            "API_KEY_TYPE_TOKEN_PLAN",
            "API_KEY_TYPE_METERED",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_replenish_status_rendered(self):
        # 状态卡从 state.replenish_status（worker 注入）读取，走独立的 renderReplenish。
        self.assertIn("function renderReplenish", DASHBOARD_HTML)
        self.assertIn("state.replenish_status", DASHBOARD_HTML)

    def test_blocked_cap_label(self):
        # blocked_cap ->「今日额度已用尽」（复用 .pill.invalid 红底红字）。
        self.assertIn("今日额度已用尽", DASHBOARD_HTML)
        self.assertIn("blocked_cap", DASHBOARD_HTML)

    def test_captcha_required_label(self):
        # captcha_required ->「需要滑块验证码」（复用 .pill.cooldown 琥珀色）。
        self.assertIn("需要滑块验证码", DASHBOARD_HTML)
        self.assertIn("captcha_required", DASHBOARD_HTML)

    def test_idle_label(self):
        # idle ->「已达目标」。
        self.assertIn("已达目标", DASHBOARD_HTML)

    def test_ok_label(self):
        # ok ->「补号成功」。
        self.assertIn("补号成功", DASHBOARD_HTML)

    def test_check_error_label(self):
        # check_error ->「补号失败」。
        self.assertIn("补号失败", DASHBOARD_HTML)

    def test_unavailable_label(self):
        # unavailable ->「不可用」。
        self.assertIn("不可用", DASHBOARD_HTML)

    def test_replenish_state_available_target(self):
        # 状态卡读取 state.replenish_state 的 available / target / used_phones / spend。
        self.assertIn("state.replenish_state", DASHBOARD_HTML)
        self.assertIn("used_phones", DASHBOARD_HTML)
        self.assertIn("start_balance", DASHBOARD_HTML)

    def test_settings_wired_to_api_options(self):
        # onApplyOptions 的 POST body 带 replenish 对象；sms_token 仅在非空时发送，
        # 且渲染时密码框永远置空（不泄露已存密钥）。
        self.assertIn("replenish: {", DASHBOARD_HTML)
        self.assertIn("body.replenish.sms_token", DASHBOARD_HTML)
        # sms_token 渲染时置空，但必须跳过正在输入的输入框（否则 2 秒轮询会抹掉用户输入）
        self.assertIn('var rps = $("opt-rp-sms-token");', DASHBOARD_HTML)
        self.assertIn(
            'if (document.activeElement !== rps) rps.value = "";', DASHBOARD_HTML
        )

    def test_options_dirty_tracking_pauses_poll_and_save_button(self):
        # 编辑任一运行参数字段即置脏并暂停 2 秒轮询回填，避免未保存改动被覆盖回旧值；
        # 保存成功后清脏。同时提供独立的「保存并应用」按钮。
        self.assertIn('id="btn-save-replenish"', DASHBOARD_HTML)
        self.assertIn("optionsDirty: false,", DASHBOARD_HTML)
        self.assertIn("if (S.optionsDirty) return;", DASHBOARD_HTML)
        self.assertIn("function markOptionsDirty", DASHBOARD_HTML)
        self.assertIn("S.optionsDirty = true;", DASHBOARD_HTML)
        self.assertIn("S.optionsDirty = false;", DASHBOARD_HTML)
        self.assertIn(
            '$("btn-save-replenish").onclick = onApplyOptions;', DASHBOARD_HTML
        )
        self.assertIn('el.addEventListener("input", markOptionsDirty);', DASHBOARD_HTML)
        self.assertIn('"opt-rp-enabled", "opt-rp-target"', DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
