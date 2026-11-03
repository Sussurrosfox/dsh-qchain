# -*- coding: utf-8 -*-
"""任务卡13 回归：单作者多节（scopes）+ 逐节认证 + 部分失败恢复。

覆盖 tasks/13.md 的 BATCH-01..06：
  BATCH-01 一个请求覆盖两节 → check_dispatch 按 scope 并集通过；
           缺 scope / 写域重叠 / 越界 artifact 必须失败。
  BATCH-02 sec1 成功 sec2 失败 → 只重派 sec2，逐节 attempt+hash 可追踪。
  BATCH-03 只认证一件产物 → 不得让整批 superseded，也不得通过 provenance。
  BATCH-04 中断恢复保持已成功节；旧单 scope 派发单仍可认证。
  BATCH-05 超限不截断原文；请求数 K 与声明批次一致。
  BATCH-06 四节分两请求可重叠且 scope/写域不冲突；合批不被强拆凑并发。

设计约束（repair-work 硬规则）：
  * 所有 fs 写入都落在 tempfile 临时目录，绝不碰 D:\\teaching 下任何项目目录；
  * 不改生产插件 C:\\Users\\63254\\.dsh\\plugins\\dsh-qchain；
  * 不启动真实流水线、不调真实 agent/模型、不读 phase0 令牌。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
for _p in (PLUGIN_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_dispatch                       # noqa: E402
import check_provenance                     # noqa: E402
import check_agent_audit                    # noqa: E402
import request_scopes                       # noqa: E402
from workflow.nodes import agent_evidence   # noqa: E402
from workflow.nodes import agent_nodes      # noqa: E402
from workflow.nodes import section_batch_nodes as sbn   # noqa: E402


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
SOURCE_ASSETS = {"figure": 0, "table": 0, "tikz": 0, "available": True}


# --------------------------------------------------------------------------
# 治具：全部写入临时目录
# --------------------------------------------------------------------------
def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _Project(object):
    """最小 self 工程：main.tex + 切片 + coverage-map.md。"""

    def __init__(self, root: str, sections=("sec1", "sec2")):
        self.root = root
        self.self_dir = os.path.join(root, "self")
        os.makedirs(os.path.join(self.self_dir, ".pd"), exist_ok=True)
        lines = ["# 学案主文件", ""]
        for sec in sections:
            lines.append("\\input{%s.tex}" % sec)
        _write(os.path.join(self.self_dir, "main.tex"), "\n".join(lines) + "\n")
        cov = ["# 覆盖清单", "", "## 切片清单", ""]
        for sec in sections:
            cov.append("- %s：主要内容" % sec)
        cov += ["", "## 规划节点总表", "", "| qid | 节点 | 来源 |", "| --- | --- | --- |"]
        for sec in sections:
            cov.append("| %s-q1 | %s 节点 | 大纲 |" % (sec, sec))
        _write(os.path.join(self.self_dir, "coverage-map.md"), "\n".join(cov) + "\n")
        self.sections = list(sections)

    def section_text(self, sec: str) -> str:
        return "% qid: {0}-q1\n\\section{{{0}}}\n正文内容。\n".format(sec)

    def write_section(self, sec: str, text: str = None) -> str:
        rel = "%s.tex" % sec
        _write(os.path.join(self.self_dir, rel), text if text is not None else self.section_text(sec))
        return _sha256(os.path.join(self.self_dir, rel))

    def write_semantic(self, sec: str, digest: str) -> None:
        payload = {
            "schema_version": 1,
            "scope": sec,
            "section_digest": digest,
            "conclusion": "PASS",
            "issues": [],
        }
        _write(os.path.join(self.self_dir, ".pd", "semantic", "%s.json" % sec),
               json.dumps(payload, ensure_ascii=False))

    def write_request(self, request: dict) -> str:
        rel = ".pd/agent-requests/%s.json" % request["dispatch_id"]
        _write(os.path.join(self.self_dir, rel), json.dumps(request, ensure_ascii=False))
        return rel


def make_request(dispatch_id: str, scopes, artifacts=None, *, schema_version=2,
                 stage="T2", input_digest=DIGEST_A, artifact_scopes=None,
                 before=None):
    """构造一个 v2 多 scope（或 legacy 单 scope）派发单。"""
    if artifacts is None:
        artifacts = ["%s.tex" % s for s in scopes]
    if artifact_scopes is None:
        artifact_scopes = {a: s for a, s in zip(artifacts, scopes)}
    req = {
        "dispatch_id": dispatch_id,
        "stage": stage,
        "role": "writer",
        "prompt_file": "subagent_prompt.md",
        "artifacts": list(artifacts),
        "instruction": "撰写 %s" % ",".join(scopes),
        "command": "python scripts/pdstate.py self/ submit %s" % scopes[0],
        "context": {"source_assets": dict(SOURCE_ASSETS)},
        "batch_id": "bat-t2-test",
        "scope": scopes[0],
        "input_digest": input_digest,
        "status": "pending",
        "attempt": 0,
        "before": dict(before or {}),
    }
    if schema_version >= 2:
        req["schema_version"] = 2
        req["scopes"] = list(scopes)
        req["artifact_scopes"] = dict(artifact_scopes)
        req["scope_inputs"] = {s: DIGEST_B for s in scopes}
    return req


def make_plan(sections, requests, **overrides):
    plan = {
        "batch_id": "bat-t2-test",
        "stage": "T2",
        "created_at": "2026-01-01T00:00:00+00:00",
        "max_concurrency": 4,
        "min_parallelism": 1,
        "sections": list(sections),
        "required_scope_count": len(sections),
        "batch_size": len(requests),
        "dispatch_contract_version": 2,
        "requests": [
            {
                "dispatch_id": r["dispatch_id"],
                "scope": r["scope"],
                "role": r.get("role", "writer"),
                "input_digest": r.get("input_digest", DIGEST_A),
                "artifacts": list(r.get("artifacts") or []),
                "status": r.get("status", "pending"),
                "batch_id": r.get("batch_id", "bat-t2-test"),
                "source_assets": dict(SOURCE_ASSETS),
                **({"scopes": list(r["scopes"]),
                    "artifact_scopes": dict(r["artifact_scopes"])}
                   if r.get("schema_version", 0) >= 2 else {}),
            }
            for r in requests
        ],
    }
    plan.update(overrides)
    return plan


def _codes(errors):
    return [e.get("code") for e in errors]


# --------------------------------------------------------------------------
# 计划门禁：BATCH-01 / BATCH-05 / BATCH-06
# --------------------------------------------------------------------------
class BATCH01_PlanCoverage(unittest.TestCase):
    """BATCH-01：两节一请求通过 coverage；缺 scope/重叠写域/越界 artifact 失败。"""

    def test_two_sections_one_request_passes(self):
        req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
        plan = make_plan(["sec1", "sec2"], [req], required_scope_count=2)
        errors, warnings, stats = check_dispatch.check_plan(plan)
        self.assertEqual(_codes(errors), [], errors)
        self.assertEqual(stats["sections"], 2)
        self.assertEqual(stats["requests"], 1)

    def test_missing_scope_coverage_fails(self):
        # 只声明 sec1，但计划里有两个待处理节 → 并集不完整
        req = make_request("dsp-t2-sec1-0001", ["sec1"])
        plan = make_plan(["sec1", "sec2"], [req], required_scope_count=2)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertIn("UNDER_DISPATCH", _codes(errors), errors)

    def test_overlapping_write_domain_fails(self):
        req_a = make_request("dsp-t2-sec1-0001", ["sec1"])
        req_b = make_request("dsp-t2-sec1-0002", ["sec1"])
        plan = make_plan(["sec1"], [req_a, req_b], required_scope_count=1)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertIn("DUPLICATE_SCOPE", _codes(errors), errors)

    def test_artifact_outside_scope_fails(self):
        req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"],
                           artifacts=["sec1.tex", "sec9.tex"],
                           artifact_scopes={"sec1.tex": "sec1", "sec9.tex": "sec9"})
        plan = make_plan(["sec1", "sec2"], [req], required_scope_count=2)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertIn("BAD_SCOPE", _codes(errors), errors)

    def test_artifact_domain_mismatch_fails(self):
        # artifact 明文声明 sec2.tex 属于 sec1 → 写域与 scope 声明不一致
        req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"],
                           artifact_scopes={"sec1.tex": "sec1", "sec2.tex": "sec1"})
        plan = make_plan(["sec1", "sec2"], [req], required_scope_count=2)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertIn("BAD_SCOPE", _codes(errors), errors)

    def test_legacy_single_scope_request_still_passes(self):
        req = make_request("dsp-t2-sec1-0001", ["sec1"], schema_version=1)
        req.pop("scopes", None)
        req.pop("artifact_scopes", None)
        plan = make_plan(["sec1"], [req], required_scope_count=1,
                         dispatch_contract_version=1)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertEqual(_codes(errors), [], errors)

    def test_legacy_one_request_per_section_requirement_kept(self):
        """legacy 计划（无 scopes）仍要求每节一个请求——旧断言不得回退。"""
        req = make_request("dsp-t2-sec1-0001", ["sec1"], schema_version=1)
        req.pop("scopes", None)
        req.pop("artifact_scopes", None)
        plan = make_plan(["sec1", "sec2", "sec3"], [req], required_scope_count=3,
                         dispatch_contract_version=1)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertIn("UNDER_DISPATCH", _codes(errors), errors)


class BATCH05_NoTruncation(unittest.TestCase):
    """BATCH-05：超限不截断原文；K 与声明一致；单节超长精确失败。"""

    def _cfg(self, self_dir, max_sections=2, max_chars=16000):
        cfg = {k: v for k, v in sbn._load_dispatch_config(self_dir).items()}
        cfg["writer_batch_max_sections"] = max_sections
        cfg["writer_batch_max_source_chars"] = max_chars
        cfg["max_concurrency"] = 4
        cfg["batch_window"] = 2
        return cfg

    def test_defaults_present(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            cfg = self._cfg(proj.self_dir)
            self.assertEqual(cfg["writer_batch_max_sections"], 2)
            self.assertEqual(cfg["writer_batch_max_source_chars"], 16000)

    def test_adjacent_two_sections_grouped_into_one_request(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2", "sec3"))
            cfg = self._cfg(proj.self_dir)
            groups = sbn.plan_writer_batches(
                ["sec1", "sec2", "sec3"], cfg,
                source_chars={"sec1": 10, "sec2": 10, "sec3": 10})
            self.assertEqual(groups, [["sec1", "sec2"], ["sec3"]])

    def test_non_adjacent_sections_not_grouped(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2", "sec3"))
            cfg = self._cfg(proj.self_dir)
            groups = sbn.plan_writer_batches(
                ["sec1", "sec3"], cfg,
                source_chars={"sec1": 10, "sec3": 10})
            self.assertEqual(groups, [["sec1"], ["sec3"]])

    def test_source_over_budget_splits_without_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            cfg = self._cfg(proj.self_dir, max_chars=1000)
            groups = sbn.plan_writer_batches(
                ["sec1", "sec2"], cfg,
                source_chars={"sec1": 800, "sec2": 800})
            self.assertEqual(groups, [["sec1"], ["sec2"]])

    def test_prompt_over_budget_splits_without_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            cfg = self._cfg(proj.self_dir)
            cfg["agent_prompt_max_chars"] = 26000
            groups = sbn.plan_writer_batches(
                ["sec1", "sec2"], cfg,
                source_chars={"sec1": 10, "sec2": 10},
                prompt_chars=lambda scopes: 20000 * len(scopes))
            self.assertEqual(groups, [["sec1"], ["sec2"]])

    def test_single_section_over_budget_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1",))
            cfg = self._cfg(proj.self_dir, max_chars=100)
            with self.assertRaises(sbn.WriterBatchError) as ctx:
                sbn.plan_writer_batches(["sec1"], cfg,
                                        source_chars={"sec1": 500})
            self.assertEqual(ctx.exception.code, "WRITER_SOURCE_OVERFLOW")

    def test_request_count_matches_declared_batches(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2", "sec3", "sec4"))
            cfg = self._cfg(proj.self_dir)
            requests, groups, lines = sbn._build_requests(
                self._state(proj), proj.self_dir,
                ["sec1", "sec2", "sec3", "sec4"], "bat-t2-test", cfg,
                dict(SOURCE_ASSETS),
                source_chars={s: 10 for s in proj.sections})
            self.assertEqual(len(requests), len(groups))
            self.assertEqual(len(requests), 2)
            self.assertEqual([r["scopes"] for r in requests],
                             [["sec1", "sec2"], ["sec3", "sec4"]])
            # 每节都出现在某个请求的 scopes 里，且只出现一次
            flattened = [s for r in requests for s in r["scopes"]]
            self.assertEqual(flattened, ["sec1", "sec2", "sec3", "sec4"])

    def test_plan_signs_batch_size_equal_to_k(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2", "sec3", "sec4"))
            cfg = self._cfg(proj.self_dir)
            requests, groups, _ = sbn._build_requests(
                self._state(proj), proj.self_dir, list(proj.sections),
                "bat-t2-test", cfg, dict(SOURCE_ASSETS),
                source_chars={s: 10 for s in proj.sections})
            plan = sbn._disk_plan("bat-t2-test", proj.sections, requests, 4,
                                  effective_limit=4,
                                  worker_capabilities={"max_concurrency": 4})
            self.assertEqual(plan["batch_size"], len(requests))
            self.assertEqual(plan["required_scope_count"], 4)
            errors, _w, _s = check_dispatch.check_plan(plan)
            self.assertEqual(_codes(errors), [], errors)

    def _state(self, proj):
        return {
            "self_dir": proj.self_dir,
            "project_root": proj.root,
            "run_id": "test-run",
            "expected_sections": list(proj.sections),
            "section_statuses": {s: "pending" for s in proj.sections},
            "user_spec": {"main_title": "测试学案"},
            "history": [],
        }


class BATCH06_FourSectionsTwoRequests(unittest.TestCase):
    """BATCH-06：四节分两请求可重叠；合批不被强拆凑并发。"""

    def test_gate_accepts_two_requests_four_sections(self):
        req_a = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
        req_b = make_request("dsp-t2-sec3sec4-0002", ["sec3", "sec4"])
        plan = make_plan(["sec1", "sec2", "sec3", "sec4"], [req_a, req_b],
                         required_scope_count=4, max_concurrency=2,
                         min_parallelism=2)
        errors, warnings, _s = check_dispatch.check_plan(plan)
        self.assertEqual(_codes(errors), [], errors)
        self.assertNotIn("LOW_PARALLELISM", _codes(warnings))

    def test_concurrency_cap_not_used_to_split_batches(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2", "sec3", "sec4"))
            cfg = dict(sbn._load_dispatch_config(proj.self_dir))
            cfg["max_concurrency"] = 1     # 并发上限 1 也不得把合批拆开
            cfg["batch_window"] = 2
            cfg["writer_batch_max_sections"] = 2
            cfg["writer_batch_max_source_chars"] = 16000
            groups = sbn.plan_writer_batches(
                ["sec1", "sec2", "sec3", "sec4"], cfg,
                source_chars={s: 10 for s in proj.sections})
            self.assertEqual(groups, [["sec1", "sec2"], ["sec3", "sec4"]])

    def test_zero_requests_allowed_when_nothing_pending(self):
        """没有待处理节 → 允许零派发（走 join），不报 UNDER_DISPATCH。"""
        plan = make_plan([], [], required_scope_count=0)
        errors, _w, _s = check_dispatch.check_plan(plan)
        self.assertNotIn("UNDER_DISPATCH", _codes(errors), errors)


# --------------------------------------------------------------------------
# 请求契约：build_request 的 v2 scopes
# --------------------------------------------------------------------------
class BATCH01_RequestContract(unittest.TestCase):

    def _state(self, proj):
        return {"self_dir": proj.self_dir, "run_id": "test-run"}

    def test_legacy_single_scope_request_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1",))
            req = agent_nodes.build_request(
                self._state(proj), stage="T2", role="writer",
                prompt_file="subagent_prompt.md", artifacts=["sec1.tex"],
                instruction="撰写 sec1", command="cmd", scope="sec1")
            self.assertEqual(req["scope"], "sec1")
            self.assertNotIn("scopes", req)
            self.assertNotIn("schema_version", req)
            self.assertEqual(request_scopes.request_scopes(req), ["sec1"])

    def test_two_scope_request_declares_scopes_and_maps(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            req = agent_nodes.build_request(
                self._state(proj), stage="T2", role="writer",
                prompt_file="subagent_prompt.md",
                artifacts=["sec1.tex", "sec2.tex"],
                instruction="撰写 sec1+sec2", command="cmd", scopes=["sec1", "sec2"],
                scope_inputs={"sec1": "s1", "sec2": "s2"})
            self.assertEqual(req["schema_version"], 2)
            self.assertEqual(req["scopes"], ["sec1", "sec2"])
            self.assertEqual(req["scope"], "sec1")      # 首元素兼容标签
            self.assertEqual(req["artifact_scopes"],
                             {"sec1.tex": "sec1", "sec2.tex": "sec2"})
            self.assertIn("sec1", req["scope_inputs"])
            self.assertIn("sec2", req["scope_inputs"])
            self.assertEqual(request_scopes.request_scopes(req), ["sec1", "sec2"])
            self.assertEqual(request_scopes.declaration_errors(req), [])

    def test_digest_binds_every_scope(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            common = dict(state=self._state(proj), stage="T2", role="writer",
                          prompt_file="subagent_prompt.md", command="cmd")
            one = agent_nodes.build_request(artifacts=["sec1.tex", "sec2.tex"],
                                            instruction="x", scopes=["sec1", "sec2"],
                                            scope_inputs={"sec1": "s1", "sec2": "s2"},
                                            **common)
            two = agent_nodes.build_request(artifacts=["sec1.tex", "sec2.tex"],
                                            instruction="x", scopes=["sec1", "sec2"],
                                            scope_inputs={"sec1": "s1", "sec2": "s9"},
                                            **common)
            self.assertNotEqual(one["input_digest"], two["input_digest"])

    def test_declaration_errors_detect_scope_mismatch(self):
        req = make_request("dsp-x", ["sec1", "sec2"],
                           artifact_scopes={"sec1.tex": "sec1", "sec2.tex": "sec1"})
        codes = [c for c, _ in request_scopes.declaration_errors(req)]
        self.assertIn("BAD_SCOPE", codes)


# --------------------------------------------------------------------------
# 逐节认证：BATCH-02 / BATCH-04
# --------------------------------------------------------------------------
def _seed(d, req, artifacts_written, output_digests):
    agent_evidence.write_evidence(
        d, dispatch_id=req["dispatch_id"],
        batch_id=req.get("batch_id", "bat-t2-test"),
        worker="subagent:test-agent", artifacts_written=artifacts_written,
        output_digests=output_digests)


class BATCH02_PartialSuccess(unittest.TestCase):
    """BATCH-02：sec1 成功 sec2 失败 → 只重派 sec2；逐节 hash 可追踪。"""

    def test_verify_requests_from_disk_splits_scopes(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1_digest = proj.write_section("sec1")   # sec2 未写
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"],
                               before={"sec1.tex": "0" * 64,
                                       "sec2.tex": "0" * 64})
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": sec1_digest})
            ok, failures, entries = sbn._verify_requests_from_disk(
                proj.self_dir, [req])
            self.assertEqual(ok, ["sec1"])
            self.assertEqual(failures, {"sec2": "missing_artifacts"})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["scope"], "sec1")
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["scope_statuses"]["sec2"], "missing_artifacts")
            self.assertEqual(entry["artifact_digests"], {"sec1.tex": sec1_digest})
            self.assertEqual(entry["scope_artifacts"]["sec1"], ["sec1.tex"])
            self.assertEqual(entry["scope_artifacts"]["sec2"], ["sec2.tex"])

    def test_verified_entry_keeps_per_scope_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
            _seed(proj.self_dir, req, ["sec1.tex", "sec2.tex"],
                  {"sec1.tex": sec1, "sec2.tex": sec2})
            ok, failures, entries = sbn._verify_requests_from_disk(
                proj.self_dir, [req])
            self.assertEqual(sorted(ok), ["sec1", "sec2"])
            self.assertEqual(failures, {})
            entry = entries[req["dispatch_id"]]
            self.assertEqual(entry["artifact_digests"],
                             {"sec1.tex": sec1, "sec2.tex": sec2})
            self.assertEqual(sorted(entry["scope_statuses"]), ["sec1", "sec2"])
            self.assertTrue(all(v == "ok" for v in entry["scope_statuses"].values()))

    def test_legacy_single_scope_entry_shape_unchanged(self):
        """旧单 scope 派发单：ok_scopes/failures 形状与改造前一致（ev01 口径）。"""
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1",))
            digest = proj.write_section("sec1")
            req = make_request("dsp-t2-sec1-0001", ["sec1"], schema_version=1)
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": digest})
            ok, failures, entries = sbn._verify_requests_from_disk(
                proj.self_dir, [req])
            self.assertEqual(ok, ["sec1"])
            self.assertEqual(failures, {})
            self.assertEqual(entries[req["dispatch_id"]]["verified_via"], "agent")
            self.assertNotIn("unattested", entries[req["dispatch_id"]])

    def test_failed_scope_reregistered_independently(self):
        """失败节重派时只携带自身 scope，成功节不再进请求。"""
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": sec1})
            ok, failures, _e = sbn._verify_requests_from_disk(proj.self_dir, [req])
            self.assertEqual(ok, ["sec1"])
            self.assertEqual(list(failures), ["sec2"])
            state = {"self_dir": proj.self_dir, "run_id": "test-run"}
            retry = agent_nodes.build_request(
                state, stage="T2", role="writer",
                prompt_file="subagent_prompt.md", artifacts=["sec2.tex"],
                instruction="重写 sec2", command="cmd", scope="sec2")
            self.assertEqual(request_scopes.request_scopes(retry), ["sec2"])
            self.assertEqual(retry["artifacts"], ["sec2.tex"])


class BATCH04_ResumeKeepsCompleted(unittest.TestCase):
    """BATCH-04：中断恢复保持已成功节；旧单 scope 请求仍可认证。"""

    def test_resume_keeps_sec1_and_retries_sec2(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1", "% qid: sec1-q1\n原始已通过内容\n")
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"],
                               before={"sec1.tex": "0" * 64,
                                       "sec2.tex": "0" * 64})
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": sec1})

            state = {
                "self_dir": proj.self_dir,
                "project_root": proj.root,
                "run_id": "test-run",
                "expected_sections": ["sec1", "sec2"],
                "section_statuses": {"sec1": "running", "sec2": "running"},
                "staged_sections": {},
                "dispatch_mode": "parallel",
                "manual_interrupted_batch": "bat-t2-test",
                "batch_id": "bat-t2-test",
                "dispatch_results": {},
                "dispatch_metrics": {},
                "dispatch_history": [],
                "history": [],
                "dispatch_plan": {
                    "batch_id": "bat-t2-test",
                    "stage": "T2",
                    "requests_full": [req],
                    "max_concurrency": 4,
                    "min_parallelism": 1,
                    "sections": ["sec1", "sec2"],
                    "requests": [{"dispatch_id": req["dispatch_id"],
                                  "scope": "sec1"}],
                },
            }
            sbn._reset_pool()
            patch = sbn.node_t2_join_sections(state)
            self.addCleanup(sbn._reset_pool)

            self.assertEqual(patch["section_statuses"]["sec1"], "staged")
            self.assertEqual(patch["staged_sections"]["sec1"],
                             os.path.join(proj.self_dir, "sec1.tex"))
            self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
            self.assertEqual(patch["repair_target"], "sec2")
            # 已通过节内容未被改写
            self.assertEqual(_read(os.path.join(proj.self_dir, "sec1.tex")),
                             "% qid: sec1-q1\n原始已通过内容\n")

    def test_legacy_request_still_certifies(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1",))
            digest = proj.write_section("sec1")
            req = make_request("dsp-t2-sec1-0001", ["sec1"], schema_version=1)
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": digest})
            ok, failures, _ = sbn._verify_requests_from_disk(proj.self_dir, [req])
            self.assertEqual((ok, failures), (["sec1"], {}))


# --------------------------------------------------------------------------
# 逐节提交：BATCH-02（落盘事务）
# --------------------------------------------------------------------------
class BATCH02_PerSectionCommit(unittest.TestCase):
    """整批返回后逐节验证、逐节原子提交；失败节不回滚成功节。"""

    def test_promote_commits_only_passing_sections(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            staging = os.path.join(proj.self_dir, sbn.WRITER_STAGING_DIR_REL)
            _write(os.path.join(staging, "sec1.tex"), "% qid: sec1-q1\n新 sec1\n")
            # sec2 压根没写 → 只有 sec1 能提交
            committed = sbn.promote_writer_sections(proj.self_dir, ["sec1"], ["sec2"])
            self.assertEqual(committed, ["sec1"])
            self.assertEqual(_read(os.path.join(proj.self_dir, "sec1.tex")),
                             "% qid: sec1-q1\n新 sec1\n")
            self.assertFalse(os.path.exists(os.path.join(proj.self_dir, "sec2.tex")))
            self.assertTrue(os.path.exists(
                os.path.join(staging, "sec1.tex")) is False)

    def test_failed_section_leaves_successful_one_intact(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            proj.write_section("sec1", "% qid: sec1-q1\n旧 sec1\n")
            staging = os.path.join(proj.self_dir, sbn.WRITER_STAGING_DIR_REL)
            _write(os.path.join(staging, "sec2.tex"), "% qid: sec2-q1\n新 sec2\n")
            committed = sbn.promote_writer_sections(proj.self_dir, ["sec2"], ["sec1"])
            self.assertEqual(committed, ["sec2"])
            # sec1 未被删除或改写（失败节不得回滚已通过节）
            self.assertEqual(_read(os.path.join(proj.self_dir, "sec1.tex")),
                             "% qid: sec1-q1\n旧 sec1\n")

    def test_parse_response_rejects_ambiguous_boundaries(self):
        text = "```tex\n% FILE: sec1.tex\nA\n```\n乱写一段没有边界的正文\n"
        with self.assertRaises(sbn.WriterParseError):
            sbn.parse_writer_response(text, ["sec1", "sec2"])

    def test_parse_response_reads_per_section_blocks(self):
        text = (
            "```tex\n% FILE: sec1.tex\n% qid: sec1-q1\n正文1\n```\n"
            "```tex\n% FILE: sec2.tex\n% qid: sec2-q1\n正文2\n```\n"
        )
        parsed = sbn.parse_writer_response(text, ["sec1", "sec2"])
        self.assertEqual(sorted(parsed), ["sec1", "sec2"])
        self.assertIn("正文1", parsed["sec1"])

    def test_parse_response_missing_section_reported(self):
        text = "```tex\n% FILE: sec1.tex\n% qid: sec1-q1\n正文1\n```\n"
        parsed = sbn.parse_writer_response(text, ["sec1", "sec2"])
        self.assertEqual(list(parsed), ["sec1"])


# --------------------------------------------------------------------------
# BATCH-03：superseded 必须 all，不能 any
# --------------------------------------------------------------------------
class BATCH03_SupersededAll(unittest.TestCase):
    """BATCH-03：只认证一件产物不能让整批 superseded / 通过 provenance。"""

    def test_old_batch_two_artifacts_only_one_attested_not_superseded(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            # 旧的整批派发单（无证据），产物两件
            old = {
                "dispatch_id": "dsp-t2-old-0001",
                "stage": "T2",
                "scope": "sec1",
                "role": "writer",
                "artifacts": ["sec1.tex", "sec2.tex"],
                "status": "pending",
                "attempt": 0,
            }
            proj.write_request(old)
            # 新派发单只认证 sec1
            new_bytes = "% qid: sec1-q1\n rewritten\n"
            _write(os.path.join(proj.self_dir, "sec1.tex"), new_bytes)
            new_req = make_request("dsp-t2-new-0002", ["sec1"])
            _seed(proj.self_dir, new_req, ["sec1.tex"],
                  {"sec1.tex": _sha256(os.path.join(proj.self_dir, "sec1.tex"))})

            result = check_agent_audit.audit_agent_evidence(proj.self_dir)
            codes = _codes(result["errors"])
            self.assertIn("EVIDENCE_MISSING", codes, result["errors"])
            self.assertEqual(result["checked"]["superseded"], 0)
            superseded_warnings = [w for w in result["warnings"]
                                   if w.get("code") == "EVIDENCE_MISSING"]
            self.assertEqual(superseded_warnings, [])
            self.assertTrue(sec1 and sec2)

    def test_all_artifacts_attested_superseded_warning(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            old = {
                "dispatch_id": "dsp-t2-old-0001",
                "stage": "T2",
                "scope": "sec1",
                "role": "writer",
                "artifacts": ["sec1.tex", "sec2.tex"],
                "status": "pending",
                "attempt": 0,
            }
            proj.write_request(old)
            new_req = make_request("dsp-t2-new-0002", ["sec1", "sec2"])
            _seed(proj.self_dir, new_req, ["sec1.tex", "sec2.tex"],
                  {"sec1.tex": sec1, "sec2.tex": sec2})
            result = check_agent_audit.audit_agent_evidence(proj.self_dir)
            self.assertEqual(_codes(result["errors"]), [], result["errors"])
            self.assertEqual(result["checked"]["superseded"], 1)

    def test_multi_scope_write_chain_certifies_every_scope(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            proj.write_semantic("sec1", sec1)
            proj.write_semantic("sec2", sec2)
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
            proj.write_request(req)
            _seed(proj.self_dir, req, ["sec1.tex", "sec2.tex"],
                  {"sec1.tex": sec1, "sec2.tex": sec2})
            result = check_provenance.check_provenance_project(proj.self_dir)
            self.assertEqual(_codes(result["errors"]), [], result["errors"])
            self.assertEqual(sorted(result["checked"]["certified"]),
                             ["sec1", "sec2"])

    def test_partial_certification_does_not_pass_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            proj.write_semantic("sec1", sec1)
            proj.write_semantic("sec2", sec2)
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
            proj.write_request(req)
            # 只认证一件产物
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": sec1})
            result = check_provenance.check_provenance_project(proj.self_dir)
            self.assertIn("PROV_WRITER_DIGEST_MISSING", _codes(result["errors"]))
            self.assertEqual(result["checked"]["certified"], ["sec1"])


# --------------------------------------------------------------------------
# BATCH-04：provenance 认多 scope 写域白名单
# --------------------------------------------------------------------------
class BATCH04_ProvenanceMultiScope(unittest.TestCase):

    def test_write_domain_checked_for_every_scope(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1", "sec2"))
            sec1 = proj.write_section("sec1")
            sec2 = proj.write_section("sec2")
            proj.write_semantic("sec1", sec1)
            proj.write_semantic("sec2", sec2)
            req = make_request("dsp-t2-sec1sec2-0001", ["sec1", "sec2"])
            proj.write_request(req)
            # 台账里多写了一件越界产物；写域检查必须覆盖到 sec2 的派发单
            agent_evidence.write_evidence(
                proj.self_dir, dispatch_id=req["dispatch_id"], batch_id="bat-t2-test",
                worker="subagent:test-agent",
                artifacts_written=["sec1.tex", "sec2.tex", "main.tex"],
                output_digests={"sec1.tex": sec1, "sec2.tex": sec2,
                                "main.tex": "d" * 64})
            result = check_provenance.check_provenance_project(proj.self_dir)
            self.assertIn("PROV_PATH_OUTSIDE_ALLOWLIST", _codes(result["errors"]))

    def test_legacy_single_scope_still_certifies(self):
        with tempfile.TemporaryDirectory() as d:
            proj = _Project(d, ("sec1",))
            sec1 = proj.write_section("sec1")
            proj.write_semantic("sec1", sec1)
            req = make_request("dsp-t2-sec1-0001", ["sec1"], schema_version=1)
            proj.write_request(req)
            _seed(proj.self_dir, req, ["sec1.tex"], {"sec1.tex": sec1})
            result = check_provenance.check_provenance_project(proj.self_dir)
            self.assertEqual(_codes(result["errors"]), [], result["errors"])
            self.assertEqual(result["checked"]["certified"], ["sec1"])


if __name__ == "__main__":
    unittest.main()
