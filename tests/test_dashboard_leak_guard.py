import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardLeakGuardTest(unittest.TestCase):
    def test_panel_elements_present(self):
        self.assertIn('id="leak-guard-stats"', DASHBOARD_HTML)
        self.assertIn('id="leak-guard-badge"', DASHBOARD_HTML)
        self.assertIn('id="leak-guard-pending"', DASHBOARD_HTML)

    def test_state_read_and_render_wired(self):
        # renderLeakGuard 从 state.leak_guard_status 读取，并在 refreshState 里被调用。
        self.assertIn("state.leak_guard_status", DASHBOARD_HTML)
        self.assertIn("renderLeakGuard(state)", DASHBOARD_HTML)

    def test_badges_defined(self):
        self.assertIn("LEAK_GUARD_BADGES", DASHBOARD_HTML)
        self.assertIn("疑似泄漏", DASHBOARD_HTML)

    def test_reuses_pill_class(self):
        self.assertIn('class="pill ', DASHBOARD_HTML)

    def test_rotate_action_wired(self):
        # 待轮换清单新增「操作」列与「更换 Key」按钮，点击调 /api/leak-guard/rotate。
        self.assertIn("操作", DASHBOARD_HTML)
        self.assertIn("更换 Key", DASHBOARD_HTML)
        self.assertIn("data-rotate=", DASHBOARD_HTML)
        self.assertIn("function onLeakGuardRotate", DASHBOARD_HTML)
        self.assertIn("/api/leak-guard/rotate", DASHBOARD_HTML)


class DashboardPoolRotateTest(unittest.TestCase):
    def test_pool_rotate_button_wired(self):
        self.assertIn("轮换 Key", DASHBOARD_HTML)
        self.assertIn('data-act="rotate"', DASHBOARD_HTML)
        self.assertIn("function onPoolRotate", DASHBOARD_HTML)
        self.assertIn("/api/pool/rotate", DASHBOARD_HTML)


class DashboardRemoveAccountTest(unittest.TestCase):
    def test_remove_account_button_wired(self):
        self.assertIn("删账号", DASHBOARD_HTML)
        self.assertIn('data-act="remove-account"', DASHBOARD_HTML)
        self.assertIn("function onRemoveAccount", DASHBOARD_HTML)
        self.assertIn("/api/accounts/remove", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
