#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A18 对话门禁假阳性回归：绑定注释前置 / 标题参数 / 设问句 / 纯文字答案 /
悬置词表同源。

四项假阳性（生产 1.3.tex 实证，sec1/sec3/sec4/sec8）：
 ① `% dialogue: qid=secN-q1` 写在 \begin{exampledialogue} 之前 → 连报两条；
 ② `\begin{sectiondialogue}[交换两列，值变不变]` 的方括号标题被纳入台词扫描；
 ③ 设问句（以 ？ 结尾）中被"提出"的术语被判答案泄露；
 ④ sectiondialogue 与全文件 \fillin 纯中文/英文答案字面比对误报。

保留能力（反向断言）：exampledialogue 绑定到具体题目仍全量检查，
sectiondialogue 的数学表达式答案仍检查，末句悬置原则仍强制执行。

夹具语法（照 templates 规范）：对话台词必须写成 \\speaker{角色}{台词}，
题目 qid 由 `% qid: <id>` 注释声明。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_dialogue as cd  # noqa: E402

CONFIG = ("dialogue_enabled: true\n"
          "dialogue_section_opening: true\n"
          "dialogue_example_opening: true\n"
          "dialogue_require_coverage_hook: false\n"
          "dialogue_max_section_turns: 8\n"
          "dialogue_max_example_turns: 8\n")


def _mkworkdir(tex):
    d = tempfile.mkdtemp(prefix="qchain-a18-")
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(CONFIG)
    with open(os.path.join(d, "sec1.tex"), "w", encoding="utf-8") as fh:
        fh.write(tex)
    return d


def _scan(tex):
    d = _mkworkdir(tex)
    try:
        return cd.scan_file(d, "sec1.tex")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _codes(problems):
    out = []
    for p in problems:
        if "[ERROR] " in p:
            out.append(p.split("[ERROR] ", 1)[1].split(":", 1)[0])
        elif "[WARN] " in p:
            out.append(p.split("[WARN] ", 1)[1].split(":", 1)[0])
    return out


def _run(tex):
    """走真实入口 check(workdir)（内含 config 读取、coverage、逐项规则）。"""
    d = _mkworkdir(tex)
    try:
        problems, _warnings, _ev = cd.check(d)
        return problems
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _sec(*body):
    return "\\section*{第一节}\n" + "\n".join(body) + "\n"


def _dlg(env, *speakers, **kw):
    title = "[%s]" % kw["title"] if kw.get("title") else ""
    bind = ("%% dialogue: qid=%s\n" % kw["qid"]) if kw.get("qid") else ""
    inner = "\n".join("\\speaker{%s}{%s}" % s for s in speakers)
    return bind + "\\begin{%s}%s\n%s\n\\end{%s}\n" % (env, title, inner, env)


def _probchain(*body, qid=None):
    head = ("%% qid: %s\n" % qid) if qid else ""
    return "\\begin{probchain}\n" + head + "\n".join(body) + \
           "\n\\end{probchain}\n"


# ────────────────────────── ① 绑定注释前置 ──────────────────────────
# 生产形态：注释写在 begin 之前；且台词里含设问（不触发 ②/③/④ 之外的误报）
SEC_PRE_BIND = _sec(
    _dlg("exampledialogue",
         ("$\\Psi$", "把两个公式的分子分母分别记牢，你凭什么说能复述？"),
         ("$\\gamma$", "我试一次，你帮我验算一下？"),
         qid="sec1-q1", title="引例"),
    _probchain("\\item \\qtype{进入题} 分数问题。",
               "\\fillin[分子]{2cm}[分子/分母/约分]",
               "\\begin{solution}\n略。\n\\end{solution}",
               qid="sec1-q1"))


class TestBindingCommentBeforeBegin(unittest.TestCase):
    def test_pre_begin_binding_is_accepted(self):
        info = _scan(SEC_PRE_BIND)
        self.assertTrue(info["blocks"], "exampledialogue 应被识别")
        self.assertEqual(info["blocks"][0]["qid"], "sec1-q1",
                         "写在 begin 之前的 % dialogue: 注释应被认领")

    def test_no_double_binding_errors(self):
        problems = _run(SEC_PRE_BIND)
        codes = _codes(problems)
        self.assertNotIn("dialogue-binding-missing", codes)
        self.assertNotIn("dialogue-unbound-entry", codes)
        self.assertNotIn("exampledialogue 缺少", codes)

    def test_blank_line_inside_comment_span(self):
        tex = _sec(
            "% dialogue: qid=sec1-q1\n\n" +
            _dlg("exampledialogue",
                 ("$\\Psi$", "这个猜测难道成立？"),
                 ("$\\gamma$", "未必，我们动笔验算？"),
                 qid="sec1-q1"),
            _probchain("\\item \\qtype{进入题} 别的题。",
                       qid="sec1-q1"))
        info = _scan(tex)
        self.assertEqual(info["blocks"][0]["qid"], "sec1-q1",
                         "注释与 begin 之间的空行不得打断绑定")

    def test_distant_comment_not_bound(self):
        """注释被实质性内容隔开（另一道题）时不得跨题误绑。"""
        tex = _sec(
            "% dialogue: qid=sec1-q1",
            _probchain("\\item \\qtype{进入题} 另一道题。",
                       qid="sec1-q9"),
            _dlg("exampledialogue",
                 ("$\\Psi$", "这个猜测难道成立？"),
                 ("$\\gamma$", "未必，我们动笔验算？")),
            _probchain("\\item \\qtype{进入题} 本题。",
                       qid="sec1-q1"))
        info = _scan(tex)
        self.assertIsNone(info["blocks"][0]["qid"],
                          "远处注释不得跨题误绑")

    def test_truly_missing_binding_still_fails(self):
        tex = _sec(
            _dlg("exampledialogue",
                 ("$\\Psi$", "这个猜测难道成立？"),
                 ("$\\gamma$", "未必，我们动笔验算？")),
            _probchain("\\item \\qtype{进入题} 本题。",
                       qid="sec1-q1"))
        problems = _run(tex)
        self.assertIn("exampledialogue 缺少", " ".join(problems))


