#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tikz_rasterize.py — tikz_rasterize.py 单元测试（unittest + tempfile）。

不依赖 xelatex 的用例在任何机器上必须全绿；编译类用例由
``@unittest.skipUnless(shutil.which("xelatex") and shutil.which("pdftoppm"), ...)``
守卫，本机有工具链时真实端到端跑通。

覆盖：提取逻辑 / standalone 装配（tikz-cd 模板）/ 源导言区宏自动提取 /
rewrite_source 纯函数 / --list 与 --rewrite CLI 契约 / 编译 e2e
（tikzcd 与 tikzpicture 各一，另含自动导言区与 --rewrite 端到端）。

运行：``python scripts\\test_tikz_rasterize.py``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tikz_rasterize as tr  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tikz_rasterize.py")

# 混合环境测试文档：真实 tikzcd / tikzpicture / 注释行内的假环境
MIXED_DOC = """\\documentclass{article}
% 注释里的假环境 \\begin{tikzcd} A \\end{tikzcd} 不应被提取
\\begin{document}
前文
\\begin{tikzcd}
  A \\arrow[r] \\& B
\\end{tikzcd}
中间文字
\\begin{tikzpicture}[scale=0.5]
  \\draw (0,0) -- (1,1); % 块内注释须逐字保留
\\end{tikzpicture}
% \\begin{tikzpicture}
%   整块被注释的假环境
% \\end{tikzpicture}
后文
\\end{document}
"""

MINIMAL_TIKZCD = """\\documentclass{article}
\\begin{document}
\\begin{tikzcd}
  A \\arrow[r, "f"] & B
\\end{tikzcd}
\\end{document}
"""

MINIMAL_TIKZPICTURE = """\\documentclass{article}
\\begin{document}
\\begin{tikzpicture}[scale=0.6]
  \\draw[thick] (0,0) -- (1,1) -- (2,0) -- cycle;
  \\fill (1,0.5) circle (1pt);
\\end{tikzpicture}
\\end{document}
"""

# 源导言区含自定义宏与 tikz 库声明的文档：定义在 \begin{document} 之前，体内定义不提取
PREAMBLE_DOC = """\\documentclass{article}
% 注释行：\\newcommand{\\fake}{x} 不应被提取
\\usepackage{amsmath}
\\usetikzlibrary{cd,calc}
\\newcommand{\\quotgrp}[2]{#1/#2}
\\providecommand{\\dual}{^\\vee}
\\DeclareMathOperator{\\im}{im}
\\newenvironment{myenv}{\\begin{center}}{\\end{center}}
\\def\\shortcut#1{[#1]}
\\newcommand{\\quotgrp}[2]{#1/#2}
\\begin{document}
\\newcommand{\\late}{x}
\\renewcommand{\\dual}{^\\star}
正文
\\end{document}
"""

# tikzcd 使用 calc 库 to path 语法（同 GR_4 蛇形引理图）：仅当自动提取源导言区
# \\usetikzlibrary{cd,calc} 后才能编译
CALC_LIB_DOC = """\\documentclass{article}
\\usetikzlibrary{cd,calc}
\\begin{document}
\\begin{tikzcd}
  A \\arrow[r] & B \\\\
  C \\arrow[r, red, to path={
    let \\p1 = (\\tikztostart.east), \\p2 = (\\tikztotarget.west) in (\\p1) -- (\\p2)
  }] & D
\\end{tikzcd}
\\end{document}
"""

# 含自定义宏 \mymark 的 tikzcd：仅当自动提取源导言区宏注入后才能编译
AUTO_PREAMBLE_DOC = """\\documentclass{article}
\\newcommand{\\mymark}[1]{\\mathbf{#1}}
\\begin{document}
\\begin{tikzcd}
  \\mymark{A} \\arrow[r] & \\mymark{B}
\\end{tikzcd}
\\end{document}
"""

# rewrite 测试文档：3 个块（tikzcd / tikzpicture / tikzcd），块外有中文与首尾结构
REWRITE_DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "前文 A\n"
    "\\begin{tikzcd}\n"
    "A \\arrow[r] & B\n"
    "\\end{tikzcd}\n"
    "中间文字\n"
    "\\begin{tikzpicture}\n"
    "\\draw (0,0) -- (1,1);\n"
    "\\end{tikzpicture}\n"
    "\\begin{tikzcd}\n"
    "C \\arrow[r] & D\n"
    "\\end{tikzcd}\n"
    "后文 Z\n"
    "\\end{document}\n"
)


