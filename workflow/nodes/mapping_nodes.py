"""mapping_nodes.py — T1 Mapping & Material Index nodes.
Generates and verifies source-map.md, dependency-map.md, coverage-map.md,
and builds .pd/materials.json machine index.

F2（audit §18.5）：T1 映射契约带来源身份。规划前先由确定性脚本刷新
source-inventory.json（F1），映射 Agent 的输入包含冻结清单而非宽泛的
"根据原材料"；source-map.md 逐行 source_id -> output_scope -> action ->
status -> reason；faithful 模式下 EXTEND 自动判 PENDING_APPROVAL，
不得以 self_study/知识密度/认知链完整为由新增未批准内容。

§5.1（audit）：三角色拆分 + barrier。原「一个 Agent 一次生成三张映射表」
mapping_nodes.py:54-114 的职责/缓存边界不清问题，拆为三个串行 round：

  1. source-auditor      → 只生成 source-map.md（原文骨架映射）
  2. dependency-planner  → 只生成 dependency-map.md（前置依赖图）
  3. coverage-planner    → 读前两者，只生成 coverage-map.md（覆盖与预算）

每个 round 独立派发单、独立缓存键（stage+scope+input_digest），下游
round 的 context 携带上游产物 digest——上游变更即失效下游缓存
（§12.3 三张映射分三个可独立失效节点）。串行链本身即 barrier：
下游开派前上游产物必须已在盘且通过 attestation。
"""

import importlib
import os
import re
import sys
from typing import Dict, Any, List, Optional, Tuple
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner
from ..adapters.artifact_cache import ArtifactCache, config_digest, prompt_version
from .agent_nodes import build_request, write_request_file, run_dispatch

# F2 映射契约状态词表（audit §18.4；check_fidelity.py 同源）
SOURCE_MAP_STATUSES = ("PRESERVED", "EXPLAINED", "BRIDGED",
                       "EXCLUDED_BY_USER", "PENDING_APPROVAL")
SOURCE_MAP_ACTIONS = ("PRESERVE", "EXPLAIN", "BRIDGE", "EXTEND")
_RE_SOURCE_MAP_ROW = re.compile(
    r"^\s*(SRC-\d+)\s*->\s*(\S+)\s*->\s*(\w+)\s*->\s*(\w+)\s*->\s*(.+?)\s*$")

# §5.1 三角色 round 定义（stage 键 → (缓存 scope, 产物清单)）。
# t1_round_digests 以 stage 为键记录本轮各 round 的 input_digest，
# 供 verify 节点分键存入派生物缓存、失败时整链进入缓存黑名单。
T1_ROUNDS: Dict[str, Tuple[str, List[str]]] = {
    "T1_SOURCE_AUDIT": ("source-map", ["source-map.md"]),
    "T1_DEPENDENCY": ("dependency-map", ["dependency-map.md"]),
    "T1_COVERAGE": ("coverage-map", ["coverage-map.md"]),
}

# §5.1 三角色名（audit §5.1：source-auditor / dependency-planner /
# coverage-planner，各自只写自己的产物）
ROLE_SOURCE_AUDITOR = "来源审计专员（source-auditor）"
ROLE_DEPENDENCY_PLANNER = "前置依赖规划专员（dependency-planner）"
ROLE_COVERAGE_PLANNER = "覆盖与预算规划专员（coverage-planner）"


def _load_inventory_module():
    """惰性导入 scripts/source_inventory.py（避免 workflow 包依赖 scripts 包）。"""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("scripts.source_inventory")


