r"""批次 G / A23：compare_versions 防泄露扫描的三类假阳性。

bug.md A23：`compare_versions` 每轮报 24 条 fillin-leak 疑似项，`status=review`
又让 build 节点 HALT 等人工复核，24 条全是假阳性：
  ① 选择题化填空的选项按钮 `<button class="qc-option" data-correct="true">`
     —— 正确答案按 design 就是其中一个可见选项；
  ② `qc-reveal qc-solution` 延迟发放揭示区；
  ③ 题干/知识卡正文里的固有短语（同一措辞在双版都出现）。

根因分两层：
  * `evidence.html_card_pages` 把"可见但设计上含答案"的区域文本也算进页面，
     于是 ①② 必然命中；
  * PDF 侧 `option_sequences` 只吐半角 `甲/乙/丙`，而成品 PDF 渲染的是
     `（甲／乙／丙）`（全角括号 + 全角斜杠，见 render_smoke.py 的填空选项页
     判定）——选项区从来没被抹掉过。

本测试钉住：
  * ①② 在 HTML 侧被整体跳过（evidence.ANSWER_REGION_CLASSES）；
  * ③ 由 prose_index 降级为 info，`--strict-answers` 仍升级回 review；
  * **题干正文里出现不在选项中的答案仍然照旧报 review**（防泄露未放松）；
  * option_sequences 同时吐半角/全角分隔与括号包裹形态。

用法：python -B -m pytest scripts/test_batch_g_a23.py -q
"""
import os
import shutil
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

import compare_versions as cv  # noqa: E402
import evidence  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具：sec1.tex + 双版 HTML（结构对齐 render_html.render_card 的真实输出）
# ---------------------------------------------------------------------------

ANSWER = "交错多重线性函数"

# 固有版：题干/知识卡里固有出现"交错多重线性函数"，\fillin 答案也是它
# ——这正是 FP③ 的形态。
SEC_INHERENT = r"""% qid: sec1-q1
\section*{第一节}

\begin{microknowledge}[核心定义与记号]
记 $\delta$ 为交错多重线性函数；行列式是唯一满足三条公理的对象。
\end{microknowledge}

\begin{probchain}
\textbf{行列式唯一性定理}：三条公理。
\begin{enumerate}
\item 条件 (iii) 对应哪条命题？\fillin[交错多重线性函数]{2.4cm}
[线性性/交错多重线性函数/乘法性质]
\end{enumerate}
\begin{solution}
略。
\end{solution}
\begin{teacherNote}
略。
\end{teacherNote}
\end{probchain}

\begin{knowledgebox}[本节结论]
交错性。
\end{knowledgebox}
"""

# 泄露版：源切片里答案只出现在 \fillin[...] 参数内，题面正文不含它
SEC_LEAK = r"""% qid: sec1-q1
\section*{第一节}

\begin{probchain}
\textbf{行列式唯一性定理}：三条公理。
\begin{enumerate}
\item 条件 (iii) 对应哪条命题？\fillin[交错多重线性函数]{2.4cm}
[线性性/交错多重线性函数/乘法性质]
\end{enumerate}
\begin{solution}
略。
\end{solution}
\begin{teacherNote}
略。
\end{teacherNote}
\end{probchain}
"""


def card(cid, body):
    return ('<section class="qc-card" id="qc-card-%s">'
            '<div class="qc-card-head"><span class="qc-kind-badge">题</span>'
            '<span class="qc-page-no">第 1 / 1 页</span></div>'
            '<div class="qc-card-body">%s</div>'
            '<div class="qc-card-foot"><span class="qc-score">'
            '本页选择题 <b>0 / 0</b></span></div></section>'
            % (cid, body))


OPTIONS_BLOCK = (
    '<span class="qc-options" role="group" aria-label="选项">'
    '<button type="button" class="qc-option" data-correct="false">线性性</button>'
    '<button type="button" class="qc-option" data-correct="true">%s</button>'
    '<button type="button" class="qc-option" data-correct="false">乘法性质</button>'
    '</span>' % ANSWER)

REVEAL_HIDDEN = ('<div class="qc-reveal qc-solution" hidden>'
                 '<div class="qc-reveal-title">参考解答</div>'
                 '<div class="qc-reveal-body">条件 (iii) 对应 %s。</div></div>'
                 % ANSWER)

REVEAL_VISIBLE = ('<div class="qc-reveal qc-solution">'
                  '<div class="qc-reveal-title">参考解答</div>'
                  '<div class="qc-reveal-body">条件 (iii) 对应 %s。</div></div>'
                  % ANSWER)

