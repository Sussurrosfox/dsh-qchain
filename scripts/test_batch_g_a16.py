#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_g_a16.py — A16：认证过的「无变化」不得判死。

A16 原文（bug.md）::

    agent_nodes.py:359-381：after==request['before'] 且全部 artifacts 未变
    → no_change → HALTED。dependency replan 中子 Agent 逐字重写同一份
    dependency-map.md 即触发（resume 从 HALTED 是 no-op，只能删 checkpoint 链）。

判死口径本身是对的（B2：陈旧产物不得被静默接受），错在把两种完全不同的
情况混为一谈：

    A. 本次派发什么都没发生——磁盘上是上一轮留下的陈旧件；
    B. 子 Agent 跑了、写了盘，只是写出来的内容和原来逐字节相同。

B 在 replan / 重规划类阶段是**正确结果**。判别式只能是证据台账：
``qchain_dispatch`` 派发成功时会把 ``artifacts_written`` 与写盘后立即计算的
``output_digests`` 落进 ``.pd/agent-calls/<dispatch_id>.json``。本次只补这一条
认证通道，无证据 / 证据状态非 ok / 覆盖不全 / 摘要不符一律仍按 no_change 失败
（安全方向不变，见 T-A16-01）。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.nodes import agent_evidence, agent_nodes  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402


def make_state(d):
    return {
        "self_dir": d,
        "project_root": d,
        "run_id": "test-run",
        "expected_sections": ["sec1"],
        "section_statuses": {},
        "user_spec": {"main_title": "测试学案"},
        "history": [],
        "dispatch_results": {},
        "dispatch_metrics": {},
        "dispatch_history": [],
    }


def write_config(d, allow_unattested=False):
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("agent_dispatch:\n  allow_unattested: %s\n"
                % ("true" if allow_unattested else "false"))


def build_req(state, scope="sec1", artifacts=("sec1.tex",)):
    return agent_nodes.build_request(
        state,
        stage="T2_%s" % scope.upper(),
        role="%s 切片制作专员" % scope,
        prompt_file="subagent_prompt.md",
        artifacts=list(artifacts),
        instruction="重规划 %s" % scope,
        command="python scripts/pdstate.py self/ submit %s" % scope,
        scope=scope,
    )


def write_artifact(d, rel, text="% 内容\n"):
    with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
        f.write(text)
    return sbn.sha256_file(os.path.join(d, rel))


def seed_evidence(d, req, digests=None, worker="subagent:test-agent",
                  status="ok", artifacts=None):
    rels = [a for a in (req.get("artifacts") or []) if isinstance(a, str)]
    if digests is None:
        digests = {rel: sbn.sha256_file(os.path.join(d, rel)) for rel in rels}
    return agent_evidence.write_evidence(
        d,
        dispatch_id=req["dispatch_id"],
        batch_id=req.get("batch_id"),
        worker=worker,
        artifacts_written=artifacts if artifacts is not None else rels,
        output_digests=digests,
        status=status,
    )


class TestNoChangeAttested(unittest.TestCase):
    """A16：no_change 的认证通道（正向放行 + 四类反向仍判死）。"""

    def setUp(self):
        """生产路径是 interrupt（无进程内认知 worker），统一打桩。"""
        original = agent_nodes.interrupt
        agent_nodes.interrupt = lambda payload: {}
        self.addCleanup(lambda: setattr(agent_nodes, "interrupt", original))

    def _run_dispatch(self, state, req):
        return agent_nodes.run_dispatch(
            state, req, success_stage="T2_SECTIONS_STAGED",
            success_history="T2: sec1 切片已由派发池完成")

    def _no_change_request(self, d):
        """构造 before == after（产物先落盘，再据此生成派发单）。"""
        write_config(d)
        state = make_state(d)
        digest = write_artifact(d, "sec1.tex")
        req = build_req(state)
        self.assertEqual(req["before"]["sec1.tex"], digest)
        return state, req, digest

    # ---- 反向：安全方向不变 ----
    def test_a16_01_unattested_no_change_still_fails(self):
        """无证据的空转仍判 no_change → HALTED（B2 口径不许放松）。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            status, _ = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": digest}, allow_unattested=True)
            self.assertEqual(status, "no_change")
            self.assertFalse(agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest})[0])
            patch = self._run_dispatch(state, req)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertEqual(
                patch["dispatch_results"][req["dispatch_id"]]["status"],
                "no_change")
            ok_scopes, failures, _entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, [])
            self.assertEqual(failures, {"sec1": "no_change"})

    def test_a16_02_evidence_not_ok_still_fails(self):
        """证据 status != ok（未完成派发）→ 不算认证，仍判 no_change。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            seed_evidence(d, req, status="failed")
            self.assertFalse(agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest})[0])
            patch = self._run_dispatch(state, req)
            self.assertEqual(patch["stage"], "HALTED")

    def test_a16_03_evidence_without_worker_still_fails(self):
        """半伪造证据（无写入者身份）不放行——防「造个台账就当跑过」。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            seed_evidence(d, req, worker="")
            self.assertFalse(agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest})[0])

    def test_a16_04_partial_coverage_still_fails(self):
        """多产物派发里只写了一部分 → 覆盖不全，仍判 no_change。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            digest = write_artifact(d, "sec1.tex")
            req = build_req(state, artifacts=("sec1.tex", "sec2.tex"))
            seed_evidence(d, req, artifacts=["sec1.tex"])
            self.assertFalse(agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest, "sec2.tex": digest})[0])

    def test_a16_05_digest_mismatch_still_fails(self):
        """证据摘要与磁盘不符 → attested_mismatch 语义，不放行。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            seed_evidence(d, req, digests={"sec1.tex": "f" * 64})
            self.assertFalse(agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest})[0])

    # ---- 正向：认证过的逐字重写放行 ----
    def test_a16_06_run_dispatch_proceeds_on_attested_no_change(self):
        """run_dispatch：认证过的无变化 → PROCEED，不再 HALTED。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            seed_evidence(d, req)
            ok, evidence = agent_evidence.no_change_is_attested(
                d, req, {"sec1.tex": digest})
            self.assertTrue(ok)
            self.assertEqual(evidence["worker"], "subagent:test-agent")
            patch = self._run_dispatch(state, req)
            self.assertEqual(patch["routing_action"], "PROCEED")
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "success")
            self.assertTrue(entry["no_change"])
            self.assertEqual(entry["no_change_attested_by"],
                             "subagent:test-agent")
            self.assertIsNone(patch.get("pending_dispatch"))

    def test_a16_07_disk_rederivation_accepts_attested_no_change(self):
        """磁盘重推导（resume 路径）同样认认证过的无变化，不再返修。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            seed_evidence(d, req)
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, ["sec1"])
            self.assertEqual(failures, {})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["verified_via"], "agent")
            self.assertEqual(entry["no_change_attested_scopes"], ["sec1"])

    def test_a16_08_interrupt_resume_path_accepts_attested_no_change(self):
        """生产唯一路径（interrupt 恢复）也要能过：不再判 no_change 死。"""
        with tempfile.TemporaryDirectory() as d:
            state, req, digest = self._no_change_request(d)
            # qchain_dispatch 已把产物（逐字重写）与证据落盘
            seed_evidence(d, req)
            original = agent_nodes.interrupt
            agent_nodes.interrupt = lambda payload: {}
            self.addCleanup(lambda: setattr(agent_nodes, "interrupt", original))
            patch = self._run_dispatch(state, req)
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertTrue(
                patch["dispatch_results"][req["dispatch_id"]]["no_change"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
