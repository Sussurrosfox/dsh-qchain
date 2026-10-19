# -*- coding: utf-8 -*-
r"""t4_review_batch_nodes.py — T4 验收并行 fan-out 派发节点（P2 重构）。

P2 背景
--------
P0/P1 之前，T4 教学验收是"单发单收"：一个 ``独立教学验收专员`` 请求覆盖全部
qid，只产出唯一的 ``.pd/acceptance/chapter.json``（``scope="chapter"``）。
audit_agent.md §12.2 Level D + §12.3 要求把固定角色
"acceptance-reviewer：只读冻结产物，返回一个 scope 的 PASS/REVISE" 下沉到
每个切片：**一个 scope 一个独立验收专员**，经共享进程级 ``WorkerPool`` 并行
派发，再由本模块以确定性 fan-in 聚合出 chapter.json。下游消费者
（``node_t4_verify_acceptance`` 与 ``scripts/deliver.py``）本就是 per-scope
的，聚合产物与旧单发形状完全一致，因此无需改动任何下游代码。

三个节点（LangGraph node 签名 ``(state) -> dict patch``）
--------------------------------------------------------
1. :func:`node_t4_plan_review`     —— 规划验收批次：per-scope 派发单 + 磁盘
                                     计划 + ``check_dispatch`` 门禁 + 证据基线。
2. :func:`node_t4_dispatch_review` —— 提交批次到共享 WorkerPool（注册了认知
                                     worker 时并行；无 worker 时退化为 manual
                                     中断，生产默认）。
3. :func:`node_t4_join_review`     —— 收尾：PARALLEL / MANUAL 双路判定
                                     per-scope 验收记录，确定性聚合
                                     ``.pd/acceptance/chapter.json``，随后交回
                                     ``node_t4_verify_acceptance`` 唯一决策。

关键约束
--------
- 只读复用 P1 的 :mod:`section_batch_nodes` 助手（本模块不复制派发基础设施）。
- 本模块**绝不**发出 ``REPAIR_SECTION`` / ``REDISPATCH_ACCEPTANCE``：验收重试
  轮次（``acceptance_rounds <= 3`` 熔断）只由 ``node_t4_verify_acceptance``
  掌握，避免出现第二个循环边界。
- fan-in 聚合是纯确定性计算（无 LLM）：同一批 per-scope 记录必然得到同一份
  chapter.json，checkpoint 重启后可重复推导。
- per-scope 的 ``.pd/acceptance/<sec>.json`` 全部保留在盘上（deliver.py 会
  逐份校验），聚合只**新增** chapter.json，不删除任何切片记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from langgraph.types import interrupt

from ..state import ShrinkCoursewareState
from ..adapters.script_runner import ScriptRunner
from .agent_nodes import (
    REQUEST_DIR_REL,
    _write_worker_files,
    build_request,
    write_request_file,
)
from .section_batch_nodes import (
    DISPATCH_PLAN_REL,
    DISPATCH_REPORT_REL,
    _disk_metrics,
    _disk_plan,
    _gate_error_codes,
    _get_pool,
    _join_timeout,
    _live_pool,
    _load_dispatch_config,
    _plan_allow_unattested,
    _result_field,
    _shrink_output,
    _verify_requests_from_disk,
    _write_json_atomic,
)
from . import agent_evidence

# scripts/evidence.py 与 delivery_nodes.py 同路径注入（本插件既有约定）。
_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import evidence  # noqa: E402

__all__ = [
    "node_t4_plan_review",
    "node_t4_dispatch_review",
    "node_t4_join_review",
]

#: qid 抽取正则（与 section_nodes._sync_required_acceptance_scopes 逐字一致，
#: 含 re.M 多行锚定，否则 ``^`` 只匹配文本开头）
RE_QID = re.compile(r"^\s*%\s*qid:\s*([^\s%]+)", re.M)

#: 验收记录目录 / 汇总记录（均相对 self_dir）
ACCEPTANCE_DIR_REL = os.path.join(".pd", "acceptance")
ACCEPTANCE_CHAPTER_REL = os.path.join(".pd", "acceptance", "chapter.json")

#: 汇总记录 reviewer_role 固定值（§12.3 固定角色的 fan-in 署名）
AGGREGATE_REVIEWER_ROLE = "T4 并行验收 fan-in 汇总（acceptance-reviewer × N）"

#: 汇总记录的 stage 记录缺省占位（保证 _ACCEPTANCE_REQUIRED 非空字符串）
_EMPTY_STAGE_RECORD = "（本批次未产生可解析的切片验收记录）"

#: 任务6：scope 级复用的验收规则版本。复审规则/记录 schema 变更时递增，
#: 旧版本验收记录一律失效重审（禁止旧规则结论冒充本轮结论）。
REVIEW_RULE_VERSION = "1.0"

#: 跨节引用指纹正则（章节级上下文变更检测：某节引用其他节的文本变化）
_RE_SEC_REF = re.compile(r"sec[0-9]+[a-z]?")

#: langgraph 的中断/冒泡异常类名（必须原样上抛，绝不能被吞掉）
_INTERRUPT_CLASS_NAMES = frozenset(
    {"GraphBubbleUp", "GraphInterrupt", "Interrupt", "ParentCommand"}
)


# ---------------------------------------------------------------------------
# 纯函数助手
# ---------------------------------------------------------------------------

def _is_interrupt_exception(exc: BaseException) -> bool:
    """判断异常是否为 langgraph 的中断/冒泡语义（需原样上抛）。"""
    for cls in type(exc).__mro__:
        if cls.__name__ in _INTERRUPT_CLASS_NAMES:
            return True
    return False


def _extract_section_qids(self_dir: str, sec: str) -> List[str]:
    """从 ``self/<sec>.tex`` 抽取该节全部 qid（缺失/不可读返回空表）。

    正则与 :func:`section_nodes._sync_required_acceptance_scopes` 逐字一致，
    保证派发阶段的 qid 口径与 run.json 的 ``required_acceptance_scopes``
    口径不会漂移。
    """
    path = os.path.join(self_dir, f"{sec}.tex")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return RE_QID.findall(text)


def _expected_total_qids(
    state: ShrinkCoursewareState,
    self_dir: str,
    qids_by_sec: Dict[str, List[str]],
) -> List[str]:
    """全批次期望 qid 并集（保持 expected_sections 顺序）。

    优先按节抽取；抽取结果为空时回退到 run.json 的
    ``required_acceptance_scopes["chapter"]``（_sync_required_acceptance_scopes
    写入的口径）。
    """
    total: List[str] = []
    for sec in (state.get("expected_sections") or []):
        for qid in (qids_by_sec.get(sec) or []):
            if qid not in total:
                total.append(qid)
    if total:
        return total
    run_path = os.path.join(self_dir, ".pd", "run.json")
    try:
        if os.path.isfile(run_path):
            with open(run_path, "r", encoding="utf-8-sig") as f:
                run = json.load(f)
            chapter = (run.get("required_acceptance_scopes") or {}).get("chapter") or []
            return [str(q) for q in chapter]
    except (OSError, ValueError):
        pass
    return total


def _evidence_baseline(
    state: ShrinkCoursewareState, self_dir: str
) -> Dict[str, Any]:
    """验收证据基线，与 ``node_t4_verify_acceptance`` 的计算逐字一致。

    派生出的 digest / sha 在派发时随 context 下发给每个验收专员，并在 fan-in
    时用于校验切片记录与汇总记录的证据绑定（同一基线，双处校验）。
    """
    fmt = (
        state.get("output_format")
        or (state.get("user_spec") or {}).get("output_format")
        or "both"
    )
    current_digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    student_pdf_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.pdf"))
        if fmt in ("pdf", "both") else None
    )
    student_html_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.html"))
        if fmt in ("html", "both") else None
    )
    return {
        "content_digest": current_digest,
        "student_pdf_sha": student_pdf_sha,
        "student_html_sha": student_html_sha,
        "output_format": fmt,
    }


# ---------------------------------------------------------------------------
# 任务6：scope 级复用（local-review / chapter-review 两层）
# ---------------------------------------------------------------------------

def _scope_content_digest(self_dir: str, sec: str) -> str:
    """单节内容摘要（``<sec>.tex`` 全文 sha256；缺失/不可读返回空串）。

    与全局 ``content_digest``（collect_inputs 全量输入）的区别：任一其他节
    变更不会影响本节摘要——这是 scope 级复用的绑定基准。
    """
    path = os.path.join(self_dir, f"{sec}.tex")
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _chapter_context_digest(
    self_dir: str, expected_sections: List[str]
) -> str:
    """章节级上下文摘要：依赖图 + 装配 main.tex + 节顺序 + 跨节引用指纹。

    任一变更 → **全部** scope 升级 chapter-review（不得局部复用）：
    依赖图/编号拓扑变化、main.tex 装配变化、节顺序变化、任一节的跨节引用
    集合变化，都会改变其他节的验收语境（q_audit §12.5）。
    """
    parts: List[str] = []
    for rel in (os.path.join(".pd", "dependency-map.md"), "main.tex"):
        path = os.path.join(self_dir, rel)
        try:
            with open(path, "rb") as f:
                parts.append(
                    rel.replace(os.sep, "/") + ":"
                    + hashlib.sha256(f.read()).hexdigest()
                )
        except OSError:
            parts.append(rel.replace(os.sep, "/") + ":missing")
    parts.append("sections:" + ",".join(expected_sections))
    for sec in expected_sections:
        path = os.path.join(self_dir, f"{sec}.tex")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            parts.append(f"xref:{sec}:missing")
            continue
        refs = sorted({m for m in _RE_SEC_REF.findall(text) if m != sec})
        parts.append(f"xref:{sec}:{','.join(refs)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _reusable_scopes(
    self_dir: str,
    batch: List[str],
    chapter_digest: str,
) -> Tuple[set, Dict[str, str]]:
    """任务6：挑出本轮可复用的 scope（内容未变且旧结论 PASS）。

    复用条件（全部满足才允许，缺一即重审——旧 PASS 不得冒充本轮结果）：

    - ``.pd/acceptance/<sec>.json`` 存在且可解析、schema 校验零问题；
    - ``conclusion == PASS``；
    - ``review_rule_version == REVIEW_RULE_VERSION``（复审规则未变更）；
    - ``scope_content_digest`` 等于当前 ``<sec>.tex`` 摘要（本节未改）；
    - ``chapter_digest`` 等于当前章节级上下文摘要（无章节级变更）。

    返回 ``(可复用集合, 未复用原因表)``；原因表供 history 留痕（可验证）。
    """
    reused: set = set()
    reasons: Dict[str, str] = {}
    for sec in batch:
        rec, err = _load_scope_record(self_dir, sec)
        if rec is None:
            reasons[sec] = str(err or "missing")
            continue
        if str(rec.get("conclusion") or "").strip().upper() != "PASS":
            reasons[sec] = f"旧结论 {rec.get('conclusion')}（非 PASS 不复用）"
            continue
        if rec.get("review_rule_version") != REVIEW_RULE_VERSION:
            reasons[sec] = "review_rule_version 过期（复审规则已变更）"
            continue
        if rec.get("scope_content_digest") != _scope_content_digest(self_dir, sec):
            reasons[sec] = "本节内容已变更"
            continue
        if rec.get("chapter_digest") != chapter_digest:
            reasons[sec] = "章节级上下文已变更（依赖图/装配/顺序/跨节引用）"
            continue
        problems = evidence.validate_acceptance_json(rec)
        if problems:
            reasons[sec] = "schema: " + "; ".join(str(p) for p in problems)
            continue
        reused.add(sec)
    return reused, reasons


def _stamp_review_provenance(
    self_dir: str, sec: str, rec: Optional[Dict[str, Any]],
    chapter_digest: str,
) -> None:
    """把 scope 级复用绑定字段盖写入验收记录（主控确定性盖章）。

    任务6：``scope_content_digest`` / ``chapter_digest`` / ``review_rule_version``
    三项**不信任 LLM 自填**，由主控按磁盘现状计算后原子覆写——否则复用判定
    可被生成方伪造。仅追加字段，不改结论与 issues。
    """
    if not isinstance(rec, dict):
        return
    rec["scope_content_digest"] = _scope_content_digest(self_dir, sec)
    rec["chapter_digest"] = chapter_digest
    rec["review_rule_version"] = REVIEW_RULE_VERSION
    path = os.path.join(self_dir, ACCEPTANCE_DIR_REL, f"{sec}.json")
    _write_json_atomic(path, rec)


def _fold_reused_scopes(
    self_dir: str,
    expected_sections: List[str],
    requested_scopes: set,
    chapter_digest: str,
    batch_id: str,
    evaluated: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]],
    history: List[str],
) -> List[str]:
    """把未本轮派发但证据仍有效的旧 PASS 记录纳入 fan-in 聚合。

    绑定口径（替代旧的全局 content_digest 校验）：三重一致
    （scope_content_digest + chapter_digest + review_rule_version）且 schema
    零问题，才允许旧 PASS 进入汇总。任一其他节变更不再顺带作废本节记录
    （scope 级复用）；但 student.pdf/html 因构建时间戳每次 sha 都变，
    故复用绑定只认内容摘要，产物 sha 仅作 provenance 留档。
    """
    folded: List[str] = []
    for sec in expected_sections:
        if not sec or sec in requested_scopes:
            continue
        rec, _err = _load_scope_record(self_dir, sec)
        if rec is None:
            continue
        if str(rec.get("conclusion") or "").strip().upper() != "PASS":
            continue
        if rec.get("review_rule_version") != REVIEW_RULE_VERSION:
            continue
        if rec.get("scope_content_digest") != _scope_content_digest(self_dir, sec):
            continue
        if rec.get("chapter_digest") != chapter_digest:
            continue
        if evidence.validate_acceptance_json(rec):
            continue
        evaluated.append((sec, {
            "dispatch_id": f"disk-{sec}",
            "batch_id": batch_id,
            "scope": sec,
            "status": "ok",
            "attempts": 1,
            "error": None,
            "verdict": "PASS",
            "conclusion": "PASS",
            "reused": True,
        }, rec))
        folded.append(sec)
    if folded:
        history.append(
            "T4 Review Dispatch: 复用旧验收记录 %s（scope 级绑定有效，未重派）"
            % ", ".join(sorted(folded))
        )
    return folded


def _t4_disk_plan(
    batch_id: str,
    batch: List[str],
    requests: List[Dict[str, Any]],
    max_concurrency: int,
    effective_limit: Optional[int] = None,
    worker_capabilities: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """磁盘派发计划：复用 P1 的 ``_disk_plan``，仅把 stage 覆盖为 ``T4``。

    check_dispatch.py 的 stage 是自由字段（只校验存在性），覆盖后 R2–R9 全部
    与 P1 同口径通过；required_scope_count / min_parallelism 也沿用 P1 推导
    （含任务8 的 worker 能力压薄：effective_limit / worker_capabilities）。
    """
    plan = dict(_disk_plan(
        batch_id, batch, requests, max_concurrency,
        effective_limit=effective_limit, worker_capabilities=worker_capabilities,
    ))
    plan["stage"] = "T4"
    return plan


def _t4_batch_interrupt_payload(
    state: ShrinkCoursewareState,
    plan: Dict[str, Any],
    requests: List[Dict[str, Any]],
    batch_id: str,
) -> Dict[str, Any]:
    """T4 验收批派发中断载荷（对齐 agent_nodes._interrupt_payload + P1 批扩展）。

    键集与 P1 ``_batch_interrupt_payload`` 一致，仅角色、prompt、context 与
    描述换成验收语义；``requests`` 逐单携带完整派发单，供 Antigravity / 人工
    逐 scope 只读复核后写回 ``.pd/acceptance/<sec>.json``。
    """
    artifacts: List[str] = []
    before_digests: Dict[str, Any] = {}
    for req in requests:
        for art in (req.get("artifacts") or []):
            if art not in artifacts:
                artifacts.append(art)
        for rel, digest in (req.get("before") or {}).items():
            before_digests[rel] = digest

    sections = [str(req.get("scope") or "") for req in requests]
    instructions = [
        f"[{req.get('scope')}] {req.get('instruction')}" for req in requests
    ]
    commands = [str(req.get("command") or "") for req in requests]
    expected_qids = {
        str(req.get("scope") or ""): list(
            (req.get("context") or {}).get("expected_qids") or []
        )
        for req in requests
    }
    return {
        "type": "AGENT_DISPATCH",
        "batch_id": batch_id,
        "dispatch_id": batch_id,
        "stage": plan.get("stage") or "T4",
        "role": "T4 独立教学验收批派发（fan-out）",
        "description": (
            f"T4 并行验收批派发：一批 {len(requests)} 个切片验收范围"
            f"（{', '.join(s for s in sections if s)}）。请以 acceptance-reviewer "
            f"身份按 prompts/acceptance_prompt.md 只读复核各自范围的学生可见产物，"
            f"出具 .pd/acceptance/<sec>.json 后恢复执行。"
        ),
        "artifacts": artifacts,
        "instruction": "\n\n".join(instructions),
        "command": " && ".join(c for c in commands if c),
        "prompt_file": "prompts/acceptance_prompt.md",
        "request_refs": [
            f"{REQUEST_DIR_REL}/{req.get('dispatch_id')}.json" for req in requests
        ],
        "before_digests": before_digests,
        "context": {"sections": sections, "expected_qids": expected_qids},
        "max_concurrency": plan.get("max_concurrency"),
        "min_parallelism": plan.get("min_parallelism"),
        "requests": [
            {
                "dispatch_id": req.get("dispatch_id"),
                "scope": req.get("scope"),
                "role": req.get("role"),
                "artifacts": list(req.get("artifacts") or []),
                "instruction": req.get("instruction"),
                "command": req.get("command"),
                "prompt_file": f"prompts/{req.get('prompt_file')}",
                "request_ref": f"{REQUEST_DIR_REL}/{req.get('dispatch_id')}.json",
                "before_digests": req.get("before") or {},
                "input_digest": req.get("input_digest"),
                "timeout_seconds": req.get("timeout_seconds"),
                "max_retries": req.get("max_retries"),
            }
            for req in requests
        ],
    }


def _load_scope_record(
    self_dir: str, sec: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """读取并解析 ``.pd/acceptance/<sec>.json``；返回 ``(记录, 错误原因)``。

    错误原因统一带机器可读前缀：``missing_artifacts:`` / ``parse_error:``。
    """
    rel = os.path.join(ACCEPTANCE_DIR_REL, f"{sec}.json")
    abs_path = os.path.join(self_dir, rel)
    if not os.path.isfile(abs_path):
        return None, f"missing_artifacts: 验收记录未生成: {rel}"
    try:
        with open(abs_path, "r", encoding="utf-8-sig") as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        try:
            with open(abs_path, "r", encoding="utf-8-sig") as f:
                raw_text = f.read()
            import re
            fixed = re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', raw_text)
            rec = json.loads(fixed)
        except Exception:
            return None, f"parse_error: 验收记录不可解析: {rel} ({exc})"
    if not isinstance(rec, dict):
        return None, f"parse_error: 验收记录根节点必须是 JSON 对象: {rel}"
    return rec, None


#: T4 批次 ID 前缀（创审分离比对时排除验收阶段自身的证据）
_T4_BATCH_PREFIX = "bat-t4"


def _reviewer_identity_violation(
    self_dir: str, req: Dict[str, Any]
) -> Tuple[bool, str]:
    """INV-2 创审分离校验：验收者不得与写作者同体（q_audit.md）。

    比对口径：本 T4 派发单的代理证据 worker 字段 vs 全量写作阶段证据
    （.pd/agent-calls/ 全量扫描，排除 bat-t4* 验收批次）的 worker 集合。
    任一交集 → 违规。无 T4 证据（无法判定验收者身份）时返回 (False, "")，
    由产物证据门禁（ok_unattested）另行处置。
    """
    dispatch_id = str(req.get("dispatch_id") or "")
    review_ev = agent_evidence.load_evidence(self_dir, dispatch_id)
    if review_ev is None:
        return False, ""
    reviewer = str(review_ev.get("worker") or "").strip()
    if not reviewer:
        return False, ""
    writers = {
        str(ev.get("worker") or "").strip()
        for ev in agent_evidence.scan_evidence(self_dir)
        if str(ev.get("worker") or "").strip()
        and not str(ev.get("batch_id") or "").startswith(_T4_BATCH_PREFIX)
    }
    if reviewer in writers:
        return True, (
            f"reviewer_identity_violation: 验收者与写作者同体（worker={reviewer}），"
            f"违反创审分离（T4 验收必须由未参与 T1/T2 写作的子 Agent 完成）"
        )
    return False, ""


def _evaluate_scope(
    self_dir: str,
    req: Dict[str, Any],
    gate_status: str,
    gate_error: Optional[str],
    attempts: Any,
    baseline: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """单 scope 验收判定（PARALLEL 与 MANUAL/disk 两路共用的统一口径）。

    先过派发门态（PARALLEL 的 ``DispatchResult.status`` 必须为 ``ok``；
    MANUAL/disk 的 ``_verify_requests_from_disk`` 产物必须存在且已变化），
    再加载并按基线校验 JSON。verdict 取值：

    - ``PASS``              —— 门态 ok 且 ``validate_acceptance_json`` 零问题
                               且 ``conclusion == "PASS"``；
    - ``REVISE``            —— 门态 ok 且结论 REVISE（教学判定需返修）；
    - ``NEEDS_EVIDENCE``    —— 门态 ok 且结论 NEEDS_EVIDENCE；
    - ``FAILED``            —— 缺失 / 未变化 / 不可解析 / schema 不符 /
                               证据绑定过期（错误串前缀即原因码）。

    返回 ``(result_entry, 记录或 None)``，``result_entry`` 形状固定为
    ``{dispatch_id, batch_id, scope, status, attempts, error, verdict, conclusion}``。
    """
    dispatch_id = str(req.get("dispatch_id") or "")
    scope = str(req.get("scope") or "")
    entry: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "batch_id": req.get("batch_id"),
        "scope": scope,
        "status": "failed",
        "attempts": attempts if attempts is not None else 0,
        "error": None,
        "verdict": "FAILED",
        "conclusion": "",
    }

    if gate_status != "ok":
        entry["error"] = f"{gate_status}: {gate_error or '派发门态未通过'}"
        # 产物证据链状态（ok_unattested / attested_mismatch）透传到 status，
        # 便于 dispatch_results 消费方与门禁审计直接识别断裂环节。
        if gate_status in ("ok_unattested", "attested_mismatch"):
            entry["status"] = gate_status
        return entry, None

    rec, load_error = _load_scope_record(self_dir, scope)
    if rec is None:
        entry["error"] = load_error
        return entry, None

    # 任务6：主控确定性盖章 scope 级复用绑定字段（不信 LLM 自填）
    _stamp_review_provenance(
        self_dir, scope, rec, str(baseline.get("chapter_digest") or "")
    )

    # INV-2 创审分离（q_audit.md）：验收者与写作者同体 → 验收无效
    violation, violation_detail = _reviewer_identity_violation(self_dir, req)
    if violation:
        entry["error"] = violation_detail
        entry["status"] = "reviewer_identity_violation"
        entry["reviewer_identity_violation"] = True
        entry["verdict"] = "FAILED"
        return entry, rec

    conclusion = str(rec.get("conclusion") or "").strip().upper()
    entry["conclusion"] = conclusion
    problems = evidence.validate_acceptance_json(
        rec,
        current_digest=baseline.get("content_digest"),
        student_pdf_sha=baseline.get("student_pdf_sha"),
        student_html_sha=baseline.get("student_html_sha"),
    )
    if problems:
        entry["error"] = "schema_error: " + "; ".join(str(p) for p in problems)
        return entry, rec
    if conclusion == "PASS":
        entry["status"] = "ok"
        entry["verdict"] = "PASS"
        entry["error"] = None
        return entry, rec
    if conclusion in ("REVISE", "NEEDS_EVIDENCE"):
        entry["verdict"] = conclusion
        entry["error"] = f"{scope} 验收结论 {conclusion}"
        return entry, rec
    entry["error"] = f"schema_error: 验收结论非法: {conclusion!r}"
    return entry, rec


def _plan_expected_total_qids(plan: Dict[str, Any]) -> List[str]:
    """从（状态态）派发计划恢复全批次期望 qid 并集。

    批次可能只是 ``expected_sections`` 的前缀（batch_window 限流），覆盖判定
    必须以全批次为基准，否则 fan-in 会漏判未派发 scope 的 qid。
    """
    qids_by_sec = plan.get("expected_qids") or {}
    total: List[str] = []
    for sec in (plan.get("expected_sections") or []):
        for qid in (qids_by_sec.get(sec) or []):
            if qid not in total:
                total.append(qid)
    if total:
        return total
    for req in (plan.get("requests_full") or []):
        for qid in ((req.get("context") or {}).get("expected_qids") or []):
            if qid not in total:
                total.append(qid)
    return total


def _aggregate_conclusion(
    entries: List[Dict[str, Any]],
    reviewed_qids: List[str],
    expected_total_qids: List[str],
    issues: List[Dict[str, Any]],
) -> str:
    """确定性 fan-in 汇总结论（无 LLM，纯函数，可重复推导）。

    - ``PASS``          —— 全部 scope verdict==PASS 且 reviewed_qids 覆盖
                           期望并集且无 open issue；
    - ``NEEDS_EVIDENCE``—— 任一 scope 判定 FAILED（缺失/不可解析/schema 不符）或
                           覆盖不足（证据不可用）；
    - ``REVISE``        —— 其余情况（存在教学判定 REVISE / NEEDS_EVIDENCE）。
    """
    if not entries:
        return "NEEDS_EVIDENCE"
    verdicts = [str(e.get("verdict") or "FAILED") for e in entries]
    covered = (
        all(q in reviewed_qids for q in expected_total_qids)
        if expected_total_qids else True
    )
    open_ids = [
        str(i.get("id") or "?") for i in issues if str(i.get("status") or "") == "open"
    ]
    if "FAILED" in verdicts:
        return "NEEDS_EVIDENCE"
    if not covered:
        return "NEEDS_EVIDENCE"
    if all(v == "PASS" for v in verdicts) and not open_ids:
        return "PASS"
    if "NEEDS_EVIDENCE" in verdicts:
        return "NEEDS_EVIDENCE"
    return "REVISE"


def _write_chapter_aggregate(
    self_dir: str,
    baseline: Dict[str, Any],
    evaluated: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]],
    expected_total_qids: List[str],
) -> Dict[str, Any]:
    """聚合写出 ``.pd/acceptance/chapter.json``（fan-in 唯一确定性产物）。

    字段与 prompts/acceptance_prompt.md 的 schema 逐字对齐（即
    ``evidence._ACCEPTANCE_REQUIRED`` 的 10 个键，不多不少）：

    - ``schema_version``      —— ``evidence.SCHEMA_VERSION``（prompt 期望值）；
    - ``scope``               —— 固定 ``"chapter"``；
    - ``reviewed_qids``       —— 各 scope 可解析记录 reviewed_qids 的排序并集；
    - ``content_digest``      —— 派发时固化的证据基线 digest；
    - ``student_pdf_sha256``  —— 基线 sha（fmt 不含 pdf 时为 ``None``，与
                                 test_runtime.write_acceptance 同口径）；
    - ``conclusion``          —— :func:`_aggregate_conclusion` 的确定性结果；
    - ``reviewer_role``       —— fan-in 署名（§12.3 固定角色）；
    - ``stage1_record``       —— 各 scope 阶段一记录按 scope 序 ``" || "`` 拼接；
    - ``stage2_record``       —— 各 scope 阶段二记录按 scope 序 ``" || "`` 拼接；
    - ``issues``              —— 各 scope issues 串联后按 id 去重。
    """
    reviewed: List[str] = []
    issues: List[Dict[str, Any]] = []
    seen_issue_ids = set()
    stage1_parts: List[str] = []
    stage2_parts: List[str] = []
    for scope, _entry, rec in sorted(evaluated, key=lambda item: item[0]):
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
        s1 = str(rec.get("stage1_record") or "").strip()
        s2 = str(rec.get("stage2_record") or "").strip()
        if s1:
            stage1_parts.append(f"[{scope}] {s1}")
        if s2:
            stage2_parts.append(f"[{scope}] {s2}")

    reviewed_qids = sorted(reviewed)
    entries = [e for _s, e, _r in evaluated]
    aggregate = {
        "schema_version": evidence.SCHEMA_VERSION,
        "scope": "chapter",
        "reviewed_qids": reviewed_qids,
        "content_digest": baseline.get("content_digest"),
        "student_pdf_sha256": baseline.get("student_pdf_sha"),
        "conclusion": _aggregate_conclusion(
            entries, reviewed_qids, expected_total_qids, issues
        ),
        "reviewer_role": AGGREGATE_REVIEWER_ROLE,
        "stage1_record": " || ".join(stage1_parts) or _EMPTY_STAGE_RECORD,
        "stage2_record": " || ".join(stage2_parts) or _EMPTY_STAGE_RECORD,
        "issues": issues,
    }
    _write_json_atomic(os.path.join(self_dir, ACCEPTANCE_CHAPTER_REL), aggregate)
    return aggregate


# ---------------------------------------------------------------------------
# 节点 1：验收批次规划
# ---------------------------------------------------------------------------

def _archived_history(state: ShrinkCoursewareState) -> List[Dict[str, Any]]:
    """把 state 里上一轮派发计划归档进 dispatch_history（无计划时原样带回）。"""
    dispatch_history = list(state.get("dispatch_history") or [])
    previous_plan = state.get("dispatch_plan") or {}
    if previous_plan.get("batch_id"):
        dispatch_history.append(
            {
                "batch_id": previous_plan.get("batch_id"),
                "sections": list(previous_plan.get("sections") or []),
                "metrics": dict(state.get("dispatch_metrics") or {}),
            }
        )
    return dispatch_history


def node_t4_plan_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """规划 T4 验收 fan-out 批次（每个 scope 一个独立验收专员）。

    步骤：qid 抽取 → 批次切分（P1 同款配置）→ 证据基线固化 → per-scope 派发单
    → 磁盘派发计划 → ``check_dispatch`` 门禁 → 通过则进入
    ``T4_REVIEW_PENDING``。门禁不通过即 HALTED/HALT（与 P1 规划节点一致）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    expected_sections = list(state.get("expected_sections") or [])

    if not expected_sections:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T4 验收派发缺少 expected_sections（无验收范围）",
            "history": history,
        }

    qids_by_sec = {
        sec: _extract_section_qids(self_dir, sec) for sec in expected_sections
    }
    expected_total_qids = _expected_total_qids(state, self_dir, qids_by_sec)

    cfg = _load_dispatch_config(self_dir, str(state.get("run_scope") or "").strip())
    # 任务6：scope 级复用——内容未变且旧结论 PASS 的节不重复派发
    # （local-review 只收 changed scopes）；章节级上下文（依赖图/编号拓扑/
    # main.tex 装配/跨节引用）变更时全部升级 chapter-review。
    chapter_digest = _chapter_context_digest(self_dir, expected_sections)
    baseline = _evidence_baseline(state, self_dir)
    baseline["chapter_digest"] = chapter_digest
    reused_scopes, reuse_reasons = _reusable_scopes(
        self_dir, list(expected_sections), chapter_digest
    )
    batch = [sec for sec in expected_sections if sec not in reused_scopes]
    if reused_scopes:
        history.append(
            "T4 Review Dispatch: scope 级复用 %d/%d 节（%s）——内容与章节级"
            "上下文均未变更且旧结论 PASS，不重复派发"
            % (len(reused_scopes), len(expected_sections),
               ", ".join(sorted(reused_scopes)))
        )
        if reuse_reasons:
            history.append(
                "T4 Review Dispatch: 需重审 scope 原因：%s"
                % "; ".join(f"{sec}={reason}"
                            for sec, reason in sorted(reuse_reasons.items()))
            )
    batch_id = f"bat-t4-{uuid.uuid4().hex[:8]}"

    requests: List[Dict[str, Any]] = []
    for sec in batch:
        qids_sec = list(qids_by_sec.get(sec) or [])
        role_name = "开篇验收专员" if sec == "sec0" else f"{sec} 切片独立教学验收专员"
        req = build_request(
            state,
            stage=f"T4_REVIEW_{sec.upper()}",
            role=role_name,
            prompt_file="acceptance_prompt.md",
            artifacts=[
                os.path.join(ACCEPTANCE_DIR_REL, f"{sec}.json"),
                os.path.join(ACCEPTANCE_DIR_REL, f"{sec}-source-vs-output.md"),
            ],
            instruction=(
                f"请以 acceptance-reviewer 身份只读复核 {sec} 的学生可见产物"
                f"（{sec}.tex 及 .pd/review/ 审读包），覆盖全部 {len(qids_sec)} 个 "
                f"qid（{', '.join(qids_sec)}），出具 "
                f".pd/acceptance/{sec}.json 验收记录（PASS/REVISE/NEEDS_EVIDENCE）。"
                f" 同时按 F3 来源对照职责直接阅读 Phase 0 冻结的原文片段"
                f"（.pd/source-manifest.json / source-inventory.json 定位），"
                f"产出 .pd/acceptance/{sec}-source-vs-output.md：逐行列出 "
                f"source_id、输出位置、动作（PRESERVE/EXPLAIN/BRIDGE/EXTEND）、"
                f"是否改变命题、是否新增对象与结论；无 source 锚点或 faithful "
                f"模式未批准 EXTEND 一律判 REVISE。"
            ),
            command=(
                "只读复核（禁止执行 deliver.py）：阅读冻结产物，将结论与 context 中的 "
                "content_digest / student_pdf_sha256 / student_html_sha256 绑定后写入 "
                ".pd/acceptance/<sec>.json"
            ),
            context={
                "run_id": state.get("run_id"),
                "scope": sec,
                "expected_qids": qids_sec,
                "content_digest": baseline["content_digest"],
                "student_pdf_sha256": baseline["student_pdf_sha"],
                "student_html_sha256": baseline["student_html_sha"],
                "output_format": baseline["output_format"],
            },
            scope=sec,
            batch_id=batch_id,
            timeout_seconds=cfg["timeout_seconds"],
            max_retries=cfg["max_retries"],
            retry_class="transient",
        )
        # T4 验收记录是本批次出具的独立审计产物，before 置空确保重推导与对账不误判为 no_change
        req["before"] = {art: None for art in req.get("artifacts", [])}
        req["request_ref"] = f"{REQUEST_DIR_REL}/{req['dispatch_id']}.json"
        req["success_stage"] = "T4_ACCEPTANCE_PENDING"
        req["success_history"] = f"T4: {sec} 验收记录已由派发池完成"
        req_ref = write_request_file(self_dir, req)
        requests.append(req)
        history.append(
            f"T4 Review Dispatch: {sec} 已纳入验收批次 {batch_id}"
            f"（{len(qids_sec)} 个 qid，派发单 {req_ref}）"
        )

    if not batch:
        # 任务6：全部 scope 复用（本轮零派发）——不写派发计划、不过派发门禁
        # （check_dispatch 的 R3 会以「requests 为空」判 UNDER_DISPATCH，那是
        # 面向「必须派发」场景的规则），直接带复用证据进 fan-in 聚合：
        # 无改动重跑不调 LLM、不编译（audit §14 验收指标）。
        state_plan = {
            "batch_id": batch_id,
            "stage": "T4",
            "sections": [],
            "requests": [],
            "max_concurrency": max(1, int(cfg["max_concurrency"])),
            "required_scope_count": 0,
            "reused_scopes": sorted(reused_scopes),
            "chapter_digest": chapter_digest,
            "expected_sections": expected_sections,
            "expected_qids": {
                sec: list(qids_by_sec.get(sec) or [])
                for sec in expected_sections
            },
            "evidence_baseline": baseline,
            "requests_full": [],
            "dispatch_config": cfg,
        }
        history.append(
            f"T4 Review Dispatch: 批次 {batch_id} 全部 "
            f"{len(reused_scopes)} 个 scope 复用旧验收记录，本轮零派发"
            f"（期望 qid {len(expected_total_qids)} 个）"
        )
        return {
            "dispatch_plan": state_plan,
            "batch_id": batch_id,
            "section_statuses": dict(state.get("section_statuses") or {}),
            "active_section": None,
            "pending_dispatch": None,
            "stage": "T4_REVIEW_RUNNING",
            "dispatch_mode": "reused",
            "routing_action": "PROCEED",
            "dispatch_metrics": {
                "submitted": 0,
                "completed": 0,
                "ok": 0,
                "failed": 0,
                "retried": 0,
                "max_in_flight": 0,
                "deferred": 0,
                "reused": len(reused_scopes),
            },
            # 与正常路径同口径归档上一轮计划（复用轮也要留可验证批次史）
            "dispatch_history": _archived_history(state),
            "agent_dispatch_profile": cfg["profile"],
            "retry_count": 0,
            "history": history,
        }

    # 建池先于派发计划落盘：真实并行度受 worker 能力声明压薄
    # （任务8，= min(档位并发, 声明的 max_concurrency)），与 T2 同口径。
    _pool = _get_pool(
        batch_id, cfg["max_concurrency"], cfg["timeout_seconds"], cfg["max_retries"])
    _effective_limit = _pool.effective_concurrency if _pool is not None else None
    _worker_caps = _pool.worker_capabilities if _pool is not None else {}

    plan_disk = _t4_disk_plan(
        batch_id, batch, requests, cfg["max_concurrency"],
        effective_limit=_effective_limit, worker_capabilities=_worker_caps)
    _write_json_atomic(os.path.join(self_dir, DISPATCH_PLAN_REL), plan_disk)

    report = ScriptRunner(self_dir).run_gate_script(
        gate_name="check_dispatch",
        script_name="check_dispatch.py",
        args=[self_dir],
        report_rel_path=DISPATCH_REPORT_REL,
        run_id=state.get("run_id", ""),
    )
    history.append(
        "T4 Review Dispatch: check_dispatch 门禁状态 %s（退出码 %s，报告 %s）"
        % (report.get("status"), report.get("exit_code"), report.get("report_path"))
    )
    if report.get("status") != "PASS":
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"派发门禁未通过: {_gate_error_codes(report)}",
            "history": history,
        }

    dispatch_history = _archived_history(state)

    state_plan = dict(plan_disk)
    state_plan["expected_sections"] = expected_sections
    state_plan["expected_qids"] = {
        sec: list(qids_by_sec.get(sec) or []) for sec in expected_sections
    }
    state_plan["evidence_baseline"] = baseline
    state_plan["requests_full"] = requests
    state_plan["dispatch_config"] = cfg
    # 任务6：复用留痕（join 节点按 chapter_digest 对账旧 PASS 绑定）
    state_plan["reused_scopes"] = sorted(reused_scopes)
    state_plan["chapter_digest"] = chapter_digest

    history.append(
        "T4 Review Dispatch: 规划验收批次 %s（%d/%d 节，档位 %s，并发 %d，窗口 %d，"
        "超时 %ds，重试 %d）"
        % (
            batch_id,
            len(batch),
            len(expected_sections),
            cfg["profile"],
            cfg["max_concurrency"],
            cfg["batch_window"],
            cfg["timeout_seconds"],
            cfg["max_retries"],
        )
    )
    history.append(
        f"T4 Review Dispatch: 批次 {batch_id} 门禁通过，"
        f"{len(batch)} 个切片验收范围进入派发（期望 qid {len(expected_total_qids)} 个）"
    )
    return {
        "dispatch_plan": state_plan,
        "batch_id": batch_id,
        "section_statuses": dict(state.get("section_statuses") or {}),
        "active_section": None,
        "pending_dispatch": None,
        "stage": "T4_REVIEW_PENDING",
        "routing_action": "PROCEED",
        "dispatch_metrics": {
            "submitted": len(batch),
            "completed": 0,
            "ok": 0,
            "failed": 0,
            "retried": 0,
            "max_in_flight": 0,
            "deferred": 0,
            "reused": len(reused_scopes),
        },
        "dispatch_history": dispatch_history,
        "agent_dispatch_profile": cfg["profile"],
        "retry_count": 0,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 2：验收批次派发
# ---------------------------------------------------------------------------

def node_t4_dispatch_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """把验收批次提交到共享进程级 WorkerPool（P1 同款语义）。

    注册了认知 worker 时请求被真正执行（``deferred == 0`` → parallel）；没有
    worker 时全部记为 deferred，收尾节点据此转为 manual 中断（生产默认）。
    池对象只活在模块级单例中，**绝不**进入状态 patch。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")

    if not plan or plan.get("batch_id") != batch_id:
        history.append(f"T4 Review Dispatch: 验收批次 {batch_id} 计划失效（stale plan）")
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

    cfg = plan.get("dispatch_config") or _load_dispatch_config(self_dir)
    pool = _get_pool(
        batch_id,
        cfg["max_concurrency"],
        cfg["timeout_seconds"],
        cfg["max_retries"],
    )
    for req in requests:
        pool.submit(req)
    # 任务4：dispatch 节点只做校验/提交/记录，绝不调 join——
    # join 是收尾节点（fan-in）的专属职责，每 batch 只允许一次。
    # 旧实现在此 join(timeout=最长请求超时+5s≈905s) 会阻塞到全部收尾，
    # 使 parallel 模式退化为串行等待且近 15 分钟无阶段反馈（q_audit §12.2）。
    # deferred 计数在 submit 时即已确定（无执行器的请求不起线程），
    # 因此判定 dispatch_mode 无需等待。
    stats = pool.stats()
    deferred = int(stats.get("deferred", 0))
    mode = "manual" if deferred > 0 else "parallel"
    history.append(
        "T4 Review Dispatch: 批次 %s 已提交 %d 个验收请求（模式 %s，deferred=%d）"
        % (batch_id, len(requests), mode, deferred)
    )
    return {
        "stage": "T4_REVIEW_RUNNING",
        "dispatch_mode": mode,
        "dispatch_metrics": stats,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 3：验收批次收尾 + 确定性 fan-in 聚合
# ---------------------------------------------------------------------------

def _t4_join_review_impl(state: ShrinkCoursewareState) -> Dict[str, Any]:
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")
    requests = list(plan.get("requests_full") or [])
    results = dict(state.get("dispatch_results") or {})
    statuses = dict(state.get("section_statuses") or {})

    if not plan or plan.get("batch_id") != batch_id:
        history.append(f"T4 Review Dispatch: 验收批次 {batch_id} 计划失效（stale plan）")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划与当前批次不符（stale plan）",
            "history": history,
        }
    # 任务6：requests_full 为空 = plan 节点判定全部 scope 复用（零派发），
    # 携带 reused_scopes 证据进 fan-in；两者皆空才是损坏的计划。
    reused_scopes = [str(s) for s in (plan.get("reused_scopes") or [])]
    if not requests and not reused_scopes:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    baseline = dict(plan.get("evidence_baseline") or {})
    if not baseline.get("content_digest"):
        baseline = _evidence_baseline(state, self_dir)
    expected_total_qids = _plan_expected_total_qids(plan)
    # 任务6：章节级上下文摘要（plan 时固化，join 时对账旧 PASS 绑定）
    chapter_digest = str(
        plan.get("chapter_digest") or baseline.get("chapter_digest") or ""
    )
    baseline["chapter_digest"] = chapter_digest

    if not requests:
        # ---- 0) 全部复用（零派发）：无池可 join，直接确定性 fan-in ---------
        # 旧 PASS 按三重绑定复用过聚合；若期间记录被删/被改，聚合结论降级为
        # NEEDS_EVIDENCE，由 verify 节点路由重派（自愈，不静默放行）。
        history.append(
            f"T4 Review Dispatch: 批次 {batch_id} 全部 "
            f"{len(reused_scopes)} 个 scope 复用旧验收记录，零派发直接聚合"
        )
        evaluated: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]] = []
        _fold_reused_scopes(
            self_dir, list(state.get("expected_sections") or []),
            set(), chapter_digest, batch_id, evaluated, history,
        )
        metrics = {
            "submitted": 0, "completed": 0, "ok": len(evaluated),
            "failed": 0, "retried": 0, "max_in_flight": 0,
            "deferred": 0, "reused": len(reused_scopes), "orphans": 0,
        }
        aggregate = _write_chapter_aggregate(
            self_dir, baseline, evaluated, expected_total_qids
        )
        history.append(
            f"T4 Review Dispatch: 批次 {batch_id} 验收汇总结论 "
            f"{aggregate['conclusion']}（reviewed_qids "
            f"{len(aggregate['reviewed_qids'])}/{len(expected_total_qids)}，"
            f"全部复用零派发）"
        )
        return {
            "stage": "T4_ACCEPTANCE_PENDING",
            "routing_action": "PROCEED",
            "dispatch_results": {},
            "dispatch_metrics": metrics,
            "section_statuses": statuses,
            "history": history,
        }

    pool = _live_pool(batch_id)
    join_timeout = _join_timeout(requests)
    raw_results: Dict[str, Any] = {}
    stats: Dict[str, Any] = {}
    if pool is not None:
        raw_results = pool.join(timeout=join_timeout)
        stats = pool.stats()
        deferred = int(stats.get("deferred", 0))
        has_all = len(raw_results) >= len(requests)
    else:
        deferred = 1
        has_all = False

    # (scope, entry, record) 三元组：record 仅在 JSON 可解析时非 None
    evaluated: List[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]] = []

    parallel_mode = pool is not None and deferred == 0 and has_all
    if parallel_mode:
        # ---- 1) PARALLEL：活池真实结果 ----------------------------------
        for req in requests:
            scope = str(req.get("scope") or "")
            dispatch_id = str(req.get("dispatch_id") or "")
            res = raw_results.get(dispatch_id)
            if res is None:
                entry, rec = _evaluate_scope(
                    self_dir, req, "missing_result", "派发池未返回该请求的结果", 0, baseline
                )
            else:
                entry, rec = _evaluate_scope(
                    self_dir,
                    req,
                    str(_result_field(res, "status", "unknown") or "unknown"),
                    _result_field(res, "error"),
                    _result_field(res, "attempts", 0),
                    baseline,
                )
                output = _shrink_output(_result_field(res, "output"))
                if output is not None:
                    entry["output"] = output
            results[dispatch_id] = entry
            evaluated.append((scope, entry, rec))
        metrics = stats
        pass_count = sum(1 for _s, e, _r in evaluated if e["verdict"] == "PASS")
        history.append(
            "T4 Review Dispatch: 批次 %s 并行收尾（PASS=%d，非 PASS=%d）"
            % (batch_id, pass_count, len(evaluated) - pass_count)
        )
    else:
        # ---- 2)/3) MANUAL：中断一次 → 磁盘重推导 ------------------------
        # 命中重推导（不 interrupt）的两种情形：
        #  a) manual_interrupted_batch==batch_id：人工/Antigravity 批次已中断过；
        #  b) dispatch_mode=="parallel"：自动并行批次执行中进程崩溃→重启→resume，
        #     活池已随进程消失（_live_pool 为 None），但 checkpoint 固化了她曾
        #     并行派发——此时同样只验证不中断：已写完的验收记录按磁盘 sha256
        #     判 ok，未写完的判 no_change/missing 由 verify 节点路由重派，
        #     绝不退化为人工 interrupt（audit_agent.md §5 恢复语义）。
        if (state.get("manual_interrupted_batch") == batch_id
                or state.get("dispatch_mode") == "parallel"):
            history.append(
                f"T4 Review Dispatch: 批次 {batch_id} 已派发过（人工中断恢复或并行批次进程重启），"
                f"从磁盘重推导验收结果（不重复 interrupt）"
            )
            ok_scopes, failures, entries = _verify_requests_from_disk(
                self_dir, requests,
                allow_unattested=_plan_allow_unattested(plan, self_dir))
            for req in requests:
                dispatch_id = str(req.get("dispatch_id") or "")
                scope = str(req.get("scope") or "")
                disk = entries.get(dispatch_id) or {}
                entry, rec = _evaluate_scope(
                    self_dir,
                    req,
                    str(disk.get("status") or "missing_artifacts"),
                    disk.get("error"),
                    disk.get("attempts", 1),
                    baseline,
                )
                entry["verified_from"] = "disk"
                if disk.get("verified_via"):
                    entry["verified_via"] = disk["verified_via"]
                results[dispatch_id] = entry
                evaluated.append((scope, entry, rec))
            metrics = _disk_metrics(requests, ok_scopes, failures)
            # 证据链断裂计数（q_audit.md）：主线程直写嫌疑 / 证据与盘面不符
            evidence_error = sum(
                1 for e in entries.values()
                if str(e.get("status")) in ("ok_unattested", "attested_mismatch")
            )
            if evidence_error:
                metrics["evidence_error"] = evidence_error
            history.append(
                "T4 Review Dispatch: 批次 %s 磁盘重推导完成（ok=%d，失败=%d）"
                % (batch_id, len(ok_scopes), len(failures))
            )
        else:
            payload = _t4_batch_interrupt_payload(state, plan, requests, batch_id)
            history.append(
                f"Agent[T4]: 挂起等待人工 / Antigravity 整批验收 "
                f"{', '.join(str(r.get('scope')) for r in requests)}"
            )
            response = interrupt(payload)
            if not isinstance(response, dict):
                response = {}
            # 恢复侧防伪造：中断响应必须经 qchain_dispatch（host-subagent-v1）
            # 回传；旧 checkpoint 无该字段（None）不拦截。
            violation = agent_evidence.dispatch_protocol_violation(response)
            if violation:
                history.append(f"Agent[T4]: {violation}")
                return {
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": violation,
                    "dispatch_results": results,
                    "history": history,
                }
            files = response.get("files") or {}
            written: List[str] = []
            for req in requests:
                written.extend(_write_worker_files(self_dir, req, files))
            if written:
                history.append(
                    f"Agent[T4]: 已从中断响应落盘验收记录 {', '.join(sorted(set(written)))}"
                )
            return {
                "manual_interrupted_batch": batch_id,
                "dispatch_mode": "manual",
                "stage": "T4_REVIEW_RUNNING",
                "dispatch_results": results,
                "history": history,
            }

    # ---- 硬门禁：实际并发度不得低于计划值（audit_agent.md §6 / P1.4） -----
    # 仅 parallel 模式校验：manual 路径是人工/Antigravity 批次，已由
    # dispatch_mode="manual" 显式标记，不属于「伪装并行」。
    if parallel_mode:
        min_parallel = int(plan.get("min_parallelism") or 1)
        actual = int((metrics or {}).get("max_in_flight", 0) or 0)
        if actual < min_parallel:
            history.append(
                "T4 Review Dispatch: 批次 %s 实际并发度 %d 低于计划值 %d，判定伪装完成"
                % (batch_id, actual, min_parallel))
            return {
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": (
                    f"实际并发度 {actual} 低于计划值 {min_parallel}，"
                    "禁止伪装完成（audit_agent.md §6）"),
                "dispatch_results": results,
                "dispatch_metrics": metrics,
                "history": history,
            }

    # ---- INV-2 创审分离注记（q_audit.md）：验收者与写作者同体即验收无效 ----
    for _scope, entry, _rec in evaluated:
        err = str(entry.get("error") or "")
        if err.startswith("reviewer_identity_violation"):
            history.append(
                f"T4 Review Dispatch: {_scope} 验收者与写作者同体，违反创审分离"
                f"（{err}）"
            )

    # ---- 4) 确定性 fan-in：聚合 chapter.json ---------------------------
    # 任务6：未本轮派发的 scope 按三重绑定（scope_content_digest +
    # chapter_digest + review_rule_version）复用旧 PASS，一并纳入聚合。
    # 替代旧的全局 content_digest 校验——任一其他节变更不再顺带作废本节
    # 记录（local-review 只收 changed scopes）。
    requested_scopes = {str(req.get("scope") or "") for req in requests}
    _fold_reused_scopes(
        self_dir, list(state.get("expected_sections") or []),
        requested_scopes, chapter_digest, batch_id, evaluated, history,
    )

    aggregate = _write_chapter_aggregate(
        self_dir, baseline, evaluated, expected_total_qids
    )
    detail = "; ".join(
        f"{scope}={entry['verdict']}"
        + (f"({entry['error']})" if entry.get("error") else "")
        for scope, entry, _rec in evaluated
    )
    history.append(
        f"T4 Review Dispatch: 批次 {batch_id} 验收汇总结论 {aggregate['conclusion']}"
        f"（reviewed_qids {len(aggregate['reviewed_qids'])}/"
        f"{len(expected_total_qids)}，issues {len(aggregate['issues'])} 项）"
        f"；逐 scope：{detail or '无'}"
    )
    # 任务6：复用计数随收尾度量一并落账（plan/join 两处同源）
    metrics = dict(metrics or {})
    metrics["reused"] = len(reused_scopes)
    return {
        "stage": "T4_ACCEPTANCE_PENDING",
        "routing_action": "PROCEED",
        "dispatch_results": results,
        "dispatch_metrics": metrics,
        "section_statuses": statuses,
        "history": history,
    }


def node_t4_join_review(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4 验收收尾：判定 per-scope 验收记录并聚合 chapter.json。

    成功与聚合失败都返回 ``T4_ACCEPTANCE_PENDING`` / ``PROCEED``：是否返修、
    重派或放行由 ``node_t4_verify_acceptance`` 唯一决定（``acceptance_rounds``
    <= 3 熔断是该流程唯一的循环边界）。仅未预期异常降级为 HALTED/HALT；
    langgraph 的中断异常原样上抛。
    """
    try:
        return _t4_join_review_impl(state)
    except Exception as exc:  # noqa: BLE001 - 统一降级 HALTED 交人工接管
        if _is_interrupt_exception(exc):
            raise
        history = list(state.get("history", []))
        history.append(
            f"T4 Review Dispatch: 验收收尾异常 {type(exc).__name__}: {exc}"
        )
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"T4 验收收尾异常: {type(exc).__name__}: {exc}",
            "history": history,
        }