TEACHER_NOTE = ('<div class="qc-teacher-note">'
                '<div class="qc-teacher-note-title">教学要点与易错分析</div>'
                '<div>略。</div></div>')


def stem_text(extra=""):
    return ('<p class="qc-stem">条件 (iii) 对应哪条命题？'
            '记 $\\delta$ 为交错多重线性函数。%s</p>' % extra)


def page(body):
    return '<ol class="qc-parts"><li><span class="qc-part-label">1.</span>' \
           '<span class="qc-part-html">%s</span></li></ol>' % body


def wrap(card_body):
    return "<html><body>\n" + card("CARD-01", card_body) + "\n</body></html>\n"


# 学生版：题干固有短语 + 选项按钮（正确答案可见）+ 隐藏揭示区
STU_HTML = wrap(stem_text() + page(OPTIONS_BLOCK) + REVEAL_HIDDEN)

# 教师版：同结构，揭示区可见 + 教学要点
TEA_HTML = wrap(stem_text() + page(OPTIONS_BLOCK) + REVEAL_VISIBLE + TEACHER_NOTE)

# 真泄露版：答案出现在学生版题干正文里（不在选项中）
LEAK_HTML = wrap(stem_text("答案就是 %s，直接抄。" % ANSWER)
                 + page(OPTIONS_BLOCK) + REVEAL_HIDDEN)

# 揭示区未带 hidden 属性（render 模式变化时的健壮性）
REVEAL_PLAIN_HTML = wrap(stem_text() + page(OPTIONS_BLOCK)
                         + REVEAL_VISIBLE.replace(
                             '<div class="qc-reveal qc-solution">',
                             '<div class="qc-reveal qc-solution">'))


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_workdir(stu_html=STU_HTML, sec_text=SEC_INHERENT):
    d = tempfile.mkdtemp()
    _write(os.path.join(d, "sec1.tex"), sec_text)
    _write(os.path.join(d, "student.html"), stu_html)
    _write(os.path.join(d, "teacher.html"), TEA_HTML)
    return d


class _Args(object):
    """compare_versions.main 的最小参数面。"""
    no_answer_check = False
    strict_answers = False
    diagnostic = False
    json = None
    answer_min_len = 4
    allow = []
    forbid = []


def run_compare(workdir, **kw):
    args = _Args()
    for k, v in kw.items():
        setattr(args, k, v)
    report = {"schema_version": evidence.SCHEMA_VERSION,
              "tool": "compare_versions.py",
              "tool_version": cv.TOOL_VERSION,
              "scope": {}, "input_digest": "", "pdf_sha256": None,
              "html_sha256": None, "status": "pass", "issues": [],
              "evidence": {}}
    raw_texts = {"sec1.tex": open(os.path.join(workdir, "sec1.tex"),
                                  encoding="utf-8").read()}
    src_texts = {"sec1.tex": cv.strip_comments(raw_texts["sec1.tex"])}
    problems = cv.compare_html_version(workdir, args, report,
                                       src_texts, raw_texts, [])
    return problems, report


def kinds(report, severity):
    return [i["kind"] for i in report["issues"] if i.get("severity") == severity]


