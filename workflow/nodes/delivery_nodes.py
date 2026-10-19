"""delivery_nodes.py — T4 Review pack, pedagogical acceptance, and final delivery.
Executes make_review_pack.py, dispatches independent acceptance, and runs deliver.py
to atomically publish to release/.
"""

import json
import os
import sys
from typing import Dict, Any, List, Optional
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import evidence


def node_t4_make_review_pack(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Generate student review pack for controlled acceptance and populate classroom-plan.md."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    # 1. 自动装配/更新翻转课堂教案 (classroom-plan.md，N1/N2)
    cov_path = os.path.join(self_dir, "coverage-map.md")
    if os.path.isfile(cov_path):
        code_plan, out_plan, err_plan = runner.run_script("gen_classroom_plan.py", [self_dir])
        if code_plan == 0:
            history.append("T4: gen_classroom_plan.py 已自动装配翻转课堂计划卡 (classroom-plan.md)")
        else:
            history.append(f"T4: gen_classroom_plan 提示 (exit {code_plan}): {(err_plan or out_plan).strip()}")

    # 2. 生成学生审读包
    code, out, err = runner.run_script("make_review_pack.py", [self_dir])
    if code != 0:
        history.append(f"T4: make_review_pack 提示 (exit {code}): {(err or out).strip()}")
    else:
        history.append("T4: make_review_pack.py 已生成学生审读包 (.pd/review/)")

    return {
        "review_pack_ready": (code == 0),
        "stage": "T4_ACCEPTANCE_PENDING",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t4_verify_acceptance(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Verify pedagogical acceptance report status with evidence binding (B4)."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    report_path = os.path.join(self_dir, ".pd", "acceptance", "chapter.json")

    # 当前证据基线：内容摘要 + 按 output_format 对应存在的学生版产物哈希
    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"
    current_digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    student_pdf_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.pdf"))
        if fmt in ("pdf", "both") else None
    )
    student_html_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.html"))
        if fmt in ("html", "both") else None
    )

    passed = False
    status = "REVISE"
    issues = []
    conclusion = "MISSING"
    data = None

    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            status_val = str(data.get("conclusion") or data.get("status") or "").upper()
            conclusion = status_val or "UNKNOWN"
            issues = data.get("issues", [])
        except Exception as e:
            issues = [{"category": "PARSE_ERROR", "message": str(e)}]
            conclusion = "PARSE_ERROR"
    # 注意：缺失 / 无法解析验收记录一律不得视为通过（BUG #3a）。
    # 旧实现在文件缺失时直接置 PASS，使独立教学验收层可被整体绕过。

    # B4: 验收证据强校验 —— chapter.json 必须通过全部字段校验、结论为 PASS、
    # content_digest 绑定当前 evidence.content_digest、学生版产物哈希绑定当前构建。
    binding_problems: List[str] = []
    if data is not None and conclusion != "PARSE_ERROR":
        binding_problems = evidence.validate_acceptance_json(
            data,
            current_digest=current_digest,
            student_pdf_sha=student_pdf_sha,
            student_html_sha=student_html_sha
        )
        if binding_problems:
            history.append(f"T4: 验收证据绑定校验未通过（{len(binding_problems)} 项）")

    if not binding_problems and conclusion == "PASS":
        passed = True
        status = "PASS"

    gate = GateReport(
        gate_name="pedagogical_acceptance",
        status=status,
        passed=passed,
        issues=issues,
        report_path=report_path,
        exit_code=0 if passed else 1
    )

    def _redispatch(reason_label: str) -> Dict[str, Any]:
        """重新派发独立教学验收（带轮次熔断，防死循环）。"""
        rounds = state.get("acceptance_rounds", 0) + 1
        if rounds > 3:
            history.append(f"T4: 验收证据{reason_label}，重新派发轮次超限（{rounds} > 3），熔断停止")
            return {
                "acceptance_gate": gate,
                "acceptance_rounds": rounds,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"独立教学验收证据{reason_label}且重新派发超限",
                "history": history
            }
        history.append(
            f"T4: 验收证据{reason_label}，重新派发独立教学验收（第 {rounds} 轮，缺证据不得放行）"
        )
        return {
            "acceptance_gate": gate,
            "acceptance_rounds": rounds,
            "stage": "T4_ACCEPTANCE_PENDING",
            "routing_action": "REDISPATCH_ACCEPTANCE",
            "history": history
        }

    if not passed and conclusion in ("MISSING", "PARSE_ERROR", "UNKNOWN"):
        # 缺证据/坏记录：重新派发独立教学验收
        return _redispatch(conclusion)

    if not passed and (binding_problems or conclusion == "NEEDS_EVIDENCE"):
        # 记录可解析但字段/摘要/哈希绑定不满足：旧证据不得冒充当前结果
        return _redispatch("绑定校验失败" if binding_problems else conclusion)

    if not passed:
        # REVISE：验收专员要求修改 —— 同样消耗验收轮次（修复 REVISE 无限循环）
        rounds = state.get("acceptance_rounds", 0) + 1
        if rounds > 3:
            history.append(f"T4: 验收结论持续为 {conclusion}，轮次超限（{rounds} > 3），熔断停止")
            return {
                "acceptance_gate": gate,
                "acceptance_rounds": rounds,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"独立教学验收结论持续为 {conclusion} 且轮次超限",
                "history": history
            }
        return {
            "acceptance_gate": gate,
            "acceptance_rounds": rounds,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_reason": f"教学验收未通过: {status}",
            "history": history
        }

    history.append("T4: 教学验收核准通过")
    return {
        "acceptance_gate": gate,
        "acceptance_rounds": state.get("acceptance_rounds", 0),
        "stage": "T4_ACCEPTED",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t4_promote_and_deliver(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Execute deliver.py for final gate synthesis and atomic release."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"
    # Run deliver.py
    code, out, err = runner.run_script("deliver.py", [self_dir, "--format", fmt])
    history.append(f"T4: deliver.py 汇总门禁执行 (退出 {code}): {(out or err).strip()[:200]}")

    manifest_path = os.path.join(self_dir, "release", "manifest.json")
    manifest_data = None
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                manifest_data = json.load(f)
        except Exception:
            pass

    promoted = (code == 0) and (manifest_data is not None)

    deliver_gate = GateReport(
        gate_name="deliver",
        status="PASS" if promoted else "REJECT",
        passed=promoted,
        exit_code=code,
        report_path=manifest_path,
        raw_report=manifest_data or {}
    )

    if not promoted:
        return {
            "deliver_gate": deliver_gate,
            "promoted": False,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"最终交付门禁未通过 (退出 {code})",
            "history": history
        }

    history.append(f"T4: 恭喜！学案已完成原子发布至 {os.path.join(self_dir, 'release')}")

    return {
        "deliver_gate": deliver_gate,
        "delivery_manifest": manifest_data,
        "promoted": True,
        "stage": "T4_DELIVERED",
        "routing_action": "PROCEED",
        "history": history
    }
