#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_issue_routing.py — 卡 05：统一 issue 适配层与显式路由表。

ROUTE-02  未知 error 不降级、不放行：HALT（unclassified）；基础设施失败 / 定位不到
          切片 / 未知预算类别 一律停机，不再派作者盲改。
ROUTE-03  review / info / warning 不自动派 Agent；渲染资源/引擎类缺陷不派作者；
          确定性问题（课堂计划）落到确定性目标；PDF 页数溢出 → human/pdf-layout。
ROUTE-04  合法打乱血缘 + REVISE / NEEDS_EVIDENCE / blocking issue 仍然阻断
          （血缘只豁免“摘要不一致”，不豁免“审读结论”）。

全部使用 fake GateRunner / ScriptRunner 与临时目录；不启动真实流水线、不派发 Agent。
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow import edges as wf_edges  # noqa: E402
from workflow import issues as iss  # noqa: E402
from workflow.nodes import build_nodes as bn  # noqa: E402
from workflow.nodes import semantic_nodes as sn  # noqa: E402
from langgraph.graph import END  # noqa: E402

import test_semantic_nodes as tsn  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

GATES = ("check_pedagogy", "check_numbering", "check_dialogue", "check_scope",
         "check_fidelity", "check_semantic", "check_provenance", "check_budget")


def rep(gate, status="PASS", raw=None, issues=None, exit_code=None, **extra):
    """构造与 script_runner._synthesize_gate_report 同形的 GateReport。"""
    passed = status == "PASS"
    out = {
        "gate_name": gate, "status": status, "passed": passed,
        "issues": list(issues or []), "raw_report": raw or {},
        "exit_code": (0 if passed else 1) if exit_code is None else exit_code,
        "report_path": None, "report_is_fresh": True,
    }
    out.update(extra)
    return out


def raw_issue_report(gate, *msgs, severity="error", **extra):
    """raw_report.issues=[{severity,message}]；GateReport.issues 同步镜像 + 退出码回声。"""
    raw_items = [{"severity": severity, "message": m} for m in msgs]
    mirrored = [{"category": "UNKNOWN", "severity": severity, "message": m}
                for m in msgs]
    mirrored.append({"category": "EXECUTION_FAILURE", "severity": "HIGH_RISK",
                     "message": f"{gate} exit 1"})
    return rep(gate, "REJECT", raw={"issues": raw_items}, issues=mirrored, **extra)


class FakeGateRunner:
    """run_gates 返回预置报告；其余门禁默认 PASS。"""

    overrides = {}
    calls = []

    def __init__(self, self_dir):
        self.self_dir = self_dir

    def run_gates(self, specs, run_id=None):
        FakeGateRunner.calls.append([s["gate_name"] for s in specs])
        return {s["gate_name"]: copy.deepcopy(
            FakeGateRunner.overrides.get(s["gate_name"], rep(s["gate_name"])))
            for s in specs}

    def run_gate(self, gate_name, **kw):
        return copy.deepcopy(FakeGateRunner.overrides.get(gate_name, rep(gate_name)))


class FakeScriptRunner:
    gate_reports = {}
    scripts = []

    def __init__(self, self_dir):
        self.self_dir = self_dir

    def run_gate_script(self, gate_name, **kw):
        return copy.deepcopy(FakeScriptRunner.gate_reports[gate_name])

    def run_script(self, script_name, args=None, **kw):
        FakeScriptRunner.scripts.append(script_name)
        return 0, "ok", ""


class NodeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        FakeGateRunner.overrides = {}
        FakeGateRunner.calls = []
        FakeScriptRunner.gate_reports = {}
        FakeScriptRunner.scripts = []
        for target, fake in ((bn, ("GateRunner", FakeGateRunner)),
                             (bn, ("ScriptRunner", FakeScriptRunner))):
            p = mock.patch.object(target, fake[0], fake[1])
            p.start()
            self.addCleanup(p.stop)
        self.state = {"self_dir": self.d, "history": [], "run_id": "r1",
                      "expected_sections": ["sec1", "sec2", "sec3"]}

    def checks(self, **overrides):
        FakeGateRunner.overrides = overrides
        return bn.node_t3_checks(dict(self.state))


# ---------------------------------------------------------------------------
# ROUTE-02
# ---------------------------------------------------------------------------

