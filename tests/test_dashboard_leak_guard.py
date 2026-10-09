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


if __name__ == "__main__":
    unittest.main()
