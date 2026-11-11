#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_section_scan.py — 小节扫描口径与"N7 fail-open"回归

审计发现（N7）：check_semantic / check_scope / check_provenance 各自写了一份
`^\\s*\\\\input\\{(sec[...]+)\\.tex\\}`，只认带扩展名的写法。而 `\\input{sec1}`
是合法 LaTeX（TeX 自动补 .tex），于是一份正常工程扫出 0 个小节，三个门禁
"零受检项 = 全绿"，一个字节都没查。

本测试锁三件事：
1. 口径能认 `\\input{sec1}` / `\\input{sec1.tex}` / `\\include{ sec1 }`；
2. main.tex 扫不出时回落磁盘实存 sec*.tex（宁可多查不可少查）；
3. 两条路都空时**三个门禁各自硬失败**（*_NO_SECTIONS），不再静默通过。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_provenance  # noqa: E402
import check_semantic  # noqa: E402
import section_scan  # noqa: E402
import test_scope  # noqa: E402  （复用其 Phase 0 契约工程夹具）


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TMainScan(unittest.TestCase):

    def test_input_without_extension(self):
        self.assertEqual(section_scan.scan_main_text("\\input{sec1}"), ["sec1"])

    def test_input_with_extension(self):
        self.assertEqual(section_scan.scan_main_text("\\input{sec1.tex}"), ["sec1"])

    def test_include_and_spaces(self):
        text = "\\include{ sec2 }\n  \\input { sec3 }\n"
        self.assertEqual(section_scan.scan_main_text(text), ["sec2", "sec3"])

    def test_ignores_non_section_inputs(self):
        text = "\\input{main-setup.tex}\n\\input{sec1}\n\\input{assets/fig}\n"
        self.assertEqual(section_scan.scan_main_text(text), ["sec1"])

    def test_order_and_dedupe(self):
        text = "\\input{sec2}\n\\input{sec1}\n\\input{sec2.tex}\n"
        self.assertEqual(section_scan.scan_main_text(text), ["sec2", "sec1"])

    def test_fallback_to_files_on_disk(self):
        d = tempfile.mkdtemp(prefix="qct_secscan_")
        self.addCleanup(shutil.rmtree, d, True)
        _write(os.path.join(d, "main.tex"), "\\input{styles}\n")
        _write(os.path.join(d, "sec1.tex"), "x")
        _write(os.path.join(d, "sec2.tex"), "x")
        sections, source = section_scan.resolve_sections(d)
        self.assertEqual(sections, ["sec1", "sec2"])
        self.assertEqual(source, "files")

    def test_empty_when_nothing_at_all(self):
        d = tempfile.mkdtemp(prefix="qct_secscan_")
        self.addCleanup(shutil.rmtree, d, True)
        _write(os.path.join(d, "main.tex"), "\\input{styles}\n")
        sections, source = section_scan.resolve_sections(d)
        self.assertEqual(sections, [])
        self.assertEqual(source, "")
        self.assertIn("GATE_NO_SLICE", section_scan.no_slice_detail(d))


class TNoSectionsFailClose(unittest.TestCase):
    """零命中必须阻断：这是 N7 的核心断言（修复前是静默 pass）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_nosec_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)

    def test_semantic_blocks_when_zero_sections(self):
        _write(os.path.join(self.self_dir, "main.tex"), "\\input{styles}\n")
        res = check_semantic.check_semantic_project(self.self_dir)
        self.assertIn("SEMANTIC_NO_SECTIONS",
                      [e["code"] for e in res["errors"]],
                      res["errors"])

    def test_semantic_detects_extension_less_input(self):
        """核心回归：`\\input{sec1}` 必须被当成"有 1 个小节"，而不是 0 个。"""
        _write(os.path.join(self.self_dir, "main.tex"), "\\input{sec1}\n")
        self.assertEqual(check_semantic.expected_sections(self.self_dir),
                         ["sec1"])
        res = check_semantic.check_semantic_project(self.self_dir)
        codes = [e["code"] for e in res["errors"]]
        # 小节被认出来了（于是要求 sec1.tex 与审读记录存在）——而不是"零小节放行"
        self.assertIn("SEMANTIC_SECTION_MISSING", codes)  # 切片文件缺失 → 阻断
        self.assertNotIn("SEMANTIC_NO_SECTIONS", codes)
        self.assertNotIn("SEMANTIC_MAIN_MISSING", codes)

    def test_semantic_extension_less_input_requires_review_record(self):
        """`\\input{sec1}` + 切片存在 → 缺审读记录时必须 SEMANTIC_MISSING。"""
        _write(os.path.join(self.self_dir, "main.tex"), "\\input{sec1}\n")
        _write(os.path.join(self.self_dir, "sec1.tex"), "\\section{一}\n")
        res = check_semantic.check_semantic_project(self.self_dir)
        self.assertIn("SEMANTIC_MISSING", [e["code"] for e in res["errors"]],
                      res["errors"])

    def test_scope_blocks_when_zero_sections(self):
        self_dir, _contract = test_scope.make_project(self.d)
        _write(os.path.join(self_dir, "main.tex"), "\\input{styles}\n")
        res = test_scope.check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_NO_SECTIONS", test_scope.codes(res))

    def test_scope_warns_and_checks_when_falling_back_to_files(self):
        self_dir, _contract = test_scope.make_project(self.d)
        _write(os.path.join(self_dir, "main.tex"), "\\input{styles}\n")
        _write(os.path.join(self_dir, "sec1.tex"), "\\section{一}\n")
        _write(os.path.join(self_dir, "sec2.tex"), "\\section{二}\n")
        res = test_scope.check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_NO_SECTIONS", test_scope.codes(res))
        self.assertIn("SCOPE_SECTIONS_FROM_FILES",
                      [w["code"] for w in res["warnings"]])

    def test_provenance_blocks_when_zero_sections(self):
        res = check_provenance.check_provenance_project(self.self_dir)
        codes = [e.get("code") for e in res.get("errors", [])]
        self.assertIn("PROVENANCE_NO_SECTIONS", codes, codes)

    def test_provenance_reads_extension_less_input(self):
        _write(os.path.join(self.self_dir, "main.tex"), "\\input{sec1}\n")
        self.assertEqual(check_provenance.expected_sections(self.self_dir),
                         ["sec1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
