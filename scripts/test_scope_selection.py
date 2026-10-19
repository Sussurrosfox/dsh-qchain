# -*- coding: utf-8 -*-
"""test_scope_selection.py — workflow/scope_selection.py 纯函数测试（audit §17 任务2）。"""
import os
import sys
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from workflow.scope_selection import (  # noqa: E402
    ScopeSelectionError, normalize_targets, select_scopes,
)

EXPECTED = ["sec0", "sec1", "sec2", "sec3"]


class TNormalize(unittest.TestCase):
    def test_t01_string_csv(self):
        self.assertEqual(normalize_targets("sec1, sec2"), ["sec1", "sec2"])

    def test_t02_list_and_none(self):
        self.assertEqual(normalize_targets(["sec1", "sec1", " sec2 "]), ["sec1", "sec2"])
        self.assertEqual(normalize_targets(None), [])
        self.assertEqual(normalize_targets(""), [])


class TSelect(unittest.TestCase):
    def test_t03_full_returns_all_in_order(self):
        got = select_scopes("full", ["sec3"], ["sec1-q1"], EXPECTED)
        self.assertEqual(got, EXPECTED)

    def test_t04_focused_subset_stable_order(self):
        got = select_scopes("focused", ["sec2", "sec1"], [], EXPECTED)
        self.assertEqual(got, ["sec1", "sec2"])

    def test_t05_focused_qid_derivation(self):
        got = select_scopes("focused", [], ["sec3-q1", "sec1-q2"], EXPECTED)
        self.assertEqual(got, ["sec1", "sec3"])

    def test_t06_focused_union_of_sections_and_qids(self):
        got = select_scopes("focused", ["sec2"], ["sec1-q1"], EXPECTED)
        self.assertEqual(got, ["sec1", "sec2"])

    def test_t07_focused_unknown_target_errors(self):
        with self.assertRaises(ScopeSelectionError) as ctx:
            select_scopes("focused", ["sec9"], [], EXPECTED)
        self.assertEqual(ctx.exception.code, "UNKNOWN_TARGET")

    def test_t08_focused_empty_targets_errors(self):
        with self.assertRaises(ScopeSelectionError) as ctx:
            select_scopes("focused", [], [], EXPECTED)
        self.assertEqual(ctx.exception.code, "EMPTY_TARGETS")

    def test_t09_edit_only_existing(self):
        got = select_scopes("edit", ["sec1", "sec2"], [], EXPECTED,
                            existing_sections=["sec0", "sec1", "sec2"])
        self.assertEqual(got, ["sec1", "sec2"])

    def test_t10_edit_rejects_missing(self):
        with self.assertRaises(ScopeSelectionError) as ctx:
            select_scopes("edit", ["sec3"], [], EXPECTED,
                          existing_sections=["sec0", "sec1"])
        self.assertEqual(ctx.exception.code, "TARGET_NOT_EXISTING")

    def test_t11_edit_unknown_still_unknown(self):
        with self.assertRaises(ScopeSelectionError) as ctx:
            select_scopes("edit", ["secX"], [], EXPECTED,
                          existing_sections=["secX"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_TARGET")

    def test_t12_bad_run_scope(self):
        with self.assertRaises(ScopeSelectionError) as ctx:
            select_scopes("partial", [], [], EXPECTED)
        self.assertEqual(ctx.exception.code, "BAD_RUN_SCOPE")

    def test_t13_full_ignores_targets(self):
        # full 模式忽略 focused/edit 目标，不做存在性校验
        got = select_scopes("full", ["nope"], [], EXPECTED)
        self.assertEqual(got, EXPECTED)

    def test_t14_empty_expected_full_returns_empty(self):
        self.assertEqual(select_scopes("full", [], [], []), [])

    def test_t15_edit_default_existing_is_expected(self):
        got = select_scopes("edit", ["sec2"], [], EXPECTED)
        self.assertEqual(got, ["sec2"])


if __name__ == "__main__":
    unittest.main()
