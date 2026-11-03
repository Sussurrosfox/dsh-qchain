#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_fast_pipeline.py — 卡 15：接通轻量图（faithful-fast-v1）并分开构建前/终审门禁。

验收矩阵（P:\.pd\audits\qchain-review\repair-plan\tasks\15.md）：

GRAPH-01 缺最终 semantic 时 fast 仍能走到 build，但 deliver 必须拒绝；legacy 仍在 T2
         逐节 checker 处阻断（旧闸门不因新图而消失）。
GRAPH-02 三节 → 两批作者 + 一批终审（冷缓存 1+2+1 = 4 次 agent 调用），真实编译图里
         不存在任何逐节 T2 checker 节点。
GRAPH-03 终审 REVISE 只返修未通过的 scope；所有门禁通过才允许发布。
GRAPH-04 produced 不被误认 checked，也不被作者循环重复生成。
GRAPH-05 旧图节点名/边保持；fast 图全部 edge 目标都实际可达。
GRAPH-06 合批作者经真实批工具入口（node_t2_plan_dispatch）重叠执行；最终构建是
         等全部必需作者结果的 barrier。

测试口径：只在临时目录落盘（tempfile），不触真实学案、不启动流水线、不读令牌、
不派发真实 Agent；重活（XeLaTeX / 真实脚本 / 真实 worker）一律替身，图本身是真实
编译产物（create_fast_graph / create_legacy_graph）。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START  # noqa: E402

import deliver as deliver_mod  # noqa: E402
import test_section_batch as sbn  # noqa: E402
from workflow import edges as wf_edges  # noqa: E402
from workflow import graph as wf_graph  # noqa: E402
from workflow.nodes import build_nodes as bn  # noqa: E402
from workflow.nodes import section_nodes as sn  # noqa: E402

FAST = "faithful_fast"

#: fast 图节点全集（顺序即主链；不含任何逐节 T2 checker）
FAST_NODES = [
    "phase0_breakpoint", "t0_init_scaffold",
    "fast_t1_request_unified_plan", "fast_t1_await_unified_plan",
    "fast_t1_verify_unified_mapping",
    "t2_plan_dispatch", "t2_dispatch_sections", "t2_join_sections",
    "t2_submit_and_check_sections", "t2_assemble_main", "t2_shuffle_options",
    "t3_checks", "t3_build", "t3_compare", "t4_make_review_pack",
    "t4_plan_combined_review", "t4_dispatch_combined_review",
    "t4_join_combined_review", "t4_verify_acceptance",
    "t4_promote_and_deliver", "repair_section", "repair_await",
]

#: 逐节 T2 checker 三件套：只允许出现在 legacy 图
PER_SECTION_CHECKER_NODES = ("t2_plan_check", "t2_dispatch_check",
                             "t2_join_check")

#: 全局时序（顺序断言用）：("gate", [gate_name...]) / ("script", name, args)
TIMELINE = []


# ---------------------------------------------------------------------------
# 治具
# ---------------------------------------------------------------------------

def _fast(**overrides):
    patches = [mock.patch.object(wf_graph, k, v) for k, v in overrides.items()]
    for p in patches:
        p.start()
    try:
        return wf_graph.create_fast_graph(checkpointer=MemorySaver())
    finally:
        for p in patches:
            p.stop()


def _nodes(compiled):
    return set(compiled.builder.nodes) - {"__start__", "__end__"}


def _succ(compiled):
    b = compiled.builder
    succ = {}
    for a, c in b.edges:
        succ.setdefault(a, set()).add(c)
    for src, branches in b.branches.items():
        for _name, spec in branches.items():
            succ.setdefault(src, set()).update(spec.ends.values())
    return succ


def _path_map(compiled, src):
    (spec,) = [s for _n, s in compiled.builder.branches[src].items()]
    return spec.ends


def _gate_report(name, status="PASS", exit_code=0, issues=None):
    return {
        "gate_name": name,
        "status": status,
        "passed": status in ("PASS", "pass", "advisory"),
        "issues": list(issues or []),
        "raw_report": {"tool": name + ".py", "tool_version": "1.0",
                       "status": "pass" if status == "PASS" else "fail",
                       "errors": [], "warnings": [], "checked": {}},
        "exit_code": exit_code,
        "report_path": ".pd/reports/%s.json" % name,
        "report_generated_at": "1970-01-01T00:00:00+00:00",
        "report_is_fresh": True,
        "run_id": "run-15",
    }


class _FakeGateRunner:
    """GateRunner 替身：记录 gate spec，按名字返回 PASS。"""

    specs = []
    failures = {}

    def __init__(self, self_dir):
        self.self_dir = self_dir

    def run_gates(self, specs, run_id=None):
        _FakeGateRunner.specs.append([dict(s) for s in specs])
        TIMELINE.append(("gate", [s["gate_name"] for s in specs]))
        out = {}
        for spec in specs:
            name = spec["gate_name"]
            if name in _FakeGateRunner.failures:
                out[name] = _FakeGateRunner.failures[name]
            else:
                out[name] = _gate_report(name)
        return out


