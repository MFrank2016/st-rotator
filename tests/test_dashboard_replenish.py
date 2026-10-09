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
        self.assertIn("last_balance", DASHBOARD_HTML)

    def test_consumed_uses_server_computed_value(self):
        # 「今日已用」直接用服务端算好的 rp.consumed（换天归零），不在前端用余额差。
        self.assertIn("rp.consumed", DASHBOARD_HTML)
        self.assertIn("换天自动归零", DASHBOARD_HTML)

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

    def test_separate_panel_markup(self):
        # 独立「自动补号」面板：统计卡片容器 + 面板内日志框 + 徽标 / 备注；
        # 配置表单（opt-rp-*）从「运行参数」卡片移入同一面板。
        for text in [
            'id="replenish-stats"',
            'id="replenish-logbox"',
            'id="replenish-badge"',
            'id="replenish-note"',
            'id="btn-clear-replenish-log"',
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_replenish_stats_render_six_metrics(self):
        # renderReplenish 往 #replenish-stats 填 6 项统计。
        self.assertIn('$("replenish-stats")', DASHBOARD_HTML)
        for text in [
            "当前余额",
            "今日已用",
            "取号数量",
            "注册成功",
            "已重置 Key",
            "已失效账号",
            "registrations_ok",
            "rotations",
            "invalid_accounts",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_replenish_logs_routed_to_panel(self):
        # [补号] 前缀的日志行投递到面板内日志框。
        self.assertIn("[补号]", DASHBOARD_HTML)
        self.assertIn("function appendReplenishLog", DASHBOARD_HTML)
        self.assertIn('item.text.indexOf("[补号]")', DASHBOARD_HTML)

    def test_main_log_hides_replenish_and_shows_datetime(self):
        # 主「实时日志」不显示 [补号] 行（只在补号面板显示）；每行前缀日期时间。
        self.assertIn("appendReplenishLog(item); return;", DASHBOARD_HTML)
        self.assertIn("function logLine", DASHBOARD_HTML)
        self.assertIn("item.time", DASHBOARD_HTML)


class DashboardReplenishRecordsTest(unittest.TestCase):
    """自动补号卡片内的注册记录分节 + 易码 Token 状态提示。"""

    def test_records_section_markup(self):
        for text in [
            "注册记录",
            'id="rp-reg-q"',
            'id="rp-reg-status"',
            'id="rp-reg-kind"',
            'id="rp-reg-search"',
            'id="rp-reg-table"',
            'id="rp-reg-prev"',
            'id="rp-reg-next"',
            'id="rp-reg-page"',
            "注册时间",
            "手机号",
            "用户名",
            "密码",
            "新号",
            "重置密码",
            "成功",
            "失败原因",
            "失败详情",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_records_render_function_and_pagination_state(self):
        self.assertIn("function renderReplenishRecords", DASHBOARD_HTML)
        self.assertIn("/api/replenish/registrations", DASHBOARD_HTML)
        self.assertIn("S.rpRegPage", DASHBOARD_HTML)
        self.assertIn("S.rpRegSize", DASHBOARD_HTML)

    def test_sms_token_flags_in_panel(self):
        self.assertIn("sms_token_configured", DASHBOARD_HTML)
        self.assertIn("sms_token_ok", DASHBOARD_HTML)
        self.assertIn("未配置易码 Token，补号未运行", DASHBOARD_HTML)
        self.assertIn("易码 Token 不可用", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
