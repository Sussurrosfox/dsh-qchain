#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_dispatch_policy.py — 任务19（C12）派发并发策略与执行器计划契约回归测试

覆盖 PARCFG-01 ~ PARCFG-06：

    PARCFG-01  新 faithful_fast 无显式选择 → fast/4；显式 safe 仍 1；工具覆盖带来源
    PARCFG-02  v2 嵌套/扁平冲突点名；0=自动；负数/小数/布尔/>16 显式拒绝
    PARCFG-03  旧契约（无版本）保持旧优先级与冻结值，旧 safe 不会变成 fast
    PARCFG-04  8 个请求、并发上限 4：计划记 batch_size=8 且 max_concurrency=4
               （请求数不再被当作并发上限）
    PARCFG-05  宿主能力未知记 null 且仍按配置上限执行；Python worker 声明 1 不被突破
    PARCFG-06  Python 与 JS 共享同一份 fixtures，结果逐例一致

全部用例只写临时目录（tempfile.TemporaryDirectory），不触碰真实 self/ 或
项目 config.yaml。

运行: python -m pytest -q -p no:cacheprovider scripts/test_dispatch_policy.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
for candidate in (HERE, PLUGIN_ROOT):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import check_dispatch  # noqa: E402
import gen_config  # noqa: E402
from workflow import dispatch_policy  # noqa: E402
from workflow.dispatch_policy import DispatchPolicyError  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402

FIXTURE_PATH = os.path.join(HERE, "fixtures", "dispatch-policy-cases.json")
JS_TEST_PATH = os.path.join(HERE, "test_dispatch_policy.mjs")
DIGEST = "a" * 64


def load_fixtures():
    with io.open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def project(policy, fields):
    """把解析结果投影成 fixtures 声明的语义字段（诊断只比错误码）。"""
    out = {}
    for field in fields:
        if field == "diagnostic_codes":
            out[field] = [d.get("code") for d in policy.get("diagnostics") or []]
        else:
            out[field] = policy.get(field)
    return out


def evaluate_case(case, fields):
    """按 fixtures 声明求解单个 case：返回投影字段或 {"error": code}。"""
    kwargs = dict(
        config=case.get("config") or {},
        run_scope=case.get("run_scope", ""),
        execution_profile=case.get("execution_profile"),
        tool_override=case.get("tool_override"),
        worker_capabilities=case.get("worker_capabilities"),
        frozen=case.get("frozen"),
    )
    try:
        return project(dispatch_policy.resolve_dispatch_policy(**kwargs), fields)
    except DispatchPolicyError as exc:
        return {"error": exc.code}


