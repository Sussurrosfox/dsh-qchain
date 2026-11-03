#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_repair_budget.py — 卡 06：内容返修的持久上限与无进展熔断（DATA-CONTRACTS C3）。

REPAIR-01  连续失败最多执行约定修复次数（legacy 每 scope 3、fast 每 scope 2/全局 6），
           dispatch 返回 ok 不再把计数 1→0；全局跨 scope 累计。
REPAIR-02  同一产物摘要 + 同一问题指纹第二次检测到 → HALT(no_progress)。
REPAIR-03  中断恢复保留次数；某节门禁成功只解除“未解决标记”，不清别节/全局累计。
REPAIR-04  运输 attempt / 覆盖 replan 与内容修复计数互相独立；内容失败不在同 prompt 重试。

只使用临时目录、mock 的 run_dispatch、假 GateRunner、MemorySaver；不启动真实流水线、
不派发真实 Agent、不读令牌。
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

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from workflow import graph as wf_graph  # noqa: E402
from workflow import repair_budget as rb  # noqa: E402
from workflow.nodes import repair_nodes  # noqa: E402

import test_issue_routing as tir  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _make_dispatch(self_dir, calls, edit=True, result=None):
    """mock run_dispatch：记录 dispatch_id；edit=True 时模拟 agent 真改了产物。"""
    def _rd(state, req, *, success_stage, success_history):
        calls.append(req["dispatch_id"])
        if edit:
            for rel in req["artifacts"]:
                with open(os.path.join(self_dir, rel), "w", encoding="utf-8") as f:
                    f.write(f"edited-by-repair-{len(calls)}\n")
        if result is not None:
            return copy.deepcopy(result)
        return {"stage": success_stage,
                "history": list(state.get("history", [])) + [success_history]}
    return _rd


class LoopBase(unittest.TestCase):
    """手工驱动 gate 失败 → repair_section → repair_await 回路（与图边语义一致）。"""

    sections = ("sec1", "sec2", "sec3", "sec4")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        for sec in self.sections:
            with open(os.path.join(self.d, f"{sec}.tex"), "w", encoding="utf-8") as f:
                f.write(f"original {sec}\n")
        self.calls = []
        self.state = {"self_dir": self.d, "history": [], "run_id": "r1",
                      "expected_sections": list(self.sections)}

    def dispatch_patch(self, **kw):
        p = mock.patch.object(repair_nodes, "run_dispatch",
                              _make_dispatch(self.d, self.calls, **kw))
        p.start()
        self.addCleanup(p.stop)

    def round(self, scope, reason, state=None):
        """一轮：gate 失败 → 安排返修 → 派发 ok。返回 ('HALT'|'OK', patch)。"""
        st = self.state if state is None else state
        st.update({"routing_action": "REPAIR_SECTION", "repair_target": scope,
                   "repair_reason": reason, "stage": "REPAIR_LOOP"})
        p1 = repair_nodes.node_repair_section(st)
        st.update(p1)
        if p1.get("stage") == "HALTED":
            return "HALT", p1
        p2 = repair_nodes.node_repair_await(st)
        st.update(p2)
        return ("HALT" if p2.get("stage") == "HALTED" else "OK"), p2


# ---------------------------------------------------------------------------
# REPAIR-01
# ---------------------------------------------------------------------------

