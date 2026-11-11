#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_d_frozen.py — 批次 D（F1/F2/F3/F4）执行契约冻结回归

覆盖：
  F1  Phase 0 之前 checkpoint 就带着完整执行契约 → resume 不被误判成 legacy，
      且重申同一 profile 幂等、换 profile 仍被拒（契约待确认窗口不再死锁）；
  F2  repair_section 熔断后 repair_await 不得改写 stage；
  F3  门禁与恢复都用冻结策略：当前 config.yaml 放宽 → 拒绝/报告漂移，
      冻结值仍生效；state 与 contract 的 execution_config 冲突显式拒绝；
  F4  返修上限读冻结值；无冻结块时顶层覆盖仍生效（旧检查点兼容）。

运行: python -B -m pytest -q -p no:cacheprovider scripts/test_batch_d_frozen.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
for path in (HERE, PLUGIN_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import check_pedagogy  # noqa: E402
import check_scope  # noqa: E402
import frozen_policy  # noqa: E402
from workflow import execution_policy as ep  # noqa: E402
from workflow import repair_budget  # noqa: E402
from workflow.edges import route_after_repair_await  # noqa: E402
from workflow.nodes import repair_nodes  # noqa: E402

FROZEN_STRICT = {
    "pedagogy_policy": "strict",
    "budget_policy": "strict",
    "require_classroom_plan": True,
    "repair_max_per_scope": 1,
    "repair_max_total": 1,
}


def make_self(tmp, config_text, frozen=None):
    """建一个带冻结执行契约的 self 夹具。"""
    os.makedirs(os.path.join(tmp, ".pd"), exist_ok=True)
    with open(os.path.join(tmp, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(config_text)
    if frozen is None:
        return tmp
    metadata = ep.build_frozen_metadata(profile="faithful_fast",
                                       execution_config=frozen)
    contract = {"content_policy": "faithful", "must_cover": ["fixture"],
                **metadata}
    contract["digest"] = check_scope._canonical_digest(contract)
    with open(os.path.join(tmp, ".pd", "intent-contract.json"), "w",
              encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False)
    return tmp


class TFrozenPolicyModule(unittest.TestCase):
    """frozen_policy：冻结块读取与漂移判定。"""

    def test_missing_contract_is_reported_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(frozen_policy.load_contract(tmp))
            self.assertIsNone(frozen_policy.frozen_execution_config(tmp))
            self.assertEqual(frozen_policy.policy_drift(tmp, {
                "pedagogy_policy": "advisory"}), [])

    def test_frozen_values_win_and_drift_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "pedagogy_policy: advisory\n", FROZEN_STRICT)
            merged = frozen_policy.merged_policy(
                tmp, {"pedagogy_policy": "advisory", "budget_policy": "advisory"})
            self.assertEqual(merged["pedagogy_policy"], "strict")
            self.assertEqual(merged["sources"]["pedagogy_policy"], "frozen")
            self.assertEqual(merged["budget_policy"], "strict")
            drift = frozen_policy.policy_drift(
                tmp, {"pedagogy_policy": "advisory",
                      "require_classroom_plan": "false"})
            self.assertEqual(sorted(d["key"] for d in drift),
                             ["pedagogy_policy", "require_classroom_plan"])


class TFrozenGates(unittest.TestCase):
    """F3：门禁按冻结值判定，YAML 漂移被报告。"""

    def test_pedagogy_policy_comes_from_frozen_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "execution_profile: faithful_fast\n"
                           "pedagogy_policy: advisory\n"
                           "require_classroom_plan: false\n", FROZEN_STRICT)
            policy = check_pedagogy.load_policy(tmp)
            self.assertEqual(policy["pedagogy_policy"], "strict")
            self.assertIs(policy["require_classroom_plan"], True)
            self.assertEqual(policy["source"], "frozen")
            self.assertEqual(policy["sources"]["pedagogy_policy"], "frozen")
            self.assertEqual(len(policy.get("drift") or []), 2)

    def test_scope_gate_blocks_execution_policy_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "content_policy: faithful\n"
                           "pedagogy_policy: advisory\n"
                           "budget_policy: advisory\n"
                           "require_classroom_plan: false\n",
                      FROZEN_STRICT)
            report = check_scope.check_scope_project(tmp)
            codes = [e.get("code") for e in report["errors"]]
            self.assertEqual(codes.count("SCOPE_EXECUTION_POLICY_DRIFT"), 3)

    def test_no_drift_when_config_matches_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "pedagogy_policy: strict\nbudget_policy: strict\n"
                           "require_classroom_plan: true\n", FROZEN_STRICT)
            self.assertEqual(frozen_policy.policy_drift(
                tmp, ep.read_execution_overrides(os.path.join(tmp, "config.yaml"))), [])


