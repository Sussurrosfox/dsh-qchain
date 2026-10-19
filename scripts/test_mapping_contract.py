"""test_mapping_contract.py — F2 T1 映射契约测试（audit §18.5）。

覆盖：T1 前确定性刷新 source-inventory、manifest 缺失硬 HALT、
source-map.md 结构校验（合法/非法 action/status/格式/缺行/缺失）、
派发请求携带 inventory 与 content_policy 上下文。
"""

import os
import shutil
import sys
import tempfile
import unittest

ENGINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

from workflow import contracts  # noqa: E402
from workflow.nodes import mapping_nodes as mn  # noqa: E402
from scripts import source_inventory as inv  # noqa: E402

SAMPLE_MD = """# 01-8 矩阵函数

## 课时定位

建议 2 × 50 分钟。

## 核心内容

1. $f(A)=Pf(D)P^{-1}$ 与幂级数定义。
2. $M'=AM$ 与谱方法。
"""

VALID_MAP = """# 来源映射

SRC-001 -> sec0 -> EXPLAIN -> EXPLAINED -> 标题改写为学案标题
SRC-003 -> sec0 -> PRESERVE -> PRESERVED -> 课时定位原样保留
SRC-005 -> sec1 -> EXPLAIN -> EXPLAINED -> 核心内容第一条展开讲解
SRC-006 -> sec1 -> PRESERVE -> PRESERVED -> 第二条原样保留
"""

BAD_MAP = """# 来源映射

SRC-001 -> sec0 -> SUMMARIZE -> EXPLAINED -> action 非法
SRC-003 -> sec0 -> PRESERVE -> DONE -> status 非法
SRC-005 -> sec1 缺箭头分隔
这行没有任何 source id 也不算映射行
"""


def _setup(root, source_files=("8.md",), excluded=()):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "8.md"), "w", encoding="utf-8") as f:
        f.write(SAMPLE_MD)
    for rel in excluded:
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write("1. 题目。\n")
    manifest = contracts.build_source_manifest(
        project_root=root, source_files=list(source_files),
        excluded_sources=["%s :: 用户排除" % r for r in excluded])
    self_dir = os.path.join(root, "self")
    os.makedirs(os.path.join(self_dir, ".pd"), exist_ok=True)
    contracts.write_contracts(self_dir, manifest,
                              contracts.build_intent_contract(
                                  user_spec={"content_policy": "faithful"}))
    return self_dir


