import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardAutoRenewTest(unittest.TestCase):
    def test_password_error_badge_text_present(self):
        # 账号列对 password_error 显示「密码错误」徽标文案。
        self.assertIn("密码错误", DASHBOARD_HTML)

    def test_account_status_read_from_state(self):
        # renderPool 从 state.account_status 读取每账号状态（worker 注入）。
        self.assertIn("state.account_status", DASHBOARD_HTML)

    def test_password_error_condition(self):
        # 徽标仅当账号状态精确等于 password_error 时渲染。
        self.assertIn('status === "password_error"', DASHBOARD_HTML)

    def test_badge_reuses_pill_invalid_class(self):
        # 复用现有 .pill.invalid 红底红字样式，不新增 CSS 类。
        self.assertIn('class="pill invalid"', DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