def write_config(directory, text):
    """在临时目录写 config.yaml（仅测试目录，绝不写真实工程）。"""
    path = os.path.join(directory, "config.yaml")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def fake_request(index, section):
    return {
        "dispatch_id": "dsp-t2-%02d" % index,
        "scope": section,
        "role": "section_writer",
        "input_digest": DIGEST,
        "artifacts": ["sections/%s.tex" % section],
        "context": {"source_assets": {"figure": 1, "table": 0, "tikz": 0}},
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# PARCFG-01 / 02 / 03 / 05：fixtures 驱动的纯解析器回归
# ---------------------------------------------------------------------------

class T19_FixturePolicy(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixtures()
        self.fields = self.fixture["fields"]

    def test_t19_01_fixture_fields_declared(self):
        for field in ("dispatch_contract_version", "profile", "profile_source",
                      "configured_concurrency", "concurrency_source",
                      "concurrency_limit", "executor_kind",
                      "worker_max_concurrency", "capability_verified",
                      "effective_concurrency", "diagnostic_codes"):
            self.assertIn(field, self.fields)

    def test_t19_02_all_cases_match_fixture(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                actual = evaluate_case(case, self.fields)
                if "error" in case:
                    self.assertEqual({"error": case["error"]}, actual,
                                     "%s: %s" % (case["name"], case.get("why")))
                else:
                    self.assertEqual(case["expect"], actual,
                                     "%s: %s" % (case["name"], case.get("why")))

    def test_t19_03_faithful_fast_default_is_four(self):
        """PARCFG-01：新 faithful_fast 未显式选档位 → fast/4。"""
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2},
            execution_profile="faithful_fast",
        )
        self.assertEqual("fast", policy["profile"])
        self.assertEqual(4, policy["effective_concurrency"])
        self.assertEqual("execution_profile:faithful_fast", policy["profile_source"])

    def test_t19_04_explicit_safe_stays_serial(self):
        """PARCFG-01：显式 safe 即使 execution_profile=faithful_fast 也保持 1。"""
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2,
                    "agent_dispatch": {"profile": "safe"}},
            run_scope="full",
            execution_profile="faithful_fast",
        )
        self.assertEqual("safe", policy["profile"])
        self.assertEqual(1, policy["effective_concurrency"])
        self.assertEqual("config:agent_dispatch.profile", policy["profile_source"])

    def test_t19_05_tool_override_has_source(self):
        """PARCFG-01：本次工具显式正整数覆盖，且来源可追溯。"""
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2,
                    "agent_dispatch": {"max_concurrency": 4}},
            tool_override=2,
        )
        self.assertEqual(2, policy["effective_concurrency"])
        self.assertEqual("tool:max_concurrency", policy["concurrency_source"])
        self.assertEqual(2, policy["configured_concurrency"])

    def test_t19_06_conflict_names_both_keys(self):
        """PARCFG-02：v2 嵌套/扁平冲突必须点名两个配置项，不静默挑一个。"""
        with self.assertRaises(DispatchPolicyError) as ctx:
            dispatch_policy.resolve_dispatch_policy(
                config={"dispatch_contract_version": 2,
                        "agent_dispatch": {"max_concurrency": 4},
                        "agent_max_concurrency": 2})
        self.assertEqual("CONFIG_CONCURRENCY_CONFLICT", ctx.exception.code)
        detail = ctx.exception.detail
        self.assertIn("agent_dispatch.max_concurrency", detail)
        self.assertIn("agent_max_concurrency", detail)

    def test_t19_07_zero_auto_and_illegal_values(self):
        """PARCFG-02：0=自动；负数/小数/布尔/>16 显式拒绝。"""
        auto = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2, "agent_max_concurrency": 0})
        self.assertEqual(0, auto["configured_concurrency"])
        self.assertEqual("auto", auto["concurrency_source"])

        for value, code in ((-2, "CONFIG_CONCURRENCY_INVALID"),
                            (2.5, "CONFIG_CONCURRENCY_INVALID"),
                            (True, "CONFIG_CONCURRENCY_INVALID"),
                            (32, "CONFIG_CONCURRENCY_OUT_OF_RANGE")):
            with self.subTest(value=value):
                with self.assertRaises(DispatchPolicyError) as ctx:
                    dispatch_policy.resolve_dispatch_policy(
                        config={"dispatch_contract_version": 2,
                                "agent_max_concurrency": value})
                self.assertEqual(code, ctx.exception.code)

    def test_t19_08_legacy_keeps_old_priority(self):
        """PARCFG-03：旧契约保留「扁平优先」，只加诊断，不改冻结值。"""
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"agent_dispatch": {"max_concurrency": 4},
                    "agent_max_concurrency": 1},
            run_scope="full")
        self.assertEqual(1, policy["dispatch_contract_version"])
        self.assertEqual(1, policy["effective_concurrency"])
        self.assertEqual("config:agent_max_concurrency", policy["concurrency_source"])
        self.assertEqual(["CONFIG_CONCURRENCY_SHADOWED"],
                         [d["code"] for d in policy["diagnostics"]])

    def test_t19_09_unknown_version_rejected(self):
        with self.assertRaises(DispatchPolicyError) as ctx:
            dispatch_policy.resolve_dispatch_policy(
                config={"dispatch_contract_version": 3})
        self.assertEqual("DISPATCH_CONTRACT_VERSION_UNSUPPORTED",
                         ctx.exception.code)


