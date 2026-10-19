#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_t4_review_batch.py — P2 T4 验收 fan-out 派发池回归测试（T01–T10）。

覆盖 workflow/nodes/t4_review_batch_nodes.py 的三个节点：
    T01  scope/qid 抽取与期望并集（含缺文件容忍、run.json 回退）
    T02  规划节点：一节一派发单、bat-t4- 前缀、artifacts/input_digest/context、
         磁盘 dispatch-plan.json 过 check_dispatch、无 expected_sections → HALTED
    T03  规划节点：门禁非 PASS → HALTED/HALT 且错误串带 codes
    T04  派发节点：parallel / manual 双模、stale plan → HALT
    T05  收尾节点（并行）：全 PASS → 逐 scope PASS + chapter.json PASS
    T06  收尾节点（并行）：单 scope REVISE → 汇总 REVISE 但仍交回 verify
    T07  收尾节点（并行）：parse_error / schema_error / 过期 digest → 单 scope 失败
    T08  收尾节点（manual）：中断载荷断言 + 二次调用不重复 interrupt + no_change
    T09  resume 安全：无活池 + manual_interrupted_batch == batch_id → 磁盘重推导
    T10  聚合 schema：chapter.json 过 evidence.validate_acceptance_json 零问题
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import check_dispatch  # noqa: E402
import evidence  # noqa: E402

from workflow.adapters.worker_pool import DispatchResult  # noqa: E402
from workflow.nodes import agent_evidence  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes import t4_review_batch_nodes as t4nodes  # noqa: E402

# 与被测节点逐字一致（含 re.M 多行锚定）
RE_QID = re.compile(r"^\s*%\s*qid:\s*([^\s%]+)", re.M)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

SEC_TEX = {
    "sec0": "\\section*{开篇}\n% qid: sec0-entry\n正文。\n",
    "sec1": "\\section*{第一节}\n% qid: sec1-entry\n% qid: sec1-second\n正文。\n",
    "sec2": "\\section*{第二节}\n% qid: sec2-entry\n正文。\n",
    "sec3": "\\section*{第三节}\n% qid: sec3-entry\n正文。\n",
}


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def register_capable_worker(max_concurrency=8):
    """注册「异步+可取消+并发 N」的 LLM worker（任务8 治具），返回清理回调。"""
    from workflow.adapters import worker_registry

    def executor(request):  # pragma: no cover - 仅能力声明，不真实执行
        raise AssertionError("治具 executor 不应被真实调用")

    worker_registry.set(executor, name="test-capable-llm", capabilities={
        "sync": False, "async": True, "cancellable": True,
        "max_concurrency": max_concurrency})
    return worker_registry.clear


