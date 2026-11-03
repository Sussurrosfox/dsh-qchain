# -*- coding: utf-8 -*-
"""test_checkpoint_compatibility.py — 任务10 / CFG-02 / CFG-03。

任务卡10 的核心缺陷（修复前）：
  * `workflow/runner.py::resume_pipeline` 先 `get_graph(db_path)` 再 resume ——
    图是按**当前 YAML/代码**编译的，旧检查点会被静默换到新图上（E 类红线
    「旧 checkpoint 按新 YAML 换图」）；
  * 检查点里没有冻结的执行版本元数据，恢复时无从判断"该用哪张图"；
  * 未知图版本没有显式拒绝路径。

本文件用**真实旧检查点的隔离副本**（`evidence/task-10-work/real-copy.sqlite`，
自生产库 `D:\\teaching\\calculus\\slides\\CA_6\\.langgraph\\courseware_checkpoints.sqlite`
复制而来，生产库本身只读、永不改写）验证三个恢复点：
  T1 等待位置 / T2 待派发位置 / T4 复用位置 —— 恢复目标必须是 legacy-v1，
  `next` 与 pending 批次身份（dispatch_id / batch_id）必须逐字不变。
"""

import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import uuid

ENGINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

from workflow import edges, execution_policy, runner
from workflow import graph as wf_graph
from workflow.adapters import artifact_cache
from workflow.checkpoints import SqliteSaver

REPAIR_WORK = os.path.abspath(os.path.join(ENGINE_ROOT, ".."))
SAMPLE_DB = os.environ.get("QCHAIN_CHECKPOINT_SAMPLE") or os.path.join(
    REPAIR_WORK, "evidence", "task-10-work", "real-copy.sqlite")
THREAD = "qchain_session"

#: 真实旧检查点中的三个恢复点（探测结果见 evidence/task-10-work/probe-real.log）
POS_T1 = "1f1d48ac-8518-6408-8034-d70e044da55d"
POS_T2_PENDING = "1f1d4770-4449-61a6-800a-f1ffdb695615"
POS_T2_STAGED = "1f1d6589-a897-6180-80cb-ff61554e7f26"

_LEGACY_NODES = {
    "phase0_breakpoint", "t0_init_scaffold",
    "t1_request_source_audit", "t1_await_source_audit",
    "t1_request_dependency_plan", "t1_await_dependency_plan",
    "t1_request_coverage_plan", "t1_await_coverage_plan", "t1_verify_mapping",
    "t2_plan_dispatch", "t2_dispatch_sections", "t2_join_sections",
    "t2_plan_check", "t2_dispatch_check", "t2_join_check",
    "t2_submit_and_check_sections", "t2_assemble_main", "t2_shuffle_options",
    "t3_checks", "t3_build", "t3_compare",
    "t4_make_review_pack", "t4_plan_review", "t4_dispatch_review",
    "t4_join_review", "t4_verify_acceptance", "t4_promote_and_deliver",
    "repair_section", "repair_await",
}

_TMPDIR = None
_SAMPLE_SHA = None


def setUpModule():
    global _TMPDIR, _SAMPLE_SHA
    if not os.path.isfile(SAMPLE_DB):
        raise unittest.SkipTest("缺少真实旧检查点隔离副本: %s" % SAMPLE_DB)
    _SAMPLE_SHA = _file_sha(SAMPLE_DB)
    _TMPDIR = tempfile.mkdtemp(prefix="qchain-ckpt-")


def tearDownModule():
    # 生产库/样本库只读：收尾核对样本哈希未变（任何写入都会改变它）
    if _SAMPLE_SHA is not None:
        assert _file_sha(SAMPLE_DB) == _SAMPLE_SHA, "样本检查点被改写（应只读）"
    if _TMPDIR and os.path.isdir(_TMPDIR):
        shutil.rmtree(_TMPDIR, ignore_errors=True)


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fresh_db(tag="db"):
    """把真实样本复制成一次性可写副本（绝不触碰样本与生产库）。"""
    dst = os.path.join(_TMPDIR, "%s-%s.sqlite" % (tag, uuid.uuid4().hex[:8]))
    shutil.copyfile(SAMPLE_DB, dst)
    return dst


def _saver(db_path):
    return SqliteSaver(os.path.abspath(db_path))


