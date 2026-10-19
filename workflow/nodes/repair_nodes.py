"""repair_nodes.py — Targeted repair dispatch and verification loop.
Dispatches specific repair instructions to the problematic section,
and halts if no artifact changes are made.
"""

from typing import Dict, Any, List
from ..state import ShrinkCoursewareState
from .agent_nodes import build_request, write_request_file, run_dispatch


def node_repair_section(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """Dispatch targeted repair request for a specific section or LaTeX macro."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = state.get("expected_sections") or []
    # 返修目标解析：显式 repair_target > 当前在写切片 > 首个计划切片。
    # 旧实现缺失时硬编码 "sec1"，会把构建/比对等全局失败误派给第一节。
    target_sec = (
        state.get("repair_target")
        or state.get("active_section")
        or (expected_sections[0] if expected_sections else None)
        or "sec1"
    )
    reason = state.get("repair_reason", "需要修复门禁未通过项")
    retry_count = state.get("retry_count", 0) + 1
    history = list(state.get("history", []))

    if retry_count > 3:
        history.append(f"Repair: 重试次数超限 ({retry_count} > 3)，熔断停止")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"返修重试次数超限: {reason}",
            "history": history
        }

    if any(target_sec.endswith(ext) for ext in (".md", ".tex", ".json", ".yaml")):
        tex_filename = target_sec
    else:
        tex_filename = f"{target_sec}.tex"
    req = build_request(
        state,
        stage="REPAIR",
        role=f"{target_sec} 返修专员",
        prompt_file="subagent_prompt.md",
        artifacts=[tex_filename],
        instruction=f"【定点返修】原因: {reason}。请修改 {tex_filename} 并修复违规项。",
        command=f"python scripts/pdstate.py self/ submit {target_sec}",
        context={"repair_reason": reason, "target_section": target_sec}
    )
    req_ref = write_request_file(self_dir, req)
    history.append(f"Repair: 已派发 {target_sec} 返修任务 (第 {retry_count} 次，派发单: {req_ref})")

    return {
        "pending_dispatch": req,
        "retry_count": retry_count,
        "active_section": target_sec,
        "repair_target": target_sec,
        "stage": "REPAIR_LOOP",
        "history": history
    }


def node_repair_await(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """Wait for repaired artifacts to be produced and modified."""
    req = state.get("pending_dispatch")
    expected_sections = state.get("expected_sections") or []
    target_sec = state.get("repair_target") or state.get("active_section") or "sec1"
    if not req:
        return {"stage": "T2_SECTIONS_STAGED", "active_section": target_sec, "routing_action": "PROCEED"}

    res = run_dispatch(
        state,
        req,
        success_stage="T2_SECTIONS_STAGED",
        success_history=f"Repair: 产物 {target_sec} 已完成定点修改"
    )
    if res.get("stage") != "HALTED":
        res["retry_count"] = 0
        if target_sec in expected_sections:
            res["active_section"] = target_sec
        else:
            res["active_section"] = None
            res["repair_target"] = None
            res["stage"] = "T3_CHECKS_PENDING"
    return res