def make_self(d, sections, fmt="both", run_chapter_qids=None, pre_artifacts=None):
    """合成最小 self/ 工程（切片含 qid 注释 + main.tex + 受审产物）。"""
    self_dir = os.path.join(d, "self")
    os.makedirs(os.path.join(self_dir, ".pd", "acceptance"), exist_ok=True)
    qids = []
    for sec in sections:
        text = SEC_TEX.get(sec, f"\\section*{{{sec}}}\n")
        with open(os.path.join(self_dir, f"{sec}.tex"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(text)
        qids.extend(RE_QID.findall(text))
    with open(os.path.join(self_dir, "main.tex"), "w",
              encoding="utf-8", newline="") as f:
        f.write("".join(f"\\input{{{s}.tex}}\n" for s in sections))
    # 显式抬高并发上限，确保一个批次装下全部切片（默认档位 safe 只有 1）
    with open(os.path.join(self_dir, "config.yaml"), "w",
              encoding="utf-8") as f:
        f.write("agent_dispatch: safe\nagent_max_concurrency: 8\n"
                "agent_batch_window: 2\n")
    if fmt in ("pdf", "both"):
        with open(os.path.join(self_dir, "student.pdf"), "wb") as f:
            f.write(b"%PDF-fake-student")
    if fmt in ("html", "both"):
        with open(os.path.join(self_dir, "student.html"), "w",
                  encoding="utf-8") as f:
            f.write("<html><body>fake-student</body></html>")
    with open(os.path.join(self_dir, ".pd", "run.json"), "w",
              encoding="utf-8") as f:
        json.dump({"required_acceptance_scopes": {
            "chapter": run_chapter_qids if run_chapter_qids is not None else qids}},
            f, ensure_ascii=False)
    for sec, content in (pre_artifacts or {}).items():
        with open(os.path.join(self_dir, ".pd", "acceptance", f"{sec}.json"),
                  "w", encoding="utf-8") as f:
            f.write(content)
        # F3：source-vs-output.md 同为必交产物，否则失败码会变成
        # missing_artifacts 而非本测试要断言的 ok_unattested。
        with open(os.path.join(self_dir, ".pd", "acceptance",
                               f"{sec}-source-vs-output.md"),
                  "w", encoding="utf-8") as f:
            f.write(f"# {sec} source-vs-output (stale)\n")
    return self_dir


def base_state(self_dir, sections, fmt="both", **extra):
    state = {
        "self_dir": self_dir,
        "project_root": self_dir,
        "run_id": "run-t4",
        "user_spec": {},
        "expected_sections": list(sections),
        "section_statuses": {s: "staged" for s in sections},
        "output_format": fmt,
        "history": [],
    }
    state.update(extra)
    return state


def write_scope_record(self_dir, sec, conclusion="PASS", qids=None, digest=None,
                       pdf_sha=None, html_sha=None, stage1=None, stage2=None,
                       issues=None, drop_key=None,
                       scope_digest=None, chapter_digest=None,
                       rule_version=None):
    """按 prompts/acceptance_prompt.md 的 schema 写一份切片验收记录。

    scope_digest / chapter_digest / rule_version：任务6 复用绑定字段
    （生产里由主控 _stamp_review_provenance 盖章，测试直接写入以构造
    「上一轮已验收」场景）。
    """
    if digest is None:
        digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    if pdf_sha is None:
        pdf_sha = evidence.sha256_file(os.path.join(self_dir, "student.pdf"))
    if html_sha is None:
        html_sha = evidence.sha256_file(os.path.join(self_dir, "student.html"))
    if qids is None:
        with open(os.path.join(self_dir, f"{sec}.tex"), "r",
                  encoding="utf-8") as f:
            qids = RE_QID.findall(f.read())
    rec = {
        "schema_version": evidence.SCHEMA_VERSION,
        "scope": sec,
        "reviewed_qids": qids,
        "content_digest": digest,
        "student_pdf_sha256": pdf_sha,
        "conclusion": conclusion,
        "reviewer_role": f"{sec} 切片独立教学验收专员",
        "stage1_record": stage1 if stage1 is not None
        else f"{sec} 学生视角通读记录",
        "stage2_record": stage2 if stage2 is not None
        else f"{sec} 教学法与前置自足性复核记录",
        "issues": issues if issues is not None else [],
    }
    if drop_key:
        rec.pop(drop_key, None)
    # 任务6：复用绑定字段（None 表示不写，由调用方按场景决定）
    if scope_digest is not None:
        rec["scope_content_digest"] = scope_digest
    if chapter_digest is not None:
        rec["chapter_digest"] = chapter_digest
    if rule_version is not None:
        rec["review_rule_version"] = rule_version
    path = os.path.join(self_dir, ".pd", "acceptance", f"{sec}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    # F3：source-vs-output.md 与 <sec>.json 同为 T4 验收必交产物
    sv_path = os.path.join(self_dir, ".pd", "acceptance",
                           f"{sec}-source-vs-output.md")
    with open(sv_path, "w", encoding="utf-8") as f:
        f.write(f"# {sec} source-vs-output\n\n"
                f"| source_id | 输出位置 | 动作 | 改变命题 | 新增对象 |\n"
                f"|---|---|---|---|---|\n"
                f"| SRC-001 | {sec} | PRESERVE | 否 | 否 |\n")
    return rec


def seed_t4_evidence(self_dir, requests, worker="subagent:t4-reviewer"):
    """为 T4 验收派发单落证据台账（模拟 qchain_dispatch 派发独立验收子 Agent）。

    q_audit.md 证据链改造后，T4 磁盘重推导放行验收记录的唯一依据；摘要按盘面
    现值计算，故必须在 write_scope_record 之后调用。
    """
    for req in requests:
        digests = {}
        for rel in (req.get("artifacts") or []):
            if isinstance(rel, str) and os.path.isfile(os.path.join(self_dir, rel)):
                digests[rel] = evidence.sha256_file(os.path.join(self_dir, rel))
        agent_evidence.write_evidence(
            self_dir,
            dispatch_id=str(req.get("dispatch_id") or ""),
            batch_id=req.get("batch_id"),
            worker=worker,
            artifacts_written=sorted(digests),
            output_digests=digests,
        )


class FakeScriptRunner:
    """ScriptRunner 替身：门禁结论由类属性注入（PASS / FAIL 两种）。"""

    status = "PASS"
    raw_report = {"status": "pass", "errors": [], "warnings": []}
    exit_code = 0

    def __init__(self, project_root=None):
        self.project_root = project_root

    def run_script(self, script_name, args, timeout=300, cwd=None):
        return (0, "", "")

    def run_gate_script(self, gate_name, script_name, args,
                        report_rel_path=None, timeout=300, run_id=None):
        return {
            "gate_name": gate_name,
            "status": type(self).status,
            "passed": type(self).status == "PASS",
            "issues": [],
            "raw_report": type(self).raw_report,
            "exit_code": type(self).exit_code,
            "report_path": report_rel_path,
            "report_generated_at": "2024-01-01T00:00:00+00:00",
            "report_is_fresh": True,
            "run_id": run_id,
        }


def fake_runner(status="PASS", raw_report=None):
    cls = type("FakeScriptRunnerTuned", (FakeScriptRunner,), {
        "status": status,
        "raw_report": raw_report if raw_report is not None else {
            "status": "pass" if status == "PASS" else "fail",
            "errors": [], "warnings": []},
        "exit_code": 0 if status == "PASS" else 1,
    })
    return cls


class FakePool:
    """WorkerPool 替身：结果/ deferred 数由构造参数注入。

    ``max_in_flight`` 缺省取已提交数（全部请求并发执行的高水位）；
    显式传入可用于构造「实际并发度低于计划值」的伪装场景。
    """

    def __init__(self, results=None, deferred=0, max_in_flight=None):
        self.results = dict(results or {})
        self.deferred = int(deferred)
        self.submitted = []
        self.joined = False
        self.join_calls = []
        self._max_in_flight = max_in_flight

    def submit(self, request):
        self.submitted.append(request.get("dispatch_id"))
        return request.get("dispatch_id")

    def join(self, timeout=None):
        # 幂等：与真实 WorkerPool.join 语义一致——已完成批次立即返回，
        # 不重复 join（audit 任务4：每 batch 只允许一次 join）
        self.joined = True
        if not self.join_calls:
            self.join_calls.append(timeout)
        return dict(self.results)

    def stats(self):
        n = len(self.submitted)
        return {
            "submitted": n, "completed": n, "ok": 0, "failed": 0, "timeout": 0,
            "no_change": 0, "deferred": self.deferred, "retried": 0,
            "max_in_flight": n if self._max_in_flight is None else self._max_in_flight,
            "orphans": 0,
        }

    def reset(self):
        return None


def ok_result(request):
    return DispatchResult(
        dispatch_id=request.get("dispatch_id"),
        scope=request.get("scope"),
        status="ok",
        output="验收记录已写入",
        error=None,
        attempts=1,
        duration_ms=1,
    )


class T04Base(unittest.TestCase):
    """公共夹具：临时工程 + 池单例隔离 + 规划助手。"""

    def setUp(self):
        sbn._reset_pool()
        # 任务8：注册「异步+可取消+并发 8」的 worker，计划按真实并行度
        # 承诺 min_parallelism=2（不注册时 registry 保守串行只承诺 1，
        # T05b 的并发度硬门禁将无法触发）
        self._unregister = register_capable_worker(8)
        self.addCleanup(self._unregister)
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        sbn._reset_pool()
        self._tmp.cleanup()

    def plan(self, state, status="PASS", raw_report=None):
        with mock.patch.object(t4nodes, "ScriptRunner",
                               fake_runner(status, raw_report)):
            patch = t4nodes.node_t4_plan_review(state)
        state.update(patch)
        return patch

    def requests_of(self, state):
        return list((state.get("dispatch_plan") or {}).get("requests_full") or [])

    def run_dispatch(self, state, pool):
        """先走派发节点（同一 FakePool 累积提交），再交给 join。"""
        with mock.patch.object(t4nodes, "_get_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_dispatch_review(state)
        state.update(patch)
        return patch


class TestT01ScopeAndQidExtraction(T04Base):

    def test_t01_scope_and_qid_extraction(self):
        self_dir = make_self(self.d, ("sec0", "sec1", "sec2"))
        self.assertEqual(t4nodes._extract_section_qids(self_dir, "sec0"),
                         ["sec0-entry"])
        self.assertEqual(t4nodes._extract_section_qids(self_dir, "sec1"),
                         ["sec1-entry", "sec1-second"])
        self.assertEqual(t4nodes._extract_section_qids(self_dir, "sec2"),
                         ["sec2-entry"])
        # 缺失切片文件必须被容忍（不抛异常，返回空表）
        self.assertEqual(t4nodes._extract_section_qids(self_dir, "sec9"), [])

        state = base_state(self_dir, ("sec0", "sec1", "sec2"))
        self.plan(state)
        plan = state["dispatch_plan"]
        self.assertEqual(plan["expected_qids"], {
            "sec0": ["sec0-entry"],
            "sec1": ["sec1-entry", "sec1-second"],
            "sec2": ["sec2-entry"],
        })
        self.assertEqual(t4nodes._plan_expected_total_qids(plan),
                         ["sec0-entry", "sec1-entry", "sec1-second", "sec2-entry"])

    def test_t01_total_qids_falls_back_to_run_json(self):
        # 切片无 qid 注释 → 回退 run.json required_acceptance_scopes["chapter"]
        self_dir = make_self(self.d, ("sec1", "sec2"),
                             run_chapter_qids=["fallback-a", "fallback-b"])
        for sec in ("sec1", "sec2"):
            with open(os.path.join(self_dir, f"{sec}.tex"), "w",
                      encoding="utf-8") as f:
                f.write("\\section*{无 qid}\n正文。\n")
        state = base_state(self_dir, ("sec1", "sec2"))
        self.assertEqual(
            t4nodes._expected_total_qids(
                state, self_dir,
                {s: t4nodes._extract_section_qids(self_dir, s)
                 for s in ("sec1", "sec2")}),
            ["fallback-a", "fallback-b"])


class TestT02PlanBuilding(T04Base):

    def test_t02_plan_builds_one_request_per_section(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        patch = self.plan(state)

        self.assertEqual(patch["stage"], "T4_REVIEW_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertTrue(patch["batch_id"].startswith("bat-t4-"))
        self.assertEqual(patch["retry_count"], 0)
        self.assertEqual(patch["dispatch_metrics"],
                         {"submitted": 2, "completed": 0, "ok": 0, "failed": 0,
                          "retried": 0, "max_in_flight": 0, "deferred": 0,
                          "reused": 0})
        # section_statuses 原样带回（不写 running/staged 状态机）
        self.assertEqual(patch["section_statuses"],
                         {"sec1": "staged", "sec2": "staged"})

        digest = evidence.content_digest(evidence.collect_inputs(self_dir))
        pdf_sha = sha(os.path.join(self_dir, "student.pdf"))
        html_sha = sha(os.path.join(self_dir, "student.html"))
        requests = self.requests_of(state)
        self.assertEqual([r["scope"] for r in requests], ["sec1", "sec2"])
        for req in requests:
            sec = req["scope"]
            self.assertEqual(req["stage"], f"T4_REVIEW_{sec.upper()}")
            self.assertEqual(req["prompt_file"], "acceptance_prompt.md")
            self.assertEqual(req["role"], f"{sec} 切片独立教学验收专员")
            self.assertEqual(req["artifacts"],
                             [os.path.join(".pd", "acceptance", f"{sec}.json"),
                              os.path.join(".pd", "acceptance",
                                           f"{sec}-source-vs-output.md")])
            self.assertRegex(req["input_digest"], HEX64)
            self.assertEqual(req["batch_id"], patch["batch_id"])
            self.assertEqual(req["context"]["run_id"], "run-t4")
            self.assertEqual(req["context"]["scope"], sec)
            self.assertEqual(req["context"]["content_digest"], digest)
            self.assertEqual(req["context"]["student_pdf_sha256"], pdf_sha)
            self.assertEqual(req["context"]["student_html_sha256"], html_sha)
            self.assertEqual(req["context"]["output_format"], "both")
            self.assertEqual(req["context"]["expected_qids"],
                             t4nodes._extract_section_qids(self_dir, sec))
            self.assertTrue(req["request_ref"].endswith(
                f"{req['dispatch_id']}.json"))
            self.assertEqual(req["success_stage"], "T4_ACCEPTANCE_PENDING")
            # 派发单已落盘（供人/Agent 阅读）
            req_path = os.path.join(self_dir, req["request_ref"])
            self.assertTrue(os.path.isfile(req_path))

        # 磁盘派发计划必须原样通过 check_dispatch（stage 自由字段）
        with open(os.path.join(self_dir, ".pd", "dispatch-plan.json"),
                  "r", encoding="utf-8-sig") as f:
            disk_plan = json.load(f)
        self.assertEqual(disk_plan["stage"], "T4")
        self.assertEqual(disk_plan["required_scope_count"], 2)
        report = check_dispatch.check_dispatch_project(self_dir)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["checked"],
                         {"sections": 2, "requests": 2})

    def test_t02_missing_expected_sections_halts(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ())
        patch = self.plan(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("expected_sections", patch["error_message"])

    def test_t02_role_for_opening_section(self):
        self_dir = make_self(self.d, ("sec0", "sec1"))
        state = base_state(self_dir, ("sec0", "sec1"))
        self.plan(state)
        roles = {r["scope"]: r["role"] for r in self.requests_of(state)}
        self.assertEqual(roles["sec0"], "开篇验收专员")
        self.assertEqual(roles["sec1"], "sec1 切片独立教学验收专员")


class TestT03GateFailure(T04Base):

    def test_t03_gate_failure_halts(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        patch = self.plan(state, status="FAIL", raw_report={
            "status": "fail",
            "errors": [{"code": "UNDER_DISPATCH", "detail": "派发单不足"}],
            "warnings": [],
        })
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("派发门禁未通过", patch["error_message"])
        self.assertIn("UNDER_DISPATCH", patch["error_message"])


class TestT04DispatchNode(T04Base):

    def test_t04_parallel_and_manual_modes(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)

        pool = FakePool(deferred=0)
        with mock.patch.object(t4nodes, "_get_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_dispatch_review(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "parallel")
        self.assertEqual(len(pool.submitted), 2)
        # 任务4：dispatch 节点绝不调 join（join 是收尾节点专属职责）
        self.assertFalse(pool.joined)
        self.assertEqual(pool.join_calls, [])
        self.assertEqual(patch["dispatch_metrics"]["submitted"], 2)

        pool2 = FakePool(deferred=2)
        with mock.patch.object(t4nodes, "_get_pool", lambda *a, **k: pool2):
            patch2 = t4nodes.node_t4_dispatch_review(state)
        self.assertEqual(patch2["dispatch_mode"], "manual")
        self.assertEqual(patch2["stage"], "T4_REVIEW_RUNNING")

    def test_t04_stale_plan_halts(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        stale = dict(state)
        stale["batch_id"] = "bat-t4-not-the-plan"
        patch = t4nodes.node_t4_dispatch_review(stale)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("stale plan", patch["error_message"])


class TestT05JoinParallel(T04Base):

    def _prepared(self, sections=("sec1", "sec2"), fmt="both"):
        self_dir = make_self(self.d, sections, fmt=fmt)
        state = base_state(self_dir, sections, fmt=fmt)
        self.plan(state)
        return state

    def test_t05_join_parallel_all_pass(self):
        state = self._prepared()
        requests = self.requests_of(state)
        for req in requests:
            write_scope_record(state["self_dir"], req["scope"])
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests},
                        deferred=0)
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_join_review(state)

        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["verdict"], "PASS")
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["scope"], req["scope"])
            self.assertEqual(entry["batch_id"], state["batch_id"])
        self.assertEqual(patch["dispatch_metrics"]["submitted"], 2)
        self.assertEqual(patch["section_statuses"],
                         {"sec1": "staged", "sec2": "staged"})
        # 任务4：join 节点是本 batch 唯一 join 者，且只 join 一次
        self.assertTrue(pool.joined)
        self.assertEqual(len(pool.join_calls), 1)
        # 不越权发出返修/重派路由
        self.assertNotIn("repair_reason", patch)

        # per-scope 记录保留在盘上（deliver.py 会逐份校验）
        for sec in ("sec1", "sec2"):
            self.assertTrue(os.path.isfile(os.path.join(
                state["self_dir"], ".pd", "acceptance", f"{sec}.json")))

        chapter_path = os.path.join(state["self_dir"], ".pd", "acceptance",
                                    "chapter.json")
        self.assertTrue(os.path.isfile(chapter_path))
        with open(chapter_path, "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "PASS")
        self.assertEqual(chapter["scope"], "chapter")
        self.assertEqual(chapter["reviewed_qids"],
                         ["sec1-entry", "sec1-second", "sec2-entry"])
        baseline = state["dispatch_plan"]["evidence_baseline"]
        self.assertEqual(chapter["content_digest"], baseline["content_digest"])
        self.assertEqual(chapter["student_pdf_sha256"],
                         baseline["student_pdf_sha"])
        self.assertEqual(chapter["reviewer_role"],
                         "T4 并行验收 fan-in 汇总（acceptance-reviewer × N）")
        self.assertTrue(any("汇总结论 PASS" in h for h in patch["history"]))

    def test_t05b_under_parallelism_halts(self):
        """audit §6 硬门禁：池全 ok 但 max_in_flight < min_parallelism → 禁止伪装完成。"""
        state = self._prepared()
        requests = self.requests_of(state)
        for req in requests:
            write_scope_record(state["self_dir"], req["scope"])
        # 2 个 scope → min_parallelism=2；伪造实际并发 1
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests},
                        deferred=0, max_in_flight=1)
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_join_review(state)

        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("禁止伪装完成", patch["error_message"])
        # 聚合不得发生：chapter.json 不存在
        self.assertFalse(os.path.isfile(os.path.join(
            state["self_dir"], ".pd", "acceptance", "chapter.json")))


class TestT06JoinParallelRevise(T04Base):

    def test_t06_single_scope_revise_aggregates_revise(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        write_scope_record(self_dir, "sec1")
        issue = {
            "id": "ISS-01", "qid": "sec2-entry", "part": "正文",
            "code": "PEDAGOGY_GAP", "evidence": "第 2 节缺判定依据",
            "status": "open", "resolution": "", "verified_digest": "",
        }
        write_scope_record(self_dir, "sec2", conclusion="REVISE", issues=[issue])
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests})
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_join_review(state)

        # 汇总 REVISE，但路由仍交回 verify 节点（绝不越权 REPAIR/REDISPATCH）
        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertNotIn("repair_reason", patch)
        self.assertNotIn("repair_target", patch)
        verdicts = {patch["dispatch_results"][r["dispatch_id"]]["scope"]:
                    patch["dispatch_results"][r["dispatch_id"]]["verdict"]
                    for r in requests}
        self.assertEqual(verdicts, {"sec1": "PASS", "sec2": "REVISE"})

        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "REVISE")
        self.assertEqual([i["id"] for i in chapter["issues"]], ["ISS-01"])
        self.assertTrue(any("汇总结论 REVISE" in h for h in patch["history"]))


class TestT07JoinParallelBadRecords(T04Base):

    def test_t07_bad_records_fail_scope_and_block_pass(self):
        sections = ("sec1", "sec2", "sec3")
        self_dir = make_self(self.d, sections)
        state = base_state(self_dir, sections)
        self.plan(state)
        requests = self.requests_of(state)
        self.assertEqual(len(requests), 3)

        # sec1：不可解析
        with open(os.path.join(self_dir, ".pd", "acceptance", "sec1.json"),
                  "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        # sec2：缺 stage2_record（schema 不完整）
        write_scope_record(self_dir, "sec2", drop_key="stage2_record")
        # sec3：digest 过期（证据绑定失效）
        write_scope_record(self_dir, "sec3", digest="0" * 64)
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests})
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            patch = t4nodes.node_t4_join_review(state)

        by_scope = {patch["dispatch_results"][r["dispatch_id"]]["scope"]:
                    patch["dispatch_results"][r["dispatch_id"]]
                    for r in requests}
        self.assertEqual(by_scope["sec1"]["verdict"], "FAILED")
        self.assertTrue(by_scope["sec1"]["error"].startswith("parse_error:"))
        self.assertEqual(by_scope["sec2"]["verdict"], "FAILED")
        self.assertTrue(by_scope["sec2"]["error"].startswith("schema_error:"))
        self.assertEqual(by_scope["sec3"]["verdict"], "FAILED")
        self.assertTrue(by_scope["sec3"]["error"].startswith("schema_error:"))

        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertNotEqual(chapter["conclusion"], "PASS")
        self.assertTrue(any("汇总结论" in h for h in patch["history"]))


class TestT08JoinManual(T04Base):

    def test_t08_manual_interrupt_payload_and_disk_derivation(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)

        captured = []

        def fake_interrupt(payload):
            captured.append(payload)
            return {"files": {}}

        # (a) 首次：无活池 + 未中断过 → 一次中断携带整批
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt", fake_interrupt):
            patch = t4nodes.node_t4_join_review(state)
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertEqual(payload["type"], "AGENT_DISPATCH")
        self.assertEqual(payload["batch_id"], state["batch_id"])
        self.assertEqual(payload["dispatch_id"], state["batch_id"])
        self.assertEqual(payload["stage"], "T4")
        self.assertEqual(payload["role"], "T4 独立教学验收批派发（fan-out）")
        self.assertEqual(payload["prompt_file"], "prompts/acceptance_prompt.md")
        self.assertEqual(len(payload["requests"]), 2)
        self.assertEqual(payload["max_concurrency"],
                         state["dispatch_plan"]["max_concurrency"])
        self.assertEqual(payload["min_parallelism"],
                         state["dispatch_plan"]["min_parallelism"])
        self.assertEqual(sorted(payload["context"]["sections"]), ["sec1", "sec2"])
        self.assertEqual(sorted(payload["context"]["expected_qids"]),
                         ["sec1", "sec2"])
        self.assertEqual(len(payload["request_refs"]), 2)
        for sub, req in zip(payload["requests"], requests):
            self.assertEqual(sub["dispatch_id"], req["dispatch_id"])
            self.assertEqual(sub["scope"], req["scope"])
            self.assertEqual(sub["artifacts"], req["artifacts"])
            self.assertEqual(sub["prompt_file"],
                             "prompts/acceptance_prompt.md")
            self.assertEqual(sub["request_ref"], req["request_ref"])
            self.assertTrue(sub["instruction"])
            self.assertTrue(sub["command"])
        self.assertEqual(patch["manual_interrupted_batch"], state["batch_id"])
        self.assertEqual(patch["dispatch_mode"], "manual")
        self.assertEqual(patch["stage"], "T4_REVIEW_RUNNING")

        # (b) 二次：已中断过 + 记录已落盘 → 磁盘重推导，绝不二次中断
        state.update(patch)
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        seed_t4_evidence(self_dir, requests)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得二次 interrupt")):
            patch2 = t4nodes.node_t4_join_review(state)
        self.assertEqual(len(captured), 1)
        self.assertEqual(patch2["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch2["routing_action"], "PROCEED")
        for req in requests:
            self.assertEqual(
                patch2["dispatch_results"][req["dispatch_id"]]["verdict"], "PASS")
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            self.assertEqual(json.load(f)["conclusion"], "PASS")

    def test_t08_no_change_failures(self):
        # 记录在派发前已存在且未变化 → 无证据台账 → ok_unattested（无活池、已中断过）。
        # 注：T4 重推导刻意置空 before（见 t4_review_batch_nodes.plan 注释），
        # 磁盘陈旧产物不判 no_change（那会与"本次派发无变化"混淆），一律走
        # 证据对账；无证据即 ok_unattested → FAILED，验收结论不得 PASS。
        pre = {"sec1": '{"stale": true}', "sec2": '{"stale": true}'}
        self_dir = make_self(self.d, ("sec1", "sec2"), pre_artifacts=pre)
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        state["manual_interrupted_batch"] = state["batch_id"]
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得二次 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["verdict"], "FAILED")
            self.assertTrue(entry["error"].startswith("ok_unattested:"))
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            self.assertNotEqual(json.load(f)["conclusion"], "PASS")


class TestT09ResumeSafety(T04Base):

    def test_t09_resume_from_checkpoint_rederives_from_disk(self):
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        # 模拟 checkpoint 恢复：manual_interrupted_batch 已持久化、内存池已丢失
        state["manual_interrupted_batch"] = state["batch_id"]
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        seed_t4_evidence(self_dir, requests)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["verdict"], "PASS")
            self.assertEqual(entry.get("verified_from"), "disk")
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            self.assertEqual(json.load(f)["conclusion"], "PASS")


    def test_t09b_parallel_crash_resume_rederives_without_interrupt(self):
        """并行验收批次进程崩溃→重启→resume：活池已失但不得退化为人工 interrupt。

        checkpoint 里 dispatch_mode=="parallel"；sec1 记录已落盘、sec2 未写
        → sec1 PASS、sec2 FAILED(missing_artifacts)，汇总结论不得 PASS，
        由 verify 节点（唯一决策点）路由重派。
        """
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        state["dispatch_mode"] = "parallel"   # 派发节点返回过、已固化
        write_scope_record(self_dir, "sec1")
        seed_t4_evidence(self_dir, [requests[0]])
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(
                    t4nodes, "interrupt",
                    lambda p: self.fail("并行批次崩溃恢复不得 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch["routing_action"], "PROCEED")
        by_scope = {patch["dispatch_results"][r["dispatch_id"]]["scope"]:
                    patch["dispatch_results"][r["dispatch_id"]]
                    for r in requests}
        self.assertEqual(by_scope["sec1"]["verdict"], "PASS")
        self.assertEqual(by_scope["sec1"].get("verified_from"), "disk")
        self.assertEqual(by_scope["sec2"]["verdict"], "FAILED")
        self.assertTrue(by_scope["sec2"]["error"].startswith("missing_artifacts:"))
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            self.assertNotEqual(json.load(f)["conclusion"], "PASS")


class TestT10AggregateSchema(T04Base):

    def test_t10_aggregate_schema_validates(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="both")
        state = base_state(self_dir, ("sec1", "sec2"), fmt="both")
        self.plan(state)
        requests = self.requests_of(state)
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests})
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            t4nodes.node_t4_join_review(state)

        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        # 真实 evidence 模块（非替身）零问题
        self.assertEqual(
            evidence.validate_acceptance_json(
                chapter,
                current_digest=state["dispatch_plan"]["evidence_baseline"]["content_digest"],
                student_pdf_sha=sha(os.path.join(self_dir, "student.pdf")),
                student_html_sha=sha(os.path.join(self_dir, "student.html"))),
            [])
        self.assertEqual(set(chapter),
                         set(evidence._ACCEPTANCE_REQUIRED))
        self.assertEqual(chapter["schema_version"], evidence.SCHEMA_VERSION)

    def test_t10_pdf_only_format_binds_pdf_sha(self):
        self_dir = make_self(self.d, ("sec1", "sec2"), fmt="pdf")
        state = base_state(self_dir, ("sec1", "sec2"), fmt="pdf")
        self.plan(state)
        baseline = state["dispatch_plan"]["evidence_baseline"]
        self.assertIsNone(baseline["student_html_sha"])
        requests = self.requests_of(state)
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests})
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            t4nodes.node_t4_join_review(state)
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "PASS")
        # fmt 不含 html → 汇总之 pdf sha 与夹具同口径（html 不参与绑定）
        self.assertEqual(chapter["student_pdf_sha256"],
                         sha(os.path.join(self_dir, "student.pdf")))
        self.assertEqual(
            evidence.validate_acceptance_json(
                chapter,
                current_digest=baseline["content_digest"],
                student_pdf_sha=sha(os.path.join(self_dir, "student.pdf")),
                student_html_sha=None),
            [])


