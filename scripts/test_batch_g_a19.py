r"""批次 G / A19：check_pedagogy「预解包办」检测的零填空误判。

bug.md A19：`check_pedagogy.py:389` 的"零填空"判定只认 `\fillin`，而
`\ansspace`（双 s，header.tex 定义）与 `\ansace`（单 s 别名）同样是学生
作答空白——全仓其余门禁（check_dialogue.py:83、check_numbering.py:404、
tex2cards.py:565-568）早已把三者同等视为答案区。sec6 第 4 小问
"……给出任意两列相等时 $\delta(A)=0$。\ansace{1.0cm}" 因此被判
"未设置任何填空"并连续返修（生产实证：agent-requests/
dsp-repair-sec6-dca372f057.json）。

本测试钉住两件事：
  * 带 \ansspace / \ansace 的小问不再误报（假阳性消除）；
  * 真正"零空白 + 直接宣布中间结果"的小问仍然拦截（不放松门禁）。

用法：python -B -m pytest scripts/test_batch_g_a19.py -q
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(SCRIPTS), "templates")
PY = sys.executable


def run(script, *argv):
    return subprocess.run([PY, os.path.join(SCRIPTS, script)] + list(argv),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace",
                          env=dict(os.environ, PYTHONUTF8="1"))


def make_project(root, secs):
    """复制模板 + 写 main.tex + gen_config，返回项目目录。"""
    for f in ("header.tex", "footer.tex", "config.yaml",
              "student.tex", "teacher.tex"):
        shutil.copy(os.path.join(TEMPLATES, f), os.path.join(root, f))
    lines = ["\\input{header.tex}", "", "\\begin{document}",
             "\\begin{center}", "  {\\Huge \\textbf{\\pdMainTitle}}",
             "\\end{center}", "\\raggedcolumns", "\\pdBeginColumns", ""]
    lines += ["\\input{%s}" % n for n in secs]
    lines += ["", "\\input{footer.tex}"]
    with open(os.path.join(root, "main.tex"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for name, body in secs.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(body)
    r = run("gen_config.py", "--project", root)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def codes(text):
    out = []
    for line in text.splitlines():
        if "预解包办" in line:
            out.append("pre-solved")
        elif "题干存在包办推导" in line:
            out.append("stem-overloaded")
    return out


def sec(sub_body):
    r"""包一层最小可用节：前置知识卡 + 题链 + 节末结论框。"""
    return (
        "\\section*{第一节}\n"
        r"\begin{microknowledge}[核心定义与记号]记 $\delta$ 为交错多重线性函数。\end{microknowledge}" "\n"
        "\\begin{probchain}\n"
        "% qid: sec1-q1\n"
        "% source: SRC-001\n"
        + sub_body +
        "\\begin{solution}略。\\end{solution}\n"
        "\\begin{teacherNote}略。\\end{teacherNote}\n"
        "\\end{probchain}\n"
        "\\begin{knowledgebox}[本节结论]交错性。\\end{knowledgebox}\n"
    )


# 生产中真实触发 A19 误判的 sec6 第 4 小问（对换分解推出 δ(A)=0）
SEC6_FP = (
    "\\item \\qtype{综合题}\\practicemode{guided}\n"
    "\\textbf{行列式唯一性定理}：三条公理。\n"
    "\\begin{enumerate}\n"
    "\\item 条件 (i) 的依据已由哪项结果给出？\\fillin[$\\det(I_n)=1$]{3.0cm}"
    "[$\\det(I_n)=0$/$\\det(I_n)=n$/$\\det(I_n)=1$]\\anspace{0.5cm}\n"
    "\\item 条件 (ii) 对应哪条命题？\\fillin[$n$ 阶线性性]{3.0cm}"
    "[$n$ 阶线性性/乘法性质/$n$ 阶交错性]\\anspace{0.5cm}\n"
    "\\item 条件 (iii) 可由哪条命题直接推出？\\fillin[交错性]{2.4cm}"
    "[线性性/交错性/乘法性质]\\anspace{0.5cm}\n"
    "\\item 条件 (iii) 只要求“相邻”两列相等。结合本题之前对一般对换的分解，"
    "说明为何由此可得任意两列相等时 $\\delta(A)=0$。\\ansace{1.0cm}\n"
    "\\end{enumerate}\n"
)


def _check(sec_body):
    d = tempfile.mkdtemp()
    try:
        make_project(d, {"sec1.tex": sec(sec_body)})
        r = run("check_pedagogy.py", d)
        return r.stdout + r.stderr, r.returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)


class TestA19PresolvedPackaging(unittest.TestCase):

    def test_single_s_ansace_is_a_student_blank(self):
        """\ansace{1.0cm}（单 s 别名）是学生落笔处，不判预解包办。"""
        out, rc = _check(SEC6_FP)
        self.assertNotIn("预解包办", out, out)

    def test_double_s_ansspace_is_a_student_blank(self):
        """\ansspace（双 s 定义）同样豁免。"""
        body = SEC6_FP.replace("\\ansace{1.0cm}", "\\ansspace{1.0cm}")
        out, rc = _check(body)
        self.assertNotIn("预解包办", out, out)

    def test_fillin_still_exempt(self):
        """\fillin 的原有豁免不变（回归保护）。"""
        body = SEC6_FP.replace(
            "$\\delta(A)=0$。\\ansace{1.0cm}",
            "$\\delta(A)=$\\fillin[0]{0.8cm}[-\\delta(A)/1/0]")
        out, rc = _check(body)
        self.assertNotIn("预解包办", out, out)

    def test_no_blank_at_all_is_still_caught(self):
        """零空白 + 直接宣布中间结果 → 仍然拦截（门禁未被放松）。"""
        body = SEC6_FP.replace("。\\ansace{1.0cm}", "。")
        out, rc = _check(body)
        self.assertIn("预解包办", out, out)
        self.assertEqual(rc, 1)

    def test_blank_only_elsewhere_in_question_does_not_shield(self):
        """空白若不在该小问内（属于别的小问），不构成豁免。"""
        body = (
            "\\item \\qtype{综合题}\n"
            "\\begin{enumerate}\n"
            "\\item 已知联立求解叉乘，求得第一分量为 $u_1=2$。\n"
            "\\end{enumerate}\n"
            "\\anspace{1.0cm}\n"
        )
        out, rc = _check(body)
        self.assertIn("预解包办", out, out)

    def test_student_blank_regex_classification(self):
        """RE_STUDENT_BLANK 对三种空白宏都命中，对普通宏不误命中。"""
        sys.path.insert(0, SCRIPTS)
        import check_pedagogy as cp  # noqa: E402
        self.assertTrue(cp.RE_STUDENT_BLANK.search("\\fillin[a]{1cm}[a/b/c]"))
        self.assertTrue(cp.RE_STUDENT_BLANK.search("\\ansspace{1.0cm}"))
        self.assertTrue(cp.RE_STUDENT_BLANK.search("\\ansace{1.0cm}"))
        # \fillinx 之类前缀不算（\b 保证）
        self.assertFalse(cp.RE_STUDENT_BLANK.search("\\fillinx{1cm}"))
        self.assertFalse(cp.RE_STUDENT_BLANK.search("\\ansspacex"))
        self.assertFalse(cp.RE_STUDENT_BLANK.search("\\begin{enumerate}"))
        self.assertFalse(cp.RE_STUDENT_BLANK.search("$\\delta(A)=0$"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
