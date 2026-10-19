"""human_nodes.py — Phase 0 Human confirmation node with LangGraph native interrupt.
Collects and freezes title, delivery profile (default self_study), lesson budget,
and dialogue toggles.

安全模型（v3.1 加固）：
- 无论 user_spec 是否已带 main_title，只要 state["phase0_confirmed"] 不为 True，
  本节点一律 interrupt() 强制挂起，杜绝 run --title 整体跳过断点。
- 首次挂起时生成一次性确认令牌，写入 <project_root>/.langgraph/phase0_token.txt。
  resume 响应必须携带匹配 token 字段，不匹配则重新挂起。
  注意：令牌用于"抬高伪造门槛 + 审计留痕"，并非密码学保证——
  Agent 物理上有读文件能力，红线规则严禁其自行读取该文件。

F0 契约冻结（audit §4.1/§4.2/§18.3）：确认后立即构建并原子写入
<self_dir>/.pd/source-manifest.json 与 intent-contract.json。source-baseline
指向生成稿/中间稿、source 与 excluded 重复登记、文件缺失、content_policy
非法一律 HALT（ContractError 稳定错误码），不静默放行。
"""

import hashlib
import os
import secrets
from typing import Dict, Any
from langgraph.types import interrupt

from .. import contracts
from ..scope_selection import (
    RUN_SCOPES,
    ScopeSelectionError,
    normalize_targets,
)
from ..state import ShrinkCoursewareState

# 交付模式默认 self_study（自主学习自学案，不设时长上限）：
# 教学模式仅在人显式选择 teaching / classroom 时启用（保留课时预算上限，默认 90 分钟）。
DEFAULT_SPEC = {
    "main_title": "学案",
    "delivery_profile": "self_study",  # self_study(默认，无时长上限) or teaching/classroom
    "output_format": "both",  # both (全部输出 PDF 与 HTML) | pdf (仅 PDF) | html (仅 HTML)
    "lesson_minutes": None,            # self_study 默认无上限；teaching/classroom 默认 90 分钟
    "core_task_limit": 12,
    "max_student_pages": 6,
    # F0（audit §18.3）：内容契约字段。content_policy 默认 faithful（忠实转换），
    # 显式 authoring 才允许 EXTEND；source_files/excluded_sources 在 Phase 0
    # 冻结进 source-manifest.json（排除项也要登记，不得从清单消失）。
    "content_policy": "faithful",      # faithful(默认) | authoring
    "source_files": "",                # 逗号分隔的用户确认原材料路径
    "excluded_sources": "",            # 逗号分隔的排除项（可带 "路径 :: 理由"）
    "allowed_extensions": [],          # authoring/显式批准的扩展项
    "reorder_allowed": False,          # faithful 默认禁止重排 source_order
    "must_cover": "",                  # 逗号分隔的必须覆盖主题
    "student_level": "",
    # 任务1（audit §17 任务1 / §12.4）：工作集选择。full=全量（默认）、
    # focused=只做目标小节、edit=只重生成已存在小节（局部微调）。
    # 配置层保留字符串，进 state 前规范化为 list[str]；非法值报错，
    # 不静默回退 full（静默回退会让"只想改一节"的用户全量重跑）。
    "run_scope": "full",
    "target_sections": "",              # 逗号分隔，如 sec1,sec3
    "target_qids": "",                  # 逗号分隔，如 sec1-entry,sec2-q3
}

TOKEN_REL_PATH = os.path.join(".langgraph", "phase0_token.txt")


def _token_file(project_root: str) -> str:
    return os.path.join(project_root, TOKEN_REL_PATH)


