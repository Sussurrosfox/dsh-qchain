#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_dispatch_contract.py — P0 派发硬合同与可观测字段回归测试（unittest + 临时目录）

覆盖 T1–T6；只构造临时工程与内存 worker 替身，不触碰真实 self/。
P0 范围仅限"可观测性 + 硬合同"，严禁引入并发（无线程/无进程池）——
本测试对 run_dispatch 的验证沿用其既有快照/未变更判定逻辑，不为迁就测试
而改动生产控制流。

运行: python -m unittest discover scripts -p "test_dispatch_contract.py" -v
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.nodes.agent_nodes import (  # noqa: E402
    build_request,
    write_request_file,
    run_dispatch,
)
from workflow.state import ShrinkCoursewareState  # noqa: E402
from workflow.adapters.worker_registry import worker_registry  # noqa: E402

# P0 硬合同新增键（每个 request dict 必须携带）
CONTRACT_KEYS = [
    "batch_id", "scope", "input_digest", "timeout_seconds",
    "max_retries", "retry_class", "attempt", "status",
]
# 既有键（P0 不得破坏）
LEGACY_KEYS = [
    "dispatch_id", "stage", "role", "prompt_file", "artifacts",
    "before", "instruction", "command", "context", "run_id",
]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def make_state(d: str) -> dict:
    """最小 state：派发根目录指向临时工程。"""
    return {"self_dir": d, "project_root": d, "run_id": "test-run"}


def base_kwargs() -> dict:
    """build_request 的关键字参数（不含 P0 新增可选项）。"""
    return dict(
        stage="TEST_STAGE",
        role="测试专员",
        prompt_file="subagent_prompt.md",
        artifacts=["sec1.tex"],
        instruction="请按照黄金模板撰写 sec1.tex。",
        command="python scripts/pdstate.py self/ submit sec1",
        context={"section_id": "sec1"},
    )


class T01_ContractKeys(unittest.TestCase):
    """T1: 硬合同键齐全、类型正确、默认值生效。"""

    def test_t01_keys_types_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            req = build_request(make_state(d), **base_kwargs())
            for k in CONTRACT_KEYS + LEGACY_KEYS:
                self.assertIn(k, req)
            # 默认值
            self.assertEqual(req["scope"], "")
            self.assertEqual(req["timeout_seconds"], 900)
            self.assertEqual(req["max_retries"], 2)
            self.assertEqual(req["retry_class"], "transient")
            self.assertEqual(req["status"], "pending")
            self.assertEqual(req["attempt"], 0)
            # 类型
            self.assertIsInstance(req["batch_id"], str)
            self.assertIsInstance(req["input_digest"], str)
            self.assertIsInstance(req["timeout_seconds"], int)
            self.assertIsInstance(req["max_retries"], int)
            self.assertIsInstance(req["retry_class"], str)
            self.assertIsInstance(req["attempt"], int)
            self.assertIsInstance(req["status"], str)
            # 既有键语义不被破坏
            self.assertEqual(req["stage"], "TEST_STAGE")
            self.assertEqual(req["artifacts"], ["sec1.tex"])
            self.assertEqual(req["run_id"], "test-run")


class T02_InputDigest(unittest.TestCase):
    """T2: input_digest 为 64 位小写十六进制、确定性、显式值被尊重。"""

    def test_t02_digest_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            r1 = build_request(state, **base_kwargs())
            r2 = build_request(state, **base_kwargs())
            self.assertRegex(r1["input_digest"], HEX64)
            # 同一组输入 → 同一指纹
            self.assertEqual(r1["input_digest"], r2["input_digest"])
            # 指令改变 → 指纹改变
            changed = base_kwargs()
            changed["instruction"] = "改写的指令：仅修改环境闭合。"
            r3 = build_request(state, **changed)
            self.assertNotEqual(r1["input_digest"], r3["input_digest"])
            # 显式传入的指纹被尊重（不重新计算）
            explicit = build_request(state, input_digest="a" * 64, **base_kwargs())
            self.assertEqual(explicit["input_digest"], "a" * 64)