class TestT11EvidenceChain(T04Base):
    """T11 证据链（q_audit.md）：创审分离 INV-2、派发协议防伪造、无证据拦截。"""

    def _manual_state(self, self_dir, requests):
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        reqs = self.requests_of(state)
        state["manual_interrupted_batch"] = state["batch_id"]
        return state, reqs

    def test_t11_inv2_reviewer_same_as_writer_rejected(self):
        """INV-2：T4 验收 worker 与写作阶段 worker 同体 → reviewer_identity_violation。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        state["manual_interrupted_batch"] = state["batch_id"]
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        # 写作阶段（T2）证据：worker=subagent:writer
        agent_evidence.write_evidence(
            self_dir, dispatch_id="dsp-t2-sec1-fake1", batch_id="bat-t2-sec1",
            worker="subagent:writer", artifacts_written=[], output_digests={})
        # 验收证据 worker 与写作者同体 → 违规
        seed_t4_evidence(self_dir, requests, worker="subagent:writer")
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得二次 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "reviewer_identity_violation")
            self.assertTrue(entry["reviewer_identity_violation"])
            self.assertEqual(entry["verdict"], "FAILED")
        self.assertTrue(any("违反创审分离" in h for h in patch["history"]))

    def test_t11_bad_dispatch_protocol_halts(self):
        """伪造派发协议（非 host-subagent-v1）→ HALT 拒绝消费响应产物。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: {"dispatch_protocol": "rogue-v9"}):
            patch = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("BAD_DISPATCH_PROTOCOL", patch["error_message"])
        # 伪造响应不得落盘任何产物
        self.assertFalse(os.path.exists(
            os.path.join(self_dir, ".pd", "acceptance", "sec1.json")))

    def test_t11_unattested_acceptance_fails_by_default(self):
        """验收记录已落盘但无证据台账 → ok_unattested 失败（创审链断裂）。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        state["manual_interrupted_batch"] = state["batch_id"]
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得二次 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "ok_unattested")
            self.assertEqual(entry["verified_via"], "unattested")
            self.assertEqual(entry["verdict"], "FAILED")
        self.assertEqual(patch["dispatch_metrics"].get("evidence_error"), 2)

    def test_t11_attested_acceptance_passes(self):
        """证据台账齐备 → 磁盘重推导判 ok（verified_via=agent），汇总 PASS。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        self.plan(state)
        requests = self.requests_of(state)
        state["manual_interrupted_batch"] = state["batch_id"]
        for req in requests:
            write_scope_record(self_dir, req["scope"])
        seed_t4_evidence(self_dir, requests)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(t4nodes, "interrupt",
                                  lambda p: self.fail("不得二次 interrupt")):
            patch = t4nodes.node_t4_join_review(state)
        for req in requests:
            entry = patch["dispatch_results"][req["dispatch_id"]]
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["verdict"], "PASS")
            self.assertEqual(entry["verified_from"], "disk")
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            self.assertEqual(json.load(f)["conclusion"], "PASS")


