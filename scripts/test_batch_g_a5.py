#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_g_a5.py — A5：返修上限的正式运维通道（config.yaml）。

A5 原文（bug.md）::

    workflow/repair_budget.py:124-191：scope_attempts>=per_scope 即
    repair_budget_exhausted HALTED，且计数永不重置。sec2/sec6/sec8 多次返修即熔断。
    规避：checkpoint blob 注入 repair_per_scope_limit=999001（必须 msgpack 编码
    b'\xce\x00\x0f>Y'，写 ASCII 会损坏状态；必须 int，str 会崩 apply_writes）。

即「想放宽上限」在旧实现里没有正式通道，只能拿 msgpack 二进制改 checkpoint——
一个写错就损坏整个会话状态的逆向工程动作。本测试锁定新通道：

  A5-1  ``self/config.yaml`` 扁平键 ``repair_per_scope_limit`` /
         ``repair_total_limit`` 被 ``load_repair_overrides`` 读到，并让
         ``plan_repair`` 按它放行（不再熔断）。
  A5-2  同一组键写在 ``agent_dispatch:`` 子块里等价生效。
  A5-3  优先级：冻结 execution_config > state 显式覆盖 > config.yaml >
         profile 默认；``limits_source`` 如实报告来源（排障证据）。
  A5-4  非法/损坏配置绝不崩：缺文件、YAML 语法错、负数、字符串数字、布尔都退
         回 profile 默认（或按字符串数字宽容解释），返修回路不被一行配置带崩。
  A5-5  计数语义不变（卡 06 契约）：gate PASS 仍不清计数，legacy 每 scope 3 次
         的熔断行为原样保留——放宽额度是运维决策，不是默认行为。

只使用临时目录与纯函数；不启动流水线、不派发 Agent、不读令牌。
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow import repair_budget as rb  # noqa: E402