# ---------------------------------------------------------------------------
# PARCFG-05：执行器能力（宿主未知 = null，Python worker 不被突破）
# ---------------------------------------------------------------------------

class T19_ExecutorCapability(unittest.TestCase):
    def test_t19_10_host_capability_is_null_not_one(self):
        """PARCFG-05：宿主能力未知记 null，绝不沿用 Python 保守值 1。"""
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2,
                    "agent_dispatch": {"max_concurrency": 4}},
            worker_capabilities=None)
        self.assertEqual("host_subagent", policy["executor_kind"])
        self.assertIsNone(policy["worker_max_concurrency"])
        self.assertFalse(policy["capability_verified"])
        self.assertEqual(4, policy["effective_concurrency"])

    def test_t19_11_python_worker_cap_not_exceeded(self):
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2,
                    "agent_dispatch": {"max_concurrency": 4}},
            worker_capabilities={"max_concurrency": 1, "sync": True})
        self.assertEqual("python_registry", policy["executor_kind"])
        self.assertEqual(1, policy["worker_max_concurrency"])
        self.assertTrue(policy["capability_verified"])
        self.assertEqual(1, policy["effective_concurrency"])

    def test_t19_12_python_worker_cap_min_of_both(self):
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2, "agent_max_concurrency": 8},
            worker_capabilities={"max_concurrency": 4, "sync": True})
        self.assertEqual(8, policy["concurrency_limit"])
        self.assertEqual(4, policy["effective_concurrency"])


# ---------------------------------------------------------------------------
# 节点侧接入：_load_dispatch_config / _disk_plan / check_dispatch 门禁
# ---------------------------------------------------------------------------

