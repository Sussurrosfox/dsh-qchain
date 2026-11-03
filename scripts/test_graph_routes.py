#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_graph_routes.py — 卡 05：真实编译图的路由完备性与零派发复用路径。

ROUTE-01  全部 scope 复用（零请求）→ 真实编译图跑到 t4_join_review，
          不出现 unknown target、不触发额外 agent 派发（t4_dispatch_review 零调用）。
ROUTE-02  每个条件边路由函数的全部返回值都在对应 path_map 内、path_map 目标
          都是真实节点、所有节点自 START 可达。

只使用 fake 节点 / 真实节点 + 假 ScriptRunner、MemorySaver；不启动真实流水线、
不派发真实 Agent、不读令牌。旧的纯边函数测试（test_t4_review_batch.py
test_t12_route_after_plan_zero_dispatch）保持不变。
"""

import ast
import inspect
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START  # noqa: E402

import test_t4_review_batch as tb  # noqa: E402
from workflow import edges as wf_edges  # noqa: E402
from workflow import graph as wf_graph  # noqa: E402
from workflow.nodes import t4_review_batch_nodes as t4nodes  # noqa: E402


def _compile(**node_overrides):
    """按需把 workflow.graph 模块名下的 node_* 换成 fake，再编译真实图。"""
    patches = [mock.patch.object(wf_graph, name, fake)
               for name, fake in node_overrides.items()]
    for p in patches:
        p.start()
    try:
        return wf_graph.create_shrink_courseware_graph(checkpointer=MemorySaver())
    finally:
        for p in patches:
            p.stop()


def _branch_specs(compiled):
    """(源节点, 路由函数名, BranchSpec) 列表——来自真实编译产物。"""
    out = []
    for src, branches in compiled.builder.branches.items():
        for name, spec in branches.items():
            out.append((src, name, spec))
    return out


def _literal_returns(func):
    """AST 枚举路由函数全部 return 值；任何非字面量/END 的返回都视为无法枚举。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    values, unresolved = set(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return):
            continue
        v = node.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            values.add(v.value)
        elif isinstance(v, ast.Name) and v.id == "END":
            values.add(END)
        else:
            unresolved.append(ast.dump(v) if v is not None else "None")
    return values, unresolved


class ROUTE02_RouteTargetsReachable(unittest.TestCase):
    """ROUTE-02：边函数返回值 ⊆ path_map ⊆ 真实节点，且全图可达。"""

    @classmethod
    def setUpClass(cls):
        cls.compiled = wf_graph.create_shrink_courseware_graph(
            checkpointer=MemorySaver())
        cls.specs = _branch_specs(cls.compiled)

    def test_there_are_conditional_edges_to_check(self):
        self.assertGreaterEqual(len(self.specs), 10)

    def test_every_route_return_value_is_in_path_map(self):
        problems = []
        for src, name, spec in self.specs:
            func = spec.path.func
            returns, unresolved = _literal_returns(func)
            if unresolved:
                problems.append(f"{src}:{name} 存在无法枚举的返回表达式 {unresolved}")
            missing = returns - set(spec.ends)
            if missing:
                problems.append(
                    f"{src}:{name} 返回 {sorted(missing)} 但 path_map 只有 "
                    f"{sorted(spec.ends)}（运行时将抛 unknown target）")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_path_map_target_is_a_real_node(self):
        nodes = set(self.compiled.builder.nodes) | {END}
        bad = [(src, k, v) for src, _n, spec in self.specs
               for k, v in spec.ends.items() if v not in nodes]
        self.assertEqual(bad, [])

    def test_t4_plan_review_path_map_has_join(self):
        """t4_plan_review 的 path_map 必含零派发直收目标 t4_join_review。"""
        (spec,) = [s for src, _n, s in self.specs if src == "t4_plan_review"]
        self.assertIn("t4_join_review", spec.ends)
        self.assertIn("t4_dispatch_review", spec.ends)
        self.assertIn(END, spec.ends)

    def test_every_node_reachable_from_start(self):
        b = self.compiled.builder
        succ = {}
        for a, c in b.edges:
            succ.setdefault(a, set()).add(c)
        for src, _n, spec in self.specs:
            succ.setdefault(src, set()).update(spec.ends.values())
        seen, todo = set(), [START]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(succ.get(cur, ()))
        unreachable = set(b.nodes) - seen
        self.assertEqual(unreachable, set(), f"不可达节点: {sorted(unreachable)}")


class _Counter:
    """记录 fake 节点调用次数与名称。"""

    def __init__(self):
        self.calls = []

    def node(self, name, patch):
        def _fake(state):
            self.calls.append(name)
            return dict(patch)
        _fake.__name__ = f"fake_{name}"
        return _fake