# ────────────────────── ② sectiondialogue 方括号标题 ──────────────────────
# 生产 sec3 原样：标题「交换两列，值变不变」与 \fillin[不变] 字面冲突
SEC_TITLE_FP = _sec(
    _dlg("sectiondialogue",
         ("$\\Psi$", "交换两列以后，行列式的值会怎样？"),
         ("$\\gamma$", "好像变号了？我动笔验算一下。"),
         title="交换两列，值变不变"),
    _probchain("\\item \\qtype{性质题} 行列式的性质。",
               "\\fillin[不变]{2cm}[变号/不变/变成两倍]"))


class TestSectiondialogueTitleIsNotDialogueText(unittest.TestCase):
    def test_title_argument_not_scanned(self):
        problems = _run(SEC_TITLE_FP)
        self.assertNotIn("dialogue-answer-leak", _codes(problems),
                         "方括号标题不是台词，不得参与泄露判定")

    def test_bracketed_title_still_parsed(self):
        info = _scan(SEC_TITLE_FP)
        self.assertEqual(len(info["blocks"]), 1)
        self.assertEqual(info["blocks"][0]["env"], "sectiondialogue")
        self.assertEqual(info["blocks"][0]["speakers"], 2)

    def test_math_answer_in_body_still_caught(self):
        """标题豁免只针对方括号参数；台词正文里的数学答案仍须抓。"""
        tex = _sec(
            _dlg("sectiondialogue",
                 ("$\\Psi$", "交换两列以后，这个比值为何偏偏成立？"),
                 ("$\\gamma$", "记住，答案就是 $D_1/D$。"),
                 title="随便的标题"),
            _probchain("\\item \\qtype{性质题} 形状问题。",
                       "\\fillin[$D_1/D$]{2cm}[$D_1/D$/$D_2/D$/1]"))
        problems = _run(tex)
        self.assertIn("dialogue-answer-leak", _codes(problems),
                      "台词正文里的真泄露仍须抓")


# ────────────────────────── ③ 设问句豁免 ──────────────────────────
# 生产 sec1「分子」/ sec4「$D_1/D$」原样：台词均为设问句
SEC_QUESTION_FP = _sec(
    _dlg("exampledialogue",
         ("$\\Psi$", "这个形状为何偏偏成立？"),
         ("$\\gamma$", "因为对称性，我证明给你看？"),
         qid="sec1-q1"),
    _probchain("\\item \\qtype{形状题} 形状问题。",
               "\\fillin[$D_1/D$]{2cm}[$D_1/D$/$D_2/D$/1]",
               "\\begin{solution}\n略。\n\\end{solution}",
               qid="sec1-q1"))


class TestQuestionEndingExemption(unittest.TestCase):
    def test_question_ending_is_exempt(self):
        problems = _run(SEC_QUESTION_FP)
        self.assertNotIn("dialogue-answer-leak", _codes(problems),
                         "设问句是在'提出'术语，不是在'给出'答案")

    def test_assertion_ending_still_caught(self):
        tex = _sec(
            _dlg("exampledialogue",
                 ("$\\Psi$", "这个比值就是 $D_1/D$，你记住。"),
                 qid="sec1-q1"),
            _probchain("\\item \\qtype{形状题} 形状问题。",
                       "\\fillin[$D_1/D$]{2cm}[$D_1/D$/$D_2/D$/1]",
                       "\\begin{solution}\n略。\n\\end{solution}",
                       qid="sec1-q1"))
        problems = _run(tex)
        self.assertIn("dialogue-answer-leak", _codes(problems),
                      "断言句给出答案仍须抓")

    def test_exampledialogue_pure_word_still_checked(self):
        """绑定到具体题目的 exampledialogue 不做纯文字豁免。"""
        tex = _sec(
            _dlg("exampledialogue",
                 ("$\\Psi$", "答案就是分子，记住它。"),
                 qid="sec1-q1"),
            _probchain("\\item \\qtype{分数题} 分数问题。",
                       "\\fillin[分子]{2cm}[分子/分母/约分]",
                       "\\begin{solution}\n略。\n\\end{solution}",
                       qid="sec1-q1"))
        problems = _run(tex)
        self.assertIn("dialogue-answer-leak", _codes(problems),
                      "exampledialogue 的纯文字答案仍全量检查")