class T19_NodeIntegration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qchain-t19-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_t19_13_load_config_v2_template_default(self):
        """模板形态（无预填 profile + 版本 2）→ full 作用域下 fast/4。"""
        write_config(self.dir,
                     "dispatch_contract_version: 2\n"
                     "agent_dispatch:\n"
                     "  max_concurrency: 0\n"
                     "  allow_unattested: false\n"
                     "agent_max_concurrency: 0\n")
        cfg = sbn._load_dispatch_config(self.dir, "full")
        self.assertEqual(2, cfg["dispatch_contract_version"])
        self.assertEqual("fast", cfg["profile"])
        self.assertEqual(4, cfg["max_concurrency"])
        self.assertEqual(4, cfg["effective_concurrency"])
        self.assertIsNone(cfg["dispatch_error"])
        # 兼容旧调用方的键必须保留
        for key in ("configured_max_concurrency", "batch_window",
                    "timeout_seconds", "max_retries", "allow_unattested",
                    "model_map", "run_scope"):
            self.assertIn(key, cfg)

    def test_t19_14_load_config_legacy_flat_wins(self):
        """PARCFG-03：无版本 → legacy，扁平 1 覆盖嵌套 4，并留诊断。"""
        write_config(self.dir,
                     "agent_dispatch:\n"
                     "  max_concurrency: 4\n"
                     "agent_max_concurrency: 1\n")
        cfg = sbn._load_dispatch_config(self.dir, "full")
        self.assertEqual(1, cfg["dispatch_contract_version"])
        self.assertEqual(1, cfg["max_concurrency"])
        self.assertEqual("config:agent_max_concurrency", cfg["concurrency_source"])
        self.assertEqual(["CONFIG_CONCURRENCY_SHADOWED"],
                         [d["code"] for d in cfg["diagnostics"]])

    def test_t19_15_load_config_conflict_reports_real_reason(self):
        """PARCFG-02/07：冲突必须报真实原因，绝不静默退回「猜 2」。"""
        write_config(self.dir,
                     "dispatch_contract_version: 2\n"
                     "agent_dispatch:\n"
                     "  max_concurrency: 4\n"
                     "agent_max_concurrency: 2\n")
        cfg = sbn._load_dispatch_config(self.dir, "full")
        self.assertIsNotNone(cfg["dispatch_error"])
        self.assertEqual("CONFIG_CONCURRENCY_CONFLICT",
                         cfg["dispatch_error"]["code"])
        # 契约版本仍是配置声明的 2（冲突不是版本问题），但并发退回「自动」，
        # 绝不把冲突值之一当成结果；节点据 dispatch_error 直接 HALT。
        self.assertEqual(2, cfg["dispatch_contract_version"])
        self.assertEqual(0, cfg["configured_max_concurrency"])
        self.assertEqual("auto", cfg["concurrency_source"])

    def test_t19_16_load_config_missing_file_defaults(self):
        cfg = sbn._load_dispatch_config(self.dir, "")
        self.assertEqual(1, cfg["dispatch_contract_version"])
        self.assertEqual("safe", cfg["profile"])
        self.assertEqual(1, cfg["max_concurrency"])
        self.assertIsNone(cfg["dispatch_error"])

    def test_t19_17_disk_plan_batch_size_separated_from_cap(self):
        """PARCFG-04：8 个请求、上限 4 → batch_size=8，max_concurrency=4。"""
        sections = ["s%02d" % i for i in range(1, 9)]
        requests = [fake_request(i, sec) for i, sec in enumerate(sections)]
        policy = dispatch_policy.resolve_dispatch_policy(
            config={"dispatch_contract_version": 2,
                    "agent_dispatch": {"max_concurrency": 4}},
            worker_capabilities=None)
        plan = sbn._disk_plan("bat-t2-t19", sections, requests, 4, policy=policy)

        self.assertEqual(8, plan["batch_size"])
        self.assertEqual(4, plan["max_concurrency"])
        self.assertEqual(4, plan["effective_concurrency"])
        self.assertEqual(4, plan["configured_concurrency"])
        self.assertEqual("host_subagent", plan["executor_kind"])
        self.assertIsNone(plan["worker_max_concurrency"])
        self.assertEqual(2, plan["dispatch_contract_version"])
        self.assertIn("concurrency_source", plan)
        self.assertIn("serial_reason", plan)
        # 旧键仍在（向后兼容）
        self.assertEqual(len(sections), plan["required_scope_count"])

    def test_t19_18_gate_accepts_queued_batch(self):
        """PARCFG-04：排队请求数 > 并发槽位不再是门禁错误。"""
        sections = ["s%02d" % i for i in range(1, 9)]
        requests = [fake_request(i, sec) for i, sec in enumerate(sections)]
        plan = sbn._disk_plan("bat-t2-t19", sections, requests, 4)
        errors, warnings, _stats = check_dispatch.check_plan(plan)
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_t19_19_gate_rejects_faked_cap(self):
        """反证：把 max_concurrency 改成超过客户端上限仍被拒。"""
        sections = ["s%02d" % i for i in range(1, 9)]
        requests = [fake_request(i, sec) for i, sec in enumerate(sections)]
        plan = sbn._disk_plan("bat-t2-t19", sections, requests, 4)
        plan["max_concurrency"] = 32
        errors, _warnings, _stats = check_dispatch.check_plan(plan)
        self.assertIn("BAD_CONCURRENCY", [e["code"] for e in errors])

    def test_t19_20_gate_rejects_batch_size_mismatch(self):
        sections = ["s%02d" % i for i in range(1, 9)]
        requests = [fake_request(i, sec) for i, sec in enumerate(sections)]
        plan = sbn._disk_plan("bat-t2-t19", sections, requests, 4)
        plan["batch_size"] = 3
        errors, _warnings, _stats = check_dispatch.check_plan(plan)
        self.assertIn("BATCH_SIZE_MISMATCH", [e["code"] for e in errors])


# ---------------------------------------------------------------------------
# 配置层：模板默认值、迁移诊断、未知版本拒绝
# ---------------------------------------------------------------------------

