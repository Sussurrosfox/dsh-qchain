#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""request_scopes.py — 派发单「节作用域」的唯一正规化口径（DATA-CONTRACTS C5）。

背景（修复卡 13）：一个写作 agent 允许一次写 1–2 个**相邻**节，因此派发单从
「一节一请求」升级为多 scope 请求：

    {"schema_version": 2,
     "scope": "sec1",                       # 兼容标签（= scopes[0]）
     "scopes": ["sec1", "sec2"],            # 授权的唯一真源
     "scope_inputs": {"sec1": "<sha256>", "sec2": "<sha256>"},
     "artifact_scopes": {"sec1.tex": "sec1", "sec2.tex": "sec2"}}

硬规则（C5）：``scope`` 在 v2 只是 first-scope 兼容标签，**任何授权 / 完整性 /
覆盖 / 去重 / 认证判定必须读 ``scopes``**；只有旧 schema（无 ``scopes`` 键）的
单 scope 派发单才回落 ``scope`` / ``context.target_section``。这样旧检查点、
旧派发单与 T2_CHECK / T4（仍是一节一请求）不被强制迁移。

本模块是纯函数库（无 IO、无副作用），供三处消费者共用：

  * workflow/nodes/section_batch_nodes.py（组批 / 计划 / 逐节认证）
  * scripts/check_dispatch.py              （R3/R4 覆盖完整性 + R5 去重）
  * scripts/check_provenance.py            （writer 链与写域按 scopes 判定）
  * scripts/check_agent_audit.py           （superseded 必须 all）
  * workflow/nodes/agent_nodes.py          （build_request 写 schame 2 字段）
"""
import re

#: 派发单 schema 版本：1 = 单 scope（旧）；2 = scopes 多节（C5）
SCHEMA_VERSION = 2

#: 一个作者 agent 最多同时承担的相邻节数（C5 合批上限）
MAX_SCOPES_PER_REQUEST = 2

#: 合法的 scope 形态（与 section_batch_nodes.RE_SECTION_NODE 同口径）
RE_SCOPE = re.compile(r"^sec[0-9A-Za-z_-]+$")


def _clean(value):
    """scope 原子校验：必须是合法 sec 名，否则返回空串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or not RE_SCOPE.match(text):
        return ""
    return text


def legacy_scope_of(request):
    """旧字段口径：``scope`` 优先，回落 ``context.target_section``。"""
    if not isinstance(request, dict):
        return ""
    raw = request.get("scope")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    context = request.get("context")
    if isinstance(context, dict):
        raw = context.get("target_section")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def is_multi_scope(request):
    """是否声明了 v2 多 scope 契约（``scopes`` 键存在且为列表）。

    只认「键存在且是 list」——旧派发单没有该键，必须继续走单 scope 语义，
    不能因为缺键而被当作空 scope 判失败。
    """
    if not isinstance(request, dict):
        return False
    return isinstance(request.get("scopes"), list)


def request_scopes(request):
    """正规化 scope 列表（授权的唯一真源）。

    * v2：返回 ``scopes`` 原序去重后的合法 sec 名列表；
    * legacy：返回 ``[legacy_scope_of(request)]``（可能为空列表）。

    非法条目（非 sec 形态）会被**保留原样**以便门禁报错——去重但不清洗：
    调用方必须能看见「声明了非法 scope」而不是被静默丢弃。
    """
    if is_multi_scope(request):
        out = []
        for raw in request.get("scopes") or []:
            text = raw.strip() if isinstance(raw, str) else raw
            if text in (None, ""):
                continue
            if text not in out:
                out.append(text)
        return out
    legacy = legacy_scope_of(request)
    return [legacy] if legacy else []


def request_covers(request, sec):
    """该派发单是否被授权写 ``sec``（多 scope 必须看 scopes）。"""
    return bool(sec) and sec in request_scopes(request)


