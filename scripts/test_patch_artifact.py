#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_patch_artifact.py — RC-2：确定性修补器的不变量。

PATCH-01  replace_literal 正常替换 + 落盘自检（sha256 变化）
PATCH-02  CAS：expect_before_sha256 与现态不符 → 拒绝，产物一个字节不动
PATCH-03  命中数强制：expect_count 与实测不符（含 0 命中）→ 拒绝，不落盘
PATCH-04  事务性：一个 plan 里多个 op，后者失败 → 前者也不落地
PATCH-05  append_section 幂等（同名标题已存在则原样跳过）
PATCH-06  ensure_table_row 按首列键去重插入，重复键不重复插
PATCH-07  copy_asset 复制并核对 sha256；源不存在 → 拒绝
PATCH-08  path 越界（../、绝对路径）→ plan 非法（退出码 2）
PATCH-09  --dry-run 只报告不落盘；退出码语义 0/1/2

全部用临时目录 + 直接调 main()，不启动流水线、不派 Agent。
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import patch_artifact as pa  # noqa: E402


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class PatchArtifactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pa-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.self_dir = os.path.join(self.tmp, "self")
        os.makedirs(self.self_dir, exist_ok=True)
        self.cov = os.path.join(self.self_dir, "coverage-map.md")
        with open(self.cov, "w", encoding="utf-8") as f:
            f.write("# cov\n\n| a | b |\n|---|---|\n| SRC-129 | |N|≤n+1 与类大小>n+1 矛盾 |\n")

    # -- helpers ---------------------------------------------------------
    def _plan(self, ops, **extra):
        plan = {"schema_version": 1, "reason": "test", "ops": ops}
        plan.update(extra)
        p = os.path.join(self.tmp, "plan.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
        return p

    def _run(self, plan_path, *extra):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = pa.main([self.self_dir, plan_path] + list(extra))
        return code, buf.getvalue() + err.getvalue()

    # -- PATCH-01 --------------------------------------------------------
    def test_replace_literal_applies_and_reports_sha(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "|N|≤n+1", "new": "N 的元素数不超过 n+1", "expect_count": 1}])
        code, out = self._run(plan)
        self.assertEqual(code, 0, out)
        with open(self.cov, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("N 的元素数不超过 n+1", text)
        self.assertNotIn("|N|≤n+1", text)
        self.assertNotEqual(sha(self.cov), before)

    # -- PATCH-02 --------------------------------------------------------
    def test_cas_mismatch_refuses_and_leaves_file_untouched(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": "0" * 64,
            "old": "|N|≤n+1", "new": "X", "expect_count": 1}])
        code, out = self._run(plan)
        self.assertEqual(code, 1, out)
        self.assertEqual(sha(self.cov), before)
        self.assertIn("sha256", out)

    # -- PATCH-03 --------------------------------------------------------
    def test_expect_count_mismatch_refuses(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "SRC-129", "new": "SRC-999", "expect_count": 2}])
        code, out = self._run(plan)
        self.assertEqual(code, 1, out)
        self.assertEqual(sha(self.cov), before)
        self.assertIn("命中数不符", out)

    def test_zero_hits_is_not_success(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "根本不存在的文本", "new": "X", "expect_count": 1}])
        code, _ = self._run(plan)
        self.assertEqual(code, 1)
        self.assertEqual(sha(self.cov), before)

    # -- PATCH-04 --------------------------------------------------------
    def test_transactional_all_or_nothing(self):
        before = sha(self.cov)
        plan = self._plan([
            {"op": "replace_literal", "path": "coverage-map.md",
             "expect_before_sha256": before,
             "old": "SRC-129", "new": "SRC-130", "expect_count": 1},
            {"op": "replace_literal", "path": "coverage-map.md",
             "expect_before_sha256": before,
             "old": "不存在", "new": "Y", "expect_count": 1},
        ])
        code, _ = self._run(plan)
        self.assertEqual(code, 1)
        # 前一个 op 合法，但整批放弃 → 文件仍是原样
        self.assertEqual(sha(self.cov), before)

    # -- PATCH-05 --------------------------------------------------------
    def test_append_section_is_idempotent(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "append_section", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "heading": "## 对话承接记录（dialogue-hook）",
            "body": "| sec1 | α | 结构相同 |\n"}])
        code, _ = self._run(plan)
        self.assertEqual(code, 0)
        first = sha(self.cov)
        # 第二次：CAS 用新摘要，标题已存在 → 幂等跳过，字节不变
        plan2 = self._plan([{
            "op": "append_section", "path": "coverage-map.md",
            "expect_before_sha256": first,
            "heading": "## 对话承接记录（dialogue-hook）",
            "body": "| sec1 | α | 结构相同 |\n"}])
        code, _ = self._run(plan2)
        self.assertEqual(code, 0)
        self.assertEqual(sha(self.cov), first)

    # -- PATCH-06 --------------------------------------------------------
    def test_ensure_table_row_dedupes_by_key(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "ensure_table_row", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "table_header_prefix": "| a |",
            "row": "| SRC-200 | new |", "key_column": 0}])
        code, out = self._run(plan)
        self.assertEqual(code, 0, out)
        with open(self.cov, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("SRC-200"), 1)
        # 同键再插 → 不重复
        plan2 = self._plan([{
            "op": "ensure_table_row", "path": "coverage-map.md",
            "expect_before_sha256": sha(self.cov),
            "table_header_prefix": "| a |",
            "row": "| SRC-200 | other |", "key_column": 0}])
        code, _ = self._run(plan2)
        self.assertEqual(code, 0)
        with open(self.cov, encoding="utf-8") as f:
            self.assertEqual(f.read().count("SRC-200"), 1)

    # -- PATCH-07 --------------------------------------------------------
    def test_copy_asset_copies_and_verifies(self):
        src = os.path.join(self.self_dir, "fig_1.jpg")
        with open(src, "wb") as f:
            f.write(b"\xff\xd8fakejpeg")
        want = hashlib.sha256(b"\xff\xd8fakejpeg").hexdigest()
        plan = self._plan([{
            "op": "copy_asset", "src": "fig_1.jpg", "dst": "assets/fig_1.jpg"}])
        code, out = self._run(plan)
        self.assertEqual(code, 0, out)
        dst = os.path.join(self.self_dir, "assets", "fig_1.jpg")
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(sha(dst), want)

    def test_copy_asset_missing_source_refuses(self):
        plan = self._plan([{
            "op": "copy_asset", "src": "nope.png", "dst": "assets/nope.png"}])
        code, out = self._run(plan)
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.exists(os.path.join(self.self_dir, "assets")))

    # -- PATCH-08 --------------------------------------------------------
    def test_path_escape_is_plan_error(self):
        plan = self._plan([{
            "op": "replace_literal", "path": "../outside.md",
            "expect_before_sha256": "0" * 64,
            "old": "a", "new": "b", "expect_count": 1}])
        code, out = self._run(plan)
        self.assertEqual(code, 2, out)

    def test_unknown_op_is_plan_error(self):
        plan = self._plan([{"op": "rewrite_everything", "path": "coverage-map.md"}])
        code, _ = self._run(plan)
        self.assertEqual(code, 2)

    # -- PATCH-09 --------------------------------------------------------
    def test_dry_run_reports_without_writing(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "SRC-129", "new": "SRC-130", "expect_count": 1}])
        code, out = self._run(plan, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(sha(self.cov), before)

    def test_json_ledger_written(self):
        before = sha(self.cov)
        plan = self._plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "SRC-129", "new": "SRC-130", "expect_count": 1}])
        ledger = os.path.join(self.tmp, "ledger.json")
        code, _ = self._run(plan, "--json", ledger)
        self.assertEqual(code, 0)
        with open(ledger, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["ops"], 1)
        self.assertTrue(rec["written"][0]["changed"])
        self.assertEqual(rec["written"][0]["after_sha256"], sha(self.cov))


if __name__ == "__main__":
    unittest.main(verbosity=2)
