"""graph.py — StateGraph assembly for QChain (题链) Pipeline.
"""

from typing import Optional, Any
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .state import ShrinkCoursewareState
from .nodes import (
    node_phase0_breakpoint,
    node_t0_init_scaffold,
    node_t1_request_source_audit,
    node_t1_await_source_audit,
    node_t1_request_dependency_plan,
    node_t1_await_dependency_plan,
    node_t1_request_coverage_plan,
    node_t1_await_coverage_plan,
    node_t1_verify_mapping,
    node_t2_plan_dispatch,
    node_t2_dispatch_sections,
    node_t2_join_sections,
    node_t2_submit_and_check_sections,
    node_t2_assemble_main,
    node_t2_shuffle_options,
    node_t3_checks,
    node_t3_build,
    node_t3_compare,
    node_t4_make_review_pack,
    node_t4_plan_review,
    node_t4_dispatch_review,
    node_t4_join_review,
    node_t4_verify_acceptance,
    node_t4_promote_and_deliver,
    node_repair_section,
    node_repair_await,
    node_t2_plan_check,
    node_t2_dispatch_check,
    node_t2_join_check
)
from .edges import (
    route_after_phase0,
    route_after_init,
    route_after_t1_request_source,
    route_after_t1_await_source,
    route_after_t1_request_dependency,
    route_after_t1_await_dependency,
    route_after_t1_request_coverage,
    route_after_mapping_await,
    route_after_mapping_verify,
    route_after_t2_plan,
    route_after_t2_dispatch,
    route_after_t2_join,
    route_after_section_check,
    route_after_assemble,
    route_after_shuffle,
    route_after_checks,
    route_after_build,
    route_after_compare,
    route_after_review_pack,
    route_after_t4_review_plan,
    route_after_t4_review_dispatch,
    route_after_t4_review_join,
    route_after_acceptance_verify,
    route_after_repair_dispatch,
    route_after_repair_await,
    route_after_t2_plan_check,
    route_after_t2_dispatch_check,
    route_after_t2_join_check
)