def artifact_map(request):
    """产物 → 归属 scope 的映射。

    优先读显式 ``artifact_scopes``（C5）；旧派发单缺失时按「artifacts 与
    scopes 一一对应」推导，最后回落「唯一 scope 承担全部产物」（旧单 scope
    请求可能声明多个产物，例如 T2_CHECK 的 sidecar）。
    """
    artifacts = [a for a in (request.get("artifacts") or [])
                 if isinstance(a, str) and a]
    scopes = request_scopes(request)
    explicit = request.get("artifact_scopes")
    mapping = {}
    if isinstance(explicit, dict):
        for rel, sec in explicit.items():
            if isinstance(rel, str) and rel and isinstance(sec, str) and sec:
                mapping[rel] = sec
    if mapping:
        return mapping
    if len(scopes) == 1:
        return {rel: scopes[0] for rel in artifacts}
    if len(scopes) == len(artifacts):
        return {rel: sec for rel, sec in zip(artifacts, scopes)}
    return {}


def artifacts_of_scope(request, sec):
    """该请求为 ``sec`` 声明的产物清单（用于逐节认证）。"""
    mapping = artifact_map(request)
    out = [rel for rel, owner in mapping.items() if owner == sec]
    if out:
        return out
    # 回退：旧派发单只有 sec.tex 约定
    artifacts = [a for a in (request.get("artifacts") or [])
                 if isinstance(a, str) and a]
    if (sec + ".tex") in artifacts and sec in request_scopes(request):
        return [sec + ".tex"]
    return []


def scope_input_digests(request):
    """``{sec: sha256}``（C5 逐节来源指纹）；缺失/畸形时返回空 dict。"""
    if not isinstance(request, dict):
        return {}
    raw = request.get("scope_inputs")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and v}


def scope_input_digest(request, sec):
    return scope_input_digests(request).get(sec, "")


def declaration_errors(request, expected_sections=None, *, max_scopes=None):
    """多 scope 声明的结构性问题（供 check_dispatch R5 报 BAD_SCOPE / DUPLICATE_SCOPE）。

    返回 ``[(code, detail), ...]``；``code`` ∈
    {"BAD_SCOPE", "DUPLICATE_SCOPE", "SCOPE_OVERLAP", "SCOPE_NOT_ADJACENT"}。
    只描述**本请求内部**的问题；跨请求的覆盖/重叠由调用方（check_plan v2）负责。
    """
    problems = []
    if not is_multi_scope(request):
        return problems
    raw = request.get("scopes")
    if not isinstance(raw, list) or not raw:
        return [("BAD_SCOPE", "scopes 必须是非空列表")]
    seen = []
    for item in raw:
        if not isinstance(item, str) or not _clean(item):
            problems.append(("BAD_SCOPE", "scopes 含非法小节名 %r" % (item,)))
            continue
        if item in seen:
            problems.append(("DUPLICATE_SCOPE", "scopes 重复声明 %s" % item))
            continue
        seen.append(item)
    expected = [s for s in (expected_sections or []) if isinstance(s, str)]
    if expected:
        for item in seen:
            if item not in expected:
                problems.append(
                    ("BAD_SCOPE", "scopes 含不在本次计划内的小节 %s" % item))
        order = {sec: i for i, sec in enumerate(expected)}
        if all(item in order for item in seen):
            indexes = [order[item] for item in seen]
            if indexes != sorted(indexes):
                problems.append(
                    ("SCOPE_ORDER", "scopes 必须按小节顺序升序排列（%s）"
                     % ",".join(seen)))
            if len(indexes) > 1 and any(
                    b != a + 1 for a, b in zip(indexes, indexes[1:])):
                problems.append(
                    ("SCOPE_NOT_ADJACENT", "一个作者只能写相邻小节（%s）"
                     % ",".join(seen)))
    limit = max_scopes if isinstance(max_scopes, int) and max_scopes >= 1 \
        else MAX_SCOPES_PER_REQUEST
    if len(seen) > limit:
        problems.append(
            ("SCOPE_OVERFLOW", "单请求 scope 数 %d 超过上限 %d"
             % (len(seen), limit)))

    # 写域唯一性：同一请求内两个小节不能争夺同一个产物
    mapping = artifact_map(request)
    owners = {}
    for rel, sec in mapping.items():
        owners.setdefault(rel, set()).add(sec)
    for rel, secs in sorted(owners.items()):
        if len(secs) > 1:
            problems.append(
                ("SCOPE_OVERLAP", "产物 %s 同时归属 %s"
                 % (rel, ",".join(sorted(secs)))))
    declared = set(a for a in (request.get("artifacts") or [])
                   if isinstance(a, str) and a)
    for rel in sorted(mapping):
        if rel not in declared:
            problems.append(
                ("SCOPE_OVERLAP", "artifact_scopes 声明了未在 artifacts 中的 %s" % rel))
    if not mapping:
        problems.append((
            "BAD_SCOPE",
            "无法把 artifacts 归属到 scopes（需要 artifact_scopes，或 artifacts "
            "与 scopes 一一对应）"))
        return problems
    for sec in seen:
        # 注意：这里直接看映射，不能用 artifacts_of_scope 的「sec.tex 约定」回落，
        # 否则把 sec2.tex 错记到 sec1 名下时会被回落掩盖，写域错配就漏检了。
        if not [rel for rel, owner in mapping.items() if owner == sec]:
            problems.append(
                ("BAD_SCOPE", "小节 %s 在该请求里没有任何产物归属" % sec))
    for rel, sec in sorted(mapping.items()):
        if sec not in seen:
            problems.append(
                ("BAD_SCOPE", "产物 %s 归属的小节 %s 不在 scopes 中" % (rel, sec)))
    return problems