def _ensure_source_inventory(self_dir: str, project_root: str
                             ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """F1+F2：确保 .pd/source-inventory.json 存在且与冻结 manifest 一致。

    返回 (inventory, error)。清单是派生数据：manifest 未变但清单缺失/过期
    时确定性重建；manifest 缺失（Phase 0 契约未冻结）属硬错误。
    """
    try:
        inv_mod = _load_inventory_module()
        manifest = inv_mod._load_manifest(
            os.path.join(self_dir, ".pd", "source-manifest.json"))
        inventory = inv_mod.build_inventory(manifest, project_root)
        if inv_mod.inventory_is_stale(self_dir, manifest):
            inv_mod.write_inventory(self_dir, inventory)
        return inventory, None
    except inv_mod.InventoryError as exc:
        return None, "%s: %s" % (exc.code, exc.detail)
    except Exception as exc:  # 导入/IO 等意外一律显式失败，不静默跳过
        return None, "SOURCE_INVENTORY_ERROR: %s" % exc


def _validate_source_map(self_dir: str) -> List[str]:
    """F2 结构校验：source-map.md 每行满足 source_id -> scope -> action ->
    status -> reason 契约（深度语义校验由 F3 check_fidelity.py 负责）。"""
    path = os.path.join(self_dir, "source-map.md")
    if not os.path.isfile(path):
        return ["source-map.md 缺失（F2 映射契约要求逐行来源映射）"]
    errors: List[str] = []
    rows = 0
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "<!--", "|")):
                continue
            if "->" not in stripped:
                continue  # 说明性文字行
            rows += 1
            m = _RE_SOURCE_MAP_ROW.match(stripped)
            if not m:
                errors.append(
                    "source-map.md:%d 映射行格式应为 "
                    "SRC-xxx -> output_scope -> action -> status -> reason"
                    % lineno)
                continue
            _, _scope, action, status, _reason = m.groups()
            if action.upper() not in SOURCE_MAP_ACTIONS:
                errors.append("source-map.md:%d action=%s 非法（应为 %s）"
                              % (lineno, action, "/".join(SOURCE_MAP_ACTIONS)))
            if status.upper() not in SOURCE_MAP_STATUSES:
                errors.append("source-map.md:%d status=%s 非法（应为 %s）"
                              % (lineno, status,
                                 "/".join(SOURCE_MAP_STATUSES)))
    if rows == 0:
        errors.append("source-map.md 没有任何 SRC-xxx 映射行（F2 契约）")
    return errors


