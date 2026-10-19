#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_provenance.py — audit §7 provenance_gate（scripts/check_provenance.py）回归

覆盖：
    T01  无派发单工程 → PROV_NO_DISPATCH_CHAIN 警告放行（非多 Agent 路径）
    T02  完整血缘链通过（writer 台账认证当前摘要 + checker PASS + qid 已登记）
    T03  写作者证据缺失/摘要不符 → PROV_WRITER_DIGEST_MISSING（旧文件不复用）
    T04  切片文件缺失 → PROV_SECTION_MISSING
    T05  写域越界（main.tex/绝对路径/..） → PROV_PATH_OUTSIDE_ALLOWLIST
    T06  checker 血缘六态 → PROV_CHECKER_MISSING/INVALID/STALE/REVISE/NEEDS_EVIDENCE
    T07  qid 未登记 → PROV_QID_UNPLANNED
    T08  打乱血缘豁免（确定性选项重排后 writer/checker 链仍有效）
    T09  CLI 报告契约（schema_version 2 / input_digest / 退出码）

运行: python -m unittest discover scripts -p "test_provenance.py" -v
"""
import hashlib
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

import check_provenance  # noqa: E402
from workflow.nodes import agent_evidence  # noqa: E402

SEC1 = "\\section{sec1}\n\n% qid: sec1-q1\n\\begin{probchain}\n候选内容。\n\\end{probchain}\n"
SEC2 = "\\section{sec2}\n\n% qid: sec2-q1\n\\begin{probchain}\n候选内容二。\n\\end{probchain}\n"

COVERAGE = """# 覆盖规划（血缘门禁测试）

切片清单: sec1, sec2

## 规划节点总表

| sec1-q1 | sec1 | 目标 | 对应问题 | sec1-q1 |
| sec2-q1 | sec2 | 目标 | 对应问题 | sec2-q1 |
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_project(d, *, sections=("sec1", "sec2"), with_requests=True,
                 semantic=True, texts=None):
    """最小工程：main.tex + 切片 + coverage-map + 派发链 + 语义记录。"""
    self_dir = os.path.join(d, "self")
    os.makedirs(self_dir)
    bodies = texts or {"sec1": SEC1, "sec2": SEC2}
    _write(os.path.join(self_dir, "main.tex"),
           "".join("\\input{%s.tex}\n" % s for s in sections)
           + "\\end{document}\n")
    for sec in sections:
        _write(os.path.join(self_dir, sec + ".tex"), bodies[sec])
    _write(os.path.join(self_dir, "coverage-map.md"), COVERAGE)
    if with_requests:
        for sec in sections:
            dispatch_id = "dsp-T2_%s-test01" % sec.upper()
            req = {
                "dispatch_id": dispatch_id,
                "stage": "T2_%s" % sec.upper(),
                "scope": sec,
                "role": "%s 切片制作专员" % sec,
                "artifacts": ["%s.tex" % sec],
                "status": "ok",
                "attempt": 1,
            }
            _write(os.path.join(self_dir, ".pd", "agent-requests",
                                dispatch_id + ".json"),
                   json.dumps(req, ensure_ascii=False, indent=2))
            agent_evidence.write_evidence(
                self_dir,
                dispatch_id=dispatch_id,
                batch_id="bat-t2-test",
                worker="subagent:test-agent",
                artifacts_written=["%s.tex" % sec],
                output_digests={"%s.tex" % sec: sha256_file(
                    os.path.join(self_dir, sec + ".tex"))},
            )
    if semantic:
        for sec in sections:
            _write(os.path.join(self_dir, ".pd", "semantic", sec + ".json"),
                   json.dumps({
                       "schema_version": 1,
                       "scope": sec,
                       "section_digest": sha256_file(
                           os.path.join(self_dir, sec + ".tex")),
                       "conclusion": "PASS",
                       "issues": [],
                   }, ensure_ascii=False, indent=2))
    return self_dir


def codes(result):
    return [e.get("code") for e in result.get("errors", [])]