class TestExtraction(unittest.TestCase):
    """提取逻辑：混合环境、注释假环境、行号正确性、逐字保留。"""

    def test_mixed_envs_and_comment_fake(self):
        blocks = tr.find_blocks(MIXED_DOC)
        self.assertEqual(len(blocks), 2)
        self.assertEqual([b.env for b in blocks], ["tikzcd", "tikzpicture"])
        self.assertEqual([b.index for b in blocks], [1, 2])

    def test_line_numbers(self):
        blocks = tr.find_blocks(MIXED_DOC)
        cd, pic = blocks
        self.assertEqual((cd.start_line, cd.end_line), (5, 7))
        self.assertEqual((pic.start_line, pic.end_line), (9, 11))

    def test_source_verbatim_with_inner_comment(self):
        blocks = tr.find_blocks(MIXED_DOC)
        pic = blocks[1]
        self.assertIn("% 块内注释须逐字保留", pic.source)
        self.assertTrue(pic.source.startswith("\\begin{tikzpicture}"))
        self.assertTrue(pic.source.rstrip().endswith("\\end{tikzpicture}"))
        self.assertEqual(pic.chars, len(pic.source))

    def test_env_precision_and_options(self):
        doc = "\\begin{tikzcd}[column sep=large]\nX\n\\end{tikzcd}\n"
        blocks = tr.find_blocks(doc)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].env, "tikzcd")
        self.assertIn("[column sep=large]", blocks[0].source)

    def test_adjacent_blocks_not_merged(self):
        doc = (
            "\\begin{tikzcd}\nA\n\\end{tikzcd}\n"
            "\\begin{tikzpicture}\nB\n\\end{tikzpicture}\n"
            "\\begin{tikzcd}\nC\n\\end{tikzcd}\n"
        )
        blocks = tr.find_blocks(doc)
        self.assertEqual([b.env for b in blocks], ["tikzcd", "tikzpicture", "tikzcd"])
        self.assertIn("A", blocks[0].source)
        self.assertNotIn("B", blocks[0].source)

    def test_escaped_percent_not_comment(self):
        doc = "文本 \\% 转义百分号\n\\begin{tikzcd}\nA\n\\end{tikzcd}\n"
        blocks = tr.find_blocks(doc)
        self.assertEqual(len(blocks), 1)

    def test_no_blocks(self):
        self.assertEqual(tr.find_blocks("hello \\% \\begin{tikzcd} 仍在注释后\n"), [])


class TestStandaloneAssembly(unittest.TestCase):
    def test_template_contains_tikz_cd_and_preamble(self):
        doc = tr.build_standalone("BODY_HERE", ["\\providecommand{\\quotgrp}[2]{#1/#2}"])
        # 任务 1：显式 tikz-cd 包（定义 tikzcd 环境并自带 tikz），border=2pt 保持
        self.assertIn("\\usepackage{tikz-cd}", doc)
        self.assertNotIn("\\usetikzlibrary{cd}", doc)
        self.assertIn("\\documentclass[tikz,border=2pt]{standalone}", doc)
        self.assertIn("border=2pt", doc)
        # ctex / 数学宏包与注入点保留
        self.assertIn("\\usepackage{ctex}", doc)
        self.assertIn("\\usepackage{amsmath,amssymb,mathtools,bm}", doc)
        self.assertIn("BODY_HERE", doc)
        self.assertIn("\\providecommand{\\quotgrp}[2]{#1/#2}", doc)
        self.assertNotIn("@PREAMBLE@", doc)
        self.assertNotIn("@BODY@", doc)

    def test_template_without_preamble_lines(self):
        doc = tr.build_standalone("BODY_HERE")
        self.assertIn("\\usepackage{tikz-cd}", doc)
        self.assertIn("BODY_HERE", doc)
        self.assertNotIn("@PREAMBLE@", doc)


