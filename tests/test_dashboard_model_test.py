import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class DashboardModelTestTest(unittest.TestCase):
    def test_panel_title_present(self):
        # 模型测试面板标题（Key 池卡片之后、运行参数行之前）。
        self.assertIn("模型测试", DASHBOARD_HTML)

    def test_all_panel_ids_present(self):
        for text in [
            'id="modeltest-model"',
            'id="modeltest-accounts"',
            'id="modeltest-prompt"',
            'id="modeltest-effort"',
            'id="modeltest-stream"',
            'id="modeltest-run"',
            'id="modeltest-results"',
            'id="modeltest-note"',
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_effort_options(self):
        # 推理强度下拉：none/关、low/低、medium/中（默认）、high/高。
        self.assertIn('value="none"', DASHBOARD_HTML)
        self.assertIn("关", DASHBOARD_HTML)
        self.assertIn('value="low"', DASHBOARD_HTML)
        self.assertIn("低", DASHBOARD_HTML)
        self.assertIn('value="medium"', DASHBOARD_HTML)
        self.assertIn("中", DASHBOARD_HTML)
        self.assertIn('value="high"', DASHBOARD_HTML)
        self.assertIn("高", DASHBOARD_HTML)

    def test_streaming_reader_present(self):
        # 仓库首个前端流式读取：parseSSE 走 fetch reader 逐帧解析。
        self.assertIn("function parseSSE", DASHBOARD_HTML)
        self.assertIn("getReader", DASHBOARD_HTML)

    def test_account_source_from_state(self):
        # 多选框选项来自 state.account_names。
        self.assertIn("account_names", DASHBOARD_HTML)

    def test_render_and_run_present(self):
        self.assertIn("function renderModelTest", DASHBOARD_HTML)
        self.assertIn("function runModelTest", DASHBOARD_HTML)

    def test_no_plaintext_api_keys(self):
        # 面板绝不渲染明文 api_keys（防误把真实 Key 拼进页面）。
        self.assertNotIn("api_keys", DASHBOARD_HTML)

    def test_error_event_creates_card_when_missing(self):
        # 账号在首个 token 前就失败（如 401/网络错误）时没有 start 事件，
        # error 分支必须兜底建卡，否则该账号在结果区完全不显示。
        self.assertIn("card = card || modelTestCard(obj.account);", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
