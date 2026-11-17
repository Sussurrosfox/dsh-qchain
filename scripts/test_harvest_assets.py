#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_harvest_assets.py — N1：图片资产收割（缺图不是内容缺陷）。

HARVEST-01  \includegraphics 引用在基准目录 → 复制进 self/，台账记 copied
HARVEST-02  重复运行幂等：同内容不重复复制（skipped identical），字节不变
HARVEST-03  目标已存在且内容不同 → conflict，不覆盖（除非 --force）
HARVEST-04  引用找不到 → missing，不误报成功（ok=False）
HARVEST-05  graphicspath 生效：引用裸文件名也能在 graphicspath 目录下命中
HARVEST-06  台账落 .pd/reports/assets-harvest.json

全部用临时目录；不起流水线、不派 Agent。
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

import harvest_assets as ha  # noqa: E402


class HarvestAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ha-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.base = os.path.join(self.tmp, "proj")
        self.self_dir = os.path.join(self.base, "self")
        os.makedirs(self.self_dir, exist_ok=True)
        self._make(self.base, "fig_1.jpg", b"\xff\xd8realjpeg")
        self._make(self.base, "fig-3.png", b"\x89PNGchunk")

    def _make(self, d, name, data):
        p = os.path.join(d, name)
        os.makedirs(d, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _tex(self, body, name="GR.tex"):
        return self._make(self.base, name, body.encode("utf-8"))

    # -- HARVEST-01 ------------------------------------------------------
    def test_copies_referenced_asset(self):
        self._tex(r"文本 \includegraphics[width=6cm]{fig_1.jpg} 结束")
        ledger = ha.harvest(self.self_dir, quiet=True)
        copied = [c["path"] for c in ledger["copied"]]
        self.assertIn("fig_1.jpg", copied)
        self.assertTrue(os.path.isfile(os.path.join(self.self_dir, "fig_1.jpg")))

    def test_only_referenced_assets_are_copied(self):
        self._tex(r"\includegraphics{fig-3.png}")
        ledger = ha.harvest(self.self_dir, quiet=True)
        copied = [c["path"] for c in ledger["copied"]]
        self.assertIn("fig-3.png", copied)
        self.assertNotIn("fig_1.jpg", copied)

    # -- HARVEST-02 ------------------------------------------------------
    def test_second_run_is_idempotent(self):
        self._tex(r"\includegraphics{fig_1.jpg}")
        ha.harvest(self.self_dir, quiet=True)
        with open(os.path.join(self.self_dir, "fig_1.jpg"), "rb") as f:
            first = hashlib.sha256(f.read()).hexdigest()
        ledger = ha.harvest(self.self_dir, quiet=True)
        self.assertEqual(ledger["copied"], [])
        self.assertEqual(len(ledger["skipped"]), 1)
        self.assertEqual(ledger["skipped"][0]["reason"], "identical")
        with open(os.path.join(self.self_dir, "fig_1.jpg"), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), first)

    # -- HARVEST-03 ------------------------------------------------------
    def test_conflicting_target_is_not_overwritten(self):
        self._tex(r"\includegraphics{fig_1.jpg}")
        self._make(self.self_dir, "fig_1.jpg", b"\xff\xd8DIFFERENT")
        ledger = ha.harvest(self.self_dir, quiet=True)
        self.assertEqual(len(ledger["conflict"]), 1)
        self.assertFalse(ledger["ok"])
        with open(os.path.join(self.self_dir, "fig_1.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8DIFFERENT")
        # --force 才覆盖
        ledger = ha.harvest(self.self_dir, force=True, quiet=True)
        self.assertEqual(len(ledger["copied"]), 1)
        with open(os.path.join(self.self_dir, "fig_1.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8realjpeg")

    # -- HARVEST-04 ------------------------------------------------------
    def test_missing_asset_is_reported(self):
        self._tex(r"\includegraphics{不存在的图.png}")
        ledger = ha.harvest(self.self_dir, quiet=True)
        self.assertEqual(ledger["missing"], ["不存在的图.png"])
        self.assertFalse(ledger["ok"])
        self.assertEqual(ledger["copied"], [])

    # -- HARVEST-05 ------------------------------------------------------
    def test_graphicspath_is_honored(self):
        self._make(os.path.join(self.base, "img"), "pic.png", b"\x89PNGgp")
        self._tex("\\graphicspath{{img/}}\n\\includegraphics{pic.png}")
        ledger = ha.harvest(self.self_dir, quiet=True)
        self.assertIn("pic.png", [c["path"] for c in ledger["copied"]])

    # -- HARVEST-06 ------------------------------------------------------
    def test_ledger_is_written(self):
        self._tex(r"\includegraphics{fig_1.jpg}")
        ha.harvest(self.self_dir, quiet=True)
        path = os.path.join(self.self_dir, ".pd", "reports",
                            "assets-harvest.json")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["schema_version"], 1)
        self.assertTrue(rec["self_dir"].endswith("self"))

    def test_no_references_is_clean_noop(self):
        self._tex("完全没有图片的文本")
        ledger = ha.harvest(self.self_dir, quiet=True)
        self.assertEqual((ledger["references"], ledger["copied"]), (0, []))
        self.assertTrue(ledger["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
