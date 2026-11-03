#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_artifact_routing.py — 卡 07：产物路由（课堂计划 / 学生 PDF 页数 / 覆盖映射）。

ART-01  需要课堂计划时：先确定性生成（gen_classroom_plan.py）再跑 delivery 预算检查；
        生成失败报告转换原因并停机，不派作者、不对占位计划自环；
        检查后仍不合格 → 只停机一次（不再二次生成）。
ART-02  student.pdf 页数超标（teaching 严格预算）→ artifact=student.pdf、
        target=pdf-layout、repair_kind=human，停机；不改写 classroom-plan.md。
        无法确定产物目标 → unknown_target 停机，不按 category 猜文件。
ART-03  仅覆盖预算类错误（coverage-map.md）→ 只回 coverage-planner，不重跑
        source-auditor；source-map 类问题仍回 source-auditor（来源变更正确失效）。

全部使用 fake GateRunner / ScriptRunner / 编译图 fake 节点与临时目录；
不启动真实流水线、不派发真实 Agent、不读令牌。
"""

import contextlib
import hashlib
import io
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

from langgraph.graph import END  # noqa: E402

import check_budget as cb  # noqa: E402
import gen_classroom_plan as gcp  # noqa: E402
import test_graph_routes as tgr  # noqa: E402
import test_issue_routing as tir  # noqa: E402
from workflow import edges as wf_edges  # noqa: E402
from workflow import issues as iss  # noqa: E402
from workflow.nodes import build_nodes as bn  # noqa: E402

COVERAGE_MD = """# coverage-map

| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes | 推荐讲法 | 易错点 |
|---|---|---|---|---|---|---|---|---|---|
| SRC-01 | sec1 | 掌握极限定义 | q1 | core | guided | q1 | 10 | 先画图再写定义 | 忘记量词顺序 |
| SRC-02 | sec2 | 夹逼准则 | q2 | core | guided | q2 | 8 | 两边夹 | 忽略条件 |
"""
TEACHING_CFG = ("delivery_profile: teaching\nlesson_minutes: 90\n"
                "max_student_pages: 6\n")

EVENTS = []   # 全局调用时序：("script", name) / ("gates", [gate...])


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _write(d, name, text):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


class RecordingGateRunner(tir.FakeGateRunner):
    """记录时序；check_budget 可由测试注入真实 check_budget.main 实现。"""

    budget_impl = None   # callable(self_dir) -> GateReport

    def run_gates(self, specs, run_id=None):
        EVENTS.append(("gates", [s["gate_name"] for s in specs]))
        out = {s["gate_name"]: tir.rep(s["gate_name"]) for s in specs}
        for name, rep_ in tir.FakeGateRunner.overrides.items():
            if name in out:
                out[name] = rep_
        if RecordingGateRunner.budget_impl and "check_budget" in out \
                and "check_budget" not in tir.FakeGateRunner.overrides:
            out["check_budget"] = RecordingGateRunner.budget_impl(self.self_dir)
        return out

    def run_gate(self, gate_name, **kw):
        EVENTS.append(("gate", gate_name))
        return super().run_gate(gate_name, **kw)


class RecordingScriptRunner(tir.FakeScriptRunner):
    """记录时序；script_impl(name,args)->(code,out,err) 可覆盖，默认成功。"""

    script_impl = None

    def run_script(self, script_name, args=None, **kw):
        EVENTS.append(("script", script_name))
        tir.FakeScriptRunner.scripts.append(script_name)
        if RecordingScriptRunner.script_impl:
            return RecordingScriptRunner.script_impl(script_name, args)
        return 0, "ok", ""


def real_gen_impl(script_name, args):
    """真实 gen_classroom_plan.generate（进程内）。"""
    assert script_name == "gen_classroom_plan.py"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = gcp.generate(args[0])
    return code, buf.getvalue(), ""


def real_budget_report(self_dir):
    """真实 check_budget.main(--stage delivery) → GateReport 形状。"""
    path = os.path.join(self_dir, ".pd", "reports", "budget.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cb.main([self_dir, "--stage", "delivery", "--json", path])
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    status = "PASS" if code == 0 else "REJECT"
    return tir.rep("check_budget", status, raw=raw,
                   issues=[{"category": "UNKNOWN", "severity": i["severity"],
                            "message": i["message"]} for i in raw["issues"]],
                   category=raw["category"])


class ArtBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        EVENTS.clear()
        tir.FakeGateRunner.overrides = {}
        tir.FakeScriptRunner.scripts = []
        RecordingGateRunner.budget_impl = None
        RecordingScriptRunner.script_impl = None
        for attr, fake in (("GateRunner", RecordingGateRunner),
                           ("ScriptRunner", RecordingScriptRunner)):
            p = mock.patch.object(bn, attr, fake)
            p.start()
            self.addCleanup(p.stop)
        self.state = {"self_dir": self.d, "history": [], "run_id": "r1",
                      "expected_sections": ["sec1", "sec2", "sec3"]}

    def project(self, cfg=TEACHING_CFG, coverage=True):
        os.makedirs(os.path.join(self.d, ".pd", "reports"), exist_ok=True)
        _write(self.d, "config.yaml", cfg)
        if coverage:
            _write(self.d, "coverage-map.md", COVERAGE_MD)

    def run_checks(self):
        return bn.node_t3_checks(dict(self.state))

    def gen_calls(self):
        return [i for i, e in enumerate(EVENTS) if e == ("script", "gen_classroom_plan.py")]

    def budget_check_index(self):
        for i, e in enumerate(EVENTS):
            if e[0] == "gates" and "check_budget" in e[1]:
                return i
        return None


# ---------------------------------------------------------------------------
# ART-01
# ---------------------------------------------------------------------------

class ART01_GenerateBeforeDeliveryCheck(ArtBase):

    def test_generation_runs_before_the_delivery_budget_check(self):
        self.project()
        patch = self.run_checks()
        self.assertEqual(self.gen_calls(), [0], EVENTS)
        self.assertEqual(self.budget_check_index(), 1, EVENTS)
        self.assertEqual(patch["routing_action"], "PROCEED")

    def test_missing_plan_real_generate_then_real_check_zero_agent(self):
        """真实生成 + 真实 delivery 检查：缺失计划 → 先生成再检查通过，0 派发。"""
        self.project()
        RecordingScriptRunner.script_impl = real_gen_impl
        RecordingGateRunner.budget_impl = staticmethod(real_budget_report)
        plan = os.path.join(self.d, "classroom-plan.md")
        self.assertFalse(os.path.exists(plan))
        patch = self.run_checks()
        self.assertTrue(os.path.isfile(plan))
        self.assertEqual(patch["routing_action"], "PROCEED", patch)
        self.assertNotEqual(patch.get("routing_action"), "REPAIR_SECTION")
        # 调用顺序：generate(1 次) 先于 check(1 次)；之后无任何再次生成
        kinds = [e[0] if e[0] == "script" else "check" for e in EVENTS]
        self.assertEqual(kinds[0], "script")
        self.assertEqual(kinds.count("script"), 1, EVENTS)
        self.assertEqual(sum(1 for e in EVENTS if e[0] == "gates"), 1, EVENTS)

    def test_generation_failure_reports_reason_without_author_or_loop(self):
        self.project()
        RecordingScriptRunner.script_impl = lambda n, a: (
            1, "[gen_classroom_plan] coverage-map.md 中未解析到有效的规划节点表格", "")
        patch = self.run_checks()
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("未解析到有效的规划节点表格", patch["error_message"])
        self.assertNotEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(len(self.gen_calls()), 1, EVENTS)       # 无自环
        self.assertIsNone(self.budget_check_index(), EVENTS)    # 不拿占位计划去检查
        self.assertEqual(wf_edges.route_after_checks(dict(self.state, **patch)), END)

    def test_plan_still_invalid_after_generation_halts_once_no_second_generation(self):
        self.project()
        bud = tir.raw_issue_report(
            "check_budget", "classroom-plan.md 存在未填写的占位符: 第 3 行 '____'",
            category="artifact")
        bud["raw_report"]["category"] = "artifact"
        bud["raw_report"]["issues"][0].update(
            code="CLASSROOM_PLAN_INVALID", artifact="classroom-plan.md")
        tir.FakeGateRunner.overrides = {"check_budget": bud}
        patch = self.run_checks()
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["repair_target"], "classroom-plan.md")
        self.assertEqual(len(self.gen_calls()), 1, EVENTS)       # 只在检查前生成一次
        self.assertEqual(self.gen_calls()[0], 0, EVENTS)

    def test_plan_issue_codes_route_to_deterministic_plan_target(self):
        for code in ("CLASSROOM_PLAN_MISSING", "CLASSROOM_PLAN_INVALID"):
            with self.subTest(code=code):
                issue = iss.normalize_issue(
                    {"severity": "error", "code": code,
                     "artifact": "classroom-plan.md", "message": "x"},
                    gate="check_budget", report_category="artifact")
                self.assertEqual((issue["repair_kind"], issue["repair_target"],
                                  issue["artifact"]),
                                 ("deterministic", "classroom-plan.md",
                                  "classroom-plan.md"))


# ---------------------------------------------------------------------------
# ART-02
# ---------------------------------------------------------------------------

class ART02_StrictPdfOverflowRoutesToPdfLayout(ArtBase):

    def make_overflow_project(self):
        self.project()
        gcp.generate(self.d)                      # 计划已存在且合格
        with open(os.path.join(self.d, "student.pdf"), "wb") as f:
            f.write(b"%PDF-stub")
        p = mock.patch.object(cb, "count_pdf_pages", return_value=7)
        p.start()
        self.addCleanup(p.stop)

    def test_budget_report_tags_pdf_overflow_with_code_and_artifact(self):
        self.make_overflow_project()
        res = cb.check_budget_project(self.d, "delivery")      # 7 页 > 上限 6
        self.assertEqual(res.category, "artifact")
        self.assertEqual(len(res.problems), 1)
        meta = res.issue_meta[res.problems[0]]
        self.assertEqual(meta, {"code": "STUDENT_PDF_PAGE_OVERFLOW",
                                "artifact": "student.pdf"})

    def test_budget_json_report_issue_carries_code_and_artifact(self):
        self.make_overflow_project()
        report = real_budget_report(self.d)["raw_report"]
        err = [i for i in report["issues"] if i["severity"] == "error"]
        self.assertEqual([(i["code"], i["artifact"]) for i in err],
                         [("STUDENT_PDF_PAGE_OVERFLOW", "student.pdf")])

    def test_seven_pages_over_limit_six_routes_human_pdf_layout(self):
        self.make_overflow_project()
        report = real_budget_report(self.d)
        (issue,) = [i for i in iss.normalize_report(report, "check_budget")
                    if i["severity"] == "error"]
        self.assertEqual(issue["artifact"], "student.pdf")
        self.assertEqual(issue["repair_target"], "pdf-layout")
        self.assertEqual(issue["repair_kind"], "human")
        self.assertTrue(issue["blocking"])
        d = iss.decide_route([issue])
        self.assertEqual((d["action"], d["dispatch_agent"]), ("HALT", False))

    def test_node_halts_with_pdf_layout_target_and_never_touches_plan(self):
        self.make_overflow_project()
        RecordingScriptRunner.script_impl = real_gen_impl
        RecordingGateRunner.budget_impl = staticmethod(real_budget_report)
        plan = os.path.join(self.d, "classroom-plan.md")
        before = _sha(plan)
        patch = self.run_checks()
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["repair_target"], "pdf-layout")
        self.assertIn("student.pdf", patch["error_message"])
        self.assertNotIn("课堂计划", patch["error_message"])
        self.assertEqual(_sha(plan), before)                    # 计划未被改写
        # 时序：只在检查前生成 1 次；溢出后无再次生成 / 无重复检查
        kinds = [e[0] for e in EVENTS]
        self.assertEqual(kinds, ["script", "gates"], EVENTS)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "student.pdf")))
        self.assertEqual(wf_edges.route_after_checks(dict(self.state, **patch)), END)

    def test_node_pdf_overflow_without_code_message_only_still_pdf_layout(self):
        """兼容旧报告（无 code/artifact 字段）：按页数消息精确识别，不按 category 猜计划。"""
        self.project()
        bud = tir.rep("check_budget", "REJECT", raw={"category": "artifact", "issues": [
            {"severity": "error",
             "message": "student.pdf 实际页数超标（当前 7 页 > 允许上限 6 页）。"}]},
            category="artifact")
        tir.FakeGateRunner.overrides = {"check_budget": bud}
        patch = self.run_checks()
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("HALT", "pdf-layout"))
        self.assertEqual([e[0] for e in EVENTS], ["script", "gates"], EVENTS)

    def test_unknown_artifact_target_halts_as_unknown_target(self):
        self.project(coverage=False)                  # 无 coverage-map：不会预生成
        bud = tir.rep("check_budget", "REJECT", raw={"category": "artifact", "issues": [
            {"severity": "error", "artifact": "weird.bin", "message": "产物异常"}]},
            category="artifact")
        tir.FakeGateRunner.overrides = {"check_budget": bud}
        (issue,) = iss.normalize_report(bud, "check_budget")
        self.assertEqual(issue["category"], "unknown_target")
        self.assertEqual(issue["repair_kind"], "human")
        patch = self.run_checks()
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("unknown_target", patch["error_message"])
        self.assertEqual(self.gen_calls(), [], EVENTS)            # 不按类别猜生成计划

    def test_category_artifact_no_longer_maps_to_classroom_plan(self):
        self.assertNotIn("artifact", iss.CATEGORY_ROUTES)


# ---------------------------------------------------------------------------
# ART-03
# ---------------------------------------------------------------------------

class ART03_CoverageOnlyDoesNotReauditSource(ArtBase):

    def test_edge_coverage_replan_goes_to_coverage_planner(self):
        self.assertEqual(
            wf_edges.route_after_checks({"routing_action": "REPLAN",
                                         "repair_target": "coverage-map.md"}),
            "t1_request_coverage_plan")

    def test_edge_source_map_replan_still_goes_to_source_auditor(self):
        for target in ("source-map.md", None):
            with self.subTest(target=target):
                self.assertEqual(
                    wf_edges.route_after_checks({"routing_action": "REPLAN",
                                                 "repair_target": target}),
                    "t1_request_source_audit")

    def test_node_coverage_budget_error_replans_coverage_map_only(self):
        self.project(coverage=False)
        bud = tir.rep("check_budget", "REJECT", raw={"category": "coverage", "issues": [
            {"severity": "error", "message": "覆盖映射不全"}]}, category="coverage")
        tir.FakeGateRunner.overrides = {"check_budget": bud}
        patch = self.run_checks()
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("REPLAN", "coverage-map.md"))
        self.assertEqual(wf_edges.route_after_checks(dict(self.state, **patch)),
                         "t1_request_coverage_plan")

    def test_node_source_map_fidelity_error_still_replans_source_map(self):
        self.project(coverage=False)
        rep_ = tir.rep("check_fidelity", "REJECT", raw={"issues": [
            {"severity": "error", "code": "FIDELITY_MANIFEST_MISSING",
             "message": "来源清单缺失"}]},
            issues=[{"category": "UNKNOWN", "severity": "error", "message": "来源清单缺失"}])
        tir.FakeGateRunner.overrides = {"check_fidelity": rep_}
        patch = self.run_checks()
        self.assertEqual((patch["routing_action"], patch["repair_target"]),
                         ("REPLAN", "source-map.md"))
        self.assertEqual(wf_edges.route_after_checks(dict(self.state, **patch)),
                         "t1_request_source_audit")

    def _graph_run(self, patch):
        cnt = tgr._Counter()
        graph = tgr._compile(
            node_t3_checks=cnt.node("t3_checks", patch),
            node_t1_request_coverage_plan=cnt.node(
                "t1_request_coverage_plan", {"stage": "HALTED"}),
            node_t1_request_source_audit=cnt.node(
                "t1_request_source_audit", {"stage": "HALTED"}),
        )
        cfg = {"configurable": {"thread_id": "art03-%s" % patch.get("repair_target")}}
        graph.update_state(cfg, {"stage": "T2_SHUFFLED", "history": []},
                           as_node="t2_shuffle_options")
        graph.invoke(None, cfg)
        return cnt.calls

    def test_compiled_graph_coverage_replan_skips_source_audit(self):
        calls = self._graph_run({"stage": "T3_CHECKS_FAILED", "routing_action": "REPLAN",
                                 "repair_target": "coverage-map.md"})
        self.assertEqual(calls, ["t3_checks", "t1_request_coverage_plan"])
        self.assertNotIn("t1_request_source_audit", calls)

    def test_compiled_graph_source_map_replan_reaches_source_audit(self):
        calls = self._graph_run({"stage": "T3_CHECKS_FAILED", "routing_action": "REPLAN",
                                 "repair_target": "source-map.md"})
        self.assertEqual(calls, ["t3_checks", "t1_request_source_audit"])

    def test_t3_checks_path_map_contains_coverage_planner(self):
        compiled = tgr.wf_graph.create_shrink_courseware_graph(
            checkpointer=tgr.MemorySaver())
        (spec,) = [s for src, _n, s in tgr._branch_specs(compiled)
                   if src == "t3_checks"]
        self.assertIn("t1_request_coverage_plan", spec.ends)
        self.assertIn("t1_request_source_audit", spec.ends)


if __name__ == "__main__":
    unittest.main()
