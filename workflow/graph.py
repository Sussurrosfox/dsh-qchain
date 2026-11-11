"""graph.py — StateGraph assembly for QChain (题链) Pipeline.
"""

from typing import Optional, Any
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .state import ShrinkCoursewareState
from .execution_policy import (
    GRAPH_VERSION_FAST,
    GRAPH_VERSION_LEGACY,
    GRAPH_VERSIONS,
    ExecutionPolicyError,
)
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
from .nodes.fast_mapping_nodes import (
    node_fast_t1_request_unified_plan,
    node_fast_t1_await_unified_plan,
    node_fast_t1_verify_unified_mapping,
)
from .nodes.fast_review_nodes import (
    node_t4_plan_combined_review,
    node_t4_dispatch_combined_review,
    node_t4_join_combined_review,
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
    route_after_fast_checks,
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
    route_after_t2_join_check,
    # 卡15：faithful_fast 专用路由（legacy 分支完全不经过它们）
    route_after_fast_init,
    route_after_fast_mapping_request,
    route_after_fast_mapping_await,
    route_after_fast_mapping_verify,
    route_after_fast_t2_join,
    route_after_fast_review_pack,
    route_after_fast_combined_review_plan,
    route_after_fast_combined_review_join,
    route_after_fast_acceptance_verify,
)


def create_legacy_graph(checkpointer: Optional[Any] = None) -> Any:
    """Build and compile the legacy-v1 graph (既有节点名与边一律保持不变)。

    任务10：本函数就是 `legacy-v1` 执行图的唯一实现。旧检查点（无执行版本号）
    的 `next` 只能落在本图的节点名上，因此**任何节点改名/删除都会让旧会话
    无法恢复**——只允许在不改变既有节点语义的前提下增补。
    """
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
            "t2_plan_check": "t2_plan_check",
            "t1_request_source_audit": "t1_request_source_audit",
            "t1_request_coverage_plan": "t1_request_coverage_plan",
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
            # 卡05：零派发复用（dispatch_mode=reused）直收 fan-in，
            # 必须登记目标，否则运行时 KeyError: 't4_join_review'
            "t4_join_review": "t4_join_review",
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


def create_shrink_courseware_graph(checkpointer: Optional[Any] = None) -> Any:
    """向后兼容别名：缺省执行图 = legacy-v1。

    任务10：旧调用点（runner / 既有测试）一律拿 legacy 图；要 faithful_fast
    必须显式走 `create_graph_for_version("faithful-fast-v1")`，避免任何
    「顺手换图」的静默行为。
    """
    return create_legacy_graph(checkpointer=checkpointer)


class ExecutionGraphNotImplemented(RuntimeError):
    """faithful-fast-v1 执行图不可用时的显式错误（**绝不静默回退 legacy**）。

    卡15 起 fast 图已真正接通，本异常不再由图本身抛出；保留类型是为了让
    外部调用点（旧测试/宿主）仍能识别这一语义：把未实现冒充已实现会污染
    验收证据，也会让旧行为冒名顶替新执行图。
    """

    code = "EXECUTION_GRAPH_NOT_IMPLEMENTED"


