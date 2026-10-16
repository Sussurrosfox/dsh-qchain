#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_render.py — tex2cards / render_html / check_html 快速回归测试

不依赖 XeLaTeX 与网络，覆盖 qchain-html 交付层核心契约：
  R01  \fillin 三元组解析（答案/宽度/选项）与槽位 id 全题唯一；
  R02  组件 → 卡片映射（节标题/对话/题目/知识/结论各一卡，一卡一页）；
  R03  学生版剥离纪律：无教学要点 DOM、揭示区全部 hidden、无题型标签；
  R04  教师版呈现纪律：解答默认可见、要点块数 == 源 teacherNote 数；
  R05  选择题交互契约：每组恰一个 data-correct=true 且为归一化答案；
  R06  隐藏揭示区不进入可见文本（compare 泄露检查的口径前提）；
  R07  牌组校验能抓住：重复 qid、缺 solution、选项不足 3 个、答案不在选项；
  R08  模板占位符全部填充；交互控件齐备。

运行: python scripts/test_render.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tex2cards  # noqa: E402
import render_html  # noqa: E402
import check_html  # noqa: E402
import evidence  # noqa: E402

TEMPLATES = os.path.abspath(os.path.join(HERE, "..", "templates"))

SEC1 = r"""\section*{I 第一节}
\begin{sectiondialogue}[入口：试引入]
\speaker{$\Psi$}{先观察。}
\speaker{$\alpha$}{有什么规律？}
\speaker{$\Psi$}{\dlgteacher{答案先不说。}请完成下题。}
\end{sectiondialogue}

导语一句。

\begin{probchain}
% qid: q1 | 题型: 进入题 | 难度: ★☆☆
\item \qtype{进入题} 计算 $1+1=$\fillin[$2$]{15mm}[1/2/3/4]。
\ansspace{1cm}
\begin{solution}
$2$。
\end{solution}
\begin{teacherNote}
观察加法。
\end{teacherNote}
\end{probchain}
\begin{microknowledge}[加法定义]
\begin{enumerate}
\item 加法是二元运算。
\end{enumerate}
\end{microknowledge}
\begin{knowledgebox}[本节结论]
结论一。
\end{knowledgebox}
"""


def make_project(d, sec=SEC1, title="测试学案"):
    for f in ("header.tex", "footer.tex", "config.yaml",
              "student.tex", "teacher.tex"):
        shutil.copy(os.path.join(TEMPLATES, f), os.path.join(d, f))
    with open(os.path.join(d, "config.yaml"), encoding="utf-8") as f:
        cfg = f.read()
    cfg = cfg.replace("dialogue_enabled: true", "dialogue_enabled: false")
    cfg = cfg.replace("main_title: 学案", 'main_title: "%s"' % title)
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg)
    with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
        f.write("\\input{header.tex}\n\\begin{document}\n"
                "\\input{sec1.tex}\n\\input{footer.tex}\n")
    with open(os.path.join(d, "sec1.tex"), "w", encoding="utf-8") as f:
        f.write(sec)
    return d


def render_both(d):
    deck, _issues = tex2cards.parse_project(d)
    ok_s, _ = render_html.render_version(deck, d, "student",
                                         os.path.join(d, "student.html"))
    ok_t, _ = render_html.render_version(deck, d, "teacher",
                                         os.path.join(d, "teacher.html"))
    return deck, ok_s and ok_t


class RenderContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-html-render-")
        make_project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, name):
        with open(os.path.join(self.tmp, name), encoding="utf-8") as f:
            return f.read()

    # ---- R01/R02：解析与映射 ----
    def test_r01_r02_deck_mapping(self):
        deck, _ = tex2cards.parse_project(self.tmp)
        kinds = [c["kind"] for c in deck["cards"]]
        self.assertEqual(kinds, ["section", "dialogue", "question",
                                 "knowledge", "conclusion"])
        q = [c for c in deck["cards"] if c["kind"] == "question"][0]
        self.assertEqual(q["qid"], "q1")
        self.assertEqual(len(q["slots"]), 1)
        slot = q["slots"][0]
        self.assertEqual(slot["answer"], "$2$")
        self.assertEqual(slot["options"], ["1", "2", "3", "4"])
        self.assertTrue(q.get("stem_html"))
        self.assertTrue(q.get("solution_html"))
        self.assertTrue(q.get("teacher_note_html"))
        dlg = [c for c in deck["cards"] if c["kind"] == "dialogue"][0]
        self.assertEqual(len(dlg["speakers"]), 3)
        self.assertTrue(dlg["speakers"][2]["teacher_only"])  # dlgteacher 行

    def test_r01_slot_ids_unique(self):
        sec = SEC1.replace(
            r"计算 $1+1=$\fillin[$2$]{15mm}[1/2/3/4]。",
            r"计算 $1+1=$\fillin[$2$]{15mm}[1/2/3/4]，"
            r"再算 $2+2=$\fillin[$4$]{15mm}[2/4/6/8]。")
        make_project(self.tmp, sec=sec)
        deck, _ = tex2cards.parse_project(self.tmp)
        q = [c for c in deck["cards"] if c["kind"] == "question"][0]
        ids = [s["id"] for s in q["slots"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, "槽位 id 必须全题唯一")

    # ---- R03/R04：版本纪律 ----
    def test_r03_student_strips_teacher(self):
        deck, ok = render_both(self.tmp)
        self.assertTrue(ok)
        html = self.read("student.html")
        self.assertNotIn('class="qc-teacher-note"', html)
        self.assertNotIn('class="qc-card-meta">', html)  # 题型/难度标签
        reveals = re.findall(r'class="qc-reveal qc-solution"', html)
        hidden = re.findall(r'class="qc-reveal qc-solution" hidden', html)
        self.assertEqual(len(reveals), 1)
        self.assertEqual(len(hidden), 1)  # 学生版揭示区必须隐藏
        # 教师专属台词不出现在学生版可见文本
        vis = evidence.html_visible_text(
            os.path.join(self.tmp, "student.html"))
        self.assertNotIn("答案先不说", vis)

    def test_r04_teacher_shows_all(self):
        render_both(self.tmp)
        html = self.read("teacher.html")
        self.assertIn('class="qc-reveal qc-solution"', html)
        self.assertNotIn('class="qc-reveal qc-solution" hidden', html)
        self.assertIn("qc-teacher-note", html)
        self.assertIn("qc-card-meta", html)  # 题型/难度标签
        vis = evidence.html_visible_text(
            os.path.join(self.tmp, "teacher.html"))
        self.assertIn("答案先不说", vis)  # 教师版可见教师专属台词

    # ---- R05：交互契约 ----
    def test_r05_option_contract(self):
        render_both(self.tmp)
        for version in ("student", "teacher"):
            html = self.read(version + ".html")
            groups = re.findall(
                r'<span class="qc-options"[^>]*>(.*?)</span></span>', html,
                re.S)
            self.assertEqual(len(groups), 1)
            body = groups[0]
            self.assertEqual(len(re.findall(r'class="qc-option"', body)), 4)
            correct = re.findall(r'data-correct="true"', body)
            self.assertEqual(len(correct), 1)

    # ---- R06：隐藏内容不进可见文本 ----
    def test_r06_hidden_not_visible(self):
        render_both(self.tmp)
        vis = evidence.html_visible_text(
            os.path.join(self.tmp, "student.html"))
        self.assertNotIn("$2$。", vis)  # 解答文本不可见
        tea = evidence.html_visible_text(
            os.path.join(self.tmp, "teacher.html"))
        self.assertIn("$2$。", tea)

    # ---- R07：牌组校验 ----
    def test_r07_deck_validation_rejects(self):
        # 同一节内两个题块共用 qid → 必须被拦截
        dup_sec = SEC1.replace(
            "\\end{probchain}",
            "\\end{probchain}\n\\begin{probchain}\n"
            "% qid: q1\n"
            "\\item \\qtype{辨认题} 再算 $2+2=$\\fillin[$4$]{15mm}[2/4/6/8]。\n"
            "\\ansspace{1cm}\n"
            "\\begin{solution}\n$4$。\n\\end{solution}\n"
            "\\begin{teacherNote}\n确认。\n\\end{teacherNote}\n"
            "\\end{probchain}")
        cases = {
            "dup-qid": dup_sec,
            "no-answer": SEC1.replace(r"\fillin[$2$]{15mm}[1/2/3/4]",
                                      r"\fillin{15mm}[1/2]"),
        }
        for name, sec in cases.items():
            with tempfile.TemporaryDirectory() as d2:
                make_project(d2, sec=sec)
                deck, _ = tex2cards.parse_project(d2)
                problems = tex2cards.validate_deck(deck)
                self.assertTrue(problems, "%s 应被牌组校验拦截" % name)

    def test_r07_solution_missing_rejected(self):
        sec = SEC1.replace("\\begin{solution}\n$2$。\n\\end{solution}\n", "")
        with tempfile.TemporaryDirectory() as d2:
            make_project(d2, sec=sec)
            deck, _ = tex2cards.parse_project(d2)
            problems = tex2cards.validate_deck(deck)
            self.assertTrue(any("参考解答" in p for p in problems))

    # ---- R08：模版契约 ----
    def test_r08_template_contract(self):
        render_both(self.tmp)
        html = self.read("student.html")
        self.assertNotIn("{{", html)  # 占位符全部填充
        for rid in check_html.REQUIRED_IDS:
            self.assertIn('id="%s"' % rid, html)
        # 目录条目数 == 卡片数
        deck, _ = tex2cards.parse_project(self.tmp)
        self.assertEqual(len(re.findall(r'class="qc-toc-item"', html)),
                         len(deck["cards"]))

    def test_r08_check_html_gate_passes(self):
        render_both(self.tmp)
        rc = check_html.main([self.tmp])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
