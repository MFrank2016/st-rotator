"""dashboard.py 内联 JS 的 7 项修复回归（字符串契约断言，与既有 dashboard 测试同风格）。

内联 JS 不会在测试进程里执行，这里锁住关键结构，防止后续改动悄悄回退。
JS 语法本身用 ``node --check`` 手工验证过（不属于测试套件依赖）。
"""

import unittest

from st_rotator.dashboard import DASHBOARD_HTML


class SubkeyHistoryBootstrapTest(unittest.TestCase):
    """1. 子 Key 用量明细：首次进入时自动拉一次，之后不跟 2 秒轮询。"""

    def test_first_load_is_triggered_from_render_subkeys(self):
        self.assertIn(
            "var skHist = { page: 1, pageSize: 10, total: 0, loading: false, pending: 0, loaded: false };",
            DASHBOARD_HTML,
        )
        self.assertIn("if (!skHist.loaded) {", DASHBOARD_HTML)
        self.assertIn("loadSubkeyHistory(1);", DASHBOARD_HTML)
        # 失败后重新置位，允许下个 tick 再试
        self.assertIn("skHist.loaded = false;", DASHBOARD_HTML)


class InflightGuardTest(unittest.TestCase):
    """2. refreshState / pollLogs 的在途标志。"""

    def test_flags_exist_and_are_cleared_in_finally(self):
        self.assertIn("stateBusy: false,", DASHBOARD_HTML)
        self.assertIn("logBusy: false,", DASHBOARD_HTML)
        self.assertIn("if (S.logBusy) return;", DASHBOARD_HTML)
        self.assertIn("finally { S.logBusy = false; }", DASHBOARD_HTML)
        self.assertIn("S.stateBusy = false;", DASHBOARD_HTML)

    def test_state_timer_skips_when_busy(self):
        self.assertIn(
            "S.timerState = setInterval(function () { if (!S.stateBusy) refreshState(); }, 2000);",
            DASHBOARD_HTML,
        )


class StreamAppendTest(unittest.TestCase):
    """3. 流式输出改纯文本追加。"""

    def test_append_uses_text_content(self):
        self.assertIn("pre.textContent += text;", DASHBOARD_HTML)
        self.assertNotIn("innerHTML += esc(text)", DASHBOARD_HTML)


class ModelSelectDirtyTest(unittest.TestCase):
    """4. 模型下拉在用户未提交时不被轮询重置。"""

    def test_dirty_flag_guards_selection(self):
        self.assertIn("var modelSelectDirty = false;", DASHBOARD_HTML)
        self.assertIn(
            "var chosen = modelSelectDirty && select.value ? select.value : current;",
            DASHBOARD_HTML,
        )
        self.assertIn("modelSelectDirty = true;", DASHBOARD_HTML)
        # 声明 1 次 + 「应用为默认模型」成功后清 1 次
        self.assertEqual(DASHBOARD_HTML.count("modelSelectDirty = false;"), 2)


class MinResetTest(unittest.TestCase):
    """5. minReset 两侧都过滤过期时间。"""

    def test_expired_a_is_dropped(self):
        self.assertIn("if (a && a <= now) a = null;", DASHBOARD_HTML)
        # 既有契约（只统计未来 reset_at）保持不变
        self.assertIn("if (!b || b <= now) return a;", DASHBOARD_HTML)


class ReplenishNoteTest(unittest.TestCase):
    """6. note 只喂 textContent，不能再 esc。"""

    def test_note_is_raw_for_text_content(self):
        self.assertIn("if (rs.message) note = rs.message;", DASHBOARD_HTML)
        self.assertNotIn("note = esc(rs.message)", DASHBOARD_HTML)
        self.assertIn("noteEl.textContent = note;", DASHBOARD_HTML)


class FmtTimeTest(unittest.TestCase):
    """7. fmtTime 只剩一份（健壮版）。"""

    def test_single_definition(self):
        self.assertEqual(DASHBOARD_HTML.count("function fmtTime("), 1)
        self.assertIn('if (isNaN(d.getTime())) return "—";', DASHBOARD_HTML)
        self.assertIn("new Date(Number(value) * 1000)", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