class REPAIR01_BoundedRepairs(LoopBase):

    def test_legacy_six_consecutive_gate_failures_stop_after_three_repairs(self):
        self.dispatch_patch()
        outcomes = []
        for i in range(6):
            outcomes.append(self.round("sec1", f"gate failure variant {i}")[0])
            if outcomes[-1] == "HALT":
                break
        self.assertEqual(outcomes, ["OK", "OK", "OK", "HALT"],
                         "legacy 每 scope 3 次：第 4 次必须熔断，而不是 dispatch ok 后 1→0 无限循环")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 3})
        self.assertEqual(self.state["repair_attempts_total"], 3)
        self.assertEqual(self.state["routing_action"], "HALT")
        self.assertEqual(self.state["repair_halt"]["halt_reason"], rb.BUDGET_EXHAUSTED)

    def test_dispatch_ok_never_clears_counters_or_retry_count(self):
        self.dispatch_patch()
        for i in range(2):
            self.assertEqual(self.round("sec1", f"variant {i}")[0], "OK")
            self.assertEqual(self.state["retry_count"], i + 1,
                             "dispatch 返回 ok 不得把 retry_count 清成 0")
            self.assertEqual(self.state["repair_attempts_by_scope"]["sec1"], i + 1)

    def test_fast_profile_two_per_scope(self):
        self.dispatch_patch()
        self.state["execution_profile"] = rb.FAST_PROFILE
        outcomes = [self.round("sec1", f"variant {i}")[0] for i in range(3)]
        self.assertEqual(outcomes, ["OK", "OK", "HALT"])
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 2})
        self.assertEqual(self.state["repair_halt"]["per_scope_limit"], 2)

    def test_fast_profile_global_total_of_six_accumulates_across_scopes(self):
        self.dispatch_patch()
        self.state["execution_profile"] = rb.FAST_PROFILE
        for sec in ("sec1", "sec2", "sec3"):
            for i in range(2):
                self.assertEqual(self.round(sec, f"{sec} variant {i}")[0], "OK")
        self.assertEqual(self.state["repair_attempts_total"], 6)
        verdict, patch = self.round("sec4", "brand new scope but global budget is gone")
        self.assertEqual(verdict, "HALT")
        self.assertEqual(self.state["repair_halt"]["halt_reason"], rb.BUDGET_EXHAUSTED)
        self.assertEqual(self.state["repair_halt"]["total_limit"], 6)
        self.assertEqual(self.state["repair_halt"]["total_attempts"], 6)
        self.assertNotIn("sec4", self.state["repair_attempts_by_scope"],
                         "被拒的返修不得增加计数")
        self.assertEqual(len(self.calls), 6)

    def test_legacy_has_no_global_cap_but_each_scope_is_bounded(self):
        self.dispatch_patch()
        for sec in ("sec1", "sec2", "sec3", "sec4"):
            for i in range(3):
                self.assertEqual(self.round(sec, f"{sec} variant {i}")[0], "OK")
        self.assertEqual(self.state["repair_attempts_total"], 12)
        self.assertEqual(self.round("sec1", "again")[0], "HALT")

    def test_halt_carries_unresolved_issue_last_artifact_and_attempt_sources(self):
        self.dispatch_patch()
        for i in range(4):
            if self.round("sec1", f"unresolved formula issue {i}")[0] == "HALT":
                break
        halt = self.state["repair_halt"]
        self.assertEqual(halt["scope"], "sec1")
        self.assertIn("unresolved formula issue 3", halt["unresolved_reason"])
        self.assertEqual(halt["last_artifact"]["path"], "sec1.tex")
        self.assertTrue(halt["last_artifact"]["digest"])
        self.assertEqual([a["dispatch_id"] for a in halt["attempts"]],
                         self.calls, "attempt 来源 = 各次返修派发单 id")
        self.assertIn("返修预算耗尽", self.state["error_message"])

    def test_real_compiled_graph_six_failures_stops_at_budget(self):
        """真实编译图 + 真实 repair 节点 + 假 gate：六次连续失败实际停在约定次数。"""
        dispatches = []
        gate_calls = []

        def fake_checks(state):
            gate_calls.append(1)
            return {"stage": "REPAIR_LOOP", "routing_action": "REPAIR_SECTION",
                    "repair_target": "sec1",
                    "repair_reason": f"gate failure round {len(gate_calls)}"}

        def fake_submit(state):
            return fake_checks(state)

        with mock.patch.object(repair_nodes, "run_dispatch",
                               _make_dispatch(self.d, dispatches)):
            graph = _compile_with(
                node_t3_checks=fake_checks,
                node_t2_submit_and_check_sections=fake_submit)
            cfg = {"configurable": {"thread_id": "six"}, "recursion_limit": 200}
            graph.update_state(cfg, dict(self.state), as_node="t2_shuffle_options")
            result = graph.invoke(None, cfg)
        self.assertEqual(len(dispatches), 3)
        self.assertEqual(result["stage"], "HALTED")
        self.assertEqual(result["repair_attempts_by_scope"], {"sec1": 3})
        self.assertEqual(result["repair_halt"]["halt_reason"], rb.BUDGET_EXHAUSTED)


