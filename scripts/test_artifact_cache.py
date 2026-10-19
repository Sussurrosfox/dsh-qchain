#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_artifact_cache.py — audit §17 任务5 派生物缓存回归测试

覆盖：
    T01-T08  模块单测（键公式 / digest 稳定性 / 存取往返 / 各维度失效 /
             副本篡改 / 缺失产物 / 路径逃逸 / scope 级失效）
    T09-T12  T2 接线（plan 探针命中→还原 staged 不派发；join ok→存缓存；
             相同输入第二次 worker submit 为 0；config 变更即失效）
    T13-T14  T1 接线（request 节点命中→跳过派发；miss 时正常派发）

运行: python -m unittest discover scripts -p "test_artifact_cache.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.adapters import artifact_cache as ac  # noqa: E402
from workflow.adapters.artifact_cache import (  # noqa: E402
    ArtifactCache,
    cache_key,
    config_digest,
    file_digest,
    prompt_version,
)
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes.section_batch_nodes import (  # noqa: E402
    node_t2_join_sections,
    node_t2_plan_dispatch,
)
from workflow import contracts  # noqa: E402
from workflow.nodes import mapping_nodes  # noqa: E402

import test_section_batch as tsb  # noqa: E402  (复用治具)


# ---------------------------------------------------------------------------
# 治具
# ---------------------------------------------------------------------------

