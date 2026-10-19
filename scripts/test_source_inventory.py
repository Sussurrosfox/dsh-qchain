"""test_source_inventory.py — F1 source-inventory 确定性测试（audit §18.4）。

覆盖：单元切分确定性、source_id 全局顺序、preserve/excluded 处置、
digest 对账（篡改拒绝）、清单过期判定、落盘回读、CLI --check、kind 分类。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ENGINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

from workflow import contracts  # noqa: E402
from scripts import source_inventory as inv  # noqa: E402


SAMPLE_MD = """# 01-8 矩阵函数

## 课时定位

建议 2 × 50 分钟。

## 核心内容

1. $f(A)=Pf(D)P^{-1}$ 与幂级数定义。
2. $M'=AM$ 与谱方法。

## 定义

**定义**：矩阵函数通过幂级数定义。
"""

SAMPLE_PROBLEM = """# 真题

1. 求矩阵指数。
"""


def _make_self(root, manifest):
    self_dir = os.path.join(root, "self")
    os.makedirs(os.path.join(self_dir, ".pd"), exist_ok=True)
    mp, _ = contracts.write_contracts(self_dir, manifest,
                                      contracts.build_intent_contract(
                                          user_spec={}))
    return self_dir, mp


class T01UnitSplit(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_inv_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        os.makedirs(self.root)
        self.src = os.path.join(self.root, "8.md")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(SAMPLE_MD)

    def test_t01_deterministic_split(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        inv1 = inv.build_inventory(manifest, self.root)
        inv2 = inv.build_inventory(manifest, self.root)
        self.assertEqual(inv1["digest"], inv2["digest"])
        # H1 标题 + H2 课时定位 + 定位段 + H2 核心内容 + 两个列表项
        # + H2 定义 + 定义段 = 8 个单元
        self.assertEqual(inv1["unit_count"], 8)
        orders = [u["source_order"] for u in inv1["units"]]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual([u["source_id"] for u in inv1["units"]],
                         ["SRC-%03d" % i for i in range(1, 9)])
        self.assertTrue(all(u["disposition"] == "preserve"
                            for u in inv1["units"]))
        self.assertTrue(all(u["source_file"] == "8.md" for u in inv1["units"]))
        self.assertTrue(all(u["text_digest"].startswith("sha256:")
                            for u in inv1["units"]))

    def test_t02_kind_classification(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        inv1 = inv.build_inventory(manifest, self.root)
        by_loc = {u["locator"]: u["kind"] for u in inv1["units"]}
        kinds = {u["kind"] for u in inv1["units"]}
        self.assertIn("goal", kinds)        # 课时定位
        self.assertIn("definition", kinds)  # 定义段
        self.assertTrue(all(u["kind"] in inv.KINDS for u in inv1["units"]))

    def test_t03_excluded_source_registered(self):
        prob = os.path.join(self.root, "2023.0.8-2.md")
        with open(prob, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PROBLEM)
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"],
            excluded_sources=["2023.0.8-2.md :: 用户设计排除"])
        inv1 = inv.build_inventory(manifest, self.root)
        exc = [u for u in inv1["units"]
               if u["source_file"] == "2023.0.8-2.md"]
        self.assertTrue(exc)
        self.assertTrue(all(u["disposition"] == "excluded" for u in exc))
        # 排除单元仍占 source_order（保证全局顺序稳定可查）
        orders = [u["source_order"] for u in inv1["units"]]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(set(orders)), len(orders))

    def test_t04_digest_mismatch_rejected(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        with open(self.src, "a", encoding="utf-8") as f:
            f.write("\n篡改。\n")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.build_inventory(manifest, self.root)
        self.assertEqual(ctx.exception.code, "SOURCE_DIGEST_MISMATCH")

    def test_t05_missing_file_rejected(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        os.remove(self.src)
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.build_inventory(manifest, self.root)
        self.assertEqual(ctx.exception.code, "SOURCE_MISSING")


class T02Persistence(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_inv_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "8.md"), "w", encoding="utf-8") as f:
            f.write(SAMPLE_MD)

    def test_t06_write_load_and_stale(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        self_dir, _ = _make_self(self.root, manifest)
        inventory = inv.build_inventory(manifest, self.root)
        out = inv.write_inventory(self_dir, inventory)
        self.assertTrue(os.path.isfile(out))
        self.assertFalse(inv.inventory_is_stale(self_dir, manifest))
        loaded = inv.load_inventory(self_dir)
        self.assertEqual(loaded["digest"], inventory["digest"])
        # manifest 摘要变化 → 过期
        with open(os.path.join(self.root, "8.md"), "w", encoding="utf-8") as f:
            f.write(SAMPLE_MD + "\n改动。\n")
        m2 = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        self.assertTrue(inv.inventory_is_stale(self_dir, m2))

    def test_t07_cli_check(self):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        self_dir, _ = _make_self(self.root, manifest)
        script = os.path.join(ENGINE_ROOT, "scripts", "source_inventory.py")
        env = dict(os.environ, PYTHONPATH=ENGINE_ROOT)
        # 先缺失 → FAIL
        r = subprocess.run([sys.executable, script, "--self-dir", self_dir,
                            "--check"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 1)
        # 生成
        r = subprocess.run([sys.executable, script, "--self-dir", self_dir],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("8 单元", r.stdout)
        # 校验 → PASS
        r = subprocess.run([sys.executable, script, "--self-dir", self_dir,
                            "--check"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self_dir, ".pd", "source-inventory.json"),
                  "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["manifest_digest"],
                             manifest["digest"])

    def test_t08_manifest_missing(self):
        with self.assertRaises(inv.InventoryError) as ctx:
            inv._load_manifest(os.path.join(self.d, "nowhere", "m.json"))
        self.assertEqual(ctx.exception.code, "MANIFEST_MISSING")


if __name__ == "__main__":
    unittest.main()
