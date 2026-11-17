#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""recovery.py — RC-3：HALT 的官方恢复原语（受支持通道，不用二进制手术）。

问题背景
--------
旧实现里流水线一旦 HALT 就是死路：``agent_nodes.py`` 自己写着「HALTED 后
resume 是 no-op，只能删 checkpoint」。于是本次 run 里凡是「误派单 / 预算被非
内容问题吃光 / 想改道」都要人工现写 sqlite 手术脚本（3 个），既不安全也不可
复用，且没有任何台账。

本模块把这类恢复收敛成**四个闭集动作 + 真人批准 + 备份 + 台账**：

  abandon_dispatch   撤销当前挂起的派发单（误派单），回到该门禁重跑
  reroute_to         改道到指定节点（如 t3_checks / t2_submit_and_check_sections）
  relax_budget       放宽返修预算上限后继续（数值写入冻结执行契约）
  retry_gate         重跑当前门禁（不改内容、不改预算）

硬约束
------
* 每个动作都必须给 ``approved_by``（真人署名）——Agent 不得自我豁免。
* 动 checkpoint 之前先备份整个 sqlite 文件（``.bak-recover-<ts>``）。
* 台账逐条落 ``.pd/recoveries/<ts>-<action>.json``（动作、批准人、理由、
  新旧 checkpoint id、patch 内容），可审计、可事后追责。
* 只经 ``langgraph`` 公开 API（``update_state``）改状态，不解析 msgpack。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

HALT_ACTIONS: Tuple[str, ...] = (
    "abandon_dispatch",
    "reroute_to",
    "relax_budget",
    "retry_gate",
)

# 每个动作允许改道到的节点（闭集；不猜、不自由跳）
REROUTE_TARGETS: Tuple[str, ...] = (
    "t1_request_source_audit",
    "t2_submit_and_check_sections",
    "t3_checks",
    "t3_build",
    "t4_make_review_pack",
    "repair_deterministic",
)


class RecoveryError(RuntimeError):
    """恢复请求本身非法或被拒绝（未动任何状态）。"""