class T01NoDispatchChain(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_no_requests_warns_and_skips_writer_audit(self):
        self_dir = make_project(self.d, with_requests=False, semantic=False)
        result = check_provenance.check_provenance_project(self_dir)
        warn_codes = [w.get("code") for w in result.get("warnings", [])]
        self.assertIn("PROV_NO_DISPATCH_CHAIN", warn_codes)
        self.assertNotIn("PROV_WRITER_DIGEST_MISSING", codes(result))


class T02CompleteChain(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = make_project(self.d)

    def test_complete_chain_passes(self):
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertEqual(codes(result), [])
        checked = result["checked"]
        self.assertEqual(checked["certified"], ["sec1", "sec2"])
        self.assertEqual(checked["checker"], {"sec1": "ok", "sec2": "ok"})


class T03WriterChain(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = make_project(self.d)

    def test_section_modified_after_writer_fails(self):
        """写后被改 → 台账摘要与当前不符 → 阻断（旧产物不得复用）。"""
        _write(os.path.join(self.self_dir, "sec1.tex"),
               SEC1 + "\\section{后加内容}\n")
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes(result))

    def test_missing_ledger_fails(self):
        os.remove(os.path.join(self.self_dir, ".pd", "agent-calls",
                               "dsp-T2_SEC1-TEST01.json"))
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes(result))


class T04SectionMissing(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_missing_section_file(self):
        self_dir = make_project(self.d)
        os.remove(os.path.join(self_dir, "sec2.tex"))
        result = check_provenance.check_provenance_project(self_dir)
        self.assertIn("PROV_SECTION_MISSING", codes(result))


class T05WriteDomain(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = make_project(self.d)

    def _patch_ledger(self, dispatch_id, **kw):
        path = os.path.join(self.self_dir, ".pd", "agent-calls",
                            dispatch_id + ".json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.update(kw)
        _write(path, json.dumps(data, ensure_ascii=False, indent=2))

    def test_forbidden_top_level_write(self):
        self._patch_ledger("dsp-T2_SEC1-TEST01",
                           artifacts_written=["sec1.tex", "main.tex"])
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_PATH_OUTSIDE_ALLOWLIST", codes(result))

    def test_path_escape_write(self):
        self._patch_ledger("dsp-T2_SEC1-TEST01",
                           artifacts_written=["../escape.tex"])
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_PATH_OUTSIDE_ALLOWLIST", codes(result))

    def test_allowed_paths_pass(self):
        self._patch_ledger("dsp-T2_SEC1-TEST01",
                           artifacts_written=["sec1.tex", ".pd/reports/x.json"])
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertNotIn("PROV_PATH_OUTSIDE_ALLOWLIST", codes(result))


class T06CheckerChain(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = make_project(self.d)

    def _write_record(self, sec, **kw):
        path = os.path.join(self.self_dir, ".pd", "semantic", sec + ".json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.update(kw)
        _write(path, json.dumps(data, ensure_ascii=False, indent=2))

    def test_missing_checker_result(self):
        os.remove(os.path.join(self.self_dir, ".pd", "semantic", "sec1.json"))
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_CHECKER_MISSING", codes(result))

    def test_invalid_checker_result(self):
        self._write_record("sec1", schema_version=2)
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_CHECKER_INVALID", codes(result))

    def test_stale_checker_result(self):
        self._write_record("sec1", section_digest="0" * 64)
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_CHECKER_STALE", codes(result))

    def test_revise_checker_result(self):
        self._write_record("sec1", conclusion="REVISE", issues=[
            {"code": "OVERSTRONG_CLAIM", "line": 4, "claim": "充要",
             "evidence": "L4", "severity": "blocking"}])
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_CHECKER_REVISE", codes(result))

    def test_needs_evidence_checker_result(self):
        self._write_record("sec1", conclusion="NEEDS_EVIDENCE")
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_CHECKER_NEEDS_EVIDENCE", codes(result))


class T07QidPlanning(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_unplanned_qid(self):
        self_dir = make_project(self.d, texts={
            "sec1": SEC1.replace("sec1-q1", "sec1-q9"), "sec2": SEC2})
        result = check_provenance.check_provenance_project(self_dir)
        self.assertIn("PROV_QID_UNPLANNED", codes(result))

    def test_planned_qid_passes(self):
        self_dir = make_project(self.d)
        result = check_provenance.check_provenance_project(self_dir)
        self.assertNotIn("PROV_QID_UNPLANNED", codes(result))


class T08ShuffleLineage(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = make_project(self.d)

    def _simulate_shuffle(self, sec):
        """确定性选项重排：记录血缘台账并改写切片。"""
        path = os.path.join(self.self_dir, sec + ".tex")
        before = sha256_file(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        _write(path, text.replace("候选内容", "候选项重排后内容"))
        after = sha256_file(path)
        _write(os.path.join(self.self_dir, ".pd", "shuffle-map.json"),
               json.dumps({"schema_version": 1,
                           "transitions": {sec: [[before, after]]}},
                          ensure_ascii=False, indent=2))
        return before, after

    def test_shuffle_lineage_exempts_writer_and_checker(self):
        self._simulate_shuffle("sec1")
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertNotIn("PROV_WRITER_DIGEST_MISSING", codes(result))
        self.assertNotIn("PROV_CHECKER_STALE", codes(result))
        self.assertEqual(result["checked"]["checker"]["sec1"], "ok")

    def test_unrelated_mutation_still_fails(self):
        with open(os.path.join(self.self_dir, "sec1.tex"), "a",
                  encoding="utf-8") as f:
            f.write("\\section{无血缘新增}\n")
        result = check_provenance.check_provenance_project(self.self_dir)
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes(result))
        self.assertIn("PROV_CHECKER_STALE", codes(result))


class T09CliContract(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prov_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_cli_report_and_exit_codes(self):
        self_dir = make_project(self.d)
        report_path = os.path.join(self_dir, ".pd", "reports",
                                   "check_provenance.json")
        rc = check_provenance.main([self_dir, "--json", report_path])
        self.assertEqual(rc, 0)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["tool"], "check_provenance.py")
        self.assertEqual(report["tool_version"], "1.0")
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["input_digest"])

        # 破坏血缘 → 退出码 1 + fail 报告
        with open(os.path.join(self_dir, "sec1.tex"), "a",
                  encoding="utf-8") as f:
            f.write("\\section{篡改}\n")
        rc = check_provenance.main([self_dir, "--json", report_path])
        self.assertEqual(rc, 1)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any(e["code"] == "PROV_WRITER_DIGEST_MISSING"
                            for e in report["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
