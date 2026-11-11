#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_ledger.py — 批次 E（N1 批准写入通路 / N2 advisory 延期）回归

N1 现场：``.pd/approvals.json`` 只有 check_fidelity 在读，全仓没有写入者 ——
faithful 模式下 BRIDGE 永远拿不到批准，返修子 Agent 每轮新写的桥接都被判
FIDELITY_BRIDGE_UNAPPROVED。本测试锁死三件事：
  1. 台账可写、可读、可撤销，撤销后门禁侧**立即**不再放行；
  2. 批准/延期必须带理由与真人署名（Agent 标识被拒）；
  3. 延期能把"无解 advisory"从 blocking 降级，且降级原因随证据留痕。

运行: python -B -m pytest -q -p no:cacheprovider scripts/test_ledger.py
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
for path in (HERE, PLUGIN_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import check_fidelity  # noqa: E402
import check_semantic  # noqa: E402
import ledger  # noqa: E402
import review_projection  # noqa: E402


def make_self(tmp, section_digest="d0", issues=None, conclusion="REVISE"):
    """造一个 self 夹具：sec1.tex + 投影后的语义证据。"""
    os.makedirs(os.path.join(tmp, ".pd", "semantic"), exist_ok=True)
    tex = os.path.join(tmp, "sec1.tex")
    with open(tex, "w", encoding="utf-8") as f:
        f.write("% source: SRC-001\n\\section{fixture}\n")
    real_digest = hashlib.sha256(open(tex, "rb").read()).hexdigest()
    section = {"scope": "sec1", "section_digest": real_digest,
               "issues": issues if issues is not None else [
                   {"code": "REVIEW_SOURCE_COMPARISON", "qid": "Q1",
                    "status": "open", "claim": "第 7 题口径与源材料不一致"}]}
    return tmp, section, real_digest


def project(self_dir, section, conclusion="REVISE"):
    payload = review_projection.project_semantic(
        {"reviewed_at": "2026-11-11T00:00:00", "dispatch_id": "d1",
         "scopes": ["sec1"]},
        ".pd/combined-review/b1-r1.json", "sha", section, self_dir=self_dir)
    payload["conclusion"] = conclusion
    return payload


class TLedgerApprove(unittest.TestCase):
    """N1：批准台账的写入 / 校验 / 撤销。"""

    def test_approve_writes_readable_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = ledger.approve(tmp, "bridge", "sec1", "原文备注给出等价说明",
                                   "张三")
            self.assertEqual(entry["id"], "BR-sec1-01")
            self.assertEqual(entry["status"], "active")
            on_disk = json.load(open(os.path.join(tmp, ".pd", "approvals.json"),
                                     encoding="utf-8"))
            self.assertEqual(on_disk["approvals"][0]["reason"],
                             "原文备注给出等价说明")
            self.assertEqual(on_disk["approvals"][0]["approved_by"], "张三")
            self.assertEqual(len(ledger.load_approvals(tmp)), 1)

    def test_reason_and_human_are_mandatory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.approve(tmp, "bridge", "sec1", "  ", "张三")
            self.assertIn("LEDGER_REASON_REQUIRED", str(ctx.exception))
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.approve(tmp, "bridge", "sec1", "理由", "")
            self.assertIn("LEDGER_APPROVED_BY_REQUIRED", str(ctx.exception))
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.approve(tmp, "bridge", "sec1", "理由", "subagent:author-1")
            self.assertIn("LEDGER_APPROVED_BY_NOT_HUMAN", str(ctx.exception))

    def test_duplicate_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.approve(tmp, "bridge", "sec1", "理由一", "张三", approval_id="BR-001")
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.approve(tmp, "bridge", "sec1", "理由二", "李四", approval_id="BR-001")
            self.assertIn("LEDGER_ID_DUPLICATE", str(ctx.exception))

    def test_revoked_approval_stops_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.approve(tmp, "bridge", "sec1", "理由", "张三", approval_id="BR-001")
            raw = json.load(open(os.path.join(tmp, ".pd", "approvals.json"),
                                 encoding="utf-8"))
            self.assertEqual(len(check_fidelity.active_approval_entries(raw)), 1)
            ledger.revoke(tmp, "BR-001", "张三")
            raw = json.load(open(os.path.join(tmp, ".pd", "approvals.json"),
                                 encoding="utf-8"))
            # 历史条目保留（可追责），但门禁侧不再放行
            self.assertEqual(len(raw["approvals"]), 1)
            self.assertEqual(raw["approvals"][0]["status"], "revoked")
            self.assertEqual(check_fidelity.active_approval_entries(raw), [])

    def test_corrupt_ledger_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".pd"), exist_ok=True)
            path = os.path.join(tmp, ".pd", "approvals.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ not json")
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.approve(tmp, "bridge", "sec1", "理由", "张三")
            self.assertIn("LEDGER_CORRUPT", str(ctx.exception))
            self.assertEqual(open(path, encoding="utf-8").read(), "{ not json")