def _compile_with(**node_overrides):
    patches = [mock.patch.object(wf_graph, name, fake)
               for name, fake in node_overrides.items()]
    for p in patches:
        p.start()
    try:
        return wf_graph.create_shrink_courseware_graph(checkpointer=MemorySaver())
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# REPAIR-02
# ---------------------------------------------------------------------------

class REPAIR02_NoProgressHalts(LoopBase):

    def test_same_artifact_and_same_issue_second_time_halts_no_progress(self):
        self.dispatch_patch(edit=False)  # 派发“成功”但产物没变
        self.state["repair_per_scope_limit"] = 9  # 额度充足：只能是无进展熔断
        self.assertEqual(self.round("sec1", "公式 $x^2$ 缺失 qid=Q3")[0], "OK")
        verdict, _ = self.round("sec1", "公式 $x^2$ 缺失 qid=Q3")
        self.assertEqual(verdict, "HALT")
        halt = self.state["repair_halt"]
        self.assertEqual(halt["halt_reason"], rb.NO_PROGRESS)
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 1},
                         "无进展 HALT 不再增加计数")
        self.assertIn("无进展", self.state["error_message"])
        self.assertEqual(len(self.calls), 1, "第二次不得再派发")

    def test_noise_only_difference_is_still_same_issue(self):
        """时间戳 / 派发 id / 轮次只是噪声：仍算同一问题。"""
        self.dispatch_patch(edit=False)
        self.state["repair_per_scope_limit"] = 9
        self.round("sec1", "审读未通过 2026-07-01T10:00:00Z parent=dsp-t2-sec1-0123456789 第1次")
        verdict, _ = self.round(
            "sec1", "审读未通过 2026-07-02T23:59:59+08:00 parent=dsp-t2-sec1-abcdef0123 第2次")
        self.assertEqual(verdict, "HALT")
        self.assertEqual(self.state["repair_halt"]["halt_reason"], rb.NO_PROGRESS)

    def test_formula_or_qid_difference_is_a_different_issue(self):
        self.dispatch_patch(edit=False)
        self.state["repair_per_scope_limit"] = 9
        self.assertEqual(self.round("sec1", "公式 $x^2$ 错误 qid=Q3")[0], "OK")
        self.assertEqual(self.round("sec1", "公式 $x^3$ 错误 qid=Q3")[0], "OK")
        self.assertEqual(self.round("sec1", "公式 $x^3$ 错误 qid=Q4")[0], "OK")
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 3})

    def test_same_issue_but_artifact_changed_keeps_going_until_budget(self):
        self.dispatch_patch(edit=True)
        self.state["repair_per_scope_limit"] = 9
        for _ in range(4):
            self.assertEqual(self.round("sec1", "同一个问题持续存在")[0], "OK")
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 4})

    def test_different_issue_same_artifact_is_not_no_progress(self):
        self.dispatch_patch(edit=False)
        self.state["repair_per_scope_limit"] = 9
        self.assertEqual(self.round("sec1", "问题 A")[0], "OK")
        self.assertEqual(self.round("sec1", "问题 B")[0], "OK")

    def test_no_progress_is_per_scope(self):
        self.dispatch_patch(edit=False)
        self.state["repair_per_scope_limit"] = 9
        self.assertEqual(self.round("sec1", "同一个问题")[0], "OK")
        self.assertEqual(self.round("sec2", "同一个问题")[0], "OK")

    def test_structured_issues_fingerprint_is_order_insensitive_and_keeps_qid(self):
        a = [{"code": "FIDELITY_ASSET_LOST", "scope": "sec1", "qid": "Q1",
              "evidence": {"raw": "图丢失", "timestamp": "2026-01-01T00:00:00Z"}},
             {"code": "PEDAGOGY_X", "scope": "sec1", "qid": "Q2", "evidence": "x"}]
        b = [dict(a[1]), dict(a[0], evidence={"raw": "图丢失", "timestamp": "2027-05-05T05:05:05Z"})]
        self.assertEqual(rb.issue_fingerprint("sec1", a), rb.issue_fingerprint("sec1", b))
        c = [dict(a[0], qid="Q9"), a[1]]
        self.assertNotEqual(rb.issue_fingerprint("sec1", a), rb.issue_fingerprint("sec1", c))
        self.assertNotEqual(
            rb.issue_fingerprint("sec1", None, "公式 $a+b$"),
            rb.issue_fingerprint("sec1", None, "公式 $a-b$"))