def _extract_expected_sections(self_dir: str, coverage_map_path: str) -> List[str]:
    """Extract section list from coverage-map.md table rows or fallback to sec*.tex found or default ['sec1'].
    Supports adaptive alphanumeric naming like sec0, sec1, sec3a, sec3b, sec4.
    """
    def sec_sort_key(s: str):
        m = re.match(r"^sec(\d+)(.*)$", s)
        if m:
            return (int(m.group(1)), m.group(2))
        return (999, s)

    sections = []
    if os.path.isfile(coverage_map_path):
        try:
            with open(coverage_map_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 严格从表格行中提取小节代号（避免正文/说明中提及已废弃切片导致复活幽灵切片）
            found = set()
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("|") and not line.startswith("|---"):
                    for match in re.finditer(r"\b(sec\d+[a-z]?)\b", line):
                        found.add(match.group(1))
            if found:
                sections = sorted(list(found), key=sec_sort_key)
        except Exception:
            pass

    if not sections:
        # Check if sec*.tex files already exist in self_dir
        sec_files = [f[:-4] for f in os.listdir(self_dir)
                     if f.startswith("sec") and f.endswith(".tex") and f != "sec_template.tex"]
        if sec_files:
            sections = sorted(sec_files, key=sec_sort_key)

    if not sections:
        sections = ["sec1"]

    return sections


def _artifact_digest(self_dir: str, rel: str) -> str:
    """产物内容摘要（sha256:…）；缺失返回空串（调用方按缺失处理）。"""
    path = os.path.join(self_dir, rel.replace("/", os.sep))
    if not os.path.isfile(path):
        return ""
    try:
        import hashlib
        with open(path, "rb") as f:
            return "sha256:" + hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _cache_blocklisted(state: ShrinkCoursewareState, digest: str) -> bool:
    """缓存黑名单（audit §10 反模式治理）：已知产生不合格产物的
    input_digest 不得再命中缓存——否则 REPLAN 回路会反复还原同一份
    不合格产物直至熔断，重派永远不发生。"""
    if not digest:
        return False
    return digest in (state.get("dispatch_blocklist") or [])


def _request_round(state: ShrinkCoursewareState, *, stage: str, role: str,
                   prompt_file: str, artifacts: List[str],
                   instruction: str, command: str, context: Dict[str, Any],
                   history_line: str) -> Dict[str, Any]:
    """§5.1 通用 round 派发：build_request → 黑名单跳过 → 派生物缓存
    lookup/restore → 写派发单。缓存命中返回 pending_dispatch=None
    （await 节点见空即放行进下一 round/verify）。"""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    cache_scope = T1_ROUNDS[stage][0]
    req = build_request(
        state,
        stage=stage,
        role=role,
        prompt_file=prompt_file,
        artifacts=artifacts,
        instruction=instruction,
        command=command,
        context=context,
    )

    round_digests = dict(state.get("t1_round_digests") or {})
    if not _cache_blocklisted(state, req["input_digest"]):
        cache = ArtifactCache(self_dir)
        hit = cache.lookup(
            stage=stage, scope=cache_scope, input_digest=req["input_digest"],
            config_digest=config_digest(self_dir),
            prompt_version=prompt_version(self_dir, prompt_file),
            artifacts=artifacts,
        )
        if hit is not None and cache.restore(hit):
            history = list(state.get("history", []))
            history.append(
                f"T1[{stage}]: 派生物缓存命中（input_digest "
                f"{req['input_digest'][:12]}…），{cache_scope} 自缓存还原，跳过派发")
            round_digests[stage] = req["input_digest"]
            return {
                "pending_dispatch": None,
                "t1_round_digests": round_digests,
                "stage": "T1_MAPPING_PENDING",
                "history": history
            }

    req_ref = write_request_file(self_dir, req)
    history = list(state.get("history", []))
    history.append(history_line % {"ref": req_ref})
    round_digests[stage] = req["input_digest"]
    return {
        "pending_dispatch": req,
        "t1_round_digests": round_digests,
        "stage": "T1_MAPPING_PENDING",
        "history": history
    }


def _source_map_brief(self_dir: str) -> List[Dict[str, str]]:
    """source-map.md 的机器可读简报（喂给下游 round 的 context）。"""
    path = os.path.join(self_dir, "source-map.md")
    rows: List[Dict[str, str]] = []
    if not os.path.isfile(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _RE_SOURCE_MAP_ROW.match(line.strip())
                if m:
                    sid, scope, action, status, reason = m.groups()
                    rows.append({
                        "source_id": sid, "output_scope": scope,
                        "action": action, "status": status, "reason": reason,
                    })
    except OSError:
        pass
    return rows


def node_t1_request_source_audit(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 1：source-auditor —— 只生成 source-map.md。"""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    project_root = state.get("project_root") or os.path.dirname(self_dir)
    artifacts = T1_ROUNDS["T1_SOURCE_AUDIT"][1]

    # F1→F2：先冻结/刷新 source-inventory.json，映射 Agent 的输入必须包含
    # 逐单元清单（source_id/locator/kind/disposition），而非宽泛的“根据原材料”。
    inventory, inv_error = _ensure_source_inventory(self_dir, project_root)
    if inventory is None:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T1 来源清单冻结失败（%s）" % inv_error,
            "history": list(state.get("history", [])) + [
                "T1: 来源清单不可用 — %s" % inv_error],
        }
    inventory_digest = inventory.get("digest", "")

    user_spec = state.get("user_spec") or {}
    contract = state.get("intent_contract") or {}
    content_policy = str(contract.get("content_policy")
                         or user_spec.get("content_policy") or "faithful")
    reorder_allowed = bool(contract.get("reorder_allowed", False))
    allowed_extensions = contract.get("allowed_extensions") or []

    inventory_brief = [
        {
            "source_id": u.get("source_id"),
            "source_file": u.get("source_file"),
            "locator": u.get("locator"),
            "kind": u.get("kind"),
            "disposition": u.get("disposition"),
            "source_order": u.get("source_order"),
        }
        for u in inventory.get("units", [])
    ]

    instruction = (
        "你是来源审计专员（source-auditor），只负责生成 source-map.md 这一份"
        "原文骨架映射；前置依赖图与覆盖预算由后续专员负责，不要越权生成。\n\n"
        "【F2 来源忠实契约（强制）】：\n"
        "1. source-map.md 必须逐行填写：source_id -> output_scope -> action -> status -> reason；"
        "status 只能是 PRESERVED / EXPLAINED / BRIDGED / EXCLUDED_BY_USER / PENDING_APPROVAL；\n"
        "2. 每个计划输出的 section/qid 必须能回指 source_ids；\n"
        "3. new_objects 非空时只能登记 BRIDGE 或 EXTEND 请求，不得直接展开正文；\n"
        "4. 没有 source_id 的主线内容不能进入 faithful 正文；\n"
        "5. 不得以 self_study、知识密度或“认知链完整”为理由新增未批准内容。\n"
    )
    if content_policy == "faithful":
        instruction += (
            "6. faithful 模式下 EXTEND 自动判为 PENDING_APPROVAL（停在人工确认点），"
            "不得交给 T2 继续生成；\n"
            "7. 必须保持 source_order，除非合同显式 reorder_allowed=true；\n"
            "8. EXCLUDED_BY_USER 的单元不得被正文引用，也不得计入覆盖缺口。\n"
        )
    else:
        instruction += (
            "6. authoring 模式：仅 allowed_extensions 清单内的扩展才可登记为 EXTEND；"
            "清单外扩展仍判 PENDING_APPROVAL；\n"
            "7. authoring 模式允许重排章节（reorder_allowed=%s），但每个输出单元仍需 source_ids。\n"
            % ("true" if reorder_allowed else "false")
        )

    return _request_round(
        state,
        stage="T1_SOURCE_AUDIT",
        role=ROLE_SOURCE_AUDITOR,
        prompt_file="research_prompt.md",
        artifacts=artifacts,
        instruction=instruction,
        command="python scripts/evidence.py materials-index self/ --out self/.pd/materials.json",
        context={
            "input_path": state.get("input_path", ""),
            "user_spec": user_spec,
            "content_policy": content_policy,
            "reorder_allowed": reorder_allowed,
            "allowed_extensions": allowed_extensions,
            "source_inventory_digest": inventory_digest,
            "source_inventory": inventory_brief,
        },
        history_line="T1[source-auditor]: 来源清单已冻结（%d 单元），派发 "
                     "source-map.md 构建任务（内容策略 %s，派发单: %%(ref)s）"
                     % (len(inventory_brief), content_policy),
    )


def node_t1_await_source_audit(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 1 await：等 source-map.md 落盘并通过基础校验。"""
    req = state.get("pending_dispatch")
    if not req:
        return {"stage": "T1_SOURCE_AUDITED", "routing_action": "PROCEED"}
    return run_dispatch(
        state,
        req,
        success_stage="T1_SOURCE_AUDITED",
        success_history="T1[source-auditor]: source-map.md 已产出并完成校验"
    )


def node_t1_request_dependency_plan(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 2：dependency-planner —— 只生成 dependency-map.md。

    barrier：上游 source-map.md 必须已在盘（round 1 产物），否则硬失败；
    context 携带其 digest 使上游变更失效本 round 缓存。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    artifacts = T1_ROUNDS["T1_DEPENDENCY"][1]
    source_map_digest = _artifact_digest(self_dir, "source-map.md")
    if not source_map_digest:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T1[dependency-planner] 上游产物 source-map.md 缺失"
                             "（barrier 破裂：round 1 未完成）",
            "history": list(state.get("history", [])) + [
                "T1[dependency-planner]: barrier 破裂 — source-map.md 不存在"],
        }

    user_spec = state.get("user_spec") or {}
    instruction = (
        "你是前置依赖规划专员（dependency-planner），只负责生成 "
        "dependency-map.md 这一份前置依赖表；source-map.md 已由来源审计专员"
        "冻结（不要改写它），覆盖与预算规划由后续专员负责。\n\n"
        "【产物契约】：\n"
        "1. 以 Markdown 表格逐行登记：| 节点 | 前置节点 | 说明 | 是否必需 |；\n"
        "2. 节点只允许登记计划生成的 sec 节名（如 sec1/sec2/sec3a）；\n"
        "3. 前置节点必须来自已冻结的 source-map.md 输出范围或前置知识卡，"
        "不得凭空发明节点；\n"
        "4. 不要生成 coverage-map.md，不要改写 source-map.md。\n"
    )

    return _request_round(
        state,
        stage="T1_DEPENDENCY",
        role=ROLE_DEPENDENCY_PLANNER,
        prompt_file="research_prompt.md",
        artifacts=artifacts,
        instruction=instruction,
        command="python scripts/evidence.py materials-index self/ --out self/.pd/materials.json",
        context={
            "user_spec": user_spec,
            "source_map_digest": source_map_digest,
            "source_map": _source_map_brief(self_dir),
        },
        history_line="T1[dependency-planner]: 依据 source-map.md（%s）派发 "
                     "dependency-map.md 构建任务（派发单: %%(ref)s）"
                     % source_map_digest[:19],
    )


def node_t1_await_dependency_plan(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 2 await：等 dependency-map.md 落盘并通过基础校验。"""
    req = state.get("pending_dispatch")
    if not req:
        return {"stage": "T1_DEPENDENCY_PLANNED", "routing_action": "PROCEED"}
    return run_dispatch(
        state,
        req,
        success_stage="T1_DEPENDENCY_PLANNED",
        success_history="T1[dependency-planner]: dependency-map.md 已产出并完成校验"
    )


def node_t1_request_coverage_plan(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 3：coverage-planner —— 只生成 coverage-map.md。

    barrier：source-map.md 与 dependency-map.md 必须已在盘；context 携带
    两份上游产物 digest（任一变更即失效本 round 缓存）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    artifacts = T1_ROUNDS["T1_COVERAGE"][1]
    source_map_digest = _artifact_digest(self_dir, "source-map.md")
    dependency_map_digest = _artifact_digest(self_dir, "dependency-map.md")
    if not source_map_digest or not dependency_map_digest:
        missing = [n for n, d in (("source-map.md", source_map_digest),
                                  ("dependency-map.md", dependency_map_digest))
                   if not d]
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T1[coverage-planner] 上游产物缺失（%s）：barrier 破裂"
                             % ", ".join(missing),
            "history": list(state.get("history", [])) + [
                "T1[coverage-planner]: barrier 破裂 — 缺少 %s"
                % ", ".join(missing)],
        }

    user_spec = state.get("user_spec") or {}
    lesson_minutes = user_spec.get("lesson_minutes", 90) or 90
    contract = state.get("intent_contract") or {}
    content_policy = str(contract.get("content_policy")
                         or user_spec.get("content_policy") or "faithful")

    instruction = (
        "你是覆盖与预算规划专员（coverage-planner），只负责生成 "
        "coverage-map.md 这一份覆盖与课堂预算规划；source-map.md 与 "
        "dependency-map.md 已由前两轮专员冻结（不得改写），正文切片由 T2 "
        "写作者负责。\n\n"
        "【产物契约】：\n"
        "1. 每个输出 section/qid 必须在 coverage-map.md 列出 source_ids、"
        "action、new_objects 和 reason；\n"
        "2. new_objects 非空时只能登记 BRIDGE 或 EXTEND 请求，不得直接写入正文；\n"
        "3. 没有 source_id 的主线内容不能进入 faithful 正文；\n"
        "4. tier 只能取 core|optional|extension：extension 不自动进入 "
        "inclass/core，需人工确认。\n\n"
        "【知识密度自适应规划规范（废除机械等额分配）】：\n"
        "1. 严禁死板机械均分为 4 节、每节 5 题、每题 3 问的均分定额制；\n"
        "2. 基础概念/计算引入型内容：知识密度较低，配置 3–4 道直观探索题，不生硬凑题；\n"
        "3. 核心定理/同构分类驱动型内容：知识密度极高，必须允许拆分为更细粒度小节（如 sec3a 与 sec3b）或扩展为 6–8 道阶梯式引理题，"
        "必须配置小素数/低阶特例算例演练与充分的中间脚手架，严禁为凑 5 题上限跳过证明关键步骤；\n"
        "4. 保证每个小节以 microknowledge 前置卡开头，以 knowledgebox 节末结论框收束；\n"
        "5. coverage-map.md 中必须包含『推荐讲法』与『易错点』两列且每行实质填写，不得留空或填占位符（此为翻转课堂教师讲解与卡点突破的必备输入）；\n"
        f"6. 双向认知预算规范：teaching/classroom 模式下 core 任务总耗时受课时预算严格约束（预算 {lesson_minutes} 分钟，超标需删减次要题量）；"
        "self_study 模式不设时长上限，但不得以“自足完备”为由新增未批准内容；无论何种模式，核心定理/引理推导长链题 planned_minutes 必须 >= 12 分钟，严禁削减长题推导台阶！\n"
    )

    # 自学模式（self_study）切片规划硬红线（report.md 模块三 3.1）：
    # 仅靠 research_prompt.md 提示词不够，规划派发指令必须按模式注入可执行约束。
    prof = str(user_spec.get("delivery_profile", "self_study")).lower()
    if prof == "self_study":
        instruction += (
            "\n\n【self_study 自学模式硬红线（必须遵守）】：\n"
            "1. 自学模式不设总时长上限，以认知台阶完备、逻辑严谨自足为唯一准绳，切勿为凑时间压缩题目推导；\n"
            "2. 严禁将原材料末尾的“习题选讲/压轴题精讲/例题讨论/例题课”单独划为独立切片小节；"
            "此类内容若适合探究，必须拆解为工具应用题并入前述理论小节，否则在 source-map.md 中登记移入课后自测题库；\n"
            "3. 单模块切片严格限制为“概念引入节 → 核心工具推导节 → 度量与应用节”的 3 节精炼自足体系"
            "（确有必要时可用 sec3a/sec3b 细分，但不得增设习题选讲节）；\n"
            "4. 大定理长链推导题（如 Routh、EFFT 类）必须在 coverage-map.md 登记 planned_minutes: 12~15，"
            "在切片题量统计中计为 2 道标准题的认知权重，废除每题均分 6~7 分钟的机械表格；\n"
            "5. coverage-map.md 中只允许登记计划生成的 sec 节名，不得提及已删除/不存在的节。"
        )

    return _request_round(
        state,
        stage="T1_COVERAGE",
        role=ROLE_COVERAGE_PLANNER,
        prompt_file="research_prompt.md",
        artifacts=artifacts,
        instruction=instruction,
        command="python scripts/evidence.py materials-index self/ --out self/.pd/materials.json",
        context={
            "user_spec": user_spec,
            "content_policy": content_policy,
            "source_map_digest": source_map_digest,
            "dependency_map_digest": dependency_map_digest,
            "source_map": _source_map_brief(self_dir),
        },
        history_line="T1[coverage-planner]: 依据 source-map.md/dependency-map.md "
                     "派发 coverage-map.md 构建任务（派发单: %%(ref)s）",
    )


def node_t1_await_coverage_plan(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """§5.1 round 3 await：等 coverage-map.md 落盘并通过基础校验。"""
    req = state.get("pending_dispatch")
    if not req:
        return {"stage": "T1_COVERAGE_PLANNED", "routing_action": "PROCEED"}
    return run_dispatch(
        state,
        req,
        success_stage="T1_COVERAGE_PLANNED",
        success_history="T1[coverage-planner]: coverage-map.md 已产出并完成校验"
    )


def node_t1_verify_mapping(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T1: Verify mapping integrity, build materials.json index, and determine expected sections."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    coverage_path = os.path.join(self_dir, "coverage-map.md")
    expected_sections = _extract_expected_sections(self_dir, coverage_path)

    # Optional: build materials index if research materials exist
    materials_out = os.path.join(self_dir, ".pd", "materials.json")
    code_mat, out_mat, err_mat = runner.run_script(
        "evidence.py", ["materials-index", self_dir, "--out", materials_out]
    )
    if code_mat == 0:
        history.append("T1: materials.json 机器索引已同步")

    history.append(f"T1: 确定切片生成清单: {', '.join(expected_sections)}")

    # §5.1：三 round 的 input_digest 台账（缓存分键存储 / 失败整链拉黑）
    round_digests = dict(state.get("t1_round_digests") or {})

    def _blocklist_chain(patch: Dict[str, Any]) -> None:
        """verify 失败：把本轮各 round 的 input_digest 拉黑，清空台账。

        否则 REPLAN 回路会在下一轮反复命中缓存还原同一份不合格产物
        （audit §10：不得以文件存在/mtime 代替 digest+attempt 校验）。
        """
        blocked = list(state.get("dispatch_blocklist") or [])
        for digest in round_digests.values():
            if digest and digest not in blocked:
                blocked.append(digest)
        patch["dispatch_blocklist"] = blocked
        patch["t1_round_digests"] = {}

    # F2 结构校验：source-map.md 必须满足 source_id -> scope -> action ->
    # status -> reason 契约（深度语义校验由 F3 check_fidelity.py 负责）。
    map_errors = _validate_source_map(self_dir)
    if map_errors:
        replan_count = state.get("replan_count", 0) + 1
        if replan_count > 3:
            history.append(f"T1: 映射契约重规划次数超限 ({replan_count} > 3)，熔断停止")
            patch = {
                "expected_sections": expected_sections,
                "replan_count": replan_count,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": "T1 映射契约重规划次数超限: %s" % map_errors[0],
                "history": history
            }
            _blocklist_chain(patch)
            return patch
        history.append("T1: source-map.md 映射契约不合规: %s" % map_errors[0])
        patch = {
            "expected_sections": expected_sections,
            "replan_count": replan_count,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPLAN",
            "repair_target": "source-map.md",
            "repair_reason": "T1 映射契约不合规: %s" % "; ".join(map_errors[:3]),
            "history": history
        }
        _blocklist_chain(patch)
        return patch
    history.append("T1: source-map.md 映射契约结构校验通过")

    # 验证规划期预算、长链密度与教师支撑门禁
    if os.path.isfile(coverage_path):
        bud_report = runner.run_gate_script(
            gate_name="check_budget",
            script_name="check_budget.py",
            args=[self_dir, "--stage", "planning"],
            report_rel_path=".pd/reports/check_budget_planning.json",
            run_id=state.get("run_id")
        )
        history.append(f"T1: check_budget (planning) 状态: {bud_report['status']} (退出 {bud_report['exit_code']})")
        if not bud_report["passed"]:
            # B1: REPLAN 重规划计数，防 T1 映射死循环（>3 熔断 HALT）
            replan_count = state.get("replan_count", 0) + 1
            if replan_count > 3:
                history.append(f"T1: 映射重规划次数超限 ({replan_count} > 3)，熔断停止")
                patch = {
                    "expected_sections": expected_sections,
                    "budget_gate": bud_report,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"T1 映射重规划次数超限: {bud_report['status']}",
                    "history": history
                }
                _blocklist_chain(patch)
                return patch
            patch = {
                "expected_sections": expected_sections,
                "budget_gate": bud_report,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN",
                "repair_target": "coverage-map.md",
                "repair_reason": f"T1 规划期教学预算或长链密度不合规: {bud_report['status']}",
                "history": history
            }
            _blocklist_chain(patch)
            return patch

    # 任务5（§5.1/§12.3）：三张映射表分键存入派生物缓存——各自独立的
    # stage+scope+input_digest 键（下游 context 携带上游 digest，上游
    # 变更只失效下游，不牵连已冻结的上游产物）。
    for stage, (cache_scope, stage_artifacts) in T1_ROUNDS.items():
        digest = round_digests.get(stage)
        if not digest:
            continue
        try:
            ArtifactCache(self_dir).store(
                stage=stage, scope=cache_scope,
                input_digest=digest,
                config_digest=config_digest(self_dir),
                prompt_version=prompt_version(self_dir, "research_prompt.md"),
                artifacts=stage_artifacts,
            )
        except OSError:
            history.append("T1[%s]: 派生物缓存写入失败（忽略，不影响本次冻结）"
                           % stage)

    return {
        "expected_sections": expected_sections,
        "mapping_frozen": True,
        "stage": "T1_MAPPED",
        "routing_action": "PROCEED",
        "t1_round_digests": round_digests,
        "history": history
    }
