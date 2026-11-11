#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_novelty_normalization.py — coverage-map「new_objects」空值归一（N9）

审计发现（N9）：原实现用穷举元组判空，coverage-planner 每换一种"无"的写法，
一行本来无事的覆盖记录就被 FIDELITY_NOVELTY_UNAPPROVED 判成"声明了新对象却没
登记扩展"，返修子 Agent 只能去改一个本来就没问题的格子。

两条断言方向相反、必须同时成立：
* 空话的各种变体（"无新增。"/"无（仅沿用原文）"/"None"/"—"/"∅"…）判空；
* 真声明一个字都不能吞（"正弦定理"/"无新增对象：正弦定理"/"无（新增 Newton 迭代）"）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_fidelity  # noqa: E402


EMPTY_VARIANTS = [
    "", " ", "\t\n", "无", "无。", "无新增", "无新增。", "无新增对象", "无新对象",
    "无新增内容", "无任何新增", "未新增", "未新增任何对象", "暂无", "没有",
    "无（仅沿用原文）", "（无）", "— 无 —", "（沿用原文）", "保持原文，无新增",
    "-", "--", "—", "–", "／", "无 /", "[]", "()", "{}",
    "none", "None", "NONE", "null", "nil", "n/a", "N/A", "na", "nothing",
    "empty", "blank", "∅",
]

REAL_DECLARATIONS = [
    "正弦定理", "新增引理：单调收敛", "无新增对象：正弦定理",
    "无（新增 Newton 迭代）", "反例（狄利克雷函数）", "Taylor 展开",
    "0 个?",  # 带问号的待确认写法，仍应阻断而不是吞掉
]


class TEmptyVariants(unittest.TestCase):

    def test_empty_variants_normalize_to_blank(self):
        for raw in EMPTY_VARIANTS:
            with self.subTest(raw=raw):
                self.assertEqual(check_fidelity.normalize_new_objects(raw), "")


class TRealDeclarations(unittest.TestCase):

    def test_real_declarations_survive(self):
        for raw in REAL_DECLARATIONS:
            with self.subTest(raw=raw):
                self.assertEqual(check_fidelity.normalize_new_objects(raw), raw)


class TNoRegressionOnCanonicalForm(unittest.TestCase):
    """原穷举表里的每一项都必须仍然判空（不许修好变体、弄坏旧例）。"""

    def test_legacy_tuple_still_blank(self):
        for raw in ("无", "无新增", "无。", "-", "—", "--", "none", "NONE",
                    "N/A", "n/a", "无新增对象", "∅"):
            with self.subTest(raw=raw):
                self.assertEqual(check_fidelity.normalize_new_objects(raw), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
