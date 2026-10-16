"""build_nodes.py — T3 Verification and Dual-Engine Compilation/Rendering nodes.
Executes check_numbering.py, build.py (PDF and/or HTML), and compare_versions.py.
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, List
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner


def node_t3_checks(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T3: Run pedagogy, numbering, and structure gates."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    # 1. Pedagogy gate (knowledgebox, microknowledge, cognitive stride, anti-spoiler)
    ped_report = runner.run_gate_script(
        gate_name="check_pedagogy",
        script_name="check_pedagogy.py",
        args=[self_dir],
        report_rel_path=".pd/reports/check_pedagogy.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: check_pedagogy 状态: {ped_report['status']} (退出 {ped_report['exit_code']})")

    if not ped_report["passed"]:
        return {
            "pedagogy_gate": ped_report,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": f"教学语义与自足性门禁未通过: {ped_report['status']}",
            "history": history
        }

    # 2. Numbering gate
    num_report = runner.run_gate_script(
        gate_name="check_numbering",
        script_name="check_numbering.py",
        args=[self_dir],
        report_rel_path=".pd/reports/check_numbering.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: check_numbering 状态: {num_report['status']} (退出 {num_report['exit_code']})")

    if not num_report["passed"]:
        return {
            "pedagogy_gate": ped_report,
            "check_numbering_gate": num_report,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": f"编号结构检查未通过: {num_report['status']}",
            "history": history
        }

    # 3. Dialogue gate (课堂对话层结构与防泄露硬门禁)
    dia_report = runner.run_gate_script(
        gate_name="check_dialogue",
        script_name="check_dialogue.py",
        args=[self_dir],
        report_rel_path=".pd/reports/check_dialogue.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: check_dialogue 状态: {dia_report['status']} (退出 {dia_report['exit_code']})")

    if not dia_report["passed"]:
        return {
            "pedagogy_gate": ped_report,
            "check_numbering_gate": num_report,
            "dialogue_gate": dia_report,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": f"对话层结构或泄露检查未通过: {dia_report['status']}",
            "history": history
        }

    # 4. Budget & classroom plan delivery gate (教学预算与翻转教案完备性门禁)
    bud_report = runner.run_gate_script(
        gate_name="check_budget",
        script_name="check_budget.py",
        args=[self_dir, "--stage", "delivery"],
        report_rel_path=".pd/reports/check_budget.json",
        run_id=state.get("run_id")
    )
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
                    "pedagogy_gate": ped_report,
                    "check_numbering_gate": num_report,
                    "dialogue_gate": dia_report,
                    "budget_gate": bud_report,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"重规划次数超限: {bud_report['status']}",
                    "history": history
                }
            return {
                "pedagogy_gate": ped_report,
                "check_numbering_gate": num_report,
                "dialogue_gate": dia_report,
                "budget_gate": bud_report,
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
                "pedagogy_gate": ped_report,
                "check_numbering_gate": num_report,
                "dialogue_gate": dia_report,
                "budget_gate": bud_report,
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
                "pedagogy_gate": ped_report,
                "check_numbering_gate": num_report,
                "dialogue_gate": dia_report,
                "budget_gate": bud_report,
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
                    "pedagogy_gate": ped_report,
                    "check_numbering_gate": num_report,
                    "dialogue_gate": dia_report,
                    "budget_gate": bud_report,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"重规划次数超限: {bud_report['status']}",
                    "history": history
                }
            return {
                "pedagogy_gate": ped_report,
                "check_numbering_gate": num_report,
                "dialogue_gate": dia_report,
                "budget_gate": bud_report,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN" if is_cov_issue else "REPAIR_SECTION",
                "repair_target": "coverage-map.md" if is_cov_issue else None,
                "repair_reason": f"教学预算或翻转教案检查未通过 (fallback): {bud_report['status']}",
                "history": history
            }

    return {
        "pedagogy_gate": ped_report,
        "check_numbering_gate": num_report,
        "dialogue_gate": dia_report,
        "budget_gate": bud_report,
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
        args=["--format", fmt, self_dir],
        report_rel_path=".pd/reports/build.json",
        run_id=state.get("run_id")
    )
    history.append(f"T3: 构建状态 [{fmt}]: {build_report['status']} (退出 {build_report['exit_code']})")

    if not build_report["passed"]:
        return {
            "build_gate": build_report,
            "build_passed": False,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": f"构建排版未通过: {build_report['status']}",
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
