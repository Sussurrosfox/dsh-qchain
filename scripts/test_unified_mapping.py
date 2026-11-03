#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_unified_mapping.py — 卡 12：一次 T1 规划产出三份既有映射。

faithful_fast 执行图把 legacy 的 T1 三次串行 round（source-auditor →
dependency-planner → coverage-planner）压成**一次** T1_UNIFIED_PLAN 派发，
同一个 agent 一次产出 source-map.md / dependency-map.md / coverage-map.md，
仍向下游提供完整合法的三张表（既有解析器契约不变）。

MAP-01 冷缓存：恰好一次派发 → 三表 → 一套 receipt 认证三件产物 →
       组事务提升 → 下游既有解析器（check_fidelity / estimate_minutes /
       mapping_nodes._extract_expected_sections）可正常读取。
MAP-02 组事务：缺一表 / 一个无效 source 引用 / 摘要不符 → 不冻结、
       不写组缓存、不提前提升任何一张表；staging 保留供局部修订。
MAP-03 组缓存：完整同摘要组命中 0 派发；来源或策略变化使整组失效；
       组缓存键与 legacy 三轮缓存不混用。
MAP-04 legacy 不回归：仍走原三轮派发（artifacts 不含 staging 路径），
       三 round barrier 语义不变。

全部写盘均在 tempfile 临时工程内完成，不触碰 D:\\teaching 下真实项目目录，
不启动真实流水线、不调用真实 agent。
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_ROOT = os.path.dirname(HERE)
for _p in (ENGINE_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workflow import contracts  # noqa: E402
from workflow.adapters.artifact_cache import (  # noqa: E402
    ArtifactCache, config_digest, prompt_version,
)
from workflow.adapters.worker_registry import worker_registry  # noqa: E402
from workflow.nodes import agent_evidence, agent_nodes, mapping_nodes  # noqa: E402
import test_section_batch as tsb  # noqa: E402  （复用派发治具：write_config /
#                                                   request_files / seed_agent_calls）

# 统一规划的产物落点：先写 staging（隐藏区），组事务通过后才提升到 self 根。
ROOT_TABLES = ["source-map.md", "dependency-map.md", "coverage-map.md"]
STAGED = {rel: ".pd/mapping-staging/" + rel for rel in ROOT_TABLES}
STAGING_DIR_REL = ".pd/mapping-staging"
CACHE_SCOPE = "unified-mapping"
CACHE_STAGE = "T1_UNIFIED_PLAN"
PROMPT_FILE = "faithful_mapping_prompt.md"

COV_HEADER = (
    "| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | "
    "practice_mode | carrier_qid | planned_minutes | 推荐讲法 | 易错点 | "
    "source_ids | action | new_objects | reason | tier |")
COV_SEP = "|" + "---|" * 15
DEP_HEADER = "| 节点 | 前置节点 | 说明 | 是否必需 |"
DEP_SEP = "|---|---|---|---|"


def unified_nodes():
    """惰性导入统一规划节点模块（缺失即为本卡待修缺陷）。"""
    return importlib.import_module("workflow.nodes.fast_mapping_nodes")


def write_file(path, text, newline="\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) \
        else None
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    return path


def merge(*patches):
    out = {}
    for patch in patches:
        if patch:
            out.update(patch)
    return out


# ---------------------------------------------------------------------------
# 治具：临时 Phase 0 工程（source.md + manifest + intent contract + config）
# ---------------------------------------------------------------------------

SOURCE_TEXT = (
    "# 原文第一节\n\n第一段：定义与基本性质。\n\n第二段：一个典型算例。\n\n"
    "## 原文第二节\n\n第三段：定理与证明要点。\n")


def make_project(root, *, execution_profile="faithful_fast",
                 config_extra=None, source_text=SOURCE_TEXT):
    """建最小 Phase 0 工程，返回 self_dir（一切写盘都在 root 内）。"""
    write_file(os.path.join(root, "source.md"), source_text,
               newline="")
    manifest = contracts.build_source_manifest(
        project_root=root, source_files=["source.md"], title="统一映射测试")
    contract = contracts.build_intent_contract(
        user_spec={"main_title": "统一映射测试",
                   "content_policy": "faithful",
                   "delivery_profile": "self_study"})
    self_dir = os.path.join(root, "self")
    os.makedirs(self_dir, exist_ok=True)
    contracts.write_contracts(self_dir, manifest, contract)
    cfg = dict(config_extra or {})
    if execution_profile:
        cfg["execution_profile"] = execution_profile
    tsb.write_config(self_dir, **cfg)
    return self_dir


def base_state(root, self_dir):
    return {
        "self_dir": self_dir,
        "project_root": root,
        "run_id": "test-run-unified",
        "input_path": os.path.join(root, "source.md"),
        "user_spec": {"main_title": "统一映射测试",
                      "content_policy": "faithful",
                      "delivery_profile": "self_study"},
        "history": [],
        "dispatch_results": {},
    }


def inventory_units(self_dir):
    from scripts import source_inventory as inv
    inv_obj = inv.load_inventory(self_dir)
    if not inv_obj:
        inv_obj = json.load(open(os.path.join(
            self_dir, ".pd", "source-inventory.json"), encoding="utf-8"))
    return inv_obj["units"]


def source_map_text(units):
    lines = ["# source-map（来源骨架映射）", ""]
    for unit in units:
        if unit.get("disposition") == "excluded":
            lines.append("%s -> none -> PRESERVE -> EXCLUDED_BY_USER -> 用户排除"
                         % unit["source_id"])
        else:
            lines.append("%s -> sec1 -> PRESERVE -> PRESERVED -> 原文直保"
                         % unit["source_id"])
    return "\n".join(lines) + "\n"


def coverage_map_text(units, *, bad_source_id=None, section="sec1"):
    rows = [COV_HEADER, COV_SEP]
    for unit in units:
        if unit.get("disposition") == "excluded":
            continue
        src = bad_source_id or unit["source_id"]
        rows.append(
            "| %s | %s | 忠实保留原文结论 | Q1 | core | 讲解 | Q1 | 10 | "
            "原文推导直接讲解 | 无 | %s | PRESERVE |  | 原文直保 | core |"
            % (unit["locator"], section, src))
    return "# coverage-map（覆盖与预算规划）\n\n" + "\n".join(rows) + "\n"


def dependency_map_text(*, node="sec1", prereq="-"):
    return ("# dependency-map（前置依赖图）\n\n" + DEP_HEADER + "\n" + DEP_SEP
            + "\n| %s | %s | 无额外前置 | 是 |\n" % (node, prereq))


class UnifiedCase(unittest.TestCase):
    """公共治具：临时工程 + 一次统一派发的完整往返。"""

    execution_profile = "faithful_fast"
    config_extra = None

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="qct-unified-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.self_dir = make_project(
            self.root, execution_profile=self.execution_profile,
            config_extra=self.config_extra)
        self.state = base_state(self.root, self.self_dir)
        self.addCleanup(worker_registry.clear)

    # -- 断言辅助 ---------------------------------------------------------

    def assert_no_root_tables(self, msg="组事务未通过时不得提升任何一张表"):
        for rel in ROOT_TABLES:
            self.assertFalse(os.path.isfile(os.path.join(self.self_dir, rel)),
                             "%s（%s 已提升）" % (msg, rel))

    def assert_no_group_cache(self, digest):
        entry = ArtifactCache(self.self_dir).lookup(
            stage=CACHE_STAGE, scope=CACHE_SCOPE, input_digest=digest,
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, PROMPT_FILE),
            artifacts=[STAGED[rel] for rel in ROOT_TABLES])
        self.assertIsNone(entry, "组事务未通过时不得写组缓存")

    def assert_group_cache(self, digest):
        entry = ArtifactCache(self.self_dir).lookup(
            stage=CACHE_STAGE, scope=CACHE_SCOPE, input_digest=digest,
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, PROMPT_FILE),
            artifacts=[STAGED[rel] for rel in ROOT_TABLES])
        self.assertIsNotNone(entry, "三表全通过后必须以三件完整组写入缓存")
        return entry

    # -- 派发往返 ---------------------------------------------------------

    def request(self, state=None):
        return unified_nodes().node_fast_t1_request_unified_plan(
            dict(state or self.state))

    def staged_files(self, *, omit=None, bad_source_id=None, dep_kwargs=None):
        units = inventory_units(self.self_dir)
        files = {
            STAGED["source-map.md"]: source_map_text(units),
            STAGED["dependency-map.md"]: dependency_map_text(
                **(dep_kwargs or {})),
            STAGED["coverage-map.md"]: coverage_map_text(
                units, bad_source_id=bad_source_id),
        }
        if omit:
            files.pop(STAGED[omit], None)
        return files

    def dispatch_with_worker(self, state, files):
        """假 worker 一次写三份 staging（等价宿主子 Agent 一次会话产三表）。"""
        worker_registry.set(
            lambda sys_prompt, user_content: json.dumps({"files": files}),
            name="fake-mapping-planner")
        return unified_nodes().node_fast_t1_await_unified_plan(dict(state))

    def request_and_await(self, *, files=None):
        patch = self.request()
        req = patch.get("pending_dispatch")
        self.assertIsNotNone(req, "冷缓存必须派发一次统一规划请求")
        files = files if files is not None else self.staged_files()
        state = merge(self.state, patch)
        await_patch = self.dispatch_with_worker(state, files)
        return req, merge(state, await_patch), await_patch

    def verify(self, state):
        state = dict(state)
        state["t1_plan_mode"] = "unified"
        return mapping_nodes.node_t1_verify_mapping(state)

    def run_to_frozen(self):
        """完整走一遍：请求 → 一次派发 → 组校验 → 冻结；返回 (req, digest)。"""
        req, state, _ = self.request_and_await()
        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "T1_MAPPED",
                         "三表全通过后必须冻结：%r" % (verify_patch,))
        self.assertTrue(verify_patch.get("mapping_frozen"))
        digest = (verify_patch.get("t1_round_digests") or {}).get(CACHE_STAGE)
        self.assertTrue(digest, "冻结补丁必须记录组 input_digest")
        return req, digest


