#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fast_mapping_nodes.py — faithful_fast 图的 T1 统一规划（卡 12）。

legacy 图把 T1 拆成三个串行 round，每轮一次派发：

    T1_SOURCE_AUDIT(source-auditor) → T1_DEPENDENCY(dependency-planner)
    → T1_COVERAGE(coverage-planner)

faithful_fast 图把它们压成**一次** `T1_UNIFIED_PLAN` 派发：一个 agent 在同一份
心智模型下同时产出 source-map.md / dependency-map.md / coverage-map.md 三份既有
映射表，仍然向下游提供完整合法的三张表（既有解析器契约不变）。

组事务（C6/C7）：
  1. 派发白名单同时声明三份 `.pd/mapping-staging/*.md`（agent 只写暂存区）；
  2. 三表逐表校验 + 跨表引用/来源覆盖校验 + 与派发时摘要比对（TOCTOU）；
  3. **全部通过**才一次性提升到 self 根并置 mapping_frozen；
     少一表、摘要不符、来源遗漏、无效 source 引用 → 不提升、不写缓存，
     只回 REPLAN 并把该组指纹加入黑名单。
  4. 缓存以"三件完整组"为单位（stage=T1_UNIFIED_PLAN / scope=unified-mapping），
     与 legacy 三轮的缓存键空间天然不混用。

