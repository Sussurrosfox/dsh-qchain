"""semantic_nodes.py — §6 语义审读轮（Writer → Checker → Gate）。

审计 §6：build 之前必须增加 Writer -> Checker -> Gate。语义审读员
（semantic-checker）与写作者严格分离——审读员只读一个小节候选并返回
结构化问题 JSON（`.pd/semantic/<sec>.json`），**绝不修改候选**。

硬前置（audit §6）：审读结果是串行提交（submit）的硬闸门——
  * 有 blocking issue（conclusion=REVISE）→ 不得提交，只重派该 scope
    的写作者返修，并记录 parent_dispatch_id；
  * 缺审读结果 / digest 过期 / schema 非法 → 不得提交，主控补派审读
    （semantic_repair_target），严禁用旧文件静默替代；
  * NEEDS_EVIDENCE → 同样阻塞提交（证据不足不得放行）。

三个节点（与 T2 写作轮同构的 fan-out/fan-in）：
    t2_plan_check → t2_dispatch_check → t2_join_check → submit

派发计划写在独立的 `.pd/dispatch-plan-semantic.json`（`--plan` 传给
check_dispatch 门禁），不覆盖写作轮计划，保留两轮审计轨迹。
"""

import json
import os
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
from . import agent_evidence
from .section_batch_nodes import (
    _disk_plan,
    _get_pool,
    _join_timeout,
    _live_pool,
    _load_dispatch_config,
    _plan_allow_unattested,
    _result_field,
    _shrink_output,
    _write_json_atomic,
)

#: 语义审读结果目录（每 scope 一个 JSON，worker 唯一可写路径）
CHECK_DIR_REL = ".pd/semantic"
#: 语义审读轮派发计划（独立于写作轮 .pd/dispatch-plan.json）
CHECK_PLAN_REL = ".pd/dispatch-plan-semantic.json"
#: 语义审读轮门禁报告
CHECK_REPORT_REL = ".pd/reports/check_semantic.json"
#: 写作轮派发计划（只读引用，用于解析 parent_dispatch_id）
AUTHOR_PLAN_REL = ".pd/dispatch-plan.json"

SEMANTIC_PROMPT_FILE = "semantic_check_prompt.md"
SEMANTIC_SCHEMA_VERSION = 1
SEMANTIC_CONCLUSIONS = ("PASS", "REVISE", "NEEDS_EVIDENCE")
#: 语义审读轮累计上限（manual 模式缺结果时防无限 interrupt 熔断）
MAX_SEMANTIC_ROUNDS = 3
#: 判为「必须重派该 scope 写作者返修」的失败态
REVISE_STATUSES = ("revise",)

#: 审读失败态 → 后续路由：
#   missing_result / invalid_result / stale_digest → 补派审读（semantic_repair_target）
#   revise / needs_evidence                   → 重派写作者返修（author repair）
REDISPATCH_CHECK_STATUSES = ("missing_result", "invalid_result", "stale_digest")


def _semantic_path(self_dir: str, sec: str) -> str:
    return os.path.join(self_dir, CHECK_DIR_REL, f"{sec}.json")


def _section_digest(self_dir: str, sec: str) -> Optional[str]:
    """候选切片 sha256（审读绑定的对象；缺失返回 None）。"""
    import hashlib

    path = os.path.join(self_dir, f"{sec}.tex")
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _checker_role(sec: str) -> str:
    if sec == "sec0":
        return "开篇引入语义审读专员"
    return f"{sec} 语义审读专员"


def _section_text(self_dir: str, sec: str, limit: int = 6000) -> str:
    """任务包内联的候选切片摘要（audit §12.7：prompt 只发本 scope 摘要）。"""
    path = os.path.join(self_dir, f"{sec}.tex")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n% …（候选过长，已截断；审读以磁盘全文为准）"
    return text