class TestPreambleMacroExtraction(unittest.TestCase):
    """extract_preamble_macros：顺序/去重/仅导言区/注释跳过/\\def 匹配（纯函数）。"""

    def test_order_and_dedupe(self):
        macros = tr.extract_preamble_macros(PREAMBLE_DOC)
        self.assertEqual(
            macros,
            [
                "\\usetikzlibrary{cd,calc}",
                "\\newcommand{\\quotgrp}[2]{#1/#2}",
                "\\providecommand{\\dual}{^\\vee}",
                "\\DeclareMathOperator{\\im}{im}",
                "\\newenvironment{myenv}{\\begin{center}}{\\end{center}}",
                "\\def\\shortcut#1{[#1]}",
            ],
        )

    def test_usetikzlibrary_extracted_and_deduped(self):
        doc = (
            "\\usetikzlibrary{cd,calc}\n"
            "\\usetikzlibrary{cd,calc}\n"
            "\\usetikzlibrary{positioning}\n"
        )
        self.assertEqual(
            tr.extract_preamble_macros(doc),
            ["\\usetikzlibrary{cd,calc}", "\\usetikzlibrary{positioning}"],
        )

    def test_only_preamble_region(self):
        macros = tr.extract_preamble_macros(PREAMBLE_DOC)
        joined = "\n".join(macros)
        # \\begin{document} 之后的定义（含 \\renewcommand）不提取
        self.assertNotIn("late", joined)
        self.assertNotIn("\\star", joined)

    def test_comment_lines_skipped(self):
        macros = tr.extract_preamble_macros(PREAMBLE_DOC)
        joined = "\n".join(macros)
        self.assertNotIn("fake", joined)
        self.assertFalse(any(m.lstrip().startswith("%") for m in macros))

    def test_renewcommand_and_renewenvironment(self):
        doc = "\\renewcommand{\\a}{b}\n\\renewenvironment{e}{x}{y}\n"
        self.assertEqual(
            tr.extract_preamble_macros(doc),
            ["\\renewcommand{\\a}{b}", "\\renewenvironment{e}{x}{y}"],
        )

    def test_leading_whitespace_and_verbatim(self):
        doc = "  \\def\\x{y}\n"
        self.assertEqual(tr.extract_preamble_macros(doc), ["  \\def\\x{y}"])

    def test_no_begin_document_treats_all_as_preamble(self):
        doc = "\\newcommand{\\a}{b}\n\\begin{tikzcd}A\\end{tikzcd}\n"
        self.assertEqual(tr.extract_preamble_macros(doc), ["\\newcommand{\\a}{b}"])

    def test_non_macro_lines_ignored(self):
        doc = (
            "\\documentclass{article}\n"
            "\\usepackage{amsmath}\n"
            "\\definitions{x}\n"
            "普通文本行\n"
        )
        self.assertEqual(tr.extract_preamble_macros(doc), [])

    def test_empty_preamble(self):
        self.assertEqual(
            tr.extract_preamble_macros("\\documentclass{article}\n\\begin{document}\n"), []
        )

    def test_crlf_source_lines_stripped(self):
        # CRLF 源文件（本工具按字节读入）提取结果应与 LF 版完全一致，且无行尾 \r
        crlf = PREAMBLE_DOC.replace("\n", "\r\n")
        macros = tr.extract_preamble_macros(crlf)
        self.assertEqual(macros, tr.extract_preamble_macros(PREAMBLE_DOC))
        self.assertFalse(any(m.endswith("\r") for m in macros))