def _now() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def build_halt_payload(*, cause: str, reason: str, gate: str = "",
                       target: Optional[str] = None,
                       dispatch_id: Optional[str] = None,
                       budget: Optional[Dict[str, Any]] = None,
                       extra: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """构造结构化 HALT 载荷（节点把 payload 放进 state['halt_payload']）。

    ``cause`` 是**闭集**：dispatch_missing / no_change / budget_exhausted /
    infrastructure / unclassified / route_gap / contract / gate_reject。
    """
    cause = str(cause or "unclassified")
    actions: List[Dict[str, Any]] = []
    if dispatch_id:
        actions.append({"action": "abandon_dispatch",
                        "why": "当前派发单是误派/无法完成时撤销它，回到门禁重跑",
                        "dispatch_id": dispatch_id})
    actions.append({"action": "retry_gate",
                    "why": "改了外部条件（补资产/装字库/清缓存）后重跑同一门禁"})
    for tgt in ("t3_checks", "t2_submit_and_check_sections"):
        actions.append({"action": "reroute_to", "target": tgt,
                        "why": "确定该走另一条路径时改道（需说明理由）"})
    if budget is not None:
        actions.append({"action": "relax_budget",
                        "why": "返修额度被非内容问题吃光时抬限额",
                        "current": budget})
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "cause": cause,
        "gate": gate,
        "target": target,
        "dispatch_id": dispatch_id,
        "reason": reason,
        "actions": actions,
        "how": ("python scripts/pdstate.py <self_dir> recover "
                "--action <动作> --approved-by <真人> "
                "[--target <节点>] [--reason <理由>] [--dispatch-id <id>]"),
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# 动作 → update_state 补丁（纯函数，便于测试）
# ---------------------------------------------------------------------------

def _patch_for(action: str, *, self_dir: str, target: Optional[str],
               dispatch_id: Optional[str], budgets: Dict[str, int],
               reason: str, halt_state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """返回 (as_node, patch)。as_node 决定从哪个节点之后续跑。"""
    if action not in HALT_ACTIONS:
        raise RecoveryError("未知恢复动作 %r（可用: %s）"
                            % (action, " / ".join(HALT_ACTIONS)))

    clear = {"error_message": None, "halt_payload": None,
             "routing_action": "PROCEED"}

    if action == "abandon_dispatch":
        cur = str(halt_state.get("pending_dispatch") or "")
        want = str(dispatch_id or "")
        if want and cur and os.path.basename(cur) and want not in cur:
            raise RecoveryError(
                "要求的派发单 %s 与当前挂起 %s 不一致（拒绝撤销别的单）"
                % (want, cur))
        if not cur and not want:
            raise RecoveryError("abandon_dispatch 需要说明要撤销的派发单")
        # 撤销后回到「该做什么就做什么」的门禁决策点
        patch = dict(clear)
        patch.update({"stage": "T3_CHECKS_PENDING",
                      "pending_dispatch": None,
                      "repair_target": None,
                      "semantic_repair_target": None})
        return "repair_section", patch

    if action == "reroute_to":
        if not target:
            raise RecoveryError("reroute_to 需要 --target")
        if target not in REROUTE_TARGETS:
            raise RecoveryError(
                "--target %r 不在闭集内（可用: %s）"
                % (target, " / ".join(REROUTE_TARGETS)))
        patch = dict(clear)
        patch.update({"stage": "ROUTED", "pending_dispatch": None,
                      "repair_target": None, "reroute_target": target,
                      "repair_failure_cause": "manual_reroute"})
        return "t2_shuffle_options", patch

    if action == "relax_budget":
        per = int(budgets.get("per_scope") or 0)
        total = int(budgets.get("total") or 0)
        if per <= 0 and total <= 0:
            raise RecoveryError(
                "relax_budget 需要给出新的上限（per_scope/total 至少一个 > 0）")
        cfg = dict(halt_state.get("execution_config") or {})
        if per > 0:
            cfg["repair_max_per_scope"] = max(
                per, int(cfg.get("repair_max_per_scope") or 0))
        if total > 0:
            cfg["repair_max_total"] = max(
                total, int(cfg.get("repair_max_total") or 0))
        patch = dict(clear)
        patch.update({"stage": "T3_CHECKS_PENDING", "execution_config": cfg,
                      "repair_halt": None, "pending_dispatch": None,
                      "repair_failure_cause": "budget_relaxed"})
        return "repair_await", patch

    # retry_gate
    patch = dict(clear)
    patch.update({"stage": "T3_CHECKS_PENDING", "pending_dispatch": None,
                  "repair_failure_cause": "gate_retried"})
    return "repair_await", patch


def recover(self_dir: str, *, action: str, approved_by: str,
            thread_id: str = "qchain_session",
            db_path: Optional[str] = None, target: Optional[str] = None,
            dispatch_id: Optional[str] = None,
            budgets: Optional[Dict[str, int]] = None, reason: str = "",
            dry_run: bool = False) -> Dict[str, Any]:
    """执行一次受支持的 HALT 恢复；返回台账 dict（同时落盘）。"""
    approved_by = (approved_by or "").strip()
    if not approved_by:
        raise RecoveryError("恢复必须由真人批准：缺 --approved-by（Agent 不得自我豁免）")
    self_dir = os.path.abspath(self_dir)
    if not os.path.isdir(self_dir):
        raise RecoveryError("工作区不存在: %s" % self_dir)
    db_path = db_path or os.path.join(self_dir, ".langgraph",
                                      "courseware_checkpoints.sqlite")
    if not os.path.isfile(db_path):
        raise RecoveryError("检查点数据库不存在: %s" % db_path)

    # 延迟导入：脚本上下文才需要 langgraph
    from .checkpoints import SqliteSaver
    from .runner import read_frozen_execution
    from . import execution_policy as ep
    from .graph import create_graph_for_version

    # 冻结图版本：优先按契约解析；契约漂移/缺失时退回 legacy（恢复本身
    # 常常正是因为契约与现场不一致，此处不能反过来把恢复卡死）。
    graph_version = None
    try:
        frozen = read_frozen_execution(db_path, thread_id=thread_id) or {}
        policy = ep.resolve_resume(frozen, frozen.get("intent_contract"))
        graph_version = policy.get("graph_version")
    except Exception as exc:
        ledger_warn = "执行契约解析失败（按 legacy 图恢复）: %s" % exc
    else:
        ledger_warn = None

    saver = SqliteSaver(db_path)
    app = create_graph_for_version(graph_version, checkpointer=saver)
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = app.get_state(cfg)
    if not snap or not snap.values:
        raise RecoveryError("读不到 thread %s 的检查点状态" % thread_id)
    state = dict(snap.values)
    if str(state.get("stage") or "") != "HALTED" and not dry_run:
        raise RecoveryError(
            "当前 stage=%s 不是 HALTED，拒绝恢复（避免动运行中的图）"
            % state.get("stage"))

    as_node, patch = _patch_for(
        action, self_dir=self_dir, target=target, dispatch_id=dispatch_id,
        budgets=budgets or {}, reason=reason, halt_state=state)

    ledger: Dict[str, Any] = {
        "schema_version": 1,
        "action": action,
        "approved_by": approved_by,
        "reason": reason,
        "self_dir": self_dir,
        "thread_id": thread_id,
        "db_path": db_path,
        "before_checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
        "before_stage": state.get("stage"),
        "as_node": as_node,
        "patch": patch,
        "dry_run": bool(dry_run),
        "graph_version": graph_version,
    }
    if ledger_warn:
        ledger["contract_warning"] = ledger_warn
    if dry_run:
        ledger["applied"] = False
        return ledger

    # 备份 → 改状态 → 记账
    ts = _now()
    bak = "%s.bak-recover-%s" % (db_path, ts)
    shutil.copy2(db_path, bak)
    ledger["backup"] = bak

    new_cfg = app.update_state(cfg, patch, as_node=as_node)
    ledger["after_checkpoint_id"] = new_cfg["configurable"].get("checkpoint_id")
    ledger["next"] = list(app.get_state(new_cfg).next or [])
    ledger["applied"] = True

    out_dir = os.path.join(self_dir, ".pd", "recoveries")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s-%s.json" % (ts, action))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    ledger["ledger"] = out
    return ledger