def _legacy_graph(db_path):
    return wf_graph.create_legacy_graph(checkpointer=_saver(db_path))


def _state_at(db_path, checkpoint_id=None):
    cfg = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return _legacy_graph(db_path).get_state(cfg)


def _write_exec_checkpoint(db_path, **overrides):
    """在副本里追加一个「带冻结执行元数据」的检查点（模拟新版本 run）。

    做法：取最新检查点 -> 改 channel_values -> 用 saver.put 追加新 checkpoint_id。
    channel_values 由 blobs 表按 channel_versions 还原，所以新 channel 必须同时
    出现在 channel_versions 与 new_versions 里。
    """
    saver = _saver(db_path)
    base_cfg = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    tup = saver.get_tuple(base_cfg)
    assert tup is not None, "样本库缺少 thread=%s" % THREAD
    cp = copy.deepcopy(tup.checkpoint)
    values = dict(cp["channel_values"])
    values.update(overrides)
    versions = dict(cp.get("channel_versions") or {})
    new_versions = {}
    for key in overrides:
        ver = "v-%s" % uuid.uuid4().hex[:12]
        versions[key] = ver
        new_versions[key] = ver
    # 最新检查点由 `SELECT MAX(checkpoint_id)` 决定（sqlite_saver.py:249），
    # 即**字符串字典序**；随机 uuid4 有可能排在样本 id（1f1d…）之前而"不是最新"，
    # 造成测试偶发失败。这里固定用字典序最大的前缀，保证新检查点一定是 latest。
    cp["id"] = "ffffffff-ffff-4fff-8fff-" + uuid.uuid4().hex[:12]
    cp["channel_values"] = values
    cp["channel_versions"] = versions
    cp["updated_channels"] = sorted(set(cp.get("updated_channels") or []) | set(overrides))
    md = dict(tup.metadata or {})
    md.update({"source": "loop", "step": int(md.get("step") or 0) + 1,
               "parents": {}})
    cfg = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    saver.put(cfg, cp, md, new_versions)
    return cp["id"]