class ROUTE01_ZeroDispatchReuseOnRealGraph(unittest.TestCase):
    """ROUTE-01：零请求复用路径走真实编译图，直达 join。"""

    def _run(self, graph, state, thread="zero-dispatch"):
        cfg = {"configurable": {"thread_id": thread}}
        graph.update_state(cfg, state, as_node="t4_make_review_pack")
        return cfg, graph.invoke(None, cfg)

    def test_fake_nodes_zero_dispatch_reaches_join_without_extra_agent(self):
        cnt = _Counter()
        graph = _compile(
            node_t4_plan_review=cnt.node("t4_plan_review", {
                "stage": "T4_REVIEW_RUNNING", "dispatch_mode": "reused"}),
            node_t4_dispatch_review=cnt.node("t4_dispatch_review", {
                "stage": "T4_REVIEW_RUNNING"}),
            node_t4_join_review=cnt.node("t4_join_review", {
                "stage": "T4_ACCEPTANCE_PENDING", "routing_action": "PROCEED"}),
            node_t4_verify_acceptance=cnt.node("t4_verify_acceptance", {
                "stage": "T4_ACCEPTED", "routing_action": "PROCEED"}),
            node_t4_promote_and_deliver=cnt.node("t4_promote_and_deliver", {
                "stage": "DELIVERED"}),
        )
        _cfg, result = self._run(graph, {"stage": "T4_REVIEW_PACKED",
                                         "history": []})
        self.assertEqual(
            cnt.calls,
            ["t4_plan_review", "t4_join_review", "t4_verify_acceptance",
             "t4_promote_and_deliver"])
        self.assertNotIn("t4_dispatch_review", cnt.calls)  # 额外派发次数 = 0
        self.assertEqual(result["stage"], "DELIVERED")

    def test_real_plan_and_join_nodes_reuse_all_scopes_zero_requests(self):
        """真实 t4_plan_review / t4_join_review + 假 ScriptRunner：全 scope 已复用。"""
        cnt = _Counter()
        with tempfile.TemporaryDirectory() as d:
            tb.sbn._reset_pool()
            unreg = tb.register_capable_worker(8)
            try:
                sections = ("sec1", "sec2")
                self_dir = tb.make_self(d, sections)
                state = tb.base_state(self_dir, sections)
                chapter_digest = t4nodes._chapter_context_digest(
                    self_dir, list(sections))
                for sec in sections:
                    tb.write_scope_record(
                        self_dir, sec,
                        scope_digest=t4nodes._scope_content_digest(self_dir, sec),
                        chapter_digest=chapter_digest,
                        rule_version=t4nodes.REVIEW_RULE_VERSION,
                        # 任务16（C7）：复审规则 1.1 起，合法的「上一轮已 PASS」记录
                        # 必须带宏/模板语义绑定，否则一律按过期内容审读重派。
                        macro_digest=t4nodes._current_macro_digest(self_dir))

                def real_plan(s):
                    cnt.calls.append("t4_plan_review")
                    with mock.patch.object(
                            t4nodes, "ScriptRunner", tb.fake_runner("PASS")):
                        return t4nodes.node_t4_plan_review(s)

                def real_join(s):
                    cnt.calls.append("t4_join_review")
                    with mock.patch.object(t4nodes, "_live_pool",
                                           lambda *a, **k: None), \
                            mock.patch.object(
                                t4nodes, "interrupt",
                                lambda p: self.fail("零派发轮不得 interrupt")):
                        return t4nodes.node_t4_join_review(s)

                graph = _compile(
                    node_t4_plan_review=real_plan,
                    node_t4_dispatch_review=cnt.node(
                        "t4_dispatch_review", {"stage": "HALTED"}),
                    node_t4_join_review=real_join,
                    node_t4_verify_acceptance=cnt.node(
                        "t4_verify_acceptance", {"stage": "T4_ACCEPTED"}),
                    node_t4_promote_and_deliver=cnt.node(
                        "t4_promote_and_deliver", {"stage": "DELIVERED"}),
                )
                _cfg, result = self._run(graph, state, thread="real-reuse")
            finally:
                unreg()
                tb.sbn._reset_pool()
        self.assertNotIn("t4_dispatch_review", cnt.calls)
        self.assertEqual(
            cnt.calls[:2], ["t4_plan_review", "t4_join_review"])
        self.assertEqual(result["dispatch_metrics"]["submitted"], 0)
        self.assertEqual(result["dispatch_metrics"]["reused"], 2)
        self.assertEqual(result["stage"], "DELIVERED")

    def test_pending_dispatch_route_still_goes_through_dispatch_node(self):
        """回归保护：有请求（T4_REVIEW_PENDING）时仍必须经 dispatch 节点。"""
        cnt = _Counter()
        graph = _compile(
            node_t4_plan_review=cnt.node("t4_plan_review", {
                "stage": "T4_REVIEW_PENDING"}),
            node_t4_dispatch_review=cnt.node("t4_dispatch_review", {
                "stage": "T4_REVIEW_RUNNING"}),
            node_t4_join_review=cnt.node("t4_join_review", {
                "stage": "T4_ACCEPTANCE_PENDING"}),
            node_t4_verify_acceptance=cnt.node("t4_verify_acceptance", {
                "stage": "T4_ACCEPTED", "routing_action": "PROCEED"}),
            node_t4_promote_and_deliver=cnt.node("t4_promote_and_deliver", {
                "stage": "DELIVERED"}),
        )
        self._run(graph, {"stage": "T4_REVIEW_PACKED", "history": []},
                  thread="pending")
        self.assertEqual(
            cnt.calls,
            ["t4_plan_review", "t4_dispatch_review", "t4_join_review",
             "t4_verify_acceptance", "t4_promote_and_deliver"])


if __name__ == "__main__":
    unittest.main()