# ─────────────────── ④ sectiondialogue 纯文字答案豁免 ───────────────────
# 生产 sec8 原样：\fillin[变号]{2.2cm}[变号/不变/变成两倍]
SEC_PURE_WORDS_FP = _sec(
    _dlg("sectiondialogue",
         ("$\\Psi$", "交换两列以后，行列式的值变不变？"),
         ("$\\gamma$", "不变，我动笔验算过了。")),
    _probchain("\\item \\qtype{性质题} 行列式的性质。",
               "\\fillin[变号]{2.2cm}[变号/不变/变成两倍]"))


class TestTextualAnswerExemptionForSectiondialogue(unittest.TestCase):
    def test_pure_word_answers_exempt(self):
        problems = _run(SEC_PURE_WORDS_FP)
        self.assertNotIn("dialogue-answer-leak", _codes(problems),
                         "纯中文/英文词是学科固有词汇，不是泄露")

    def test_is_textual_answer_classification(self):
        self.assertTrue(cd._is_textual_answer("分子"))
        self.assertTrue(cd._is_textual_answer("不变"))
        self.assertTrue(cd._is_textual_answer("转置定理"))
        self.assertTrue(cd._is_textual_answer("sign"))
        self.assertFalse(cd._is_textual_answer("$D_1/D$"))
        self.assertFalse(cd._is_textual_answer(r"\pi/2"))
        self.assertFalse(cd._is_textual_answer("n-1"))


# ─────────────────────── 悬置词表同源（文档漂移） ───────────────────────
class TestSuspensionWordsAreSingleSourced(unittest.TestCase):
    def test_regex_built_from_tuple(self):
        for w in cd._SUSPENSION_WORDS:
            self.assertTrue(cd._SUSPENSION_INDICATORS.search(w),
                            "词 %s 应被正则命中" % w)

    def test_hint_mentions_every_word(self):
        for w in cd._SUSPENSION_WORDS:
            self.assertIn(w, cd._SUSPENSION_HINT)

    def test_word_count_is_fourteen(self):
        self.assertEqual(len(cd._SUSPENSION_WORDS), 14)

    def test_error_message_uses_hint_not_hardcoded_list(self):
        tex = _sec(
            _dlg("sectiondialogue",
                 ("$\\Psi$", "答案是 3。"),
                 ("$\\gamma$", "对，就是 3。")))
        problems = _run(tex)
        leak = [p for p in problems if "dialogue-suspension-missing" in p]
        self.assertTrue(leak)
        # 完整词表必须出现在错误文案里，作者才看得到真实要求
        self.assertIn(cd._SUSPENSION_HINT, leak[0])

    def test_all_fourteen_words_accepted_in_runtime(self):
        w = cd._SUSPENSION_WORDS
        tex = _sec(
            _dlg("sectiondialogue",
                 ("$\\Psi$", "这道题用 %s 试试？" % w[1]),
                 ("$\\gamma$", "好，我来 %s 一遍，你看行不行？" % w[0])))
        problems = _run(tex)
        self.assertNotIn("dialogue-suspension-missing", _codes(problems))

    def test_premature_resolution_still_caught(self):
        tex = _sec(
            _dlg("sectiondialogue",
                 ("$\\Psi$", "这个形状为何偏偏成立？"),
                 ("$\\gamma$", "对，由此可见就是 3，你记住。")))
        problems = _run(tex)
        self.assertIn("dialogue-premature-resolution", _codes(problems))


# ────────────────────── 健康工程仍须通过（不放松） ──────────────────────
HEALTHY = _sec(
    _dlg("exampledialogue",
         ("$\\Psi$", "交换两列以后，行列式的值难道会变吗？该怎样检验？"),
         ("$\\gamma$", "我动笔算一算，看看为何偏偏是变号？"),
         qid="sec1-q1"),
    _probchain("\\item \\qtype{性质题} 行列式的性质是什么？",
               "\\fillin[变号]{2.2cm}[变号/不变/变成两倍]",
               "\\begin{solution}\n变号。\n\\end{solution}",
               "\\begin{teacherNote}\n观察符号。\n\\end{teacherNote}",
               qid="sec1-q1"))


class TestHealthyProjectStillPasses(unittest.TestCase):
    def test_no_errors(self):
        problems = _run(HEALTHY)
        errs = [p for p in problems if "[ERROR]" in p]
        self.assertEqual(errs, [], "健康工程不得有任何 error")

    def test_conflict_density_still_enforced(self):
        """冲突密度是独立规则，不能被豁免顺带放松。"""
        tex = _sec(_dlg("sectiondialogue",
                        ("$\\Psi$", "好的。"),
                        ("$\\gamma$", "嗯。")))
        problems = _run(tex)
        codes = _codes(problems)
        self.assertIn("dialogue-conflict-density", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
