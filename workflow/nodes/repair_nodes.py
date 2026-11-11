"""repair_nodes.py — Targeted repair dispatch and verification loop.
Dispatches specific repair instructions to the problematic section,
and halts if no artifact changes are made.

卡 06（DATA-CONTRACTS C3）：内容返修有**持久**上限与无进展熔断——
计数 / 前次 issue 指纹 / 前次产物摘要全部写进 checkpoint 状态（见
``workflow/repair_budget.py``）；dispatch 返回 ok **不清零**，只有相关门禁真正
PASS 才解除“本次未解决”标记；全局累计永不重置。
"""

from typing import Dict, Any, List
from ..state import ShrinkCoursewareState
from .. import repair_budget
from .agent_nodes import build_request, write_request_file, run_dispatch, snapshot


def _halt_repair(state: ShrinkCoursewareState, history: List[str], plan: Dict[str, Any],
                 reason: str) -> Dict[str, Any]:
    """无进展 / 预算耗尽 → HALT，并附未解决 issue、最后产物、attempt 来源。"""
    history.append(f"Repair: {plan['detail']}，熔断停止 [{plan['halt_reason']}]")
    return {
        "stage": "HALTED",
        "routing_action": "HALT",
        "error_message": f"{plan['detail']}: {reason}",
        "repair_halt": plan["halt"],
        "history": history,
    }


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
    history = list(state.get("history", []))

    if any(target_sec.endswith(ext) for ext in (".md", ".tex", ".json", ".yaml")):
        tex_filename = target_sec
    else:
        tex_filename = f"{target_sec}.tex"

    # C3：先判无进展/额度，再安排返修。输入摘要 = 返修前该产物的 sha256。
    input_digest = snapshot(self_dir, [tex_filename]).get(tex_filename)
    # A5：返修上限的运维覆盖走 config.yaml（扁平键或 agent_dispatch 子块），
    # 不再需要拿 msgpack 二进制改 checkpoint 的 repair_per_scope_limit。
    repair_cfg = repair_budget.load_repair_overrides(self_dir)
    plan = repair_budget.plan_repair(
        state, target_sec, reason, input_digest,
        issues=state.get("repair_issues"), artifact=tex_filename,
        config=repair_cfg)
    if plan["action"] == "HALT":
        return _halt_repair(state, history, plan, reason)

    # retry_count 保留为 legacy 展示字段（本回路累计安排的返修次数）；
    # 熔断判定已改由持久计数承担，dispatch ok 不再把它清零。
    retry_count = state.get("retry_count", 0) + 1
    req = build_request(
        state,
        stage="REPAIR",
        role=f"{target_sec} 返修专员",
        prompt_file="subagent_prompt.md",
        artifacts=[tex_filename],
        instruction=f"【定点返修】原因: {reason}。请修改 {tex_filename} 并修复违规项。",
        command=f"python scripts/pdstate.py self/ submit {target_sec}",
        context={"repair_reason": reason, "target_section": target_sec},
        scope=target_sec
    )
    req_ref = write_request_file(self_dir, req)
    history.append(
        f"Repair: 已派发 {target_sec} 返修任务 (第 {plan['attempt_no']} 次/本 scope，"
        f"全局第 {plan['patch']['repair_attempts_total']} 次，派发单: {req_ref})")

    # 计数 + 指纹 + 输入摘要与派发单在**同一次**状态更新里落盘（先持久化后派发）。
    budget_patch = plan["patch"]
    budget_patch["repair_attempt_log"] = list(budget_patch["repair_attempt_log"]) + [
        repair_budget.attempt_log_entry(
            target_sec, plan["attempt_no"], budget_patch["repair_attempts_total"],
            req["dispatch_id"], plan["fingerprint"], input_digest, reason)
    ]
    return {
        **budget_patch,
        "repair_halt": None,
        "pending_dispatch": req,
        "retry_count": retry_count,
        "active_section": target_sec,
        "repair_target": target_sec,
        "stage": "REPAIR_LOOP",
        "history": history
    }


def node_repair_await(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """Wait for repaired artifacts to be produced and modified.

    本节点**只**执行一次 run_dispatch：内容失败（缺产物/无变更/无证据）由派发层
    直接返回 HALTED，不在同一 prompt 上重试；也不动任何返修计数——
    成功派发不清零 retry_count / repair_*，门禁 PASS 才解除未解决标记。
    """
    # F2（defense in depth）：上游 repair_section 熔断时 state 已是 HALTED，
    # 这里绝不能把 stage 改写成 T2_SECTIONS_STAGED/PROCEED——那会把"预算耗尽"
    # 变成"返修成功"，让流水线带着未修的问题继续往交付走。原样传回 HALT。
    if state.get("stage") == "HALTED" or state.get("routing_action") == "HALT":
        history = list(state.get("history", []))
        history.append("Repair: 上游已熔断（stage=HALTED），repair_await 不再改写状态")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": state.get("error_message") or "返修回路已熔断",
            "repair_halt": state.get("repair_halt"),
            "history": history,
        }
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
        # 卡 06：不再 res["retry_count"] = 0（dispatch ok ≠ 问题已解决）。
        if target_sec in expected_sections:
            res["active_section"] = target_sec
            res["semantic_check_rounds"] = 0
        else:
            res["active_section"] = None
            res["repair_target"] = None
            res["stage"] = "T3_CHECKS_PENDING"
    return res