class ROUTE02_UnknownErrorHalts(NodeBase):

    def test_unknown_gate_error_is_unclassified_blocking_and_keeps_raw(self):
        raw = {"severity": "error", "message": "全新的未知故障", "extra": 7}
        issue = iss.normalize_issue(raw, gate="check_something_new")
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["category"], "unclassified")
        self.assertTrue(issue["blocking"])
        self.assertEqual(issue["repair_kind"], "human")
        self.assertEqual(issue["evidence"]["raw"], raw)
        d = iss.decide_route([issue])
        self.assertEqual(d["action"], "HALT")
        self.assertFalse(d["dispatch_agent"])
        self.assertTrue(d["unclassified"])

    def test_unknown_severity_is_never_downgraded_to_warning(self):
        issue = iss.normalize_issue({"severity": "catastrophic", "message": "x"},
                                    gate="check_something_new")
        self.assertEqual(issue["severity"], "error")
        self.assertTrue(issue["blocking"])
        issue2 = iss.normalize_issue({"message": "no severity at all"},
                                     gate="check_something_new")
        self.assertEqual(issue2["severity"], "error")
        self.assertTrue(issue2["blocking"])

    def test_non_pass_without_explanation_halts(self):
        report = rep("check_numbering", "REJECT", exit_code=1)
        d = iss.route_gate_report(report, "check_numbering")
        self.assertEqual(d["action"], "HALT")
        self.assertTrue(d["unclassified"])

    def test_normalize_does_not_mutate_report(self):
        report = raw_issue_report("check_pedagogy", "sec3.tex 缺少知识盒")
        before = copy.deepcopy(report)
        iss.route_gate_report(report, "check_pedagogy")
        self.assertEqual(report, before)

    def test_budget_unknown_category_halts_instead_of_blind_repair(self):
        patch = self.checks(check_budget=raw_issue_report(
            "check_budget", "出现了规则表之外的预算错误"))
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")
        self.assertNotEqual(patch.get("routing_action"), "REPAIR_SECTION")

    def test_gate_infrastructure_error_halts_not_author_repair(self):
        err = rep("check_pedagogy", "ERROR", exit_code=130,
                  issues=[{"category": "REPORT_MISSING_OR_STALE",
                           "severity": "HIGH_RISK", "message": "报告缺失"}])
        patch = self.checks(check_pedagogy=err)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")

    def test_unlocated_pedagogy_error_halts_not_blind_repair(self):
        patch = self.checks(check_pedagogy=raw_issue_report(
            "check_pedagogy", "全局缺少前置知识卡（无法定位切片）"))
        self.assertEqual(patch["routing_action"], "HALT")

    def test_located_pedagogy_error_still_repairs_that_section(self):
        patch = self.checks(check_pedagogy=raw_issue_report(
            "check_pedagogy", "sec3.tex 缺少结论框"))
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec3")

    def test_unlocated_numbering_and_dialogue_halt(self):
        for gate in ("check_numbering", "check_dialogue"):
            with self.subTest(gate=gate):
                patch = self.checks(**{gate: raw_issue_report(gate, "结构错误（无切片）")})
                self.assertEqual(patch["routing_action"], "HALT")

    def test_unlocated_semantic_failure_halts(self):
        sem = rep("check_semantic", "REJECT", raw={"errors": [
            {"code": "SEMANTIC_REVISE", "detail": "没有切片名的描述"}]})
        patch = self.checks(check_semantic=sem)
        self.assertEqual(patch["routing_action"], "HALT")

    def test_build_failure_without_locatable_section_halts(self):
        FakeScriptRunner.gate_reports["build"] = raw_issue_report(
            "build", "LaTeX 编译失败（日志无切片信息）")
        patch = bn.node_t3_build(dict(self.state))
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertNotEqual(patch.get("repair_target"), "sec2")

    def test_build_failure_locatable_from_message_repairs_that_section(self):
        FakeScriptRunner.gate_reports["build"] = raw_issue_report(
            "build", "! Missing \\item  (sec2.tex)")
        patch = bn.node_t3_build(dict(self.state))
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec2")

    def test_scope_semantic_provenance_fidelity_paths_unchanged(self):
        # 旧行为护栏：fidelity 正文级 → REPAIR_SECTION；契约级 → REPLAN
        fid = rep("check_fidelity", "REJECT", raw={"errors": [
            {"code": "FIDELITY_UNGROUNDED_QID", "detail": "sec2 引用了未登记 qid"}]})
        self.assertEqual(self.checks(check_fidelity=fid)["repair_target"], "sec2")
        # N0：契约级改用**真实**码（此前用合成码 FIDELITY_MAP_BROKEN 钉住了
        # “未登记码一律 REPLAN source-map.md”的大锤行为；现在未登记码收敛为
        # HALT + 缺口报告，真实码照旧按表裁决）。
        fid2 = rep("check_fidelity", "REJECT", raw={"errors": [
            {"code": "FIDELITY_MAP_MISSING", "detail": "映射表损坏"}]})
        p2 = self.checks(check_fidelity=fid2)
        self.assertEqual((p2["routing_action"], p2["repair_target"]),
                         ("REPLAN", "source-map.md"))
        sco = rep("check_scope", "REJECT", raw={"errors": [
            {"code": "SCOPE_PROFILE_DRIFT", "detail": "x"}]})
        self.assertEqual(self.checks(check_scope=sco)["routing_action"], "HALT")

    def test_unregistered_fidelity_code_halts_with_gap_instead_of_replan(self):
        # N0：兜底必须收敛——未登记码不再被前缀大锤放大成 T1 全链重跑，
        # 而是停机并把缺口写进 error_message（先暴露、后补表）。
        fid = rep("check_fidelity", "REJECT", raw={"errors": [
            {"code": "FIDELITY_MAP_BROKEN", "detail": "映射表损坏"}]})
        patch = self.checks(check_fidelity=fid)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("未登记", patch["error_message"])
        self.assertIn("FIDELITY_MAP_BROKEN", patch["error_message"])

    def test_fidelity_gate_crash_halts_instead_of_replan(self):
        err = rep("check_fidelity", "ERROR", exit_code=130)
        patch = self.checks(check_fidelity=err)
        self.assertEqual(patch["routing_action"], "HALT")

    def test_every_route_table_entry_is_well_formed(self):
        tables = [iss.CODE_ROUTES, iss.CATEGORY_ROUTES, iss.GATE_ERROR_ROUTES,
                  dict(iss.PREFIX_ROUTES), iss.STRICT_WARNING_GATES]
        routes = [r for t in tables for r in t.values()]
        routes += [r for _, r in iss.RENDER_MESSAGE_ROUTES]
        for r in routes:
            self.assertIn(r.repair_kind, iss.REPAIR_KINDS, r)
            if r.repair_kind == "agent":
                self.assertTrue(r.repair_target, r)
            if r.repair_kind == "human":
                self.assertEqual(r.repair_kind, "human")

    def test_halt_edge_ends_the_graph(self):
        patch = self.checks(check_budget=raw_issue_report("check_budget", "未知"))
        self.assertEqual(wf_edges.route_after_checks(dict(self.state, **patch)), END)