class TestRewriteSource(unittest.TestCase):
    """rewrite_source：原位替换 / 相邻块 / 逐字节保持 / 相对路径 / --only 场景。"""

    def _pngs(self, td, names):
        return {i + 1: os.path.join(td, "assets", n) for i, n in enumerate(names)}

    def test_rewrite_all_blocks_exact_output(self):
        with tempfile.TemporaryDirectory() as td:
            blocks = tr.find_blocks(REWRITE_DOC)
            self.assertEqual([b.env for b in blocks], ["tikzcd", "tikzpicture", "tikzcd"])
            png_map = self._pngs(td, ["demo-01.png", "demo-02.png", "demo-03.png"])
            out = tr.rewrite_source(
                REWRITE_DOC, blocks, png_map, os.path.join(td, "out")
            )
            self.assertEqual(
                out,
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "前文 A\n"
                "% [tikz-rasterize] 原 L4-6 tikzcd → demo-01.png\n"
                "\\includegraphics{../assets/demo-01.png}\n"
                "中间文字\n"
                "% [tikz-rasterize] 原 L8-10 tikzpicture → demo-02.png\n"
                "\\includegraphics{../assets/demo-02.png}\n"
                "% [tikz-rasterize] 原 L11-13 tikzcd → demo-03.png\n"
                "\\includegraphics{../assets/demo-03.png}\n"
                "后文 Z\n"
                "\\end{document}\n",
            )

    def test_adjacent_blocks_not_swallowed(self):
        doc = "\\begin{tikzcd}A\\end{tikzcd}\\begin{tikzcd}B\\end{tikzcd}\n"
        blocks = tr.find_blocks(doc)
        self.assertEqual(len(blocks), 2)
        with tempfile.TemporaryDirectory() as td:
            png_map = self._pngs(td, ["a-01.png", "a-02.png"])
            out = tr.rewrite_source(doc, blocks, png_map, td)
            self.assertEqual(
                out,
                "% [tikz-rasterize] 原 L1-1 tikzcd → a-01.png\n"
                "\\includegraphics{assets/a-01.png}\n"
                "% [tikz-rasterize] 原 L1-1 tikzcd → a-02.png\n"
                "\\includegraphics{assets/a-02.png}\n",
            )
            self.assertNotIn("\\begin{tikzcd}", out)

    def test_no_blocks_returns_text_verbatim(self):
        text = "\\documentclass{article}\n\\begin{document}\n无图正文\n\\end{document}\n"
        self.assertEqual(tr.rewrite_source(text, [], {}, "somedir"), text)
        self.assertEqual(
            tr.rewrite_source(text, tr.find_blocks(text), {1: "x.png"}, "d"), text
        )

    def test_only_scenario_preserves_unprocessed_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            blocks = tr.find_blocks(REWRITE_DOC)
            assets = os.path.join(td, "assets")
            # 只处理块 1 与块 3，缺块 2（tikzpicture）→ 原样保留
            png_map = {
                1: os.path.join(assets, "demo-01.png"),
                3: os.path.join(assets, "demo-03.png"),
            }
            out = tr.rewrite_source(
                REWRITE_DOC, blocks, png_map, os.path.join(td, "out")
            )
            self.assertEqual(out.count("\\includegraphics"), 2)
            self.assertNotIn("\\begin{tikzcd}", out)
            # 未处理的 tikzpicture 块原样保留
            self.assertIn(
                "\\begin{tikzpicture}\n\\draw (0,0) -- (1,1);\n\\end{tikzpicture}\n", out
            )
            self.assertEqual(
                out,
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "前文 A\n"
                "% [tikz-rasterize] 原 L4-6 tikzcd → demo-01.png\n"
                "\\includegraphics{../assets/demo-01.png}\n"
                "中间文字\n"
                "\\begin{tikzpicture}\n"
                "\\draw (0,0) -- (1,1);\n"
                "\\end{tikzpicture}\n"
                "% [tikz-rasterize] 原 L11-13 tikzcd → demo-03.png\n"
                "\\includegraphics{../assets/demo-03.png}\n"
                "后文 Z\n"
                "\\end{document}\n",
            )

    def test_outside_blocks_byte_identical(self):
        # 无尾随换行 + 转义百分号 + 行内块（块前非行首）
        doc = "前置中文 \\% 转义\n\\begin{tikzcd}\nX\n\\end{tikzcd}尾随文字"
        blocks = tr.find_blocks(doc)
        with tempfile.TemporaryDirectory() as td:
            out = tr.rewrite_source(doc, blocks, {1: os.path.join(td, "a.png")}, td)
        self.assertTrue(out.startswith("前置中文 \\% 转义\n"))
        self.assertTrue(out.endswith("尾随文字"))
        self.assertIn(
            "% [tikz-rasterize] 原 L2-4 tikzcd → a.png\n\\includegraphics{a.png}", out
        )

    def test_inline_block_gets_leading_newline(self):
        doc = "前置 \\begin{tikzcd}A\\end{tikzcd} 后继"
        blocks = tr.find_blocks(doc)
        with tempfile.TemporaryDirectory() as td:
            out = tr.rewrite_source(doc, blocks, {1: os.path.join(td, "a.png")}, td)
        self.assertEqual(
            out,
            "前置 \n"
            "% [tikz-rasterize] 原 L1-1 tikzcd → a.png\n"
            "\\includegraphics{a.png} 后继",
        )

    def test_indented_block_keeps_indent_on_marker(self):
        # 块在行首（仅前有空白缩进）：不插换行，缩进原样留在标记注释前
        doc = "\\begin{center}\n    \\begin{tikzcd}A\\end{tikzcd}\n\\end{center}\n"
        blocks = tr.find_blocks(doc)
        with tempfile.TemporaryDirectory() as td:
            out = tr.rewrite_source(doc, blocks, {1: os.path.join(td, "a.png")}, td)
        self.assertEqual(
            out,
            "\\begin{center}\n"
            "    % [tikz-rasterize] 原 L2-2 tikzcd → a.png\n"
            "\\includegraphics{a.png}\n"
            "\\end{center}\n",
        )