class T01LegacyResolution(unittest.TestCase):
    """CFG-02：无版本号旧记录一律 legacy-v1，next / pending 批次逐字不变。"""

    def test_t01_absent_metadata_resolves_legacy(self):
        """完全没有执行字段 -> legacy-v1；content_policy=faithful 也不换图。"""
        policy = execution_policy.resolve_resume({}, None)
        self.assertEqual(policy["execution_profile"], "legacy")
        self.assertEqual(policy["graph_version"], "legacy-v1")
        self.assertTrue(policy["legacy"])
        self.assertIsNone(policy["frozen"])

        with_faithful = execution_policy.resolve_resume(
            {"content_policy": "faithful"}, None)
        self.assertEqual(with_faithful["graph_version"], "legacy-v1")
        self.assertTrue(with_faithful["legacy"])

    def test_t02_real_checkpoint_has_no_execution_metadata(self):
        """真实旧检查点确实没有执行版本元数据（否则本卡测试是空的）。"""
        db = _fresh_db("probe")
        values = runner.read_frozen_execution(db, THREAD)
        self.assertTrue(values, "未读到检查点 channel_values")
        for field in execution_policy.FROZEN_FIELDS:
            self.assertNotIn(field, values,
                             "旧检查点不该含冻结字段 %s" % field)
        self.assertEqual(values.get("content_policy"), "faithful")

    def test_t03_t1_waiting_position(self):
        """T1 等待位置：next=t1_await_coverage_plan，pending 批次身份不变。"""
        db = _fresh_db("t1")
        snap = _state_at(db, POS_T1)
        before = copy.deepcopy(snap.values)
        self.assertEqual(snap.values.get("stage"), "T1_MAPPING_PENDING")
        self.assertEqual(list(snap.next), ["t1_await_coverage_plan"])

        policy = runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertEqual(policy["graph_version"], "legacy-v1")
        self.assertTrue(policy["legacy"])

        after = _state_at(db, POS_T1).values
        self.assertEqual(list(_state_at(db, POS_T1).next),
                         ["t1_await_coverage_plan"])
        self.assertEqual(after.get("pending_dispatch"),
                         before.get("pending_dispatch"))
        self.assertEqual(after.get("batch_id"), before.get("batch_id"))
        self.assertTrue((after.get("pending_dispatch") or {}).get("dispatch_id"))

    def test_t04_t2_pending_dispatch_position(self):
        """T2 待派发位置：next=t2_dispatch_sections，batch_id 不变。"""
        db = _fresh_db("t2p")
        snap = _state_at(db, POS_T2_PENDING)
        before = copy.deepcopy(snap.values)
        self.assertEqual(snap.values.get("stage"), "T2_SECTIONS_PENDING")
        self.assertEqual(list(snap.next), ["t2_dispatch_sections"])
        self.assertEqual(before.get("batch_id"), "bat-t2-da0c829e")

        policy = runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertEqual(policy["graph_version"], "legacy-v1")

        snap2 = _state_at(db, POS_T2_PENDING)
        self.assertEqual(list(snap2.next), ["t2_dispatch_sections"])
        self.assertEqual(snap2.values.get("batch_id"), before.get("batch_id"))
        self.assertEqual(snap2.values.get("dispatch_plan"),
                         before.get("dispatch_plan"))

    def test_t05_t2_staged_position(self):
        """T2 已暂存位置：next=t2_assemble_main，暂存节身份不变。"""
        db = _fresh_db("t2s")
        snap = _state_at(db, POS_T2_STAGED)
        before = copy.deepcopy(snap.values)
        self.assertEqual(snap.values.get("stage"), "T2_SECTIONS_STAGED")
        self.assertEqual(list(snap.next), ["t2_assemble_main"])

        runner.resume_target(db_path=db, thread_id=THREAD)
        snap2 = _state_at(db, POS_T2_STAGED)
        self.assertEqual(list(snap2.next), ["t2_assemble_main"])
        self.assertEqual(snap2.values.get("staged_sections"),
                         before.get("staged_sections"))
        self.assertEqual(snap2.values.get("batch_id"), before.get("batch_id"))

    def test_t06_t4_reuse_position(self):
        """T4 复用位置：零派发复用 -> next=t4_plan_review -> t4_join_review。"""
        db = _fresh_db("t4")
        pending = {"dispatch_id": "bat-t4-reuse-0001", "batch_id": "bat-t4-reuse",
                   "stage": "T4_REVIEW_PENDING", "status": "ok"}
        graph = _legacy_graph(db)
        cfg = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
        graph.update_state(cfg, {
            "stage": "T4_REVIEW_RUNNING",
            "dispatch_mode": "reused",
            "batch_id": "bat-t4-reuse",
            "pending_dispatch": pending,
            "review_pack_ready": True,
        }, as_node="t4_make_review_pack")

        snap = _state_at(db)
        self.assertEqual(list(snap.next), ["t4_plan_review"])
        self.assertEqual(snap.values.get("dispatch_mode"), "reused")
        self.assertEqual(snap.values.get("pending_dispatch"), pending)

        # 卡05 的零派发复用路由：reused -> 直接 fan-in 收口，不再派发
        self.assertEqual(
            edges.route_after_t4_review_plan(
                {"stage": "T4_REVIEW_RUNNING", "dispatch_mode": "reused"}),
            "t4_join_review")

        # 计划节点本身不产生新派发 -> 仍指向 join，pending 批次身份原样保留
        graph.update_state(cfg, {
            "stage": "T4_REVIEW_RUNNING",
            "dispatch_mode": "reused",
            "batch_id": "bat-t4-reuse",
            "pending_dispatch": pending,
        }, as_node="t4_plan_review")
        snap2 = _state_at(db)
        self.assertEqual(list(snap2.next), ["t4_join_review"])
        self.assertEqual(snap2.values.get("pending_dispatch"), pending)
        self.assertEqual(snap2.values.get("batch_id"), "bat-t4-reuse")

        policy = runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertEqual(policy["graph_version"], "legacy-v1")

    def test_t07_legacy_graph_keeps_all_old_node_names(self):
        """legacy-v1 必须保留全部旧节点名（旧 next 只能落在这些名字上）。"""
        g = wf_graph.create_graph_for_version("legacy-v1")
        nodes = set(g.get_graph().nodes)
        missing = _LEGACY_NODES - nodes
        self.assertFalse(missing, "legacy 图丢失节点: %s" % sorted(missing))
        # 向后兼容别名仍指向 legacy
        alias = wf_graph.create_shrink_courseware_graph()
        self.assertTrue(_LEGACY_NODES <= set(alias.get_graph().nodes))