# ---------------------------------------------------------------------------
# ROUTE-03
# ---------------------------------------------------------------------------

class ROUTE03_NoAgentForReviewInfoAndMechanical(NodeBase):

    def test_warning_and_info_never_block_or_dispatch(self):
        for sev in ("warning", "info"):
            with self.subTest(sev=sev):
                issue = iss.normalize_issue({"severity": sev, "message": "sec1.tex 建议"},
                                            gate="check_pedagogy")
                self.assertFalse(issue["blocking"])
                self.assertEqual(issue["repair_kind"], "none")
                d = iss.decide_route([issue])
                self.assertEqual(d["action"], "PROCEED")
                self.assertFalse(d["dispatch_agent"])

    def test_pedagogy_advisory_warning_blocks_only_in_strict(self):
        raw = {"severity": "warning", "message": "sec1.tex 建议补知识盒"}
        loose = iss.normalize_issue(raw, gate="check_pedagogy")
        strict = iss.normalize_issue(raw, gate="check_pedagogy", strict=True)
        self.assertFalse(loose["blocking"])
        self.assertTrue(strict["blocking"])

    def test_limit_warning_is_advisory_but_strict_stops_without_agent(self):
        raw = {"severity": "warning", "message": "总课时 120 分钟超出预算 90 分钟"}
        loose = iss.normalize_issue(raw, gate="check_budget")
        self.assertEqual((loose["category"], loose["blocking"]), ("limit_advisory", False))
        strict = iss.normalize_issue(raw, gate="check_budget", strict=True)
        self.assertTrue(strict["blocking"])
        self.assertEqual(iss.decide_route([strict])["action"], "HALT")

    def test_pdf_page_overflow_is_human_pdf_layout_and_strict_only(self):
        raw = {"severity": "warning", "message": "student.pdf 页数 14 超出上限 10 页"}
        loose = iss.normalize_issue(raw, gate="check_render")
        self.assertFalse(loose["blocking"])
        strict = iss.normalize_issue(raw, gate="check_render", strict=True)
        self.assertEqual(strict["artifact"], "student.pdf")
        self.assertEqual(strict["repair_target"], "pdf-layout")
        self.assertEqual(strict["repair_kind"], "human")
        self.assertTrue(strict["blocking"])
        d = iss.decide_route([strict])
        self.assertEqual((d["action"], d["dispatch_agent"]), ("HALT", False))

    def test_render_resource_and_engine_defects_never_dispatch_author(self):
        cases = [
            "缺少 student.html", "缺少本地 KaTeX 资源", "引用外网 CDN",
            "图片资产缺失: fig1.png", "PDF 无法解析", "目录 TOC 缺失",
            "HTML 含双重转义", "残留未转换记号", "定界符不成对",
            "3 个 .qc-math 段为空", "选项按钮数量不符",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                issue = iss.normalize_issue({"severity": "error", "message": msg},
                                            gate="check_render")
                self.assertEqual(issue["repair_kind"], "deterministic", issue)
                self.assertNotEqual(issue["category"], "unclassified")
                d = iss.decide_route([issue], located_target="sec1")
                self.assertFalse(d["dispatch_agent"], d)
                # RC-2：确定性缺陷不再干停机，交引擎（patch_artifact）执行；
                # 但绝不派 Agent 改 sec*.tex。
                self.assertEqual(d["action"], "REPAIR_DETERMINISTIC", d)
                self.assertTrue(d.get("engine_action"), d)
                self.assertNotEqual(d["action"], "REPAIR_SECTION")

    def test_render_source_defect_dispatches_author_only_when_located(self):
        issue = iss.normalize_issue(
            {"severity": "error", "message": ".qc-math 含 KaTeX 不支持的宏 \\bigsqcup"},
            gate="check_render")
        self.assertEqual((issue["category"], issue["repair_kind"]),
                         ("render_source", "agent"))
        self.assertEqual(iss.decide_route([issue])["action"], "HALT")
        d = iss.decide_route([issue], located_target="sec2")
        self.assertEqual((d["action"], d["repair_target"]), ("REPAIR_SECTION", "sec2"))

    def test_render_resource_failure_in_node_never_dispatches_author(self):
        FakeScriptRunner.gate_reports["build"] = rep("build")
        FakeGateRunner.overrides["check_render"] = raw_issue_report(
            "check_render", "缺少本地 KaTeX 资源")
        patch = bn.node_t3_build(dict(self.state))
        # 资源类缺陷由引擎确定性修复（REPAIR_DETERMINISTIC），不派作者、
        # 也不再干停机把人工晾在那里。
        self.assertEqual(patch["routing_action"], "REPAIR_DETERMINISTIC")
        self.assertNotEqual(patch["routing_action"], "REPAIR_SECTION")

    def test_render_source_failure_in_node_repairs_located_section(self):
        with open(os.path.join(self.d, "sec2.tex"), "w", encoding="utf-8") as f:
            f.write(r"$\bigsqcup_i A_i$")
        FakeScriptRunner.gate_reports["build"] = rep("build")
        FakeGateRunner.overrides["check_render"] = raw_issue_report(
            "check_render", ".qc-math 含 KaTeX 不支持的宏 \\bigsqcup")
        patch = bn.node_t3_build(dict(self.state))
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("REPAIR_SECTION", "sec2"))

    def test_compare_review_item_is_human_review_not_agent(self):
        issue = iss.normalize_issue(
            {"severity": "review", "qid": "sec3-q1", "message": "疑似泄露"},
            gate="compare_versions")
        self.assertEqual((issue["repair_kind"], issue["category"]),
                         ("human", "compare_review"))
        self.assertTrue(issue["blocking"])
        self.assertEqual(issue["evidence"]["raw_severity"], "review")
        d = iss.decide_route([issue])
        self.assertEqual((d["action"], d["dispatch_agent"]), ("HALT", False))

    def test_compare_review_node_without_notes_halts_with_zero_agent(self):
        report = rep("compare_versions", "REVIEW", raw={"issues": [
            {"severity": "review", "qid": "sec3-q1", "message": "疑似泄露"}]},
            issues=[{"category": "UNKNOWN", "severity": "review", "message": "疑似泄露"}],
            exit_code=0)
        FakeScriptRunner.gate_reports["compare_versions"] = report
        patch = bn.node_t3_compare(dict(self.state))
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(wf_edges.route_after_compare(dict(self.state, **patch))
                         if hasattr(wf_edges, "route_after_compare") else END, END)

    def test_compare_info_only_passes(self):
        report = rep("compare_versions", "PASS", raw={"issues": [
            {"severity": "info", "message": "两版仅答案差异"}]},
            issues=[{"category": "UNKNOWN", "severity": "info", "message": "两版仅答案差异"}])
        FakeScriptRunner.gate_reports["compare_versions"] = report
        patch = bn.node_t3_compare(dict(self.state))
        self.assertEqual(patch["routing_action"], "PROCEED")

    def test_compare_hard_leak_with_qid_repairs_that_section(self):
        report = raw_issue_report("compare_versions", "sec3-q1 学生版含答案")
        report["raw_report"]["issues"][0]["qid"] = "sec3-q1"
        FakeScriptRunner.gate_reports["compare_versions"] = report
        patch = bn.node_t3_compare(dict(self.state))
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("REPAIR_SECTION", "sec3"))

    def test_compare_hard_leak_unlocated_halts(self):
        FakeScriptRunner.gate_reports["compare_versions"] = raw_issue_report(
            "compare_versions", "学生版含答案（无位置）")
        patch = bn.node_t3_compare(dict(self.state))
        self.assertEqual(patch["routing_action"], "HALT")

    def test_classroom_plan_missing_is_deterministic_generation_not_author(self):
        bud = rep("check_budget", "REJECT", raw={"category": "artifact", "issues": [
            {"severity": "error", "message": "classroom-plan.md 缺失"}]},
            issues=[{"category": "UNKNOWN", "severity": "error",
                     "message": "classroom-plan.md 缺失"}], category="artifact")
        issue = iss.normalize_report(bud, "check_budget")[0]
        self.assertEqual((issue["repair_kind"], issue["repair_target"]),
                         ("deterministic", "classroom-plan.md"))
        d = iss.decide_route([issue])
        self.assertFalse(d["dispatch_agent"])
        self.assertEqual(d["repair_target"], "classroom-plan.md")

        # 节点层（卡 07：生成发生在检查之前而非失败之后）：有 coverage-map.md
        # 就先确定性生成，再做预算检查（生成后检查通过）；不派作者
        with open(os.path.join(self.d, "coverage-map.md"), "w", encoding="utf-8") as f:
            f.write("| 原文节点 |\n|---|\n| n1 |\n")
        patch = bn.node_t3_checks(dict(self.state))
        self.assertIn("gen_classroom_plan.py", FakeScriptRunner.scripts)
        self.assertEqual(patch["routing_action"], "PROCEED")

    def test_classroom_plan_still_missing_after_generation_halts(self):
        bud = rep("check_budget", "REJECT", raw={"category": "artifact", "issues": [
            {"severity": "error", "message": "classroom-plan.md 缺失"}]},
            category="artifact")
        with open(os.path.join(self.d, "coverage-map.md"), "w", encoding="utf-8") as f:
            f.write("| 原文节点 |\n|---|\n| n1 |\n")
        patch = self.checks(check_budget=bud)
        self.assertIn("gen_classroom_plan.py", FakeScriptRunner.scripts)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["repair_target"], "classroom-plan.md")

    def test_coverage_budget_still_replans_coverage_map(self):
        bud = rep("check_budget", "REJECT", raw={"category": "coverage", "issues": [
            {"severity": "error", "message": "覆盖映射不全"}]}, category="coverage")
        patch = self.checks(check_budget=bud)
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("REPLAN", "coverage-map.md"))

    def test_config_budget_still_halts(self):
        bud = rep("check_budget", "REJECT", raw={"category": "config", "issues": [
            {"severity": "error", "message": "配置漂移"}]}, category="config")
        self.assertEqual(self.checks(check_budget=bud)["routing_action"], "HALT")

    def test_mapping_inconsistency_replans_minimal_map(self):
        issue = iss.normalize_issue(
            {"code": "FIDELITY_COVERAGE_MISSING", "detail": "q9 未覆盖"},
            gate="check_fidelity")
        d = iss.decide_route([issue])
        self.assertEqual((d["action"], d["repair_target"]), ("REPLAN", "coverage-map.md"))

    def test_evidence_keeps_original_issue_for_gate_reports(self):
        report = raw_issue_report("check_dialogue", "sec1.tex 泄露答案")
        issues = iss.normalize_report(report, "check_dialogue")
        real = [i for i in issues if i["category"] != "exit_code_echo"]
        self.assertEqual(real[0]["evidence"]["raw"],
                         {"severity": "error", "message": "sec1.tex 泄露答案"})
        echo = [i for i in issues if i["category"] == "exit_code_echo"]
        self.assertEqual(len(echo), 1)
        self.assertFalse(echo[0]["blocking"])
        # 所有 C2 字段齐全
        for key in ("code", "severity", "category", "scope", "qid", "artifact",
                    "message", "evidence", "input_digest", "repair_kind",
                    "repair_target", "blocking"):
            self.assertIn(key, real[0])