class TestListModeCLI(unittest.TestCase):
    """--list 输出（JSON：env/行号/字符数），不编译、不需要外部工具。"""

    def test_list_json(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(MIXED_DOC)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--list", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["mode"], "list")
            self.assertEqual(report["found"], 2)
            self.assertEqual(report["by_env"], {"tikzcd": 1, "tikzpicture": 1})
            b0 = report["blocks"][0]
            self.assertEqual(
                set(b0), {"index", "env", "start_line", "end_line", "chars"}
            )
            self.assertEqual((b0["start_line"], b0["end_line"]), (5, 7))

    def test_list_human_readable(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(MIXED_DOC)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("tikzcd", proc.stdout)
            self.assertIn("tikzpicture", proc.stdout)

    def test_missing_source_exit_1(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--source", "nonexistent.tex", "--list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("不存在", proc.stderr)

    def test_help_lists_new_flags(self):
        # --rewrite 帮助文本含 % 字符，须 %% 转义，否则 argparse 格式化即崩
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--rewrite", "--no-auto-preamble", "--only", "--preamble"):
            self.assertIn(flag, proc.stdout)
        self.assertIn("% [tikz-rasterize]", proc.stdout)

    def test_list_and_rewrite_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(MIXED_DOC)
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "--source",
                    src,
                    "--list",
                    "--rewrite",
                    os.path.join(td, "out.tex"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("互斥", proc.stderr)
            self.assertFalse(os.path.exists(os.path.join(td, "out.tex")))


@unittest.skipUnless(
    shutil.which("xelatex") and shutil.which("pdftoppm"),
    "需要 xelatex 与 pdftoppm（MiKTeX/TeX Live）",
)
class TestCompileEndToEnd(unittest.TestCase):
    """编译类用例：真实跑 xelatex + pdftoppm。"""

    def test_minimal_tikzcd_produces_png(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            out = os.path.join(td, "assets")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(MINIMAL_TIKZCD)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--out-dir", out, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(report["failed"], 0)
            png = report["results"][0]["png"]
            self.assertTrue(png.endswith("demo-01.png"))
            self.assertTrue(os.path.isfile(png))
            self.assertGreater(os.path.getsize(png), 0)
            # 确认是合法 PNG
            with open(png, "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_broken_block_exit_2_with_log_tail(self):
        broken = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{tikzcd}\nA \\arrow[r, \"\\undefinedmacroxyz{\"] & B\n\\end{tikzcd}\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "bad.tex")
            out = os.path.join(td, "assets")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(broken)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--out-dir", out, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["failed"], 1)
            self.assertTrue(report["results"][0]["log_tail"])

    def test_minimal_tikzpicture_produces_png(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            out = os.path.join(td, "assets")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(MINIMAL_TIKZPICTURE)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--out-dir", out, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["found"], 1)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(report["results"][0]["env"], "tikzpicture")
            png = report["results"][0]["png"]
            self.assertTrue(png.endswith("demo-01.png"))
            self.assertTrue(os.path.isfile(png))
            self.assertGreater(os.path.getsize(png), 0)
            with open(png, "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_auto_preamble_macro_enables_compile(self):
        # \mymark 仅定义于源导言区：自动提取注入后才可能编译成功
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            out = os.path.join(td, "assets")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(AUTO_PREAMBLE_DOC)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--out-dir", out, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(
                report["auto_preamble_lines"],
                ["\\newcommand{\\mymark}[1]{\\mathbf{#1}}"],
            )
            png = report["results"][0]["png"]
            self.assertTrue(os.path.isfile(png))

    def test_auto_preamble_tikzlibrary_enables_compile(self):
        # calc 库坐标语法：仅当自动提取源导言区 \usetikzlibrary{cd,calc} 后才能编译
        # （复现 GR_4 蛇形引理图缺 calc 库的真实失败）
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            out = os.path.join(td, "assets")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(CALC_LIB_DOC)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--source", src, "--out-dir", out, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(
                report["auto_preamble_lines"], ["\\usetikzlibrary{cd,calc}"]
            )
            png = report["results"][0]["png"]
            self.assertTrue(os.path.isfile(png))

    def test_rewrite_mode_writes_out_tex(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            assets = os.path.join(td, "assets")
            out_tex = os.path.join(td, "out", "demo.raster.tex")
            with open(src, "w", encoding="utf-8", newline="") as fh:
                fh.write(REWRITE_DOC)
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "--source",
                    src,
                    "--out-dir",
                    assets,
                    "--rewrite",
                    out_tex,
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(
                (report["found"], report["succeeded"], report["failed"]), (3, 3, 0)
            )
            rewrite = report["rewrite"]
            self.assertEqual(rewrite["out"], os.path.abspath(out_tex))
            self.assertEqual(rewrite["assets_dir"], os.path.abspath(assets))
            self.assertEqual(len(rewrite["mapping"]), 3)
            for entry, env, (s, e) in zip(
                rewrite["mapping"],
                ["tikzcd", "tikzpicture", "tikzcd"],
                [(4, 6), (8, 10), (11, 13)],
            ):
                self.assertEqual(
                    set(entry),
                    {"index", "env", "png", "start_line", "end_line"},
                )
                self.assertEqual(entry["env"], env)
                self.assertEqual((entry["start_line"], entry["end_line"]), (s, e))
                self.assertTrue(os.path.isfile(entry["png"]))
            with open(out_tex, encoding="utf-8", newline="") as fh:
                content = fh.read()
            self.assertEqual(content.count("\\includegraphics"), 3)
            self.assertEqual(content.count("% [tikz-rasterize]"), 3)
            self.assertNotIn("\\begin{tikzcd}", content)
            self.assertNotIn("\\begin{tikzpicture}", content)
            self.assertIn("\\includegraphics{../assets/demo-01.png}", content)
            self.assertIn("\\includegraphics{../assets/demo-02.png}", content)
            self.assertIn("\\includegraphics{../assets/demo-03.png}", content)
            # 块外内容逐字节不变：与纯函数 oracle 用同一输入的结果完全一致
            png_map = {r["index"]: r["png"] for r in report["results"]}
            expected = tr.rewrite_source(
                REWRITE_DOC,
                tr.find_blocks(REWRITE_DOC),
                png_map,
                os.path.dirname(os.path.abspath(out_tex)),
            )
            self.assertEqual(content, expected)
            self.assertTrue(content.startswith("\\documentclass{article}\n"))
            self.assertTrue(content.endswith("后文 Z\n\\end{document}\n"))

    def test_rewrite_failure_no_write(self):
        broken = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{tikzcd}\nA \\arrow[r, \"\\undefinedmacroxyz{\"] & B\n\\end{tikzcd}\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "bad.tex")
            out_tex = os.path.join(td, "bad.raster.tex")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(broken)
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "--source",
                    src,
                    "--out-dir",
                    os.path.join(td, "assets"),
                    "--rewrite",
                    out_tex,
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["failed"], 1)
            self.assertNotIn("rewrite", report)
            self.assertFalse(os.path.exists(out_tex))

    def test_rewrite_human_readable_line(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "demo.tex")
            out_tex = os.path.join(td, "demo.raster.tex")
            with open(src, "w", encoding="utf-8", newline="") as fh:
                fh.write(MINIMAL_TIKZCD)
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "--source",
                    src,
                    "--out-dir",
                    os.path.join(td, "assets"),
                    "--rewrite",
                    out_tex,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("重写文件：", proc.stdout)
            self.assertIn("\\includegraphics", proc.stdout)
            self.assertIn("源导言区宏自动提取：0 行", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