class T19_ConfigLayer(unittest.TestCase):
    def setUp(self):
        self.template = os.path.join(PLUGIN_ROOT, "templates", "config.yaml")

    def test_t19_21_template_declares_v2_without_prefilled_profile(self):
        """PARCFG-01：模板不得预填 safe 压掉 faithful_fast 的默认 4。"""
        cfg = gen_config.load_config(self.template)
        self.assertEqual(2, int(str(cfg["dispatch_contract_version"]).strip()))
        dispatch = cfg.get("agent_dispatch") or {}
        self.assertIsInstance(dispatch, dict)
        self.assertNotIn("profile", dispatch,
                         "模板不得预填 agent_dispatch.profile（否则等于用户显式选择）")
        self.assertEqual(0, int(str(dispatch.get("max_concurrency", 0)).strip()))

    def test_t19_22_template_validates_clean(self):
        cfg = gen_config.load_config(self.template)
        self.assertEqual([], gen_config.validate(cfg))

    def test_t19_23_unknown_version_rejected_by_config(self):
        errors = gen_config.validate({"dispatch_contract_version": 3})
        self.assertTrue(any("不受支持" in msg for msg in errors), errors)

    def test_t19_24_bool_concurrency_rejected_by_config(self):
        errors = gen_config.validate({"agent_max_concurrency": True})
        self.assertTrue(any("布尔" in msg for msg in errors), errors)

    def test_t19_25_over_limit_rejected_by_config(self):
        errors = gen_config.validate({"agent_max_concurrency": 32})
        self.assertTrue(any("上限" in msg for msg in errors), errors)

    def test_t19_26_conflict_diagnosed_at_config_time(self):
        errors = gen_config.validate({
            "dispatch_contract_version": 2,
            "agent_dispatch": {"max_concurrency": 4},
            "agent_max_concurrency": 2,
        })
        self.assertTrue(any("CONFIG_CONCURRENCY_CONFLICT" in msg for msg in errors),
                        errors)

    def test_t19_27_legacy_conflict_not_reported(self):
        """PARCFG-03：旧契约保持扁平优先，不把冲突当错误。"""
        errors = gen_config.validate({
            "agent_dispatch": {"max_concurrency": 4},
            "agent_max_concurrency": 2,
        })
        self.assertEqual([], errors)


# ---------------------------------------------------------------------------
# PARCFG-06：Python / JS 共享 fixtures，逐例一致
# ---------------------------------------------------------------------------

class T19_CrossLanguage(unittest.TestCase):
    def test_t19_28_js_matches_python(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH，跳过跨语言比对")
        proc = subprocess.run(
            [node, JS_TEST_PATH, "--dump"],
            cwd=PLUGIN_ROOT, capture_output=True, text=True, timeout=180,
            # node 的 stdout 是 UTF-8；Windows 上不声明就会按 cp936 解
            encoding="utf-8", errors="replace")
        self.assertEqual(0, proc.returncode,
                         "JS fixtures 求值失败: %s" % (proc.stderr or ""))
        js = json.loads(proc.stdout)

        fixture = load_fixtures()
        fields = fixture["fields"]
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertIn(case["name"], js)
                expected = evaluate_case(case, fields)
                self.assertEqual(expected, js[case["name"]],
                                 "%s: Python 与 JS 结果不一致" % case["name"])

    def test_t19_29_index_js_has_no_silent_default(self):
        """PARCFG-07：JS 侧不得保留 catch→默认 2 的静默兜底。"""
        index_path = os.path.join(PLUGIN_ROOT, "index.js")
        with io.open(index_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("dispatch_policy.mjs", source)
        self.assertNotIn("return { max_concurrency: 2, model_map: {} };", source)
        self.assertNotIn("|| Number(cfg.max_concurrency) || 2", source)
        self.assertIn("CONFIG_UNREADABLE", source)
        self.assertIn("effective_concurrency", source)


if __name__ == "__main__":
    unittest.main()
