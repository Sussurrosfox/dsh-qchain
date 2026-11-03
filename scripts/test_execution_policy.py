#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_execution_policy.py — 任务10 CFG-01 / CFG-04（执行配置白名单与冻结契约）。

覆盖：
  * C1 新增扁平键登记进 gen_config.FIELDS，合法值通过、非法枚举/布尔冒充整数/
    字符串 "false" 误判都被防止；
  * 新字段不进入 TeX 版式宏（config.tex / config-class.tex 无对应宏）；
  * 冻结元数据（execution_schema_version / graph_version / policy_digest）
    形状与摘要稳定性，并随 intent-contract 一并冻结；
  * 新 run 的确认记录包含有效策略，Phase 0 令牌不可绕过。
"""

import json
import os
import sys
import tempfile
import unittest

ENGINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ENGINE_ROOT, "scripts")
for path in (ENGINE_ROOT, SCRIPTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import gen_config  # noqa: E402
from workflow import contracts, execution_policy  # noqa: E402

NEW_KEYS = {
    "execution_profile": ("enum", ("legacy", "faithful_fast")),
    "pedagogy_policy": ("enum", ("advisory", "strict")),
    "budget_policy": ("enum", ("advisory", "strict")),
    "require_classroom_plan": ("bool", None),
    "writer_batch_max_sections": ("int", None),
    "writer_batch_max_source_chars": ("int", None),
    "agent_prompt_max_chars": ("int", None),
    "repair_max_per_scope": ("int", None),
    "repair_max_total": ("int", None),
}

LEGAL_CONFIG = {
    "main_title": "执行契约测试",
    "output_format": "both",
    "delivery_profile": "self_study",
    "execution_profile": "faithful_fast",
    "pedagogy_policy": "advisory",
    "budget_policy": "advisory",
    "require_classroom_plan": "false",
    "writer_batch_max_sections": "2",
    "writer_batch_max_source_chars": "16000",
    "agent_prompt_max_chars": "32000",
    "repair_max_per_scope": "2",
    "repair_max_total": "6",
}


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class T01ConfigWhitelist(unittest.TestCase):
    """CFG-01：新增合法字段通过白名单；非法取值被防止。"""

    def test_t01_new_keys_registered_with_expected_kinds(self):
        for key, spec in NEW_KEYS.items():
            self.assertIn(key, gen_config.FIELDS, "FIELDS 未登记 %s" % key)
            self.assertEqual(gen_config.FIELDS[key], spec,
                             "%s 的 kind/enum 与 C1 不一致" % key)

    def test_t02_legal_config_passes_validation(self):
        self.assertEqual(gen_config.validate(LEGAL_CONFIG), [])

    def test_t03_unknown_enum_rejected(self):
        cfg = dict(LEGAL_CONFIG, pedagogy_policy="loose")
        errors = gen_config.validate(cfg)
        self.assertTrue(errors)
        self.assertIn("pedagogy_policy", errors[0])

    def test_t04_unknown_execution_profile_rejected(self):
        cfg = dict(LEGAL_CONFIG, execution_profile="turbo")
        errors = gen_config.validate(cfg)
        self.assertTrue(errors)
        self.assertIn("execution_profile", errors[0])
        with self.assertRaises(execution_policy.ExecutionPolicyError) as ctx:
            execution_policy.resolve_profile("turbo")
        self.assertEqual(ctx.exception.code, "BAD_ENUM")

    def test_t05_bool_cannot_masquerade_as_int(self):
        # isinstance(True, int) 为真：必须显式拒绝，而不是当 1 通过
        errors = gen_config.validate(dict(LEGAL_CONFIG, writer_batch_max_sections=True))
        self.assertTrue(errors)
        self.assertIn("布尔", errors[0])
        with self.assertRaises(execution_policy.ExecutionPolicyError) as ctx:
            execution_policy.coerce_int(True, "writer_batch_max_sections")
        self.assertEqual(ctx.exception.code, "BAD_INT")

    def test_t06_string_false_is_not_truthy(self):
        # bool('false') is True —— 错误实现；必须按字符串规范化后显式比较
        resolved = execution_policy.resolve_execution_config(
            {"require_classroom_plan": "false"})
        self.assertIs(resolved["require_classroom_plan"], False)
        self.assertEqual(gen_config.validate(
            {"require_classroom_plan": "false"}), [])
        self.assertIs(execution_policy.normalize_bool("TRUE", "x"), True)
        with self.assertRaises(execution_policy.ExecutionPolicyError):
            execution_policy.normalize_bool("nope", "require_classroom_plan")

    def test_t07_non_positive_int_rejected(self):
        for bad in ("0", "-3", "abc", 0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(execution_policy.ExecutionPolicyError):
                    execution_policy.coerce_int(bad, "repair_max_total")
                self.assertTrue(
                    gen_config.validate(dict(LEGAL_CONFIG, repair_max_total=bad)))

    def test_t08_new_keys_do_not_emit_tex_macros(self):
        tmpdir = tempfile.mkdtemp(prefix="qchain-gencfg-")
        lines = ["%s: %s" % (k, v) for k, v in LEGAL_CONFIG.items()]
        _write(os.path.join(tmpdir, "config.yaml"), "\n".join(lines) + "\n")
        self.assertEqual(gen_config.generate(tmpdir), 0)
        with open(os.path.join(tmpdir, "config.tex"), encoding="utf-8") as f:
            tex = f.read()
        with open(os.path.join(tmpdir, "config-class.tex"), encoding="utf-8") as f:
            class_tex = f.read()
        forbidden_macros = [
            "pdExecutionProfile", "pdPedagogyPolicy", "pdBudgetPolicy",
            "pdRequireClassroomPlan", "pdWriterBatchMaxSections",
            "pdWriterBatchMaxSourceChars", "pdAgentPromptMaxChars",
            "pdRepairMaxPerScope", "pdRepairMaxTotal",
        ]
        for macro in forbidden_macros:
            self.assertNotIn(macro, tex, "%s 不应进入 TeX 版式宏" % macro)
        for value in ("16000", "32000", "advisory", "faithful_fast"):
            self.assertNotIn(value, tex, "执行配置值 %s 不应进入 config.tex" % value)
        self.assertNotIn("pdExecutionProfile", class_tex)
        # 既有版面宏仍在（没有顺手删掉旧输出）
        self.assertIn("\\pdMainTitle", tex)
        self.assertIn("\\pdOutputFormat", tex)

    def test_t09_unknown_key_still_rejected(self):
        errors = gen_config.validate({"totally_unknown_key": "1"})
        self.assertTrue(errors)
        self.assertIn("totally_unknown_key", errors[0])


class T02DefaultMatrix(unittest.TestCase):
    """C1 默认矩阵：fast 工程初始值 + legacy 旧默认值。"""

    def test_t10_fast_defaults_match_c1(self):
        self.assertEqual(execution_policy.EXECUTION_DEFAULTS, {
            "pedagogy_policy": "advisory",
            "budget_policy": "advisory",
            "require_classroom_plan": False,
            "writer_batch_max_sections": 2,
            "writer_batch_max_source_chars": 16000,
            "agent_prompt_max_chars": 32000,
            "repair_max_per_scope": 2,
            "repair_max_total": 6,
        })
        self.assertEqual(execution_policy.resolve_execution_config({}),
                         execution_policy.EXECUTION_DEFAULTS)

    def test_t11_legacy_defaults_preserved(self):
        legacy = execution_policy.LEGACY_EXECUTION_DEFAULTS
        self.assertEqual(legacy["pedagogy_policy"], "strict")
        self.assertEqual(legacy["budget_policy"], "strict")
        self.assertIs(legacy["require_classroom_plan"], True)
        self.assertEqual(legacy["writer_batch_max_sections"], 1)
        self.assertEqual(legacy["repair_max_per_scope"], 3)
        self.assertIsNone(legacy["repair_max_total"])

    def test_t12_content_policy_only_suggests_profile(self):
        self.assertEqual(execution_policy.suggest_profile("faithful"),
                         execution_policy.FAST_PROFILE)
        self.assertEqual(execution_policy.suggest_profile("authoring"),
                         execution_policy.LEGACY_PROFILE)
        self.assertEqual(execution_policy.suggest_profile(None),
                         execution_policy.FAST_PROFILE)
        # 建议不等于自动换图：缺省仍是 legacy
        self.assertEqual(execution_policy.resolve_profile(None),
                         execution_policy.LEGACY_PROFILE)

    def test_t13_graph_versions_and_cache_namespace(self):
        self.assertEqual(execution_policy.graph_version_for_profile("legacy"),
                         "legacy-v1")
        self.assertEqual(execution_policy.graph_version_for_profile("faithful_fast"),
                         "faithful-fast-v1")
        self.assertNotEqual(execution_policy.cache_namespace("legacy"),
                            execution_policy.cache_namespace("faithful_fast"))


class T03FrozenMetadata(unittest.TestCase):
    """冻结元数据形状、摘要稳定性与 intent-contract 落盘。"""

    def test_t14_frozen_block_shape_matches_c1(self):
        block = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=execution_policy.EXECUTION_DEFAULTS,
            content_policy="faithful")
        self.assertEqual(sorted(block), sorted(execution_policy.FROZEN_FIELDS))
        self.assertEqual(block["execution_schema_version"], 2)
        self.assertEqual(block["execution_profile"], "faithful_fast")
        self.assertEqual(block["graph_version"], "faithful-fast-v1")
        self.assertTrue(block["policy_digest"].startswith("sha256:"))
        self.assertEqual(block["execution_config"],
                         execution_policy.EXECUTION_DEFAULTS)

    def test_t15_policy_digest_is_stable_and_sensitive(self):
        base = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=execution_policy.EXECUTION_DEFAULTS,
            content_policy="faithful",
            extra_config={"dialogue_enabled": "false"})
        again = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=dict(execution_policy.EXECUTION_DEFAULTS),
            content_policy="faithful",
            extra_config={"dialogue_enabled": "false"})
        self.assertEqual(base["policy_digest"], again["policy_digest"])

        changed_dialogue = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=execution_policy.EXECUTION_DEFAULTS,
            content_policy="faithful",
            extra_config={"dialogue_enabled": "sec0"})
        self.assertNotEqual(base["policy_digest"], changed_dialogue["policy_digest"])

        changed_policy = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=execution_policy.EXECUTION_DEFAULTS,
            content_policy="authoring",
            extra_config={"dialogue_enabled": "false"})
        self.assertNotEqual(base["policy_digest"], changed_policy["policy_digest"])

        changed_budget = execution_policy.build_frozen_metadata(
            profile="faithful_fast",
            execution_config=dict(execution_policy.EXECUTION_DEFAULTS,
                                  repair_max_total=9),
            content_policy="faithful",
            extra_config={"dialogue_enabled": "false"})
        self.assertNotEqual(base["policy_digest"], changed_budget["policy_digest"])

    def test_t16_intent_contract_carries_frozen_execution(self):
        spec = {
            "main_title": "执行契约",
            "content_policy": "faithful",
            "delivery_profile": "self_study",
            "execution_profile": "faithful_fast",
        }
        frozen = execution_policy.resolve_new_run(spec)
        contract = contracts.build_intent_contract(user_spec=spec, execution=frozen)
        for field in execution_policy.FROZEN_FIELDS:
            self.assertIn(field, contract)
        self.assertEqual(contract["execution_profile"], "faithful_fast")
        self.assertEqual(contract["graph_version"], "faithful-fast-v1")
        self.assertEqual(contract["content_policy"], "faithful")
        # 契约摘要覆盖执行块：执行块变化必须改变 digest
        other = contracts.build_intent_contract(
            user_spec=spec,
            execution=execution_policy.build_frozen_metadata(
                profile="legacy",
                execution_config=execution_policy.EXECUTION_DEFAULTS,
                content_policy="faithful"))
        self.assertNotEqual(contract["digest"], other["digest"])

    def test_t17_incomplete_execution_block_rejected(self):
        with self.assertRaises(contracts.ContractError) as ctx:
            contracts.build_intent_contract(
                user_spec={"content_policy": "faithful"},
                execution={"execution_profile": "faithful_fast"})
        self.assertEqual(ctx.exception.code, "INCOMPLETE_EXECUTION_METADATA")

    def test_t18_legacy_contract_has_no_execution_block(self):
        contract = contracts.build_intent_contract(
            user_spec={"content_policy": "faithful", "delivery_profile": "self_study"})
        for field in execution_policy.FROZEN_FIELDS:
            self.assertNotIn(field, contract,
                             "旧契约不得凭空写入执行元数据（否则旧摘要被动改写）")
        self.assertIsNone(execution_policy.frozen_from_contract(contract))

    def test_t19_new_run_writes_frozen_record_to_disk(self):
        tmpdir = tempfile.mkdtemp(prefix="qchain-contract-")
        self_dir = os.path.join(tmpdir, "self")
        os.makedirs(self_dir)
        source = os.path.join(tmpdir, "raw.md")
        _write(source, "# 原料\n\n正文\n")
        spec = {
            "main_title": "执行契约",
            "content_policy": "faithful",
            "delivery_profile": "self_study",
            "execution_profile": "faithful_fast",
            "source_files": source,
        }
        frozen = execution_policy.resolve_new_run(spec)
        contract = contracts.build_intent_contract(user_spec=spec, execution=frozen)
        manifest = contracts.build_source_manifest(
            project_root=tmpdir, self_dir=self_dir, source_files=source,
            title=spec["main_title"])
        contracts.write_contracts(self_dir, manifest, contract)
        with open(os.path.join(self_dir, ".pd", "intent-contract.json"),
                  encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["execution_profile"], "faithful_fast")
        self.assertEqual(written["graph_version"], "faithful-fast-v1")
        self.assertEqual(written["policy_digest"], frozen["policy_digest"])
        self.assertEqual(written["execution_config"], frozen["execution_config"])

    def test_t22_config_yaml_cannot_rewrite_frozen_execution_profile(self):
        """CFG-03：config.yaml 不得改写已冻结的 execution_profile（gen_config 侧复核）。"""
        tmpdir = tempfile.mkdtemp(prefix="qchain-frozen-cfg-")
        pd_dir = os.path.join(tmpdir, ".pd")
        os.makedirs(pd_dir)
        with open(os.path.join(pd_dir, "intent-contract.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"execution_profile": "legacy", "graph_version": "legacy-v1"}, f)

        def _errors(declared):
            cfg = {"main_title": "冻结校验", "delivery_profile": "self_study",
                   "execution_profile": declared}
            return gen_config.verify_confirmed_spec(tmpdir, cfg)

        mismatched = _errors("faithful_fast")
        self.assertTrue(any("执行策略被篡改" in e for e in mismatched),
                        "config.yaml 改写冻结策略必须报错，实际: %s" % mismatched)
        self.assertEqual(_errors("legacy"), [])
        # 旧契约（无执行字段）保持旧行为：不因缺字段报错
        with open(os.path.join(pd_dir, "intent-contract.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"content_policy": "faithful"}, f)
        self.assertEqual(_errors("faithful_fast"), [])


class T04Phase0Surface(unittest.TestCase):
    """CFG-04：Phase 0 显示有效策略，且令牌不可绕过。"""

    def test_t20_phase0_payload_shows_effective_policy(self):
        from workflow.nodes import human_nodes

        described = execution_policy.describe_for_phase0(
            {"content_policy": "faithful", "execution_profile": "legacy"})
        self.assertEqual(described["effective_profile"], "legacy")
        self.assertEqual(described["effective_graph_version"], "legacy-v1")
        self.assertEqual(described["recommended_profile"], "faithful_fast")
        self.assertEqual(described["execution_config"],
                         execution_policy.EXECUTION_DEFAULTS)
        self.assertIsNone(described["error"])
        # Phase 0 节点确实把执行策略并入断点载荷
        source = open(human_nodes.__file__, encoding="utf-8").read()
        self.assertIn('"execution"', source,
                      "Phase 0 载荷必须展示有效执行策略（CFG-04）")

    def test_t21_phase0_token_cannot_be_bypassed(self):
        from langgraph.types import Command

        from workflow.graph import create_legacy_graph
        from workflow.checkpoints import SqliteSaver

        tmpdir = tempfile.mkdtemp(prefix="qchain-phase0-")
        db = os.path.join(tmpdir, "cp.sqlite")
        graph = create_legacy_graph(checkpointer=SqliteSaver(db))
        cfg = {"configurable": {"thread_id": "phase0-token"}}
        state = {
            "project_root": tmpdir,
            "self_dir": os.path.join(tmpdir, "self"),
            "input_path": "",
            "stage": "INIT",
            "user_spec": {"main_title": "令牌测试"},
            "history": [],
        }
        first = graph.invoke(state, config=cfg)
        self.assertIn("__interrupt__", first)
        payload = first["__interrupt__"][0].value
        self.assertEqual(payload["type"], "PHASE0_SPEC")
        self.assertIn("execution", payload)

        # 错误令牌：不得确认，必须继续挂起
        second = graph.invoke(
            Command(resume={"user_spec": {"main_title": "令牌测试"}, "token": "wrong"}),
            config=cfg)
        self.assertIn("__interrupt__", second,
                      "错误令牌必须重新挂起，不能推进流水线")
        snap = graph.get_state(cfg)
        self.assertNotEqual((snap.values or {}).get("stage"), "T0_INITIALIZED")
        self.assertFalse((snap.values or {}).get("phase0_confirmed"))


if __name__ == "__main__":
    unittest.main()