def load_checker_result(self_dir: str, sec: str) -> Optional[Dict[str, Any]]:
    path = _semantic_path(self_dir, sec)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: scripts 目录（用于复用 check_semantic.py 的打乱血缘台账口径）
_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts")


def _shuffle_lineage_ok(self_dir: str, record_digest: str,
                        current_digest: str) -> bool:
    """打乱血缘豁免（与 check_semantic.py 同口径）。

    shuffle_options.py 的选项随机重排是语义中性确定性变换；审读记录
    绑定打乱前候选摘要，`.pd/shuffle-map.json` 血缘台账证明当前切片
    是"被审读候选 + 唯一打乱"，此时不得判 stale（否则每次 shuffle
    之后都会全量重派审读员）。
    """
    if not record_digest or not current_digest:
        return False
    try:
        import sys
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        import check_semantic
        edges = check_semantic.load_shuffle_transitions(self_dir)
        return check_semantic.shuffle_bound(record_digest, current_digest, edges)
    except Exception:
        return False


def evaluate_checker_result(
    self_dir: str,
    sec: str,
    expected_digest: Optional[str],
) -> Tuple[str, str]:
    """审读结果求值 → (status, detail)。

    status:
      ok              通过（无 blocking issue）
      revise          有 blocking issue → 重派写作者返修
      needs_evidence  证据不足 → 阻塞提交
      missing_result  结果文件缺失
      invalid_result  schema/scope 非法
      stale_digest     绑定的 section_digest 与当前候选不一致
    """
    data = load_checker_result(self_dir, sec)
    if data is None:
        return "missing_result", "语义审读结果缺失或不是合法 JSON"
    if int(data.get("schema_version") or 0) != SEMANTIC_SCHEMA_VERSION:
        return "invalid_result", (
            f"语义审读结果 schema_version={data.get('schema_version')}，"
            f"期望 {SEMANTIC_SCHEMA_VERSION}")
    if str(data.get("scope") or "") != sec:
        return "invalid_result", (
            f"语义审读结果 scope={data.get('scope')!r} 与本节 {sec!r} 不符")
    if expected_digest and str(data.get("section_digest") or "") != expected_digest:
        # 打乱血缘豁免：候选经确定性选项重排后摘要必变，血缘台账证明
        # 当前切片源自被审读候选（shuffle 是语义中性变换）
        if _shuffle_lineage_ok(self_dir, str(data.get("section_digest") or ""),
                               expected_digest):
            return "ok", "打乱血缘有效（候选经确定性选项重排）"
        return "stale_digest", (
            "语义审读绑定的候选摘要与当前切片不一致"
            "（候选已变更，旧审读结果不得复用）")
    conclusion = str(data.get("conclusion") or "")
    if conclusion not in SEMANTIC_CONCLUSIONS:
        return "invalid_result", f"语义审读结论 {conclusion!r} 不在合法集合内"
    issues = data.get("issues")
    if issues is not None and not isinstance(issues, list):
        return "invalid_result", "语义审读 issues 必须是列表"
    blocking = [
        i for i in (issues or [])
        if isinstance(i, dict) and str(i.get("severity") or "") == "blocking"
    ]
    if conclusion == "REVISE" or blocking:
        detail = "; ".join(
            f"{i.get('code')}: {str(i.get('claim') or '')[:60]}"
            for i in blocking) or "审读结论为 REVISE"
        return "revise", detail
    if conclusion == "NEEDS_EVIDENCE":
        return "needs_evidence", "审读员判定证据不足（NEEDS_EVIDENCE）"
    return "ok", ""


def needs_check(self_dir: str, sec: str) -> bool:
    """该 scope 是否需要（重新）派发审读员。"""
    digest = _section_digest(self_dir, sec)
    if digest is None:
        return True
    status, _ = evaluate_checker_result(self_dir, sec, digest)
    return status != "ok"