class TLedgerDefer(unittest.TestCase):
    """N2：延期台账的匹配口径与到期/撤销语义。"""

    def test_defer_requires_target_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.defer(tmp, "sec1", "理由", "张三")
            self.assertIn("LEDGER_TARGET_REQUIRED", str(ctx.exception))
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.defer(tmp, "sec1", "", "张三", code="REVIEW_X")
            self.assertIn("LEDGER_REASON_REQUIRED", str(ctx.exception))
            with self.assertRaises(ledger.LedgerError) as ctx:
                ledger.defer(tmp, "sec1", "理由", "张三", code="REVIEW_X",
                             expires_at="2026/11/11")
            self.assertIn("LEDGER_EXPIRES_INVALID", str(ctx.exception))

    def test_match_by_code_or_qid_within_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.defer(tmp, "sec1", "源材料未给该口径", "张三",
                         code="REVIEW_SOURCE_COMPARISON")
            hit = ledger.find_deferral(tmp, scope="sec1",
                                       code="REVIEW_SOURCE_COMPARISON")
            self.assertIsNotNone(hit)
            # scope 不符不匹配（延期不得漫出小节）
            self.assertIsNone(ledger.find_deferral(tmp, scope="sec2",
                                                   code="REVIEW_SOURCE_COMPARISON"))
            # 同节其它 code 不受影响
            self.assertIsNone(ledger.find_deferral(tmp, scope="sec1",
                                                   code="REVIEW_OTHER"))

    def test_expired_or_revoked_deferral_is_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = ledger.defer(tmp, "sec1", "临时豁免", "张三",
                                 code="REVIEW_X", expires_at="2020-01-01")
            self.assertIsNone(ledger.find_deferral(tmp, scope="sec1", code="REVIEW_X"))
            ledger.revoke(tmp, entry["id"], "张三")
            self.assertIsNone(ledger.find_deferral(tmp, scope="sec1", code="REVIEW_X"))


class TDeferralProjection(unittest.TestCase):
    """N2：延期让 blocking 降级为 advisory，且理由留痕。"""

    def test_open_issue_becomes_advisory_when_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp, section, _ = make_self(tmp)
            before = project(tmp, section)["issues"][0]
            self.assertEqual(before["severity"], "blocking")
            self.assertNotIn("deferred", before)
            ledger.defer(tmp, "sec1", "源材料本身未给第 7 题口径", "张三",
                         qid="Q1")
            after = project(tmp, section)["issues"][0]
            self.assertEqual(after["severity"], "advisory")
            self.assertTrue(after["deferred"])
            self.assertEqual(after["deferral"]["reason"], "源材料本身未给第 7 题口径")
            self.assertEqual(after["deferral"]["deferred_by"], "张三")
            # 原 status 不被篡改（延期不等于修好了）
            self.assertEqual(after["status"], "open")

    def test_projection_without_deferral_is_byte_identical_to_before(self):
        # 存量证据要能继续被 verify_projection 复算：没有延期时不得多出键
        with tempfile.TemporaryDirectory() as tmp:
            tmp, section, _ = make_self(tmp)
            payload = project(tmp, section)
            self.assertNotIn("deferred", payload["issues"][0])
            self.assertNotIn("deferral", payload["issues"][0])


class TDeferralSemanticGate(unittest.TestCase):
    """N2：全 open issue 被延期时，语义门禁提示而不阻断。"""

    def _write(self, tmp, payload):
        path = os.path.join(tmp, ".pd", "semantic", "sec1.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_revise_with_only_deferred_issues_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp, section, _ = make_self(tmp)
            self._write(tmp, project(tmp, section))
            errors = check_semantic.check_scope(tmp, "sec1")
            self.assertEqual([e["code"] for e in errors], ["SEMANTIC_REVISE"])

            ledger.defer(tmp, "sec1", "源材料未给该口径", "张三", qid="Q1")
            self._write(tmp, project(tmp, section))
            warnings = []
            errors = check_semantic.check_scope(tmp, "sec1", warnings=warnings)
            self.assertEqual(errors, [])
            self.assertEqual([w["code"] for w in warnings],
                             ["SEMANTIC_REVISE_DEFERRED"])
            self.assertIn("源材料未给该口径", warnings[0]["detail"])

    def test_single_deferred_issue_does_not_excuse_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp, section, _ = make_self(tmp, issues=[
                {"code": "REVIEW_SOURCE_COMPARISON", "qid": "Q1",
                 "status": "open", "claim": "口径不一致"},
                {"code": "REVIEW_MATH_ERROR", "qid": "Q2",
                 "status": "open", "claim": "结论算错"}])
            ledger.defer(tmp, "sec1", "口径问题先放着", "张三", qid="Q1")
            payload = project(tmp, section)
            sev = {i["code"]: i["severity"] for i in payload["issues"]}
            self.assertEqual(sev["REVIEW_SOURCE_COMPARISON"], "advisory")
            self.assertEqual(sev["REVIEW_MATH_ERROR"], "blocking")
            # 计算错误那条仍是 blocking → 整体仍阻断
            self._write(tmp, payload)
            warnings = []
            errors = check_semantic.check_scope(tmp, "sec1", warnings=warnings)
            self.assertEqual([e["code"] for e in errors], ["SEMANTIC_REVISE"])
            self.assertEqual(warnings, [])


class TDispatchPromptWording(unittest.TestCase):
    """N2：返修提示词不得再写"未修的 advisory 一律升级为 blocking"。"""

    def test_prompt_no_longer_threatens_blanket_escalation(self):
        source = open(os.path.join(PLUGIN_ROOT, "scripts", "dispatch_runtime.mjs"),
                      encoding="utf-8").read()
        self.assertNotIn("未修的 advisory 在下一轮审读会被升级为 blocking", source)
        self.assertIn("[deferred]", source)
        self.assertIn("本轮不要动它", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