class T01InventoryEnsure(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_map_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        self.self_dir = _setup(self.root)

    def test_t01_inventory_built_and_persisted(self):
        inventory, err = mn._ensure_source_inventory(self.self_dir, self.root)
        self.assertIsNone(err)
        self.assertIsNotNone(inventory)
        self.assertTrue(os.path.isfile(
            os.path.join(self.self_dir, ".pd", "source-inventory.json")))
        loaded = inv.load_inventory(self.self_dir)
        self.assertEqual(loaded["digest"], inventory["digest"])
        self.assertEqual(loaded["manifest_digest"],
                         inventory["manifest_digest"])

    def test_t02_manifest_missing_is_hard_error(self):
        empty = os.path.join(self.d, "empty")
        os.makedirs(empty)
        inventory, err = mn._ensure_source_inventory(empty, self.root)
        self.assertIsNone(inventory)
        self.assertIn("MANIFEST_MISSING", err)

    def test_t03_tampered_source_detected(self):
        mn._ensure_source_inventory(self.self_dir, self.root)
        with open(os.path.join(self.root, "8.md"), "a", encoding="utf-8") as f:
            f.write("\n篡改。\n")
        inventory, err = mn._ensure_source_inventory(self.self_dir, self.root)
        self.assertIsNone(inventory)
        self.assertIn("SOURCE_DIGEST_MISMATCH", err)


class T02SourceMapValidation(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_map_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)

    def _write_map(self, text):
        with open(os.path.join(self.self_dir, "source-map.md"), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def test_t04_valid_map_passes(self):
        self._write_map(VALID_MAP)
        self.assertEqual(mn._validate_source_map(self.self_dir), [])

    def test_t05_bad_action_and_status_detected(self):
        self._write_map(BAD_MAP)
        errors = mn._validate_source_map(self.self_dir)
        joined = " | ".join(errors)
        self.assertIn("action=SUMMARIZE", joined)
        self.assertIn("status=DONE", joined)
        self.assertTrue(any("映射行格式" in e for e in errors))

    def test_t06_missing_map_detected(self):
        errors = mn._validate_source_map(self.self_dir)
        self.assertTrue(any("缺失" in e for e in errors))

    def test_t07_no_rows_detected(self):
        self._write_map("# 只有说明\n\n本文件不包含任何映射行。\n")
        errors = mn._validate_source_map(self.self_dir)
        self.assertTrue(any("没有任何 SRC-xxx 映射行" in e for e in errors))


class T03RequestNode(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_map_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        self.self_dir = _setup(self.root, excluded=("2023.0.8-2.md",))

    def _state(self, **kw):
        base = {
            "self_dir": self.self_dir,
            "project_root": self.root,
            "run_id": "run-test",
            "history": [],
            "user_spec": {"delivery_profile": "self_study",
                          "lesson_minutes": 90},
        }
        base.update(kw)
        return base

    def test_t08_request_halted_without_manifest(self):
        os.remove(os.path.join(self.self_dir, ".pd", "source-manifest.json"))
        out = mn.node_t1_request_source_audit(self._state())
        self.assertEqual(out["stage"], "HALTED")
        self.assertEqual(out["routing_action"], "HALT")
        self.assertIn("T1 来源清单冻结失败", out["error_message"])

    def test_t09_request_carries_inventory_context(self):
        out = mn.node_t1_request_source_audit(self._state())
        self.assertEqual(out["stage"], "T1_MAPPING_PENDING")
        req = out["pending_dispatch"]
        ctx = req["context"]
        self.assertEqual(ctx["content_policy"], "faithful")
        self.assertEqual(ctx["reorder_allowed"], False)
        units = ctx["source_inventory"]
        self.assertTrue(units)
        self.assertTrue(all("source_id" in u for u in units))
        self.assertTrue(ctx["source_inventory_digest"].startswith("sha256:"))
        self.assertIn("F2 来源忠实契约", req["instruction"])
        self.assertIn("PENDING_APPROVAL", req["instruction"])
        # 派发单落盘
        self.assertTrue(os.path.isfile(os.path.join(
            self.self_dir, ".pd", "agent-requests",
            req["dispatch_id"] + ".json")))
        # 历史记录清单冻结
        self.assertTrue(any("来源清单已冻结" in h
                            for h in out["history"]))

    def test_t10_verify_replans_on_bad_map(self):
        with open(os.path.join(self.self_dir, "source-map.md"), "w",
                  encoding="utf-8") as f:
            f.write(BAD_MAP)
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w",
                  encoding="utf-8") as f:
            f.write("| sec | qid |\n|---|---|\n| sec1 | Q1 |\n")
        out = mn.node_t1_verify_mapping(self._state())
        self.assertEqual(out["stage"], "REPAIR_LOOP")
        self.assertEqual(out["routing_action"], "REPLAN")
        self.assertEqual(out["repair_target"], "source-map.md")

    def test_t11_verify_valid_map_not_replanned_by_contract(self):
        """合法 source-map 不得因 F2 契约被判 REPLAN（预算门禁另论）。"""
        with open(os.path.join(self.self_dir, "source-map.md"), "w",
                  encoding="utf-8") as f:
            f.write(VALID_MAP)
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w",
                  encoding="utf-8") as f:
            f.write("| sec | qid |\n|---|---|\n| sec1 | Q1 |\n")
        out = mn.node_t1_verify_mapping(self._state())
        if out.get("routing_action") == "REPLAN":
            # 允许因预算门禁 REPLAN，但不得是映射契约原因
            self.assertEqual(out.get("repair_target"), "coverage-map.md")
            self.assertIn("预算", out.get("repair_reason", ""))
        else:
            self.assertTrue(out.get("mapping_frozen"))
        self.assertTrue(any("结构校验通过" in h
                            for h in out.get("history", [])))


if __name__ == "__main__":
    unittest.main()