def create_shrink_courseware_graph(checkpointer: Optional[Any] = None) -> Any:
    """Build and compile the QChain (题链) StateGraph."""
    workflow = StateGraph(ShrinkCoursewareState)

    # 1. Register all nodes
    workflow.add_node("phase0_breakpoint", node_phase0_breakpoint)
    workflow.add_node("t0_init_scaffold", node_t0_init_scaffold)

    # T1: Mapping
    workflow.add_node("t1_request_source_audit", node_t1_request_source_audit)
    workflow.add_node("t1_await_source_audit", node_t1_await_source_audit)
    workflow.add_node("t1_request_dependency_plan", node_t1_request_dependency_plan)
    workflow.add_node("t1_await_dependency_plan", node_t1_await_dependency_plan)
    workflow.add_node("t1_request_coverage_plan", node_t1_request_coverage_plan)
    workflow.add_node("t1_await_coverage_plan", node_t1_await_coverage_plan)
    workflow.add_node("t1_verify_mapping", node_t1_verify_mapping)

    # T2: Sections & Assembly（P1：fan-out 派发链替换原串行首次写作链；
    # §6：写作轮之后是语义审读轮 Writer → Checker → Gate）
    workflow.add_node("t2_plan_dispatch", node_t2_plan_dispatch)
    workflow.add_node("t2_dispatch_sections", node_t2_dispatch_sections)
    workflow.add_node("t2_join_sections", node_t2_join_sections)
    workflow.add_node("t2_plan_check", node_t2_plan_check)
    workflow.add_node("t2_dispatch_check", node_t2_dispatch_check)
    workflow.add_node("t2_join_check", node_t2_join_check)
    workflow.add_node("t2_submit_and_check_sections", node_t2_submit_and_check_sections)
    workflow.add_node("t2_assemble_main", node_t2_assemble_main)
    workflow.add_node("t2_shuffle_options", node_t2_shuffle_options)

    # T3: Verification & Build
    workflow.add_node("t3_checks", node_t3_checks)
    workflow.add_node("t3_build", node_t3_build)
    workflow.add_node("t3_compare", node_t3_compare)

    # T4: Review, Acceptance & Deliver（P2：验收 fan-out 派发链替换原单请求验收链）
    workflow.add_node("t4_make_review_pack", node_t4_make_review_pack)
    workflow.add_node("t4_plan_review", node_t4_plan_review)
    workflow.add_node("t4_dispatch_review", node_t4_dispatch_review)
    workflow.add_node("t4_join_review", node_t4_join_review)
    workflow.add_node("t4_verify_acceptance", node_t4_verify_acceptance)
    workflow.add_node("t4_promote_and_deliver", node_t4_promote_and_deliver)

    # Repair loop
    workflow.add_node("repair_section", node_repair_section)
    workflow.add_node("repair_await", node_repair_await)

    # 2. Connect Edges
    workflow.add_edge(START, "phase0_breakpoint")

    workflow.add_conditional_edges(
        "phase0_breakpoint",
        route_after_phase0,
        {
            "t0_init_scaffold": "t0_init_scaffold",
            # 令牌未确认时自环回本节点重新 interrupt（单节点多次
            # interrupt 再挂起不可靠，必须以图级自环表达重新挂起）
            "phase0_breakpoint": "phase0_breakpoint",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t0_init_scaffold",
        route_after_init,
        {
            "t1_request_source_audit": "t1_request_source_audit",
            END: END
        }
    )

    # §5.1 三角色串行 barrier：source-auditor → dependency-planner →
    # coverage-planner → verify。每个 request 节点只写自己的产物，await
    # 节点是唯一等待/校验点（round 内无并发，链本身即 barrier）。
    workflow.add_conditional_edges(
        "t1_request_source_audit",
        route_after_t1_request_source,
        {
            "t1_await_source_audit": "t1_await_source_audit",
            END: END
        }
    )
    workflow.add_conditional_edges(
        "t1_await_source_audit",
        route_after_t1_await_source,
        {
            "t1_request_dependency_plan": "t1_request_dependency_plan",
            END: END
        }
    )
    workflow.add_conditional_edges(
        "t1_request_dependency_plan",
        route_after_t1_request_dependency,
        {
            "t1_await_dependency_plan": "t1_await_dependency_plan",
            END: END
        }
    )
    workflow.add_conditional_edges(
        "t1_await_dependency_plan",
        route_after_t1_await_dependency,
        {
            "t1_request_coverage_plan": "t1_request_coverage_plan",
            END: END
        }
    )
    workflow.add_conditional_edges(
        "t1_request_coverage_plan",
        route_after_t1_request_coverage,
        {
            "t1_await_coverage_plan": "t1_await_coverage_plan",
            END: END
        }
    )
    workflow.add_conditional_edges(
        "t1_await_coverage_plan",
        route_after_mapping_await,
        {
            "t1_verify_mapping": "t1_verify_mapping",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t1_verify_mapping",
        route_after_mapping_verify,
        {
            "t2_plan_dispatch": "t2_plan_dispatch",
            "t1_request_source_audit": "t1_request_source_audit",
            "t1_request_coverage_plan": "t1_request_coverage_plan",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_plan_dispatch",
        route_after_t2_plan,
        {
            "t2_dispatch_sections": "t2_dispatch_sections",
            "t2_submit_and_check_sections": "t2_submit_and_check_sections",
            "t2_assemble_main": "t2_assemble_main",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_dispatch_sections",
        route_after_t2_dispatch,
        {
            "t2_join_sections": "t2_join_sections",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_join_sections",
        route_after_t2_join,
        {
            # manual 模式 interrupt 后自环回本节点，从磁盘重推导结果
            # （单节点多次 interrupt 再挂起不可靠，须图级自环）
            "t2_join_sections": "t2_join_sections",
            "repair_section": "repair_section",
            # §6：写作轮全部暂存后进入语义审读轮（Writer → Checker → Gate）
            "t2_plan_check": "t2_plan_check",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_plan_check",
        route_after_t2_plan_check,
        {
            "t2_dispatch_check": "t2_dispatch_check",
            "t2_submit_and_check_sections": "t2_submit_and_check_sections",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_dispatch_check",
        route_after_t2_dispatch_check,
        {
            "t2_join_check": "t2_join_check",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_join_check",
        route_after_t2_join_check,
        {
            # manual 模式 interrupt 后自环回本节点，从磁盘重推导审读结果
            "t2_join_check": "t2_join_check",
            # 缺结果/摘要过期 → 补派审读（写作者无过错）
            "t2_plan_check": "t2_plan_check",
            # REVISE/NEEDS_EVIDENCE → 重派写作者返修
            "repair_section": "repair_section",
            "t2_submit_and_check_sections": "t2_submit_and_check_sections",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_submit_and_check_sections",
        route_after_section_check,
        {
            "t2_plan_dispatch": "t2_plan_dispatch",
            # §6：提交时语义硬闸门要求补派审读
            "t2_plan_check": "t2_plan_check",
            "t2_assemble_main": "t2_assemble_main",
            "repair_section": "repair_section",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_assemble_main",
        route_after_assemble,
        {
            "t2_shuffle_options": "t2_shuffle_options",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t2_shuffle_options",
        route_after_shuffle,
        {
            "t3_checks": "t3_checks",
            "repair_section": "repair_section",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t3_checks",
        route_after_checks,
        {
            "t3_build": "t3_build",
            "repair_section": "repair_section",
            "t1_request_source_audit": "t1_request_source_audit",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t3_build",
        route_after_build,
        {
            "t3_compare": "t3_compare",
            "repair_section": "repair_section",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t3_compare",
        route_after_compare,
        {
            "t4_make_review_pack": "t4_make_review_pack",
            "repair_section": "repair_section",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t4_make_review_pack",
        route_after_review_pack,
        {
            "t4_plan_review": "t4_plan_review",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t4_plan_review",
        route_after_t4_review_plan,
        {
            "t4_dispatch_review": "t4_dispatch_review",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t4_dispatch_review",
        route_after_t4_review_dispatch,
        {
            "t4_join_review": "t4_join_review",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t4_join_review",
        route_after_t4_review_join,
        {
            # manual 模式 interrupt 后自环回本节点，从磁盘重推导结果
            # （单节点多次 interrupt 再挂起不可靠，须图级自环）
            "t4_join_review": "t4_join_review",
            "t4_verify_acceptance": "t4_verify_acceptance",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "t4_verify_acceptance",
        route_after_acceptance_verify,
        {
            "t4_promote_and_deliver": "t4_promote_and_deliver",
            "t4_plan_review": "t4_plan_review",
            "repair_section": "repair_section",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "repair_section",
        route_after_repair_dispatch,
        {
            "repair_await": "repair_await",
            END: END
        }
    )

    workflow.add_conditional_edges(
        "repair_await",
        route_after_repair_await,
        {
            "t2_submit_and_check_sections": "t2_submit_and_check_sections",
            "t3_checks": "t3_checks",
            END: END
        }
    )

    workflow.add_edge("t4_promote_and_deliver", END)

    saver = checkpointer if checkpointer is not None else MemorySaver()
    return workflow.compile(checkpointer=saver)