# ---------------------------------------------------------------------------
# MAP-01：冷缓存一次派发产出三表，下游既有解析器可读
# ---------------------------------------------------------------------------

class T12_MAP01_SingleCallThreeTables(UnifiedCase):

    def test_map01_one_dispatch_three_tables(self):
        patch = self.request()
        req = patch.get("pending_dispatch")
        self.assertIsNotNone(req)
        self.assertEqual(req["stage"], CACHE_STAGE)
        self.assertEqual(sorted(req["artifacts"]), sorted(STAGED.values()),
                         "一次派发必须同时声明三份 staging 产物")
        self.assertEqual(len(tsb.request_files(self.self_dir)), 1,
                         "一次规划只允许产生一张派发单")
        # 组指纹必须携带来源摘要与新图版本（C7）
        self.assertIn("source_inventory_digest", req["context"])
        self.assertEqual(req["context"]["graph_version"], "faithful-fast-v1")

        state = merge(self.state, patch)
        files = self.staged_files()
        await_patch = self.dispatch_with_worker(state, files)
        self.assertEqual(await_patch.get("stage"), "T1_UNIFIED_PLAN_READY")
        self.assertEqual(len(tsb.request_files(self.self_dir)), 1,
                         "等待阶段不得追加派发单")

        # 一个真实 receipt 认证三件产物
        receipt_path = os.path.join(
            self.self_dir, ".pd", "agent-calls",
            "%s.json" % req["dispatch_id"])
        self.assertTrue(os.path.isfile(receipt_path), "派发必须落一份证据台账")
        with open(receipt_path, encoding="utf-8") as f:
            receipt = json.load(f)
        self.assertEqual(sorted(receipt["artifacts_written"]),
                         sorted(STAGED.values()),
                         "一份 receipt 必须认证三件产物")
        self.assertEqual(sorted(receipt["output_digests"]),
                         sorted(STAGED.values()))

        state = merge(state, await_patch)
        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "T1_MAPPED")
        self.assertTrue(verify_patch.get("mapping_frozen"))
        self.assertEqual(verify_patch.get("expected_sections"), ["sec1"])
        self.assertTrue(verify_patch.get("t1_round_digests", {}).get(CACHE_STAGE))

        for rel in ROOT_TABLES:
            self.assertTrue(os.path.isfile(os.path.join(self.self_dir, rel)),
                            "组事务通过后 %s 必须提升到 self 根" % rel)

        # 下游既有解析器契约不变：三表都能被既有代码正常读取
        import check_fidelity
        rows, errors = check_fidelity.parse_source_map(self.self_dir)
        self.assertEqual(errors, [], "source-map 必须满足既有 F2 解析器")
        self.assertTrue(rows)

        cov_rows, warnings = check_fidelity.parse_coverage_map(self.self_dir)
        self.assertEqual(warnings, [])
        self.assertTrue(cov_rows, "coverage-map 必须登记 source_ids/action")

        import estimate_minutes
        parsed = estimate_minutes.parse_coverage_map(
            os.path.join(self.self_dir, "coverage-map.md"))
        self.assertTrue(parsed, "coverage-map 必须能被既有课时估算器解析")

        self.assertEqual(
            mapping_nodes._extract_expected_sections(
                self.self_dir, os.path.join(self.self_dir, "coverage-map.md")),
            ["sec1"])

        self.assert_group_cache(verify_patch["t1_round_digests"][CACHE_STAGE])

    def test_map01_interrupt_receipt_covers_three_artifacts(self):
        """生产路径（interrupt + qchain_dispatch）同样是一 receipt 三产物。"""
        patch = self.request()
        req = patch["pending_dispatch"]
        state = merge(self.state, patch)
        for rel, text in self.staged_files().items():
            write_file(os.path.join(self.self_dir, rel), text)
        tsb.seed_agent_calls(self.self_dir, [req])
        original = agent_nodes.interrupt
        agent_nodes.interrupt = lambda payload: {}
        self.addCleanup(lambda: setattr(agent_nodes, "interrupt", original))

        await_patch = unified_nodes().node_fast_t1_await_unified_plan(dict(state))
        self.assertEqual(await_patch.get("routing_action"), "PROCEED")
        entry = await_patch["dispatch_results"][req["dispatch_id"]]
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["verified_via"], "agent",
                         "单 receipt 必须为三件产物背书")
        self.assertEqual(sorted(entry["produced"]), sorted(STAGED.values()))


