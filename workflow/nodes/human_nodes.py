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
"""

import hashlib
import os
import secrets
from typing import Dict, Any
from langgraph.types import interrupt

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
            "description": "请确认学案主标题、交付模式(self_study 默认无时长上限/teaching 教学模式保留课时上限)以及输出格式(pdf/html/both)。",
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

    return {
        "user_spec": spec,
        "phase0_confirmed": True,
        "phase0_token_digest": token_digest,
        "token_digest": token_digest,
        "output_format": fmt,
        "human_interrupted": False,
        "stage": "T0_INITIALIZED",
        "routing_action": "PROCEED",
        "history": history
    }
