#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_dispatch_interrupt.py — 任务卡20：批次派发入口（C12.2）回归测试。

覆盖「同批独立 section 一次取全部、批次索引可核对、中断载荷直接可执行」：

    PARSEL-03  dispatch_protocol_block 输出可直接执行的批次调用参数
    BIDX-01    .pd/dispatch-batches/<batch_id>.json 落盘 / 读取 / 输入校验
    BIDX-02    T2_WRITE / T2_CHECK / T4 三个计划生产节点都写批次索引
    BIDX-03    索引 plan_digest 与计划文件字节 sha256 一致
    INTR-01    批次中断载荷（T2_WRITE / T2_CHECK / T4）带 dispatch_args={self_dir,batch_id}
    INTR-02    单请求派发载荷带 dispatch_args={request_ref} 且不含 batch_id
    INTR-03    真实 interrupt() 捕获：join 节点实际下发的载荷含 dispatch_args

运行: python -m pytest -q -p no:cacheprovider scripts/test_dispatch_interrupt.py
"""

import hashlib
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

from workflow.nodes import agent_evidence  # noqa: E402
from workflow.nodes import agent_nodes  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes import semantic_nodes as sn  # noqa: E402
from workflow.nodes import t4_review_batch_nodes as t4nodes  # noqa: E402
from workflow.nodes.semantic_nodes import CHECK_PLAN_REL  # noqa: E402

import test_section_batch as tsb  # noqa: E402
import test_semantic_nodes as tsn  # noqa: E402
import test_t4_review_batch as tt4  # noqa: E402


def sha_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def index_path(d, batch_id):
    return os.path.join(d, ".pd", "dispatch-batches", f"{batch_id}.json")


# ---------------------------------------------------------------------------
# PARSEL-03 / INTR-02：派发协议块
# ---------------------------------------------------------------------------

class TestDispatchProtocolBlock(unittest.TestCase):

    def test_keeps_three_legacy_keys(self):
        blk = agent_evidence.dispatch_protocol_block()
        self.assertEqual(
            set(blk), {"dispatch_protocol", "required_tool", "forbidden"})
        self.assertEqual(blk["dispatch_protocol"], "host-subagent-v1")
        self.assertEqual(blk["required_tool"], "qchain_dispatch")

    def test_batch_block_carries_dispatch_args(self):
        blk = agent_evidence.dispatch_protocol_block(
            batch_id="bat-t2-check-abcd1234", self_dir="/tmp/proj/self")
        self.assertEqual(blk["dispatch_protocol"], "host-subagent-v1")
        self.assertEqual(blk["dispatch_args"],
                         {"self_dir": "/tmp/proj/self",
                          "batch_id": "bat-t2-check-abcd1234"})
        self.assertEqual(blk["dispatch_batch_mode"], "single-call-per-batch")
        text = blk["dispatch_instructions"]
        self.assertIn("batch_id", text)
        self.assertIn("一次", text)          # 明确「一次取全部」
        self.assertIn("request_ref", text)   # 明确「不要逐单调用」
        self.assertNotIn("request_ref", blk["dispatch_args"])

    def test_single_request_block_carries_request_ref(self):
        blk = agent_evidence.dispatch_protocol_block(
            request_ref=".pd/agent-requests/dsp-x.json", self_dir="/tmp/proj/self")
        self.assertEqual(blk["dispatch_args"],
                         {"self_dir": "/tmp/proj/self",
                          "request_ref": ".pd/agent-requests/dsp-x.json"})
        self.assertEqual(blk["dispatch_batch_mode"], "single-request")
        self.assertNotIn("batch_id", blk["dispatch_args"])


# ---------------------------------------------------------------------------
# BIDX-01：批次索引读写
# ---------------------------------------------------------------------------

class TestBatchIndex(unittest.TestCase):

    def test_write_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            rel = agent_evidence.write_batch_index(
                d,
                batch_id="bat-t2-abcd1234",
                run_id="run-1",
                stage="T2",
                request_ids=["dsp-a", "dsp-b"],
                plan_rel=".pd/dispatch-plan.json",
                plan_digest="a" * 64,
                sections=["sec1", "sec2"],
                required_scope_count=2,
                dispatch_contract_version=2,
            )
            rel_norm = rel.replace("\\", "/")
            self.assertEqual(rel_norm, ".pd/dispatch-batches/bat-t2-abcd1234.json")
            self.assertTrue(os.path.isfile(index_path(d, "bat-t2-abcd1234")))
            idx = agent_evidence.load_batch_index(d, "bat-t2-abcd1234")
            self.assertIsNotNone(idx)
            self.assertEqual(idx["schema_version"], 1)
            self.assertEqual(idx["batch_id"], "bat-t2-abcd1234")
            self.assertEqual(idx["run_id"], "run-1")
            self.assertEqual(idx["stage"], "T2")
            self.assertEqual(idx["request_ids"], ["dsp-a", "dsp-b"])
            self.assertEqual(idx["request_refs"],
                             [".pd/agent-requests/dsp-a.json",
                              ".pd/agent-requests/dsp-b.json"])
            self.assertEqual(idx["plan_rel"], ".pd/dispatch-plan.json")
            self.assertEqual(idx["plan_digest"], "a" * 64)
            self.assertEqual(idx["dispatch_contract_version"], 2)

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(agent_evidence.load_batch_index(d, "bat-nope"))

    def test_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                agent_evidence.write_batch_index(
                    d, batch_id="", run_id="r", stage="T2",
                    request_ids=["dsp-a"], plan_rel=".pd/dispatch-plan.json")
            with self.assertRaises(ValueError):
                agent_evidence.write_batch_index(
                    d, batch_id="bat-dup", run_id="r", stage="T2",
                    request_ids=["dsp-a", "dsp-a"],
                    plan_rel=".pd/dispatch-plan.json")
            with self.assertRaises(ValueError):
                agent_evidence.write_batch_index(
                    d, batch_id="bat-empty", run_id="r", stage="T2",
                    request_ids=[], plan_rel=".pd/dispatch-plan.json")


# ---------------------------------------------------------------------------
# BIDX-02 / BIDX-03：三个计划生产节点都写索引
# ---------------------------------------------------------------------------

class TestPlanProducersWriteIndex(unittest.TestCase):

    def test_t2_write_plan_writes_index(self):
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(tsb.register_capable_worker(4))
            tsb.write_config(d, agent_dispatch="fast")
            state = tsb.make_state(d, sections=("sec0", "sec1", "sec2"))
            patch = tsb.run_plan(state)
            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
            batch_id = patch["batch_id"]
            idx = agent_evidence.load_batch_index(d, batch_id)
            self.assertIsNotNone(idx, "T2_WRITE 计划必须写批次索引")
            self.assertEqual(idx["stage"], "T2")
            self.assertEqual(idx["run_id"], "test-run")
            self.assertEqual(
                idx["request_ids"],
                [r["dispatch_id"] for r in patch["dispatch_plan"]["requests_full"]])
            self.assertEqual(idx["request_refs"],
                             [f".pd/agent-requests/{did}.json"
                              for did in idx["request_ids"]])
            self.assertEqual(idx["plan_rel"], ".pd/dispatch-plan.json")
            self.assertEqual(
                idx["plan_digest"],
                sha_file(os.path.join(d, ".pd", "dispatch-plan.json")))

    def test_t2_check_plan_writes_index(self):
        with tempfile.TemporaryDirectory() as d:
            tsn.write_sec(d, "sec1")
            tsn.write_sec(d, "sec2")
            state = tsn.make_check_state(d, sections=("sec1", "sec2"))
            patch, _ = tsn.run_plan_check(state)
            self.assertEqual(patch["stage"], "T2_CHECK_PENDING")
            batch_id = patch["batch_id"]
            idx = agent_evidence.load_batch_index(d, batch_id)
            self.assertIsNotNone(idx, "T2_CHECK 语义计划必须写批次索引")
            self.assertEqual(idx["stage"], "T2_CHECK")
            self.assertEqual(idx["plan_rel"], CHECK_PLAN_REL)
            self.assertEqual(
                idx["request_ids"],
                [r["dispatch_id"] for r in patch["dispatch_plan"]["requests_full"]])
            self.assertEqual(
                idx["plan_digest"], sha_file(tsn.check_plan_file(d)))

    def test_t4_plan_writes_index(self):
        with tempfile.TemporaryDirectory() as d:
            sbn._reset_pool()
            self.addCleanup(sbn._reset_pool)
            self.addCleanup(tt4.register_capable_worker(8))
            self_dir = tt4.make_self(d, ("sec0", "sec1", "sec2"))
            state = tt4.base_state(self_dir, ("sec0", "sec1", "sec2"))
            with mock.patch.object(t4nodes, "ScriptRunner",
                                   tt4.fake_runner("PASS")):
                patch = t4nodes.node_t4_plan_review(state)
            self.assertEqual(patch["stage"], "T4_REVIEW_PENDING")
            batch_id = patch["batch_id"]
            idx = agent_evidence.load_batch_index(self_dir, batch_id)
            self.assertIsNotNone(idx, "T4 计划必须写批次索引")
            self.assertEqual(idx["stage"], "T4")
            self.assertEqual(
                idx["request_ids"],
                [r["dispatch_id"] for r in patch["dispatch_plan"]["requests_full"]])
            self.assertEqual(
                idx["plan_digest"],
                sha_file(os.path.join(self_dir, ".pd", "dispatch-plan.json")))


# ---------------------------------------------------------------------------
# INTR-01：三种批次中断载荷都带 dispatch_args
# ---------------------------------------------------------------------------

class TestBatchInterruptPayloads(unittest.TestCase):

    def _t2_write_batch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        self.addCleanup(tsb.register_capable_worker(4))
        tsb.write_config(d, agent_dispatch="fast")
        state = tsb.make_state(d, sections=("sec0", "sec1", "sec2"))
        patch = tsb.run_plan(state)
        state.update(patch)
        return d, state, state["batch_id"], state["dispatch_plan"]["requests_full"]

    def test_t2_write_payload(self):
        d, state, batch_id, requests = self._t2_write_batch()
        plan = state["dispatch_plan"]
        payload = sbn._batch_interrupt_payload(state, plan, requests, batch_id)
        self.assertEqual(payload["dispatch_args"],
                         {"self_dir": d, "batch_id": batch_id})
        self.assertEqual(payload["batch_id"], batch_id)
        self.assertEqual(payload["dispatch_batch_mode"], "single-call-per-batch")

    def test_t2_check_payload(self):
        with tempfile.TemporaryDirectory() as d:
            tsn.write_sec(d, "sec1")
            tsn.write_sec(d, "sec2")
            state = tsn.make_check_state(d, sections=("sec1", "sec2"))
            patch, _ = tsn.run_plan_check(state)
            state.update(patch)
            batch_id = patch["batch_id"]
            plan = state["dispatch_plan"]
            requests = plan["requests_full"]
            payload = sn._check_interrupt_payload(state, plan, requests, batch_id)
            self.assertEqual(payload["dispatch_args"],
                             {"self_dir": d, "batch_id": batch_id})

    def test_t4_payload(self):
        with tempfile.TemporaryDirectory() as d:
            sbn._reset_pool()
            self.addCleanup(sbn._reset_pool)
            self.addCleanup(tt4.register_capable_worker(8))
            self_dir = tt4.make_self(d, ("sec0", "sec1", "sec2"))
            state = tt4.base_state(self_dir, ("sec0", "sec1", "sec2"))
            with mock.patch.object(t4nodes, "ScriptRunner",
                                   tt4.fake_runner("PASS")):
                patch = t4nodes.node_t4_plan_review(state)
            state.update(patch)
            batch_id = patch["batch_id"]
            plan = state["dispatch_plan"]
            requests = plan["requests_full"]
            payload = t4nodes._t4_batch_interrupt_payload(
                state, plan, requests, batch_id)
            self.assertEqual(payload["dispatch_args"],
                             {"self_dir": self_dir, "batch_id": batch_id})


# ---------------------------------------------------------------------------
# INTR-02：单请求载荷
# ---------------------------------------------------------------------------

class TestSingleRequestPayload(unittest.TestCase):

    def test_single_payload_uses_request_ref(self):
        with tempfile.TemporaryDirectory() as d:
            tsb.write_config(d)
            state = tsb.make_state(d, sections=("sec1",))
            req = agent_nodes.build_request(
                state, stage="T2_REPAIR_SEC1", role="author",
                prompt_file="subagent_prompt.md", artifacts=["sec1.tex"],
                instruction="返修 sec1", command="true", scope="sec1")
            payload = agent_nodes._interrupt_payload(req)
            self.assertEqual(payload["type"], "AGENT_DISPATCH")
            self.assertEqual(payload["dispatch_batch_mode"], "single-request")
            self.assertEqual(
                payload["dispatch_args"],
                {"request_ref": f".pd/agent-requests/{req['dispatch_id']}.json"})
            self.assertNotIn("batch_id", payload["dispatch_args"])


# ---------------------------------------------------------------------------
# INTR-03：真实 interrupt() 捕获（穿过 join 节点真实下发路径）
# ---------------------------------------------------------------------------

class TestRealInterruptCarriesDispatchArgs(unittest.TestCase):

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        tsb.write_config(self.d, agent_dispatch="fast")
        self.state = tsb.make_state(self.d, sections=("sec1", "sec2", "sec3"))
        self.state.update(tsb.run_plan(self.state))
        self.batch_id = self.state["batch_id"]
        sbn._reset_pool()  # manual 模式：无活池
        self.captured = []
        original = sbn.interrupt
        sbn.interrupt = lambda payload: self.captured.append(payload) or {}
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))

    def test_join_interrupt_payload_has_dispatch_args(self):
        patch = sbn.node_t2_join_sections(self.state)
        self.assertEqual(len(self.captured), 1)
        payload = self.captured[0]
        self.assertEqual(payload["batch_id"], self.batch_id)
        self.assertEqual(payload["dispatch_args"],
                         {"self_dir": self.d, "batch_id": self.batch_id})
        self.assertEqual(patch["manual_interrupted_batch"], self.batch_id)


if __name__ == "__main__":
    unittest.main()