# ---------------------------------------------------------------------------
# REPAIR-03
# ---------------------------------------------------------------------------

class REPAIR03_PersistenceAndResume(LoopBase):

    def test_state_json_roundtrip_keeps_counts_and_halts_at_same_point(self):
        self.dispatch_patch()
        self.round("sec1", "variant 0")
        self.round("sec1", "variant 1")
        # 进程重启：状态只剩 JSON（checkpoint 序列化后的纯数据）
        restored = json.loads(json.dumps(self.state, ensure_ascii=False))
        self.assertEqual(restored["repair_attempts_by_scope"], {"sec1": 2})
        self.assertEqual(restored["repair_attempts_total"], 2)
        self.assertEqual(self.round("sec1", "variant 2", state=restored)[0], "OK")
        self.assertEqual(self.round("sec1", "variant 3", state=restored)[0], "HALT")
        self.assertEqual(restored["repair_attempts_by_scope"], {"sec1": 3})

    def test_counters_survive_a_crash_and_resume_from_memory_saver(self):
        """真实编译图：第 3 次 gate 时崩溃；新编译图同 checkpoint 续跑，次数不消失。"""
        dispatches = []
        gate_calls = []

        def crashing_checks(state):
            gate_calls.append(1)
            if len(gate_calls) == 3:
                raise RuntimeError("simulated crash")
            return {"stage": "REPAIR_LOOP", "routing_action": "REPAIR_SECTION",
                    "repair_target": "sec1",
                    "repair_reason": f"gate failure round {len(gate_calls)}"}

        saver = MemorySaver()
        cfg = {"configurable": {"thread_id": "resume"}, "recursion_limit": 200}
        with mock.patch.object(repair_nodes, "run_dispatch",
                               _make_dispatch(self.d, dispatches)):
            patches = [mock.patch.object(wf_graph, "node_t3_checks", crashing_checks),
                       mock.patch.object(wf_graph, "node_t2_submit_and_check_sections",
                                         crashing_checks)]
            for p in patches:
                p.start()
            try:
                g1 = wf_graph.create_shrink_courseware_graph(checkpointer=saver)
            finally:
                for p in patches:
                    p.stop()
            g1.update_state(cfg, dict(self.state), as_node="t2_shuffle_options")
            with self.assertRaises(RuntimeError):
                g1.invoke(None, cfg)
            before = g1.get_state(cfg).values
            self.assertEqual(before["repair_attempts_by_scope"], {"sec1": 2})
            self.assertEqual(before["repair_attempts_total"], 2)
            self.assertEqual(before["retry_count"], 2)

            # “新进程”：全新编译图，同一份 checkpoint
            def always_fail(state):
                gate_calls.append(1)
                return {"stage": "REPAIR_LOOP", "routing_action": "REPAIR_SECTION",
                        "repair_target": "sec1",
                        "repair_reason": f"gate failure round {len(gate_calls)}"}
            patches = [mock.patch.object(wf_graph, "node_t3_checks", always_fail),
                       mock.patch.object(wf_graph, "node_t2_submit_and_check_sections",
                                         always_fail)]
            for p in patches:
                p.start()
            try:
                g2 = wf_graph.create_shrink_courseware_graph(checkpointer=saver)
            finally:
                for p in patches:
                    p.stop()
            result = g2.invoke(None, cfg)
        self.assertEqual(len(dispatches), 3, "崩溃前 2 次 + 恢复后仅再 1 次 = legacy 上限 3")
        self.assertEqual(result["stage"], "HALTED")
        self.assertEqual(result["repair_attempts_by_scope"], {"sec1": 3})
        self.assertEqual(result["repair_halt"]["halt_reason"], rb.BUDGET_EXHAUSTED)

    def test_dispatch_ok_does_not_clear_unresolved_marker(self):
        self.dispatch_patch()
        self.round("sec1", "variant 0")
        self.assertIn("sec1", self.state["repair_last_fingerprint_by_scope"])
        self.assertIn("sec1", self.state["repair_last_digest_by_scope"])
        res = repair_nodes.node_repair_await(self.state)
        for key in ("repair_attempts_by_scope", "repair_attempts_total",
                    "repair_last_fingerprint_by_scope", "repair_last_digest_by_scope"):
            self.assertNotIn(key, res, f"await 节点不得改写 {key}")

    def test_gate_pass_clears_only_unresolved_markers_never_counters(self):
        self.dispatch_patch()
        self.round("sec1", "variant 0")
        self.round("sec2", "variant 0")
        patch = rb.resolved_patch(self.state, ["sec1"])
        self.assertNotIn("sec1", patch["repair_last_fingerprint_by_scope"])
        self.assertIn("sec2", patch["repair_last_fingerprint_by_scope"],
                      "某节成功不清别节的未解决标记")
        for key in ("repair_attempts_by_scope", "repair_attempts_total",
                    "repair_attempt_log"):
            self.assertNotIn(key, patch, f"gate PASS 不得重置计数 {key}")
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 1, "sec2": 1})
        self.assertEqual(self.state["repair_attempts_total"], 2)

    def test_resolved_marker_lets_later_identical_failure_be_scheduled_again(self):
        """门禁真正 PASS 后同一问题复发 = 新的未解决状态，不会被旧标记误判无进展。"""
        self.dispatch_patch(edit=False)
        self.state["repair_per_scope_limit"] = 9
        self.round("sec1", "同一个问题")
        self.state.update(rb.resolved_patch(self.state, None))
        self.assertEqual(self.round("sec1", "同一个问题")[0], "OK")
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 2},
                         "清标记不清计数")

    def test_t3_checks_all_pass_clears_markers_but_keeps_counters(self):
        """真实 node_t3_checks 全部 PASS 的返回：只解除标记，计数不动。"""
        class Base(tir.NodeBase):
            def runTest(self):
                pass
        base = Base()
        base.setUp()
        try:
            base.state.update({
                "repair_attempts_by_scope": {"sec1": 2},
                "repair_attempts_total": 2,
                "repair_last_fingerprint_by_scope": {"sec1": "fp"},
                "repair_last_digest_by_scope": {"sec1": "dg"},
            })
            patch = base.checks()
        finally:
            base.doCleanups()
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["repair_last_fingerprint_by_scope"], {})
        self.assertEqual(patch["repair_last_digest_by_scope"], {})
        self.assertNotIn("repair_attempts_by_scope", patch)
        self.assertNotIn("repair_attempts_total", patch)

    def test_old_state_without_new_keys_does_not_crash(self):
        """旧 checkpoint 缺全部新键：按空/0 处理，不抛 KeyError。"""
        self.dispatch_patch()
        legacy_state = {"self_dir": self.d, "history": [], "retry_count": 1,
                        "repair_target": "sec1", "repair_reason": "old"}
        p = repair_nodes.node_repair_section(legacy_state)
        self.assertEqual(p["repair_attempts_by_scope"], {"sec1": 1})
        self.assertEqual(p["repair_attempts_total"], 1)


