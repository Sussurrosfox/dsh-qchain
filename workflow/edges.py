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
    return "t1_request_mapping"


def route_after_mapping_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t1_verify_mapping"


def route_after_mapping_verify(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPLAN":
        return "t1_request_mapping"
    return "t2_request_sections"


def route_after_section_request(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("active_section") is None:
        return "t2_assemble_main"
    return "t2_await_sections"


def route_after_section_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t2_submit_and_check_sections"


def route_after_section_check(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"

    # Check if there are still pending sections
    expected = state.get("expected_sections", ["sec1"])
    statuses = state.get("section_statuses", {})
    has_pending = any(statuses.get(s) not in ("checked", "accepted") for s in expected)
    if has_pending:
        return "t2_request_sections"

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
        return "t1_request_mapping"
    if state.get("routing_action") == "REPAIR_SECTION":
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
    return "t4_request_acceptance"


def route_after_acceptance_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t4_verify_acceptance"


def route_after_acceptance_verify(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REDISPATCH_ACCEPTANCE":
        return "t4_request_acceptance"
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
    # After repair, resubmit through section check
    return "t2_submit_and_check_sections"
