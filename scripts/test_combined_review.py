# -*- coding: utf-8 -*-
r"""卡 14 回归测试：合并终审（一次独立审读覆盖多节）+ 可验证证据投影。

失败前复现要点（本卡针对的真实缺陷）
------------------------------------
1. 一条真实审读结果**无法**同时产出「语义证据」与「验收证据」两套可重算、
   可溯源（``derived_from``）的投影——于是同一份候选被读两遍，两套证据互不
   引用，主 Agent 自己写个 ``conclusion=PASS`` 的 JSON 就能过门。
2. 投影被手改、父主产物被改写、缺节/缺 qid、REVISE/NEEDS_EVIDENCE、审读与
   写作同一 worker 等情形，全部无人拦下（``verify_projection`` 不存在）。
3. html-only 被逼着写 ``student.pdf = "0"*64`` 的假 PDF 证明，或反过来 v1
   验收被新格式静默放宽（``review_kind`` 判别式缺失）。

本文件只做**单元/节点级**验证（不启流水线、不读令牌、不碰真实学案）：
   * REV-01 一条审读 → 两套可溯源投影；篡改投影/父产物即失败。
   * REV-02 缺节、缺 qid、REVISE、NEEDS_EVIDENCE、创审同体全部阻塞。
   * REV-03 html-only 不需要（也不许伪造）PDF 证明；pdf/both 缺 PDF 指纹即失败。
   * REV-04 旧 v1 验收不因 v2 出现而放宽；无 receipt 的投影不得认证。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import evidence                                       # noqa: E402
import review_projection as rp                        # noqa: E402
import check_agent_audit                              # noqa: E402
from workflow.adapters.worker_pool import DispatchResult  # noqa: E402
from workflow.nodes import agent_evidence             # noqa: E402
from workflow.nodes import fast_review_nodes as frn   # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes import semantic_nodes             # noqa: E402

REVIEWER_WORKER = "subagent:combined-reviewer"
WRITER_WORKER = "subagent:sec-writer"

SEC_TEX = {
    "sec1": "\\section{集合与映射}\n% qid: sec1-01\n正文一\n",
    "sec2": "\\section{极限}\n% qid: sec2-01\n正文二\n",
    "sec3": "\\section{连续}\n% qid: sec3-01\n正文三\n",
    "sec4": "\\section{导数}\n% qid: sec4-01\n正文四\n",
}

SOURCE_TEXT = ("来源《数学分析》第 12 页定理 2.3：已将 3 条结论逐条比对，"
               "覆盖全部定义与例题，无缺口。")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def sha(path):
    return evidence.sha256_file(path)


def register_capable_worker(max_concurrency=8):
    from workflow.adapters import worker_registry

    def _fn(system_prompt, user_content):      # 测试替身：同步返回空串
        return ""

    worker_registry.set(_fn, name="test-capable-llm", capabilities={
        "sync": False, "async": True, "cancellable": True,
        "max_concurrency": max_concurrency,
    })
    return worker_registry.clear


def make_self(d, sections=("sec1", "sec2"), fmt="both"):
    """临时工程：切片候选 + 主装配 + 配置 + 本轮交付产物。"""
    self_dir = os.path.join(d, "self")
    os.makedirs(os.path.join(self_dir, ".pd", "acceptance"), exist_ok=True)
    for sec in sections:
        with open(os.path.join(self_dir, sec + ".tex"), "w",
                  encoding="utf-8") as fh:
            fh.write(SEC_TEX.get(sec, "\\section{%s}\n%% qid: %s-01\n正文\n"
                                 % (sec, sec)))
    with open(os.path.join(self_dir, "main.tex"), "w", encoding="utf-8") as fh:
        fh.write("\n".join("\\input{%s.tex}" % sec for sec in sections) + "\n")
    with open(os.path.join(self_dir, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("agent_dispatch: safe\nagent_max_concurrency: 8\n"
                 "agent_batch_window: 2\n")
    if fmt in ("pdf", "both"):
        with open(os.path.join(self_dir, "student.pdf"), "wb") as fh:
            fh.write(b"%PDF-fake-student")
    if fmt in ("html", "both"):
        with open(os.path.join(self_dir, "student.html"), "w",
                  encoding="utf-8") as fh:
            fh.write("<html><body>cards</body></html>\n")
    with open(os.path.join(self_dir, ".pd", "run.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"required_acceptance_scopes": {"chapter": []}}, fh)
    return self_dir


def base_state(self_dir, sections, fmt="both", **extra):
    state = {
        "self_dir": self_dir,
        "project_root": os.path.dirname(self_dir),
        "run_id": "run-t4c",
        "user_spec": {},
        "expected_sections": list(sections),
        "section_statuses": {sec: "staged" for sec in sections},
        "output_format": fmt,
        "history": [],
    }
    state.update(extra)
    return state


def section_digests(self_dir, sections):
    return {sec: sha(os.path.join(self_dir, sec + ".tex")) for sec in sections}


def artifact_digests(self_dir, fmt):
    out = {}
    for rel in rp.FORMAT_ARTIFACTS[fmt]:
        path = os.path.join(self_dir, rel)
        if os.path.isfile(path):
            out[rel] = sha(path)
    return out


def review_record(self_dir, sections=("sec1", "sec2"), fmt="both",
                  dispatch_id="dsp-t4c-abc1234567", batch_id="bat-t4c-abc12345",
                  input_digest=None, semantic="PASS", acceptance="PASS",
                  per_section=None):
    """构造一条 C6 schema2 合并终审主产物（内容与盘面真实指纹一致）。"""
    digests = section_digests(self_dir, sections)
    per_section = per_section or {}
    secs = []
    for sec in sections:
        item = {
            "scope": sec,
            "section_digest": digests.get(sec),
            "reviewed_qids": [sec + "-01"],
            "semantic_conclusion": semantic,
            "acceptance_conclusion": acceptance,
            "issues": [],
            "source_comparison": SOURCE_TEXT,
        }
        item.update(per_section.get(sec) or {})
        secs.append(item)
    return {
        "schema_version": 2,
        "review_kind": rp.REVIEW_KIND,
        "dispatch_id": dispatch_id,
        "batch_id": batch_id,
        "reviewer_role": rp.combined_reviewer_role(list(sections)),
        "reviewed_at": "2024-05-01T00:00:00+00:00",
        "input_digest": input_digest or ("a" * 64),
        "output_format": fmt,
        "scopes": list(sections),
        "artifact_digests": artifact_digests(self_dir, fmt),
        "sections": secs,
    }


def write_review(self_dir, record, batch_id=None, index=1):
    rel = rp.review_artifact_rel(batch_id or record["batch_id"], index)
    path = os.path.join(self_dir, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    return rel


def seed_receipt(self_dir, record, review_rel, worker=REVIEWER_WORKER,
                 status=None, digest=None):
    digests = {review_rel: digest or sha(os.path.join(
        self_dir, review_rel.replace("/", os.sep)))}
    agent_evidence.write_evidence(
        self_dir,
        dispatch_id=record["dispatch_id"],
        batch_id=record["batch_id"],
        worker=worker,
        artifacts_written=sorted(digests),
        output_digests=digests,
        **({"status": status} if status else {}))
    return digests


def project(self_dir, record, review_rel, scopes=None, qids=None, fmt=None):
    """宿主确定性投影（不落盘），返回 (projections, problems)。"""
    scopes = list(scopes or record["scopes"])
    want_qids = qids if qids is not None else {
        sec: [sec + "-01"] for sec in scopes}
    return rp.build_projections(
        self_dir, record, review_rel,
        expected_scopes=scopes,
        expected_qids=want_qids,
        output_format=fmt or record["output_format"],
        section_digests=section_digests(self_dir, scopes),
        disk_digests=artifact_digests(self_dir, fmt or record["output_format"]))


def write_request(self_dir, dispatch_id, stage, batch_id, scope,
                  artifacts, **extra):
    rel = "%s/%s.json" % (agent_evidence.__dict__.get(
        "REQUESTS_DIR_REL", ".pd/agent-requests"), dispatch_id)
    path = os.path.join(self_dir, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    req = {
        "schema_version": 2,
        "dispatch_id": dispatch_id,
        "batch_id": batch_id,
        "stage": stage,
        "scope": scope,
        "role": "测试角色",
        "prompt_file": "combined_review_prompt.md",
        "artifacts": list(artifacts),
        "input_digest": "b" * 64,
        "instruction": "测试派发单",
        "command": "true",
    }
    req.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(req, fh, ensure_ascii=False, indent=2)
    return rel


# ---------------------------------------------------------------------------
# REV-01：一条审读 → 两套可溯源投影；篡改即失败
# ---------------------------------------------------------------------------

class REV01ProjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.self_dir = make_self(self.d, ("sec1", "sec2"))
        self.record = review_record(self.self_dir)
        self.review_rel = write_review(self.self_dir, self.record)
        seed_receipt(self.self_dir, self.record, self.review_rel)

    def test_one_review_yields_two_traceable_projections(self):
        projections, problems = project(self.self_dir, self.record,
                                        self.review_rel)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(projections["semantic"]), ["sec1", "sec2"])
        self.assertEqual(sorted(projections["acceptance"]), ["sec1", "sec2"])

        review_sha = sha(os.path.join(self.self_dir, self.review_rel))
        for kind in ("semantic", "acceptance"):
            for scope in ("sec1", "sec2"):
                rec = projections[kind][scope]
                derived = rec["derived_from"]
                self.assertEqual(derived["review_artifact"], self.review_rel)
                self.assertEqual(derived["review_artifact_sha256"], review_sha)
                self.assertEqual(derived["dispatch_id"], self.record["dispatch_id"])
                self.assertEqual(derived["scope"], scope)
                self.assertEqual(derived["projector"], rp.PROJECTOR)
                self.assertEqual(derived["projection_schema_version"],
                                 rp.PROJECTION_SCHEMA_VERSION)

        # 语义侧仍是 v1 基础字段（旧闸门可直读），并额外带 derived_from
        sem = projections["semantic"]["sec1"]
        self.assertEqual(sem["schema_version"], 1)
        self.assertEqual(sem["conclusion"], "PASS")
        self.assertEqual(sem["checked_at"], self.record["reviewed_at"])

        # 验收侧是新 v2 分支：显式 review_kind + 双证据引用分离可溯源
        acc = projections["acceptance"]["sec1"]
        self.assertEqual(acc["review_kind"], rp.REVIEW_KIND)
        self.assertEqual(acc["content_review_refs"]["review_artifact"],
                         self.review_rel)
        self.assertEqual(acc["content_review_refs"]["dispatch_id"],
                         self.record["dispatch_id"])
        self.assertTrue(acc["content_review_refs"]["section_digest"])
        self.assertTrue(acc["delivery_check_ref"]["source"])
        self.assertTrue(acc["delivery_check_ref"]["artifact_digests"])
        self.assertNotIn("stage1_record", acc)  # 不伪造旧盲审体验

        # v2 验收记录必须能通过 evidence 的 v2 校验
        checked = evidence.validate_acceptance_json(
            acc, current_digest=self.record["input_digest"],
            student_pdf_sha=sha(os.path.join(self.self_dir, "student.pdf")),
            student_html_sha=sha(os.path.join(self.self_dir, "student.html")))
        self.assertEqual(checked, [])

    def test_written_projections_verify_clean(self):
        written, problems = rp.write_projections(
            self.self_dir, self.review_rel,
            expected_scopes=self.record["scopes"],
            expected_qids={sec: [sec + "-01"] for sec in self.record["scopes"]},
            output_format=self.record["output_format"],
            section_digests=section_digests(self.self_dir, self.record["scopes"]),
            disk_digests=artifact_digests(self.self_dir,
                                          self.record["output_format"]))
        self.assertEqual(problems, [])
        self.assertEqual(len(written), 4)
        for scope in ("sec1", "sec2"):
            paths = rp.project_paths(self.self_dir, scope)
            for kind in ("semantic", "acceptance"):
                self.assertEqual(
                    rp.verify_projection_file(self.self_dir, paths[kind], kind), [])

    def test_hand_edited_projection_is_rejected(self):
        paths = rp.project_paths(self.self_dir, "sec1")
        _, problems = rp.write_projections(
            self.self_dir, self.review_rel,
            expected_scopes=self.record["scopes"])
        self.assertEqual(problems, [])
        path = os.path.join(self.self_dir, paths["acceptance"].replace("/", os.sep))
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["conclusion"] = "PASS"
        rec["reviewed_qids"] = []          # 手改：把审读过的题目抹掉
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        problems = rp.verify_projection_file(self.self_dir, paths["acceptance"],
                                             "acceptance")
        self.assertTrue(any(p.startswith("PROJECTION_TAMPERED") for p in problems),
                        problems)

    def test_parent_artifact_tamper_invalidates_projection(self):
        paths = rp.project_paths(self.self_dir, "sec1")
        rp.write_projections(self.self_dir, self.review_rel,
                            expected_scopes=self.record["scopes"])
        review_path = os.path.join(self.self_dir,
                                   self.review_rel.replace("/", os.sep))
        with open(review_path, "a", encoding="utf-8") as fh:
            fh.write("\n")                 # 父产物被改写（哪怕多一个换行）
        for kind in ("semantic", "acceptance"):
            problems = rp.verify_projection_file(self.self_dir, paths[kind], kind)
            self.assertTrue(
                any(p.startswith("PROJECTION_PARENT_TAMPERED") for p in problems),
                (kind, problems))


# ---------------------------------------------------------------------------
# REV-02：缺节 / 缺 qid / 负面结论 / 创审同体 一律阻塞
# ---------------------------------------------------------------------------

class REV02BlockingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.self_dir = make_self(self.d, ("sec1", "sec2"))

    def test_missing_scope_blocks(self):
        record = review_record(self.self_dir, ("sec1",))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(self.self_dir, record, rel,
                                       scopes=["sec1", "sec2"])
        self.assertEqual(projections, {})   # 绝不从缺节产物投影出 PASS
        self.assertTrue(any(p.startswith("REVIEW_SCOPE_MISSING") for p in problems),
                        problems)

    def test_missing_qid_blocks(self):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(
            self.self_dir, record, rel,
            qids={"sec1": ["sec1-01", "sec1-02"], "sec2": ["sec2-01"]})
        self.assertEqual(projections, {})
        self.assertTrue(any(p.startswith("REVIEW_QID_MISSING") for p in problems),
                        problems)

    def test_revise_conclusion_blocks_even_with_shuffle_lineage(self):
        record = review_record(self.self_dir, ("sec1", "sec2"),
                               per_section={"sec2": {"semantic_conclusion": "REVISE"}})
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(projections, {})
        self.assertTrue(any(p.startswith("REVIEW_NOT_PASS") for p in problems),
                        problems)

        # 打乱血缘只证明「当前切片 = 被审读候选 + 确定性重排」，不得覆盖负面结论
        with open(os.path.join(self.self_dir, ".pd", "shuffle-transitions.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"transitions": []}, fh)
        projections2, problems2 = project(self.self_dir, record, rel)
        self.assertEqual(projections2, {})
        self.assertTrue(any(p.startswith("REVIEW_NOT_PASS") for p in problems2),
                        problems2)

    def test_needs_evidence_conclusion_blocks(self):
        record = review_record(self.self_dir, ("sec1", "sec2"),
                               per_section={"sec1": {
                                   "acceptance_conclusion": "NEEDS_EVIDENCE"}})
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(projections, {})
        self.assertTrue(any(p.startswith("REVIEW_NOT_PASS") for p in problems),
                        problems)

    def test_open_issue_blocks(self):
        record = review_record(self.self_dir, ("sec1", "sec2"),
                               per_section={"sec1": {"issues": [{
                                   "id": "i-1", "qid": "sec1-01", "part": "proof",
                                   "code": "MATH_ERROR", "evidence": "第 3 行",
                                   "status": "open", "resolution": "",
                                   "verified_digest": ""}]}})
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(projections, {})
        self.assertTrue(any(p.startswith("REVIEW_ISSUE_OPEN") for p in problems),
                        problems)

    def test_same_writer_identity_cannot_certify(self):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        # 写作台账：T2 写作批次，worker 与审读员同一身份
        agent_evidence.write_evidence(
            self.self_dir, dispatch_id="dsp-t2-sec1-1", batch_id="bat-t2-write01",
            worker=REVIEWER_WORKER, artifacts_written=["sec1.tex"],
            output_digests={"sec1.tex": sha(os.path.join(self.self_dir, "sec1.tex"))})
        seed_receipt(self.self_dir, record, rel, worker=REVIEWER_WORKER)

        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(problems, [])
        rp.write_projections(self.self_dir, rel,
                            expected_scopes=record["scopes"])
        paths = rp.project_paths(self.self_dir, "sec1")
        for kind in ("semantic", "acceptance"):
            found = rp.verify_projection_file(self.self_dir, paths[kind], kind)
            self.assertTrue(
                any(p.startswith("PROJECTION_REVIEWER_IDENTITY") for p in found),
                (kind, found))


# ---------------------------------------------------------------------------
# REV-03：html-only 不许伪造 PDF；pdf/both 缺 PDF 指纹即失败
# ---------------------------------------------------------------------------

class REV03FormatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name

    def test_html_only_needs_no_pdf_and_no_zero_hash(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="html")
        record = review_record(self_dir, ("sec1", "sec2"), fmt="html")
        rel = write_review(self_dir, record)
        seed_receipt(self_dir, record, rel)
        projections, problems = project(self_dir, record, rel, fmt="html")
        self.assertEqual(problems, [])
        acc = projections["acceptance"]["sec1"]
        self.assertIsNone(acc["student_pdf_sha256"])
        self.assertNotIn("student.pdf", acc["delivery_check_ref"]["artifact_digests"])
        self.assertEqual(evidence.validate_acceptance_json(
            acc, current_digest=record["input_digest"],
            student_html_sha=sha(os.path.join(self_dir, "student.html"))), [])

    def test_html_only_fake_pdf_proof_is_rejected(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="html")
        record = review_record(self_dir, ("sec1", "sec2"), fmt="html")
        record["artifact_digests"]["student.pdf"] = "0" * 64   # 零 hash 假证明
        rel = write_review(self_dir, record)
        seed_receipt(self_dir, record, rel)
        projections, problems = project(self_dir, record, rel, fmt="html")
        self.assertEqual(projections, {})
        self.assertTrue(any(p.startswith("REVIEW_BAD_DIGEST") for p in problems),
                        problems)
        self.assertTrue(any(p.startswith("REVIEW_FAKE_PDF_PROOF") for p in problems),
                        problems)

    def test_both_format_without_pdf_digest_fails(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="both")
        record = review_record(self_dir, ("sec1", "sec2"), fmt="both")
        record["artifact_digests"].pop("student.pdf")
        rel = write_review(self_dir, record)
        seed_receipt(self_dir, record, rel)
        projections, problems = project(self_dir, record, rel, fmt="both")
        self.assertEqual(projections, {})
        self.assertTrue(
            any(p.startswith("REVIEW_ARTIFACT_MISSING") and "student.pdf" in p
                for p in problems), problems)

    def test_stale_delivery_hash_fails(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="both")
        record = review_record(self_dir, ("sec1", "sec2"), fmt="both")
        rel = write_review(self_dir, record)
        seed_receipt(self_dir, record, rel)
        # 交付产物在本轮被重建 → 审读员看到的指纹过期
        with open(os.path.join(self_dir, "student.html"), "w",
                  encoding="utf-8") as fh:
            fh.write("<html><body>rebuilt</body></html>\n")
        projections, problems = project(self_dir, record, rel, fmt="both")
        self.assertEqual(projections, {})
        self.assertTrue(
            any(p.startswith("REVIEW_ARTIFACT_HASH_MISMATCH") for p in problems),
            problems)


# ---------------------------------------------------------------------------
# REV-04：旧 v1 验收不放宽；无 receipt 的投影不得认证
# ---------------------------------------------------------------------------

class REV04CompatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.self_dir = make_self(self.d, ("sec1", "sec2"))

    def _v1_record(self, **extra):
        rec = {
            "schema_version": 2,
            "scope": "sec1",
            "reviewed_qids": ["sec1-01"],
            "content_digest": "a" * 64,
            "student_pdf_sha256": None,
            "conclusion": "PASS",
            "reviewer_role": "sec1 切片独立教学验收专员",
            "stage1_record": "阶段一盲审记录",
            "stage2_record": "阶段二盲审记录",
            "issues": [],
        }
        rec.update(extra)
        return rec

    def test_v1_acceptance_rules_unchanged(self):
        self.assertEqual(evidence.validate_acceptance_json(self._v1_record()), [])
        # 缺 stage1_record → 旧 v1 仍必须报缺字段（新格式不得放宽旧档）
        broken = self._v1_record()
        broken.pop("stage1_record")
        self.assertTrue(evidence.validate_acceptance_json(broken))
        # PASS + open issue 仍必须失败
        self.assertTrue(evidence.validate_acceptance_json(self._v1_record(
            issues=[{"id": "i-1", "qid": "sec1-01", "part": "proof",
                     "code": "MATH_ERROR", "evidence": "x", "status": "open",
                     "resolution": "", "verified_digest": ""}])))
        # 旧 PASS 绑定旧摘要 → 仍必须失败
        self.assertTrue(evidence.validate_acceptance_json(
            self._v1_record(), current_digest="b" * 64))

    def test_v2_projection_is_not_a_v1_record(self):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(problems, [])
        acc = projections["acceptance"]["sec1"]
        self.assertEqual(evidence.validate_acceptance_json(
            acc, current_digest=record["input_digest"]), [])
        # 抹掉 review_kind 就会掉回旧 v1 分支 → 报缺 stage1_record
        stripped = {k: v for k, v in acc.items() if k != "review_kind"}
        problems = evidence.validate_acceptance_json(stripped)
        self.assertTrue(any("stage1_record" in p for p in problems), problems)

    def test_projection_without_receipt_is_not_certified(self):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)   # 只写文件，不落 receipt
        projections, problems = project(self.self_dir, record, rel)
        self.assertEqual(problems, [])
        rp.write_projections(self.self_dir, rel, expected_scopes=record["scopes"])
        paths = rp.project_paths(self.self_dir, "sec1")
        for kind in ("semantic", "acceptance"):
            found = rp.verify_projection_file(self.self_dir, paths[kind], kind)
            self.assertTrue(any(p.startswith("PROJECTION_NO_RECEIPT")
                                for p in found), (kind, found))

    def test_receipt_digest_mismatch_is_not_certified(self):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel, digest="c" * 64)
        rp.write_projections(self.self_dir, rel, expected_scopes=record["scopes"])
        paths = rp.project_paths(self.self_dir, "sec1")
        found = rp.verify_projection_file(self.self_dir, paths["semantic"], "semantic")
        self.assertTrue(any(p.startswith("PROJECTION_RECEIPT_MISMATCH")
                            for p in found), found)

    def test_semantic_node_blocks_unverifiable_projection(self):
        """语义硬闸门：投影不带可认证溯源 → invalid_result → BLOCK。"""
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel)
        rp.write_projections(self.self_dir, rel, expected_scopes=record["scopes"])
        sem_rel = rp.project_paths(self.self_dir, "sec1")["semantic"]
        digest = sha(os.path.join(self.self_dir, "sec1.tex"))

        status, detail = semantic_nodes.evaluate_checker_result(
            self.self_dir, "sec1", digest)
        self.assertEqual(status, "ok", detail)
        self.assertEqual(
            semantic_nodes.semantic_gate_verdict(self.self_dir, "sec1")[0], "PASS")

        # 父产物被改写 → 投影不可认证 → 语义闸门必须 BLOCK
        with open(os.path.join(self.self_dir, rel.replace("/", os.sep)), "a",
                  encoding="utf-8") as fh:
            fh.write("\n")
        status, detail = semantic_nodes.evaluate_checker_result(
            self.self_dir, "sec1", digest)
        self.assertEqual(status, "invalid_result", detail)
        self.assertIn("投影不可认证", detail)
        verdict, reason = semantic_nodes.semantic_gate_verdict(self.self_dir, "sec1")
        self.assertEqual(verdict, "BLOCK", reason)
        self.assertTrue(os.path.isfile(
            os.path.join(self.self_dir, sem_rel.replace("/", os.sep))))


# ---------------------------------------------------------------------------
# 派发计划：R 批覆盖全部待审节
# ---------------------------------------------------------------------------

class FakePool:
    def __init__(self, results=None, deferred=0):
        self.results = dict(results or {})
        self.deferred = int(deferred)
        self.submitted = []

    def submit(self, request):
        self.submitted.append(request.get("dispatch_id"))
        return request.get("dispatch_id")

    def join(self, timeout=None):
        return dict(self.results)

    def stats(self):
        n = len(self.submitted)
        return {"submitted": n, "completed": n, "ok": 0, "failed": 0,
                "timeout": 0, "no_change": 0, "deferred": self.deferred,
                "retried": 0, "max_in_flight": n, "orphans": 0}

    def reset(self):
        return None


def ok_result(request):
    return DispatchResult(dispatch_id=request.get("dispatch_id"),
                          scope=request.get("scope"), status="ok",
                          output="合并终审主产物已写入", error=None,
                          attempts=1, duration_ms=1)


class PlanTest(unittest.TestCase):
    def setUp(self):
        sbn._reset_pool()
        self._unregister = register_capable_worker(8)
        self.addCleanup(self._unregister)
        self.addCleanup(sbn._reset_pool)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name

    def test_plan_batches_cover_every_scope(self):
        sections = ("sec1", "sec2", "sec3", "sec4")
        self_dir = make_self(self.d, sections)
        state = base_state(self_dir, sections)
        patch = frn.node_t4_plan_combined_review(state)
        self.assertEqual(patch["stage"], "T4_COMBINED_REVIEW_PENDING", patch)
        plan = patch["dispatch_plan"]
        self.assertEqual(plan["stage"], "T4_COMBINED_REVIEW")
        self.assertEqual(len(plan["requests_full"]), 2)  # 4 节 / 每批 2 节
        covered = []
        for req in plan["requests_full"]:
            self.assertEqual(req["stage"], "T4_COMBINED_REVIEW")
            self.assertTrue(req["role"].startswith(rp.REVIEWER_ROLE_PREFIX))
            self.assertEqual(req["prompt_file"], rp.REVIEW_PROMPT_FILE)
            self.assertEqual(req["reviewer_role_kind"], "combined_source_fidelity")
            self.assertEqual(req["scope"], req["review_scopes"][0])
            self.assertEqual(len(req["review_scopes"]), 2)
            covered.extend(req["review_scopes"])
        self.assertEqual(sorted(covered), sorted(sections))
        self.assertEqual(rp.validate_review_plan(
            plan, expected_stage="T4_COMBINED_REVIEW",
            batch_id=plan["batch_id"]), [])
        report_path = os.path.join(self_dir,
                                   frn.COMBINED_REVIEW_REPORT_REL.replace("/", os.sep))
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["status"], "PASS")
        plan_path = os.path.join(self_dir,
                                frn.COMBINED_REVIEW_PLAN_REL.replace("/", os.sep))
        self.assertTrue(os.path.isfile(plan_path))

    def test_plan_gate_rejects_under_dispatch(self):
        sections = ("sec1", "sec2")
        self_dir = make_self(self.d, sections)
        state = base_state(self_dir, sections)
        patch = frn.node_t4_plan_combined_review(state)
        plan = json.loads(json.dumps(patch["dispatch_plan"]))
        for req in (plan["requests"][0], plan["requests_full"][0]):
            req["review_scopes"] = ["sec1"]      # 只审第一节
            req["scope_input_digests"] = {
                "sec1": req["scope_input_digests"]["sec1"]}
        plan["sections"] = ["sec1", "sec2"]
        problems = rp.validate_review_plan(
            plan, expected_stage="T4_COMBINED_REVIEW",
            batch_id=plan["batch_id"])
        self.assertTrue(any(p.startswith("PLAN_UNDER_DISPATCH") for p in problems),
                        problems)

    def test_dispatch_halts_on_stale_plan(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"), batch_id="bat-t4c-stale",
                           dispatch_plan={"batch_id": "bat-t4c-other",
                                          "requests_full": [{"dispatch_id": "x"}]})
        patch = frn.node_t4_dispatch_combined_review(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("stale plan", patch["error_message"])

    def test_dispatch_submits_all_requests(self):
        sections = ("sec1", "sec2", "sec3", "sec4")
        self_dir = make_self(self.d, sections)
        plan_patch = frn.node_t4_plan_combined_review(
            base_state(self_dir, sections))
        pool = FakePool()
        state = base_state(self_dir, sections,
                           batch_id=plan_patch["batch_id"],
                           dispatch_plan=plan_patch["dispatch_plan"])
        with mock.patch.object(frn, "_get_pool", return_value=pool):
            patch = frn.node_t4_dispatch_combined_review(state)
        self.assertEqual(patch["stage"], "T4_COMBINED_REVIEW_RUNNING")
        self.assertEqual(len(pool.submitted), 2)


class JoinTest(unittest.TestCase):
    def setUp(self):
        sbn._reset_pool()
        self._unregister = register_capable_worker(8)
        self.addCleanup(self._unregister)
        self.addCleanup(sbn._reset_pool)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name

    def _plan(self, sections=("sec1", "sec2"), fmt="both"):
        self.self_dir = make_self(self.d, sections, fmt=fmt)
        patch = frn.node_t4_plan_combined_review(
            base_state(self.self_dir, sections, fmt=fmt))
        self.assertEqual(patch["stage"], "T4_COMBINED_REVIEW_PENDING", patch)
        return patch

    def _produce(self, plan_patch, worker=REVIEWER_WORKER, semantic="PASS",
                 receipt=True):
        """模拟独立审读子 Agent 产出主产物 + 宿主登记 receipt。"""
        results = {}
        for req in plan_patch["dispatch_plan"]["requests_full"]:
            record = review_record(
                self.self_dir, tuple(req["review_scopes"]),
                fmt=req["context"]["output_format"],
                dispatch_id=req["dispatch_id"],
                batch_id=req["batch_id"],
                input_digest=req["input_digest"],
                semantic=semantic)
            rel = write_review(self.self_dir, record)
            self.assertEqual(rel, req["artifacts"][0])
            if receipt:
                seed_receipt(self.self_dir, record, rel, worker=worker)
            results[req["dispatch_id"]] = ok_result(req)
        return results

    def _join(self, plan_patch, results):
        state = base_state(self.self_dir,
                           plan_patch["dispatch_plan"]["expected_sections"],
                           fmt=plan_patch["dispatch_plan"]["output_format"],
                           batch_id=plan_patch["batch_id"],
                           dispatch_plan=plan_patch["dispatch_plan"])
        pool = FakePool(results=results)
        with mock.patch.object(frn, "_live_pool", return_value=pool):
            return frn.node_t4_join_combined_review(state)

    def test_join_pass_projects_and_aggregates(self):
        plan_patch = self._plan()
        results = self._produce(plan_patch)
        patch = self._join(plan_patch, results)
        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING", patch)
        self.assertEqual(patch["combined_review_conclusion"], "PASS")
        self.assertEqual(patch["combined_review_problems"], [])

        chapter_path = os.path.join(
            self.self_dir, ".pd/acceptance/chapter.json".replace("/", os.sep))
        with open(chapter_path, encoding="utf-8") as fh:
            chapter = json.load(fh)
        self.assertEqual(chapter["conclusion"], "PASS")
        self.assertEqual(sorted(chapter["reviewed_qids"]), ["sec1-01", "sec2-01"])
        self.assertIn("合并终审", chapter["stage1_record"])
        self.assertEqual(evidence.validate_acceptance_json(chapter), [])

        # 两套投影都落盘且可认证
        for scope in ("sec1", "sec2"):
            paths = rp.project_paths(self.self_dir, scope)
            for kind in ("semantic", "acceptance"):
                self.assertEqual(
                    rp.verify_projection_file(self.self_dir, paths[kind], kind), [])

    def test_join_halts_without_receipt(self):
        plan_patch = self._plan()
        results = self._produce(plan_patch, receipt=False)
        patch = self._join(plan_patch, results)
        self.assertEqual(patch["stage"], "HALTED", patch)
        self.assertTrue(any("REVIEW_UNATTESTED" in p
                            for p in patch["combined_review_problems"]),
                        patch["combined_review_problems"])
        chapter_path = os.path.join(
            self.self_dir, ".pd/acceptance/chapter.json".replace("/", os.sep))
        with open(chapter_path, encoding="utf-8") as fh:
            chapter = json.load(fh)
        self.assertNotEqual(chapter["conclusion"], "PASS")

    def test_join_halts_on_same_writer_identity(self):
        plan_patch = self._plan()
        results = self._produce(plan_patch, worker=WRITER_WORKER)
        # 该 worker 同时出现在写作台账里
        agent_evidence.write_evidence(
            self.self_dir, dispatch_id="dsp-t2-sec1-9", batch_id="bat-t2-write09",
            worker=WRITER_WORKER, artifacts_written=["sec1.tex"],
            output_digests={"sec1.tex": sha(
                os.path.join(self.self_dir, "sec1.tex"))})
        patch = self._join(plan_patch, results)
        self.assertEqual(patch["stage"], "HALTED", patch)
        joined = " ".join(patch["combined_review_problems"])
        self.assertIn("PROJECTION_REVIEWER_IDENTITY", joined)

    def test_join_reports_revise(self):
        plan_patch = self._plan()
        results = self._produce(plan_patch, semantic="REVISE")
        patch = self._join(plan_patch, results)
        # REVISE 是审读员的真实负面结论：必须先被主产物校验拦下（不进投影）
        self.assertEqual(patch["stage"], "HALTED", patch)
        self.assertNotEqual(patch["combined_review_conclusion"], "PASS")


# ---------------------------------------------------------------------------
# 审计：合并终审批次必须被认定为「审读角色」而非写作者
# ---------------------------------------------------------------------------

class AuditRoleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.self_dir = make_self(self.d, ("sec1", "sec2"))

    def _seed(self, reviewer_worker):
        record = review_record(self.self_dir, ("sec1", "sec2"))
        rel = write_review(self.self_dir, record)
        seed_receipt(self.self_dir, record, rel, worker=reviewer_worker)
        write_request(self.self_dir, record["dispatch_id"],
                      "T4_COMBINED_REVIEW", record["batch_id"], "sec1", [rel],
                      review_scopes=["sec1", "sec2"],
                      reviewer_role_kind="combined_source_fidelity")
        return record, rel

    def _writer(self):
        agent_evidence.write_evidence(
            self.self_dir, dispatch_id="dsp-t2-sec1-1", batch_id="bat-t2-write01",
            worker=WRITER_WORKER, artifacts_written=["sec1.tex"],
            output_digests={"sec1.tex": sha(
                os.path.join(self.self_dir, "sec1.tex"))})

    def test_combined_reviewer_not_counted_as_writer(self):
        self._writer()
        self._seed(REVIEWER_WORKER)
        report = check_agent_audit.audit_agent_evidence(self.self_dir)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("REVIEWER_IDENTITY_VIOLATION", blob)

    def test_combined_reviewer_same_as_writer_is_violation(self):
        self._writer()
        self._seed(WRITER_WORKER)
        report = check_agent_audit.audit_agent_evidence(self.self_dir)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertIn("REVIEWER_IDENTITY_VIOLATION", blob)


# ---------------------------------------------------------------------------
# 卡22：合并终审在「宿主批派发（manual/deferred）」路径下的中断与恢复
# ---------------------------------------------------------------------------

class ManualDispatchChainTest(unittest.TestCase):
    """没有 python worker 时（生产真实形态：宿主 subagent 派发），合并终审必须
    像 T2 写作 / legacy T4 验收那样走「批中断 → 宿主派发 → 磁盘重推导」。

    失败前复现（卡22 修前实测）
    --------------------------
    ``node_t4_dispatch_combined_review`` 在池内请求全部 deferred 时返回
    ``dispatch_mode="manual"``、``stage="T4_COMBINED_REVIEW_RUNNING"``，而
    ``node_t4_join_combined_review`` **没有任何 manual/磁盘重推导分支**：

    * 它只读 ``_live_pool(batch_id).join()``，宿主派发永远拿不到结果；
    * 于是每个请求都被判 ``REVIEW_RESULT: … 结果状态 'missing_result'（非 ok）``
      → ``stage="HALTED"``、``routing_action="HALT"``；
    * 结果：fast 生产链（无 worker）里合并终审**必然空转失败**，宿主连被派发的
      机会都没有，graph 里 ``T4_COMBINED_REVIEW_RUNNING → t4_join_combined_review``
      的自环也永远走不到。
    """

    def setUp(self):
        sbn._reset_pool()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self.sections = ("sec1", "sec2", "sec3", "sec4")
        self.self_dir = make_self(self.d, self.sections)
        self.captured = []

        original = getattr(frn, "interrupt", None)

        def fake_interrupt(payload):
            self.captured.append(payload)
            # 恢复侧只认 host-subagent-v1（防主 Agent 自写）：
            return {"dispatch_protocol": "host-subagent-v1"}

        frn.interrupt = fake_interrupt
        self.addCleanup(lambda: setattr(frn, "interrupt", original))

        self.plan_patch = frn.node_t4_plan_combined_review(
            base_state(self.self_dir, self.sections))
        self.assertEqual(self.plan_patch["stage"],
                         "T4_COMBINED_REVIEW_PENDING", self.plan_patch)
        self.plan = self.plan_patch["dispatch_plan"]
        self.requests = list(self.plan["requests_full"])
        self.assertEqual(len(self.requests), 2, self.requests)

    # --- 宿主派发夹具（池内请求全部 deferred = 没有 python worker 能跑）---
    def _state(self, **extra):
        state = base_state(self.self_dir, self.sections,
                           batch_id=self.plan_patch["batch_id"],
                           dispatch_plan=self.plan)
        state.update(extra)
        return state

    def _host_dispatch(self, state):
        pool = FakePool(results={}, deferred=len(self.requests))
        with mock.patch.object(frn, "_get_pool", return_value=pool):
            return frn.node_t4_dispatch_combined_review(state)

    def _host_join(self, state):
        pool = FakePool(results={}, deferred=len(self.requests))
        with mock.patch.object(frn, "_live_pool", return_value=pool):
            return frn.node_t4_join_combined_review(state)

    def _produce(self, only=None, worker=REVIEWER_WORKER):
        """模拟宿主子 Agent 产出批级审读主产物 + 宿主登记 receipt。"""
        produced = []
        for index, req in enumerate(self.requests, start=1):
            if only is not None and req["dispatch_id"] not in only:
                continue
            record = review_record(
                self.self_dir, tuple(req["review_scopes"]),
                fmt=req["context"]["output_format"],
                dispatch_id=req["dispatch_id"],
                batch_id=req["batch_id"],
                input_digest=req["input_digest"])
            rel = write_review(self.self_dir, record,
                               batch_id=req["batch_id"], index=index)
            self.assertEqual(rel, req["artifacts"][0])
            seed_receipt(self.self_dir, record, rel, worker=worker)
            produced.append(req["dispatch_id"])
        return produced

    # --- 用例 ---
    def test_host_dispatch_interrupts_once_then_settles_from_disk(self):
        state = self._state()
        dispatch = self._host_dispatch(state)
        self.assertEqual(dispatch["stage"], "T4_COMBINED_REVIEW_RUNNING")
        self.assertEqual(dispatch["dispatch_mode"], "manual")
        state.update(dispatch)

        # ① 首次进入收尾：必须携带整批派发单中断，而不是判 missing_result。
        patch = self._host_join(state)
        self.assertEqual(len(self.captured), 1, patch)
        payload = self.captured[0]
        self.assertEqual(payload.get("type"), "AGENT_DISPATCH")
        self.assertEqual(payload.get("batch_id"), self.plan_patch["batch_id"])
        self.assertEqual(payload.get("dispatch_protocol"), "host-subagent-v1")
        self.assertEqual(payload.get("dispatch_args", {}).get("batch_id"),
                         self.plan_patch["batch_id"])
        self.assertEqual(payload.get("dispatch_args", {}).get("self_dir"),
                         self.self_dir)
        batch_scopes = [tuple(r.get("review_scopes") or ())
                        for r in payload.get("requests") or []]
        self.assertEqual(batch_scopes,
                         [tuple(r["review_scopes"]) for r in self.requests])
        self.assertEqual(patch.get("stage"), "T4_COMBINED_REVIEW_RUNNING",
                         patch)
        self.assertEqual(patch.get("manual_interrupted_batch"),
                         self.plan_patch["batch_id"])
        self.assertEqual(patch.get("dispatch_mode"), "manual")

        # ② 宿主写完全部主产物 + receipt 后恢复：从磁盘重推导，不再二次中断。
        self._produce()
        state.update(patch)
        patch2 = self._host_join(state)
        self.assertEqual(len(self.captured), 1, "同一批次绝不二次 interrupt")
        self.assertEqual(patch2.get("stage"), "T4_ACCEPTANCE_PENDING", patch2)
        self.assertEqual(patch2.get("combined_review_conclusion"), "PASS")
        self.assertEqual(patch2.get("combined_review_problems"), [])
        for scope in self.sections:
            paths = rp.project_paths(self.self_dir, scope)
            for kind in ("semantic", "acceptance"):
                self.assertEqual(
                    rp.verify_projection_file(self.self_dir, paths[kind], kind),
                    [])

    def test_host_settle_without_artifact_blocks(self):
        state = self._state()
        state.update(self._host_dispatch(state))
        patch = self._host_join(state)
        self.assertEqual(len(self.captured), 1, patch)

        # 宿主什么都没写就恢复：不得静默放行（缺产物必须阻塞交付）。
        state.update(patch)
        patch2 = self._host_join(state)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(patch2.get("stage"), "HALTED", patch2)
        joined = " ".join(patch2.get("combined_review_problems") or [])
        self.assertTrue("REVIEW_RESULT" in joined or "REVIEW_UNATTESTED" in joined,
                        joined)
        self.assertNotEqual(patch2.get("combined_review_conclusion"), "PASS")

    def test_partial_host_settle_keeps_finished_batch(self):
        state = self._state()
        state.update(self._host_dispatch(state))
        patch = self._host_join(state)
        state.update(patch)

        # 只完成第一批：已完成的 scope 有投影，未完成的 scope 不得被冒充。
        done = self._produce(only={self.requests[0]["dispatch_id"]})
        self.assertEqual(len(done), 1)
        patch2 = self._host_join(state)
        self.assertEqual(patch2.get("stage"), "HALTED", patch2)
        self.assertNotEqual(patch2.get("combined_review_conclusion"), "PASS")
        problems = " ".join(patch2.get("combined_review_problems") or [])
        self.assertIn(self.requests[1]["dispatch_id"], problems)
        self.assertNotIn(self.requests[0]["dispatch_id"], problems)

        done_scopes = list(self.requests[0]["review_scopes"])
        left_scopes = list(self.requests[1]["review_scopes"])
        for scope in done_scopes:
            paths = rp.project_paths(self.self_dir, scope)
            for kind in ("semantic", "acceptance"):
                self.assertEqual(
                    rp.verify_projection_file(self.self_dir, paths[kind], kind),
                    [])
        for scope in left_scopes:
            rel = rp.project_paths(self.self_dir, scope)["acceptance"]
            self.assertFalse(os.path.isfile(
                os.path.join(self.self_dir, rel.replace("/", os.sep))),
                "未完成的批次不得生成验收投影（禁止跨域覆盖）")


if __name__ == "__main__":
    unittest.main()