def _ensure_token(project_root: str) -> str:
    """读取或生成 Phase 0 确认令牌（原子写入）。"""
    path = _token_file(project_root)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            pass
    tok = secrets.token_hex(8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%s" % (path, secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(tok + "\n")
    os.replace(tmp, path)
    return tok


def _calc_token_digest(token: str) -> str:
    """计算令牌的 sha256 摘要（格式: sha256:...）。"""
    if not token:
        return ""
    h = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    return f"sha256:{h}"


def _consume_token(project_root: str) -> None:
    """成功通过断点后销毁令牌文件，防止旧令牌重复回放。"""
    path = _token_file(project_root)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _extract_response(user_response: Any) -> Dict[str, Any]:
    """兼容两种恢复载荷：{"user_spec": {...}, "token": ...} 或直接 spec 字典。"""
    resp = user_response if isinstance(user_response, dict) else {}
    token = resp.get("token")
    if "user_spec" in resp and isinstance(resp["user_spec"], dict):
        spec_part = dict(resp["user_spec"])
        token = token or spec_part.pop("token", None)
        return {"spec": spec_part, "token": token}
    spec_part = dict(resp)
    token = token or spec_part.pop("token", None)
    return {"spec": spec_part, "token": token}


def node_phase0_breakpoint(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """Phase 0 断点：强制挂起等待真人确认学案主标题、交付模式、输出格式与令牌。"""
    history = list(state.get("history", []))
    project_root = state.get("project_root") or "."

    if not state.get("phase0_confirmed"):
        token = _ensure_token(project_root)
        token_digest = _calc_token_digest(token)
        payload = {
            "type": "PHASE0_SPEC",
            "description": "请确认学案主标题、交付模式(self_study 默认无时长上限/teaching 教学模式保留课时上限)、输出格式(pdf/html/both)、内容策略(faithful 忠实转换默认 / authoring 显式扩写)、工作集(full 全量 / focused 只做目标小节 / edit 只重生成已存在小节，focused 与 edit 需给 target_sections 或 target_qids)与原材料清单(source_files / excluded_sources，排除项也将登记)。",
            "defaults": DEFAULT_SPEC,
            "current_spec": state.get("user_spec") or {},
            "token_file": TOKEN_REL_PATH.replace(os.sep, "/"),
            "token_digest": token_digest,
            "instructions": (
                "真人用户请从令牌文件读取 Phase 0 令牌，并在恢复时随确认一并提供"
                "（resume 载荷顶层或 user_spec 内的 token 字段）。"
                "Agent 严禁自行读取令牌文件。"
            ),
        }
        # 注意 LangGraph 语义：单节点内多次 interrupt() 的再挂起不可靠
        # （恢复值按序匹配首个 interrupt，后续 interrupt 不会再次挂起）。
        # 因此令牌不匹配时返回 phase0_confirmed=False，由 route_after_phase0
        # 自环边把流程导回本节点，以"首个 interrupt"姿态重新挂起。
        history.append("Phase 0: 挂起等待用户确认学案主标题、交付模式与输出格式（需令牌）...")
        parsed = _extract_response(interrupt(payload))
        if parsed["token"] != token:
            # 拒绝伪造确认：不推进，由自环边重新挂起
            history.append("Phase 0: 令牌缺失或不匹配，拒绝伪造确认，保持挂起。")
            return {
                "phase0_confirmed": False,
                "token_digest": token_digest,
                "phase0_token_digest": token_digest,
                "stage": "PHASE0_BREAKPOINT",
                "routing_action": "PROCEED",
                "history": history
            }
        # F10：成功确认并通过断点，销毁旧令牌防止重复回放
        _consume_token(project_root)
        response_spec = parsed["spec"]
    else:
        # 已在检查点中确认过（如断点续跑）：无需再次中断
        response_spec = state.get("user_spec") or {}
        token_digest = state.get("phase0_token_digest") or state.get("token_digest") or ""

    # 合并优先级：DEFAULT_SPEC（默认 self_study）< 检查点中此前已确认值 < 本次输入，
    # 既让缺字段落到默认，又避免已确认值被默认值覆盖而静默降级。
    merged_spec = dict(DEFAULT_SPEC)
    existing = state.get("user_spec")
    if isinstance(existing, dict):
        merged_spec.update(existing)
    if isinstance(response_spec, dict):
        merged_spec.update(response_spec)
    spec = merged_spec

    prof = str(spec.get("delivery_profile", "self_study")).lower()
    if prof in ("teaching", "classroom"):
        if spec.get("lesson_minutes") is None:
            spec["lesson_minutes"] = 90
    else:
        spec["lesson_minutes"] = None

    fmt = spec.get("output_format", "both")
    history.append(f"Phase 0: 配置已锁定 — 标题: {spec.get('main_title')}, 模式: {spec.get('delivery_profile')}, 输出格式: {fmt}")

    # 任务1：工作集规范化（配置字符串 -> state list[str]；非法值 HALT 不回退 full）。
    run_scope = str(spec.get("run_scope") or "full").strip()
    try:
        target_sections = normalize_targets(spec.get("target_sections"))
        target_qids = normalize_targets(spec.get("target_qids"))
        if run_scope not in RUN_SCOPES:
            raise ScopeSelectionError(
                "BAD_RUN_SCOPE",
                "run_scope 取值 “%s” 非法，只能是: %s" % (run_scope, " / ".join(RUN_SCOPES)))
        if run_scope in ("focused", "edit") and not (target_sections or target_qids):
            raise ScopeSelectionError(
                "EMPTY_TARGETS",
                "run_scope=%s 需要 target_sections 或 target_qids 指定工作集" % run_scope)
    except ScopeSelectionError as exc:
        history.append(f"Phase 0: 工作集选择被拒绝 [{exc.code}] {exc}")
        return {
            "user_spec": spec,
            "phase0_confirmed": True,
            "phase0_token_digest": token_digest,
            "token_digest": token_digest,
            "output_format": fmt,
            "human_interrupted": False,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "Phase 0 工作集选择失败 [%s]: %s" % (exc.code, exc),
            "history": history,
        }
    # 配置层保留逗号分隔字符串（init_nodes 写回 config.yaml 用）
    spec["run_scope"] = run_scope
    spec["target_sections"] = ",".join(target_sections)
    spec["target_qids"] = ",".join(target_qids)
    if run_scope != "full":
        history.append(
            "Phase 0: 工作集已锁定 — run_scope=%s, 目标节: %s, 目标题: %s"
            % (run_scope, ",".join(target_sections) or "-", ",".join(target_qids) or "-"))

    # F0：冻结 source-manifest 与 intent-contract（audit §4.1/§4.2）。
    # 任何契约拒绝（生成稿伪装源、重复登记、文件缺失、策略非法）一律 HALT，
    # 不静默放行到下游 Agent。
    self_dir = state.get("self_dir") or os.path.join(project_root, "self")
    try:
        manifest = contracts.build_source_manifest(
            project_root=project_root,
            self_dir=self_dir,
            source_files=spec.get("source_files"),
            excluded_sources=spec.get("excluded_sources"),
            title=spec.get("main_title"),
        )
        intent_contract = contracts.build_intent_contract(user_spec=spec)
    except contracts.ContractError as exc:
        history.append(f"Phase 0: 契约冻结被拒绝 [{exc.code}] {exc.detail}")
        return {
            "user_spec": spec,
            "phase0_confirmed": True,
            "phase0_token_digest": token_digest,
            "token_digest": token_digest,
            "output_format": fmt,
            "human_interrupted": False,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "Phase 0 契约冻结失败 [%s]: %s" % (exc.code, exc.detail),
            "history": history,
        }

    manifest_path, contract_path = contracts.write_contracts(
        self_dir, manifest, intent_contract)
    history.append(
        "Phase 0: 输入清单与目标合同已冻结 — %s"
        % contracts.scope_summary(manifest, intent_contract))
    history.append(
        "Phase 0: 契约落盘 %s / %s" % (manifest_path, contract_path))

    return {
        "user_spec": spec,
        "phase0_confirmed": True,
        "phase0_token_digest": token_digest,
        "token_digest": token_digest,
        "output_format": fmt,
        "human_interrupted": False,
        "source_manifest": manifest,
        "intent_contract": intent_contract,
        "source_manifest_digest": manifest.get("digest", ""),
        "contract_digest": intent_contract.get("digest", ""),
        "content_policy": intent_contract.get("content_policy"),
        "source_files": list(intent_contract.get("source_files") or []),
        "excluded_sources": list(intent_contract.get("excluded_sources") or []),
        "run_scope": run_scope,
        "target_sections": target_sections,
        "target_qids": target_qids,
        "stage": "T0_INITIALIZED",
        "routing_action": "PROCEED",
        "history": history
    }
