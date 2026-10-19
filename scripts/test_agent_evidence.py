#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_agent_evidence.py — q_audit.md 多 Agent 证据链回归测试（T-EV-01~10）

覆盖证据台账 .pd/agent-calls/<dispatch_id>.json 的读写对账、磁盘重推导判级、
中断载荷协议防伪造、T4 创审分离（INV-2）与 check_agent_audit.py 门禁。

测试对象：
    workflow/nodes/agent_evidence.py（证据读写对账）
    workflow/nodes/section_batch_nodes.py::_verify_requests_from_disk（判级）
    workflow/nodes/agent_nodes.py::run_dispatch（协议拦截）
    workflow/nodes/t4_review_batch_nodes.py::_reviewer_identity_violation
    scripts/check_agent_audit.py（门禁 CLI）

运行: python -m unittest discover scripts -p "test_agent_evidence.py" -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.nodes import agent_evidence  # noqa: E402
from workflow.nodes import agent_nodes  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes import t4_review_batch_nodes as t4  # noqa: E402


# ---------------------------------------------------------------------------
# 治具
# ---------------------------------------------------------------------------

def make_state(d):
    """最小 state：self_dir 指向临时工程。"""
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
    """写一份带 agent_dispatch 映射块（含 allow_unattested）的 config.yaml。"""
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("agent_dispatch:\n")
        f.write("  profile: safe\n")
        f.write("  allow_unattested: %s\n"
                % ("true" if allow_unattested else "false"))


def build_req(state, scope="sec1", artifact="sec1.tex", stage=None):
    return agent_nodes.build_request(
        state,
        stage=stage or "T2_%s" % scope.upper(),
        role="%s 切片制作专员" % scope,
        prompt_file="subagent_prompt.md",
        artifacts=[artifact],
        instruction="撰写切片 %s" % scope,
        command="python scripts/pdstate.py self/ submit %s" % scope,
        scope=scope,
    )


def write_artifact(d, rel, text="% 切片内容\n"):
    with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
        f.write(text)
    return sbn.sha256_file(os.path.join(d, rel))


def disk_digest(d, rel):
    return sbn.sha256_file(os.path.join(d, rel))


def seed_evidence(d, req, digests=None, worker="subagent:test-agent",
                  batch_id=None, status="ok"):
    """模拟 qchain_dispatch 的证据台账落盘（摘要缺省按盘面现值计算）。"""
    rels = [a for a in (req.get("artifacts") or []) if isinstance(a, str)]
    digests = digests or {rel: disk_digest(d, rel) for rel in rels}
    return agent_evidence.write_evidence(
        d,
        dispatch_id=req["dispatch_id"],
        batch_id=batch_id if batch_id is not None else req.get("batch_id"),
        worker=worker,
        artifacts_written=[r for r in rels if digests.get(r)],
        output_digests=digests,
        status=status,
    )


def run_gate(d, extra_args=()):
    """跑 check_agent_audit.py，返回 (returncode, report_dict)。"""
    report_path = os.path.join(d, ".pd", "reports", "check_agent_audit.json")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "check_agent_audit.py"), d,
         "--json", report_path] + list(extra_args),
        capture_output=True, text=True)
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    return proc.returncode, report


# ---------------------------------------------------------------------------
# T-EV-01~06：证据对账与磁盘重推导判级
# ---------------------------------------------------------------------------

