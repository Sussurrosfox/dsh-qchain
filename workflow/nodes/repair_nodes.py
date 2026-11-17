"""repair_nodes.py — Targeted repair dispatch and verification loop.
Dispatches specific repair instructions to the problematic section,
and halts if no artifact changes are made.

卡 06（DATA-CONTRACTS C3）：内容返修有**持久**上限与无进展熔断——
计数 / 前次 issue 指纹 / 前次产物摘要全部写进 checkpoint 状态（见
``workflow/repair_budget.py``）；dispatch 返回 ok **不清零**，只有相关门禁真正
PASS 才解除“本次未解决”标记；全局累计永不重置。
"""

import json
import os
from typing import Dict, Any, List
from ..state import ShrinkCoursewareState
from .. import repair_budget
from .. import recovery
from ..adapters.script_runner import ScriptRunner
from .agent_nodes import build_request, write_request_file, run_dispatch, snapshot


def _halt_repair(state: ShrinkCoursewareState, history: List[str], plan: Dict[str, Any],
                 reason: str) -> Dict[str, Any]:
    """无进展 / 预算耗尽 → HALT，并附未解决 issue、最后产物、attempt 来源。"""
    history.append(f"Repair: {plan['detail']}，熔断停止 [{plan['halt_reason']}]")
    cause = ("budget_exhausted" if str(plan.get("halt_reason") or "") ==
             "repair_budget_exhausted" else "no_progress")
    return {
        "stage": "HALTED",
        "routing_action": "HALT",
        "error_message": f"{plan['detail']}: {reason}",
        "repair_halt": plan["halt"],
        # RC-3：不再只给一句 error_message——给出闭集恢复动作与真人批准入口，
        # 免得下一个人现写 sqlite 手术脚本。
        "halt_payload": recovery.build_halt_payload(
            cause=cause, reason=f"{plan['detail']}: {reason}",
            gate="repair", target=state.get("repair_target"),
            dispatch_id=state.get("pending_dispatch"),
            budget={"per_scope": (state.get("repair_attempts_by_scope") or {}),
                    "total": state.get("repair_attempts_total"),
                    "halt": plan.get("halt")}),
        "history": history,
    }


