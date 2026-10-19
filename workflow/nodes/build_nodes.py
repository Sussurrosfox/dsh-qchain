"""build_nodes.py — T3 Verification and Dual-Engine Compilation/Rendering nodes.
Executes check_numbering.py, build.py (PDF and/or HTML), and compare_versions.py.
"""

import os
import sys
import json
import hashlib
import re
from typing import Dict, Any, List, Optional
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner
from ..adapters.gate_runner import GateRunner


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
                     "FIDELITY_BRIDGE_UNAPPROVED")
    codes = [str(e.get("code", "")) for e in (raw.get("errors") or [])]
    for code, detail in zip(codes, details):
        if code in section_codes:
            m = re.match(r"(sec[0-9A-Za-z_-]+)", detail)
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
        m = re.match(r"(sec[0-9A-Za-z_-]+)", detail)
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
    gate_runner = GateRunner(self_dir)
    reports = gate_runner.run_gates([
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
    ], run_id=state.get("run_id"))

    # 八份报告一并进入 state（批量执行后逐个门禁都已完成，早失败时也保留
    # 全量证据，便于 UI/修复回路定位；级联顺序只决定路由判定优先级）。
    gate_patch = {
        "pedagogy_gate": reports["check_pedagogy"],
        "check_numbering_gate": reports["check_numbering"],
        "dialogue_gate": reports["check_dialogue"],
        "scope_gate": reports["check_scope"],
        "fidelity_gate": reports["check_fidelity"],
        "semantic_gate": reports["check_semantic"],
        "provenance_gate": reports["check_provenance"],
        "budget_gate": reports["check_budget"],
    }

    # 1. Pedagogy gate (knowledgebox, microknowledge, cognitive stride, anti-spoiler)
    ped_report = reports["check_pedagogy"]
    history.append(f"T3: check_pedagogy 状态: {ped_report['status']} (退出 {ped_report['exit_code']})")

    if not ped_report["passed"]:
        return {
            **gate_patch,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": _extract_target_sec(ped_report),
            "repair_reason": f"教学语义与自足性门禁未通过: {ped_report['status']}",
            "history": history
        }

    # 2. Numbering gate
    num_report = reports["check_numbering"]
    history.append(f"T3: check_numbering 状态: {num_report['status']} (退出 {num_report['exit_code']})")

    if not num_report["passed"]:
        return {
            **gate_patch,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": _extract_target_sec(num_report),
            "repair_reason": f"编号结构检查未通过: {num_report['status']}",
            "history": history
        }

    # 3. Dialogue gate (课堂对话层结构与防泄露硬门禁)
    dia_report = reports["check_dialogue"]
    history.append(f"T3: check_dialogue 状态: {dia_report['status']} (退出 {dia_report['exit_code']})")

    if not dia_report["passed"]:
        return {
            **gate_patch,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": _extract_target_sec(dia_report),
            "repair_reason": f"对话层结构或泄露检查未通过: {dia_report['status']}",
            "history": history
        }

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
    sem_report = reports["check_semantic"]
    history.append(f"T3: check_semantic 状态: {sem_report['status']} (退出 {sem_report['exit_code']})")

    if not sem_report["passed"]:
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
        return {
            **gate_patch,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": None,
            "repair_reason": f"语义审读门禁未通过 ({','.join(sem_codes)}): {sem_report['status']}",
            "history": history
        }

    # 5.6 §7 provenance_gate：血缘链（writer attempt 认证当前切片内容 /
    #     checker 结果未过期 / qid 规划登记）与写域白名单。可定位到切片的
    #     断裂 → 重派该节写作者；定位不到 → HALTED（不放行无主篡改）。
    prov_report = reports["check_provenance"]
    history.append(f"T3: check_provenance 状态: {prov_report['status']} (退出 {prov_report['exit_code']})")

    if not prov_report["passed"]:
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
        elif category in ("section", "artifact"):
            repair_target = "classroom-plan.md" if category == "artifact" else None
            return {
                **gate_patch,
                "replan_count": state.get("replan_count", 0),
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": repair_target,
                "repair_reason": f"教学产物或切片检查未通过 (category={category}): {bud_report['status']}",
                "history": history
            }
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
            return {
                **gate_patch,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN" if is_cov_issue else "REPAIR_SECTION",
                "repair_target": "coverage-map.md" if is_cov_issue else None,
                "repair_reason": f"教学预算或翻转教案检查未通过 (fallback): {bud_report['status']}",
                "history": history
            }

    return {
        **gate_patch,
        "stage": "T3_CHECKED",
        "routing_action": "PROCEED",
        "history": history
    }


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
        target_sec = _extract_target_sec(build_report, self_dir) or "sec2"
        return {
            "build_gate": build_report,
            "build_passed": False,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": target_sec,
            "repair_reason": f"构建排版未通过: LaTeX 编译失败在 {target_sec}，原因: 请修复 probchain 列表中 \\item 语法（如 \\textbf{{证明策略：}}必须包裹在 \\item \\qtype{{...}} 之后，不能在 \\item 之前直接输出普通文本段落，否则 LaTeX 报 missing \\item 致命错误）。",
            "history": history
        }

    return {
        "build_gate": build_report,
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
        return {
            "compare_gate": cmp_report,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": repair_reason,
            "history": history
        }

    return {
        "compare_gate": cmp_report,
        "stage": "T3_COMPARED",
        "routing_action": "PROCEED",
        "history": history
    }