def scope_union_coverage(requests, expected_sections):
    """R3/R4 的 v2 口径：所有请求 scopes 的无重叠并集。

    返回 ``(covered, duplicates, uncovered)``：
        covered    按 expected_sections 顺序的已覆盖小节
        duplicates 被多个请求同时声明的小节（写域冲突）
        uncovered  expected_sections 中没人认领的小节
    """
    expected = [s for s in (expected_sections or []) if isinstance(s, str)]
    owners = {}
    for req in requests or []:
        for sec in request_scopes(req):
            owners.setdefault(sec, []).append(req)
    covered = [sec for sec in expected if sec in owners]
    uncovered = [sec for sec in expected if sec not in owners]
    duplicates = sorted(sec for sec, reqs in owners.items() if len(reqs) > 1)
    return covered, duplicates, uncovered


def plan_is_v2(plan):
    """计划是否采用 v2 多 scope 契约（任何一个请求带 ``scopes`` 键即为 v2）。"""
    if not isinstance(plan, dict):
        return False
    for req in plan.get("requests") or []:
        if is_multi_scope(req):
            return True
    if plan.get("schema_version") == SCHEMA_VERSION:
        return True
    return False


def plan_sections(plan):
    """计划声明的待处理节（保持顺序、去重）。"""
    if not isinstance(plan, dict):
        return []
    out = []
    for sec in plan.get("sections") or []:
        if isinstance(sec, str) and sec and sec not in out:
            out.append(sec)
    return out


def plan_scope_errors(plan, *, max_scopes=None):
    """v2 计划的覆盖完整性 + 写域唯一性校验。

    返回 ``[(code, detail), ...]``。legacy 计划（无 scopes）返回空列表——
    旧口径由 check_dispatch 的 R3/R4 legacy 分支处理，不得在此改语义。
    """
    if not plan_is_v2(plan):
        return []
    sections = plan_sections(plan)
    requests = [r for r in (plan.get("requests") or []) if isinstance(r, dict)]
    if not sections:
        return []
    if not requests:
        # 「没有待处理节 → 允许零派发走 join」由 check_plan 的 requests 空分支处理
        return []
    errors = []
    for req in requests:
        for code, detail in declaration_errors(req, sections, max_scopes=max_scopes):
            errors.append((code, "%s: %s" % (req.get("dispatch_id") or "?", detail)))
    covered, duplicates, uncovered = scope_union_coverage(requests, sections)
    if uncovered:
        errors.append((
            "UNDER_DISPATCH",
            "计划声明 %d 个小节（%s），但请求的 scopes 并集只覆盖 %d 个，"
            "缺 %s —— 每个小节都必须被某个请求的 scopes 认领"
            % (len(sections), ",".join(sections), len(covered),
               ",".join(uncovered))))
    if duplicates:
        errors.append((
            "DUPLICATE_SCOPE",
            "小节 %s 被多个请求同时声明 scope（写域重叠，必须且只能由一个"
            "作者负责）" % ",".join(duplicates)))
    return errors