class TEV_EvidenceLedger(unittest.TestCase):
    """T-EV-01~06：证据台账对账 / ok_unattested / 逃生阀 / mismatch / no_change / missing。"""

    def test_ev01_attested_ok_verified_via_agent(self):
        """T-EV-01: 证据吻合 → ok，verified_via=agent。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            req = build_req(state)
            digest = write_artifact(d, "sec1.tex")
            seed_evidence(d, req)
            status, evidence = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": digest}, allow_unattested=False)
            self.assertEqual(status, "ok")
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["worker"], "subagent:test-agent")
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, ["sec1"])
            self.assertEqual(failures, {})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["verified_via"], "agent")
            self.assertNotIn("unattested", entry)

    def test_ev02_unattested_enters_failures(self):
        """T-EV-02: 主 Agent 直写无证据 → ok_unattested 进 failures。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, allow_unattested=False)
            state = make_state(d)
            req = build_req(state)
            digest = write_artifact(d, "sec1.tex", "% 主线程直写\n")
            status, evidence = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": digest}, allow_unattested=False)
            self.assertEqual(status, "ok_unattested")
            self.assertIsNone(evidence)
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, [])
            self.assertEqual(failures, {"sec1": "ok_unattested"})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["verified_via"], "unattested")
            self.assertIn("产物无代理证据", entry["error"])

    def test_ev03_escape_valve_allows_with_flag(self):
        """T-EV-03: allow_unattested=true 放行，entry.unattested=true。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, allow_unattested=True)
            state = make_state(d)
            req = build_req(state)
            digest = write_artifact(d, "sec1.tex", "% 直写放行\n")
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, ["sec1"])
            self.assertEqual(failures, {})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["status"], "ok_unattested")
            self.assertTrue(entry["unattested"])
            self.assertEqual(entry["verified_via"], "unattested")

    def test_ev04_attested_mismatch_fails_without_escape(self):
        """T-EV-04: 证据在案但摘要不符 → attested_mismatch，逃生阀不救。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, allow_unattested=True)   # 逃生阀对 mismatch 无效
            state = make_state(d)
            req = build_req(state)
            write_artifact(d, "sec1.tex")
            seed_evidence(d, req, digests={"sec1.tex": "0" * 64})
            digest = disk_digest(d, "sec1.tex")
            status, evidence = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": digest}, allow_unattested=True)
            self.assertEqual(status, "attested_mismatch")
            self.assertIsNotNone(evidence)
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(
                d, [req], allow_unattested=True)
            self.assertEqual(ok_scopes, [])
            self.assertEqual(failures, {"sec1": "attested_mismatch"})
            self.assertEqual(entries[req["dispatch_id"]]["verified_via"], "n/a")

    def test_ev05_no_change_fails(self):
        """T-EV-05: 产物未变化（before==after）→ no_change。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            digest = write_artifact(d, "sec1.tex")   # 先落盘再派发：快照==现值
            req = build_req(state)
            self.assertEqual(req["before"]["sec1.tex"], digest)
            status, evidence = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": digest}, allow_unattested=True)
            self.assertEqual(status, "no_change")
            self.assertIsNone(evidence)
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, [])
            self.assertEqual(failures, {"sec1": "no_change"})

    def test_ev06_missing_artifacts_fails(self):
        """T-EV-06: 产物缺失 → missing_artifacts。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            req = build_req(state)
            status, evidence = agent_evidence.check_request_attested(
                d, req, {"sec1.tex": None}, allow_unattested=True)
            self.assertEqual(status, "missing_artifacts")
            self.assertIsNone(evidence)
            ok_scopes, failures, entries = sbn._verify_requests_from_disk(d, [req])
            self.assertEqual(ok_scopes, [])
            self.assertEqual(failures, {"sec1": "missing_artifacts"})
            self.assertIn("产物未生成", entries[req["dispatch_id"]]["error"])


# ---------------------------------------------------------------------------
# T-EV-07~08：中断载荷协议防伪造
# ---------------------------------------------------------------------------

class TEV_DispatchProtocol(unittest.TestCase):
    """T-EV-07~08：载荷缺 dispatch_protocol 不拦截 / bogus protocol → HALT。"""

    def test_ev07_missing_protocol_does_not_block(self):
        """T-EV-07: 旧 checkpoint 无 dispatch_protocol（None）不拦截。

        兼容路径：中断响应不带协议字段（甚至不带 files），产物与证据已由
        qchain_dispatch 落盘 → 走磁盘重推导，正常放行。
        """
        self.assertIsNone(agent_evidence.dispatch_protocol_violation(None))
        self.assertIsNone(agent_evidence.dispatch_protocol_violation({}))
        self.assertIsNone(agent_evidence.dispatch_protocol_violation(
            {"files": {"sec1.tex": "x"}}))
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            req = build_req(state)
            # qchain_dispatch 已把产物与证据落盘（恢复前完成）
            write_artifact(d, "sec1.tex", "% 子 Agent 产物\n")
            seed_evidence(d, req)
            original = agent_nodes.interrupt
            agent_nodes.interrupt = lambda payload: {}   # 旧 checkpoint 恢复载荷
            self.addCleanup(lambda: setattr(agent_nodes, "interrupt", original))
            patch = agent_nodes.run_dispatch(
                state, req, success_stage="T2_SECTIONS_STAGED",
                success_history="T2: sec1 切片已由派发池完成")
            self.assertEqual(patch["routing_action"], "PROCEED")
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "success")
            self.assertEqual(entry["verified_via"], "agent")

    def test_ev08_bogus_protocol_halts(self):
        """T-EV-08: 声明非法 dispatch_protocol → 丢弃 files + HALT。"""
        detail = agent_evidence.dispatch_protocol_violation(
            {"dispatch_protocol": "evil-protocol", "files": {"sec1.tex": "x"}})
        self.assertIsNotNone(detail)
        self.assertIn("BAD_DISPATCH_PROTOCOL", detail)
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            req = build_req(state)
            original = agent_nodes.interrupt
            agent_nodes.interrupt = lambda payload: {
                "dispatch_protocol": "evil-protocol",
                "files": {"sec1.tex": "% 伪造产物\n"}}
            self.addCleanup(lambda: setattr(agent_nodes, "interrupt", original))
            patch = agent_nodes.run_dispatch(
                state, req, success_stage="T2_SECTIONS_STAGED",
                success_history="T2: sec1 切片已由派发池完成")
            self.assertEqual(patch["stage"], "HALTED")
            self.assertEqual(patch["routing_action"], "HALT")
            self.assertIn("BAD_DISPATCH_PROTOCOL", patch["error_message"])
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "bad_dispatch_protocol")
            # 伪造 files 被丢弃：不落盘、不落证据
            self.assertFalse(os.path.exists(os.path.join(d, "sec1.tex")))
            self.assertFalse(os.path.exists(os.path.join(
                d, ".pd", "agent-calls", "%s.json" % req["dispatch_id"])))