# ---------------------------------------------------------------------------
# MAP-02：组事务失败不冻结 / 不缓存 / 不部分提升
# ---------------------------------------------------------------------------

class T12_MAP02_GroupTransaction(UnifiedCase):

    def test_map02_missing_table_never_promotes(self):
        patch = self.request()
        req = patch["pending_dispatch"]
        state = merge(self.state, patch)
        files = self.staged_files(omit="dependency-map.md")
        await_patch = self.dispatch_with_worker(state, files)
        self.assertEqual(await_patch.get("stage"), "HALTED")
        self.assertIn("dependency-map.md", await_patch.get("error_message", ""))
        state = merge(state, await_patch)

        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "REPAIR_LOOP")
        self.assertEqual(verify_patch.get("routing_action"), "REPLAN")
        self.assertEqual(verify_patch.get("repair_target"), "dependency-map.md")
        self.assertFalse(verify_patch.get("mapping_frozen"))
        self.assert_no_root_tables()
        self.assert_no_group_cache(req["input_digest"])
        # staging 保留：下一轮只补做缺的那一张
        self.assertTrue(os.path.isfile(os.path.join(
            self.self_dir, STAGED["source-map.md"])))
        self.assertFalse(os.path.isfile(os.path.join(
            self.self_dir, STAGED["dependency-map.md"])))

    def test_map02_invalid_source_reference_never_frozen(self):
        patch = self.request()
        req = patch["pending_dispatch"]
        state = merge(self.state, patch)
        files = self.staged_files(bad_source_id="SRC-999")
        await_patch = self.dispatch_with_worker(state, files)
        self.assertEqual(await_patch.get("routing_action"), "PROCEED")
        state = merge(state, await_patch)

        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "REPAIR_LOOP")
        self.assertEqual(verify_patch.get("repair_target"), "coverage-map.md")
        self.assertIn("SRC-999", "; ".join(verify_patch.get("history", []))
                      + verify_patch.get("repair_reason", ""))
        self.assertFalse(verify_patch.get("mapping_frozen"))
        self.assert_no_root_tables()
        self.assert_no_group_cache(req["input_digest"])
        self.assertIn(req["input_digest"],
                      verify_patch.get("dispatch_blocklist") or [],
                      "失败的组指纹必须进入缓存黑名单")

    def test_map02_digest_mismatch_never_promoted(self):
        """摘要不符（await 后 staging 被改写）同样不得提升。"""
        req, state, _ = self.request_and_await()
        write_file(os.path.join(self.self_dir, STAGED["source-map.md"]),
                   "# 被改写\n\nSRC-001 -> sec9 -> PRESERVE -> PRESERVED -> x\n")
        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "REPAIR_LOOP")
        self.assertEqual(verify_patch.get("repair_target"), "source-map.md")
        self.assertFalse(verify_patch.get("mapping_frozen"))
        self.assert_no_root_tables()
        self.assert_no_group_cache(req["input_digest"])

    def test_map02_source_omission_never_frozen(self):
        """来源遗漏（inventory 单元未登记）不得冻结。"""
        patch = self.request()
        req = patch["pending_dispatch"]
        state = merge(self.state, patch)
        units = inventory_units(self.self_dir)
        dropped = source_map_text(units).splitlines()
        files = self.staged_files()
        files[STAGED["source-map.md"]] = "\n".join(
            line for line in dropped
            if not line.startswith(units[0]["source_id"])) + "\n"
        await_patch = self.dispatch_with_worker(state, files)
        state = merge(state, await_patch)
        verify_patch = self.verify(state)
        self.assertEqual(verify_patch.get("stage"), "REPAIR_LOOP")
        self.assertEqual(verify_patch.get("repair_target"), "source-map.md")
        self.assertIn(units[0]["source_id"],
                      verify_patch.get("repair_reason", ""))
        self.assert_no_root_tables()
        self.assert_no_group_cache(req["input_digest"])