class TestA23AnswerRegionSkip(unittest.TestCase):
    """①② 可见但设计上含答案的区域，整体不进泄露扫描。"""

    def test_option_buttons_are_not_a_leak(self):
        """选项按钮里的正确答案不是泄露（FP①）。"""
        d = make_workdir()
        try:
            problems, report = run_compare(d)
            self.assertEqual(problems, [], problems)
            self.assertNotIn("html-fillin-leak", kinds(report, "review"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_hidden_reveal_region_is_not_a_leak(self):
        """学生版 hidden 的 qc-reveal 揭示区不是泄露（FP②）。"""
        d = make_workdir()
        try:
            problems, report = run_compare(d)
            self.assertNotIn("html-fillin-leak", kinds(report, "review"))
            self.assertNotIn("html-fillin-leak", kinds(report, "info"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_reveal_without_hidden_attr_is_also_skipped(self):
        """未带 hidden 属性的 qc-reveal 同样跳过（render 模式变化时仍安全）。"""
        d = make_workdir(REVEAL_PLAIN_HTML)
        try:
            problems, report = run_compare(d)
            self.assertNotIn("html-fillin-leak", kinds(report, "review"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_html_card_pages_skip_classes(self):
        """html_card_pages(skip_classes=...) 与默认行为对比验证。"""
        d = make_workdir()
        try:
            p = os.path.join(d, "student.html")
            full = evidence.html_card_pages(p)
            skipped = evidence.html_card_pages(
                p, skip_classes=evidence.ANSWER_REGION_CLASSES)
            self.assertEqual([c for c, _ in full], [c for c, _ in skipped])
            # 选项区与揭示区文本被抹掉
            self.assertIn(ANSWER, full[0][1])
            self.assertIn("线性性", full[0][1])
            self.assertNotIn("线性性", skipped[0][1])
            self.assertNotIn("乘法性质", skipped[0][1])
            # 题干正文（含固有措辞）仍在——它不属于被跳过区域
            self.assertIn("交错多重线性函数", skipped[0][1])
            self.assertIn("对应哪条命题", skipped[0][1])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_real_leak_in_stem_is_still_review(self):
        """题干正文里出现不在选项中的答案 → 仍报 review（防泄露未放松）。"""
        d = make_workdir(LEAK_HTML, SEC_LEAK)
        try:
            problems, report = run_compare(d)
            self.assertEqual(problems, [], problems)
            self.assertIn("html-fillin-leak", kinds(report, "review"))
            leak = [i for i in report["issues"]
                    if i["kind"] == "html-fillin-leak"]
            self.assertTrue(leak, report["issues"])
            self.assertIn(ANSWER, leak[0]["message"])
            self.assertEqual(leak[0]["severity"], "review")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestA23InherentProseDowngrade(unittest.TestCase):
    """③ 题干固有短语降级为 info，strict 模式升级回 review。"""

    def test_inherent_phrase_is_info_not_review(self):
        d = make_workdir()
        try:
            problems, report = run_compare(d)
            self.assertIn("html-fillin-inherent", kinds(report, "info"))
            self.assertNotIn("html-fillin-inherent", kinds(report, "review"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_strict_answers_upgrades_inherent_back(self):
        d = make_workdir()
        try:
            problems, report = run_compare(d, strict_answers=True)
            self.assertIn("html-fillin-inherent", kinds(report, "review"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_prose_index_strips_answer_and_options(self):
        """prose_index 剔除 \\fillin 的答案/宽度/选项三段，只留题面正文。"""
        prose = cv.prose_index(SEC_INHERENT)
        self.assertIn("交错多重线性函数", prose)   # 知识卡里的固有措辞仍在
        self.assertNotIn("线性性", prose)          # 选项位被剔除
        self.assertNotIn("2.4cm", prose)           # 宽度位被剔除
        self.assertNotIn("乘法性质", prose)

    def test_prose_index_keeps_phrase_outside_fillin(self):
        """题面正文里（不在 \\fillin 参数内）的措辞仍然在索引中。"""
        prose = cv.prose_index(SEC_INHERENT)
        self.assertIn("对应哪条命题", prose)

    def test_prose_index_of_answer_only_in_fillin_is_empty(self):
        """答案只出现在 \\fillin 参数里 → 索引不含它（真泄露才有判别力）。"""
        prose = cv.prose_index(SEC_LEAK)
        self.assertNotIn(ANSWER, prose)
        self.assertNotIn("线性性", prose)


class TestA23OptionSequences(unittest.TestCase):
    """选项区在成品文本里的字面形态必须覆盖半角与全角两种渲染。"""

    FILLIN = r"\fillin[交错多重线性函数]{2.4cm}[线性性/交错多重线性函数/乘法性质]"

    def test_both_slash_forms_emitted(self):
        seqs = cv.option_sequences(self.FILLIN)
        self.assertIn("线性性/交错多重线性函数/乘法性质", seqs)
        self.assertIn("线性性／交错多重线性函数／乘法性质", seqs)

    def test_wrapped_forms_emitted(self):
        seqs = cv.option_sequences(self.FILLIN)
        self.assertIn("（线性性／交错多重线性函数／乘法性质）", seqs)
        self.assertIn("(线性性/交错多重线性函数/乘法性质)", seqs)

    def test_correct_option_is_not_stripped_alone(self):
        """C2 红线：绝不单独抹本空正确选项文本（否则制造防泄露盲区）。"""
        seqs = cv.option_sequences(self.FILLIN)
        self.assertNotIn(ANSWER, seqs)

    def test_distractors_are_emitted(self):
        seqs = cv.option_sequences(self.FILLIN)
        self.assertIn("线性性", seqs)
        self.assertIn("乘法性质", seqs)

    def test_no_options_yields_nothing(self):
        self.assertEqual(cv.option_sequences(r"\fillin[答案]{2cm}"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