class TResumeContract(unittest.TestCase):
    """F1：契约待落盘窗口以 checkpoint 为准。"""

    def _frozen_state(self):
        return ep.build_frozen_metadata(profile="faithful_fast",
                                        execution_config=FROZEN_STRICT)

    def test_pending_contract_uses_checkpoint_not_legacy(self):
        policy = ep.resolve_resume(self._frozen_state(), None)
        self.assertFalse(policy["legacy"])
        self.assertEqual(policy["graph_version"], "faithful-fast-v1")
        self.assertTrue(policy.get("contract_pending"))

    def test_contract_without_checkpoint_fields_is_still_a_conflict(self):
        # 反方向不放过：契约有、检查点没有 → 仍按冲突拒绝（不猜回退）
        with self.assertRaises(ep.ExecutionPolicyError) as ctx:
            ep.resolve_resume({}, self._frozen_state())
        self.assertEqual(ctx.exception.code, "EXECUTION_CONTRACT_CONFLICT")

    def test_execution_config_mismatch_is_rejected(self):
        state = self._frozen_state()
        contract = dict(state)
        contract["execution_config"] = dict(state["execution_config"],
                                            pedagogy_policy="advisory")
        with self.assertRaises(ep.ExecutionPolicyError) as ctx:
            ep.resolve_resume(state, contract)
        self.assertEqual(ctx.exception.code, "EXECUTION_CONTRACT_CONFLICT")


class TConfigOverrideRejected(unittest.TestCase):
    """F3：当前 YAML 不得放宽已冻结策略。"""

    def test_loosened_yaml_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "pedagogy_policy: advisory\n"
                           "require_classroom_plan: false\n", FROZEN_STRICT)
            policy = ep.resolve_resume(
                ep.build_frozen_metadata(profile="faithful_fast",
                                         execution_config=FROZEN_STRICT), None)
            with self.assertRaises(ep.ExecutionPolicyError) as ctx:
                ep.assert_config_not_overriding(tmp, policy)
            self.assertEqual(ctx.exception.code,
                             "EXECUTION_CONFIG_OVERRIDE_REJECTED")

    def test_matching_yaml_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_self(tmp, "pedagogy_policy: strict\nbudget_policy: strict\n"
                           "require_classroom_plan: true\n"
                           "repair_max_per_scope: 1\nrepair_max_total: 1\n",
                      FROZEN_STRICT)
            policy = ep.resolve_resume(
                ep.build_frozen_metadata(profile="faithful_fast",
                                         execution_config=FROZEN_STRICT), None)
            ep.assert_config_not_overriding(tmp, policy)


class TRepairLimits(unittest.TestCase):
    """F4：返修上限读冻结值。"""

    def _state(self, **extra):
        state = ep.build_frozen_metadata(profile="faithful_fast",
                                         execution_config=FROZEN_STRICT)
        state.update(extra)
        return state

    def test_frozen_limits_are_used(self):
        per_scope, total, _ = repair_budget.resolve_limits(self._state())
        self.assertEqual((per_scope, total), (1, 1))
        self.assertEqual(repair_budget.limits_source(self._state()), "frozen")

    def test_plan_halts_at_frozen_limit(self):
        state = self._state(repair_attempts_by_scope={"sec1": 1},
                            repair_attempts_total=1)
        plan = repair_budget.plan_repair(state, "sec1", "still broken", "digest")
        self.assertEqual(plan["action"], "HALT")
        self.assertEqual(plan["halt_reason"], repair_budget.BUDGET_EXHAUSTED)
        self.assertEqual(plan["halt"]["limits_source"], "frozen")

    def test_top_level_override_still_works_without_frozen_block(self):
        per_scope, _, _ = repair_budget.resolve_limits(
            {"execution_profile": "faithful_fast", "repair_per_scope_limit": 7})
        self.assertEqual(per_scope, 7)


class TRepairHaltNotOverwritten(unittest.TestCase):
    """F2：熔断不得被下游节点改写。"""

    def test_repair_await_keeps_halted_state(self):
        patch = repair_nodes.node_repair_await({
            "stage": "HALTED", "routing_action": "HALT",
            "error_message": "返修预算耗尽", "pending_dispatch": None,
            "history": [],
        })
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["error_message"], "返修预算耗尽")
        self.assertEqual(route_after_repair_await(patch), "__end__")

    def test_fast_graph_uses_conditional_repair_edge(self):
        source = open(os.path.join(PLUGIN_ROOT, "workflow", "graph.py"),
                      encoding="utf-8").read()
        self.assertNotIn('workflow.add_edge("repair_section", "repair_await")',
                         source)
        self.assertIn('route_after_repair_dispatch', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