def _apply_fast_join_routing(state: Any, patch: Any) -> Any:
    """把合并终审的评审结论翻译成图上的走向与门禁相位（纯函数）。

    节点本身（卡14）只表达「PASS / REVISE / HALT」的评审结论；把它翻译成
    图上的走向与门禁相位在这里做，好处是**组合路由只由 graph+edges 决定**，
    审读节点不必知道 executing graph 长什么样：

    * 全部 PASS → gate_phase="final"：随后才允许跑全量 semantic/provenance
      门禁，通过后逐节 mark checked，最后交付；
    * REVISE → REPAIR_SECTION，且只返修未通过(非 reviewed)的 scope，
      不回 T1、也不回到逐节 checker（契约 C6）；
    * 结论无法定位到切片 → HALT 交人工（绝不盲改）。
    """
    if patch.get("routing_action") == "REVISE":
        expected = list(state.get("expected_sections") or [])
        reviewed = dict(state.get("section_statuses") or {})
        reviewed.update(patch.get("section_statuses") or {})
        failing = [s for s in expected if reviewed.get(s) != "reviewed"]
        if not failing:
            patch.update({
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": "合并终审结论为 REVISE，但无法定位未通过的切片，"
                                 "停机交人工（不盲改、不放行）",
            })
            return patch
        patch.update({
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": failing[0],
            "repair_reason": str(patch.get("repair_reason")
                                 or "合并终审 REVISE：只返修未通过的切片"),
            # 只重审这些 scope；gate_phase 回到构建前，避免跳过 prebuild 门禁
            "combined_review_scopes": failing,
            "gate_phase": "prebuild",
        })
        return patch

    if patch.get("routing_action") == "PROCEED" \
            and patch.get("stage") == "T4_ACCEPTANCE_PENDING":
        patch["gate_phase"] = "final"
    return patch


def node_fast_join_combined_review(state: Any) -> Any:
    """合并终审收尾的图级包装（卡15）：调用时解析被包装节点。"""
    return _apply_fast_join_routing(
        state, dict(node_t4_join_combined_review(state) or {}))


def make_fast_join_combined_review(join_impl: Any = None) -> Any:
    """构建期绑定被包装节点：编译出的图是一份快照，不留运行期隐式耦合。"""
    impl = join_impl if join_impl is not None else node_t4_join_combined_review

    def _node(state: Any) -> Any:
        return _apply_fast_join_routing(state, dict(impl(state) or {}))

    _node.__name__ = "node_fast_join_combined_review"
    return _node