class T03_BatchId(unittest.TestCase):
    """T3: batch_id 显式值跨请求保持；缺省生成互异非空 id。"""

    def test_t03_batch_id_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            kw = base_kwargs()
            kw["batch_id"] = "bat-explicit-001"
            r1 = build_request(state, **kw)
            r2 = build_request(state, **kw)
            self.assertEqual(r1["batch_id"], "bat-explicit-001")
            self.assertEqual(r2["batch_id"], "bat-explicit-001")
            g1 = build_request(state, **base_kwargs())["batch_id"]
            g2 = build_request(state, **base_kwargs())["batch_id"]
            self.assertTrue(g1)
            self.assertTrue(g2)
            self.assertNotEqual(g1, g2)


class T04_WriteRequestFile(unittest.TestCase):
    """T4: write_request_file 落盘合同字段、JSON 往返一致、dispatch_id 形态。"""

    def test_t04_persist_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            kw = base_kwargs()
            kw["scope"] = "sec1"
            req = build_request(state, **kw)
            rel = write_request_file(d, req)
            self.assertEqual(
                rel, f".pd/agent-requests/{req['dispatch_id']}.json")
            with open(os.path.join(d, rel.replace("/", os.sep)),
                      "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # JSON 往返一致（合同字段全部持久化）
            self.assertEqual(loaded, req)
            self.assertEqual(loaded["scope"], "sec1")
            self.assertEqual(loaded["timeout_seconds"], 900)
            self.assertEqual(loaded["retry_class"], "transient")
            self.assertRegex(loaded["input_digest"], HEX64)
            # dispatch_id 形态：带 scope 时包含 scope
            self.assertRegex(
                req["dispatch_id"], r"^dsp-test-stage-sec1-[0-9a-f]{10}$")
            # 不带 scope 时退回旧形态
            req2 = build_request(state, **base_kwargs())
            self.assertRegex(req2["dispatch_id"], r"^dsp-test-stage-[0-9a-f]{10}$")


class T05_RunDispatch(unittest.TestCase):
    """T5: worker 替身改变产物 → ok；产物无变化 → no_change（沿用既有判定逻辑）。"""

    def setUp(self):
        # worker_registry 是进程级单例：用后必须清除，避免污染其他用例
        self.addCleanup(worker_registry.clear)

    def _dispatch(self, worker_files: dict):
        """构造临时工程 + 注册假 worker + 执行 run_dispatch，返回 (req, res)。"""
        d_ctx = tempfile.TemporaryDirectory()
        self.addCleanup(d_ctx.cleanup)
        d = d_ctx.name
        state = make_state(d)
        art = "sec1.tex"
        with open(os.path.join(d, art), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("% v1 初始内容\n")
        req = build_request(state, **base_kwargs())
        worker_registry.set(
            lambda sys_prompt, user_content: json.dumps({"files": worker_files}),
            name="fake-worker",
        )
        res = run_dispatch(
            state,
            req,
            success_stage="T2_SECTIONS_STAGED",
            success_history="T2: 切片 sec1 候选文件已就绪",
        )
        return req, res

    def test_t05_worker_changes_artifact_ok(self):
        req, res = self._dispatch({"sec1.tex": "% v2 已被 worker 改写\n"})
        self.assertEqual(res["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(res["routing_action"], "PROCEED")
        self.assertEqual(
            res["dispatch_results"][req["dispatch_id"]]["status"], "success")
        self.assertEqual(
            res["dispatch_results"][req["dispatch_id"]]["dispatched_via"],
            "worker")

    def test_t05_no_change_fails(self):
        req, res = self._dispatch({"sec1.tex": "% v1 初始内容\n"})
        self.assertEqual(res["stage"], "HALTED")
        self.assertEqual(res["routing_action"], "HALT")
        self.assertEqual(
            res["dispatch_results"][req["dispatch_id"]]["status"], "no_change")
        self.assertIn("未发生变化", res["error_message"])


class T06_StateFields(unittest.TestCase):
    """T6: ShrinkCoursewareState 声明 P0 可观测字段。"""

    def test_t06_state_annotations(self):
        ann = ShrinkCoursewareState.__annotations__
        for k in ("dispatch_plan", "batch_id", "dispatch_metrics",
                  "dispatch_history", "agent_dispatch_profile"):
            self.assertIn(k, ann)


if __name__ == "__main__":
    unittest.main(verbosity=2)