class TestT12ScopeReuse(T04Base):
    """任务6（audit §12.5）：local-review / chapter-review 两层 scope 复用。

    复用绑定只认内容摘要：``scope_content_digest``（本节）+ ``chapter_digest``
    （依赖图/装配/顺序/跨节引用）+ ``review_rule_version``——任一其他节变更
    不再顺带作废本节旧 PASS；章节级变更才升级全量 chapter-review。
    """

    def _chapter_digest(self, self_dir, sections):
        return t4nodes._chapter_context_digest(self_dir, list(sections))

    def _stamped(self, self_dir, sec, chapter_digest, **kw):
        """写一份带复用绑定的旧 PASS 记录（scope 摘要取当前盘面）。"""
        kw.setdefault("scope_digest",
                      t4nodes._scope_content_digest(self_dir, sec))
        kw.setdefault("chapter_digest", chapter_digest)
        kw.setdefault("rule_version", t4nodes.REVIEW_RULE_VERSION)
        return write_scope_record(self_dir, sec, **kw)

    def test_t12_all_scopes_reused_zero_dispatch(self):
        """全部 scope 未变 → 零派发直收：不写派发计划、不调 LLM、不 interrupt。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        chapter_digest = self._chapter_digest(self_dir, ("sec1", "sec2"))
        for sec in ("sec1", "sec2"):
            self._stamped(self_dir, sec, chapter_digest)

        patch = self.plan(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "reused")
        self.assertEqual(patch["dispatch_metrics"],
                         {"submitted": 0, "completed": 0, "ok": 0, "failed": 0,
                          "retried": 0, "max_in_flight": 0, "deferred": 0,
                          "reused": 2})
        self.assertEqual(self.requests_of(state), [])
        # 零派发不写派发计划（check_dispatch 的 R3 面向「必须派发」场景）
        self.assertFalse(os.path.isfile(
            os.path.join(self_dir, ".pd", "dispatch-plan.json")))

        # join：无活池 + 禁止 interrupt → 复用记录进 fan-in，chapter PASS
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(
                    t4nodes, "interrupt",
                    lambda p: self.fail("零派发轮不得 interrupt")):
            patch2 = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch2["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch2["routing_action"], "PROCEED")
        self.assertEqual(patch2["dispatch_results"], {})
        self.assertEqual(patch2["dispatch_metrics"]["reused"], 2)
        self.assertEqual(patch2["section_statuses"],
                         {"sec1": "staged", "sec2": "staged"})
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "PASS")
        self.assertEqual(chapter["reviewed_qids"],
                         ["sec1-entry", "sec1-second", "sec2-entry"])
        self.assertTrue(any("零派发" in h for h in patch2["history"]))

    def test_t12_partial_reuse_dispatches_changed_scope_only(self):
        """仅 sec2 内容变更 → 只重审 sec2；sec1 旧 PASS 折叠进聚合。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        chapter_digest = self._chapter_digest(self_dir, ("sec1", "sec2"))
        for sec in ("sec1", "sec2"):
            self._stamped(self_dir, sec, chapter_digest)
        # sec2 追加正文（不改跨节引用 → chapter_digest 不变，仅 scope 摘要变）
        with open(os.path.join(self_dir, "sec2.tex"), "a",
                  encoding="utf-8") as f:
            f.write("补充一句正文。\n")

        patch = self.plan(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_PENDING")
        requests = self.requests_of(state)
        self.assertEqual([r["scope"] for r in requests], ["sec2"])
        self.assertEqual(patch["dispatch_metrics"],
                         {"submitted": 1, "completed": 0, "ok": 0, "failed": 0,
                          "retried": 0, "max_in_flight": 0, "deferred": 0,
                          "reused": 1})

        write_scope_record(self_dir, "sec2")
        pool = FakePool({r["dispatch_id"]: ok_result(r) for r in requests})
        self.run_dispatch(state, pool)
        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: pool):
            patch2 = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch2["stage"], "T4_ACCEPTANCE_PENDING")
        self.assertEqual(patch2["dispatch_metrics"]["reused"], 1)
        self.assertEqual(len(pool.join_calls), 1)

        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "PASS")
        # sec1 的旧记录虽然全局 content_digest 已过期，仍按三重绑定进聚合
        self.assertEqual(chapter["reviewed_qids"],
                         ["sec1-entry", "sec1-second", "sec2-entry"])
        self.assertTrue(any("scope 级复用" in h for h in patch2["history"]))

    def test_t12_chapter_context_change_invalidates_reuse(self):
        """跨节引用变化 → chapter_digest 变 → 全部升级 chapter-review。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        chapter_digest = self._chapter_digest(self_dir, ("sec1", "sec2"))
        for sec in ("sec1", "sec2"):
            self._stamped(self_dir, sec, chapter_digest)
        # sec2 新增指向 sec1 的跨节引用 → 章节级上下文变化
        with open(os.path.join(self_dir, "sec2.tex"), "a",
                  encoding="utf-8") as f:
            f.write("判定方法详见 \\ref{sec1-q1}。\n")

        patch = self.plan(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_PENDING")
        self.assertEqual(patch["dispatch_metrics"]["reused"], 0)
        self.assertEqual([r["scope"] for r in self.requests_of(state)],
                         ["sec1", "sec2"])
        # 章节级变更不写复用留痕（reused_scopes 为空即全量重审）
        self.assertFalse(any("scope 级复用" in h for h in patch["history"]))

    def test_t12_stale_rule_version_forces_review(self):
        """review_rule_version 过期 → 旧 PASS 不得冒充本轮结论。"""
        self_dir = make_self(self.d, ("sec1",))
        state = base_state(self_dir, ("sec1",))
        chapter_digest = self._chapter_digest(self_dir, ("sec1",))
        self._stamped(self_dir, "sec1", chapter_digest, rule_version="0.9")

        patch = self.plan(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_PENDING")
        self.assertEqual(patch["dispatch_metrics"]["reused"], 0)
        self.assertEqual([r["scope"] for r in self.requests_of(state)], ["sec1"])
        # 机制直查：复用判定给出 rule_version 过期原因
        reused, reasons = t4nodes._reusable_scopes(
            self_dir, ["sec1"], chapter_digest)
        self.assertEqual(reused, set())
        self.assertIn("review_rule_version", reasons["sec1"])

    def test_t12_non_pass_record_not_reused(self):
        """旧结论 REVISE → 不复用（不得跳过未通过项）。"""
        self_dir = make_self(self.d, ("sec1",))
        state = base_state(self_dir, ("sec1",))
        chapter_digest = self._chapter_digest(self_dir, ("sec1",))
        self._stamped(self_dir, "sec1", chapter_digest, conclusion="REVISE")

        patch = self.plan(state)
        self.assertEqual(patch["dispatch_metrics"]["reused"], 0)
        self.assertEqual([r["scope"] for r in self.requests_of(state)], ["sec1"])

    def test_t12_deleted_reused_record_self_heals(self):
        """plan 判定复用后记录被删 → 聚合 NEEDS_EVIDENCE，交 verify 重派。"""
        self_dir = make_self(self.d, ("sec1", "sec2"))
        state = base_state(self_dir, ("sec1", "sec2"))
        chapter_digest = self._chapter_digest(self_dir, ("sec1", "sec2"))
        for sec in ("sec1", "sec2"):
            self._stamped(self_dir, sec, chapter_digest)
        self.plan(state)
        os.remove(os.path.join(self_dir, ".pd", "acceptance", "sec1.json"))

        with mock.patch.object(t4nodes, "_live_pool", lambda *a, **k: None), \
                mock.patch.object(
                    t4nodes, "interrupt",
                    lambda p: self.fail("零派发轮不得 interrupt")):
            patch2 = t4nodes.node_t4_join_review(state)
        self.assertEqual(patch2["stage"], "T4_ACCEPTANCE_PENDING")
        with open(os.path.join(self_dir, ".pd", "acceptance", "chapter.json"),
                  "r", encoding="utf-8-sig") as f:
            chapter = json.load(f)
        self.assertEqual(chapter["conclusion"], "NEEDS_EVIDENCE")

    def test_t12_route_after_plan_zero_dispatch(self):
        """图路由：零派发轮绕过 dispatch 节点直收 fan-in。"""
        from workflow import edges as graph_edges
        self.assertEqual(
            graph_edges.route_after_t4_review_plan(
                {"stage": "T4_REVIEW_RUNNING", "dispatch_mode": "reused"}),
            "t4_join_review")
        self.assertEqual(
            graph_edges.route_after_t4_review_plan(
                {"stage": "T4_REVIEW_PENDING"}),
            "t4_dispatch_review")
        self.assertEqual(
            graph_edges.route_after_t4_review_plan({"stage": "HALTED"}),
            graph_edges.END)


if __name__ == "__main__":
    unittest.main()