def create_fast_graph(checkpointer: Optional[Any] = None) -> Any:
    """faithful-fast-v1 执行图（契约 C6 的职责序列）。

    与 legacy 的实质差别（不是「少跑几个门禁」，而是门禁分层）：

      T1  一次派发声明三张映射表（组事务提升/冻结），不再三角色串行；
      T2  批派发 → 批收尾 → 逐节「提交为 produced + 记 prebuild 就绪 hash」，
          **图里没有逐节语义 checker**（t2_plan_check/t2_dispatch_check/
          t2_join_check 三个节点在本图中根本不存在）；
      T3  t3_checks 按 gate_phase 跑两趟：构建前只跑确定性检查（semantic 记
          pending_review、provenance 只做 writer-only），合并终审全部 PASS 之后
          再跑全量 semantic + 全量 provenance，通过才逐节 mark checked；
      T4  多节一批的合并终审，REVISE 只返修未通过的 scope。

    "没有最终审读绝不交付"由三层共同保证：图内 gate_phase=final 才进验收、
    final 全量门禁通过才 mark checked（pdstate 契约要求 checked/accepted）、
    deliver 的 MUST_PASS 逐项要求 status=="pass"（pending_review 显式阻断）。
    """
    workflow = StateGraph(ShrinkCoursewareState)

    # ---- T0：断点确认与脚手架（与 legacy 同名同实现）----
    workflow.add_node("phase0_breakpoint", node_phase0_breakpoint)
    workflow.add_node("t0_init_scaffold", node_t0_init_scaffold)

    # ---- T1：一次派发三张映射表 ----
    workflow.add_node("fast_t1_request_unified_plan",
                      node_fast_t1_request_unified_plan)
    workflow.add_node("fast_t1_await_unified_plan",
                      node_fast_t1_await_unified_plan)
    workflow.add_node("fast_t1_verify_unified_mapping",
                      node_fast_t1_verify_unified_mapping)

    # ---- T2：批写作（无逐节 checker）----
    workflow.add_node("t2_plan_dispatch", node_t2_plan_dispatch)
    workflow.add_node("t2_dispatch_sections", node_t2_dispatch_sections)
    workflow.add_node("t2_join_sections", node_t2_join_sections)
    workflow.add_node("t2_submit_and_check_sections",
                      node_t2_submit_and_check_sections)
    workflow.add_node("t2_assemble_main", node_t2_assemble_main)
    workflow.add_node("t2_shuffle_options", node_t2_shuffle_options)

    # ---- T3：分相门禁 + 构建/渲染/对比 ----
    workflow.add_node("t3_checks", node_t3_checks)
    workflow.add_node("t3_build", node_t3_build)
    workflow.add_node("t3_compare", node_t3_compare)

    # ---- T4：合并终审 → 验收 → 发布 ----
    workflow.add_node("t4_make_review_pack", node_t4_make_review_pack)
    workflow.add_node("t4_plan_combined_review", node_t4_plan_combined_review)
    workflow.add_node("t4_dispatch_combined_review",
                      node_t4_dispatch_combined_review)
    workflow.add_node("t4_join_combined_review",
                      make_fast_join_combined_review())
    workflow.add_node("t4_verify_acceptance", node_t4_verify_acceptance)
    workflow.add_node("t4_promote_and_deliver", node_t4_promote_and_deliver)

    # ---- 返修回路（与 legacy 同名同实现）----
    workflow.add_node("repair_section", node_repair_section)
    workflow.add_node("repair_await", node_repair_await)

    workflow.add_edge(START, "phase0_breakpoint")
    workflow.add_conditional_edges("phase0_breakpoint", route_after_phase0, {
        "t0_init_scaffold": "t0_init_scaffold",
        "phase0_breakpoint": "phase0_breakpoint",
        END: END,
    })

    # T1：唯一入口是统一规划节点（卡12 的组事务校验/冻结）。
    workflow.add_conditional_edges("t0_init_scaffold", route_after_fast_init, {
        "fast_t1_request_unified_plan": "fast_t1_request_unified_plan",
        END: END,
    })
    workflow.add_conditional_edges(
        "fast_t1_request_unified_plan", route_after_fast_mapping_request, {
            "fast_t1_await_unified_plan": "fast_t1_await_unified_plan",
            END: END,
        })
    workflow.add_conditional_edges(
        "fast_t1_await_unified_plan", route_after_fast_mapping_await, {
            "fast_t1_verify_unified_mapping": "fast_t1_verify_unified_mapping",
            END: END,
        })
    workflow.add_conditional_edges(
        "fast_t1_verify_unified_mapping", route_after_fast_mapping_verify, {
            "t2_plan_dispatch": "t2_plan_dispatch",
            "fast_t1_request_unified_plan": "fast_t1_request_unified_plan",
            END: END,
        })

    # T2：批派发 → 批收尾 → 逐节提交（produced + prebuild 就绪 hash）→ 循环。
    workflow.add_conditional_edges("t2_plan_dispatch", route_after_t2_plan, {
        "t2_dispatch_sections": "t2_dispatch_sections",
        "t2_submit_and_check_sections": "t2_submit_and_check_sections",
        END: END,
    })
    workflow.add_edge("t2_dispatch_sections", "t2_join_sections")
    workflow.add_conditional_edges("t2_join_sections", route_after_fast_t2_join, {
        "t2_join_sections": "t2_join_sections",
        "repair_section": "repair_section",
        "t2_submit_and_check_sections": "t2_submit_and_check_sections",
        END: END,
    })
    workflow.add_conditional_edges(
        "t2_submit_and_check_sections", route_after_section_check, {
            "t2_plan_dispatch": "t2_plan_dispatch",
            # 本图没有逐节 checker：需要审读证据的地方统一走合并终审
            "t2_plan_check": "t4_plan_combined_review",
            "t2_assemble_main": "t2_assemble_main",
            "repair_section": "repair_section",
            END: END,
        })

    workflow.add_edge("t2_assemble_main", "t2_shuffle_options")
    workflow.add_conditional_edges("t2_shuffle_options", route_after_shuffle, {
        "t3_checks": "t3_checks",
        "repair_section": "repair_section",
        END: END,
    })

    # T3：同一节点跑两趟（prebuild / final）；final 通过才允许进验收。
    workflow.add_conditional_edges("t3_checks", route_after_fast_checks, {
        "t3_build": "t3_build",
        "repair_section": "repair_section",
        # 审读证据缺失/过期 → 补派合并终审（本图不设逐节 checker）
        "t4_plan_combined_review": "t4_plan_combined_review",
        # REPLAN：来源/覆盖类问题在 fast 图里同样重新生成三表（同一次派发）
        "t1_request_source_audit": "fast_t1_request_unified_plan",
        "t1_request_coverage_plan": "fast_t1_request_unified_plan",
        # 终审相位（gate_phase=final）全量门禁通过 → 验收 → 发布
        "t4_verify_acceptance": "t4_verify_acceptance",
        END: END,
    })
    workflow.add_conditional_edges("t3_build", route_after_build, {
        "t3_compare": "t3_compare",
        "repair_section": "repair_section",
        END: END,
    })
    workflow.add_conditional_edges("t3_compare", route_after_compare, {
        "t4_make_review_pack": "t4_make_review_pack",
        "repair_section": "repair_section",
        END: END,
    })

    # T4：合并终审（多节一批）→ final 门禁 → 验收 → 发布。
    workflow.add_conditional_edges(
        "t4_make_review_pack", route_after_fast_review_pack, {
            "t4_plan_combined_review": "t4_plan_combined_review",
            END: END,
        })
    workflow.add_conditional_edges(
        "t4_plan_combined_review", route_after_fast_combined_review_plan, {
            "t4_dispatch_combined_review": "t4_dispatch_combined_review",
            END: END,
        })
    workflow.add_edge("t4_dispatch_combined_review", "t4_join_combined_review")
    workflow.add_conditional_edges(
        "t4_join_combined_review", route_after_fast_combined_review_join, {
            "t4_join_combined_review": "t4_join_combined_review",
            "repair_section": "repair_section",
            "t3_checks": "t3_checks",
            END: END,
        })
    workflow.add_conditional_edges(
        "t4_verify_acceptance", route_after_fast_acceptance_verify, {
            "t4_promote_and_deliver": "t4_promote_and_deliver",
            "t4_plan_combined_review": "t4_plan_combined_review",
            "repair_section": "repair_section",
            END: END,
        })
    workflow.add_edge("t4_promote_and_deliver", END)

    # F2：repair_section 可能直接熔断（预算耗尽/无进展）并返回
    # stage=HALTED/routing_action=HALT。旧实现这里是一条**无条件边**
    # repair_section → repair_await，于是 repair_await 在 pending_dispatch
    # 为空时把 stage 改写回 T2_SECTIONS_STAGED/PROCEED，熔断被抹掉、返修
    # 被当成成功继续提交。改为与 legacy 一致的条件边：HALTED 直接 END。
    workflow.add_conditional_edges("repair_section", route_after_repair_dispatch, {
        "repair_await": "repair_await",
        END: END,
    })
    workflow.add_conditional_edges("repair_await", route_after_repair_await, {
        "t3_checks": "t3_checks",
        "t2_submit_and_check_sections": "t2_submit_and_check_sections",
        END: END,
    })

    saver = checkpointer if checkpointer is not None else MemorySaver()
    return workflow.compile(checkpointer=saver)


def create_graph_for_version(graph_version: Optional[str] = None,
                             checkpointer: Optional[Any] = None) -> Any:
    """按**已冻结**的图版本编译对应执行图；未知版本显式拒绝，不猜回退。"""
    version = str(graph_version or GRAPH_VERSION_LEGACY).strip()
    if version == GRAPH_VERSION_LEGACY:
        return create_legacy_graph(checkpointer=checkpointer)
    if version == GRAPH_VERSION_FAST:
        return create_fast_graph(checkpointer=checkpointer)
    raise ExecutionPolicyError(
        "UNKNOWN_GRAPH_VERSION",
        "执行图版本 “%s” 未知（已知: %s）；拒绝猜测回退"
        % (version, " / ".join(GRAPH_VERSIONS)))
