#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_semantic_nodes.py — audit §6 语义审读轮（Writer → Checker → Gate）回归

覆盖：
    T01  审读结果求值纯函数（evaluate_checker_result / semantic_gate_verdict /
         needs_check：ok / revise / needs_evidence / missing / invalid / stale）
    T02  规划节点（零派发放行 / 规划批次 / repair_target 强制入批 /
         轮次熔断 / 门禁失败 HALT / 计划文件独立于写作轮）
    T03  派发节点（stale/空单 HALT；提交不 join——任务4；parallel/manual）
    T04  汇总节点（并行全过放行；REVISE→重派作者带父子链；
         缺结果/过期→semantic_repair_target；并发度不足→HALT；
         manual 只 interrupt 一次；恢复磁盘重推导；协议伪造 HALT）
    T05  打乱血缘豁免（shuffle-map.json 台账：确定性选项重排不判 stale）

运行: python -m unittest discover scripts -p "test_semantic_nodes.py" -v
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

from workflow.nodes import semantic_nodes as sn  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes.semantic_nodes import (  # noqa: E402
    CHECK_PLAN_REL,
    node_t2_dispatch_check,
    node_t2_join_check,
    node_t2_plan_check,
)
from workflow.adapters.worker_pool import DispatchResult  # noqa: E402

import test_section_batch as tsb  # noqa: E402  (复用治具)


# ---------------------------------------------------------------------------
# 治具
# ---------------------------------------------------------------------------

