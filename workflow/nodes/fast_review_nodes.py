# -*- coding: utf-8 -*-
r"""fast_review_nodes.py — 合并终审（T4_COMBINED_REVIEW）派发节点（修复卡 14）。

为什么需要它
------------
旧链路把「同一份候选」读两遍：T2 每节一个语义审读员写
``.pd/semantic/<sec>.json``，T4 又每节一个验收员写
``.pd/acceptance/<sec>.json``。两套证据互不引用、谁也无法复算对方，于是
**主 Agent 自己写一份 ```conclusion=PASS``` 的 JSON 就能过门**（复盘报告
「重复审读 + 伪证据」根因）。修复卡 14 的合成方案：

    R 个批，每批**一次独立审读**覆盖 1–2 节
        ↓ 审读员真实主产物 .pd/combined-review/<batch>-r<N>.json（schema 2）
        ↓ 宿主确定性投影（scripts/review_projection.py，可重算 + derived_from）
    .pd/semantic/<sec>.json     语义硬闸门（v1 基础字段 + derived_from）
    .pd/acceptance/<sec>.json   验收硬闸门（v2 + review_kind + 双证据引用）

一次审读同时承担「来源忠实性 + 数学正确性 + 呈现完整性 + 交付证据」，因此
既不重复派发，也不可能凭主 Agent 的断言造出 PASS：投影必须能由主产物重算，
且必须在 ``.pd/agent-calls/<dispatch_id>.json`` 里存在该次真实派发的 receipt。

三个节点
--------
1. :func:`node_t4_plan_combined_review`   —— 分批 + 派发单 + 确定性计划门禁。
2. :func:`node_t4_dispatch_combined_review` —— 提交共享进程级 WorkerPool
   （无认知 worker 时全部 deferred → manual 中断，生产默认）。
3. :func:`node_t4_join_combined_review`   —— 收尾：逐批校验主产物 → 投影 →
   认证投影 → 确定性 fan-in 汇总 ``.pd/acceptance/chapter.json``。

本卡边界
--------
- **不删除**旧语义 checker 路径与旧 T4 逐节验收节点（图切换是下一卡），两套
  实现并存，节点各自独立可测。
- 派发计划门禁用 :func:`review_projection.validate_review_plan`（审读轮次）
  而非 ``scripts/check_dispatch.py``（写作者轮次：它的 UNDER_DISPATCH 规则
  建立在「每节拥有自己的产物」上，套到「一个批级主产物覆盖多节」的审读轮次
  只会逼出重复审读）。这不是降低校验，而是换了同一强度的正确判据，见该函数
  文档；门禁报告仍落盘 ``.pd/reports/check_combined_review_plan.json``。
- 任何缺节/缺 qid/结论非 PASS/open issue/摘要不符/无 receipt/创审同人 都
  **阻塞交付**；打乱血缘只豁免「摘要不一致」这一项，绝不覆盖负面结论。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

# 注意：本环境的 langgraph 只在 langgraph.types 下导出 interrupt（顶层无该符号），
# 与 section_batch_nodes / semantic_nodes / t4_review_batch_nodes 同一导入口径。
from langgraph.types import interrupt

from ..state import ShrinkCoursewareState
from .agent_nodes import (
    REQUEST_DIR_REL,
    build_request,
    write_request_file,
)
from .section_batch_nodes import (
    _get_pool,
    _join_timeout,
    _live_pool,
    _load_dispatch_config,
    _plan_allow_unattested,
    _result_field,
    _write_json_atomic,
)
from . import agent_evidence
from . import t4_review_batch_nodes as t4nodes

# scripts/ 同路径注入（插件既有约定），供 review_projection / evidence 使用。
_SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import review_projection  # noqa: E402

__all__ = [
    "COMBINED_REVIEW_PLAN_REL",
    "COMBINED_REVIEW_REPORT_REL",
    "STAGE_COMBINED_REVIEW",
    "DEFAULT_REVIEW_SECTIONS_PER_BATCH",
    "split_review_batches",
    "node_t4_plan_combined_review",
    "node_t4_dispatch_combined_review",
    "node_t4_join_combined_review",
]

#: 合并终审派发计划 / 计划门禁报告（相对 self_dir）
COMBINED_REVIEW_PLAN_REL = ".pd/dispatch-plan-combined-review.json"
COMBINED_REVIEW_REPORT_REL = ".pd/reports/check_combined_review_plan.json"
#: 审读阶段名（角色化真源）
STAGE_COMBINED_REVIEW = "T4_COMBINED_REVIEW"
#: 每批最多覆盖的节数：一次审读覆盖 2 节，既避免重复派发又不让单批过长
DEFAULT_REVIEW_SECTIONS_PER_BATCH = 2


# ---------------------------------------------------------------------------
# 纯函数助手
# ---------------------------------------------------------------------------

def split_review_batches(sections: List[str], batch_size: int = 0
                         ) -> List[List[str]]:
    """把待审小节按 ``batch_size`` 切批（保序、不重排、不丢节）。

    ``batch_size <= 0`` 时用默认 2；只接受 1–2（与 C5「一次覆盖 1–2 个相邻
    节」同口径，审读侧同样不让单批过大而失焦）。
    """
    clean = [str(s) for s in (sections or []) if isinstance(s, str) and s]
    size = int(batch_size or 0) or DEFAULT_REVIEW_SECTIONS_PER_BATCH
    size = max(1, min(2, size))
    return [clean[i:i + size] for i in range(0, len(clean), size)]


def _sha256_file(path: str) -> Optional[str]:
    return review_projection.sha256_file(path)


def _shrink_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """计划里的磁盘视图（只保留门禁需要的字段，避免体积失控）。"""
    keys = ("dispatch_id", "stage", "scope", "role", "input_digest", "artifacts",
            "batch_id", "review_scopes", "scope_input_digests", "prompt_file",
            "success_stage", "request_ref")
    return {k: req.get(k) for k in keys if k in req}


def _expected_qids_by_scope(state: ShrinkCoursewareState, self_dir: str,
                            scopes: List[str]) -> Dict[str, List[str]]:
    plan = state.get("expected_qids") or {}
    out: Dict[str, List[str]] = {}
    for sec in scopes:
        declared = plan.get(sec) if isinstance(plan, dict) else None
        if isinstance(declared, list) and declared:
            out[sec] = [str(q) for q in declared if isinstance(q, str) and q]
            continue
        out[sec] = t4nodes._extract_section_qids(self_dir, sec)
    return out


def _format_artifacts(self_dir: str, output_format: str) -> Dict[str, Optional[str]]:
    """本轮交付产物在派发时的真实指纹（审读员必须绑定它实际看到的文件）。"""
    return review_projection.disk_artifact_digests(self_dir, output_format)


def _accepted_attestation(status: str, allow_unattested: bool) -> bool:
    if status == "ok":
        return True
    return bool(allow_unattested) and status == "ok_unattested"


def _combined_review_interrupt_payload(
    state: ShrinkCoursewareState,
    plan: Dict[str, Any],
    requests: List[Dict[str, Any]],
    batch_id: str,
) -> Dict[str, Any]:
    """宿主批派发（无 python worker）时的整批中断载荷（卡22，C12.2/C12.3）。

    与 ``section_batch_nodes._batch_interrupt_payload`` /
    ``t4_review_batch_nodes._t4_batch_interrupt_payload`` **同键同语义**：一次
    中断携带整批审读派发单 + 可直接执行的 ``dispatch_args``（``self_dir`` +
    ``batch_id``），让宿主在一次 ``qchain_dispatch`` 调用里并发跑完整批，
    而不是逐条 ``request_ref`` 串行。

    审读请求的授权真源是 ``review_scopes``（一个批级主产物覆盖多节，不套 C5
    的逐节产物归属），因此这里逐单带出 ``review_scopes``，不伪造
    ``artifact_scopes``。
    """
    self_dir = state.get("self_dir") or state.get("project_root") or ""
    artifacts: List[str] = []
    sections: List[str] = []
    before_digests: Dict[str, Any] = {}
    for req in requests:
        for art in (req.get("artifacts") or []):
            if art not in artifacts:
                artifacts.append(art)
        for sec in (req.get("review_scopes") or []):
            if sec not in sections:
                sections.append(sec)
        for rel, digest in (req.get("before") or {}).items():
            before_digests[rel] = digest

    max_concurrency = plan.get("max_concurrency")
    try:
        limit = int(max_concurrency)
    except (TypeError, ValueError):
        limit = 1
    min_parallelism = 1 if len(requests) <= 1 else min(2, max(1, limit))

    return {
        "type": "AGENT_DISPATCH",
        "batch_id": batch_id,
        "dispatch_id": batch_id,
        "stage": plan.get("stage") or STAGE_COMBINED_REVIEW,
        "role": "T4 合并终审批派发（fan-out，一次取全批）",
        "description": (
            "T4 合并终审批次 %s：一批 %d 个独立审读范围（%s）。请以 "
            "combined-reviewer 身份按 prompts/%s 对各自范围做一次合并终审，"
            "把 schema 2 主产物写到各自 artifacts 后恢复执行。"
            % (batch_id, len(requests), "、".join(sections) or "<无>",
               review_projection.REVIEW_PROMPT_FILE)),
        "artifacts": artifacts,
        "instruction": "\n\n".join(
            "[%s] %s" % ("、".join(req.get("review_scopes") or []),
                         req.get("instruction") or "")
            for req in requests),
        "command": " && ".join(
            str(req.get("command") or "") for req in requests
            if req.get("command")),
        "prompt_file": "prompts/%s" % review_projection.REVIEW_PROMPT_FILE,
        "request_refs": [
            str(req.get("request_ref")
                or "%s/%s.json" % (REQUEST_DIR_REL, req.get("dispatch_id")))
            for req in requests
        ],
        "before_digests": before_digests,
        "context": {
            "sections": sections,
            "review_batches": list(plan.get("review_batches") or []),
            "output_format": plan.get("output_format"),
            "chapter_digest": plan.get("chapter_digest"),
        },
        **agent_evidence.dispatch_protocol_block(
            batch_id=batch_id, self_dir=self_dir),
        "max_concurrency": max_concurrency,
        "min_parallelism": min_parallelism,
        "batch_size": len(requests),
        "effective_concurrency": plan.get("effective_concurrency"),
        "executor_kind": plan.get("executor_kind"),
        "worker_max_concurrency": plan.get("worker_max_concurrency"),
        "dispatch_contract_version": plan.get("dispatch_contract_version"),
        "requests": [
            {
                "dispatch_id": req.get("dispatch_id"),
                "scope": req.get("scope"),
                "review_scopes": list(req.get("review_scopes") or []),
                "role": req.get("role"),
                "artifacts": list(req.get("artifacts") or []),
                "instruction": req.get("instruction"),
                "command": req.get("command"),
                "prompt_file": "prompts/%s" % review_projection.REVIEW_PROMPT_FILE,
                "request_ref": str(
                    req.get("request_ref")
                    or "%s/%s.json" % (REQUEST_DIR_REL,
                                       req.get("dispatch_id"))),
                "before_digests": req.get("before") or {},
                "input_digest": req.get("input_digest"),
                "timeout_seconds": req.get("timeout_seconds"),
                "max_retries": req.get("max_retries"),
            }
            for req in requests
        ],
    }


def _review_results_from_disk(
    self_dir: str, requests: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """宿主派发后按磁盘事实重推导每请求状态（卡22，绝不伪造成功）。

    只回答「主产物在不在盘上」；产物是否**可认证**由下游的
    ``agent_evidence.check_request_attested`` 与
    ``review_projection.validate_review_file`` 逐项判（receipt / 指纹 /
    节覆盖 / qid / 结论），因此这一步不构成任何放行捷径。
    """
    results: Dict[str, Dict[str, Any]] = {}
    for req in requests:
        dispatch_id = str(req.get("dispatch_id") or "")
        rel = str((req.get("artifacts") or [""])[0] or "")
        path = os.path.join(self_dir, rel.replace("/", os.sep)) if rel else ""
        present = bool(path) and os.path.isfile(path)
        results[dispatch_id] = {
            "dispatch_id": dispatch_id,
            "status": "ok" if present else "missing_artifacts",
            "settled_from_disk": True,
            "observation_scope": "host_call",
        }
    return results


# ---------------------------------------------------------------------------
# 节点 1：分批规划
# ---------------------------------------------------------------------------

def node_t4_plan_combined_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """规划合并终审批次：R 个请求，每请求一次审读覆盖 1–2 节。

    输出 ``dispatch_plan``（磁盘视图 + ``requests_full``）与
    ``stage="T4_COMBINED_REVIEW_PENDING"``；计划门禁不过一律 HALT——
    绝不在覆盖不全/指纹缺失的计划上派发「看起来审过了」的批次。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))

    expected = [str(s) for s in (state.get("expected_sections") or [])
                if isinstance(s, str) and s]
    if not expected:
        history.append("T4 Combined Review: 缺少 expected_sections，无法规划审读批次")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "合并终审缺少待审小节（expected_sections 为空）",
            "history": history,
        }

    # 只审「本轮需要复审」的节：缺省 = 全部待审节（不静默跳过任何一节）。
    pending = [str(s) for s in (state.get("combined_review_scopes") or [])
               if isinstance(s, str) and s] or expected
    pending = [s for s in expected if s in set(pending)] or expected

    fmt = t4nodes._evidence_baseline(state, self_dir).get("output_format") or "both"
    qids_by_sec = _expected_qids_by_scope(state, self_dir, pending)
    baseline = t4nodes._evidence_baseline(state, self_dir)
    chapter_digest = t4nodes._chapter_context_digest(self_dir, expected)
    cfg = _load_dispatch_config(self_dir, str(state.get("run_scope") or "").strip())
    batch_size = int(state.get("review_sections_per_batch")
                     or DEFAULT_REVIEW_SECTIONS_PER_BATCH)
    batches = split_review_batches(pending, batch_size)
    batch_id = "bat-t4c-%s" % uuid.uuid4().hex[:8]

    delivery = _format_artifacts(self_dir, fmt)
    section_digests = review_projection.disk_section_digests(self_dir, pending)
    requests: List[Dict[str, Any]] = []
    review_batches: List[Dict[str, Any]] = []
    for index, scopes in enumerate(batches, start=1):
        artifact = review_projection.review_artifact_rel(batch_id, index)
        scope_digests = {sec: section_digests.get(sec) for sec in scopes}
        req = build_request(
            state,
            stage=STAGE_COMBINED_REVIEW,
            role=review_projection.combined_reviewer_role(scopes),
            prompt_file=review_projection.REVIEW_PROMPT_FILE,
            artifacts=[artifact],
            instruction=(
                "对 %s 做一次合并终审：逐节比对来源与候选（忠实性）、核对数学"
                "正确性、检查呈现完整性与编号、绑定本轮交付产物指纹，输出"
                " schema_version=2 主产物到 %s。禁止引用其他小节的结论代替本节"
                "审读。" % ("、".join(scopes), artifact)),
            command="python scripts/review_projection.py %s --review %s"
                    % (self_dir, artifact),
            context={
                "run_id": state.get("run_id", ""),
                "batch_id": batch_id,
                "scopes": scopes,
                "scope_digests": scope_digests,
                "expected_qids": {sec: qids_by_sec.get(sec) or [] for sec in scopes},
                "output_format": fmt,
                "delivery_artifacts": delivery,
                "content_digest": baseline.get("content_digest"),
                "chapter_digest": chapter_digest,
            },
            scope=scopes[0],
            batch_id=batch_id,
            timeout_seconds=cfg["timeout_seconds"],
            max_retries=cfg["max_retries"],
            retry_class="transient",
        )
        # 审读请求的授权真源：review_scopes（一个批级主产物覆盖多节，
        # 不套 C5 的逐节产物归属；覆盖完整性由计划门禁判定）。
        req["review_scopes"] = list(scopes)
        req["scope_input_digests"] = dict(scope_digests)
        req["reviewer_role_kind"] = "combined_source_fidelity"
        req["before"] = {artifact: None}
        req["request_ref"] = "%s/%s.json" % (REQUEST_DIR_REL, req["dispatch_id"])
        req["success_stage"] = "T4_COMBINED_REVIEW_PENDING"
        req["success_history"] = "合并终审批次 %s 已产出 %s" % (batch_id, artifact)
        write_request_file(self_dir, req)
        requests.append(req)
        review_batches.append({
            "batch_index": index,
            "scopes": list(scopes),
            "dispatch_id": req["dispatch_id"],
            "artifact": artifact,
            "section_digests": dict(scope_digests),
        })

    plan_disk: Dict[str, Any] = {
        "batch_id": batch_id,
        "stage": STAGE_COMBINED_REVIEW,
        "sections": list(pending),
        "requests": [_shrink_request(req) for req in requests],
        "max_concurrency": int(cfg["max_concurrency"]),
        "batch_size": len(requests),
        "review_batches": review_batches,
        "expected_sections": list(expected),
        "expected_qids": qids_by_sec,
        "evidence_baseline": baseline,
        "chapter_digest": chapter_digest,
        "output_format": fmt,
        "delivery_artifacts": delivery,
        "requests_full": requests,
        "dispatch_config": cfg,
        "plan_version": 1,
    }

    # ---- 派发前确定性门禁（覆盖 + 角色化 + 指纹 + 写域互斥） ----
    problems = review_projection.validate_review_plan(
        plan_disk, expected_stage=STAGE_COMBINED_REVIEW, batch_id=batch_id)
    report = {
        "schema_version": 1,
        "gate_name": "check_combined_review_plan",
        "tool": review_projection.TOOL_NAME,
        "tool_version": review_projection.TOOL_VERSION,
        "status": "PASS" if not problems else "FAIL",
        "batch_id": batch_id,
        "stage": STAGE_COMBINED_REVIEW,
        "checked": {"requests": len(requests), "sections": len(pending),
                    "batches": len(batches)},
        "issues": problems,
    }
    _write_json_atomic(os.path.join(self_dir, COMBINED_REVIEW_REPORT_REL), report)
    _write_json_atomic(os.path.join(self_dir, COMBINED_REVIEW_PLAN_REL), plan_disk)
    if problems:
        history.append("T4 Combined Review: 计划门禁未通过（%d 项），拒绝派发"
                       % len(problems))
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "合并终审计划门禁失败: %s" % "; ".join(problems[:5]),
            "combined_review_plan_problems": problems,
            "history": history,
        }

    try:
        agent_evidence.write_batch_index(
            self_dir,
            batch_id=batch_id,
            run_id=str(state.get("run_id") or ""),
            stage="T4_COMBINED_REVIEW",
            request_ids=[req["dispatch_id"] for req in requests],
            plan_rel=COMBINED_REVIEW_PLAN_REL,
            plan_digest=_sha256_file(
                os.path.join(self_dir, COMBINED_REVIEW_PLAN_REL)),
            sections=list(pending),
            required_scope_count=len(pending),
            dispatch_contract_version=1,
            extra={"review_batches": review_batches,
                   "review_sections_per_batch": batch_size},
        )
    except ValueError as exc:            # pragma: no cover - 防御性
        history.append("T4 Combined Review: 批次索引落盘失败（%s）" % exc)
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "合并终审批次索引落盘失败: %s" % exc,
            "history": history,
        }

    history.append(
        "T4 Combined Review: 批次 %s 规划 %d 个审读请求覆盖 %d 节（每批≤%d 节，"
        "%s）" % (batch_id, len(requests), len(pending), batch_size, fmt))
    return {
        "stage": "T4_COMBINED_REVIEW_PENDING",
        "routing_action": "PROCEED",
        "dispatch_plan": plan_disk,
        "batch_id": batch_id,
        "combined_review_scopes": list(pending),
        "retry_count": 0,
        "dispatch_history": t4nodes._archived_history(state),
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 2：派发
# ---------------------------------------------------------------------------

def node_t4_dispatch_combined_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """把合并终审批次提交到共享进程级 WorkerPool（与 T4 逐节验收同款语义）。"""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")

    if not plan or plan.get("batch_id") != batch_id:
        history.append("T4 Combined Review: 批次 %s 计划失效（stale plan）" % batch_id)
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划与当前批次不符（stale plan）",
            "history": history,
        }
    requests = list(plan.get("requests_full") or [])
    if not requests:
        history.append("T4 Combined Review: 计划缺少 requests_full")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    cfg = plan.get("dispatch_config") or _load_dispatch_config(self_dir)
    pool = _get_pool(batch_id, cfg["max_concurrency"], cfg["timeout_seconds"],
                     cfg["max_retries"])
    for req in requests:
        pool.submit(req)
    stats = pool.stats()
    deferred = int(stats.get("deferred", 0) or 0)
    mode = "manual" if deferred > 0 else "parallel"
    history.append(
        "T4 Combined Review: 批次 %s 已提交 %d 个审读请求（模式 %s，deferred=%d）"
        % (batch_id, len(requests), mode, deferred))
    return {
        "stage": "T4_COMBINED_REVIEW_RUNNING",
        "dispatch_mode": mode,
        "dispatch_metrics": stats,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 3：收尾（校验 → 投影 → 认证 → 确定性 fan-in）
# ---------------------------------------------------------------------------

def _chapter_aggregate_record(self_dir: str, baseline: Dict[str, Any],
                              evaluated: List[Tuple[str, Dict[str, Any],
                                                    Optional[Dict[str, Any]]]],
                              expected_total_qids: List[str],
                              review_refs: List[str]) -> Dict[str, Any]:
    """写 ``.pd/acceptance/chapter.json``（与旧 fan-in 同一 10 键 v1 形状）。

    下游（``node_t4_verify_acceptance`` / ``scripts/deliver.py``）按 v1 形状读
    汇总记录，因此这里保持 10 键；但 stage1/stage2 的**文字**如实说明它们来自
    合并终审的确定性投影，绝不冒充「两阶段独立盲审」的历史记录。
    """
    reviewed: List[str] = []
    issues: List[Dict[str, Any]] = []
    seen_issue_ids = set()
    for _scope, _entry, rec in sorted(evaluated, key=lambda item: item[0]):
        if rec is None:
            continue
        for qid in (rec.get("reviewed_qids") or []):
            if qid not in reviewed:
                reviewed.append(qid)
        for issue in (rec.get("issues") or []):
            if not isinstance(issue, dict):
                continue
            iid = str(issue.get("id") or "")
            if iid and iid in seen_issue_ids:
                continue
            if iid:
                seen_issue_ids.add(iid)
            issues.append(issue)
    reviewed_qids = sorted(reviewed)
    entries = [entry for _s, entry, _r in evaluated]
    refs = "，".join(review_refs) or "<无主产物>"
    aggregate = {
        "schema_version": t4nodes.evidence.SCHEMA_VERSION,
        "scope": "chapter",
        "reviewed_qids": reviewed_qids,
        "content_digest": baseline.get("content_digest"),
        "student_pdf_sha256": baseline.get("student_pdf_sha"),
        "conclusion": t4nodes._aggregate_conclusion(
            entries, reviewed_qids, expected_total_qids, issues),
        "reviewer_role": review_projection.AGGREGATE_REVIEWER_ROLE,
        "stage1_record": "合并终审确定性投影（review_kind=%s；主产物 %s）"
                         % (review_projection.REVIEW_KIND, refs),
        "stage2_record": "逐节忠实性/数学/交付证据见 .pd/acceptance/<sec>.json 的 "
                         "source_comparison 与 content_review_refs/delivery_check_ref",
        "issues": issues,
    }
    _write_json_atomic(os.path.join(self_dir, t4nodes.ACCEPTANCE_CHAPTER_REL),
                       aggregate)
    return aggregate


def node_t4_join_combined_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """收尾：逐批「校验主产物 → 投影 → 认证投影」，再确定性 fan-in 汇总。

    任一环节不成立（缺产物/缺节/缺 qid/结论非 PASS/摘要不符/无 receipt/创审
    同人）都让该批判 FAILED，从而**阻塞交付**——成功批次可以复用其投影，但只有
    全部批次 PASS 才能得到全局 PASS。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")

    if not plan or plan.get("batch_id") != batch_id:
        history.append("T4 Combined Review: 批次 %s 计划失效（stale plan）" % batch_id)
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划与当前批次不符（stale plan）",
            "history": history,
        }
    requests = list(plan.get("requests_full") or [])
    if not requests:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    pool = _live_pool(batch_id)
    raw_results: Dict[str, Any] = {}
    if pool is not None:
        raw_results = pool.join(timeout=_join_timeout(requests)) or {}

    # 卡22（C12.2/C12.3）：宿主批派发（无 python worker → 请求全部 deferred）路径。
    # 与 T2 写作 / legacy T4 验收同构：
    #   a) 首次进入 → 一次整批 AGENT_DISPATCH 中断（同一 batch_id 绝不二次中断），
    #      返回 T4_COMBINED_REVIEW_RUNNING 让 graph 的收尾自环再进来；
    #   b) 已中断过 / 并行批次崩溃重启 → 逐请求从磁盘重推导，缺产物的请求独立
    #      判失败（不牵连已完成的批次，也不退化为串行重派）。
    manual_mode = str(state.get("dispatch_mode") or "") == "manual"
    already_interrupted = state.get("manual_interrupted_batch") == batch_id
    settled_from_disk = False
    if not raw_results and (manual_mode or already_interrupted):
        if not already_interrupted:
            payload = _combined_review_interrupt_payload(
                state, plan, requests, batch_id)
            history.append(
                "T4 Combined Review: 批次 %s 无可用 python worker（宿主派发），"
                "中断一次携带整批 %d 份审读派发单" % (batch_id, len(requests)))
            response = interrupt(payload)
            violation = agent_evidence.dispatch_protocol_violation(response)
            if violation:
                history.append("T4 Combined Review: %s" % violation)
                return {
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": violation,
                    "history": history,
                }
            return {
                "dispatch_results": {},
                "manual_interrupted_batch": batch_id,
                "dispatch_mode": "manual",
                "stage": "T4_COMBINED_REVIEW_RUNNING",
                "history": history,
            }
        raw_results = _review_results_from_disk(self_dir, requests)
        settled_from_disk = True
        history.append(
            "T4 Combined Review: 批次 %s 从磁盘重推导 %d 个审读结果"
            "（不重复中断）" % (batch_id, len(raw_results)))
    allow_unattested = _plan_allow_unattested(plan, self_dir)
    baseline = dict(plan.get("evidence_baseline")
                    or t4nodes._evidence_baseline(state, self_dir))
    fmt = str(plan.get("output_format") or baseline.get("output_format") or "both")
    expected_qids = dict(plan.get("expected_qids") or {})
    expected_total_qids = t4nodes._plan_expected_total_qids(
        {**plan, "expected_sections": plan.get("expected_sections")
         or plan.get("sections")})

    evaluated: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]] = []
    problems_all: List[str] = []
    review_refs: List[str] = []
    written: List[str] = []
    for req in requests:
        scopes = [str(s) for s in (req.get("review_scopes") or [])
                  if isinstance(s, str) and s]
        artifact = str((req.get("artifacts") or [""])[0] or "")
        dispatch_id = str(req.get("dispatch_id") or "")
        result = raw_results.get(dispatch_id)
        res_status = (str(_result_field(result, "status", "missing_result")
                          or "missing_result") if result is not None
                      else "missing_result")

        if res_status not in ("ok", "completed", "success"):
            problems = ["REVIEW_RESULT: 请求 %s 结果状态 %r（非 ok）"
                        % (dispatch_id, res_status)]
        else:
            review_path = os.path.join(
                self_dir, artifact.replace("/", os.sep))
            review_sha = _sha256_file(review_path)
            attest, _attest_ev = agent_evidence.check_request_attested(
                self_dir, req, {artifact: review_sha} if review_sha else {},
                allow_unattested)
            problems = [] if _accepted_attestation(attest, allow_unattested) else [
                "REVIEW_UNATTESTED: 请求 %s 的合并终审主产物缺少真实子 Agent "
                "证据台账（%s）—— 主 Agent 自写的 PASS 不得认证"
                % (dispatch_id, attest)]
            if not problems:
                problems = review_projection.validate_review_file(
                    self_dir, artifact,
                    expected_scopes=scopes,
                    expected_qids={sec: expected_qids.get(sec) or []
                                   for sec in scopes},
                    request_digest=str(req.get("input_digest") or ""),
                    output_format=fmt,
                    section_digests=review_projection.disk_section_digests(
                        self_dir, scopes),
                    disk_digests=review_projection.disk_artifact_digests(
                        self_dir, fmt))
            if not problems:
                fresh, problems = review_projection.write_projections(
                    self_dir, artifact,
                    expected_scopes=scopes,
                    expected_qids={sec: expected_qids.get(sec) or []
                                   for sec in scopes},
                    output_format=fmt,
                    section_digests=review_projection.disk_section_digests(
                        self_dir, scopes),
                    disk_digests=review_projection.disk_artifact_digests(
                        self_dir, fmt))
                if not problems:
                    written.extend(fresh)
                    for scope in scopes:
                        for kind in ("semantic", "acceptance"):
                            rel = review_projection.project_paths(
                                self_dir, scope)[kind]
                            problems.extend(
                                review_projection.verify_projection_file(
                                    self_dir, rel, kind))
        if problems:
            problems_all.extend(problems)

        for scope in scopes:
            rec = None
            if not problems:
                rec = review_projection._load_json(os.path.join(
                    self_dir,
                    review_projection.project_paths(self_dir, scope)[
                        "acceptance"].replace("/", os.sep)))
            if problems:
                entry = {"verdict": "FAILED",
                         "error": "combined_review: %s" % "; ".join(problems[:3])}
            else:
                conclusion = str((rec or {}).get("conclusion") or "NEEDS_EVIDENCE")
                verdict = "PASS" if conclusion == "PASS" else (
                    "REVISE" if conclusion == "REVISE" else "NEEDS_EVIDENCE")
                entry = {"verdict": verdict}
            evaluated.append((scope, entry, rec))
        review_refs.append("%s(%s)" % (artifact, dispatch_id))

    aggregate = _chapter_aggregate_record(
        self_dir, baseline, evaluated, expected_total_qids, review_refs)
    conclusion = str(aggregate.get("conclusion") or "NEEDS_EVIDENCE")
    ok_scopes = [scope for scope, entry, _r in evaluated
                 if entry.get("verdict") == "PASS"]
    fail_scopes = [scope for scope, entry, _r in evaluated
                   if entry.get("verdict") != "PASS"]
    metrics = {
        "submitted": len(requests), "completed": len(requests),
        "ok": len(ok_scopes), "failed": len(fail_scopes),
        "reused": int((state.get("dispatch_metrics") or {}).get("reused", 0) or 0),
        "review_batches": len(requests), "projections_written": len(written),
        # 卡22：本次收尾的结果是宿主派发后从磁盘重推导的（不是进程内 join 拿到的）
        "settled_from_disk": settled_from_disk,
    }
    statuses = dict(state.get("section_statuses") or {})
    for scope in ok_scopes:
        statuses.setdefault(scope, "reviewed")

    history.append(
        "T4 Combined Review: 批次 %s 收尾，汇总结论 %s（PASS 节 %d/%d，投影 %d 份）"
        % (batch_id, conclusion, len(ok_scopes), len(evaluated), len(written)))
    patch: Dict[str, Any] = {
        "dispatch_metrics": metrics,
        "section_statuses": statuses,
        "combined_review_conclusion": conclusion,
        "combined_review_problems": problems_all,
        "combined_review_artifacts": review_refs,
        "history": history,
    }
    if problems_all or conclusion == "NEEDS_EVIDENCE":
        patch.update({
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "合并终审失败: %s" % "; ".join(
                (problems_all or ["证据不足（NEEDS_EVIDENCE）"])[:5]),
        })
    elif conclusion == "REVISE":
        patch.update({
            "stage": "T4_COMBINED_REVIEW_REVISE",
            "routing_action": "REVISE",
            "error_message": "合并终审要求返工（REVISE）",
        })
    else:
        patch.update({
            "stage": "T4_ACCEPTANCE_PENDING",
            "routing_action": "PROCEED",
        })
    return patch
