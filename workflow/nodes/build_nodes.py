"""build_nodes.py — T3 Verification and Dual-Engine Compilation/Rendering nodes.
Executes check_numbering.py, build.py (PDF and/or HTML), check_render.py
(render-quality gate), and compare_versions.py.
"""

import os
import sys
import json
import hashlib
import re
from typing import Dict, Any, List, Optional, Sequence
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner
from ..adapters.gate_runner import GateRunner
from .. import issues as issues_mod
from .. import repair_budget


def _extract_target_sec(report: Dict[str, Any], self_dir: str = None) -> str:
    if report:
        for iss in report.get("issues", []):
            if iss.get("severity") == "error":
                m = re.search(r'(sec\w+)\.tex', iss.get("message", ""))
                if m:
                    return m.group(1)
        for iss in report.get("issues", []):
            m = re.search(r'(sec\w+)\.tex', iss.get("message", ""))
            if m:
                return m.group(1)
    if self_dir:
        failed_targets = []
        if report:
            for iss in report.get("issues", []):
                if iss.get("severity") == "error" and iss.get("target"):
                    failed_targets.append(iss.get("target"))
        target_logs = [f"{t}.log" for t in failed_targets if t in ("student", "teacher")]
        if not target_logs:
            target_logs = ["teacher.log", "student.log"]
        for log_name in target_logs:
            log_path = os.path.join(self_dir, log_name)
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    err_pos = content.rfind("\n!")
                    if err_pos == -1:
                        m_over = re.search(r'Overfull \\hbox \(([3-9]\d{2}|\d{4,})', content)
                        if m_over:
                            err_pos = m_over.start()
                    search_content = content[:err_pos] if err_pos != -1 else content
                    secs = re.findall(r'\((sec\w+)\.tex', search_content)
                    if secs:
                        return secs[-1]
                except Exception:
                    pass
    return None


def _fidelity_codes(report: Dict[str, Any]) -> List[str]:
    """从 GateReport 提取 check_fidelity 的错误码（errors[].code）。"""
    raw = report.get("raw_report") or {}
    codes = [str(e.get("code", "")) for e in (raw.get("errors") or [])]
    if codes:
        return sorted({c for c in codes if c})
    text = str(report.get("status") or "")
    found = re.findall(r"FIDELITY_[A-Z_]+", text)
    return sorted(set(found))


def _fidelity_repair_section(report: Dict[str, Any]) -> Optional[str]:
    """正文级忠实性违规 → 返回需 REPAIR_SECTION 的切片名，否则 None。"""
    raw = report.get("raw_report") or {}
    details = [str(e.get("detail", "")) for e in (raw.get("errors") or [])]
    section_codes = ("FIDELITY_UNGROUNDED_QID", "FIDELITY_UNGROUNDED_BLOCK",
                     "FIDELITY_EXCLUDED_REFERENCED",
                     "FIDELITY_BRIDGE_UNAPPROVED",
                     "FIDELITY_ASSET_LOST", "FIDELITY_SOURCE_NOT_COVERED")
    codes = [str(e.get("code", "")) for e in (raw.get("errors") or [])]
    for code, detail in zip(codes, details):
        if code in section_codes:
            m = re.search(r"\b(sec[0-9A-Za-z_-]+)\b", detail)
            return m.group(1) if m else None
    return None


def _semantic_repair_section(report: Dict[str, Any]) -> tuple:
    """语义审读门禁违规 → (错误码列表, 需返修的切片名或 None)。

    check_semantic.py 的 detail 均以“小节 <sec> …”开头，与
    _fidelity_repair_section 同一解析口径。
    """
    raw = report.get("raw_report") or {}
    errors = raw.get("errors") or []
    codes = [str(e.get("code", "")) for e in errors if isinstance(e, dict)]
    for e in errors:
        detail = str(e.get("detail", "")) if isinstance(e, dict) else ""
        m = re.search(r"\b(sec[0-9A-Za-z_-]+)\b", detail)
        if m:
            return codes, m.group(1)
    return codes, None


def _provenance_repair_section(report: Dict[str, Any]) -> Optional[str]:
    """血缘链违规 → 返回需重派写作者的切片名，否则 None。

    check_provenance.py 的 detail 均以“切片 <sec> …”开头
    （PROV_WRITER_DIGEST / PROV_CHECKER_* / PROV_MISSING_QID /
    PROV_PATH_ESCAPE）；解析不出切片时返回 None（由调用方按
    HALTED 处理，不放行无主血缘断裂）。
    """
    raw = report.get("raw_report") or {}
    for e in (raw.get("errors") or []):
        detail = str(e.get("detail", "")) if isinstance(e, dict) else ""
        m = re.match(r"(?:切片\s*)?(sec[0-9A-Za-z_-]+)", detail)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# 卡 05：门禁失败 → 统一 issue 适配层（workflow/issues.py）→ 路由裁决。
# 基础设施失败 / 未知 error / 定位不到切片一律 HALT；绝不派作者盲改。
# ---------------------------------------------------------------------------

def _halt_patch(gate_patch: Dict[str, Any], history: List[str], label: str,
                reason: str, repair_target: Optional[str] = None) -> Dict[str, Any]:
    history.append(f"T3: {label} -> HALT: {reason}")
    return {
        **gate_patch,
        "stage": "HALTED",
        "routing_action": "HALT",
        # 卡 07：HALT 也如实带出“真正的问题产物”（pdf-layout / classroom-plan.md /
        # unknown_target），供人工与证据使用；HALT 一律由 stage 终止图，不据此派发。
        "repair_target": repair_target,
        "error_message": f"{label}: {reason}",
        "history": history,
    }