def write_sec(d, sec, text="\\section{sec}\n候选内容。\n"):
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sec}.tex")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def sec_digest(d, sec):
    with open(os.path.join(d, f"{sec}.tex"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_check_result(d, sec, conclusion="PASS", issues=None, digest=None,
                       scope=None, schema_version=1):
    """写 .pd/semantic/<sec>.json（digest 缺省=当前切片摘要）。"""
    directory = os.path.join(d, sn.CHECK_DIR_REL)
    os.makedirs(directory, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "scope": scope if scope is not None else sec,
        "section_digest": digest if digest is not None else sec_digest(d, sec),
        "conclusion": conclusion,
        "issues": issues if issues is not None else [],
        "input_digest": "",
        "checked_at": "2024-01-01T00:00:00+00:00",
    }
    path = os.path.join(directory, f"{sec}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def check_plan_file(d):
    return os.path.join(d, CHECK_PLAN_REL)


def load_check_plan(d):
    with open(check_plan_file(d), encoding="utf-8") as f:
        return json.load(f)


def make_check_state(d, sections=("sec1", "sec2"), statuses=None, **extra):
    state = tsb.make_state(d, sections=sections, statuses=statuses, **extra)
    return state


def run_plan_check(state, gate=None):
    """用 FakeRunner 跑一次 plan_check 节点；返回 (patch, gate 调用记录)。"""
    tsb.FakeRunner.gate_result = gate if gate is not None else tsb.gate_report("PASS")
    created = []

    class RecordingRunner(tsb.FakeRunner):
        def __init__(self, project_root, scripts_dir=None):
            super().__init__(project_root, scripts_dir)
            created.append(self)

    original = sn.ScriptRunner
    sn.ScriptRunner = RecordingRunner
    try:
        patch = node_t2_plan_check(state)
    finally:
        sn.ScriptRunner = original
    return patch, (created[0].calls if created else [])


def author_results(requests):
    """模拟写作轮结果（join 节点用它解析 parent_dispatch_id 父子链）。"""
    out = {}
    for req in requests:
        did = str(req.get("dispatch_id") or "")
        out[did] = {"dispatch_id": did, "batch_id": req.get("batch_id"),
                    "scope": req.get("scope"), "status": "ok", "attempts": 1}
    return out


def blocking_issue(code="OVERSTRONG_CLAIM", line=3, claim="充分条件写成 iff"):
    return {"code": code, "line": line, "claim": claim,
            "evidence": "原文：x>0 当且仅当", "severity": "blocking"}


# ---------------------------------------------------------------------------
# T01 求值纯函数
# ---------------------------------------------------------------------------

class T01_EvaluateCheckerResult(unittest.TestCase):
    """T01: 审读结果 schema/结论/摘要绑定求值。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_ok_when_pass_and_digest_matches(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", conclusion="PASS")
        status, detail = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "ok")
        self.assertEqual(detail, "")

    def test_advisory_issue_still_ok(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", issues=[
            {"code": "TEMPLATE_CONTAMINATION", "claim": "重复词", "severity": "advisory"}])
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "ok")

    def test_blocking_issue_is_revise(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", conclusion="PASS",
                           issues=[blocking_issue()])
        status, detail = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "revise")
        self.assertIn("OVERSTRONG_CLAIM", detail)

    def test_revise_conclusion(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", conclusion="REVISE",
                           issues=[blocking_issue("MISSING_CONDITION")])
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "revise")

    def test_needs_evidence_blocks(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", conclusion="NEEDS_EVIDENCE")
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "needs_evidence")

    def test_missing_result(self):
        write_sec(self.d, "sec1")
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "missing_result")

    def test_invalid_schema_version(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", schema_version=2)
        status, detail = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "invalid_result")
        self.assertIn("schema_version", detail)

    def test_invalid_scope_mismatch(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", scope="sec2")
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "invalid_result")

    def test_invalid_conclusion_and_issues_type(self):
        write_sec(self.d, "sec1")
        write_check_result(self.d, "sec1", conclusion="MAYBE")
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "invalid_result")
        write_check_result(self.d, "sec1", conclusion="PASS", issues="not-a-list")
        status, _ = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "invalid_result")

    def test_stale_digest_without_lineage(self):
        write_sec(self.d, "sec1", "旧候选")
        write_check_result(self.d, "sec1")           # 绑定旧摘要
        write_sec(self.d, "sec1", "新候选（内容已变更）")
        status, detail = sn.evaluate_checker_result(
            self.d, "sec1", sec_digest(self.d, "sec1"))
        self.assertEqual(status, "stale_digest")
        self.assertIn("不得复用", detail)

    def test_gate_verdicts(self):
        write_sec(self.d, "sec1")
        # 缺结果 → BLOCK（补派审读，不是放行）
        verdict, reason = sn.semantic_gate_verdict(self.d, "sec1")
        self.assertEqual(verdict, "BLOCK")
        self.assertIn("missing_result", reason)
        # 过期 → BLOCK
        write_check_result(self.d, "sec1")
        write_sec(self.d, "sec1", "变更后")
        verdict, reason = sn.semantic_gate_verdict(self.d, "sec1")
        self.assertEqual(verdict, "BLOCK")
        # blocking issue → REVISE（作者返修）
        write_check_result(self.d, "sec1", conclusion="REVISE",
                           issues=[blocking_issue()])
        verdict, _ = sn.semantic_gate_verdict(self.d, "sec1")
        self.assertEqual(verdict, "REVISE")
        # PASS → 放行
        write_check_result(self.d, "sec1")
        verdict, _ = sn.semantic_gate_verdict(self.d, "sec1")
        self.assertEqual(verdict, "PASS")
        # 切片缺失 → BLOCK
        os.remove(os.path.join(self.d, "sec1.tex"))
        verdict, _ = sn.semantic_gate_verdict(self.d, "sec1")
        self.assertEqual(verdict, "BLOCK")

    def test_needs_check(self):
        write_sec(self.d, "sec1")
        self.assertTrue(sn.needs_check(self.d, "sec1"))      # 无记录
        write_check_result(self.d, "sec1")
        self.assertFalse(sn.needs_check(self.d, "sec1"))     # 绑定且 PASS
        write_sec(self.d, "sec1", "内容变更")
        self.assertTrue(sn.needs_check(self.d, "sec1"))      # 摘要过期


# ---------------------------------------------------------------------------
# T02 规划节点
# ---------------------------------------------------------------------------

class T02_PlanCheck(unittest.TestCase):
    """T02: 零派发放行 / 规划批次 / 强制入批 / 熔断 / 门禁失败。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _staged_state(self, sections=("sec1", "sec2"), **extra):
        for sec in sections:
            write_sec(self.d, sec)
        state = make_check_state(
            self.d, sections=sections,
            statuses={sec: "staged" for sec in sections}, **extra)
        return state

    def test_zero_dispatch_when_all_checked(self):
        """§14：无改动重跑不调 LLM——全部已有匹配 PASS 审读 → 零派发放行。"""
        state = self._staged_state()
        for sec in ("sec1", "sec2"):
            write_check_result(self.d, sec)
        patch, runner_calls = run_plan_check(state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["active_section"], "sec1")
        self.assertIsNone(patch["semantic_repair_target"])
        self.assertFalse(os.path.exists(check_plan_file(self.d)))
        self.assertEqual(tsb.request_files(self.d), [])
        self.assertEqual(runner_calls, [])             # 不起子进程

    def test_plans_batch_for_unchecked_scopes(self):
        state = self._staged_state()
        patch, runner_calls = run_plan_check(state)
        self.assertEqual(patch["stage"], "T2_CHECK_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["semantic_check_rounds"], 1)
        self.assertEqual(patch["dispatch_metrics"]["submitted"], 2)
        self.assertEqual(patch["dispatch_metrics"]["reused"], 0)
        # 派发计划独立文件，round/stage 标记供 dispatch/join 校验
        plan = load_check_plan(self.d)
        self.assertEqual(plan["round"], "semantic")
        self.assertEqual(plan["stage"], "T2_CHECK")
        self.assertEqual(sorted(plan["sections"]), ["sec1", "sec2"])
        self.assertEqual(len(plan["requests"]), 2)
        self.assertIn("parent_dispatch_ids", plan)
        # 写作轮计划不被覆盖
        self.assertFalse(os.path.exists(tsb.plan_file(self.d)))
        # 派发单落盘（每 scope 一个）
        self.assertEqual(len(tsb.request_files(self.d)), 2)
        req = patch["dispatch_plan"]["requests_full"][0]
        self.assertEqual(req["prompt_file"], sn.SEMANTIC_PROMPT_FILE)
        self.assertEqual(req["artifacts"], [f".pd/semantic/{req['scope']}.json"])
        self.assertTrue(req["command"].startswith("python scripts/check_semantic.py"))
        self.assertEqual(req["context"]["section_digest"],
                         sec_digest(self.d, req["scope"]))
        # check_dispatch 门禁以 --plan 指向审读轮计划
        calls = [c for c in runner_calls if c[0] == "run_gate_script"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "check_dispatch")
        self.assertIn(CHECK_PLAN_REL, calls[0][3])
        self.assertEqual(calls[0][4], ".pd/reports/check_dispatch_semantic.json")

    def test_repair_target_forced_into_batch(self):
        """submit 硬闸门要求的补派 scope 即使已有 PASS 也重派。"""
        state = self._staged_state(semantic_repair_target="sec1")
        write_check_result(self.d, "sec1")
        patch, _ = run_plan_check(state)
        self.assertEqual(patch["stage"], "T2_CHECK_PENDING")
        plan = load_check_plan(self.d)
        self.assertEqual(sorted(plan["sections"]), ["sec1", "sec2"])
        self.assertIsNone(patch["semantic_repair_target"])

    def test_rounds_exceeded_halts(self):
        state = self._staged_state(semantic_check_rounds=sn.MAX_SEMANTIC_ROUNDS)
        patch, _ = run_plan_check(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("轮次超限", patch["error_message"])

    def test_gate_failure_halts(self):
        state = self._staged_state()
        patch, _ = run_plan_check(state, gate=tsb.gate_report(
            "REJECT", errors=[{"code": "UNDER_DISPATCH"}]))
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("派发门禁未通过", patch["error_message"])


# ---------------------------------------------------------------------------
# T03 派发节点
# ---------------------------------------------------------------------------

class T03_DispatchCheck(unittest.TestCase):
    """T03: stale plan / 空单 HALT；提交全部且绝不 join；模式判定。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.addCleanup(tsb.register_capable_worker(4))
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        tsb.write_config(self.d, agent_dispatch="fast")
        for sec in ("sec1", "sec2"):
            write_sec(self.d, sec)
        self.state = make_check_state(self.d, sections=("sec1", "sec2"),
                                      statuses={"sec1": "staged", "sec2": "staged"})
        self.plan_patch, _ = run_plan_check(self.state)
        self.state.update(self.plan_patch)
        self.batch_id = self.state["batch_id"]
        self.requests = self.state["dispatch_plan"]["requests_full"]

    def _patch_pool(self, pool):
        # semantic_nodes 在 import 时把 _get_pool 绑进自身命名空间，
        # 必须同时替换两处绑定（sbn 与 sn）才能让节点拿到替身池。
        original = sn._get_pool
        sn._get_pool = lambda *a, **kw: pool
        self.addCleanup(lambda: setattr(sn, "_get_pool", original))

    def test_stale_plan_halts(self):
        state = dict(self.state)
        state["batch_id"] = "bat-t2-check-other"
        patch = node_t2_dispatch_check(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("stale plan", patch["error_message"])

    def test_empty_requests_halts(self):
        state = dict(self.state)
        plan = dict(state["dispatch_plan"])
        plan["requests_full"] = []
        state["dispatch_plan"] = plan
        patch = node_t2_dispatch_check(state)
        self.assertEqual(patch["stage"], "HALTED")

    def test_parallel_submits_without_join(self):
        pool = tsb.FakePool(stats={"deferred": 0, "ok": 2, "max_in_flight": 2})
        self._patch_pool(pool)
        patch = node_t2_dispatch_check(self.state)
        self.assertEqual(patch["stage"], "T2_CHECK_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "parallel")
        self.assertEqual(len(pool.submitted), 2)
        # 任务4：dispatch 节点绝不 join（join 是 join 节点专属职责）
        self.assertEqual(pool.join_calls, [])

    def test_manual_mode_when_deferred(self):
        pool = tsb.FakePool(stats={"deferred": 2})
        self._patch_pool(pool)
        patch = node_t2_dispatch_check(self.state)
        self.assertEqual(patch["dispatch_mode"], "manual")
        self.assertEqual(pool.join_calls, [])


# ---------------------------------------------------------------------------
# T04 汇总节点
# ---------------------------------------------------------------------------

class T04_JoinCheck(unittest.TestCase):
    """T04: 并行收尾 / 返修路由 / 补派审读 / 并发度硬门禁 / manual 中断。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.addCleanup(tsb.register_capable_worker(4))
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        tsb.write_config(self.d, agent_dispatch="fast")
        for sec in ("sec1", "sec2"):
            write_sec(self.d, sec)
        self.state = make_check_state(self.d, sections=("sec1", "sec2"),
                                      statuses={"sec1": "staged", "sec2": "staged"})
        self.plan_patch, _ = run_plan_check(self.state)
        self.state.update(self.plan_patch)
        self.batch_id = self.state["batch_id"]
        self.requests = self.state["dispatch_plan"]["requests_full"]
        # 写作轮结果（父子链来源）
        self.state["dispatch_results"] = author_results(self.requests)

    def _install_pool(self, results, stats=None):
        pool = tsb.FakePool(results=results, stats=stats)
        sbn._POOL_STATE["pool"] = pool
        sbn._POOL_STATE["batch_id"] = self.batch_id
        return pool

    @staticmethod
    def _ok_result(req):
        return DispatchResult(
            dispatch_id=req["dispatch_id"], scope=req["scope"],
            status="ok", output={"files": {}}, attempts=1)

    def test_all_pass_proceeds(self):
        for sec in ("sec1", "sec2"):
            write_check_result(self.d, sec)
        results = {req["dispatch_id"]: self._ok_result(req)
                   for req in self.requests}
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "max_in_flight": 2})
        patch = node_t2_join_check(self.state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["active_section"], "sec1")
        self.assertIsNone(patch["semantic_repair_target"])
        self.assertEqual(patch["dispatch_metrics"]["ok"], 2)

    def test_revise_routes_to_author_repair_with_parent_chain(self):
        write_check_result(self.d, "sec1", conclusion="REVISE",
                           issues=[blocking_issue("PROOF_DIVISION_BY_ZERO")])
        write_check_result(self.d, "sec2")
        results = {req["dispatch_id"]: self._ok_result(req)
                   for req in self.requests}
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "max_in_flight": 2})
        patch = node_t2_join_check(self.state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec1")
        self.assertEqual(patch["stage"], "REPAIR_LOOP")
        self.assertIn("PROOF_DIVISION_BY_ZERO", patch["repair_reason"])
        # 父子链：写作者 dispatch_id 必须记录在返修理由里
        author_did = self.requests[0]["dispatch_id"]
        self.assertIn(author_did, patch["repair_reason"])
        # revise 不触发补派审读（是写作者的错）
        self.assertNotIn("semantic_repair_target", patch)

    def test_missing_result_sets_repair_target(self):
        write_check_result(self.d, "sec1")            # sec2 缺审读结果
        results = {req["dispatch_id"]: self._ok_result(req)
                   for req in self.requests}
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "max_in_flight": 2})
        patch = node_t2_join_check(self.state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec2")
        self.assertEqual(patch["semantic_repair_target"], "sec2")

    def test_stale_digest_sets_repair_target(self):
        write_check_result(self.d, "sec1")
        write_check_result(self.d, "sec2")
        write_sec(self.d, "sec2", "作者返修后的新候选")   # sec2 摘要过期
        results = {req["dispatch_id"]: self._ok_result(req)
                   for req in self.requests}
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "max_in_flight": 2})
        patch = node_t2_join_check(self.state)
        self.assertEqual(patch["semantic_repair_target"], "sec2")
        self.assertEqual(patch["repair_target"], "sec2")

    def test_under_parallelism_halts(self):
        for sec in ("sec1", "sec2"):
            write_check_result(self.d, sec)
        results = {req["dispatch_id"]: self._ok_result(req)
                   for req in self.requests}
        # 计划承诺 min_parallelism=2，实际 max_in_flight=1 → 伪装完成
        state = dict(self.state)
        plan = dict(state["dispatch_plan"])
        plan["min_parallelism"] = 2
        state["dispatch_plan"] = plan
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "max_in_flight": 1})
        patch = node_t2_join_check(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("并发度", patch["error_message"])

    def test_manual_interrupt_once_then_disk_rederive(self):
        captured = []
        original = sn.interrupt

        def fake_interrupt(payload):
            captured.append(payload)
            return {}

        sn.interrupt = fake_interrupt
        self.addCleanup(lambda: setattr(sn, "interrupt", original))
        for sec in ("sec1", "sec2"):
            write_check_result(self.d, sec)
        # 无活池、未中断过 → interrupt 一次（整批语义审读挂起）
        patch = node_t2_join_check(self.state)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["type"], "AGENT_DISPATCH")
        self.assertEqual(captured[0]["stage"], "T2_CHECK")
        self.assertEqual(len(captured[0]["requests"]), 2)
        self.assertEqual(patch["manual_interrupted_batch"], self.batch_id)
        self.assertEqual(patch["stage"], "T2_CHECK_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "manual")
        # 恢复后（manual_interrupted_batch 已等于 batch_id）→ 磁盘重推导，不重复 interrupt
        resumed = dict(self.state)
        resumed.update(patch)
        patch2 = node_t2_join_check(resumed)
        self.assertEqual(len(captured), 1)
        self.assertEqual(patch2["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch2["routing_action"], "PROCEED")

    def test_protocol_violation_halts(self):
        original = sn.interrupt
        sn.interrupt = lambda payload: {"dispatch_protocol": "forged-v9"}
        self.addCleanup(lambda: setattr(sn, "interrupt", original))
        patch = node_t2_join_check(self.state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("BAD_DISPATCH_PROTOCOL", patch["error_message"])

    def test_stale_plan_halts(self):
        state = dict(self.state)
        state["batch_id"] = "bat-t2-check-other"
        patch = node_t2_join_check(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("stale plan", patch["error_message"])


# ---------------------------------------------------------------------------
# T05 打乱血缘
# ---------------------------------------------------------------------------

class T05_ShuffleLineage(unittest.TestCase):
    """T05: 确定性选项重排（shuffle）不判 stale——血缘台账桥接。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _write_shuffle_map(self, transitions):
        path = os.path.join(self.d, ".pd", "shuffle-map.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1,
                       "identity": "sec1#0",
                       "order": ["a", "b", "c", "d"],
                       "transitions": transitions}, f, ensure_ascii=False)

    def test_lineage_exempts_stale(self):
        before = "a" * 64
        write_sec(self.d, "sec1", "打乱前候选")
        write_check_result(self.d, "sec1", digest=before)
        after = sec_digest(self.d, "sec1")
        self._write_shuffle_map({before: [[before, after]]})
        status, detail = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "ok")
        self.assertIn("血缘", detail)
        self.assertFalse(sn.needs_check(self.d, "sec1"))

    def test_unrelated_digest_stays_stale(self):
        before = "a" * 64
        write_sec(self.d, "sec1", "新候选")
        write_check_result(self.d, "sec1", digest=before)
        after = sec_digest(self.d, "sec1")
        # 台账里只有别的迁移链 → 血缘不成立
        self._write_shuffle_map({"b" * 64: [["b" * 64, after]]})
        status, _ = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "stale_digest")

    def test_check_script_lineage_helpers(self):
        """check_semantic.py 的打乱血缘助手与节点侧同口径。"""
        import check_semantic
        edges = check_semantic.load_shuffle_transitions(self.d)
        self.assertEqual(edges, {})
        self._write_shuffle_map({"a" * 64: [["a" * 64, "b" * 64]]})
        edges = check_semantic.load_shuffle_transitions(self.d)
        self.assertEqual(edges.get("a" * 64), ["b" * 64])
        self.assertTrue(check_semantic.shuffle_bound("a" * 64, "b" * 64, edges))
        self.assertFalse(check_semantic.shuffle_bound("a" * 64, "c" * 64, edges))


if __name__ == "__main__":
    unittest.main()
