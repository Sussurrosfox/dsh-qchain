"""edges.py — Conditional routing logic for Shrink Courseware Pipeline.
"""

from typing import Literal
from langgraph.graph import END
from .state import ShrinkCoursewareState

#: 卡15：faithful_fast（轻量图）执行档位标识——决定「完成」判据与 T3 门禁分层
EXECUTION_PROFILE_FAST = "faithful_fast"


def _is_fast(state: ShrinkCoursewareState) -> bool:
    """当前会话是否走轻量图（缺省 = legacy，旧会话语义不变）。"""
    return str(state.get("execution_profile") or "") == EXECUTION_PROFILE_FAST


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


# --- 卡 12：faithful_fast 的一次规划产三表 ---------------------------------
def route_after_fast_mapping_request(state: ShrinkCoursewareState) -> str:
    """统一规划请求之后：HALT 收口，否则进入等待（一次派发三件产物）。"""
    if state.get("stage") == "HALTED":
        return END
    return "fast_t1_await_unified_plan"


def route_after_fast_mapping_await(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "fast_t1_verify_unified_mapping"


def route_after_fast_mapping_verify(state: ShrinkCoursewareState) -> str:
    """组事务校验之后：通过 → T2；REPLAN → 重发同一张统一规划单。

    faithful_fast 只有**一个** T1 派发口：局部修规划不回到某个人为拆出的 round，
    而是在同一张统一派发上重来（上一轮三表留在 staging，派发单会带上打回原因，
    未变更的表原样保留，只有需要改的表会被重写）。
    """
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPLAN":
        return "fast_t1_request_unified_plan"
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
    done = ("checked", "accepted", "reused", "skipped")
    if _is_fast(state):
        # 卡15：faithful_fast 的提交只做到「构建前就绪」（produced + 逐节 hash），
        # 终审通过前不产生 checked。若仍以 checked 为完成判据，作者循环会把
        # 已产出的节当 pending 反复重写（GRAPH-04）。
        done = done + ("prebuild_ready",)
    has_pending = any(statuses.get(s) not in done for s in expected)
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
        # 卡 07：覆盖/预算类问题只回 coverage-planner（来源哈希未变，不重跑
        # source-auditor）；source-map 类问题（来源/映射契约）仍回 source-auditor。
        if state.get("repair_target") == "coverage-map.md":
            return "t1_request_coverage_plan"
        return "t1_request_source_audit"
    if state.get("routing_action") == "REPAIR_SECTION":
        if state.get("semantic_repair_target"):
            return "t2_plan_check"
        return "repair_section"
    return "t3_build"


def route_after_fast_checks(state: ShrinkCoursewareState) -> str:
    """fast 图的 t3_checks 路由：legacy 语义 + 终审相位的验收出口。

    legacy 的 ``route_after_checks`` 逐字保持原样（旧检查点的 next 与本图无关），
    新图多出来的两个语义单列在此，避免把只有 faithful-fast-v1 才存在的目标
    塞进 legacy 的 path_map：

    * 审读证据缺失/过期（semantic_repair_target）→ 补派**合并终审**，
      而不是回到逐节 checker（fast 图里根本没有那三个节点）；
    * 终审相位（gate_phase=final）全量门禁 PASS → 验收 → 发布。
      构建前（prebuild）这一趟无论如何只去 build，绝不提前放行。
    """
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION" \
            and state.get("semantic_repair_target"):
        return "t4_plan_combined_review"
    if state.get("routing_action") == "PROCEED" \
            and _is_fast(state) and state.get("gate_phase") == "final":
        return "t4_verify_acceptance"
    return route_after_checks(state)


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


# ---------------------------------------------------------------------------
# 卡15：faithful_fast（faithful-fast-v1）专用路由
#
# 与 legacy 的差别只有三处：
#   1) T1 后进入统一规划（一张派发单三表），不回到某个人为拆出的 round；
#   2) T2 不做逐节语义审读（checker 节点在图里不存在），批收尾直接进提交；
#   3) T4 用「合并终审」一批复查多节，终审通过后才允许跑 final 门禁。
# 全部改动都以 execution_profile=faithful_fast 为条件，legacy 分支逐字节不变。
# ---------------------------------------------------------------------------


def route_after_fast_init(state: ShrinkCoursewareState) -> str:
    """T0 之后：fast 的唯一 T1 入口是统一规划节点。"""
    if state.get("stage") == "HALTED":
        return END
    return "fast_t1_request_unified_plan"


def route_after_fast_t2_join(state: ShrinkCoursewareState) -> str:
    """批收尾：返修 / 自环等待 / 提交（无逐节 checker，不进 t2_plan_check）。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    if state.get("stage") == "T2_SECTIONS_RUNNING":
        return "t2_join_sections"
    return "t2_submit_and_check_sections"


def route_after_fast_review_pack(state: ShrinkCoursewareState) -> str:
    if state.get("stage") == "HALTED":
        return END
    return "t4_plan_combined_review"


def route_after_fast_combined_review_plan(state: ShrinkCoursewareState) -> str:
    """合并终审计划：有派发单 → 派发；计划失败 → 收口。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("stage") == "T4_COMBINED_REVIEW_PENDING":
        return "t4_dispatch_combined_review"
    return END


def route_after_fast_combined_review_join(state: ShrinkCoursewareState) -> str:
    """合并终审收尾：通过 → final 门禁；返修 → 修相关 scope；在途 → 自环。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    if state.get("stage") == "T4_COMBINED_REVIEW_RUNNING":
        return "t4_join_combined_review"
    if state.get("routing_action") == "PROCEED" \
            and state.get("gate_phase") == "final":
        return "t3_checks"
    return END


def route_after_fast_acceptance_verify(state: ShrinkCoursewareState) -> str:
    """交付验收：返修 → 修节；验收证据需补派 → 重排合并终审；否则发布。"""
    if state.get("stage") == "HALTED":
        return END
    if state.get("routing_action") == "REDISPATCH_ACCEPTANCE":
        return "t4_plan_combined_review"
    if state.get("routing_action") == "REPAIR_SECTION":
        return "repair_section"
    return "t4_promote_and_deliver"