def write_file(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class T01_CacheKey(unittest.TestCase):
    def test_deterministic(self):
        kw = dict(stage="T1", scope="mapping", input_digest="a" * 64,
                  config_digest="b" * 64, prompt_version="c" * 64)
        self.assertEqual(cache_key(**kw), cache_key(**kw))

    def test_sensitive_to_each_component(self):
        base = cache_key(stage="T1", scope="mapping", input_digest="a" * 64,
                         config_digest="b" * 64, prompt_version="c" * 64)
        variants = {
            "stage": cache_key(stage="T2", scope="mapping",
                               input_digest="a" * 64, config_digest="b" * 64,
                               prompt_version="c" * 64),
            "scope": cache_key(stage="T1", scope="sec1",
                               input_digest="a" * 64, config_digest="b" * 64,
                               prompt_version="c" * 64),
            "input_digest": cache_key(stage="T1", scope="mapping",
                                      input_digest="0" * 64,
                                      config_digest="b" * 64,
                                      prompt_version="c" * 64),
            "config_digest": cache_key(stage="T1", scope="mapping",
                                       input_digest="a" * 64,
                                       config_digest="0" * 64,
                                       prompt_version="c" * 64),
            "prompt_version": cache_key(stage="T1", scope="mapping",
                                        input_digest="a" * 64,
                                        config_digest="b" * 64,
                                        prompt_version="0" * 64),
        }
        for name, key in variants.items():
            self.assertNotEqual(base, key, "缓存键必须对 %s 敏感" % name)


class T02_DigestHelpers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t02-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_config_digest_tracks_file(self):
        tsb.write_config(self.d)
        d1 = config_digest(self.d)
        with open(os.path.join(self.d, "config.yaml"), "a", encoding="utf-8") as f:
            f.write("lesson_minutes: 91\n")
        d2 = config_digest(self.d)
        self.assertNotEqual(d1, d2)

    def test_missing_config_is_stable_empty_digest(self):
        empty = tempfile.mkdtemp(prefix="qchain-cache-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(config_digest(empty), config_digest(empty))

    def test_prompt_version_uses_plugin_prompts(self):
        # 插件自带 prompts/subagent_prompt.md 必须可解析（提示词改版即失效）
        v = prompt_version(self.d, "subagent_prompt.md")
        self.assertRegex(v, r"^[0-9a-f]{64}$")
        v2 = prompt_version(self.d, "prompts/subagent_prompt.md")
        self.assertEqual(v, v2)

    def test_prompt_version_unknown_file_is_stable(self):
        self.assertEqual(prompt_version(self.d, "no-such-prompt.md"),
                         prompt_version(self.d, "no-such-prompt.md"))


class T03_StoreLookupRestore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t03-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        self.cache = ArtifactCache(self.d)
        self.kw = dict(stage="T2_SEC1", scope="sec1",
                       input_digest="a" * 64, config_digest=config_digest(self.d),
                       prompt_version=prompt_version(self.d, "subagent_prompt.md"),
                       artifacts=["sec1.tex"])

    def test_roundtrip(self):
        entry = self.cache.store(**self.kw)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["artifacts"], {"sec1.tex": file_digest(
            os.path.join(self.d, "sec1.tex"))})
        hit = self.cache.lookup(**self.kw)
        self.assertIsNotNone(hit)
        # 破坏 self_dir 中的产物后，restore 应原样恢复
        write_file(os.path.join(self.d, "sec1.tex"), "CLOBBERED\n")
        self.assertTrue(self.cache.restore(hit))
        with open(os.path.join(self.d, "sec1.tex"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "SEC1-OK\n")


class T04_MissOnKeyChange(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t04-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        self.cache = ArtifactCache(self.d)
        self.base = dict(stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
                         config_digest=config_digest(self.d),
                         prompt_version=prompt_version(self.d, "subagent_prompt.md"),
                         artifacts=["sec1.tex"])
        self.assertIsNotNone(self.cache.store(**self.base))

    def test_miss_when_input_digest_changes(self):
        kw = dict(self.base, input_digest="0" * 64)
        self.assertIsNone(self.cache.lookup(**kw))

    def test_miss_when_config_changes(self):
        tsb.write_config(self.d, lesson_minutes=137)
        kw = dict(self.base, config_digest=config_digest(self.d))
        self.assertIsNone(self.cache.lookup(**kw))

    def test_miss_when_prompt_version_changes(self):
        kw = dict(self.base, prompt_version="0" * 64)
        self.assertIsNone(self.cache.lookup(**kw))


class T05_TamperedCopyIsMiss(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t05-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        self.cache = ArtifactCache(self.d)
        self.kw = dict(stage="T2_SEC1", scope="sec1",
                       input_digest="a" * 64, config_digest=config_digest(self.d),
                       prompt_version=prompt_version(self.d, "subagent_prompt.md"),
                       artifacts=["sec1.tex"])
        self.assertIsNotNone(self.cache.store(**self.kw))

    def test_tamper_detected_and_entry_dropped(self):
        entry = self.cache.lookup(**self.kw)
        key = entry["key"]
        cached = os.path.join(self.cache.root, key, "sec1.tex")
        write_file(cached, "TAMPERED\n")
        self.assertIsNone(self.cache.lookup(**self.kw))
        # 失效条目必须删除（不得留下以后可能误用的半成品）
        self.assertFalse(os.path.isdir(os.path.join(self.cache.root, key)))


class T06_StoreGuards(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t06-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)
        self.cache = ArtifactCache(self.d)

    def test_missing_artifact_not_stored(self):
        kw = dict(stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
                  config_digest=config_digest(self.d),
                  prompt_version=prompt_version(self.d, "subagent_prompt.md"),
                  artifacts=["nope.tex"])
        self.assertIsNone(self.cache.store(**kw))

    def test_path_escape_rejected(self):
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        kw = dict(stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
                  config_digest=config_digest(self.d),
                  prompt_version=prompt_version(self.d, "subagent_prompt.md"),
                  artifacts=["../escape.tex"])
        self.assertIsNone(self.cache.store(**kw))

    def test_artifact_list_mismatch_is_miss(self):
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        self.assertIsNotNone(self.cache.store(
            stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
            config_digest=config_digest(self.d),
            prompt_version=prompt_version(self.d, "subagent_prompt.md"),
            artifacts=["sec1.tex"]))
        self.assertIsNone(self.cache.lookup(
            stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
            config_digest=config_digest(self.d),
            prompt_version=prompt_version(self.d, "subagent_prompt.md"),
            artifacts=["sec1.tex", "sec2.tex"]))


class T07_Invalidate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t07-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)
        write_file(os.path.join(self.d, "sec1.tex"), "SEC1-OK\n")
        write_file(os.path.join(self.d, "sec2.tex"), "SEC2-OK\n")
        self.cache = ArtifactCache(self.d)
        common = dict(config_digest=config_digest(self.d),
                      prompt_version=prompt_version(self.d, "subagent_prompt.md"))
        self.assertIsNotNone(self.cache.store(
            stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
            artifacts=["sec1.tex"], **common))
        self.assertIsNotNone(self.cache.store(
            stage="T2_SEC2", scope="sec2", input_digest="b" * 64,
            artifacts=["sec2.tex"], **common))

    def test_scope_invalidate_keeps_others(self):
        self.assertEqual(self.cache.invalidate(scope="sec1"), 1)
        self.assertIsNone(self.cache.lookup(
            stage="T2_SEC1", scope="sec1", input_digest="a" * 64,
            config_digest=config_digest(self.d),
            prompt_version=prompt_version(self.d, "subagent_prompt.md"),
            artifacts=["sec1.tex"]))
        self.assertIsNotNone(self.cache.lookup(
            stage="T2_SEC2", scope="sec2", input_digest="b" * 64,
            config_digest=config_digest(self.d),
            prompt_version=prompt_version(self.d, "subagent_prompt.md"),
            artifacts=["sec2.tex"]))

    def test_invalidate_all(self):
        self.assertEqual(self.cache.invalidate(), 2)


# ---------------------------------------------------------------------------
# T2 接线：plan 探针 + join 落库
# ---------------------------------------------------------------------------

def _run_t2_cycle(d, state):
    """跑一次 plan →（写产物+证据）→ join，返回 (plan_patch, join_patch)。"""
    plan_patch = tsb.run_plan(state)
    self_stage = plan_patch.get("stage")
    if self_stage == "HALTED":
        return plan_patch, None
    requests = list((plan_patch.get("dispatch_plan") or {}).get("requests_full") or [])
    for req in requests:
        scope = req.get("scope")
        write_file(os.path.join(d, f"{scope}.tex"), f"{scope}-CONTENT\n")
    tsb.seed_agent_calls(d, requests, worker="subagent:cache-test")
    join_state = dict(state)
    join_state.update(plan_patch)
    join_state["dispatch_mode"] = "parallel"
    join_patch = node_t2_join_sections(join_state)
    return plan_patch, join_patch


class T08_T2CacheHitSkipsDispatch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t08-")
        self.addCleanup(shutil.rmtree, self.d, True)
        tsb.write_config(self.d)

    def test_second_run_submits_zero(self):
        state = tsb.make_state(self.d, sections=("sec1",))
        plan1, join1 = _run_t2_cycle(self.d, state)
        self.assertEqual(plan1.get("stage"), "T2_SECTIONS_PENDING")
        self.assertIn("sec1", join1.get("section_statuses", {}))
        # 缓存条目已落库
        self.assertTrue(os.path.isdir(os.path.join(self.d, ".pd", "artifact-cache")))
        files_before = tsb.request_files(self.d)

        # 第二次相同输入运行：状态回到 planned（模拟重跑）
        state2 = tsb.make_state(self.d, sections=("sec1",))
        plan2 = tsb.run_plan(state2)
        self.assertEqual(plan2.get("stage"), "T2_SECTIONS_STAGED")
        self.assertEqual(plan2.get("section_statuses", {}).get("sec1"), "staged")
        self.assertEqual(plan2.get("active_section"), "sec1")
        metrics = plan2.get("dispatch_metrics") or {}
        self.assertEqual(metrics.get("reused"), 1)
        self.assertEqual(metrics.get("submitted"), 0)
        # 未写任何新派发单（worker submit 为 0）
        self.assertEqual(tsb.request_files(self.d), files_before)
        # 产物由缓存还原（内容一致）
        with open(os.path.join(self.d, "sec1.tex"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "sec1-CONTENT\n")

    def test_config_change_invalidates(self):
        state = tsb.make_state(self.d, sections=("sec1",))
        _run_t2_cycle(self.d, state)
        # 配置变更 → config_digest 变化 → 缓存失效，必须重新派发
        tsb.write_config(self.d, lesson_minutes=137)
        state2 = tsb.make_state(self.d, sections=("sec1",))
        plan2 = tsb.run_plan(state2)
        self.assertEqual(plan2.get("stage"), "T2_SECTIONS_PENDING")
        self.assertEqual((plan2.get("dispatch_metrics") or {}).get("reused"), 0)
        self.assertEqual((plan2.get("dispatch_metrics") or {}).get("submitted"), 1)


# ---------------------------------------------------------------------------
# T1 接线：request 节点缓存命中跳过派发
# ---------------------------------------------------------------------------

def _make_t1_project(d):
    """最小 Phase 0 契约工程（source.md + manifest + intent contract）。"""
    write_file(os.path.join(d, "source.md"),
               "# 原材料\n\n第一段原文内容。\n\n第二段原文内容。\n")
    manifest = contracts.build_source_manifest(
        project_root=d, source_files=["source.md"], title="缓存测试")
    contract = contracts.build_intent_contract(
        user_spec={"main_title": "缓存测试"})
    self_dir = os.path.join(d, "self")
    os.makedirs(self_dir, exist_ok=True)
    contracts.write_contracts(self_dir, manifest, contract)
    return self_dir


class T09_T1CacheHitSkipsDispatch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qchain-cache-t09-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = _make_t1_project(self.d)
        tsb.write_config(self.self_dir)
        self.state = {
            "self_dir": self.self_dir,
            "project_root": self.d,
            "run_id": "test-run",
            "user_spec": {"main_title": "缓存测试"},
            "intent_contract": contracts.build_intent_contract(
                user_spec={"main_title": "缓存测试"}),
            "history": [],
        }

    def _request_node(self, **kw):
        state = dict(self.state)
        state.update(kw)
        return mapping_nodes.node_t1_request_source_audit(state)

    def test_miss_then_hit(self):
        patch1 = self._request_node()
        self.assertEqual(patch1.get("stage"), "T1_MAPPING_PENDING")
        req = patch1.get("pending_dispatch")
        self.assertIsNotNone(req)
        files_after_first = tsb.request_files(self.self_dir)
        self.assertEqual(len(files_after_first), 1)

        # 手动落库（等价于 verify 节点成功分支的 store，§5.1 分键存储）
        write_file(os.path.join(self.self_dir, "source-map.md"), "source-map 内容\n")
        ArtifactCache(self.self_dir).store(
            stage="T1_SOURCE_AUDIT", scope="source-map",
            input_digest=req["input_digest"],
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, "research_prompt.md"),
            artifacts=["source-map.md"],
        )

        # 第二次相同输入：命中 → 不派发、不写派发单
        patch2 = self._request_node()
        self.assertEqual(patch2.get("stage"), "T1_MAPPING_PENDING")
        self.assertIsNone(patch2.get("pending_dispatch"))
        self.assertEqual(tsb.request_files(self.self_dir), files_after_first)
        self.assertTrue(any("缓存命中" in line for line in patch2.get("history", [])))
        # 产物已自缓存还原
        with open(os.path.join(self.self_dir, "source-map.md"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "source-map 内容\n")

    def test_source_change_invalidates(self):
        patch1 = self._request_node()
        req = patch1["pending_dispatch"]
        write_file(os.path.join(self.self_dir, "source-map.md"), "source-map 内容\n")
        ArtifactCache(self.self_dir).store(
            stage="T1_SOURCE_AUDIT", scope="source-map",
            input_digest=req["input_digest"],
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, "research_prompt.md"),
            artifacts=["source-map.md"],
        )
        # 原材料变更 → 必须重新冻结 Phase 0 契约（manifest digest 对账），
        # 之后 inventory/instruction/input_digest 全变 → 缓存必须失效
        write_file(os.path.join(self.d, "source.md"),
                   "# 原材料\n\n第一段原文内容（已修改）。\n\n第二段原文内容。\n\n第三段。\n")
        manifest = contracts.build_source_manifest(
            project_root=self.d, source_files=["source.md"], title="缓存测试")
        contract = contracts.build_intent_contract(
            user_spec={"main_title": "缓存测试"})
        contracts.write_contracts(self.self_dir, manifest, contract)
        patch2 = self._request_node()
        self.assertIsNotNone(patch2.get("pending_dispatch"))
        self.assertNotEqual(patch2["pending_dispatch"]["input_digest"],
                            req["input_digest"])

    # --- §5.1 round 2/3 barrier 与独立缓存键 ---

    def _store_round(self, stage, scope, digest, artifacts, contents=None):
        for rel in artifacts:
            write_file(os.path.join(self.self_dir, rel),
                       (contents or {}).get(rel, rel + " 内容\n"))
        ArtifactCache(self.self_dir).store(
            stage=stage, scope=scope, input_digest=digest,
            config_digest=config_digest(self.self_dir),
            prompt_version=prompt_version(self.self_dir, "research_prompt.md"),
            artifacts=artifacts,
        )

    def test_round2_barrier_requires_source_map(self):
        """dependency-planner 开派前 source-map.md 必须已在盘。"""
        out = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self.assertEqual(out["stage"], "HALTED")
        self.assertIn("barrier", out["error_message"])

    def test_round2_digest_binds_source_map(self):
        write_file(os.path.join(self.self_dir, "source-map.md"), "v1\n")
        out1 = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self.assertEqual(out1["stage"], "T1_MAPPING_PENDING")
        req1 = out1["pending_dispatch"]
        self.assertTrue(req1["context"]["source_map_digest"].startswith("sha256:"))
        # source-map 变更 → round 2 input_digest 必须变（独立失效）
        write_file(os.path.join(self.self_dir, "source-map.md"), "v2\n")
        out2 = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self.assertNotEqual(out2["pending_dispatch"]["input_digest"],
                            req1["input_digest"])

    def test_round2_round3_independent_cache_keys(self):
        """三张表分键缓存：上游命中不影响下游失效。"""
        write_file(os.path.join(self.self_dir, "source-map.md"), "v1\n")
        dep1 = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self._store_round("T1_DEPENDENCY", "dependency-map",
                          dep1["pending_dispatch"]["input_digest"],
                          ["dependency-map.md"])
        dep2 = mapping_nodes.node_t1_request_dependency_plan(dict(self.state))
        self.assertIsNone(dep2.get("pending_dispatch"))  # round 2 命中
        # round 3 首次仍要派发（自己的键未存过）
        cov1 = mapping_nodes.node_t1_request_coverage_plan(dict(self.state))
        self.assertIsNotNone(cov1.get("pending_dispatch"))

    def test_blocklist_prevents_cache_hit_after_failed_verify(self):
        """REPLAN 后同一 input_digest 不得再命中缓存（audit §10）。"""
        patch1 = self._request_node()
        req = patch1["pending_dispatch"]
        self._store_round("T1_SOURCE_AUDIT", "source-map", req["input_digest"],
                          ["source-map.md"])
        blocked = dict(self.state)
        blocked["dispatch_blocklist"] = [req["input_digest"]]
        patch2 = mapping_nodes.node_t1_request_source_audit(blocked)
        self.assertIsNotNone(patch2.get("pending_dispatch"))
        self.assertEqual(len(tsb.request_files(self.self_dir)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