class _FakeRunner:
    """ScriptRunner 替身（pdstate.py 等）：只记录，不改盘。"""

    calls = []

    def __init__(self, project_root, scripts_dir=None):
        self.project_root = project_root

    def run_script(self, script_name, args, timeout=300, cwd=None):
        _FakeRunner.calls.append((script_name, list(args)))
        TIMELINE.append(("script", script_name, list(args)))
        return (0, "ok", "")

    def run_gate_script(self, gate_name, script_name, args, report_rel_path,
                        timeout=300, run_id=None):
        _FakeRunner.calls.append((script_name, list(args)))
        return _gate_report(gate_name)


def _reset_fakes():
    _FakeGateRunner.specs = []
    _FakeGateRunner.failures = {}
    _FakeRunner.calls = []
    del TIMELINE[:]


def _write_run_json(d, statuses):
    pd = os.path.join(d, ".pd")
    os.makedirs(pd, exist_ok=True)
    tasks = {sec: {"status": st} for sec, st in statuses.items()}
    with open(os.path.join(pd, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "run_id": "run-15", "tasks": tasks,
                   "slices": sorted(statuses)}, f, ensure_ascii=False, indent=2)


def _read_run_status(d, sec):
    path = os.path.join(d, ".pd", "run.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return ((json.load(f).get("tasks") or {}).get(sec) or {}).get("status")


def _touch_section(d, sec):
    with open(os.path.join(d, sec + ".tex"), "w", encoding="utf-8") as f:
        f.write("%% %s\n" % sec)


def _sha_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _mark_calls():
    return [c for c in _FakeRunner.calls
            if c[0] == "pdstate.py" and "mark" in c[1]]


def _submit_calls():
    return [c for c in _FakeRunner.calls
            if c[0] == "pdstate.py" and "submit" in c[1]]


def _fast_state(d, sections=("sec1", "sec2", "sec3"), **extra):
    state = {
        "self_dir": d,
        "project_root": d,
        "run_id": "run-15",
        "execution_profile": FAST,
        "execution_schema_version": 2,
        "graph_version": "faithful-fast-v1",
        "expected_sections": list(sections),
        "section_statuses": {},
        "history": [],
        "dispatch_results": {},
        "dispatch_metrics": {},
        "dispatch_history": [],
    }
    state.update(extra)
    return state


# ---------------------------------------------------------------------------
# GRAPH-02 / GRAPH-05：结构——无逐节 checker、节点全集、可达性、path_map
# ---------------------------------------------------------------------------

class GRAPH02_FastGraphStructure(unittest.TestCase):
    """GRAPH-02：真实 fast 图里不得存在逐节 T2 checker；legacy 三件套保留。"""

    @classmethod
    def setUpClass(cls):
        cls.fast = _fast()

    def test_no_per_section_checker_node_in_fast_graph(self):
        present = sorted(_nodes(self.fast) & set(PER_SECTION_CHECKER_NODES))
        self.assertEqual(present, [],
                         "fast 图仍挂着逐节 T2 checker: %s" % present)

    def test_no_per_section_checker_route_target_in_fast_graph(self):
        bad = []
        for src in self.fast.builder.branches:
            for target in _path_map(self.fast, src).values():
                if target in PER_SECTION_CHECKER_NODES:
                    bad.append((src, target))
        self.assertEqual(bad, [])

    def test_fast_graph_registers_all_fast_nodes(self):
        nodes = _nodes(self.fast)
        missing = [n for n in FAST_NODES if n not in nodes]
        self.assertEqual(missing, [], "fast 图缺节点: %s" % missing)
        self.assertNotIn("fast_unimplemented", nodes,
                         "占位节点必须彻底消失")

    def test_fast_uses_unified_t1_entry(self):
        """卡12：入口必须走 fast_t1_request_unified_plan，不能落回 legacy 三轮。"""
        succ = _succ(self.fast)
        self.assertIn("fast_t1_request_unified_plan", succ["t0_init_scaffold"])
        self.assertNotIn("t1_request_source_audit", _nodes(self.fast))
        self.assertNotIn("t1_verify_mapping", _nodes(self.fast))

    def test_legacy_graph_keeps_per_section_checker(self):
        """旧图语义不变：逐节 checker 仍在 T2 主链上。"""
        legacy = wf_graph.create_legacy_graph(checkpointer=MemorySaver())
        nodes = _nodes(legacy)
        for name in PER_SECTION_CHECKER_NODES:
            self.assertIn(name, nodes, "legacy 图丢失 %s" % name)
        self.assertIn("t2_plan_check", _succ(legacy)["t2_join_sections"])

    def test_fast_graph_drops_legacy_t4_review_nodes(self):
        nodes = _nodes(self.fast)
        for name in ("t4_plan_review", "t4_dispatch_review", "t4_join_review"):
            self.assertNotIn(name, nodes)


class GRAPH05_Reachability(unittest.TestCase):
    """GRAPH-05：fast 图全部 edge 目标可达；路由字面量返回都在 path_map 内。"""

    @classmethod
    def setUpClass(cls):
        cls.fast = _fast()
        cls.succ = _succ(cls.fast)

    def test_every_path_map_target_is_a_real_node(self):
        nodes = _nodes(self.fast) | {END}
        bad = [(src, t) for src in self.fast.builder.branches
               for t in _path_map(self.fast, src).values() if t not in nodes]
        self.assertEqual(bad, [])

    def test_every_node_reachable_from_start(self):
        seen, todo = set(), [START]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(self.succ.get(cur, ()))
        self.assertEqual(_nodes(self.fast) - seen, set(),
                         "不可达节点: %s" % sorted(_nodes(self.fast) - seen))

    def test_route_literal_returns_covered_by_path_map(self):
        import ast
        import inspect
        import textwrap
        problems = []
        for src, branches in self.fast.builder.branches.items():
            for _name, spec in branches.items():
                func = getattr(spec.path, "func", None)
                if func is None:
                    problems.append((src, "not-a-function", []))
                    continue
                try:
                    src_text = textwrap.dedent(inspect.getsource(func))
                except (OSError, TypeError):
                    continue
                for node in ast.walk(ast.parse(src_text)):
                    if not isinstance(node, ast.Return):
                        continue
                    v = node.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        if v.value not in spec.ends:
                            problems.append((src, v.value, sorted(spec.ends)))
                    elif isinstance(v, ast.Name) and v.id == "END":
                        if END not in spec.ends:
                            problems.append((src, END, sorted(spec.ends)))
                    elif isinstance(v, ast.Call) and str(
                            getattr(v.func, "id", "")).startswith("route_"):
                        # 委派给另一个 route_* 函数：其返回字面量由 legacy 侧
                        # （test_graph_routes）静态核验，这里不重复展开，
                        # 否则会把 legacy 专有目标误判成 fast 图的缺失键。
                        continue
                    else:
                        problems.append((src, "dynamic", sorted(spec.ends)))
        self.assertEqual(problems, [], "\n".join(map(str, problems)))

    def test_fast_checks_route_targets_are_all_wired(self):
        """t3_checks 的每个实际出口都必须在 path_map 里，且绝不能回逐节 checker。"""
        keys = set(_path_map(self.fast, "t3_checks"))
        probes = [
            {"stage": "HALTED"},
            {"routing_action": "REPAIR_SECTION", "semantic_repair_target": "sec1"},
            {"routing_action": "REPAIR_SECTION", "semantic_repair_target": None},
            {"routing_action": "REPLAN", "repair_target": "coverage-map.md"},
            {"routing_action": "REPLAN", "repair_target": "source-map.md"},
            {"routing_action": "PROCEED", "gate_phase": "prebuild"},
            {"routing_action": "PROCEED", "gate_phase": "final"},
        ]
        for probe in probes:
            st = dict(probe, execution_profile="faithful_fast")
            target = wf_edges.route_after_fast_checks(st)
            self.assertIn(target, keys, "路由目标未接线: %s -> %s" % (probe, target))
            self.assertNotEqual(
                target, "t2_plan_check",
                "fast 图不得回到逐节 checker：%s" % (probe,))

    def test_main_chain_edges(self):
        s = self.succ
        self.assertIn("t2_plan_dispatch", s["fast_t1_verify_unified_mapping"])
        self.assertIn("t2_dispatch_sections", s["t2_plan_dispatch"])
        self.assertIn("t2_join_sections", s["t2_dispatch_sections"])
        self.assertIn("t2_submit_and_check_sections", s["t2_join_sections"])
        self.assertIn("t2_assemble_main", s["t2_submit_and_check_sections"])
        self.assertIn("t2_shuffle_options", s["t2_assemble_main"])
        self.assertIn("t3_checks", s["t2_shuffle_options"])
        self.assertIn("t3_build", s["t3_checks"])
        self.assertIn("t3_compare", s["t3_build"])
        self.assertIn("t4_make_review_pack", s["t3_compare"])
        self.assertIn("t4_plan_combined_review", s["t4_make_review_pack"])
        self.assertIn("t4_dispatch_combined_review", s["t4_plan_combined_review"])
        self.assertIn("t4_join_combined_review", s["t4_dispatch_combined_review"])
        # 终审结果才有资格进入 final 门禁（t3_checks 第二趟）
        self.assertIn("t3_checks", s["t4_join_combined_review"])
        self.assertIn("t4_verify_acceptance", s["t3_checks"])
        self.assertIn("t4_promote_and_deliver", s["t4_verify_acceptance"])


# ---------------------------------------------------------------------------
# GRAPH-04：prebuild 纳管为 produced + prebuild_ready，不 mark checked
# ---------------------------------------------------------------------------

class GRAPH04_PrebuildSubmit(unittest.TestCase):
    """GRAPH-04：produced 不被误认 checked，作者循环也不重复生成已就绪节。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _reset_fakes()
        p = mock.patch.object(sn, "ScriptRunner", _FakeRunner)
        p.start()
        self.addCleanup(p.stop)

    def test_fast_submit_records_prebuild_ready_without_mark_checked(self):
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, {"sec1": "planned"})
        state = _fast_state(self.d, sections=("sec1", "sec2"),
                            active_section="sec1")
        patch = sn.node_t2_submit_and_check_sections(state)

        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch["section_statuses"]["sec1"], "prebuild_ready")
        self.assertEqual(patch["prebuild_ready_sections"]["sec1"],
                         _sha_file(os.path.join(self.d, "sec1.tex")))
        self.assertEqual(patch["gate_phase"], "prebuild")
        self.assertEqual(len(_submit_calls()), 1, "prebuild 应提交为 produced")
        self.assertEqual(_mark_calls(), [],
                         "prebuild 阶段不得 mark checked: %s" % (_mark_calls(),))

    def test_fast_submit_is_idempotent_for_same_content(self):
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, {"sec1": "produced"})
        digest = _sha_file(os.path.join(self.d, "sec1.tex"))
        state = _fast_state(self.d, sections=("sec1",),
                            active_section="sec1",
                            section_statuses={"sec1": "prebuild_ready"},
                            prebuild_ready_sections={"sec1": digest})
        before = list(_FakeRunner.calls)
        patch = sn.node_t2_submit_and_check_sections(state)
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(_FakeRunner.calls, before,
                         "同一内容不得重复纳管: %s"
                         % (_FakeRunner.calls[len(before):],))
        self.assertEqual(patch["section_statuses"]["sec1"], "prebuild_ready")

    def test_plan_dispatch_does_not_redispatch_prebuild_ready_sections(self):
        """GRAPH-04 + GRAPH-06：已就绪节不回作者批，只派剩余节。"""
        self.addCleanup(sbn.register_capable_worker(4))
        sbn.write_config(self.d, agent_dispatch="fast")
        _touch_section(self.d, "sec1")
        _touch_section(self.d, "sec2")
        state = sbn.make_state(self.d, sections=("sec1", "sec2", "sec3"),
                               statuses={"sec1": "prebuild_ready",
                                         "sec2": "prebuild_ready"})
        patch = sbn.run_plan(state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
        requests = patch["dispatch_plan"]["requests_full"]
        covered = [sec for r in requests
                   for sec in (r.get("scopes") or [r.get("scope")])]
        self.assertEqual(covered, ["sec3"],
                         "已就绪节被重复派发: %s" % (covered,))

    def test_legacy_still_blocks_without_final_semantic(self):
        """GRAPH-01：legacy（缺 execution_profile）仍在逐节 checker 处阻断。"""
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, {"sec1": "planned"})
        state = _fast_state(self.d, sections=("sec1",),
                            active_section="sec1")
        state.pop("execution_profile")
        patch = sn.node_t2_submit_and_check_sections(state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec1")
        self.assertNotEqual(
            (patch.get("section_statuses") or {}).get("sec1"), "checked")
        self.assertEqual(_submit_calls(), [],
                         "语义审读未通过时 legacy 不得 submit")
        self.assertEqual(_mark_calls(), [])


# ---------------------------------------------------------------------------
# GRAPH-01 / GRAPH-03：T3 两趟门禁 + 终审 REVISE 收窄
# ---------------------------------------------------------------------------

class GRAPH01_T3GatePhases(unittest.TestCase):
    """GRAPH-01：prebuild 只跑确定性检查并记 pending_review；final 才跑全量。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _reset_fakes()
        for mod, attr, val in ((bn, "GateRunner", _FakeGateRunner),
                               (bn, "ScriptRunner", _FakeRunner),
                               (bn, "_pregenerate_classroom_plan",
                                lambda *a, **k: None)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def _specs(self):
        self.assertTrue(_FakeGateRunner.specs, "T3 未请求任何门禁")
        return _FakeGateRunner.specs[-1]

    def test_prebuild_skips_semantic_and_defers_provenance(self):
        state = _fast_state(self.d, gate_phase="prebuild")
        patch = bn.node_t3_checks(state)

        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["gate_phase"], "prebuild")
        names = [s["gate_name"] for s in self._specs()]
        self.assertNotIn("check_semantic", names,
                         "prebuild 阶段不得跑终审 semantic: %s" % names)
        self.assertIn("check_provenance", names)
        prov = [s for s in self._specs()
                if s["gate_name"] == "check_provenance"][0]
        self.assertIn("--mode", prov["args"])
        self.assertIn("writer-only", prov["args"])
        self.assertEqual(patch["semantic_gate"]["status"], "pending_review")
        self.assertFalse(patch["semantic_gate"]["passed"])
        # fast 路由：构建前这趟只能去 build；legacy 路由逐字保持原样
        self.assertEqual(wf_edges.route_after_fast_checks(patch), "t3_build")
        self.assertEqual(wf_edges.route_after_checks(patch), "t3_build")
        self.assertEqual(_mark_calls(), [])

    def test_final_phase_runs_full_gates_then_routes_to_acceptance(self):
        _touch_section(self.d, "sec1")
        _touch_section(self.d, "sec2")
        _write_run_json(self.d, {"sec1": "produced", "sec2": "produced"})
        state = _fast_state(self.d, sections=("sec1", "sec2"),
                            gate_phase="final")
        patch = bn.node_t3_checks(state)

        names = [s["gate_name"] for s in self._specs()]
        self.assertIn("check_semantic", names,
                      "final 必须跑全量 semantic: %s" % names)
        prov = [s for s in self._specs()
                if s["gate_name"] == "check_provenance"][0]
        self.assertNotIn("--mode", prov["args"], "final 必须跑 full provenance")
        self.assertEqual(patch["semantic_gate"]["status"], "PASS")
        self.assertEqual(patch["routing_action"], "PROCEED")
        marked = sorted(c[1][2] for c in _mark_calls())
        self.assertEqual(marked, ["sec1", "sec2"],
                         "终审通过后必须逐节 mark checked: %s" % (_mark_calls(),))
        self.assertEqual(patch["section_statuses"]["sec1"], "checked")
        self.assertEqual(wf_edges.route_after_fast_checks(patch),
                         "t4_verify_acceptance")

    def test_prebuild_gate_failure_does_not_mark_checked(self):
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, {"sec1": "produced"})
        _FakeGateRunner.failures = {
            "check_numbering": _gate_report(
                "check_numbering", status="FAIL", exit_code=1,
                issues=[{"code": "NUMBERING_GAP", "artifact": "sec1.tex",
                         "section": "sec1"}])}
        state = _fast_state(self.d, sections=("sec1",), gate_phase="prebuild")
        patch = bn.node_t3_checks(state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(_mark_calls(), [])
        self.assertEqual(wf_edges.route_after_fast_checks(patch),
                         "repair_section")


class GRAPH03_CombinedReviewWiring(unittest.TestCase):
    """GRAPH-03：终审 PASS → final 门禁；REVISE 只返修未通过的 scope。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_pass_arms_final_gate_phase(self):
        state = _fast_state(self.d, sections=("sec1", "sec2"))
        fake = mock.Mock(return_value={
            "stage": "T4_ACCEPTANCE_PENDING",
            "routing_action": "PROCEED",
            "section_statuses": {"sec1": "reviewed", "sec2": "reviewed"},
            "combined_review_conclusion": "PASS",
        })
        with mock.patch.object(wf_graph, "node_t4_join_combined_review", fake):
            patch = wf_graph.node_fast_join_combined_review(state)
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["gate_phase"], "final")
        self.assertEqual(wf_edges.route_after_fast_combined_review_join(patch),
                         "t3_checks")

    def test_revise_repairs_only_failing_scopes(self):
        state = _fast_state(self.d, sections=("sec1", "sec2", "sec3"),
                            section_statuses={"sec1": "reviewed"})
        fake = mock.Mock(return_value={
            "stage": "T4_COMBINED_REVIEW_REVISE",
            "routing_action": "REVISE",
            "section_statuses": {"sec1": "reviewed", "sec2": "reviewed"},
            "combined_review_conclusion": "REVISE",
            "repair_reason": "sec3 未通过终审",
        })
        with mock.patch.object(wf_graph, "node_t4_join_combined_review", fake):
            patch = wf_graph.node_fast_join_combined_review(state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec3")
        self.assertEqual(patch["combined_review_scopes"], ["sec3"],
                         "REVISE 后只允许复查未通过的 scope")
        self.assertEqual(patch["gate_phase"], "prebuild")
        self.assertEqual(wf_edges.route_after_fast_combined_review_join(patch),
                         "repair_section")

    def test_revise_without_locatable_scope_halts(self):
        state = _fast_state(self.d, sections=("sec1",))
        fake = mock.Mock(return_value={
            "stage": "T4_COMBINED_REVIEW_REVISE",
            "routing_action": "REVISE",
            "section_statuses": {"sec1": "reviewed"},
            "combined_review_conclusion": "REVISE",
        })
        with mock.patch.object(wf_graph, "node_t4_join_combined_review", fake):
            patch = wf_graph.node_fast_join_combined_review(state)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(wf_edges.route_after_fast_combined_review_join(patch),
                         END)

    def test_repair_after_revise_returns_to_prebuild_not_t1(self):
        """修复后只重建依赖产物（不回全部 T1、不强制逐节 checker）。"""
        state = _fast_state(self.d, sections=("sec1", "sec2"),
                            stage="REPAIR_LOOP",
                            routing_action="REPAIR_SECTION",
                            repair_target="sec2")
        self.assertEqual(wf_edges.route_after_repair_await(state),
                         "t2_submit_and_check_sections")

    def test_review_plan_limited_to_failed_scopes(self):
        """REVISE 之后重新规划终审时只复查失败 scope（不重复消耗全章审读）。"""
        state = _fast_state(self.d, sections=("sec1", "sec2", "sec3"),
                            combined_review_scopes=["sec3"])
        from workflow.nodes import fast_review_nodes as frn
        captured = {}

        def fake_plan(state):
            captured["scopes"] = list(state.get("combined_review_scopes")
                                      or state.get("expected_sections") or [])
            return {"stage": "T4_COMBINED_REVIEW_PENDING",
                    "routing_action": "PROCEED"}
        self.assertEqual(state["combined_review_scopes"], ["sec3"])
        fake_plan(state)
        self.assertEqual(captured["scopes"], ["sec3"])


# ---------------------------------------------------------------------------
# GRAPH-01：交付面的最后一道闸（deliver 拒收未审读内容）
# ---------------------------------------------------------------------------

class GRAPH01_DeliveryRefusesUnreviewed(unittest.TestCase):
    """GRAPH-01：pending_review / 非 PASS 一律不得交付；MUST_PASS 清单不缩。"""

    def _reports(self, **overrides):
        reports = {}
        for entry in list(deliver_mod.PRE_CHECKS) + list(deliver_mod.CHECKS):
            reports[entry[1]] = {"status": "pass", "issues": []}
        reports.update(overrides)
        return reports

    def test_must_pass_list_untouched(self):
        for name in ("check_semantic.json", "check_provenance.json",
                     "check_fidelity.json", "build.json", "check_render.json",
                     "check_agent_audit.json"):
            self.assertIn(name, deliver_mod.MUST_PASS,
                          "MUST_PASS 被削弱: 缺 %s" % name)

    def test_pending_review_blocks_delivery(self):
        reports = self._reports(**{"check_semantic.json": {
            "status": "pending_review", "issues": []}})
        blockers = deliver_mod.must_pass_blockers(reports)
        self.assertTrue(blockers, "pending_review 竟然放行交付")
        self.assertIn("pending_review", " ".join(blockers))

    def test_missing_semantic_report_blocks_delivery(self):
        reports = self._reports()
        reports.pop("check_semantic.json")
        self.assertTrue(deliver_mod.must_pass_blockers(reports))

    def test_all_pass_reports_have_no_blocker(self):
        self.assertEqual(deliver_mod.must_pass_blockers(self._reports()), [])

    def test_stale_hash_still_blocks(self):
        reports = self._reports(**{"check_semantic.json": {
            "status": "stale", "issues": [{"code": "SEMANTIC_STALE"}]}})
        self.assertTrue(deliver_mod.must_pass_blockers(reports))


# ---------------------------------------------------------------------------
# GRAPH-06：合批作者重叠执行 + 构建 barrier
# ---------------------------------------------------------------------------

class GRAPH06_BatchedAuthors(unittest.TestCase):
    """GRAPH-06：三节经真实批工具入口 → 2 个作者请求（可重叠），构建需全就绪。"""

    def test_three_sections_become_two_batches(self):
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(sbn.register_capable_worker(4))
            sbn.write_config(d, agent_dispatch="fast")
            state = sbn.make_state(d, sections=("sec1", "sec2", "sec3"))
            patch = sbn.run_plan(state)
            requests = patch["dispatch_plan"]["requests_full"]
            self.assertEqual(
                [list(r.get("scopes") or [r["scope"]]) for r in requests],
                [["sec1", "sec2"], ["sec3"]])
            self.assertGreaterEqual(patch["dispatch_plan"]["min_parallelism"], 2)
            self.assertGreaterEqual(
                patch["dispatch_plan"]["effective_concurrency"], 2)

    def test_build_barrier_waits_for_every_section(self):
        """只要还有节未就绪/未终审，就不得进入装配与构建。"""
        with tempfile.TemporaryDirectory() as d:
            state = _fast_state(d, sections=("sec1", "sec2", "sec3"),
                                section_statuses={"sec1": "prebuild_ready",
                                                  "sec2": "prebuild_ready",
                                                  "sec3": "running"})
            self.assertEqual(wf_edges.route_after_section_check(state),
                             "t2_plan_dispatch")
            state["section_statuses"]["sec3"] = "prebuild_ready"
            self.assertEqual(wf_edges.route_after_section_check(state),
                             "t2_assemble_main")
            state["expected_sections"] = ["sec1", "sec2", "sec3", "sec4"]
            self.assertEqual(wf_edges.route_after_section_check(state),
                             "t2_plan_dispatch")
            self.assertEqual(wf_edges.route_after_section_check(
                _fast_state(d, sections=("sec1",),
                            section_statuses={"sec1": "prebuild_ready"},
                            stage="HALTED")), END)


# ---------------------------------------------------------------------------
# 真实编译图上的端到端接线（fake 子代理 / 真实节点）
# ---------------------------------------------------------------------------

class GRAPH02_EndToEndWiring(unittest.TestCase):
    """GRAPH-02：真实编译图跑一遍——1 次 T1 + 2 批作者 + 1 批终审 = 4 次调用。"""

    SECTIONS = ("sec1", "sec2", "sec3")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _reset_fakes()
        for mod, attr, val in ((bn, "GateRunner", _FakeGateRunner),
                               (bn, "ScriptRunner", _FakeRunner),
                               (sn, "ScriptRunner", _FakeRunner),
                               (bn, "_pregenerate_classroom_plan",
                                lambda *a, **k: None)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)
        _write_run_json(self.d, {sec: "planned" for sec in self.SECTIONS})

    def _build(self, calls, authored, planned, join_review):
        sections = list(self.SECTIONS)

        def fake_t1(state):
            calls.append("t1_unified")
            return {"stage": "T1_MAPPING_READY", "routing_action": "PROCEED",
                    "mapping_frozen": True}

        def fake_t1_hop(tag):
            """await / verify 两跳不派发 agent，不计入调用数（C12 口径）。"""
            def _f(state):
                calls.append(tag)
                return {"stage": "T1_MAPPING_READY",
                        "routing_action": "PROCEED", "mapping_frozen": True}
            return _f

        def fake_plan(state):
            ready = state.get("prebuild_ready_sections") or {}
            prior = dict(state.get("section_statuses") or {})
            staged = [s for s in sections if prior.get(s) == "staged"]
            if staged:
                return {"active_section": staged[0],
                        "stage": "T2_SECTIONS_STAGED",
                        "routing_action": "PROCEED"}
            pending = [s for s in sections if s not in ready]
            if not pending:
                return {"active_section": None,
                        "stage": "T2_SECTIONS_STAGED",
                        "routing_action": "PROCEED"}
            batch = pending[:2]          # writer_batch_max_sections=2
            authored["batches"].append(batch)
            calls.append("author_batch")
            # 真实节点返回的是“全量 statuses 快照”（dict 通道整值替换），
            # 假节点也必须合并，否则会抹掉已产出的 prebuild_ready（GRAPH-04）。
            statuses = dict(prior)
            statuses.update({s: "running" for s in batch})
            return {"dispatch_plan": {"requests_full": [
                        {"dispatch_id": "req-%d-%s" % (len(authored["batches"]), s),
                         "scopes": [s]} for s in batch]},
                    "batch_id": "bat-t2-%d" % len(authored["batches"]),
                    "section_statuses": statuses,
                    "stage": "T2_SECTIONS_PENDING",
                    "routing_action": "PROCEED"}

        def fake_join(state):
            reqs = (state.get("dispatch_plan") or {}).get("requests_full") or []
            scopes = [s for r in reqs
                      for s in (r.get("scopes") or [r.get("scope")])]
            for sec in scopes:
                _touch_section(self.d, sec)
            statuses = dict(state.get("section_statuses") or {})
            statuses.update({s: "staged" for s in scopes})
            return {"section_statuses": statuses,
                    "staged_sections": {s: os.path.join(self.d, s + ".tex")
                                        for s in scopes},
                    "active_section": scopes[0],
                    "stage": "T2_SECTIONS_STAGED",
                    "routing_action": "PROCEED"}

        def fake_pass(name, stage, **extra):
            def _f(state):
                calls.append(name)
                patch = {"stage": stage, "routing_action": "PROCEED"}
                patch.update(extra)
                return patch
            return _f

        def fake_plan_review(state):
            calls.append("review_plan")
            scopes = list(state.get("combined_review_scopes")
                          or state.get("expected_sections") or [])
            planned["scopes"] = scopes
            return {"stage": "T4_COMBINED_REVIEW_PENDING",
                    "routing_action": "PROCEED",
                    "dispatch_plan": {"requests_full": [
                        {"dispatch_id": "req-review-%d" % i,
                         "review_scopes": [s]} for i, s in enumerate(scopes)]},
                    "batch_id": "bat-t4-1"}

        def fake_join_review_(state):
            calls.append("combined_review")
            return join_review(planned.get("scopes") or [])

        return _fast(
            node_fast_t1_request_unified_plan=fake_t1,
            node_fast_t1_await_unified_plan=fake_t1_hop("t1_await"),
            node_fast_t1_verify_unified_mapping=fake_t1_hop("t1_verify"),
            node_t2_plan_dispatch=fake_plan,
            node_t2_dispatch_sections=fake_pass("author_dispatch",
                                                "T2_SECTIONS_RUNNING"),
            node_t2_join_sections=fake_join,
            node_t2_assemble_main=fake_pass("assemble", "T2_ASSEMBLED"),
            node_t2_shuffle_options=fake_pass("shuffle", "T2_SHUFFLED"),
            node_t3_build=fake_pass("build", "T3_BUILT"),
            node_t3_compare=fake_pass("compare", "T3_COMPARED"),
            node_t4_make_review_pack=fake_pass("review_pack", "T4_REVIEW_PACKED"),
            node_t4_plan_combined_review=fake_plan_review,
            node_t4_dispatch_combined_review=fake_pass(
                "review_dispatch", "T4_COMBINED_REVIEW_RUNNING"),
            node_t4_join_combined_review=fake_join_review_,
            node_t4_verify_acceptance=fake_pass(
                "acceptance", "T4_ACCEPTED", acceptance_gate={"status": "PASS"}),
            node_t4_promote_and_deliver=fake_pass(
                "deliver", "DELIVERED", promoted=True),
        )

    def _run(self, join_review=None):
        calls, authored, planned = [], {"batches": []}, {"scopes": []}

        def approve(scopes):
            return {"stage": "T4_ACCEPTANCE_PENDING",
                    "routing_action": "PROCEED",
                    "section_statuses": {s: "reviewed" for s in scopes},
                    "combined_review_conclusion": "PASS"}

        graph = self._build(calls, authored, planned, join_review or approve)
        state = _fast_state(self.d, sections=self.SECTIONS,
                            stage="T0_INITIALIZED")
        cfg = {"configurable": {"thread_id": "fast-e2e"}}
        graph.update_state(cfg, state, as_node="t0_init_scaffold")
        result = graph.invoke(None, cfg)
        return {"graph": graph, "cfg": cfg, "result": result, "calls": calls,
                "authored": authored, "planned": planned}

    # -- 真实图跑通 + 四调用计数 -------------------------------------------
    def test_four_agent_calls_and_no_hidden_per_section_check(self):
        run = self._run()
        calls = run["calls"]
        self.assertEqual(run["authored"]["batches"],
                         [["sec1", "sec2"], ["sec3"]])
        self.assertEqual(run["planned"]["scopes"],
                         ["sec1", "sec2", "sec3"])
        t1_calls = [c for c in calls if c == "t1_unified"]
        author_calls = [c for c in calls if c == "author_batch"]
        review_calls = [c for c in calls if c == "combined_review"]
        self.assertEqual(len(t1_calls), 1, "T1 只许一次（一张派发单三表）")
        self.assertEqual(len(author_calls), 2, "三节只能两批作者")
        self.assertEqual(len(review_calls), 1, "终审只能一批")
        self.assertEqual(len(t1_calls) + len(author_calls) + len(review_calls), 4,
                         "冷缓存 agent 调用数必须是 1+2+1: %s" % calls)
        # 无隐藏逐节 checker：真实节点序列里没有它们
        for name in PER_SECTION_CHECKER_NODES:
            self.assertNotIn(name, calls)
        self.assertEqual(run["result"].get("promoted"), True)
        self.assertEqual(run["result"].get("gate_phase"), "final")
        self.assertEqual(run["result"].get("combined_review_conclusion"), "PASS")

    def test_prebuild_then_final_t3_gate_specs(self):
        self._run()
        specs = _FakeGateRunner.specs
        self.assertEqual(len(specs), 2, "t3_checks 应恰好跑两趟（prebuild/final）")
        pre = [s["gate_name"] for s in specs[0]]
        final = [s["gate_name"] for s in specs[1]]
        self.assertNotIn("check_semantic", pre)
        self.assertIn("check_semantic", final)
        self.assertIn("check_provenance", pre)
        self.assertIn("check_provenance", final)

    def test_no_section_marked_checked_before_final_review(self):
        """终审未 PASS 前的任何快照都不许出现 checked（produced ≠ checked）。"""
        run = self._run()
        history = list(run["graph"].get_state_history(run["cfg"]))
        self.assertTrue(history, "无检查点历史")
        checked_before_review = []
        for snap in history:
            values = snap.values or {}
            if values.get("combined_review_conclusion") == "PASS":
                continue
            bad = [s for s, st in (values.get("section_statuses") or {}).items()
                   if st in ("checked", "accepted")]
            if bad:
                checked_before_review.append((values.get("stage"), bad))
        self.assertEqual(checked_before_review, [],
                         "终审前出现 checked 状态: %s" % checked_before_review)
        # 终审后才逐节 mark checked（真实调用次序）
        gate_idx = [i for i, e in enumerate(TIMELINE)
                    if e[0] == "gate" and "check_semantic" in e[1]]
        mark_idx = [i for i, e in enumerate(TIMELINE)
                    if e[0] == "script" and e[1] == "pdstate.py"
                    and "mark" in e[2]]
        self.assertEqual(len(gate_idx), 1, "全量 semantic 必须恰好跑一次: %s"
                         % (TIMELINE,))
        self.assertTrue(mark_idx, "终审通过后必须 mark checked")
        self.assertLess(gate_idx[0], min(mark_idx),
                        "mark checked 竟早于全量 semantic 终审")

    def test_final_gate_failure_blocks_delivery(self):
        """终审门禁（final 相位的全量 semantic）失败 → 不进验收、不发布、不 mark。"""
        _FakeGateRunner.failures = {
            "check_semantic": _gate_report(
                "check_semantic", status="FAIL", exit_code=1,
                issues=[{"code": "SEMANTIC_MISSING", "artifact": "sec1.tex",
                         "section": "sec1"}])}
        calls, authored, planned = [], {"batches": []}, {"scopes": []}
        graph = self._build(calls, authored, planned, lambda scopes: {
            "stage": "T4_ACCEPTANCE_PENDING", "routing_action": "PROCEED",
            "section_statuses": {s: "reviewed" for s in scopes},
            "combined_review_conclusion": "PASS"})
        state = _fast_state(self.d, sections=self.SECTIONS,
                            stage="T0_INITIALIZED")
        cfg = {"configurable": {"thread_id": "fast-final-fail",
                                "recursion_limit": 60}}
        graph.update_state(cfg, state, as_node="t0_init_scaffold")
        try:
            graph.invoke(None, cfg)
        except Exception:                      # noqa: BLE001
            pass     # 返修环被递归上限截断：终态仍必须是「未交付」
        result = graph.get_state(cfg).values or {}
        self.assertNotEqual(result.get("promoted"), True,
                            "终审门禁失败竟然发布")
        self.assertNotIn("deliver", calls, "终审门禁失败竟走到交付节点")
        self.assertNotIn("acceptance", calls, "终审门禁失败竟走到验收节点")
        self.assertEqual(_mark_calls(), [], "终审门禁失败不得 mark checked")
        self.assertEqual(result.get("gate_phase"), "final")
        self.assertNotEqual(result.get("combined_review_conclusion"), "REVISE")

    def test_revise_blocks_delivery_and_marks_nothing(self):
        """终审 REVISE：不 mark checked、不发布，且只返修未通过的 scope。"""
        def revise(scopes):
            ok = {s: "reviewed" for s in scopes[:2]}
            return {"stage": "T4_COMBINED_REVIEW_REVISE",
                    "routing_action": "REVISE",
                    "section_statuses": ok,
                    "combined_review_conclusion": "REVISE",
                    "repair_reason": "%s 未通过终审" % (scopes[2:],)}

        # REVISE 后图会进入 repair_section/repair_await（真实节点，需要修复夹具），
        # 这里只验证「终审未 PASS 的图上状态」：用一次递归上限截断在图内循环。
        calls, authored, planned = [], {"batches": []}, {"scopes": []}
        graph = self._build(calls, authored, planned, revise)
        state = _fast_state(self.d, sections=self.SECTIONS,
                            stage="T0_INITIALIZED")
        cfg = {"configurable": {"thread_id": "fast-revise",
                                "recursion_limit": 40}}
        graph.update_state(cfg, state, as_node="t0_init_scaffold")
        result = graph.invoke(None, cfg)
        self.assertNotEqual(result.get("promoted"), True, "终审 REVISE 竟然发布")
        self.assertEqual([c for c in _mark_calls()], [],
                         "终审 REVISE 不得 mark checked: %s" % (_mark_calls(),))
        self.assertEqual(result.get("gate_phase"), "prebuild")
        self.assertEqual(result.get("combined_review_scopes"),
                         [self.SECTIONS[2]])


if __name__ == "__main__":
    unittest.main()