def _infra_halt(gate_patch: Dict[str, Any], history: List[str], label: str,
                report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """门禁自身执行失败（ERROR / 报告缺失或过期）→ HALT，不当内容问题返修。"""
    reason = issues_mod.infrastructure_failure(report)
    if reason is None:
        return None
    return _halt_patch(gate_patch, history, label, f"门禁基础设施失败（非内容问题）: {reason}")


def _routed_failure(gate_patch: Dict[str, Any], history: List[str], gate: str,
                    label: str, report: Dict[str, Any], repair_reason: str,
                    located: Optional[str] = None, *,
                    content_exit_failure: bool = False) -> Dict[str, Any]:
    """按显式路由表裁决一个失败门禁：HALT 或带明确目标的 REPAIR_SECTION/REPLAN。"""
    infra = _infra_halt(gate_patch, history, label, report)
    if infra is not None:
        return infra
    decision = issues_mod.route_gate_report(
        report, gate, located_target=located,
        content_exit_failure=content_exit_failure)
    if decision["action"] == "HALT":
        return _halt_patch(gate_patch, history, label, decision["reason"])
    return {
        **gate_patch,
        "stage": "REPAIR_LOOP",
        "routing_action": decision["action"],
        "repair_target": decision["repair_target"],
        "repair_reason": repair_reason,
        "history": history,
    }


def _require_classroom_plan(self_dir: str,
                            execution_config: Optional[Dict[str, Any]] = None) -> bool:
    """卡 11：课堂计划是否必需（教案是可选交付物）。

    F3（冻结优先）：checkpoint 里的 ``execution_config`` 是 Phase 0 冻结值，
    优先级高于当前 config.yaml——否则把 YAML 改一行就能让已开始的会话不再
    要求课堂计划。旧检查点（无冻结块）仍按 YAML/legacy 默认判定。
    缺 execution_profile 的旧项目 → legacy 默认（要求教案）；faithful_fast 默认
    不要求，此时既不预生成也不因缺教案阻断，来源/数学门禁照旧。
    """
    from .. import execution_policy as ep
    frozen = execution_config if isinstance(execution_config, dict) else {}
    if frozen.get("require_classroom_plan") is not None:
        try:
            return ep.normalize_bool(frozen["require_classroom_plan"],
                                     "require_classroom_plan")
        except Exception:
            return True
    try:
        values = ep.read_execution_overrides(os.path.join(self_dir, "config.yaml")) or {}
    except Exception:
        return True
    if values.get("require_classroom_plan") is not None:
        try:
            return ep.normalize_bool(values["require_classroom_plan"],
                                     "require_classroom_plan")
        except Exception:
            return True
    profile = ep.resolve_profile(values.get("execution_profile"))
    defaults = (ep.EXECUTION_DEFAULTS if profile == ep.FAST_PROFILE
                else ep.LEGACY_EXECUTION_DEFAULTS)
    return bool(defaults.get("require_classroom_plan", True))


def _pregenerate_classroom_plan(self_dir: str, history: List[str],
                                execution_config: Optional[Dict[str, Any]] = None
                                ) -> Optional[Dict[str, Any]]:
    """卡 07（ART-01）：delivery 预算检查之前先确定性生成课堂计划。

    只有存在 coverage-map.md（生成输入）时才生成；生成由 gen_classroom_plan.py 完成
    （人手改过的计划它会保护不覆盖）。生成失败 → 返回 HALT patch，报告转换原因，
    不拿占位/缺失的计划去检查，也不派作者、不自环。成功或无需生成 → None。

    卡 11：require_classroom_plan=false 时教案非必需——不生成、不检查、不阻断。
    """
    if not _require_classroom_plan(self_dir, execution_config):
        history.append("T3: require_classroom_plan=false，课堂计划为可选交付物，"
                       "不预生成也不检查")
        return None
    if not os.path.isfile(os.path.join(self_dir, "coverage-map.md")):
        return None
    code, out, err = ScriptRunner(self_dir).run_script("gen_classroom_plan.py", [self_dir])
    if code == 0:
        history.append("T3: 检查前确定性生成课堂计划 gen_classroom_plan.py (exit 0)，不派 Agent")
        return None
    detail = " ".join(str(x).strip() for x in (out, err) if x and str(x).strip())
    return _halt_patch({}, history, "课堂计划生成",
                       f"gen_classroom_plan.py 转换失败 (exit {code}): {detail[:300] or '无输出'}",
                       repair_target="classroom-plan.md")


def _halt_artifact_failure(gate_patch: Dict[str, Any], history: List[str],
                           bud_report: Dict[str, Any]) -> Dict[str, Any]:
    """卡 07（ART-02）：artifact 类预算失败按“码 + 产物”精确路由，一律 HALT。

    * 学生 PDF 页数超标 → target=pdf-layout（人工版式问题），绝不改写课堂计划；
    * 课堂计划检查前已确定性生成，仍不合格 → target=classroom-plan.md，不再二次生成；
    * 定位不到产物 → unknown_target，不按类别猜文件。
    """
    decision = issues_mod.route_gate_report(bud_report, "check_budget")
    return _halt_patch(gate_patch, history, "预算门禁", decision["reason"],
                       repair_target=decision["repair_target"])


# ---------------------------------------------------------------------------
# 卡15 / C6：把 T3 门禁分成「构建前（prebuild）」与「终审（final）」两趟
#
#   prebuild：结构/编号/忠实映射/所选预算策略 + provenance 的 writer-only 部分，
#             允许尚无 reviewer 证据；语义审读记 status=pending_review（绝不算 PASS）。
#   final   ：合并终审全部 PASS 之后才跑——全量 semantic + 全量 provenance，
#             通过后再逐节 mark checked（唯一写 checked 的地方）。
#   legacy  ：execution_profile 缺省/legacy → 原样跑八门禁并在 T2 逐节审读，
#             旧会话语义逐字节不变。
# ---------------------------------------------------------------------------

EXECUTION_PROFILE_FAST = "faithful_fast"
#: 构建前一趟的 provenance 报告落盘名（不覆盖 final 的正式报告）
PROVENANCE_PREBUILD_REPORT_REL = ".pd/reports/check_provenance_prebuild.json"
#: 构建前语义审读的明确状态：证据未产生，既不 PASS 也不伪造失败
SEMANTIC_PENDING_REVIEW = "pending_review"


def _is_fast_execution(state: ShrinkCoursewareState) -> bool:
    return str(state.get("execution_profile") or "") == EXECUTION_PROFILE_FAST


def _fast_gate_phase(state: ShrinkCoursewareState) -> Optional[str]:
    """None=legacy；"prebuild"/"final"=faithful_fast 的两趟 T3 门禁。"""
    if not _is_fast_execution(state):
        return None
    if str(state.get("gate_phase") or "") == "final":
        return "final"
    return "prebuild"


def _pending_review_report(self_dir: str, state: ShrinkCoursewareState) -> Dict[str, Any]:
    """构建前语义审读占位报告：status=pending_review，passed=False。

    它不是证据、也不落盘成 PASS 报告——交付侧任何 MUST_PASS 报告要求
    status=="pass"，因此 pending_review 必然阻断交付（GRAPH-01）。
    """
    return {
        "gate_name": "check_semantic",
        "status": SEMANTIC_PENDING_REVIEW,
        "passed": False,
        "issues": [{
            "code": "SEMANTIC_PENDING_REVIEW",
            "artifact": "combined-review",
            "message": "语义审读证据要等合并终审产出，构建前只做确定性检查",
        }],
        "raw_report": {
            "schema_version": 2,
            "tool": "check_semantic.py",
            "tool_version": "deferred",
            "status": SEMANTIC_PENDING_REVIEW,
            "errors": [],
            "warnings": ["faithful_fast：构建前不产生审读证据，等合并终审"],
            "checked": {},
        },
        "exit_code": 0,
        "report_path": ".pd/reports/check_semantic.json",
        "report_generated_at": "",
        "report_is_fresh": False,
        "run_id": state.get("run_id"),
    }


def _pdstate_task_status(self_dir: str, sec: str) -> Optional[str]:
    path = os.path.join(self_dir, ".pd", "run.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return ((data.get("tasks") or {}).get(sec) or {}).get("status")
    except Exception:
        return None


def _mark_scopes_checked(self_dir: str, expected: Sequence[str],
                         history: List[str]) -> Dict[str, Any]:
    """终审通过后逐节 mark checked（pdstate 契约的唯一 checked 入口）。"""
    runner = ScriptRunner(self_dir)
    statuses: Dict[str, Any] = {}
    for sec in expected:
        if _pdstate_task_status(self_dir, sec) == "checked":
            statuses[sec] = "checked"
            continue
        code, out, err = runner.run_script(
            "pdstate.py", [self_dir, "mark", sec, "checked"])
        if code != 0:
            detail = (err or out or "mark checked failed").strip()
            history.append(
                f"T3: 终审已通过但 pdstate mark {sec} checked 失败 (exit {code}): {detail}")
            return {
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": (
                    f"终审已通过但切片 {sec} 无法标记 checked (exit {code}): "
                    f"{detail[:300]}"),
            }
        statuses[sec] = "checked"
        history.append(f"T3: 切片 {sec} 终审通过，已标记 checked")
    return {"section_statuses": statuses}


def node_t3_checks(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T3: Run pedagogy, numbering, and structure gates."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))

    # 任务7（audit §12.6）：八个纯 Python 门禁合并为一次进程内批量执行
    # （GateRunner），省去每脚本一个子进程的固定成本，并共享一份输入快照
    # （batch_input_digest 附在各报告上作 provenance）。
    # 级联判定顺序保持 pedagogy → numbering → dialogue → scope → fidelity
    # → semantic → provenance → budget；XeLaTeX 编译与版本比对仍在后续
    # 节点走子进程（进程不内联编译器）。
    # 卡 07：delivery 预算检查需要课堂计划，先确定性生成再检查（不是检查失败后补救）。
    gen_halt = _pregenerate_classroom_plan(
        self_dir, history, state.get("execution_config"))
    if gen_halt is not None:
        return gen_halt

    gate_phase = _fast_gate_phase(state)
    fast_prebuild = gate_phase == "prebuild"

    gate_runner = GateRunner(self_dir)
    gate_specs = [
        {
            "gate_name": "check_pedagogy",
            "script_name": "check_pedagogy.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_pedagogy.json",
        },
        {
            "gate_name": "check_numbering",
            "script_name": "check_numbering.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_numbering.json",
        },
        {
            "gate_name": "check_dialogue",
            "script_name": "check_dialogue.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_dialogue.json",
        },
        {
            # §7 scope_gate：Phase 0 冻结合约（source-manifest /
            # intent-contract）与生效配置的零漂移对账——模式降级、课时/
            # 节数/策略篡改、未经确认的范围变更一律阻断（契约级问题无法
            # 靠重派切片修复，走 HALTED 交人工）。
            "gate_name": "check_scope",
            "script_name": "check_scope.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_scope.json",
        },
        {
            "gate_name": "check_fidelity",
            "script_name": "check_fidelity.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_fidelity.json",
        },
        {
            # §6/§7 semantic_gate：语义审读一致性（缺审读记录/候选摘要过期/
            # blocking issue 一律不放行）。T2 审读轮已逐节通过，此处是装配后
            # 的确定性复核（同一脚本、同一摘要口径）。
            "gate_name": "check_semantic",
            "script_name": "check_semantic.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_semantic.json",
        },
        {
            # §7 provenance_gate：writer attempt → checker 结果 → qid 规划的
            # 血缘链与写域白名单复核（check_provenance.py）。血缘链断裂
            # （切片被改写但无 writer attempt 认证）→ 重派该节写作者。
            "gate_name": "check_provenance",
            "script_name": "check_provenance.py",
            "args": [self_dir],
            "report_rel_path": ".pd/reports/check_provenance.json",
        },
        {
            "gate_name": "check_budget",
            "script_name": "check_budget.py",
            "args": [self_dir, "--stage", "delivery"],
            "report_rel_path": ".pd/reports/check_budget.json",
        },
    ]

    if fast_prebuild:
        # 卡15：构建前一趟只跑确定性检查——
        #   * check_semantic 此时必然没有 reviewer 证据（合并终审还没跑），跑它
        #     只会制造假失败并逼迫逐节审读，故整项不跑，state 里记 pending_review；
        #   * check_provenance 降级 writer-only（writer attempt 链/写域白名单/qid
        #     规划仍硬校验），checker 血缘链留到 final 一趟，且报告单独落盘，
        #     绝不覆盖正式报告。
        gate_specs = [spec for spec in gate_specs
                      if spec["gate_name"] != "check_semantic"]
        rest = []
        for spec in gate_specs:
            if spec["gate_name"] == "check_provenance":
                spec = dict(spec)
                spec["args"] = [self_dir, "--mode", "writer-only"]
                spec["report_rel_path"] = PROVENANCE_PREBUILD_REPORT_REL
            rest.append(spec)
        gate_specs = rest
        history.append("T3: 构建前门禁（prebuild）：语义审读记 pending_review，"
                       "provenance 只校验 writer-only 部分")

    reports = gate_runner.run_gates(gate_specs, run_id=state.get("run_id"))

    # 八份报告一并进入 state（批量执行后逐个门禁都已完成，早失败时也保留
    # 全量证据，便于 UI/修复回路定位；级联顺序只决定路由判定优先级）。
    gate_patch = {
        "pedagogy_gate": reports["check_pedagogy"],
        "check_numbering_gate": reports["check_numbering"],
        "dialogue_gate": reports["check_dialogue"],
        "scope_gate": reports["check_scope"],
        "fidelity_gate": reports["check_fidelity"],
        # 构建前没有审读证据 → 明确记 pending_review（不伪造 PASS）
        "semantic_gate": (reports.get("check_semantic")
                          or _pending_review_report(self_dir, state)),
        "provenance_gate": reports["check_provenance"],
        "budget_gate": reports["check_budget"],
    }

    # 1. Pedagogy gate (knowledgebox, microknowledge, cognitive stride, anti-spoiler)
    ped_report = reports["check_pedagogy"]
    history.append(f"T3: check_pedagogy 状态: {ped_report['status']} (退出 {ped_report['exit_code']})")

    if not ped_report["passed"]:
        return _routed_failure(
            gate_patch, history, "check_pedagogy", "教学语义门禁", ped_report,
            f"教学语义与自足性门禁未通过: {ped_report['status']}",
            _extract_target_sec(ped_report))

    # 2. Numbering gate
    num_report = reports["check_numbering"]
    history.append(f"T3: check_numbering 状态: {num_report['status']} (退出 {num_report['exit_code']})")

    if not num_report["passed"]:
        return _routed_failure(
            gate_patch, history, "check_numbering", "编号结构门禁", num_report,
            f"编号结构检查未通过: {num_report['status']}",
            _extract_target_sec(num_report))

    # 3. Dialogue gate (课堂对话层结构与防泄露硬门禁)
    dia_report = reports["check_dialogue"]
    history.append(f"T3: check_dialogue 状态: {dia_report['status']} (退出 {dia_report['exit_code']})")

    if not dia_report["passed"]:
        return _routed_failure(
            gate_patch, history, "check_dialogue", "对话层门禁", dia_report,
            f"对话层结构或泄露检查未通过: {dia_report['status']}",
            _extract_target_sec(dia_report))

    # 4. §7 scope_gate：Phase 0 冻结合约 vs 生效配置零漂移。契约级违规
    #    （模式降级/课时策略篡改/范围变更未经人工确认）无法靠重派切片修复，
    #    直接 HALTED 交人工重新确认目标合同。
    scope_report = reports["check_scope"]
    history.append(f"T3: check_scope 状态: {scope_report['status']} (退出 {scope_report['exit_code']})")

    if not scope_report["passed"]:
        return {
            **gate_patch,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"目标合同/配置范围漂移（scope_gate 未通过）: {scope_report['status']}",
            "history": history
        }

    # 5. 来源忠实性门禁（audit §18.6：check_fidelity.py，T2 锚点/映射/
    #    批准台账的确定性校验；契约级违规回退 T1 重规划，正文级违规修节）
    fid_report = reports["check_fidelity"]
    history.append(f"T3: check_fidelity 状态: {fid_report['status']} (退出 {fid_report['exit_code']})")

    if not fid_report["passed"]:
        infra = _infra_halt(gate_patch, history, "来源忠实性门禁", fid_report)
        if infra is not None:
            return infra
        fid_codes = _fidelity_codes(fid_report)
        sec_hit = _fidelity_repair_section(fid_report)
        if sec_hit:
            return {
                **gate_patch,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": sec_hit,
                "repair_reason": f"来源忠实性门禁未通过 ({','.join(fid_codes)}): {fid_report['status']}",
                "history": history
            }
        replan_count = state.get("replan_count", 0) + 1
        if replan_count > 3:
            history.append(f"T3: 忠实性重规划次数超限 ({replan_count} > 3)，熔断停止")
            return {
                **gate_patch,
                "replan_count": replan_count,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"来源忠实性重规划次数超限: {fid_report['status']}",
                "history": history
            }
        return {
            **gate_patch,
            "replan_count": replan_count,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPLAN",
            "repair_target": "source-map.md",
            "repair_reason": f"来源映射/扩展契约违规 ({','.join(fid_codes)}): {fid_report['status']}",
            "history": history
        }

    # 5.5 语义审读门禁（audit §6/§7 semantic_gate：Writer→Checker→Gate 的
    #     Gate 层复核）。缺审读记录/候选摘要过期/schema 非法 → 补派审读
    #     （semantic_repair_target，写作者无过错）；REVISE/NEEDS_EVIDENCE →
    #     重派写作者返修。任何情形不得静默放行装配后的内容。
    sem_report = gate_patch["semantic_gate"]
    history.append(f"T3: check_semantic 状态: {sem_report['status']} (退出 {sem_report['exit_code']})")

    if fast_prebuild:
        # 构建前：语义审读尚未发生（pending_review），不是失败也不是通过——
        # 这里只记一笔，真正的硬闸门在 final 一趟（合并终审 PASS 之后）。
        history.append("T3: 构建前不做语义裁决（pending_review），终审通过后统一复核")
    elif not sem_report["passed"]:
        infra = _infra_halt(gate_patch, history, "语义审读门禁", sem_report)
        if infra is not None:
            return infra
        sem_codes, sec_hit = _semantic_repair_section(sem_report)
        # 审读证据类违规（缺记录/过期/非法）→ 补派审读；语义阻断类 → 返修作者
        evidence_codes = ("SEMANTIC_MISSING", "SEMANTIC_STALE",
                          "SEMANTIC_INVALID", "SEMANTIC_SECTION_MISSING")
        need_checker = any(c in evidence_codes for c in sem_codes)
        if sec_hit:
            return {
                **gate_patch,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": sec_hit,
                "semantic_repair_target": sec_hit if need_checker else None,
                "repair_reason": f"语义审读门禁未通过 ({','.join(sem_codes)}): {sem_report['status']}",
                "history": history
            }
        # 定位不到切片：停机交人工，不让 node_repair_section 回退到“上一节/第一节”盲改
        return _halt_patch(
            gate_patch, history, "语义审读门禁",
            f"语义审读门禁未通过 ({','.join(sem_codes)}) 但无法定位切片: {sem_report['status']}")

    # 5.6 §7 provenance_gate：血缘链（writer attempt 认证当前切片内容 /
    #     checker 结果未过期 / qid 规划登记）与写域白名单。可定位到切片的
    #     断裂 → 重派该节写作者；定位不到 → HALTED（不放行无主篡改）。
    prov_report = reports["check_provenance"]
    history.append(f"T3: check_provenance 状态: {prov_report['status']} (退出 {prov_report['exit_code']})")

    if not prov_report["passed"]:
        infra = _infra_halt(gate_patch, history, "血缘门禁", prov_report)
        if infra is not None:
            return infra
        sec_hit = _provenance_repair_section(prov_report)
        if sec_hit:
            return {
                **gate_patch,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": sec_hit,
                "repair_reason": f"产物血缘链断裂（provenance_gate 未通过）: {prov_report['status']}",
                "history": history
            }
        return {
            **gate_patch,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"产物血缘链断裂且无法定位切片（provenance_gate 未通过）: {prov_report['status']}",
            "history": history
        }

    # 6. Budget & classroom plan delivery gate (教学预算与翻转教案完备性门禁)
    bud_report = reports["check_budget"]
    history.append(f"T3: check_budget (delivery) 状态: {bud_report['status']} (退出 {bud_report['exit_code']})")

    if not bud_report["passed"]:
        infra = _infra_halt(gate_patch, history, "预算门禁", bud_report)
        if infra is not None:
            return infra
        # 结构化分类路由逻辑 (修复 F9 与子串匹配脆弱性)
        raw_bud = bud_report.get("raw_report") or {}
        category = bud_report.get("category") or raw_bud.get("category", "")
        category = str(category).lower().strip()

        if category == "coverage":
            # B1: REPLAN 重规划计数，防 T1 映射死循环（>3 熔断 HALT）
            replan_count = state.get("replan_count", 0) + 1
            if replan_count > 3:
                history.append(f"T3: 重规划次数超限 ({replan_count} > 3)，熔断停止")
                return {
                    **gate_patch,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"重规划次数超限: {bud_report['status']}",
                    "history": history
                }
            return {
                **gate_patch,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN",
                "repair_target": "coverage-map.md",
                "repair_reason": f"教学预算或题量规划不合规 (category=coverage): {bud_report['status']}",
                "history": history
            }
        elif category == "artifact":
            # 卡 07：计划已在检查前生成；此处只按“码 + 产物”精确分流并停机
            return _halt_artifact_failure(gate_patch, history, bud_report)
        elif category == "section":
            patch = _routed_failure(
                gate_patch, history, "check_budget", "预算门禁", bud_report,
                f"教学产物或切片检查未通过 (category={category}): {bud_report['status']}",
                _extract_target_sec(bud_report))
            patch["replan_count"] = state.get("replan_count", 0)
            return patch
        elif category == "config":
            history.append(f"T3: 触发配置安全告警 (category=config): {bud_report['status']}")
            return {
                **gate_patch,
                "replan_count": state.get("replan_count", 0),
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"配置安全对账违规: {bud_report['status']}",
                "history": history
            }
        else:
            # Fallback 容错分支
            is_cov_issue = "coverage-map" in str(bud_report)
            replan_count = state.get("replan_count", 0) + 1 if is_cov_issue else state.get("replan_count", 0)
            if is_cov_issue and replan_count > 3:
                history.append(f"T3: 重规划次数超限 ({replan_count} > 3)，熔断停止")
                return {
                    **gate_patch,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"重规划次数超限: {bud_report['status']}",
                    "history": history
                }
            if not is_cov_issue:
                # 未知预算类别：不再用“内容返修 + repair_target=None”顶替，停机
                return _halt_patch(
                    gate_patch, history, "预算门禁",
                    f"未知预算失败类别 category={category!r}，无法归类: {bud_report['status']}")
            return {
                **gate_patch,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN",
                "repair_target": "coverage-map.md",
                "repair_reason": f"教学预算或翻转教案检查未通过 (fallback): {bud_report['status']}",
                "history": history
            }

    passed_patch = {
        **gate_patch,
        # 卡 06：全部门禁真正 PASS → 只解除“本次未解决”标记；返修计数永不在此清零。
        **repair_budget.resolved_patch(state, None),
        "stage": "T3_CHECKED",
        "routing_action": "PROCEED",
        # 卡15：路由函数只拿得到本节点返回的 patch，执行档案必须随 patch 透传，
        # 否则 route_after_checks 无法判断该走 build（prebuild）还是验收（final）。
        "execution_profile": state.get("execution_profile"),
    }

    if gate_phase == "final":
        # 卡15：final 门禁（全量 semantic + 全量 provenance）全部通过之后，
        # 才允许把切片标记为 checked——这是全流程唯一写 checked 的地方。
        mark_patch = _mark_scopes_checked(
            self_dir, list(state.get("expected_sections") or []), history)
        if mark_patch.get("stage") == "HALTED":
            return {**passed_patch, **mark_patch, "history": history}
        passed_patch.update(mark_patch)
        passed_patch["gate_phase"] = "final"
        history.append("T3: 终审门禁（final）通过，逐节 checked 已写入 pdstate")
    elif fast_prebuild:
        passed_patch["gate_phase"] = "prebuild"
        history.append("T3: 构建前门禁通过（语义审读仍为 pending_review，"
                       "等合并终审裁决）")

    passed_patch["history"] = history
    return passed_patch


def _run_render_gate(self_dir: str, fmt: str, run_id: Optional[str] = None
                     ) -> Dict[str, Any]:
    r"""渲染质量门禁（check_render.py，audit §19：乱码/选项空白防复发）。

    必须在 build.py 产出 *.html / *.pdf / *.log 之后运行，故由
    node_t3_build 单独调 GateRunner 执行（in-process，失败回退子进程）。
    与 t3_checks 的装配前结构门禁并列、互不替代：
    结构门禁查“卡片数/槽位数对得上”，本门禁查“渲染出来的字是不是字”。
    """
    gate_runner = GateRunner(self_dir)
    report = gate_runner.run_gate(
        gate_name="check_render",
        script_name="check_render.py",
        args=[self_dir, "--format", fmt],
        report_rel_path=".pd/reports/check_render.json",
        timeout=180,
        run_id=run_id,
    )
    return report


def _extract_render_target_sec(render_report: Dict[str, Any], self_dir: str,
                               expected_sections: List[str]) -> Optional[str]:
    r"""渲染门禁失败 → 定位真正含违规记号的那个切片。

    check_render 的 issue 只报 HTML/PDF 侧现象（如 “.qc-math 含 KaTeX
    不支持的宏 \bigsqcup, \pmod”），不带切片名。旧实现直接置
    repair_target=None，node_repair_section 便回退到 active_section
    （上一轮刚写完的 secN），把构建/渲染类全局失败误派给无关切片——
    子 Agent 在干净切片上找不到违规项、只能原样返回，最终以
    “产物未发生变化” 熔断停机（GR.5 死循环事故）。

    这里把 issue 里的宏名抽出来，回扫各 sec*.tex，按 expected 顺序返回
    第一个真正命中的切片；全部不命中则返回 None（交由上游决定）。
    """
    if not render_report:
        return None
    macros: set = set()
    for iss in render_report.get("issues", []) or []:
        msg = iss.get("message") or ""
        m = re.search(r'不支持的宏\s+(.+)$', msg)
        if m:
            for tok in re.split(r'[,\s]+', m.group(1)):
                tok = tok.strip().strip(',')
                if tok:
                    macros.add(tok)
        for tok in re.findall(r'\\[a-zA-Z]+', msg):
            macros.add(tok)
    if not macros:
        return None
    secs = list(expected_sections or [])
    if self_dir and not secs:
        try:
            secs = sorted(
                os.path.splitext(f)[0] for f in os.listdir(self_dir)
                if re.match(r'^sec\w+\.tex$', f)
            )
        except OSError:
            secs = []
    for sec in secs:
        if not self_dir:
            break
        path = os.path.join(self_dir, sec + ".tex")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if any(mac in text for mac in macros):
            return sec
    return None


def _extract_compare_target_sec(cmp_report: Dict[str, Any],
                                expected_sections: List[str]) -> Optional[str]:
    r"""compare REVIEW 失败 → 定位真正含疑似泄露项的那个切片。

    compare.json 的每条 review issue 都带 `qid`（如 "sec3-q1"），
    但 node_t3_compare 的失败分支只写 repair_reason、不写
    repair_target，node_repair_section 便回退到 active_section
    （上一轮刚返修完的切片）。GR.5 事故即此形态：sec1 修好后下一轮
    的 compare 仍报 REVIEW（残留项在 sec2/sec3），返修单却继续指向
    sec1 —— 子 Agent 在干净切片上找不到泄露项、原样返回，最终以
    “产物未发生变化” 熔断停机。

    这里从 qid 前缀收集全部待修切片，按 expected 顺序返回第一个；
    一个都无法确定则返回 None（交由上游回退）。
    """
    if not cmp_report:
        return None
    raw = cmp_report.get("raw_report") or cmp_report
    hits: set = set()
    for iss in raw.get("issues", []) or []:
        if (iss.get("severity") or "").lower() != "review":
            continue
        qid = str(iss.get("qid") or "")
        m = re.match(r'^(sec[\w-]*?)-[Qq]\d+$', qid)
        if m:
            hits.add(m.group(1))
    if not hits:
        return None
    for sec in (expected_sections or []):
        if sec in hits:
            return sec
    return sorted(hits)[0] if hits else None


def _first_latex_error(self_dir, targets=("student", "teacher")):
    """从 XeLaTeX 编译日志提取首个致命错误行（`! ` 开头）及其下一行
    上下文，供构建返修理由携带真实诊断而非固定猜测。"""
    errs = []
    for t in targets:
        log_path = os.path.join(self_dir, "%s.log" % t)
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            if ln.startswith("! "):
                ctx = lines[i + 1].strip() if i + 1 < len(lines) else ""
                errs.append("%s: %s（%s）" % (t, ln.strip(), ctx[:80]))
                break
    return "；".join(errs)


def node_t3_build(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T3: Compile dual-source XeLaTeX PDFs and/or HTML card decks."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"

    build_report = runner.run_gate_script(
        gate_name="build",
        script_name="build.py",
        args=["--format", fmt, "--max-overfull-pt", "300", "--allow-mdframed-breaks", self_dir],
        report_rel_path=".pd/reports/build.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: 构建状态 [{fmt}]: {build_report['status']} (退出 {build_report['exit_code']})")

    if not build_report["passed"]:
        located_sec = _extract_target_sec(build_report, self_dir)
        base = {"build_gate": build_report, "build_passed": False}
        routed = _routed_failure(base, history, "build", "构建排版门禁", build_report,
                                 "", located_sec, content_exit_failure=True)
        if routed["routing_action"] == "HALT":
            return routed  # 基础设施失败 / 定位不到切片：停机，不再盲猜 sec2
        target_sec = routed["repair_target"]
        latex_err = _first_latex_error(self_dir)
        reason_hint = (
            "probchain 是 enumerate 环境：\\begin{probchain} 之后、第一个 "
            "\\item 之前禁止放普通段落或 sectiondialogue/exampledialogue 等"
            "非列表内容（LaTeX 报 missing \\item），须把它们移到 probchain "
            "之外；item 内也不可在 \\item 之前先输出文本。"
        )
        return {
            "build_gate": build_report,
            "build_passed": False,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": target_sec,
            "repair_reason": (
                "构建排版未通过: LaTeX 编译失败在 %s。日志诊断: %s。修复指引: %s"
                % (target_sec,
                   latex_err or "编译日志无明确 `! ` 错误行，请检查 "
                                "%s/student.log 与 %s/teacher.log"
                                % (self_dir, self_dir),
                   reason_hint)
            ),
            "history": history
        }

    # 渲染质量门禁：构建产物已就绪，此处查“渲染级”失败（HTML 双重转义 /
    # center/tabular 压平、KaTeX 宏不支持、选项汉字被 \ensuremath 吞掉、
    # PDF Missing character / 空选项组）。此类缺陷此前全部结构门禁均显示
    # PASS（GR.4 交付后由人工发现），故单列硬门禁，不过即返修切片。
    try:
        render_report = _run_render_gate(self_dir, fmt, state.get("run_id"))
    except Exception as exc:  # 门禁自身异常不得静默放行
        render_report = {
            "gate_name": "check_render",
            "status": "ERROR",
            "passed": False,
            "issues": [{"category": "EXECUTION_FAILURE",
                        "severity": "HIGH_RISK",
                        "message": f"render gate crashed: {exc}"}],
            "exit_code": 1,
            "report_path": None,
            "report_is_fresh": False,
        }
    history.append(f"T3: 渲染质量门禁 (check_render) 状态: "
                   f"{render_report['status']} "
                   f"(退出 {render_report['exit_code']})")

    if not render_report["passed"]:
        expected = state.get("expected_sections") or []
        target_sec = _extract_render_target_sec(render_report, self_dir, expected)
        base = {"build_gate": build_report, "render_gate": render_report,
                "build_passed": False}
        routed = _routed_failure(base, history, "check_render", "渲染质量门禁",
                                 render_report, "", target_sec)
        if routed["routing_action"] == "HALT":
            # 资源/引擎类缺陷（机械问题）、基础设施失败、定位不到切片：不派作者
            return routed
        target_sec = routed["repair_target"]
        return {
            "build_gate": build_report,
            "render_gate": render_report,
            "build_passed": False,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": target_sec,
            "repair_reason": (
                "渲染质量门禁未通过（HTML 乱码/源码泄漏/数学段损坏/选项汉字丢失/"
                "PDF 缺字或空选项组）：请修 sec*.tex 中把汉字写进数学模式的 "
                "\\fillin/\\ensuremath，改用 \\text{} 包裹；详见 "
                ".pd/reports/check_render.json。"
            ),
            "history": history
        }

    return {
        "build_gate": build_report,
        "render_gate": render_report,
        "build_passed": True,
        "stage": "T3_BUILT",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t3_compare(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T3: Run student version leakage and consistency comparison."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"

    cmp_report = runner.run_gate_script(
        gate_name="compare_versions",
        script_name="compare_versions.py",
        args=["--format", fmt, self_dir],
        report_rel_path=".pd/reports/compare.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: compare_versions 差异诊断状态: {cmp_report['status']} (退出 {cmp_report['exit_code']})")

    # B5: compare REVIEW 不放行 —— 疑似泄露项须修复，或经人工复核写入
    # .pd/review-notes.json（落盘由 deliver.py 终检强制，节点层只负责不放行）。
    if not cmp_report["passed"]:
        if cmp_report["status"] == "REVIEW":
            notes_path = os.path.join(self_dir, ".pd", "review-notes.json")
            if os.path.exists(notes_path):
                try:
                    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
                    if scripts_dir not in sys.path:
                        sys.path.insert(0, scripts_dir)
                    import evidence
                    with open(notes_path, "r", encoding="utf-8") as f:
                        notes_data = json.load(f)
                    cmp_raw = cmp_report.get("raw_report") or {}
                    cmp_sha = cmp_report.get("report_sha256")
                    if not cmp_sha and cmp_report.get("report_path") and os.path.exists(cmp_report["report_path"]):
                        cmp_sha = hashlib.sha256(open(cmp_report["report_path"], "rb").read()).hexdigest()
                    manifest = evidence.collect_inputs(self_dir)
                    input_dg = evidence.content_digest(manifest)
                    pdf_sha = cmp_raw.get("pdf_sha256") or {
                        n: evidence.sha256_file(os.path.join(self_dir, n + ".pdf"))
                        for n in ("student", "teacher") if os.path.exists(os.path.join(self_dir, n + ".pdf"))
                    }
                    html_sha = cmp_raw.get("html_sha256") or {
                        n: evidence.sha256_file(os.path.join(self_dir, n + ".html"))
                        for n in ("student", "teacher") if os.path.exists(os.path.join(self_dir, n + ".html"))
                    }
                    # 同步最新报告与产物哈希
                    notes_data["compare_report_sha256"] = cmp_sha
                    notes_data["input_digest"] = input_dg
                    if pdf_sha:
                        notes_data["pdf_sha256"] = pdf_sha
                    if html_sha:
                        notes_data["html_sha256"] = html_sha
                    with open(notes_path, "w", encoding="utf-8") as f:
                        json.dump(notes_data, f, ensure_ascii=False, indent=2)

                    probs = evidence.validate_review_notes(
                        notes_data, cmp_raw, cmp_sha, input_dg, pdf_sha, html_sha
                    )
                    if not probs:
                        history.append("T3: compare_versions 存在待复核项，但经有效 .pd/review-notes.json 人工复核放行")
                        return {
                            "compare_gate": cmp_report,
                            "stage": "T3_COMPARED",
                            "routing_action": "PROCEED",
                            "history": history
                        }
                    else:
                        history.append(f"T3: 校验 review-notes.json 存在问题: {probs}")
                except Exception as e:
                    history.append(f"T3: 处理 review-notes.json 异常: {e}")
                except Exception:
                    pass
            repair_reason = (
                "compare 存在疑似泄露项（severity=review），须修复或经人工复核写入 "
                ".pd/review-notes.json"
            )
        else:
            repair_reason = f"双版防泄露检查未通过: {cmp_report['status']}"
        if cmp_report["status"] == "REVIEW":
            # 卡 05：review 项只能人工复核（写入有效 review-notes.json），
            # 绝不自动派 Agent 去“修”一个只是疑似的泄露项。
            history.append(f"T3: compare REVIEW 无有效人工复核记录 -> HALT（不派 Agent）")
            return {
                "compare_gate": cmp_report,
                "stage": "HALTED",
                "routing_action": "HALT",
                "repair_target": None,
                "error_message": repair_reason,
                "history": history
            }
        return _routed_failure(
            {"compare_gate": cmp_report}, history, "compare_versions",
            "双版防泄露门禁", cmp_report, repair_reason,
            _extract_compare_target_sec(cmp_report, state.get("expected_sections") or []))

    return {
        "compare_gate": cmp_report,
        "stage": "T3_COMPARED",
        "routing_action": "PROCEED",
        "history": history
    }