# ---------------------------------------------------------------------------
# REPAIR-04
# ---------------------------------------------------------------------------

class REPAIR04_TransportReplanIndependent(LoopBase):

    def test_content_failure_is_not_retried_with_same_prompt(self):
        """内容失败（产物无变化）→ 派发层返回 HALTED：await 只调一次 run_dispatch。"""
        failure = {"stage": "HALTED", "routing_action": "HALT",
                   "error_message": "派发 dsp-x 的产物未发生变化: sec1.tex"}
        self.dispatch_patch(edit=False, result=failure)
        self.state.update({"repair_target": "sec1", "repair_reason": "content problem",
                           "routing_action": "REPAIR_SECTION"})
        self.state.update(repair_nodes.node_repair_section(self.state))
        counters = {k: copy.deepcopy(self.state[k]) for k in (
            "repair_attempts_by_scope", "repair_attempts_total", "retry_count")}
        res = repair_nodes.node_repair_await(self.state)
        self.assertEqual(len(self.calls), 1, "内容错误不得在同 prompt 上重试")
        self.assertEqual(res["stage"], "HALTED")
        for key, val in counters.items():
            self.assertEqual(res.get(key, val), val, f"await 失败不得改写 {key}")

    def test_transport_attempts_do_not_inflate_content_repair_counter(self):
        """请求里的运输 attempt/max_retries 与内容修复计数是两套字段。"""
        self.dispatch_patch()
        self.state.update({"repair_target": "sec1", "repair_reason": "r",
                           "routing_action": "REPAIR_SECTION"})
        p = repair_nodes.node_repair_section(self.state)
        req = p["pending_dispatch"]
        self.assertEqual(req["attempt"], 0)
        self.state.update(p)
        # 模拟 dispatcher 对该派发单做了 2 次运输重试（只动 request.attempt）
        self.state["pending_dispatch"] = dict(req, attempt=2)
        res = repair_nodes.node_repair_await(self.state)
        self.state.update(res)
        self.assertEqual(self.state["repair_attempts_by_scope"], {"sec1": 1})
        self.assertEqual(self.state["repair_attempts_total"], 1)

    def test_replan_count_is_independent_both_ways(self):
        self.dispatch_patch()
        self.state["replan_count"] = 3  # 已到 replan 上限
        self.assertEqual(self.round("sec1", "variant 0")[0], "OK",
                         "replan 额度耗尽不影响内容返修")
        self.assertEqual(self.state["replan_count"], 3, "返修不改 replan 计数")
        for i in range(1, 4):
            self.round("sec1", f"variant {i}")
        self.assertEqual(self.state["replan_count"], 3, "返修熔断也不改 replan 计数")

    def test_rejected_repair_does_not_increment_any_counter(self):
        self.dispatch_patch()
        for i in range(3):
            self.round("sec1", f"variant {i}")
        snap = (copy.deepcopy(self.state["repair_attempts_by_scope"]),
                self.state["repair_attempts_total"], self.state["retry_count"])
        verdict, _ = self.round("sec1", "variant 3")
        self.assertEqual(verdict, "HALT")
        self.assertEqual((self.state["repair_attempts_by_scope"],
                          self.state["repair_attempts_total"],
                          self.state["retry_count"]), snap)


if __name__ == "__main__":
    unittest.main()