# ---------------------------------------------------------------------------
# MAP-03：组缓存（同摘要组 0 派发 / 来源或策略变化失效 / 不与 legacy 混用）
# ---------------------------------------------------------------------------

class T12_MAP03_GroupCache(UnifiedCase):

    def test_map03_same_group_hits_without_dispatch(self):
        req, digest = self.run_to_frozen()
        request_count = len(tsb.request_files(self.self_dir))
        self.assertEqual(request_count, 1)

        # 产物被清掉：命中必须自缓存整组还原（三件一起回到 staging）
        for rel in ROOT_TABLES:
            if os.path.isfile(os.path.join(self.self_dir, rel)):
                os.remove(os.path.join(self.self_dir, rel))
        patch2 = self.request()
        self.assertIsNone(patch2.get("pending_dispatch"),
                          "完整同摘要组必须零派发命中")
        self.assertEqual(len(tsb.request_files(self.self_dir)), request_count,
                         "缓存命中不得新增派发单")
        self.assertTrue(any("缓存命中" in line
                            for line in patch2.get("history", [])))
        for rel in ROOT_TABLES:
            self.assertTrue(os.path.isfile(os.path.join(
                self.self_dir, STAGED[rel])), "组缓存命中必须还原 %s" % rel)
            self.assertFalse(os.path.isfile(os.path.join(self.self_dir, rel)),
                             "还原只回暂存区，提升仍由组事务负责")
        verify_patch = self.verify(merge(self.state, patch2))
        self.assertEqual(verify_patch.get("stage"), "T1_MAPPED")
        self.assertTrue(verify_patch.get("mapping_frozen"))
        for rel in ROOT_TABLES:
            self.assertTrue(os.path.isfile(os.path.join(self.self_dir, rel)),
                            "还原后组事务仍须把 %s 提升到 self 根" % rel)

    def test_map03_source_change_invalidates_group(self):
        req, _digest = self.run_to_frozen()
        write_file(os.path.join(self.root, "source.md"),
                   SOURCE_TEXT + "\n新增第四段：补充说明。\n", newline="")
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["source.md"],
            title="统一映射测试")
        contract = contracts.build_intent_contract(
            user_spec={"main_title": "统一映射测试",
                       "content_policy": "faithful",
                       "delivery_profile": "self_study"})
        contracts.write_contracts(self.self_dir, manifest, contract)

        patch2 = self.request()
        self.assertIsNotNone(patch2.get("pending_dispatch"),
                             "来源变化必须使整组缓存失效")
        self.assertNotEqual(patch2["pending_dispatch"]["input_digest"],
                            req["input_digest"])

    def test_map03_policy_change_invalidates_group(self):
        req, _digest = self.run_to_frozen()
        tsb.write_config(self.self_dir, execution_profile="faithful_fast",
                         pedagogy_policy="advisory")
        patch2 = self.request()
        self.assertIsNotNone(patch2.get("pending_dispatch"),
                             "策略变化必须使整组缓存失效")
        self.assertNotEqual(patch2["pending_dispatch"]["input_digest"],
                            req["input_digest"])

    def test_map03_group_cache_not_shared_with_legacy_rounds(self):
        req, digest = self.run_to_frozen()
        legacy = ArtifactCache(self.self_dir).lookup(
            stage="T1_SOURCE_AUDIT", scope="source-map", input_digest=digest,
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, "research_prompt.md"),
            artifacts=["source-map.md"])
        self.assertIsNone(legacy, "新组缓存与旧三轮缓存不得混用")