# ---------------------------------------------------------------------------
# ROUTE-04
# ---------------------------------------------------------------------------

class ROUTE04_ShuffleLineageDoesNotMaskVerdict(unittest.TestCase):
    """合法打乱血缘只豁免摘要不一致，REVISE/NEEDS_EVIDENCE/blocking 仍须阻断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.helper = tsn.T05_ShuffleLineage("test_lineage_exempts_stale")
        self.helper.d = self.d

    def _lineage_case(self, conclusion="PASS", issues=None):
        before = "a" * 64
        tsn.write_sec(self.d, "sec1", "打乱前候选")
        tsn.write_check_result(self.d, "sec1", conclusion=conclusion,
                               issues=issues, digest=before)
        after = tsn.sec_digest(self.d, "sec1")
        self.helper._write_shuffle_map({before: [[before, after]]})
        return after

    def test_legal_lineage_with_pass_still_ok(self):
        after = self._lineage_case("PASS")
        status, detail = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "ok")
        self.assertIn("血缘", detail)
        self.assertEqual(sn.semantic_gate_verdict(self.d, "sec1")[0], "PASS")

    def test_legal_lineage_with_revise_still_revises(self):
        after = self._lineage_case("REVISE")
        status, _ = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "revise")
        self.assertEqual(sn.semantic_gate_verdict(self.d, "sec1")[0], "REVISE")
        self.assertTrue(sn.needs_check(self.d, "sec1"))

    def test_legal_lineage_with_needs_evidence_still_blocks_submission(self):
        after = self._lineage_case("NEEDS_EVIDENCE")
        status, _ = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "needs_evidence")
        self.assertEqual(sn.semantic_gate_verdict(self.d, "sec1")[0], "REVISE")

    def test_legal_lineage_with_blocking_issue_still_revises(self):
        after = self._lineage_case("PASS", issues=[
            {"code": "ANSWER_MISMATCH", "severity": "blocking", "claim": "答案与解析不符"}])
        status, detail = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "revise")
        self.assertIn("ANSWER_MISMATCH", detail)

    def test_legal_lineage_with_invalid_conclusion_is_invalid(self):
        after = self._lineage_case("MAYBE")
        status, _ = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "invalid_result")
        self.assertEqual(sn.semantic_gate_verdict(self.d, "sec1")[0], "BLOCK")

    def test_without_lineage_digest_mismatch_is_still_stale(self):
        before = "a" * 64
        tsn.write_sec(self.d, "sec1", "候选")
        tsn.write_check_result(self.d, "sec1", conclusion="REVISE", digest=before)
        after = tsn.sec_digest(self.d, "sec1")
        status, _ = sn.evaluate_checker_result(self.d, "sec1", after)
        self.assertEqual(status, "stale_digest")


if __name__ == "__main__":
    unittest.main()
