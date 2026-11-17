#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_repair_deterministic.py — RC-2：确定性修复节点（引擎执行，不派 Agent）。

DET-01  有合法 plan → 调 patch_artifact 落盘成功 → stage=T3_CHECKS_PENDING/
        PROCEED，engine_repair.worker=engine（审计承认的第二类执行者）
DET-02  plan 缺失 → 写出空模板并 HALT，error_message 指名模板路径
DET-03  plan 非法/CAS 不符 → HALT，且产物一个字节不动（绝不半改）
DET-04  路由：decide_route 对 deterministic issue 返回 REPAIR_DETERMINISTIC，
        edges.route_after_checks 把它导向 repair_deterministic 节点
DET-05  编译图里确实存在 repair_deterministic 节点与回边（legacy + fast）

用 fake ScriptRunner 与临时目录；不启动真实流水线、不派 Agent。
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

from workflow import edges as wf_edges  # noqa: E402
from workflow import issues as iss  # noqa: E402
from workflow.nodes import repair_nodes as rn  # noqa: E402

FIXTURE = "| SRC-129 | |N|≤n+1 与类大小>n+1 矛盾 |\n"


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


class FakeScriptRunner:
    """替身：把 patch_artifact.py 的调用转成真实子进程以外的最小实现。"""

    calls = []
    exit_code = 0
    output = "[patch_artifact] 应用 1 个操作"
    fail_error = ""

    def __init__(self, self_dir):
        self.self_dir = self_dir

    def run_script(self, script_name, args):
        FakeScriptRunner.calls.append((script_name, list(args)))
        return (FakeScriptRunner.exit_code, FakeScriptRunner.output,
                FakeScriptRunner.fail_error)


class RepairDeterministicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="det-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.self_dir = os.path.join(self.tmp, "self")
        os.makedirs(self.self_dir, exist_ok=True)
        self.cov = os.path.join(self.self_dir, "coverage-map.md")
        with open(self.cov, "w", encoding="utf-8") as f:
            f.write(FIXTURE)
        FakeScriptRunner.calls = []
        FakeScriptRunner.exit_code = 0
        FakeScriptRunner.fail_error = ""
        self._orig = rn.ScriptRunner
        rn.ScriptRunner = FakeScriptRunner

    def tearDown(self):
        rn.ScriptRunner = self._orig

    def _state(self, **extra):
        st = {"self_dir": self.self_dir, "history": [],
              "repair_target": "coverage-map.md"}
        st.update(extra)
        return st

    def _write_plan(self, ops):
        rel = rn._plan_rel("coverage-map.md")
        abs_path = os.path.join(self.self_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "reason": "test", "ops": ops}, f,
                      ensure_ascii=False)
        return rel

    # -- DET-01 ----------------------------------------------------------
    def test_plan_present_is_executed_by_engine(self):
        with open(self.cov, "rb") as f:
            before = sha_bytes(f.read())
        rel = self._write_plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": before,
            "old": "|N|≤n+1", "new": "N 的元素数不超过 n+1", "expect_count": 1}])
        patch = rn.node_repair_deterministic(self._state())
        self.assertEqual(patch["stage"], "T3_CHECKS_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["engine_repair"]["worker"], "engine")
        self.assertEqual(patch["engine_repair"]["status"], "applied")
        # 实际调了 patch_artifact.py，且带 --json 台账
        self.assertEqual(len(FakeScriptRunner.calls), 1)
        script, args = FakeScriptRunner.calls[0]
        self.assertEqual(script, "patch_artifact.py")
        self.assertIn("--json", args)
        self.assertEqual(args[0], self.self_dir)
        # 计划文件仍在（不经手删）
        self.assertTrue(os.path.isfile(
            os.path.join(self.self_dir, *rel.split("/"))))

    # -- DET-02 ----------------------------------------------------------
    def test_missing_plan_writes_template_and_halts(self):
        patch = rn.node_repair_deterministic(self._state())
        self.assertEqual((patch["stage"], patch["routing_action"]),
                         ("HALTED", "HALT"))
        self.assertEqual(patch["engine_repair"]["status"], "plan_missing")
        rel = rn._plan_rel("coverage-map.md")
        self.assertIn(rel, patch["error_message"])
        abs_path = os.path.join(self.self_dir, *rel.split("/"))
        self.assertTrue(os.path.isfile(abs_path))
        with open(abs_path, encoding="utf-8") as f:
            tpl = json.load(f)
        self.assertEqual(tpl["schema_version"], 1)
        self.assertIn("ops", tpl)
        # 缺计划时不调执行器、不派 Agent
        self.assertEqual(FakeScriptRunner.calls, [])

    # -- DET-03 ----------------------------------------------------------
    def test_failed_plan_halts_and_leaves_artifacts_untouched(self):
        with open(self.cov, "rb") as f:
            before = sha_bytes(f.read())
        self._write_plan([{
            "op": "replace_literal", "path": "coverage-map.md",
            "expect_before_sha256": "0" * 64,
            "old": "|N|≤n+1", "new": "X", "expect_count": 1}])
        FakeScriptRunner.exit_code = 1
        FakeScriptRunner.fail_error = "[patch_artifact] [放弃] coverage-map.md: 摘要与 expect_before_sha256 不符"
        patch = rn.node_repair_deterministic(self._state())
        self.assertEqual((patch["stage"], patch["routing_action"]),
                         ("HALTED", "HALT"))
        self.assertEqual(patch["engine_repair"]["status"], "failed")
        self.assertIn("未被应用", patch["error_message"])
        with open(self.cov, "rb") as f:
            self.assertEqual(sha_bytes(f.read()), before)

    # -- DET-04 ----------------------------------------------------------
    def test_route_and_edge_send_deterministic_to_engine_node(self):
        # 真实确定性码：课堂计划缺失 → 确定性生成（不是派作者改文本）
        issue = iss.normalize_issue(
            {"severity": "error", "message": "classroom-plan.md 缺失",
             "category": "artifact"},
            gate="check_budget")
        self.assertEqual(issue["repair_kind"], "deterministic")
        d = iss.decide_route([issue])
        self.assertEqual(d["action"], "REPAIR_DETERMINISTIC")
        self.assertFalse(d["dispatch_agent"])
        state = {"routing_action": "REPAIR_DETERMINISTIC"}
        self.assertEqual(wf_edges.route_after_checks(state),
                         "repair_deterministic")
        # HALTED 收口到 END
        self.assertEqual(wf_edges.route_after_deterministic_repair(
            {"stage": "HALTED", "routing_action": "HALT"}), END_SENTINEL)

    # -- DET-05 ----------------------------------------------------------
    def test_graph_registers_node_in_both_versions(self):
        from workflow import graph as g
        for factory in (g.create_legacy_graph, g.create_fast_graph):
            compiled = factory()
            nodes = set(compiled.get_graph().nodes)
            self.assertIn("repair_deterministic", nodes, factory.__name__)


from langgraph.graph import END as END_SENTINEL  # noqa: E402


if __name__ == "__main__":
    unittest.main(verbosity=2)