# ---------------------------------------------------------------------------
# MAP-04：legacy 三轮不回归
# ---------------------------------------------------------------------------

class T12_MAP04_LegacyRounds(UnifiedCase):
    execution_profile = None   # 旧工程：config.yaml 无 execution_profile

    def test_map04_legacy_still_one_table_per_round(self):
        patch = mapping_nodes.node_t1_request_source_audit(dict(self.state))
        req = patch.get("pending_dispatch")
        self.assertIsNotNone(req)
        self.assertEqual(req["stage"], "T1_SOURCE_AUDIT")
        self.assertEqual(req["artifacts"], ["source-map.md"],
                         "legacy round 仍只声明一张表")
        self.assertFalse(os.path.isdir(
            os.path.join(self.self_dir, STAGING_DIR_REL)),
            "legacy 路径不得产生 staging 目录")

    def test_map04_legacy_verify_promotes_single_table(self):
        patch = mapping_nodes.node_t1_request_source_audit(dict(self.state))
        units = inventory_units(self.self_dir)
        write_file(os.path.join(self.self_dir, "source-map.md"),
                   source_map_text(units))
        verify_patch = mapping_nodes.node_t1_verify_mapping(
            merge(self.state, patch))
        self.assertEqual(verify_patch.get("stage"), "T1_MAPPED")
        self.assertTrue(verify_patch.get("mapping_frozen"))
        self.assertFalse(os.path.isdir(
            os.path.join(self.self_dir, STAGING_DIR_REL)))

    def test_map04_legacy_round2_barrier_intact(self):
        out = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self.assertEqual(out.get("stage"), "HALTED")
        self.assertIn("barrier", out.get("error_message", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
