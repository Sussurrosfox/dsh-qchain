#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_fast_cache.py — 修复卡16：精确缓存失效与复用证据（CACHE-01..04）。

本文件把 DATA-CONTRACTS C7 的三条红线变成可执行断言：

1. 缓存键必须覆盖 graph_version / execution_schema_version / policy_digest /
   role / prompt_version / 模板语义 / 工具版本 —— 新旧 execution_profile
   绝不能跨档共享同一份派生物（CACHE-01）。
2. 只改一节时，只有这一节的写入缓存失效；其余节的作者不重派（CACHE-02）。
3. 模板/宏语义变化必须让「内容审读」失效；但纯 CSS/版面变化只需重跑机械
   交付检查并重新绑定本轮实测指纹，不得拿旧指纹冒充见过新产物（CACHE-03）。
4. 缓存条目损坏 / 缺产物 / 缺键 / 被拉黑都算 miss 且必须给出原因；复用台账
   必须能和历史派发单双向对上，绝不能伪造一个从未运行过的 agent（CACHE-04）。

全部写盘都发生在 tempfile.mkdtemp() 里，绝不触碰 D:\teaching 下的真实学案。
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
for path in (HERE, PLUGIN_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import check_agent_audit  # noqa: E402
import evidence  # noqa: E402
from workflow.adapters.artifact_cache import ArtifactCache, config_digest  # noqa: E402
from workflow.nodes import agent_nodes  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes import t4_review_batch_nodes as t4nodes  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

CFG = {"timeout_seconds": 900, "max_retries": 2}

MACRO_TEXT = "\\newcommand{\\R}{\\mathbb{R}}\n"
CSS_TEXT = ".katex { font-size: 1em; }\n"
MATHCONFIG_TEXT = '{"macros": {"\\\\R": "\\\\mathbb{R}"}}\n'


def write_file(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def make_self(root, sections=("sec1", "sec2", "sec3")):
    """造一个最小 self 项目（模板/宏/CSS + 节正文 + 交付件）。"""
    write_file(os.path.join(root, "config.yaml"),
               "content_policy: faithful\nagent_dispatch:\n  max_concurrency: 2\n")
    write_file(os.path.join(root, "main.tex"), "\\documentclass{article}\n\\begin{document}\n\\input{sec1}\n\\end{document}\n")
    write_file(os.path.join(root, "header.tex"), MACRO_TEXT)
    write_file(os.path.join(root, "sec_template.tex"), "% section template v1\n")
    write_file(os.path.join(root, "footer.tex"), "% footer\n")
    write_file(os.path.join(root, "config.tex"), "\\geometry{margin=2cm}\n")
    write_file(os.path.join(root, "student.tex"), "% student wrapper\n")
    write_file(os.path.join(root, "teacher.tex"), "% teacher wrapper\n")
    write_file(os.path.join(root, "html", "math-config.json"), MATHCONFIG_TEXT)
    write_file(os.path.join(root, "html", "deck.template.html"), "<html><body>{{content}}</body></html>\n")
    write_file(os.path.join(root, "html", "katex", "katex.min.css"), CSS_TEXT)
    write_file(os.path.join(root, ".pd", "dependency-map.md"), "- sec1 -> sec2\n")
    write_file(os.path.join(root, "student.pdf"), "%PDF-1.4 fake\n")
    write_file(os.path.join(root, "student.html"), "<html>fake</html>\n")
    for sec in sections:
        write_file(os.path.join(root, "%s.tex" % sec),
                   "%% %s\n%% qid: %s-entry\n$1+1=2$\n" % (sec, sec))
    return root


def section_sources(sections=("sec1", "sec2", "sec3")):
    return dict((sec, "源文本 %s 第一版" % sec) for sec in sections)


def writer_state(self_dir, sections=("sec1", "sec2", "sec3"), profile=None,
                 sources=None, **extra):
    state = {
        "self_dir": self_dir,
        "project_root": os.path.dirname(self_dir),
        "run_id": "run-cache",
        "user_spec": {"title": "缓存测试"},
        "expected_sections": list(sections),
        "section_statuses": dict((sec, "pending") for sec in sections),
        "section_sources": dict(sources if sources is not None else section_sources(sections)),
        "history": [],
        "output_format": "both",
    }
    if profile:
        state["execution_profile"] = profile
    state.update(extra)
    return state


def stamp_scope_record(self_dir, sec, chapter_digest=None, **over):
    """按 T4 主控盖章口径预置一条「上一轮已 PASS」的验收记录。"""
    rec = {
        "schema_version": 1,
        "scope": sec,
        "reviewed_qids": ["%s-entry" % sec],
        "content_digest": evidence.content_digest(evidence.collect_inputs(self_dir)),
        "student_pdf_sha256": evidence.sha256_file(os.path.join(self_dir, "student.pdf")),
        "conclusion": "PASS",
        "reviewer_role": t4nodes.reviewer_role(sec),
        "stage1_record": {"checks": []},
        "stage2_record": {"checks": []},
        "issues": [],
    }
    rec.update(over)
    if chapter_digest is not None:
        rec["chapter_digest"] = chapter_digest
    t4nodes._stamp_review_provenance(self_dir, sec, rec,
                                     chapter_digest or t4nodes._chapter_context_digest(
                                         self_dir, ["sec1", "sec2"]))
    write_file(os.path.join(self_dir, ".pd", "acceptance",
                            "%s-source-vs-output.md" % sec), "# 对照 %s\n" % sec)
    return rec


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="qchain-cache16-")
        self.self_dir = os.path.join(self.root, "self")
        os.makedirs(self.self_dir)
        make_self(self.self_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# CACHE-01：profile / 图版本 / 策略必须进入缓存键，新旧档不得串用
# ---------------------------------------------------------------------------

class TestCacheKeyCoversGraphAndPolicy(CacheCase):
    def test_section_spec_carries_digest_context(self):
        state = writer_state(self.self_dir)
        spec = sbn._section_request_spec(state, "sec1", CFG, sbn._unavailable_source_assets())
        ctx = spec.get("digest_context")
        self.assertIsInstance(ctx, dict, "T2 单节 spec 必须携带 digest_context")
        for key in ("graph_version", "execution_schema_version", "execution_profile",
                    "policy_digest", "role", "prompt_version",
                    "template_semantic_digest", "tool_version"):
            self.assertIn(key, ctx, "digest_context 缺维度：%s" % key)
        self.assertEqual(ctx["execution_profile"], "legacy")
        self.assertEqual(ctx["graph_version"], "legacy-v1")
        self.assertTrue(str(ctx["template_semantic_digest"]))
        self.assertTrue(str(ctx["tool_version"]))

    def test_group_spec_carries_digest_context(self):
        state = writer_state(self.self_dir)
        spec = sbn._group_request_spec(state, ["sec1", "sec2"], CFG,
                                       sbn._unavailable_source_assets())
        self.assertEqual(spec.get("scopes"), ["sec1", "sec2"])
        ctx = spec.get("digest_context")
        self.assertIsInstance(ctx, dict, "多节合批 spec 必须携带 digest_context")
        self.assertIn("policy_digest", ctx)

    def test_profiles_do_not_share_input_digest(self):
        assets = sbn._unavailable_source_assets()
        digests = {}
        for profile in (None, "faithful_fast"):
            state = writer_state(self.self_dir, profile=profile)
            spec = sbn._section_request_spec(state, "sec1", CFG, assets)
            digests[profile] = agent_nodes.build_request(state, **spec)["input_digest"]
        self.assertNotEqual(digests[None], digests["faithful_fast"],
                            "legacy 与 faithful_fast 不得共享同一指纹（会跨档命中旧派生物）")

    def test_cache_namespace_is_consumed(self):
        """命中判定必须按 profile 隔离：legacy 写入的条目，fast 档不得命中。"""
        assets = sbn._unavailable_source_assets()
        legacy = writer_state(self.self_dir)
        probe_before = sbn.section_cache_probe(self.self_dir, legacy, "sec1", CFG, assets)
        self.assertFalse(probe_before["hit"])
        sbn.store_section_cache(self.self_dir, legacy, "sec1", CFG, assets)
        self.assertTrue(sbn.section_cache_probe(self.self_dir, legacy, "sec1", CFG, assets)["hit"])
        fast = writer_state(self.self_dir, profile="faithful_fast")
        probe_fast = sbn.section_cache_probe(self.self_dir, fast, "sec1", CFG, assets)
        self.assertFalse(probe_fast["hit"], "faithful_fast 不得命中 legacy 命名空间的条目")
        self.assertIn("无缓存条目", probe_fast["reason"])


# ---------------------------------------------------------------------------
# CACHE-02：只改一节 → 只有这一节失效
# ---------------------------------------------------------------------------

class TestSingleSectionChange(CacheCase):
    def test_only_changed_section_loses_cache(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        for sec in ("sec1", "sec2", "sec3"):
            sbn.store_section_cache(self.self_dir, state, sec, CFG, assets)
        before = dict((sec, sbn.section_cache_probe(self.self_dir, state, sec, CFG, assets))
                      for sec in ("sec1", "sec2", "sec3"))
        self.assertEqual([before[sec]["hit"] for sec in ("sec1", "sec2", "sec3")],
                         [True, True, True])

        changed = writer_state(self.self_dir,
                              sources=dict(section_sources(),
                                           sec2="源文本 sec2 第二版（返修）"))
        probes = dict((sec, sbn.section_cache_probe(self.self_dir, changed, sec, CFG, assets))
                      for sec in ("sec1", "sec2", "sec3"))
        self.assertTrue(probes["sec1"]["hit"], "只改 sec2 时 sec1 的作者不得重派")
        self.assertTrue(probes["sec3"]["hit"], "只改 sec2 时 sec3 的作者不得重派")
        self.assertFalse(probes["sec2"]["hit"], "sec2 自身必须失效")
        self.assertTrue(probes["sec2"]["reason"])

    def test_repair_attempt_busts_section_cache(self):
        """返修轮次（repair_attempts_by_scope）必须让该节缓存失效，否则返修会命中旧产物。"""
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec2", CFG, assets)
        self.assertTrue(sbn.section_cache_probe(self.self_dir, state, "sec2", CFG, assets)["hit"])
        repaired = writer_state(self.self_dir, **{"repair_attempts_by_scope": {"sec2": 1}})
        probe = sbn.section_cache_probe(self.self_dir, repaired, "sec2", CFG, assets)
        self.assertFalse(probe["hit"], "返修轮必须重派该节作者，不得复用旧产物")
        # 返修只影响该 scope，其它节的缓存不受牵连
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        self.assertTrue(sbn.section_cache_probe(self.self_dir, repaired, "sec1", CFG, assets)["hit"])

    def test_macro_semantic_change_busts_writer_cache(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        self.assertTrue(sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)["hit"])
        write_file(os.path.join(self.self_dir, "header.tex"),
                   MACRO_TEXT + "\\newcommand{\\eps}{\\varepsilon}\n")
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(probe["hit"], "宏语义变化必须让写入缓存失效")
        self.assertTrue(probe["reason"])

    def test_pure_css_change_keeps_writer_cache(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        digest_before = sbn.template_semantic_digest(self.self_dir)
        write_file(os.path.join(self.self_dir, "html", "katex", "katex.min.css"),
                   CSS_TEXT + ".katex-display { margin: 1em 0; }\n")
        self.assertEqual(digest_before, sbn.template_semantic_digest(self.self_dir),
                         "纯 CSS 变化不属于宏语义变化")
        self.assertTrue(sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)["hit"],
                        "纯 CSS 变化不得触发作者重派")


# ---------------------------------------------------------------------------
# CACHE-03：模板/宏语义变化必须让交付证据失效并重绑本轮实测指纹
# ---------------------------------------------------------------------------

class TestDeliveryRebinding(CacheCase):
    SECTIONS = ("sec1", "sec2")

    def _chapter(self):
        return t4nodes._chapter_context_digest(self.self_dir, self.SECTIONS)

    def test_macro_change_invalidates_content_review(self):
        chapter = self._chapter()
        for sec in self.SECTIONS:
            stamp_scope_record(self.self_dir, sec, chapter_digest=chapter)
        reused, reasons = t4nodes._reusable_scopes(self.self_dir, self.SECTIONS, chapter)
        self.assertEqual(reused, set(self.SECTIONS))

        # 纯 CSS：内容审读仍可复用
        write_file(os.path.join(self.self_dir, "html", "katex", "katex.min.css"),
                   CSS_TEXT + ".katex{color:#333}\n")
        reused, reasons = t4nodes._reusable_scopes(self.self_dir, self.SECTIONS, chapter)
        self.assertEqual(reused, set(self.SECTIONS), "纯 CSS 变化不得让内容审读失效：%r" % (reasons,))

        # 宏语义变化：内容审读必须失效，且原因要能读懂
        write_file(os.path.join(self.self_dir, "header.tex"),
                   MACRO_TEXT + "\\newcommand{\\dd}{\\mathrm{d}}\n")
        reused, reasons = t4nodes._reusable_scopes(self.self_dir, self.SECTIONS, chapter)
        self.assertEqual(reused, set(), "宏语义变化后不得复用旧内容审读")
        self.assertTrue(any("宏" in str(reasons.get(sec, "")) for sec in self.SECTIONS),
                        "失效原因必须说明是宏/模板语义变化：%r" % (reasons,))

    def test_math_config_change_invalidates_content_review(self):
        chapter = self._chapter()
        for sec in self.SECTIONS:
            stamp_scope_record(self.self_dir, sec, chapter_digest=chapter)
        write_file(os.path.join(self.self_dir, "html", "math-config.json"),
                   '{"macros": {"\\\\R": "\\\\mathbf{R}"}}\n')
        reused, reasons = t4nodes._reusable_scopes(self.self_dir, self.SECTIONS, chapter)
        self.assertEqual(reused, set(), "KaTeX 转换规则变化会影响数学含义，必须重审")

    def test_rebind_binds_current_artifacts_and_keeps_history(self):
        rec = self._v2_record("sec1")
        old_pdf = rec["content_review_refs"]["artifact_digests"]["student.pdf"]
        write_file(os.path.join(self.self_dir, "student.pdf"), "%PDF-1.4 rebuilt after macro change\n")
        new_pdf = evidence.sha256_file(os.path.join(self.self_dir, "student.pdf"))
        self.assertNotEqual(old_pdf, new_pdf)

        t4nodes._rebind_reused_delivery(self.self_dir, "sec1", rec, output_format="both")

        self.assertEqual(rec["delivery_check_ref"]["artifact_digests"]["student.pdf"], new_pdf,
                         "本轮交付检查必须绑定本轮实测指纹")
        self.assertEqual(rec["content_review_refs"]["artifact_digests"]["student.pdf"], old_pdf,
                         "历史内容审读指纹绝不能被改写冒充见过新产物")
        self.assertTrue(rec["content_review_reused"])
        self.assertIn("source", rec["delivery_check_ref"])
        self.assertEqual(evidence.validate_acceptance_json(rec), [])

    def test_unchanged_artifacts_do_not_claim_reuse_of_new_sight(self):
        rec = self._v2_record("sec1")
        t4nodes._rebind_reused_delivery(self.self_dir, "sec1", rec, output_format="both")
        self.assertFalse(rec["content_review_reused"],
                         "指纹完全一致时不得置 content_review_reused（证据会自相矛盾）")
        self.assertTrue(rec["reuse_binding"]["reused"])
        self.assertEqual(evidence.validate_acceptance_json(rec), [])

    def _v2_record(self, sec):
        """构造一条**真正合规**的 v2 合并终审记录（必须能过 evidence 校验器）。

        关键：``content_review_refs``（审读员当时看到的产物指纹 + 派发身份）与
        ``delivery_check_ref``（本轮磁盘机械实测）必须是两份可溯源引用，
        ``derived_from`` 还必须对回父主产物摘要与派发单号。
        """
        pdf = evidence.sha256_file(os.path.join(self.self_dir, "student.pdf"))
        html = evidence.sha256_file(os.path.join(self.self_dir, "student.html"))
        artifacts = {"student.pdf": pdf, "student.html": html}
        review_sha = hashlib.sha256(b"review-main-artifact-v1").hexdigest()
        dispatch_id = "bat-t4-historical-0001"
        return {
            "schema_version": 2,
            "review_kind": "combined_source_fidelity",
            "scope": sec,
            "reviewed_qids": ["%s-entry" % sec],
            "content_digest": evidence.content_digest(evidence.collect_inputs(self.self_dir)),
            "output_format": "both",
            "conclusion": "PASS",
            "reviewer_role": t4nodes.reviewer_role(sec),
            "issues": [],
            "source_comparison": "逐条对校来源切片与产出，未见漏题、错题或口径漂移。",
            "content_review_refs": {
                "review_artifact": ".pd/acceptance/%s.json" % sec,
                "review_artifact_sha256": review_sha,
                "dispatch_id": dispatch_id,
                "section_digest": t4nodes._scope_content_digest(self.self_dir, sec),
                "artifact_digests": dict(artifacts),
                "reviewed_at": "2024-01-01T00:00:00Z",
            },
            "delivery_check_ref": {
                "artifact_digests": dict(artifacts),
                "output_format": "both",
                "source": "T4/1.1 投影时磁盘实测",
            },
            "content_review_reused": False,
            "derived_from": {
                "projection_schema_version": 1,
                "scope": sec,
                "review_artifact": ".pd/acceptance/%s.json" % sec,
                "dispatch_id": dispatch_id,
                "review_artifact_sha256": review_sha,
            },
        }


# ---------------------------------------------------------------------------
# CACHE-04：miss 必须带原因；台账必须能和历史回执双向对上
# ---------------------------------------------------------------------------

class TestMissReasons(CacheCase):
    def test_missing_entry_reason(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(probe["hit"])
        self.assertIn("无缓存条目", probe["reason"])
        self.assertTrue(probe["cache_key"])
        self.assertTrue(probe["input_digest"])

    def test_corrupt_entry_reason(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        key = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)["cache_key"]
        write_file(os.path.join(self.self_dir, ".pd", "artifact-cache", key, "entry.json"),
                   "{ this is not json")
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(probe["hit"])
        self.assertIn("损坏", probe["reason"])

    def test_missing_cached_copy_reason(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        key = sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)["cache_key"]
        cache_dir = os.path.join(self.self_dir, ".pd", "artifact-cache", key)
        for name in os.listdir(cache_dir):
            if name != "entry.json":
                write_file(os.path.join(cache_dir, name), "tampered\n")
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(probe["hit"])
        self.assertTrue(probe["reason"])

    def test_key_mismatch_reason(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        stored = sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        entry_path = os.path.join(self.self_dir, ".pd", "artifact-cache",
                                  stored["cache_key"], "entry.json")
        entry = read_json(entry_path)
        entry["key"] = "0" * 64
        write_file(entry_path, json.dumps(entry, ensure_ascii=False, indent=2))
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(probe["hit"])
        self.assertIn("不符", probe["reason"])

    def test_blocklisted_digest_reason(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        digest = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)["input_digest"]
        blocked = writer_state(self.self_dir, **{"dispatch_blocklist": [digest]})
        probe = sbn.section_cache_probe(self.self_dir, blocked, "sec1", CFG, assets)
        self.assertFalse(probe["hit"])
        self.assertIn("拉黑", probe["reason"])


class TestUnavailableSourceAssets(CacheCase):
    """卡13 保守缺口：源资产不可用时曾静默不写缓存，必须改成可验证键 + 留痕。"""

    def test_probe_digest_exists_when_assets_unavailable(self):
        assets = sbn._source_asset_counts(self.self_dir)
        self.assertFalse(assets.get("available"))
        state = writer_state(self.self_dir)
        digest = sbn._section_probe_digest(state, "sec1", assets)
        self.assertTrue(digest, "源资产不可用时也要给出可验证键（否则永远写不进分节缓存）")

    def test_store_and_hit_records_asset_availability(self):
        assets = sbn._source_asset_counts(self.self_dir)
        state = writer_state(self.self_dir)
        stored = sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        self.assertFalse(stored["source_assets_available"])
        probe = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertTrue(probe["hit"])
        ledger = sbn.load_cache_reuse_ledger(self.self_dir)
        hits = [e for e in ledger["entries"] if e.get("hit") and e.get("scope") == "sec1"]
        self.assertTrue(hits)
        self.assertFalse(hits[-1]["source_assets_available"])
        self.assertTrue(hits[-1]["input_digest"])

    def test_available_assets_use_different_key(self):
        """「资产不可用」与「资产可用但计数为 0」是两个不同事实，键必须不同。"""
        state = writer_state(self.self_dir)
        unavailable = sbn._unavailable_source_assets()
        available = {"figure": 0, "table": 0, "tikz": 0, "available": True}
        a = sbn._section_probe_digest(state, "sec1", unavailable)
        b = sbn._section_probe_digest(state, "sec1", available)
        self.assertNotEqual(a, b)


class TestReuseLedgerCrossCheck(CacheCase):
    def _historical_request(self, state, digest):
        req = {
            "dispatch_id": "bat-t2-historical-0007",
            "stage": "T2_SEC1",
            "scope": "sec1",
            "input_digest": digest,
            "artifacts": ["sec1.tex"],
        }
        write_file(os.path.join(self.self_dir, ".pd", "agent-requests",
                                "bat-t2-historical-0007.json"),
                   json.dumps(req, ensure_ascii=False, indent=2))
        return req

    def test_verified_reuse_is_not_counted_as_new_dispatch(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        stored = sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        hit = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertTrue(hit["hit"])
        self._historical_request(state, stored["input_digest"])
        reuses, errors = check_agent_audit.verify_cache_reuse_ledger(self.self_dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(reuses), 1)
        self.assertEqual(reuses[0]["scope"], "sec1")

    def test_unbacked_reuse_is_flagged(self):
        """台账与缓存条目彼此自洽，但没有任何历史派发能对上 → 仍按伪造拦下。"""
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        hit = sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self.assertTrue(hit["hit"])
        forged = "f" * 64
        # ① 缓存条目摘要也改成同一假摘要：消掉「台账 vs 条目不一致」这条噪声，
        #    让断言只盯住 C7 的红线——「没有任何 agent 真的产出过这份产物」。
        entry_path = os.path.join(self.self_dir, ".pd", "artifact-cache",
                                  hit["cache_key"], "entry.json")
        entry = read_json(entry_path)
        entry["input_digest"] = forged
        write_file(entry_path, json.dumps(entry, ensure_ascii=False, indent=2))
        # ② 台账声称命中的正是这条摘要
        ledger_path = os.path.join(self.self_dir, ".pd", "artifact-cache", "reuse-ledger.json")
        ledger = read_json(ledger_path)
        for item in ledger["entries"]:
            if item.get("hit"):
                item["input_digest"] = forged
        write_file(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
        reuses, errors = check_agent_audit.verify_cache_reuse_ledger(self.self_dir)
        self.assertTrue(errors, "台账没有历史回执支撑时不得静默通过")
        detail = json.dumps(errors, ensure_ascii=False)
        self.assertIn("CACHE_REUSE_UNVERIFIED", detail)
        self.assertIn("没有任何历史派发单", detail)

    def test_drifted_artifacts_are_flagged(self):
        assets = sbn._unavailable_source_assets()
        state = writer_state(self.self_dir)
        stored = sbn.store_section_cache(self.self_dir, state, "sec1", CFG, assets)
        sbn.section_cache_probe(self.self_dir, state, "sec1", CFG, assets)
        self._historical_request(state, stored["input_digest"])
        write_file(os.path.join(self.self_dir, "sec1.tex"), "被手改过\n")
        reuses, errors = check_agent_audit.verify_cache_reuse_ledger(self.self_dir)
        self.assertTrue(errors, "盘面产物与缓存记录不一致时必须报错")
        self.assertIn("CACHE_REUSE_DRIFT", json.dumps(errors, ensure_ascii=False))


class TestFullHitZeroDispatch(unittest.TestCase):
    """CACHE-01：同一输入全命中 → 真·0 次模型调用；换执行档立刻不共享缓存。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-cache16-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.tmp, "self")
        os.makedirs(self.proj)

    def _prepare(self):
        """造一个最小 T2 工程，并把「第一轮真实产物」写进分节缓存。"""
        import test_section_batch as tsb
        tsb.write_config(self.proj)
        state = tsb.make_state(self.proj, sections=("sec1", "sec2"))
        for sec in ("sec1", "sec2"):
            write_file(os.path.join(self.proj, "%s.tex" % sec),
                       "%% %s\n%% qid: %s-entry\n$1+1=2$\n" % (sec, sec))
        assets = sbn._source_asset_counts(self.proj)
        for sec in ("sec1", "sec2"):
            stored = sbn.store_section_cache(self.proj, state, sec, CFG, assets)
            self.assertTrue(stored["stored"], "第一轮真实产物必须写进分节缓存")
        return tsb, state

    def test_full_hit_writes_no_request(self):
        tsb, state = self._prepare()
        before = tsb.request_files(self.proj)
        patch = sbn.node_t2_plan_dispatch(state)
        self.assertEqual(tsb.request_files(self.proj), before,
                         "全部命中时不得写出任何新派发单（等价于 0 次模型调用）")
        self.assertNotIn("dispatch_plan", patch)
        self.assertEqual((patch.get("section_statuses") or {}).get("sec1"), "staged")

    def test_other_profile_does_not_share_cache(self):
        tsb, state = self._prepare()
        before = tsb.request_files(self.proj)
        fast = dict(state)
        fast["execution_profile"] = "faithful_fast"
        fast["section_statuses"] = dict(state["section_statuses"])
        sbn.node_t2_plan_dispatch(fast)
        self.assertNotEqual(tsb.request_files(self.proj), before,
                            "legacy 的派生物缓存绝不能被 faithful_fast 直接复用")


if __name__ == "__main__":
    unittest.main()
