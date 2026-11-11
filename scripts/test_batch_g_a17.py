# -*- coding: utf-8 -*-
"""A17：\fillin 参数内嵌方括号（列向量/区间/矩阵记号）不得被截断。

bug.md A17：check_numbering._scan_bracket 只做花括号分组感知、遇第一个 ]
即闭合 → `\fillin[\\det([\\bm a_2,\\bm a_1])]{2cm}[...]` 的答案被截断成
`\\det([\\bm a_2,\\bm a_1`，其后不再是 {宽度}，整个 \\fillin 被静默跳过。
后果：选择题化填空门禁对它失效，shuffle 失去重排依据，而 compare_versions
却能把同一个答案提取出来（三处口径不一致）。

修法：check_numbering.scan_bracket 增加 [] 嵌套深度（与 compare_versions.py:70-97
extract_fillin_answers 同口径），tex2cards 改为复用同一实现，删掉自带的分叉副本。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
for _p in (PLUGIN_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_numbering as cn  # noqa: E402
import tex2cards  # noqa: E402


def _fillin_gate_problems(body):
    """跑选择题化填空门禁，返回问题列表。"""
    problems = []
    cn.check_fillin_options("sec1.tex", body, problems)
    return problems


class TBracketScan(unittest.TestCase):
    """scan_bracket 的 [] 嵌套语义。"""

    def test_flat_bracket_unchanged(self):
        k, content, ok = cn.scan_bracket("[0]{2cm}", 0)
        self.assertTrue(ok)
        self.assertEqual(content, "0")
        self.assertEqual(k, 3)

    def test_nested_bracket_answer(self):
        text = "[\\det([\\bm a_2,\\bm a_1])]{2cm}[x/y/z]"
        k, content, ok = cn.scan_bracket(text, 0)
        self.assertTrue(ok)
        self.assertEqual(content, "\\det([\\bm a_2,\\bm a_1])")
        self.assertEqual(text[k], "{")

    def test_bracket_inside_braces(self):
        # {} 分组内的 ] 不闭合；[] 嵌套也按深度
        text = "[\\mathbf{[a]}]{2cm}[x/y/z]"
        k, content, ok = cn.scan_bracket(text, 0)
        self.assertTrue(ok)
        self.assertEqual(content, "\\mathbf{[a]}")
        self.assertEqual(text[k], "{")

    def test_display_math_delimiter_is_escaped(self):
        # \[ ... \] 作为转义对整体吃掉，不构成参数边界
        text = "[a\\]b]{2cm}"
        k, content, ok = cn.scan_bracket(text, 0)
        self.assertTrue(ok)
        self.assertEqual(content, "a\\]b")

    def test_unclosed_returns_false(self):
        k, content, ok = cn.scan_bracket("[abc", 0)
        self.assertFalse(ok)
        self.assertEqual(content, "abc")


class TScanFillins(unittest.TestCase):
    """scan_fillins：含方括号的答案/选项都应被完整取出。"""

    def test_det_bracket_vector_fillin(self):
        body = ("设 $A=(\\bm a_1,\\bm a_2)$，则 $\\det A=\\fillin"
                "[\\det([\\bm a_2,\\bm a_1])]{2cm}"
                "[\\det([\\bm a_2,\\bm a_1])/\\det([\\bm a_1,\\bm a_2])/0]$。")
        got = cn.scan_fillins(body)
        self.assertEqual(len(got), 1)
        _pos, answer, width, options = got[0]
        self.assertEqual(answer, "\\det([\\bm a_2,\\bm a_1])")
        self.assertEqual(width, "2cm")
        self.assertEqual(options, "\\det([\\bm a_2,\\bm a_1])/"
                                  "\\det([\\bm a_1,\\bm a_2])/0")

    def test_interval_answer_fillin(self):
        body = "解集为\\fillin[[0,\\pi/2]]{2cm}[[0,\\pi/2]/[0,\\pi]/[0,1]]。"
        got = cn.scan_fillins(body)
        self.assertEqual(len(got), 1)
        _pos, answer, _w, options = got[0]
        self.assertEqual(answer, "[0,\\pi/2]")
        self.assertEqual(options, "[0,\\pi/2]/[0,\\pi]/[0,1]")

    def test_plain_fillin_still_parses(self):
        body = "答案是\\fillin[0]{2cm}[0/1/2]。"
        got = cn.scan_fillins(body)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], "0")
        self.assertEqual(got[0][3], "0/1/2")

    def test_unclosed_answer_skipped_not_crash(self):
        body = "\\fillin[abc{2cm}[0/1/2]"
        self.assertEqual(cn.scan_fillins(body), [])


class TSplitOptions(unittest.TestCase):
    """split_options：[] 内的 / 不是选项分隔符（A17 的第二半）。"""

    def test_interval_options_not_split_inside_brackets(self):
        self.assertEqual(
            cn.split_options("[0,\\pi/2]/[0,\\pi]/[0,1]"),
            ["[0,\\pi/2]", "[0,\\pi]", "[0,1]"])

    def test_braced_slash_still_protected(self):
        self.assertEqual(
            cn.split_options("{\\mathbf{Z}/p\\mathbf{Z}}/\\mathbb{Q}/\\mathbb{R}"),
            ["{\\mathbf{Z}/p\\mathbf{Z}}", "\\mathbb{Q}", "\\mathbb{R}"])

    def test_escaped_slash_still_protected(self):
        self.assertEqual(cn.split_options("a\\/b/c"), ["a\\/b", "c"])

    def test_plain_options_unchanged(self):
        self.assertEqual(cn.split_options("0/1/2"), ["0", "1", "2"])


class TFillinGate(unittest.TestCase):
    """门禁层：修好后不再误报，且该抓的仍然抓得到。"""

    def test_bracket_answer_passes_gate(self):
        body = ("\\fillin[\\det([\\bm a_2,\\bm a_1])]{2cm}"
                "[\\det([\\bm a_2,\\bm a_1])/\\det([\\bm a_1,\\bm a_2])/0]")
        self.assertEqual(_fillin_gate_problems(body), [])

    def test_interval_answer_passes_gate(self):
        body = "\\fillin[[0,\\pi/2]]{2cm}[[0,\\pi/2]/[0,\\pi]/[0,1]]"
        self.assertEqual(_fillin_gate_problems(body), [])

    def test_missing_options_still_reported(self):
        body = "\\fillin[0]{2cm}"
        problems = _fillin_gate_problems(body)
        self.assertEqual(len(problems), 1)
        self.assertIn("未配置选项", problems[0])

    def test_too_few_options_still_reported(self):
        body = "\\fillin[0]{2cm}[0/1]"
        problems = _fillin_gate_problems(body)
        self.assertEqual(len(problems), 1)
        self.assertIn("至少 3 个", problems[0])

    def test_answer_not_in_options_still_reported(self):
        body = "\\fillin[0]{2cm}[1/2/3]"
        problems = _fillin_gate_problems(body)
        self.assertEqual(len(problems), 1)
        self.assertIn("不在选项", problems[0])


class TTex2cardsSameSemantics(unittest.TestCase):
    """tex2cards 复用同一实现：不再出现三处口径分叉。"""

    def test_shared_implementation(self):
        self.assertIs(tex2cards.scan_bracket, cn.scan_bracket)

    def test_fillin_slot_records_full_bracket_answer(self):
        # \fillin 的 answers/options 进 cards.json 的 slots；截断会让槽位答案
        # 与选项对不上，data-correct 全标 false。
        body = ("$\\det A=\\fillin[\\det([\\bm a_2,\\bm a_1])]{2cm}"
                "[\\det([\\bm a_2,\\bm a_1])/\\det([\\bm a_1,\\bm a_2])/0]$")
        ctx = tex2cards.InlineCtx("sec1")
        html = tex2cards.inline_to_html(body, ctx)
        self.assertEqual(len(ctx.slots), 1)
        slot = ctx.slots[0]
        self.assertEqual(slot["answer"], "\\det([\\bm a_2,\\bm a_1])")
        self.assertEqual(slot["options"],
                         ["\\det([\\bm a_2,\\bm a_1])",
                          "\\det([\\bm a_1,\\bm a_2])", "0"])
        # 正确选项必须被标成 data-correct="true"
        self.assertIn('data-correct="true"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
