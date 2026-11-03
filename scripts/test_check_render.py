#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_check_render.py — check_render v1.1 三项补强回归测试

不依赖 XeLaTeX 与网络，tempfile 建最小 self/ 夹具（HTML 字符串内联），
覆盖本轮事故复盘补强的三条防线：
  G1  H2 扩充：HTML 残留 \begin{tikzcd} 等 TikZ 源码 → FAIL；
  G2  P1 增强：log 含 3 行 Missing character（2 个同字）→ FAIL 且报告
      含按字符聚合的统计（必×2, 然×1）；
  G3  H7 新增：cards.json 槽位 < n_fillin → FAIL（RENDER_SLOT_LOST）；
      选项精确不守恒 → FAIL（RENDER_OPTION_LOST）；cards.json 缺失 → FAIL；
  G4  守恒基线：cards.json 守恒 + log 干净 + HTML 干净 → PASS（不误报）。

运行: python scripts/test_check_render.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_render  # noqa: E402

KATEX_SRC = os.path.join(HERE, "..", "templates", "html", "katex")

SEC1 = r"""\section*{I 第一节}
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
"""

# 最小合法 HTML：1 个 qc-slot span + 4 个 qc-option 按钮（与 SEC1 守恒）
HTML_TMPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{v}</title></head>
<body>
{extra}
<span class="qc-slot" data-slot="sec1-01-s1"><span class="qc-blank"></span>
<span class="qc-options" role="group">
<button type="button" class="qc-option" data-correct="false">1</button>
<button type="button" class="qc-option" data-correct="true">2</button>
<button type="button" class="qc-option" data-correct="false">3</button>
<button type="button" class="qc-option" data-correct="false">4</button>
</span></span>
</body></html>
"""

CARDS_OK = {"schema": "qchain-html-deck/1", "title": "t",
            "cards": [{"id": "sec1-01", "kind": "question",
                       "slots": [{"id": "sec1-01-s1", "answer": "$2$",
                                  "width": "15mm",
                                  "options": ["1", "2", "3", "4"]}]}]}


def make_self(d, extra="", cards=CARDS_OK, with_pdf_log=None):
    """建最小 self/ 夹具；with_pdf_log=(log_text) 时补 student.pdf/.log。"""
    with open(os.path.join(d, "sec1.tex"), "w", encoding="utf-8") as f:
        f.write(SEC1)
    for v in ("student", "teacher"):
        with open(os.path.join(d, v + ".html"), "w", encoding="utf-8") as f:
            f.write(HTML_TMPL.format(v=v, extra=extra if v == "student"
                                     else ""))
    kdir = os.path.join(d, "katex")
    os.makedirs(kdir, exist_ok=True)
    # 任务卡02：不再放空 JS 占位——复制随包真实 KaTeX 资源（严格数学校验用
    # 的是随包引擎；夹具里的 katex/ 只代表交付目录资源齐备）。
    for need in ("katex.min.js", "auto-render.min.js", "katex.min.css"):
        shutil.copy(os.path.join(KATEX_SRC, need), os.path.join(kdir, need))
    if cards is not None:
        with open(os.path.join(d, "cards.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cards, f, ensure_ascii=False)
    if with_pdf_log is not None:
        open(os.path.join(d, "student.pdf"), "wb").close()
        with open(os.path.join(d, "student.log"), "w",
                  encoding="utf-8") as f:
            f.write(with_pdf_log)
    return d


def run_gate(d):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check_render.main([d])
    return rc, buf.getvalue()


class CheckRenderGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-check-render-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- G1：H2 扩充，TikZ 泄漏即 FAIL ----
    def test_g1_tikzcd_leak_fails(self):
        make_self(self.tmp, extra="\\begin{tikzcd} A \\arrow[r] & B "
                                  "\\end{tikzcd}")
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("tikzcd", out)

    def test_g1_usetikzlibrary_leak_fails(self):
        make_self(self.tmp, extra="\\usetikzlibrary{cd}")
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("usetikzlibrary", out)

    # ---- G2：P1 增强，缺字形按字符聚合 ----
    def test_g2_missing_char_aggregation(self):
        log = ("Missing character: There is no 必 (U+5FC5) in font cmr10.\n"
               "Missing character: There is no 必 (U+5FC5) in font cmr10.\n"
               "Missing character: There is no 然 (U+7136) in font cmr10.\n")
        make_self(self.tmp, with_pdf_log=log)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("Missing character", out)
        self.assertIn("必×2", out)
        self.assertIn("然×1", out)

    def test_g2_unrecognized_char_counted(self):
        log = "Missing character\n"  # 无字符明细的行
        make_self(self.tmp, with_pdf_log=log)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("（未识别）×1", out)

    # ---- G3：H7 cards.json 槽位/选项守恒 ----
    def test_g3_slot_lost_fails(self):
        cards = {"schema": "qchain-html-deck/1",
                 "cards": [{"id": "sec1-01", "kind": "question",
                            "slots": []}]}  # 槽位被 tex2cards 丢失
        make_self(self.tmp, cards=cards)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("RENDER_SLOT_LOST", out)
        self.assertIn("0", out)  # 两侧计数入消息

    def test_g3_option_lost_fails(self):
        cards = {"schema": "qchain-html-deck/1",
                 "cards": [{"id": "sec1-01", "kind": "question",
                            "slots": [{"id": "s1", "answer": "$2$",
                                       "options": ["1", "2"]}]}]}
        make_self(self.tmp, cards=cards)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("RENDER_OPTION_LOST", out)

    def test_g3_cards_json_missing_fails(self):
        make_self(self.tmp, cards=None)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("cards.json", out)

    def test_g3_cards_json_bad_schema_fails(self):
        make_self(self.tmp, cards={"schema": "qchain-html-deck/1"})  # 无 cards
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("schema", out)

    # ---- G4：守恒基线 PASS（不误报） ----
    def test_g4_clean_pass(self):
        make_self(self.tmp)
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