legacy 三轮的 request/await/cache 代码保持原样，供旧图与回退使用。
"""

import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ..adapters.artifact_cache import (
    ArtifactCache, config_digest, prompt_version,
)
from ..execution_policy import (
    EXECUTION_SCHEMA_VERSION, CONTENT_AFFECTING_EXTRA_KEYS,
    graph_version_for_profile, policy_digest,
)
from ..state import ShrinkCoursewareState
from . import agent_nodes, mapping_nodes

STAGE_UNIFIED_PLAN = "T1_UNIFIED_PLAN"
STAGE_UNIFIED_READY = "T1_UNIFIED_PLAN_READY"
UNIFIED_CACHE_SCOPE = "unified-mapping"
ROLE_UNIFIED_MAPPING_PLANNER = "统一映射规划专员（mapping-planner）"
PROMPT_FILE_FAITHFUL_MAPPING = "faithful_mapping_prompt.md"

#: agent 的三件产物先在暂存区落盘，组事务通过后才提升到 self 根。
STAGING_DIR_REL = ".pd/mapping-staging"
UNIFIED_ARTIFACTS: Tuple[str, ...] = (
    "source-map.md", "dependency-map.md", "coverage-map.md")

MAX_GROUP_REPLANS = 3

_SOURCE_ID_RE = mapping_nodes._RE_SOURCE_MAP_ROW
_SEC_TOKEN_RE = re.compile(r"\bsec\d+[a-z]?\b")
_ID_TOKEN_RE = re.compile(r"\bSRC-\d+\b")
_COV_REQUIRED_COLUMNS = ("source_ids", "action", "new_objects", "reason", "tier")
_DEP_COLUMNS = ("节点", "前置节点", "说明", "是否必需")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def staged_rel(rel: str) -> str:
    """self 内的暂存区相对路径（POSIX 分隔符，跨平台稳定）。"""
    return "%s/%s" % (STAGING_DIR_REL, rel)


def staged_artifacts() -> List[str]:
    return [staged_rel(rel) for rel in UNIFIED_ARTIFACTS]


def _abs(self_dir: str, rel: str) -> str:
    return os.path.join(self_dir, *rel.split("/"))


def _read_text(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _snapshot(self_dir: str, rels: List[str]) -> Dict[str, Optional[str]]:
    return agent_nodes.snapshot(self_dir, rels)


def _monotonic_snapshot(self_dir: str) -> Dict[str, Optional[str]]:
    return _snapshot(self_dir, staged_artifacts())


def _cell_tokens(cell: str) -> List[str]:
    return _SEC_TOKEN_RE.findall(cell or "")


def _source_ids(cell: str) -> List[str]:
    return _ID_TOKEN_RE.findall(cell or "")


def _is_separator(cells: List[str]) -> bool:
    return bool(cells) and all(set(c.strip()) <= set("-: ") for c in cells)


def _split_row(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _table_rows(text: str, *, header_hint: str) -> Tuple[Optional[List[str]],
                                                        List[List[str]]]:
    """返回 (表头列名|None, 数据行列表)；表头按 header_hint 定位。"""
    header: Optional[List[str]] = None
    rows: List[List[str]] = []
    for line in (text or "").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if header is None:
            joined = "|".join(cells)
            if header_hint in joined:
                header = cells
            continue
        if _is_separator(cells):
            continue
        rows.append(cells)
    return header, rows


# ---------------------------------------------------------------------------
# 指纹（C7：组缓存/黑名单的唯一标识）
# ---------------------------------------------------------------------------

def _group_digest_context(policy: Dict[str, Any], inventory: Dict[str, Any],
                          *, prompt_ver: str, content_policy: str,
                          self_dir: str = "",
                          extra_config: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
    profile = policy.get("execution_profile") or "faithful_fast"
    return {
        "graph_version": graph_version_for_profile(profile),
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_profile": profile,
        "policy_digest": policy_digest(
            policy, execution_profile=profile, content_policy=content_policy,
            extra_config=extra_config),
        "config_digest": config_digest(self_dir) if self_dir else "",
        "content_policy": str(content_policy or "faithful").strip().lower(),
        "source_inventory_digest": inventory.get("digest", ""),
        "manifest_digest": inventory.get("manifest_digest", ""),
        "prompt_version": prompt_ver,
        "role": ROLE_UNIFIED_MAPPING_PLANNER,
        "group_artifacts": UNIFIED_ARTIFACTS,
        "cache_group": UNIFIED_CACHE_SCOPE,
    }


def _content_affecting(policy: Dict[str, Any]) -> Dict[str, Any]:
    return {key: policy.get(key) for key in CONTENT_AFFECTING_EXTRA_KEYS
            if key in policy}


# ---------------------------------------------------------------------------
# 三表结构解析（尽量与既有 check_fidelity / estimate_minutes 的列契约一致）
# ---------------------------------------------------------------------------

def parse_dependency_map(path: str) -> Tuple[Optional[List[Dict[str, str]]],
                                             List[str]]:
    text = _read_text(path)
    if text is None:
        return None, ["dependency-map.md 缺失（统一规划必须一次交齐三份映射）"]
    header, rows = _table_rows(text, header_hint="节点")
    if header is None:
        return None, ["dependency-map.md 缺少 `| 节点 | 前置节点 | 说明 | 是否必需 |` 表头"]
    missing_cols = [c for c in _DEP_COLUMNS if c not in header]
    if missing_cols:
        return None, ["dependency-map.md 表头缺少列：%s（应为 %s）" % (
            "、".join(missing_cols), " | ".join(_DEP_COLUMNS))]
    index = {name: header.index(name) for name in _DEP_COLUMNS}
    parsed: List[Dict[str, str]] = []
    errors: List[str] = []
    for cells in rows:
        if len(cells) < len(header):
            errors.append("dependency-map.md 行 `%s` 列数不足（应为 %d 列）" % (
                " | ".join(cells), len(header)))
            continue
        row = {name: cells[index[name]] for name in _DEP_COLUMNS}
        if not row["节点"] or row["节点"] == "-":
            errors.append("dependency-map.md 存在空节点行：%s" % " | ".join(cells))
            continue
        parsed.append(row)
    if not parsed and not errors:
        errors.append("dependency-map.md 没有任何依赖行（至少登记一节）")
    return parsed, errors


def parse_coverage_map(path: str) -> Tuple[Optional[List[Dict[str, str]]],
                                           List[str]]:
    text = _read_text(path)
    if text is None:
        return None, ["coverage-map.md 缺失（统一规划必须一次交齐三份映射）"]
    header, rows = _table_rows(text, header_hint="原文节点")
    if header is None:
        return None, ["coverage-map.md 缺少含 `原文节点` 的表头（既有覆盖表契约）"]
    lowered = [c.strip().lower() for c in header]
    missing_cols = [c for c in _COV_REQUIRED_COLUMNS if c not in lowered]
    if missing_cols:
        return None, ["coverage-map.md 表头缺少 F2 必填列：%s" % "、".join(missing_cols)]
    index = {c: lowered.index(c) for c in _COV_REQUIRED_COLUMNS}
    node_index = lowered.index("原文节点")
    parsed: List[Dict[str, str]] = []
    errors: List[str] = []
    for cells in rows:
        if len(cells) < len(header):
            errors.append("coverage-map.md 行 `%s` 列数不足（应为 %d 列）" % (
                " | ".join(cells), len(header)))
            continue
        parsed.append({
            "原文节点": cells[node_index],
            "source_ids": cells[index["source_ids"]],
            "action": cells[index["action"]],
            "new_objects": cells[index["new_objects"]],
            "reason": cells[index["reason"]],
            "tier": cells[index["tier"]],
            "sections": "、".join(_cell_tokens(" | ".join(cells))),
            "cells": " | ".join(cells),
        })
    if not parsed and not errors:
        errors.append("coverage-map.md 没有任何覆盖行（至少登记一节）")
    return parsed, errors


# ---------------------------------------------------------------------------
# 组校验：逐表 → 跨表引用/节名/来源覆盖 → 派发摘要比对
# ---------------------------------------------------------------------------

def _validate_group(self_dir: str, inventory: Optional[Dict[str, Any]],
                    expected_digests: Optional[Dict[str, Optional[str]]] = None
                    ) -> Tuple[List[str], str, Dict[str, Any]]:
    """返回 (errors, repair_target, facts)。errors 为空表示整组可提升。"""
    errors: List[str] = []
    target = UNIFIED_ARTIFACTS[0]
    facts: Dict[str, Any] = {}

    # ① 三表齐全（缺一表整组不校验，避免"部分映射提前提升"）
    missing = [rel for rel in UNIFIED_ARTIFACTS
               if not os.path.isfile(_abs(self_dir, staged_rel(rel)))]
    if missing:
        return (["三表未交齐，缺少：%s（整组不校验、不提升、不缓存）"
                 % "、".join(missing)], missing[0], facts)

    # ② TOCTOU：staging 摘要必须与派发 wait 阶段记录的一致
    if expected_digests:
        actual = _monotonic_snapshot(self_dir)
        for rel in UNIFIED_ARTIFACTS:
            key = staged_rel(rel)
            if expected_digests.get(key) != actual.get(key):
                errors.append("摘要不符：%s 在派发后被改动（派发摘要 %s ≠ 当前 %s）"
                              % (rel, str(expected_digests.get(key))[:19],
                                 str(actual.get(key))[:19]))
                target = rel
                break
        if errors:
            return errors, target, facts

    # ③ 逐表结构
    source_errors = mapping_nodes._validate_source_map(
        self_dir, os.path.join(self_dir, *staged_rel("source-map.md").split("/")))
    if source_errors:
        errors.extend(source_errors)
        target = "source-map.md"

    dep_rows, dep_errors = parse_dependency_map(
        _abs(self_dir, staged_rel("dependency-map.md")))
    cov_rows, cov_errors = parse_coverage_map(
        _abs(self_dir, staged_rel("coverage-map.md")))
    if dep_errors:
        errors.extend(dep_errors)
        if target not in ("dependency-map.md",) and not source_errors:
            target = "dependency-map.md"
    if cov_errors:
        errors.extend(cov_errors)
        if not source_errors and not dep_errors:
            target = "coverage-map.md"
    if errors:
        return errors, target, facts

    # ④ 跨表：source-map 登记的 id 与节名是唯一权威
    source_rows = _parse_source_rows(self_dir)
    mapped_ids = {row["source_id"] for row in source_rows}
    scopes = {row["output_scope"] for row in source_rows
              if row["output_scope"] and row["output_scope"] != "none"}

    inventory_ids = set()
    unit_by_id: Dict[str, Dict[str, Any]] = {}
    for unit in (inventory or {}).get("units") or []:
        inventory_ids.add(unit.get("source_id"))
        unit_by_id[unit.get("source_id")] = unit

    phantom = sorted(mapped_ids - inventory_ids)
    if phantom:
        errors.append("source-map.md 引用了来源清单中不存在的 id：%s"
                      % "、".join(phantom))
    omitted = [sid for sid, unit in sorted(unit_by_id.items())
               if unit.get("disposition") != "excluded" and sid not in mapped_ids]
    if omitted:
        errors.append("来源遗漏：来源清单中 %s 未在 source-map.md 登记（必须映射或"
                      "标 EXCLUDED_BY_USER）" % "、".join(omitted))
    if errors:
        # 来源侧不合规一律归因到 source-map.md：它是 id/节名的唯一权威，
        # coverage 里随之出现的悬空引用只是它的下游症状。
        return errors, "source-map.md", facts

    unknown_refs: List[str] = []
    for row in cov_rows:
        ids = _source_ids(row["source_ids"])
        if not ids:
            errors.append("coverage-map.md 行 `%s` 的 source_ids 未登记任何 SRC-xxx"
                          % row["cells"])
            target = "coverage-map.md"
            continue
        for sid in ids:
            if sid not in mapped_ids:
                unknown_refs.append(sid)
        secs = _cell_tokens(row["sections"])
        if not secs:
            errors.append("coverage-map.md 行 `%s` 的学案位置未使用 sec 节名"
                          % row["cells"])
            target = "coverage-map.md"
        else:
            off = [s for s in secs if s not in scopes]
            if off:
                errors.append("coverage-map.md 行 `%s` 引用了 source-map 未映射的节：%s"
                              % (row["cells"], "、".join(sorted(set(off)))))
                target = "coverage-map.md"
    if unknown_refs:
        errors.append("coverage-map.md 引用了 source-map.md 未登记的来源 id：%s"
                      % "、".join(sorted(set(unknown_refs))))
        target = "coverage-map.md"

    cov_sections = set()
    for row in cov_rows:
        cov_sections.update(_cell_tokens(row["sections"]))
    for row in dep_rows:
        nodes = _cell_tokens(row["节点"])
        if not nodes:
            errors.append("dependency-map.md 节点 `%s` 未使用 sec 节名" % row["节点"])
            target = "dependency-map.md"
            continue
        for node in nodes:
            if node not in cov_sections or node not in scopes:
                errors.append("dependency-map.md 节点 `%s` 不在 coverage/source-map "
                              "的节集合内（%s）"
                              % (node, "、".join(sorted(cov_sections)) or "空"))
                target = "dependency-map.md"

    facts = {"source_rows": source_rows, "mapped_ids": sorted(mapped_ids),
             "sections": sorted(scopes), "coverage_rows": len(cov_rows),
             "dependency_rows": len(dep_rows)}
    return errors, target, facts


def _parse_source_rows(self_dir: str) -> List[Dict[str, str]]:
    text = _read_text(_abs(self_dir, staged_rel("source-map.md"))) or ""
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        match = _SOURCE_ID_RE.match(line)
        if not match:
            continue
        rows.append({"source_id": match.group(1), "output_scope": match.group(2),
                     "action": match.group(3), "status": match.group(4),
                     "reason": match.group(5)})
    return rows


# ---------------------------------------------------------------------------
# 组事务提升（三表一起上/一起不上）
# ---------------------------------------------------------------------------

def _promote_group(self_dir: str) -> Tuple[bool, str]:
    """把三份 staging 表一次性提升到 self 根；任一步失败整体回滚。"""
    staged = {rel: _abs(self_dir, staged_rel(rel)) for rel in UNIFIED_ARTIFACTS}
    missing = [rel for rel, path in staged.items() if not os.path.isfile(path)]
    if missing:
        return False, "组事务提升失败：暂存区缺少 %s" % "、".join(missing)

    backups: Dict[str, str] = {}
    written: List[str] = []
    temps: List[str] = []
    try:
        for rel, src in staged.items():
            dst = os.path.join(self_dir, rel)
            if os.path.isfile(dst):
                backup = dst + ".preunified.bak"
                shutil.copy2(dst, backup)
                backups[rel] = backup
            fd, tmp = tempfile.mkstemp(dir=self_dir, prefix=".unified-",
                                      suffix=".tmp")
            os.close(fd)
            temps.append(tmp)
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            temps.remove(tmp)
            written.append(rel)
    except OSError as exc:
        for rel in written:
            dst = os.path.join(self_dir, rel)
            backup = backups.get(rel)
            try:
                if backup and os.path.isfile(backup):
                    os.replace(backup, dst)
                    backups.pop(rel, None)
                elif os.path.isfile(dst):
                    os.remove(dst)
            except OSError:
                pass
        for tmp in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        for backup in backups.values():
            try:
                os.remove(backup)
            except OSError:
                pass
        return False, "组事务提升失败（%s），三表均已回滚" % exc

    for backup in backups.values():
        try:
            os.remove(backup)
        except OSError:
            pass
    return True, ""


# ---------------------------------------------------------------------------
# instruction
# ---------------------------------------------------------------------------

def _build_instruction(self_dir: str, inventory: Optional[Dict[str, Any]],
                       policy: Dict[str, Any], feedback: str = "") -> str:
    units = (inventory or {}).get("units") or []
    lines: List[str] = []
    lines.append("你在 faithful_fast 图里承担【一次】T1 统一映射规划派发"
                 "（stage=%s），一次交齐三份既有映射表。" % STAGE_UNIFIED_PLAN)
    lines.append("")
    lines.append("一、产物（三个路径全部必须写出，缺一表整组作废）")
    for rel in UNIFIED_ARTIFACTS:
        lines.append("  - %s" % staged_rel(rel))
    lines.append("只写上述暂存区路径；不要写 self 根、不要写 TeX、不要自评通过。")
    lines.append("")
    lines.append("二、三表既有列结构（不可增删列名/顺序）")
    lines.append("  1) source-map.md 每行一条映射：")
    lines.append("     SRC-001 -> sec1 -> PRESERVE -> PRESERVED -> 一句话理由")
    lines.append("     action∈%s；status∈%s"
                 % ("/".join(mapping_nodes.SOURCE_MAP_ACTIONS),
                    "/".join(mapping_nodes.SOURCE_MAP_STATUSES)))
    lines.append("     被排除的单元写 output_scope=none 且 status=EXCLUDED_BY_USER；"
                 "faithful 下 EXTEND 自动判 PENDING_APPROVAL。")
    lines.append("  2) dependency-map.md 表格表头：| 节点 | 前置节点 | 说明 | 是否必需 |")
    lines.append("     节点用 sec 节名；无前置写 `-`；只有真逻辑依赖才写 是。")
    lines.append("  3) coverage-map.md 表格表头必须含：")
    lines.append("     原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | "
                 "practice_mode | carrier_qid | planned_minutes | 推荐讲法 | 易错点 | "
                 "source_ids | action | new_objects | reason | tier")
    lines.append("     source_ids 必须是 source-map 已登记的 SRC-xxx（逗号分隔）；"
                 "tier∈core/optional/extension。")
    lines.append("")
    lines.append("三、忠实优先（硬约束）")
    lines.append("  - 只做：原文→学案呈现范围的忠实映射、必要前置依赖、呈现载体登记。")
    lines.append("  - 不额外研究、不引入外部知识、不自动补全原文没有的背景。")
    lines.append("  - 不固定节数（不套用「三节精炼自足体系」），节数由原文结构决定。")
    lines.append("  - 原文确实缺失的信息标 needs_evidence 并说明缺什么，不许用常识填空。")
    lines.append("  - 来源清单里非 excluded 单元必须全部在 source-map 登记（来源无遗漏）。")
    lines.append("  - 三表节名/id 必须互相自洽：coverage.source_ids ⊆ source-map 的 id；"
                 "dependency 节点 ⊆ coverage 的 sec 节名。")
    lines.append("")
    lines.append("四、来源清单（%d 个单元，摘要 %s）"
                 % (len(units), (inventory or {}).get("digest", "")))
    for unit in units[:400]:
        lines.append("  - %s | %s | %s | %s" % (
            unit.get("source_id"), unit.get("source_file"),
            unit.get("locator"), unit.get("disposition")))
    if len(units) > 400:
        lines.append("  - …（其余 %d 个单元见派发 context）" % (len(units) - 400))
    if str(policy.get("execution_profile") or "") == "faithful_fast":
        lines.append("")
        lines.append("五、执行策略：faithful_fast（graph_version=%s，"
                     "execution_schema_version=%d）；内容策略 %s。"
                     % (graph_version_for_profile("faithful_fast"),
                        EXECUTION_SCHEMA_VERSION,
                        str(policy.get("content_policy") or "faithful")))
    if feedback:
        lines.append("")
        lines.append("六、上一轮打回原因（本轮只重写需要改的表，但必须重验三表一致性）")
        lines.append(feedback.strip())
    return "\n".join(lines)


def _brief(inventory: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for unit in (inventory or {}).get("units") or []:
        out.append({key: unit.get(key) for key in (
            "source_id", "source_file", "locator", "kind", "disposition",
            "source_order")})
    return out


# ---------------------------------------------------------------------------
# 节点 1：请求（一次派发声明三件产物）
# ---------------------------------------------------------------------------

def node_fast_t1_request_unified_plan(state: ShrinkCoursewareState
                                      ) -> Dict[str, Any]:
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    project_root = state.get("project_root") or self_dir
    policy = mapping_nodes._execution_policy(self_dir)
    content_policy = str(
        (state.get("user_spec") or {}).get("content_policy")
        or policy.get("content_policy") or "faithful")

    inventory, inv_error = mapping_nodes._ensure_source_inventory(
        self_dir, project_root)
    if inv_error or not inventory:
        return {"stage": "HALTED", "routing_action": "HALT",
                "error_message": "T1[unified] 来源清单冻结失败（%s）"
                                 % (inv_error or "空清单")}

    prompt_ver = prompt_version(self_dir, PROMPT_FILE_FAITHFUL_MAPPING)
    digest_ctx = _group_digest_context(
        policy, inventory, prompt_ver=prompt_ver, content_policy=content_policy,
        self_dir=self_dir, extra_config=_content_affecting(policy))
    context = {
        "input_path": state.get("input_path", ""),
        "user_spec": state.get("user_spec") or {},
        "content_policy": content_policy,
        "execution_profile": policy.get("execution_profile") or "faithful_fast",
        "graph_version": digest_ctx["graph_version"],
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "policy_digest": digest_ctx["policy_digest"],
        "source_inventory_digest": inventory.get("digest", ""),
        "source_inventory": _brief(inventory),
        "mapping_tables": list(UNIFIED_ARTIFACTS),
        "staging_dir": STAGING_DIR_REL,
        "frozen_metadata": {
            "graph_version": digest_ctx["graph_version"],
            "execution_schema_version": EXECUTION_SCHEMA_VERSION,
            "execution_profile": digest_ctx["execution_profile"],
            "policy_digest": digest_ctx["policy_digest"],
        },
    }
    feedback = str(state.get("unified_plan_feedback") or "")
    request = agent_nodes.build_request(
        state,
        stage=STAGE_UNIFIED_PLAN,
        role=ROLE_UNIFIED_MAPPING_PLANNER,
        prompt_file=PROMPT_FILE_FAITHFUL_MAPPING,
        artifacts=staged_artifacts(),
        instruction=_build_instruction(self_dir, inventory, policy,
                                       feedback=feedback),
        command="python scripts/evidence.py materials-index self/ "
                "--out self/.pd/materials.json",
        context=context,
        digest_context=digest_ctx,
    )
    digest = request["input_digest"]
    history: List[str] = []

    if not mapping_nodes._cache_blocklisted(state, digest):
        cache = ArtifactCache(self_dir)
        entry = cache.lookup(
            stage=STAGE_UNIFIED_PLAN, scope=UNIFIED_CACHE_SCOPE,
            input_digest=digest, config_digest=config_digest(self_dir),
            prompt_version=prompt_ver, artifacts=staged_artifacts())
        if entry is not None and cache.restore(entry):
            errors, _target, _facts = _validate_group(self_dir, inventory)
            if not errors:
                history.append("T1[unified]: 三表组缓存命中（input_digest %s），"
                               "零派发" % digest[:19])
                return {"pending_dispatch": None, "stage": STAGE_UNIFIED_READY,
                        "routing_action": "PROCEED",
                        "unified_plan_digest": digest,
                        "unified_plan_digests": _monotonic_snapshot(self_dir),
                        "history": history}
            # 缓存还原出来的三表仍不合规：清掉暂存区，重新派发
            for rel in UNIFIED_ARTIFACTS:
                path = _abs(self_dir, staged_rel(rel))
                if os.path.isfile(path):
                    os.remove(path)
            history.append("T1[unified]: 组缓存还原后校验失败（%s），改为重新派发"
                           % errors[0])

    request_path = agent_nodes.write_request_file(self_dir, request)
    history.append("T1[unified]: 一次派发声明三件产物（%s），request=%s"
                   % ("、".join(UNIFIED_ARTIFACTS),
                      os.path.basename(request_path)))
    return {"pending_dispatch": request, "stage": STAGE_UNIFIED_PLAN,
            "routing_action": "PROCEED", "unified_plan_digest": digest,
            "history": history}


# ---------------------------------------------------------------------------
# 节点 2：等待（一份 receipt 认证三件产物）
# ---------------------------------------------------------------------------

def node_fast_t1_await_unified_plan(state: ShrinkCoursewareState
                                    ) -> Dict[str, Any]:
    request = state.get("pending_dispatch")
    if not request:
        return {"stage": STAGE_UNIFIED_READY, "routing_action": "PROCEED"}

    result = agent_nodes.run_dispatch(
        state, request,
        success_stage=STAGE_UNIFIED_READY,
        success_history="T1[unified]: 一份 receipt 认证三件映射产物")
    if result.get("stage") == "HALTED" or result.get("routing_action") == "HALT":
        return result
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    patch = dict(result)
    # 派发时的三表摘要：verify 用它做 TOCTOU（摘要不符不得提升）
    patch["unified_plan_digests"] = _monotonic_snapshot(self_dir)
    patch["stage"] = STAGE_UNIFIED_READY
    return patch


# ---------------------------------------------------------------------------
# 节点 3：组校验 + 组事务提升
# ---------------------------------------------------------------------------

def _replan(state: ShrinkCoursewareState, digest: str, errors: List[str],
            target: str) -> Dict[str, Any]:
    replan_count = int(state.get("replan_count") or 0) + 1
    reason = "T1 统一映射组契约不合规: %s" % "；".join(errors[:6])
    if replan_count > MAX_GROUP_REPLANS:
        return {"stage": "HALTED", "routing_action": "HALT",
                "error_message": "T1 统一映射组重规划次数超限: %s" % reason,
                "mapping_frozen": False, "t1_round_digests": {}}

    blocklist = list(state.get("dispatch_blocklist") or [])
    if digest and digest not in blocklist:
        blocklist.append(digest)
    history = list(state.get("history") or [])
    history.append("T1[unified]: 组事务未通过（%s），不提升、不写缓存，回 REPLAN"
                   % "；".join(errors[:3]))
    return {"expected_sections": list(state.get("expected_sections") or []),
            "replan_count": replan_count,
            "dispatch_blocklist": blocklist,
            "mapping_frozen": False,
            "t1_round_digests": {},
            "stage": "REPAIR_LOOP", "routing_action": "REPLAN",
            "repair_target": target, "repair_reason": reason,
            "unified_plan_feedback": reason,
            "history": history}


def node_fast_t1_verify_unified_mapping(state: ShrinkCoursewareState
                                        ) -> Dict[str, Any]:
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    project_root = state.get("project_root") or self_dir
    digest = (state.get("unified_plan_digest")
              or (state.get("t1_round_digests") or {}).get(STAGE_UNIFIED_PLAN)
              or "")

    inventory, inv_error = mapping_nodes._ensure_source_inventory(
        self_dir, project_root)
    if inv_error or not inventory:
        return {"stage": "HALTED", "routing_action": "HALT",
                "error_message": "T1[unified] 来源清单冻结失败（%s）"
                                 % (inv_error or "空清单"),
                "mapping_frozen": False}

    errors, target, facts = _validate_group(
        self_dir, inventory, state.get("unified_plan_digests"))
    if errors:
        return _replan(state, digest, errors, target)

    ok, promote_error = _promote_group(self_dir)
    if not ok:
        return {"stage": "HALTED", "routing_action": "HALT",
                "error_message": promote_error, "mapping_frozen": False,
                "t1_round_digests": {}}

    coverage_path = os.path.join(self_dir, "coverage-map.md")
    expected_sections = mapping_nodes._extract_expected_sections(
        self_dir, coverage_path)
    history = list(state.get("history") or [])
    history.append("T1[unified]: 三表整组通过（%d 条来源映射 / %d 个节），"
                   "已提升并冻结"
                   % (len(facts.get("source_rows") or []),
                      len(expected_sections or [])))

    prompt_ver = prompt_version(self_dir, PROMPT_FILE_FAITHFUL_MAPPING)
    if digest:
        try:
            ArtifactCache(self_dir).store(
                stage=STAGE_UNIFIED_PLAN, scope=UNIFIED_CACHE_SCOPE,
                input_digest=digest, config_digest=config_digest(self_dir),
                prompt_version=prompt_ver, artifacts=staged_artifacts())
        except OSError as exc:
            history.append("T1[unified]: 组缓存写入失败（%s），不影响本轮冻结"
                           % exc)

    return {"expected_sections": expected_sections,
            "mapping_frozen": True,
            "t1_round_digests": {STAGE_UNIFIED_PLAN: digest} if digest else {},
            "unified_plan_digest": digest,
            "stage": "T1_MAPPED", "routing_action": "PROCEED",
            "pending_dispatch": None,
            "history": history}