def semantic_gate_verdict(
    self_dir: str,
    sec: str,
) -> Tuple[str, str]:
    """submit 硬闸门（audit §6）：返回 (verdict, reason)。

    verdict: PASS / REVISE / BLOCK——BLOCK 表示缺结果/摘要过期/schema
    非法，主控必须补派审读而非放行。
    """
    digest = _section_digest(self_dir, sec)
    if digest is None:
        return "BLOCK", f"切片 {sec}.tex 缺失，无法进行语义审读绑定"
    status, detail = evaluate_checker_result(self_dir, sec, digest)
    if status == "ok":
        return "PASS", ""
    if status in REDISPATCH_CHECK_STATUSES:
        return "BLOCK", f"语义审读硬前置未满足（{status}）: {detail}"
    return "REVISE", f"语义审读未通过（{status}）: {detail}"


# ---------------------------------------------------------------------------
# 节点 1：规划审读批次
# ---------------------------------------------------------------------------

def _parent_dispatch_ids(state: ShrinkCoursewareState) -> Dict[str, str]:
    """从写作轮结果解析每个 scope 的写作者 dispatch_id（REVISE 时记录父子链）。"""
    out: Dict[str, str] = {}
    for entry in (state.get("dispatch_results") or {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "") != "ok":
            continue
        scope = str(entry.get("scope") or "")
        did = str(entry.get("dispatch_id") or "")
        if scope and did:
            out[scope] = did
    return out


def _build_check_requests(
    state: ShrinkCoursewareState,
    self_dir: str,
    batch: List[str],
    batch_id: str,
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    parents = _parent_dispatch_ids(state)
    requests: List[Dict[str, Any]] = []
    lines: List[str] = []
    for sec in batch:
        digest = _section_digest(self_dir, sec)
        instruction = (
            f"【语义审读·只读】请审读候选切片 {sec}.tex（sha256={digest}）。"
            f"严格按 prompts/semantic_check_prompt.md 的 schema 输出审读结果 JSON，"
            f"只写 .pd/semantic/{sec}.json，不得修改候选切片或其他任何文件。"
            f"审读绑定：section_digest 必须原样回填 {digest}。"
        )
        req = build_request(
            state,
            stage=f"T2_CHECK_{sec.upper()}",
            role=_checker_role(sec),
            prompt_file=SEMANTIC_PROMPT_FILE,
            artifacts=[f"{CHECK_DIR_REL}/{sec}.json"],
            instruction=instruction,
            command=f"python scripts/check_semantic.py {self_dir} --scope {sec}",
            context={
                "section_id": sec,
                "section_digest": digest,
                "section_text": _section_text(self_dir, sec),
                "parent_dispatch_id": parents.get(sec, ""),
                "user_spec": state.get("user_spec", {}),
            },
            scope=sec,
            batch_id=batch_id,
            timeout_seconds=int(cfg.get("timeout_seconds") or 900),
            max_retries=int(cfg.get("max_retries") or 2),
            retry_class="transient",
        )
        req_ref = write_request_file(self_dir, req)
        lines.append(f"T2 Check: 派发 {sec} 语义审读单 (派发单: {req_ref})")
        requests.append(req)
    return requests, lines


def node_t2_plan_check(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """语义审读轮主控：为缺少有效审读结果的 staged scope 规划审读批次。

    - 无待审 scope（全部已有与当前候选摘要匹配的 PASS 审读）→ 直接放行
      到串行提交（stage=T2_SECTIONS_STAGED，零派发不烧 LLM，对应 audit
      §14「无改动重跑不调 LLM」）；
    - `semantic_repair_target`（submit 硬闸门要求补派）强制纳入本批，
      即使已存在审读结果也重派；
    - 派发计划写独立文件并过 check_dispatch 门禁（契约违规硬停止）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = list(state.get("expected_sections") or [])
    statuses = dict(state.get("section_statuses", {}))
    history = list(state.get("history", []))
    repair_target = state.get("semantic_repair_target")

    batch: List[str] = []
    for sec in expected_sections:
        if statuses.get(sec) == "staged" or sec == repair_target or needs_check(self_dir, sec):
            batch.append(sec)
    if repair_target and repair_target in expected_sections and repair_target not in batch:
        batch.insert(0, repair_target)
    for sec in batch:
        statuses[sec] = "staged"

    if not batch:
        active = next(
            (sec for sec in expected_sections if statuses.get(sec) == "staged"),
            None)
        history.append(
            "T2 Check: 全部暂存切片均已有与当前候选匹配的语义审读结果，零派发放行")
        return {
            "stage": "T2_SECTIONS_STAGED",
            "routing_action": "PROCEED",
            "active_section": active,
            "semantic_repair_target": None,
            "history": history,
        }

    rounds = int(state.get("semantic_check_rounds") or 0) + 1
    if rounds > MAX_SEMANTIC_ROUNDS:
        history.append(
            "T2 Check: 语义审读轮次超限（%d > %d），熔断停止"
            % (rounds, MAX_SEMANTIC_ROUNDS))
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": (
                "语义审读轮次超限（%d > %d）：审读结果持续缺失或未通过，"
                "请人工介入（不得静默放行提交）"
                % (rounds, MAX_SEMANTIC_ROUNDS)),
            "semantic_repair_target": None,
            "history": history,
        }

    cfg = _load_dispatch_config(self_dir, state.get("run_scope") or "")
    batch_id = f"bat-t2-check-{uuid.uuid4().hex[:8]}"

    # 建池先于计划落盘：min_parallelism 按真实并行度承诺（任务8）
    _pool = _get_pool(
        batch_id, cfg["max_concurrency"], cfg["timeout_seconds"], cfg["max_retries"])
    _effective_limit = _pool.effective_concurrency if _pool is not None else None
    _worker_caps = _pool.worker_capabilities if _pool is not None else {}

    requests, build_lines = _build_check_requests(
        state, self_dir, batch, batch_id, cfg)
    history.extend(build_lines)

    plan_disk = _disk_plan(
        batch_id, batch, requests, cfg["max_concurrency"],
        effective_limit=_effective_limit, worker_capabilities=_worker_caps)
    plan_disk["round"] = "semantic"
    plan_disk["stage"] = "T2_CHECK"
    plan_disk["parent_dispatch_ids"] = _parent_dispatch_ids(state)
    _write_json_atomic(os.path.join(self_dir, CHECK_PLAN_REL), plan_disk)

    runner = ScriptRunner(self_dir)
    report = runner.run_gate_script(
        gate_name="check_dispatch",
        script_name="check_dispatch.py",
        args=[self_dir, "--plan", CHECK_PLAN_REL],
        report_rel_path=".pd/reports/check_dispatch_semantic.json",
        run_id=state.get("run_id", ""),
    )
    history.append(
        "T2 Check: check_dispatch 门禁状态 %s（退出 %s，报告 %s）"
        % (report.get("status"), report.get("exit_code"), report.get("report_path")))
    if report.get("status") != "PASS":
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"语义审读派发门禁未通过: {report.get('status')}",
            "history": history,
        }

    dispatch_history = list(state.get("dispatch_history") or [])
    previous_plan = state.get("dispatch_plan") or {}
    if previous_plan.get("batch_id"):
        dispatch_history.append({
            "batch_id": previous_plan.get("batch_id"),
            "sections": list(previous_plan.get("sections") or []),
            "metrics": dict(state.get("dispatch_metrics") or {}),
        })

    state_plan = dict(plan_disk)
    state_plan["requests_full"] = requests
    state_plan["dispatch_config"] = cfg

    history.append(
        "T2 Check: 规划语义审读批次 %s（%d 个 scope，档位 %s 并发 %d）"
        % (batch_id, len(batch), cfg["profile"], cfg["max_concurrency"]))

    return {
        "section_statuses": statuses,
        "dispatch_plan": state_plan,
        "batch_id": batch_id,
        "pending_dispatch": None,
        "active_section": None,
        "semantic_repair_target": None,
        "semantic_check_rounds": rounds,
        "stage": "T2_CHECK_PENDING",
        "routing_action": "PROCEED",
        "dispatch_metrics": {
            "submitted": len(batch),
            "completed": 0,
            "ok": 0,
            "failed": 0,
            "retried": 0,
            "max_in_flight": 0,
            "deferred": 0,
            "reused": 0,
        },
        "dispatch_history": dispatch_history,
        "agent_dispatch_profile": cfg["profile"],
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 2：批量派发（只提交，不 join）
# ---------------------------------------------------------------------------

def node_t2_dispatch_check(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """语义审读轮派发节点：整批提交到进程级 WorkerPool，绝不在此 join。

    任务4：join 是 join 节点的专属职责（每 batch 只允许一次）；dispatch
    节点只做校验/提交/记录，deferred 在 submit 时即确定，无需等待。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")
    history = list(state.get("history", []))

    if not plan or plan.get("batch_id") != batch_id \
            or plan.get("round") != "semantic":
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "语义审读计划与当前批次不符（stale plan）",
            "history": history,
        }

    requests = list(plan.get("requests_full") or [])
    if not requests:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "语义审读计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    cfg = plan.get("dispatch_config") or _load_dispatch_config(self_dir)
    pool = _get_pool(
        batch_id, cfg["max_concurrency"], cfg["timeout_seconds"], cfg["max_retries"])
    for req in requests:
        pool.submit(req)

    stats = pool.stats()
    deferred = int(stats.get("deferred", 0))
    mode = "manual" if deferred > 0 else "parallel"
    history.append(
        "T2 Check: 批次 %s 已提交 %d 个审读请求（模式 %s，deferred=%d）"
        % (batch_id, len(requests), mode, deferred))

    return {
        "stage": "T2_CHECK_RUNNING",
        "dispatch_mode": mode,
        "dispatch_metrics": stats,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 3：汇总回归（审读结论求值 + 硬门禁）
# ---------------------------------------------------------------------------

def _check_interrupt_payload(
    state: ShrinkCoursewareState,
    plan: Dict[str, Any],
    requests: List[Dict[str, Any]],
    batch_id: str,
) -> Dict[str, Any]:
    artifacts: List[str] = []
    before_digests: Dict[str, Optional[str]] = {}
    for req in requests:
        for art in (req.get("artifacts") or []):
            if art not in artifacts:
                artifacts.append(art)
        for rel, digest in (req.get("before") or {}).items():
            before_digests[rel] = digest
    sections = [str(req.get("scope") or "") for req in requests]
    return {
        "type": "AGENT_DISPATCH",
        "batch_id": batch_id,
        "dispatch_id": batch_id,
        "stage": plan.get("stage") or "T2_CHECK",
        "role": "T2 语义审读批派发（semantic-checker × N）",
        "description": (
            f"T2 语义审读批派发：一批 {len(requests)} 个小节审读"
            f"（{', '.join(s for s in sections if s)}）。审读员只读候选切片并"
            f"按 semantic_check_prompt.md 输出结构化 JSON，不得修改候选；"
            f"完成后恢复执行。"
        ),
        "artifacts": artifacts,
        "instruction": "\n\n".join(
            f"[{req.get('scope')}] {req.get('instruction')}" for req in requests),
        "command": " && ".join(
            str(req.get("command") or "") for req in requests if req.get("command")),
        "prompt_file": f"prompts/{SEMANTIC_PROMPT_FILE}",
        "request_refs": [
            f"{REQUEST_DIR_REL}/{req.get('dispatch_id')}.json" for req in requests
        ],
        "before_digests": before_digests,
        "context": {"sections": sections, "user_spec": state.get("user_spec", {})},
        **agent_evidence.dispatch_protocol_block(),
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
                "parent_dispatch_id": (req.get("context") or {}).get(
                    "parent_dispatch_id", ""),
            }
            for req in requests
        ],
    }


def node_t2_join_check(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """语义审读轮汇总节点：并行结果 / manual 中断恢复 / 磁盘重推导。

    求值后：
      - 全部 ok → 放行串行提交（stage=T2_SECTIONS_STAGED，PROCEED）；
      - revise / needs_evidence → REPAIR_SECTION（重派写作者返修，
        repair_reason 带 parent_dispatch_id 与检查码）；
      - missing/invalid/stale → REPAIR_SECTION + semantic_repair_target
        （图路由改走 t2_plan_check 补派审读，不重派写作者）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = list(state.get("expected_sections") or [])
    statuses = dict(state.get("section_statuses", {}))
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")
    requests = list(plan.get("requests_full") or [])

    if not plan or plan.get("batch_id") != batch_id \
            or plan.get("round") != "semantic":
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "语义审读计划与当前批次不符（stale plan）",
            "history": history,
        }
    if not requests:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "语义审读计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    pool = _live_pool(batch_id)
    join_timeout = _join_timeout(requests)
    raw_results: Dict[str, Any] = {}
    stats: Dict[str, int] = {}
    if pool is not None:
        raw_results = pool.join(timeout=join_timeout)
        stats = pool.stats()
        deferred = int(stats.get("deferred", 0))
        has_all = len(raw_results) >= len(requests)
    else:
        deferred = 1
        has_all = False

    parallel_mode = pool is not None and deferred == 0 and has_all
    ok_scopes: List[str] = []
    failures: Dict[str, str] = {}
    details: Dict[str, str] = {}
    results = dict(state.get("dispatch_results") or {})

    def _record(dispatch_id: str, scope: str, status: str, detail: str,
                attempts: int = 0, output: Any = None) -> None:
        entry: Dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "batch_id": batch_id,
            "scope": scope,
            "status": status,
            "attempts": attempts,
            "error": detail,
            "round": "semantic",
        }
        if output is not None:
            entry["output"] = output
        results[dispatch_id] = entry

    if parallel_mode:
        # ---- 1) PARALLEL：活池真实结果 → 磁盘审读 JSON 求值 ---------------
        for req in requests:
            dispatch_id = str(req.get("dispatch_id") or "")
            scope = str(req.get("scope") or "")
            res = raw_results.get(dispatch_id)
            if res is None:
                failures[scope] = "missing_result"
                details[scope] = "派发池未返回该审读请求的结果"
                _record(dispatch_id, scope, "missing_result", details[scope])
                continue
            status = str(_result_field(res, "status", "unknown") or "unknown")
            if status != "ok":
                failures[scope] = status
                details[scope] = str(_result_field(res, "error") or "审读派发失败")
                _record(dispatch_id, scope, status, details[scope],
                        _result_field(res, "attempts", 0))
                continue
            verdict, detail = evaluate_checker_result(
                self_dir, scope, _section_digest(self_dir, scope))
            _record(dispatch_id, scope, verdict, detail,
                    _result_field(res, "attempts", 0),
                    _shrink_output(_result_field(res, "output")))
            if verdict == "ok":
                ok_scopes.append(scope)
            else:
                failures[scope] = verdict
                details[scope] = detail
        history.append(
            "T2 Check: 批次 %s 并行收尾（通过=%d，未通过=%d）"
            % (batch_id, len(ok_scopes), len(failures)))
    else:
        # ---- 2)/3) MANUAL：中断一次 → 磁盘重推导 -------------------------
        if (state.get("manual_interrupted_batch") == batch_id
                or state.get("dispatch_mode") == "parallel"):
            history.append(
                f"T2 Check: 批次 {batch_id} 已派发过（人工中断恢复或进程重启），"
                f"从磁盘重推导审读结果（不重复 interrupt）")
            allow_unattested = _plan_allow_unattested(plan, self_dir)
            for req in requests:
                dispatch_id = str(req.get("dispatch_id") or "")
                scope = str(req.get("scope") or "")
                verdict, detail = evaluate_checker_result(
                    self_dir, scope, _section_digest(self_dir, scope))
                if verdict in ("missing_result", "invalid_result") \
                        and not allow_unattested:
                    failures[scope] = verdict
                    details[scope] = detail
                    _record(dispatch_id, scope, verdict, detail)
                    continue
                _record(dispatch_id, scope, verdict, detail)
                if verdict == "ok":
                    ok_scopes.append(scope)
                else:
                    failures[scope] = verdict
                    details[scope] = detail
        else:
            payload = _check_interrupt_payload(state, plan, requests, batch_id)
            history.append(
                f"Agent[T2-Check]: 挂起等待人工 / Antigravity 整批语义审读 "
                f"{', '.join(str(r.get('scope')) for r in requests)}")
            response = interrupt(payload)
            if not isinstance(response, dict):
                response = {}
            violation = agent_evidence.dispatch_protocol_violation(response)
            if violation:
                history.append(f"Agent[T2-Check]: {violation}")
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
                    f"Agent[T2-Check]: 已从中断响应落盘审读结果 "
                    f"{', '.join(sorted(set(written)))}")
            return {
                "manual_interrupted_batch": batch_id,
                "dispatch_mode": "manual",
                "stage": "T2_CHECK_RUNNING",
                "dispatch_results": results,
                "history": history,
            }

    # ---- 硬门禁：实际并发度不得低于计划值（仅 parallel 模式） -------------
    if parallel_mode:
        min_parallel = int(plan.get("min_parallelism") or 1)
        actual = int((stats or {}).get("max_in_flight", 0) or 0)
        if actual < min_parallel:
            history.append(
                "T2 Check: 批次 %s 实际并发度 %d 低于计划值 %d，判定伪装完成"
                % (batch_id, actual, min_parallel))
            return {
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": (
                    f"语义审读实际并发度 {actual} 低于计划值 {min_parallel}，"
                    "禁止伪装完成"),
                "dispatch_results": results,
                "dispatch_metrics": stats,
                "history": history,
            }

    if failures:
        repair_target = next(
            (sec for sec in expected_sections if sec in failures), None)
        detail = "; ".join(
            f"{scope}={status}({details.get(scope, '')})"
            for scope, status in failures.items())
        parents = _parent_dispatch_ids(state)
        parent_note = ", ".join(
            f"{sec}→{parents.get(sec, 'unknown')}"
            for sec in expected_sections if sec in failures)
        redispatch = [
            sec for sec in expected_sections
            if failures.get(sec) in REDISPATCH_CHECK_STATUSES]
        history.append(
            f"T2 Check: 批次 {batch_id} 存在未通过审读（{detail}），转入返修"
            + (f"（父子链 {parent_note}）" if parent_note else ""))
        patch: Dict[str, Any] = {
            "routing_action": "REPAIR_SECTION",
            "repair_target": repair_target,
            "repair_reason": (
                f"语义审读未通过: {detail}"
                + (f"；父子链 {parent_note}" if parent_note else "")),
            "stage": "REPAIR_LOOP",
            "dispatch_results": results,
            "dispatch_metrics": stats,
            "history": history,
        }
        if redispatch:
            # 缺结果/摘要过期/schema 非法 → 补派审读（不是写作者的错）
            patch["semantic_repair_target"] = redispatch[0]
        return patch

    active_section = next(
        (sec for sec in expected_sections if statuses.get(sec) == "staged"), None)
    history.append(
        f"T2 Check: 批次 {batch_id} 全部审读通过，{len(ok_scopes)} 个切片获得提交资格")
    return {
        "active_section": active_section,
        "routing_action": "PROCEED",
        "stage": "T2_SECTIONS_STAGED",
        "dispatch_results": results,
        "dispatch_metrics": stats,
        "semantic_repair_target": None,
        "history": history,
    }