class T02ResumeGraphChoice(unittest.TestCase):
    """CFG-03：先读冻结版本再选图；未知版本/冲突显式拒绝；不得静默回退。"""

    def test_t08_resume_reads_checkpoint_before_compiling(self):
        """resume_pipeline 必须把**冻结版本**传给 get_graph（先读后选）。"""
        db = _fresh_db("order")
        calls = []

        class _FakeGraph(object):
            def get_state(self, config):
                raise RuntimeError("no state")

            def invoke(self, cmd, config=None):
                return {"stage": "RESUMED"}

        def _fake_get_graph(db_path=None, graph_version=None):
            calls.append(graph_version)
            return _FakeGraph()

        original = runner.get_graph
        runner.get_graph = _fake_get_graph
        try:
            result = runner.resume_pipeline(
                {"user_spec": {"main_title": "x"}, "token": "t"},
                thread_id=THREAD, db_path=db)
        finally:
            runner.get_graph = original

        self.assertEqual(result.get("stage"), "RESUMED")
        self.assertEqual(calls, ["legacy-v1"],
                         "resume 必须按检查点冻结版本编译图（实际: %r）" % (calls,))

    def test_t09_unknown_graph_version_rejected(self):
        """未知冻结版本 -> 显式拒绝，不猜回退。"""
        db = _fresh_db("unknown")
        block = dict(execution_schema_version=2, execution_profile="legacy",
                     graph_version="mystery-v9", policy_digest="sha256:deadbeef",
                     execution_config=dict(execution_policy.EXECUTION_DEFAULTS))
        _write_exec_checkpoint(
            db, intent_contract=dict({"schema_version": 1,
                                      "content_policy": "faithful"}, **block),
            **block)
        with self.assertRaises(SystemExit) as ctx:
            runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertIn("UNKNOWN_GRAPH_VERSION", str(ctx.exception))

    def test_t10_contract_checkpoint_conflict_rejected(self):
        """state 与 intent-contract 的执行版本冲突 -> 显式拒绝。"""
        db = _fresh_db("conflict")
        _write_exec_checkpoint(
            db,
            execution_schema_version=2,
            execution_profile="faithful_fast",
            graph_version="faithful-fast-v1",
            policy_digest="sha256:deadbeef",
            execution_config=dict(execution_policy.EXECUTION_DEFAULTS),
            intent_contract={
                "schema_version": 1,
                "content_policy": "faithful",
                "execution_schema_version": 2,
                "execution_profile": "legacy",
                "graph_version": "legacy-v1",
                "policy_digest": "sha256:deadbeef",
                "execution_config": dict(execution_policy.LEGACY_EXECUTION_DEFAULTS),
            })
        with self.assertRaises(SystemExit) as ctx:
            runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertIn("EXECUTION_CONTRACT_CONFLICT", str(ctx.exception))

    def test_t11_frozen_fast_checkpoint_selects_fast_graph(self):
        """已冻结 faithful_fast 的会话 -> 选 faithful-fast-v1 并真正接通轻量图。

        卡15 起 fast 图不再是占位：这里断言它登记的是 C6 职责序列的节点，
        且**没有**逐节 T2 checker 三件套（那是 legacy 的提前语义闸门）。
        """
        db = _fresh_db("fast")
        block = dict(execution_schema_version=2, execution_profile="faithful_fast",
                     graph_version="faithful-fast-v1", policy_digest="sha256:cafe",
                     execution_config=dict(execution_policy.EXECUTION_DEFAULTS))
        _write_exec_checkpoint(
            db, intent_contract=dict({"schema_version": 1,
                                      "content_policy": "faithful"}, **block),
            **block)
        policy = runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertEqual(policy["graph_version"], "faithful-fast-v1")
        self.assertFalse(policy["legacy"])

        fast = runner.get_graph(db, graph_version=policy["graph_version"])
        fast_nodes = set(fast.get_graph().nodes) - {"__start__", "__end__"}
        self.assertNotIn("fast_unimplemented", fast_nodes,
                         "faithful_fast 必须接真实轻量图，不得留占位节点")
        self.assertIn("fast_t1_request_unified_plan", fast_nodes,
                      "T1 必须走一次派发三表: %s" % sorted(fast_nodes))
        self.assertIn("t4_plan_combined_review", fast_nodes,
                      "T4 必须是合并终审: %s" % sorted(fast_nodes))
        for name in ("t2_plan_check", "t2_dispatch_check", "t2_join_check"):
            self.assertNotIn(name, fast_nodes,
                             "fast 图不得保留逐节 T2 checker: %s" % name)
        # 终审相位出口必须真接上：final 门禁通过才可能走到验收/发布
        self.assertIn("t4_verify_acceptance", fast_nodes)
        self.assertIn("t4_promote_and_deliver", fast_nodes)

    def test_t12_requested_profile_cannot_override_frozen(self):
        """显式请求的 execution_profile 不得覆盖旧会话的冻结值。"""
        db = _fresh_db("override")
        with self.assertRaises(SystemExit) as ctx:
            runner.resume_target(db_path=db, thread_id=THREAD,
                                 requested_profile="faithful_fast")
        self.assertIn("EXECUTION_PROFILE_OVERRIDE_REJECTED", str(ctx.exception))
        # 相同值不报错（幂等）
        ok = runner.resume_target(db_path=db, thread_id=THREAD,
                                  requested_profile="legacy")
        self.assertEqual(ok["graph_version"], "legacy-v1")

    def test_t13_config_yaml_cannot_override_frozen(self):
        """当前 config.yaml 写 faithful_fast 也不得改写旧会话的执行图。"""
        db = _fresh_db("yaml")
        self_dir = os.path.join(_TMPDIR, "yaml-self")
        os.makedirs(self_dir, exist_ok=True)
        with open(os.path.join(self_dir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("main_title: 测试\nexecution_profile: faithful_fast\n")
        _write_exec_checkpoint(db, self_dir=self_dir)
        with self.assertRaises(SystemExit) as ctx:
            runner.resume_target(db_path=db, thread_id=THREAD)
        self.assertIn("EXECUTION_PROFILE_OVERRIDE_REJECTED", str(ctx.exception))

        # 未声明执行策略的旧 config.yaml -> 照旧 legacy，不报错
        plain = os.path.join(_TMPDIR, "plain-self")
        os.makedirs(plain, exist_ok=True)
        with open(os.path.join(plain, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("main_title: 测试\n")
        db2 = _fresh_db("yaml2")
        _write_exec_checkpoint(db2, self_dir=plain)
        self.assertEqual(
            runner.resume_target(db_path=db2, thread_id=THREAD)["graph_version"],
            "legacy-v1")


class T03CacheNamespace(unittest.TestCase):
    """CFG-03：新旧执行策略的缓存命名空间必须分离。"""

    def _digest_for(self, tag, extra):
        self_dir = os.path.join(_TMPDIR, "cache-" + tag)
        os.makedirs(self_dir, exist_ok=True)
        with open(os.path.join(self_dir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("main_title: 测试\n" + extra)
        return artifact_cache.config_digest(self_dir)

    def test_t14_legacy_and_fast_cache_keys_do_not_collide(self):
        self.assertNotEqual(execution_policy.cache_namespace("legacy"),
                            execution_policy.cache_namespace("faithful_fast"))
        legacy_digest = self._digest_for("legacy", "execution_profile: legacy\n")
        fast_digest = self._digest_for("fast", "execution_profile: faithful_fast\n")
        self.assertNotEqual(legacy_digest, fast_digest,
                            "执行策略不同的两个工作区不能共用缓存命名空间")

        common = dict(stage="t2", scope="sec1", input_digest="i",
                      prompt_version="p")
        k_legacy = artifact_cache.cache_key(config_digest=legacy_digest, **common)
        k_fast = artifact_cache.cache_key(config_digest=fast_digest, **common)
        self.assertNotEqual(k_legacy, k_fast)

        # 同一策略 -> 稳定命中（不因执行策略解析而抖动）
        same = self._digest_for("legacy", "execution_profile: legacy\n")
        self.assertEqual(legacy_digest, same)


if __name__ == "__main__":
    unittest.main(verbosity=2)