def _failure_cause(state: ShrinkCoursewareState) -> str:
    """RC-4：判定这次返修属于哪类失败，决定是否消耗内容返修额度。

    优先级：显式 ``repair_failure_cause``（节点自己已经知道原因）> issue 的
    ``repair_kind``（路由表给的分类）> 返修理由里的契约特征词 > content。

    额度只给 content——契约漂移/缺资产/环境缺口不是写作者的过错，让它们吃掉
    额度，真实内容问题就无额度可用了（本次 run 的 sec2 2/2 就是这样没的）。
    """
    explicit = state.get("repair_failure_cause")
    if explicit:
        return repair_budget.normalize_cause(explicit)
    for issue in (state.get("repair_issues") or []):
        if not isinstance(issue, dict):
            continue
        kind = str(issue.get("repair_kind") or "")
        if kind in ("asset", "environment", "deterministic"):
            return kind
        if kind == "human":
            return "infrastructure"
    reason = str(state.get("repair_reason") or "")
    for marker in ("当前状态为", "输入摘要已变化", "没有当前 attempt",
                   "缺少冻结的", "不能 submit", "作废（stale）"):
        if marker in reason:
            return "contract"
    return "content"


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
    # RC-4：失败原因决定是否消耗内容返修额度（额度是给写作者的）。
    cause = _failure_cause(state)
    # A5：返修上限的运维覆盖走 config.yaml（扁平键或 agent_dispatch 子块），
    # 不再需要拿 msgpack 二进制改 checkpoint 的 repair_per_scope_limit。
    repair_cfg = repair_budget.load_repair_overrides(self_dir)
    plan = repair_budget.plan_repair(
        state, target_sec, reason, input_digest,
        issues=state.get("repair_issues"), artifact=tex_filename,
        config=repair_cfg, cause=cause)
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
    charged = cause in repair_budget.CHARGED_CAUSES
    history.append(
        f"Repair: 已派发 {target_sec} 返修任务 (第 {plan['attempt_no']} 次/本 scope，"
        f"全局第 {plan['patch']['repair_attempts_total']} 次，原因 {cause}"
        f"{'' if charged else '（不占内容额度）'}，派发单: {req_ref})")

    # 计数 + 指纹 + 输入摘要与派发单在**同一次**状态更新里落盘（先持久化后派发）。
    budget_patch = plan["patch"]
    budget_patch["repair_attempt_log"] = list(budget_patch["repair_attempt_log"]) + [
        repair_budget.attempt_log_entry(
            target_sec, plan["attempt_no"], budget_patch["repair_attempts_total"],
            req["dispatch_id"], plan["fingerprint"], input_digest, reason,
            cause=cause)
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


def _plan_rel(target: str) -> str:
    """确定性修复计划的规范位置（按产物名分组，便于人写、便于审计）。"""
    stem = (target or "artifact").replace("\\", "/").strip("/").replace("/", "__")
    if stem.lower().endswith(".tex"):
        stem = stem[:-4]
    return f".pd/repairs/{stem}.plan.json"


_PLAN_TEMPLATE = {
    "schema_version": 1,
    "reason": "",
    "_说明": [
        "这是确定性修复计划的空模板。每个 op 都必须带 expect_before_sha256（CAS），",
        "命中数不符或摘要不符时整批放弃、不落盘（见 scripts/patch_artifact.py）。",
        "可用 op：replace_literal / append_section / ensure_table_row / copy_asset。",
        "填好删掉本 _说明 键，再 resume 流水线即可由引擎执行（不派 Agent）。",
    ],
    "ops": [
        {
            "op": "replace_literal",
            "path": "coverage-map.md",
            "expect_before_sha256": "<sha256 of coverage-map.md>",
            "old": "<待替换的原文片段>",
            "new": "<替换后的文本>",
            "expect_count": 1,
        }
    ],
}


def node_repair_deterministic(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """RC-2：执行确定性修复（不派 Agent）。

    查找 ``.pd/repairs/<target>.plan.json``：

      * 存在 → 调 ``scripts/patch_artifact.py`` 执行（白名单 op + CAS + 自检）。
        成功则回到门禁重跑；失败（CAS 不符/命中数不符）→ HALT，绝不半改。
      * 不存在 → 写出**空模板**并 HALT，error_message 指名模板路径。
        这是"先暴露后补表"的收敛兜底：不猜、不盲改、不派 Agent。

    审计口径：本节点把执行者记为 ``engine``（与 agent 并列的合法执行者），
    产物仍必须落盘并能被门禁复核——证据链不变。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    target = (state.get("repair_target") or state.get("det_repair_target")
              or "artifact")
    plan_rel = state.get("det_repair_plan") or _plan_rel(target)
    plan_abs = os.path.join(self_dir, *plan_rel.split("/"))

    if not os.path.isfile(plan_abs):
        os.makedirs(os.path.dirname(plan_abs), exist_ok=True)
        try:
            with open(plan_abs, "w", encoding="utf-8") as f:
                json.dump(_PLAN_TEMPLATE, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            history.append(f"Repair(det): 模板写入失败: {exc}")
        history.append(
            f"Repair(det): 缺确定性修复计划 {plan_rel}，已写出空模板，停机待补")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "repair_kind": "deterministic",
            "engine_repair": {"worker": "engine", "plan": plan_rel,
                              "status": "plan_missing"},
            "error_message": (
                f"确定性修复（{target}）缺计划：请填写 {plan_rel} 后 resume 重跑；"
                f"本轮不派 Agent、不盲改产物"),
            "history": history,
        }

    history.append(f"Repair(det): 执行确定性修复计划 {plan_rel}")
    runner = ScriptRunner(self_dir)
    ledger_rel = f".pd/repairs/{os.path.basename(plan_rel)}.result.json"
    ledger_abs = os.path.join(self_dir, *ledger_rel.split("/"))
    code, out, err = runner.run_script(
        "patch_artifact.py", [self_dir, plan_abs, "--json", ledger_abs])
    detail = ((err or out or "").strip().splitlines() or [""])[-1][:300]
    if code != 0:
        history.append(f"Repair(det): 计划未通过（exit {code}）: {detail}")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "repair_kind": "deterministic",
            "engine_repair": {"worker": "engine", "plan": plan_rel,
                              "status": "failed", "detail": detail},
            "error_message": (
                f"确定性修复未通过（{target}）: {detail}；"
                f"计划 {plan_rel} 未被应用（整批放弃，产物未被改动）"),
            "history": history,
        }
    history.append(f"Repair(det): 计划已应用（engine）: {detail}")
    return {
        "stage": "T3_CHECKS_PENDING",
        "routing_action": "PROCEED",
        "repair_kind": "deterministic",
        "engine_repair": {"worker": "engine", "plan": plan_rel,
                          "status": "applied", "ledger": ledger_rel},
        "repair_target": None,
        "repair_reason": "",
        "history": history,
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
            # 原样带出上游 halt_payload（恢复入口不因经过一个节点而丢失）
            "halt_payload": state.get("halt_payload"),
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
