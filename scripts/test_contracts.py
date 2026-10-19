"""test_contracts.py — F0 Phase 0 冻结契约测试（audit §4.1/§4.2/§18.3）。

覆盖：source-manifest 登记与拒绝规则、intent-contract 冻结与默认值、
digest 对账（下游篡改检测）、契约对账、原子落盘与回读、digest 稳定性。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ENGINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))
if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

from workflow import contracts  # noqa: E402


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class T01ManifestBuild(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_contracts_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        os.makedirs(self.root)
        self.src = _write(os.path.join(self.root, "8.md"), "source body")
        self.exc = _write(os.path.join(self.root, "2023.0.8-2.md"), "excluded")

    def test_t01_source_and_exclusion_both_registered(self):
        m = contracts.build_source_manifest(
            project_root=self.root,
            source_files=["8.md"],
            excluded_sources=["2023.0.8-2.md :: 本轮按用户设计暂不纳入"],
            title="T")
        entries = m["sources"]
        self.assertEqual(len(entries), 2)
        inc = [e for e in entries if e["include"]]
        exc = [e for e in entries if not e["include"]]
        self.assertEqual([e["path"] for e in inc], ["8.md"])
        self.assertEqual(inc[0]["role"], "source-baseline")
        self.assertTrue(inc[0]["digest"].startswith("sha256:"))
        self.assertEqual([e["path"] for e in exc], ["2023.0.8-2.md"])
        self.assertEqual(exc[0]["reason"], "本轮按用户设计暂不纳入")
        self.assertTrue(m["digest"].startswith("sha256:"))

    def test_t02_digest_is_content_pure_function(self):
        """digest 排除 frozen_at：同输入两次构建摘要一致（cache key 稳定）。"""
        kw = dict(project_root=self.root, source_files=["8.md"],
                  excluded_sources=["2023.0.8-2.md"])
        m1 = contracts.build_source_manifest(**kw)
        m2 = contracts.build_source_manifest(**kw)
        self.assertEqual(m1["digest"], m2["digest"])
        c1 = contracts.build_intent_contract(
            user_spec={"content_policy": "faithful"})
        c2 = contracts.build_intent_contract(
            user_spec={"content_policy": "faithful"})
        self.assertEqual(c1["digest"], c2["digest"])

    def test_t03_reject_source_pointing_to_generated(self):
        self_dir = os.path.join(self.root, "self")
        _write(os.path.join(self_dir, "source-map.md"), "draft")
        _write(os.path.join(self.root, "out.pdf"), "%PDF")
        for bad in ("self/source-map.md", "out.pdf"):
            with self.assertRaises(contracts.ContractError) as ctx:
                contracts.build_source_manifest(
                    project_root=self.root, self_dir=self_dir,
                    source_files=[bad])
            self.assertEqual(ctx.exception.code, "SOURCE_IS_GENERATED")

    def test_t04_reject_duplicate_role(self):
        with self.assertRaises(contracts.ContractError) as ctx:
            contracts.build_source_manifest(
                project_root=self.root,
                source_files=["8.md"],
                excluded_sources=["8.md"])
        self.assertEqual(ctx.exception.code, "SOURCE_DUPLICATE_ROLE")

    def test_t05_reject_missing_source(self):
        with self.assertRaises(contracts.ContractError) as ctx:
            contracts.build_source_manifest(
                project_root=self.root, source_files=["nope.md"])
        self.assertEqual(ctx.exception.code, "SOURCE_MISSING")

    def test_t06_empty_source_list_allowed(self):
        m = contracts.build_source_manifest(project_root=self.root)
        self.assertEqual(m["sources"], [])


class T02IntentContract(unittest.TestCase):

    def test_t07_faithful_defaults(self):
        c = contracts.build_intent_contract(user_spec={})
        self.assertEqual(c["content_policy"], "faithful")
        self.assertTrue(c["must_not_expand_without_confirmation"])
        self.assertFalse(c["reorder_allowed"])
        self.assertEqual(c["source_files"], [])
        self.assertEqual(c["excluded_sources"], [])

    def test_t08_teaching_defaults_90_minutes(self):
        c = contracts.build_intent_contract(
            user_spec={"delivery_profile": "teaching"})
        self.assertEqual(c["lesson_minutes"], 90)

    def test_t09_self_study_no_budget(self):
        c = contracts.build_intent_contract(
            user_spec={"delivery_profile": "self_study"})
        self.assertIsNone(c["lesson_minutes"])

    def test_t10_invalid_content_policy_rejected(self):
        with self.assertRaises(contracts.ContractError) as ctx:
            contracts.build_intent_contract(
                user_spec={"content_policy": "expand-everything"})
        self.assertEqual(ctx.exception.code, "INVALID_CONTENT_POLICY")

    def test_t11_authoring_allows_expansion_flag(self):
        c = contracts.build_intent_contract(
            user_spec={"content_policy": "authoring",
                       "allowed_extensions": ["EXT-1"]})
        self.assertFalse(c["must_not_expand_without_confirmation"])
        self.assertEqual(c["allowed_extensions"][0]["id"], "EXT-1")


class T03Verification(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_contracts_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        os.makedirs(self.root)
        self.src = _write(os.path.join(self.root, "8.md"), "body v1")

    def test_t12_verify_detects_tamper_and_missing(self):
        m = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        self.assertEqual(contracts.verify_source_manifest(m, self.root), [])
        _write(self.src, "body v2 tampered")
        bad = contracts.verify_source_manifest(m, self.root)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["path"], "8.md")
        self.assertNotEqual(bad[0]["expected"], bad[0]["actual"])
        os.remove(self.src)
        bad2 = contracts.verify_source_manifest(m, self.root)
        self.assertIsNone(bad2[0]["actual"])

    def test_t13_contract_pair_mismatch_detected(self):
        m = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        c = contracts.build_intent_contract(user_spec={"source_files": "9.md"})
        errors = contracts.verify_contract_pair(m, c)
        self.assertTrue(any("source_files" in e for e in errors))

    def test_t14_contract_pair_ok(self):
        m = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"],
            excluded_sources=["x.md :: 排除"])
        _write(os.path.join(self.root, "x.md"), "x")
        c = contracts.build_intent_contract(
            user_spec={"source_files": "8.md", "excluded_sources": "x.md :: 排除"})
        self.assertEqual(contracts.verify_contract_pair(m, c), [])


class T04Persistence(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_contracts_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        self.self_dir = os.path.join(self.root, "self")
        os.makedirs(self.self_dir)
        _write(os.path.join(self.root, "8.md"), "body")

    def test_t15_write_load_roundtrip(self):
        m = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"])
        c = contracts.build_intent_contract(user_spec={"source_files": "8.md"})
        mp, cp = contracts.write_contracts(self.self_dir, m, c)
        self.assertTrue(os.path.isfile(mp))
        self.assertTrue(os.path.isfile(cp))
        m2, c2 = contracts.load_contracts(self.self_dir)
        self.assertEqual(m2["digest"], m["digest"])
        self.assertEqual(c2["digest"], c["digest"])
        # 落盘内容可被人直接审读（JSON）
        with open(mp, "r", encoding="utf-8-sig") as f:
            self.assertEqual(json.load(f)["sources"][0]["path"], "8.md")

    def test_t16_load_missing_returns_none(self):
        m, c = contracts.load_contracts(os.path.join(self.d, "nowhere"))
        self.assertIsNone(m)
        self.assertIsNone(c)

    def test_t17_scope_summary_reports_counts(self):
        _write(os.path.join(self.root, "x.md"), "x")
        m = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"],
            excluded_sources=["x.md :: 排除"])
        c = contracts.build_intent_contract(
            user_spec={"delivery_profile": "teaching",
                       "source_files": "8.md",
                       "excluded_sources": "x.md :: 排除",
                       "must_cover": "matrix-functions,Liouville"})
        summary = contracts.scope_summary(m, c)
        self.assertIn("1 个原材料(include)", summary)
        self.assertIn("1 个排除(include=false)", summary)
        self.assertIn("faithful", summary)
        self.assertIn("90 分钟", summary)
        self.assertIn("must_cover: 2 项", summary)


if __name__ == "__main__":
    unittest.main()