# ---------------------------------------------------------------------------
# T-EV-09：T4 创审分离（INV-2）
# ---------------------------------------------------------------------------

class TEV_ReviewerIdentity(unittest.TestCase):
    """T-EV-09: 验收者与写作者同体 → reviewer_identity_violation。"""

    def test_ev09_same_worker_violates_inv2(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            # T2 写作阶段证据：worker=A
            t2_req = build_req(state, scope="sec1", artifact="sec1.tex")
            seed_evidence(d, t2_req, worker="subagent:agent-a",
                          batch_id="bat-t2-abcd1234")
            # T4 验收阶段证据：同一 worker=A
            t4_req = build_req(state, scope="sec1",
                               artifact=".pd/acceptance/sec1.json",
                               stage="T4_REVIEW_SEC1")
            seed_evidence(d, t4_req, worker="subagent:agent-a",
                          batch_id="bat-t4-ef567890")
            violated, detail = t4._reviewer_identity_violation(d, t4_req)
            self.assertTrue(violated)
            self.assertTrue(detail.startswith("reviewer_identity_violation:"))
            self.assertIn("agent-a", detail)

    def test_ev09b_independent_reviewer_passes(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            t2_req = build_req(state, scope="sec1", artifact="sec1.tex")
            seed_evidence(d, t2_req, worker="subagent:agent-a",
                          batch_id="bat-t2-abcd1234")
            t4_req = build_req(state, scope="sec1",
                               artifact=".pd/acceptance/sec1.json",
                               stage="T4_REVIEW_SEC1")
            seed_evidence(d, t4_req, worker="subagent:agent-b",
                          batch_id="bat-t4-ef567890")
            violated, detail = t4._reviewer_identity_violation(d, t4_req)
            self.assertFalse(violated)
            self.assertEqual(detail, "")

    def test_ev09c_missing_t4_evidence_defers(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            t2_req = build_req(state, scope="sec1", artifact="sec1.tex")
            seed_evidence(d, t2_req, worker="subagent:agent-a")
            t4_req = build_req(state, scope="sec1",
                               artifact=".pd/acceptance/sec1.json",
                               stage="T4_REVIEW_SEC1")
            violated, detail = t4._reviewer_identity_violation(d, t4_req)
            self.assertFalse(violated)
            self.assertEqual(detail, "")


# ---------------------------------------------------------------------------
# T-EV-10：check_agent_audit.py 门禁 CLI
# ---------------------------------------------------------------------------

class TEV_GateCli(unittest.TestCase):
    """T-EV-10: 一份缺证据 + 一份齐 → 退出码 1，恰一条 EVIDENCE_MISSING。"""

    def test_ev10_gate_reports_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            req_a = build_req(state, scope="sec1", artifact="sec1.tex")
            req_b = build_req(state, scope="sec2", artifact="sec2.tex")
            req_dir = os.path.join(d, ".pd", "agent-requests")
            os.makedirs(req_dir, exist_ok=True)
            for req in (req_a, req_b):
                with open(os.path.join(req_dir, "%s.json" % req["dispatch_id"]),
                          "w", encoding="utf-8") as f:
                    json.dump(req, f, ensure_ascii=False, indent=2)
            write_artifact(d, "sec1.tex")
            write_artifact(d, "sec2.tex")
            seed_evidence(d, req_b, worker="subagent:test-agent")  # 仅 B 齐
            code, report = run_gate(d)
            self.assertEqual(code, 1)
            self.assertEqual(report["status"], "fail")
            errs = [i for i in report["issues"] if i["severity"] == "error"]
            codes = [i["code"] for i in errs]
            self.assertEqual(codes.count("EVIDENCE_MISSING"), 1)
            self.assertEqual(len(errs), 1)
            self.assertIn(req_a["dispatch_id"], errs[0]["message"])
            self.assertEqual(report["evidence"]["checked"]["dispatches"], 2)

    def test_ev10b_gate_passes_when_all_attested(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(d)
            req_a = build_req(state, scope="sec1", artifact="sec1.tex")
            req_dir = os.path.join(d, ".pd", "agent-requests")
            os.makedirs(req_dir, exist_ok=True)
            with open(os.path.join(req_dir, "%s.json" % req_a["dispatch_id"]),
                      "w", encoding="utf-8") as f:
                json.dump(req_a, f, ensure_ascii=False, indent=2)
            write_artifact(d, "sec1.tex")
            seed_evidence(d, req_a)
            code, report = run_gate(d)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "pass")
            errs = [i for i in report["issues"] if i["severity"] == "error"]
            self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
