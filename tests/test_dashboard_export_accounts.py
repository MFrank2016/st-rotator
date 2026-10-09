import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardExportAccountsTest(unittest.TestCase):
    def test_export_button_present(self):
        self.assertIn('id="btn-export-accounts"', DASHBOARD_HTML)
        self.assertIn("复制全部账号", DASHBOARD_HTML)

    def test_export_handler_defined(self):
        self.assertIn("function onExportAccounts", DASHBOARD_HTML)
        self.assertIn("/api/accounts/export", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
