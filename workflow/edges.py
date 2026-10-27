"""edges.py — Conditional routing logic for Shrink Courseware Pipeline.
"""

from typing import Literal
from langgraph.graph import END
from .state import ShrinkCoursewareState


def route_after_phase0(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    # A1：令牌未确认（伪造确认被拒）→ 自环回 phase0_breakpoint 重新挂起
    if not state.get("phase0_confirmed"):
        return "phase0_breakpoint"
    return "t0_init_scaffold"


def route_after_init(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_request_source_audit"


def route_after_t1_request_source(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_await_source_audit"


def route_after_t1_await_source(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_request_dependency_plan"


def route_after_t1_request_dependency(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_await_dependency_plan"


def route_after_t1_await_dependency(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_request_coverage_plan"


def route_after_t1_request_coverage(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_await_coverage_plan"


def route_after_mapping_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_verify_mapping"


def route_after_mapping_verify(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPLAN":
        # §5.1：按 repair_target 回到对应 round（缓存使未变更 round 免派发）；
        # 覆盖/预算问题只重跑 coverage-planner，映射契约问题从 source-auditor 重跑。
        if state.get("repair_target") == "coverage-map.md":
            return "t1_request_coverage_plan"
        return "t1_request_source_audit"
    return "t2_plan_dispatch"


def route_after_t2_plan(state: ShrinkCoursewareState) -> str:
    """T2 fan-out 主控三岔路由：派发 / 转串行提交 / 装配或停止。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("stage") == "T2_SECTIONS_PENDING":
        return "t2_dispatch_sections"
    # T2_SECTIONS_STAGED：ADVANCE（active_section=首个 staged）或
    # DONE（active_section=None 空转）均由既有串行提交节点收口
    return "t2_submit_and_check_sections"


def route_after_t2_dispatch(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t2_join_sections"


def route_after_t2_join(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    # manual 模式刚 interrupt（或并行在途）：图级自环回汇总节点，
    # 从磁盘重推导/等待结果，不重复 interrupt
    if state.get("stage") == "T2_SECTIONS_RUNNING":
        return "t2_join_sections"
    # §6：写作轮全部暂存后进入语义审读轮（Writer → Checker → Gate），
    # 审读通过才获得提交资格（checker 结果是 submit 硬前置）
    return "t2_plan_check"


def route_after_t2_plan_check(state: ShrinkCoursewareState) -> str:
    """语义审读轮主控路由：派发审读 / 零派发放行提交 / 停止。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("stage") == "T2_CHECK_PENDING":
        return "t2_dispatch_check"
    # 无待审 scope（全部已有匹配的 PASS 审读）→ 直接放行串行提交
    return "t2_submit_and_check_sections"


def route_after_t2_dispatch_check(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t2_join_check"


def route_after_t2_join_check(state: ShrinkCoursewareState) -> str:
    """语义审读轮汇总路由：补派审读 / 写作者返修 / 放行提交 / 自环。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        # 缺结果/摘要过期/schema 非法 → 补派审读（不是写作者的错）；
        # REVISE/NEEDS_EVIDENCE → 重派写作者返修（repair_reason 带父子链）
        if state.get("semantic_repair_target"):
            return "t2_plan_check"
        return "repair_section"
    # manual 模式刚 interrupt（或并行在途）：自环回收尾节点重推导
    if state.get("stage") == "T2_CHECK_RUNNING":
        return "t2_join_check"
    return "t2_submit_and_check_sections"


def route_after_section_check(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        # §6：语义审读硬闸门（缺结果/摘要过期/schema 非法）要求补派审读，
        # 不得走写作者返修环（写作者没错，是审读证据缺失）
        if state.get("semantic_repair_target"):
            return "t2_plan_check"
        return "repair_section"

    # Check if there are still pending sections
    # reused/skipped（audit §12.4 工作集外的复用/跳过节）是终态：
    # 不视为 pending，否则工作集全终态时 plan→submit 无限循环。
    expected = state.get("expected_sections", ["sec1"])
    statuses = state.get("section_statuses", {})
    has_pending = any(statuses.get(s) not in
                      ("checked", "accepted", "reused", "skipped")
                      for s in expected)
    if has_pending:
        return "t2_plan_dispatch"

    return "t2_assemble_main"


def route_after_assemble(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t2_shuffle_options"


def route_after_shuffle(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    return "t3_checks"


def route_after_checks(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPLAN":
        return "t1_request_source_audit"
    if state.get("routing_action") == "REPAIR_SECTION":
        if state.get("semantic_repair_target"):
            return "t2_plan_check"
        return "repair_section"
    return "t3_build"


def route_after_build(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    return "t3_compare"


def route_after_compare(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    return "t4_make_review_pack"


def route_after_review_pack(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t4_plan_review"


def route_after_t4_review_plan(state: ShrinkCoursewareState) -> str:
    """T4 验收 fan-out 主控路由：派发 / 零派发直收 / 停止。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("stage") == "T4_REVIEW_PENDING":
        return "t4_dispatch_review"
    # 任务6：全部 scope 复用（plan 判定零派发，dispatch_mode="reused"）——
    # 不经派发节点，直接进 fan-in 收尾（无改动重跑不调 LLM）。
    if state.get("stage") == "T4_REVIEW_RUNNING" \
            and state.get("dispatch_mode") == "reused":
        return "t4_join_review"
    return END


def route_after_t4_review_dispatch(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t4_join_review"


def route_after_t4_review_join(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    # manual 模式刚 interrupt（或并行在途）：图级自环回收尾节点，
    # 从磁盘重推导/等待结果，不重复 interrupt
    if state.get("stage") == "T4_REVIEW_RUNNING":
        return "t4_join_review"
    # T4_ACCEPTANCE_PENDING：验收记录已聚合，交由 verify 节点唯一裁决
    return "t4_verify_acceptance"


def route_after_acceptance_verify(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REDISPATCH_ACCEPTANCE":
        return "t4_plan_review"
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    return "t4_promote_and_deliver"


def route_after_repair_dispatch(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "repair_await"


def route_after_repair_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("stage") == "T3_CHECKS_PENDING":
        return "t3_checks"
    repair_target = state.get("repair_target")
    expected = state.get("expected_sections", [])
    if repair_target and repair_target not in expected:
        return "t3_checks"
    # After repair, resubmit through section check
    return "t2_submit_and_check_sections"