def _write_cfg(self_dir, text):
    with open(os.path.join(self_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(text)


class A5ConfigChannel(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-a5-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    # ---- A5-1 扁平键 ----
    def test_flat_keys_relax_budget(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 12\nrepair_total_limit: 40\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg, {"repair_per_scope_limit": 12, "repair_total_limit": 40})
        state = {"execution_profile": "legacy"}
        self.assertEqual(rb.resolve_limits(state, cfg)[:2], (12, 40))
        self.assertEqual(rb.limits_source(state, cfg), "config")
        # 第 4 轮（旧默认 3 次即熔断）现在可以继续安排
        state["repair_attempts_by_scope"] = {"sec1": 3}
        state["repair_attempts_total"] = 3
        plan = rb.plan_repair(state, "sec1", "still broken", "digest", config=cfg)
        self.assertEqual(plan["action"], "SCHEDULE")
        self.assertEqual(plan["patch"]["repair_attempts_by_scope"], {"sec1": 4})

    def test_flat_global_limit_only(self):
        _write_cfg(self.tmp, "repair_total_limit: 7\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg, {"repair_total_limit": 7})
        state = {"execution_profile": "legacy"}
        self.assertEqual(rb.resolve_limits(state, cfg), (3, 7, "legacy"))
        state.update({"repair_attempts_by_scope": {"sec1": 0},
                      "repair_attempts_total": 7})
        plan = rb.plan_repair(state, "sec2", "new scope, budget gone", "d", config=cfg)
        self.assertEqual(plan["action"], "HALT")
        self.assertEqual(plan["halt_reason"], rb.BUDGET_EXHAUSTED)
        self.assertEqual(plan["halt"]["total_limit"], 7)
        self.assertEqual(plan["halt"]["limits_source"], "config")

    # ---- A5-2 agent_dispatch 子块 ----
    def test_nested_agent_dispatch_keys(self):
        _write_cfg(self.tmp, "agent_dispatch:\n  repair_per_scope_limit: 9\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg, {"repair_per_scope_limit": 9})
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 9)

    def test_flat_beats_nested(self):
        _write_cfg(self.tmp,
                   "repair_per_scope_limit: 5\n"
                   "agent_dispatch:\n  repair_per_scope_limit: 9\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg["repair_per_scope_limit"], 5)

    # ---- A5-3 优先级 ----
    def test_frozen_beats_config_and_state(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 12\n")
        cfg = rb.load_repair_overrides(self.tmp)
        state = {"execution_profile": "faithful_fast",
                 "execution_config": {"repair_max_per_scope": 1},
                 "repair_per_scope_limit": 8}
        self.assertEqual(rb.resolve_limits(state, cfg)[0], 1)
        self.assertEqual(rb.limits_source(state, cfg), "frozen")

    def test_state_beats_config(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 12\n")
        cfg = rb.load_repair_overrides(self.tmp)
        state = {"repair_per_scope_limit": 8}
        self.assertEqual(rb.resolve_limits(state, cfg)[0], 8)
        self.assertEqual(rb.limits_source(state, cfg), "state")

    def test_config_beats_profile_default(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 12\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 12)
        self.assertEqual(rb.resolve_limits({}, {})[0], rb.LEGACY_PER_SCOPE_LIMIT)
        self.assertEqual(rb.limits_source({}, {}), "profile")

    def test_fast_profile_default_untouched_without_config(self):
        self.assertEqual(rb.resolve_limits({"execution_profile": "faithful_fast"}),
                         (rb.FAST_PER_SCOPE_LIMIT, rb.FAST_TOTAL_LIMIT, "fast"))

    # ---- A5-4 损坏配置不崩 ----
    def test_missing_config_file(self):
        self.assertEqual(rb.load_repair_overrides(self.tmp), {})
        self.assertEqual(rb.resolve_limits({}, None), (3, None, "legacy"))
        self.assertEqual(rb.limits_source({}, None), "profile")

    def test_broken_yaml_falls_back_to_flat_lines(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 6\nbad: [unclosed\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg, {"repair_per_scope_limit": "6"})
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 6,
                         "扁平行解析出字符串数字，resolve_limits 宽容接受")

    def test_negative_and_garbage_values_ignored(self):
        _write_cfg(self.tmp,
                   "repair_per_scope_limit: -3\n"
                   "repair_total_limit: not-a-number\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(cfg, {"repair_per_scope_limit": -3,
                               "repair_total_limit": "not-a-number"})
        self.assertEqual(rb.resolve_limits({}, cfg), (3, None, "legacy"),
                         "负数/非数字一律视为未设置，回落 profile 默认")
        self.assertEqual(rb.limits_source({}, cfg), "profile")

    def test_string_digits_accepted(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: '11'\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 11)
        self.assertEqual(rb.limits_source({}, cfg), "config")

    def test_bool_rejected(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: true\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 3)
        self.assertEqual(rb.limits_source({}, cfg), "profile")

    def test_zero_is_honoured_not_treated_as_missing(self):
        _write_cfg(self.tmp, "repair_per_scope_limit: 0\n")
        cfg = rb.load_repair_overrides(self.tmp)
        self.assertEqual(rb.resolve_limits({}, cfg)[0], 0)
        plan = rb.plan_repair({"execution_profile": "legacy"}, "sec1", "x", "d",
                              config=cfg)
        self.assertEqual(plan["action"], "HALT")
        self.assertEqual(plan["halt_reason"], rb.BUDGET_EXHAUSTED)

    def test_empty_self_dir(self):
        self.assertEqual(rb.load_repair_overrides(None), {})
        self.assertEqual(rb.load_repair_overrides(""), {})

    # ---- A5-5 计数语义不变（卡 06） ----
    def test_gate_pass_still_never_resets_counters(self):
        state = {"repair_attempts_by_scope": {"sec1": 3, "sec2": 1},
                 "repair_attempts_total": 4,
                 "repair_last_fingerprint_by_scope": {"sec1": "fp"},
                 "repair_last_digest_by_scope": {"sec1": "dg"},
                 "repair_attempt_log": []}
        patch = rb.resolved_patch(state, ["sec1"])
        self.assertNotIn("repair_attempts_by_scope", patch)
        self.assertNotIn("repair_attempts_total", patch)
        self.assertNotIn("repair_attempt_log", patch)

    def test_legacy_three_per_scope_default_unchanged(self):
        state = {"execution_profile": "legacy",
                 "repair_attempts_by_scope": {"sec1": 3}}
        plan = rb.plan_repair(state, "sec1", "same shape as before", "d")
        self.assertEqual(plan["action"], "HALT")
        self.assertEqual(plan["halt_reason"], rb.BUDGET_EXHAUSTED)
        self.assertEqual(plan["halt"]["limits_source"], "profile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
