#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""
check_dispatch.py — T2 切片编写 fan-out 派发计划硬门禁

审计硬规则（“派发了足够的 Agent”）：一个计划声明 3 个小节却只派发 1 个
请求，必须失败。本脚本对 `self/.pd/dispatch-plan.json`（T2 一批 section
writing 派发请求的计划文件）做零信任校验。

检查项（违规 → status "fail"、退出码 1，错误条目 {"code", "detail"}）：
  R1 计划文件缺失 → PLAN_MISSING；JSON 不可解析 → PLAN_BAD_JSON。
  R2 缺失必需顶层键（batch_id, stage, sections, requests, max_concurrency）
     → MISSING_KEY（每个缺失键一条）。
  R3 len(sections) >= 2 且 len(requests) < 2 → UNDER_DISPATCH。
  R4 required_scope_count == 1 或 len(sections) <= 1 时允许单请求
     （min_parallelism 可为 1）；required_scope_count >= 2 却只有 1 个请求
     → UNDER_DISPATCH。requests 为空一律 UNDER_DISPATCH（一个都没派发）。
  R5 逐请求字段：dispatch_id 非空且唯一（DUP_DISPATCH_ID）；scope 非空且在
     sections 内（BAD_SCOPE）；role 非空（BAD_ROLE）；input_digest 匹配
     ^[0-9a-f]{64}$（BAD_DIGEST）；artifacts 为非空字符串数组（BAD_ARTIFACTS）。
  R6 请求自带 batch_id（若存在）必须等于计划 batch_id；计划 batch_id 必须
     非空 → BATCH_MISMATCH。
  R7 max_concurrency 必须是 int >= 1（BAD_CONCURRENCY），且它只表示**执行
     并发上限**（同时活跃请求数）。任务19（C12/PARCFG-04）：排队请求数可以
     大于并发槽位（批次窗口 batch_size 允许排队），因此**不再**用
     len(requests) <= max_concurrency 判违规；改成一致性校验：
       - max_concurrency 不得超过 16（BAD_CONCURRENCY，客户端上限）；
       - 计划若声明 batch_size，必须等于 len(requests)（BATCH_SIZE_MISMATCH）；
       - 计划若声明 effective_concurrency，必须是 int >= 1 且
         <= max_concurrency（BAD_CONCURRENCY）；
       - 计划若声明 executor_kind，必须是 python_registry / host_subagent
         （BAD_EXECUTOR_KIND）；声明了 dispatch_contract_version 的必须是
         受支持版本（BAD_DISPATCH_VERSION）。
     旧计划（无这些字段）仍按 R7 旧要求校验 max_concurrency 本身。
  R8 同一 scope 被多个请求重复派发 → DUPLICATE_SCOPE。
  R9 仅警告（不失败）：min_parallelism > len(requests) → LOW_PARALLELISM。

   R10 仅 T2 写作轮（stage == "T2"；复盘报告 §4.3.3 内容审计前置）：每个
      请求必须携带 source_assets 字典（源侧资产计数 figure/table/tikz），
      且三者均为 >= 0 的整数，否则 BAD_SOURCE_ASSETS（一条请求一条错误）；
      source_assets.available 为 false（源清单缺失、无对账基线）时记警告
      SOURCE_ASSETS_UNAVAILABLE（不失败）。其他轮次（T2_CHECK / T4 等）
      不适用该规则，不受影响。

防御性结构码（超出 R1–R10 的兜底，避免计划畸形时抛栈而非干净失败）：
  BAD_PLAN_STRUCTURE：根节点不是对象 / sections 或 requests 类型错误；
  BAD_REQUEST：requests[i] 不是对象。

用法: python check_dispatch.py [工作目录] [--plan PATH] [--json PATH]
退出码: 0=通过, 1=发现问题。
"""
import argparse
import json
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# C5：多 scope 派发单的唯一正规化口径（与写作节点、来源链共用同一份实现）
import request_scopes  # noqa: E402

TOOL_NAME = "check_dispatch.py"
DEFAULT_PLAN_REL = os.path.join(".pd", "dispatch-plan.json")

REQUIRED_KEYS = ("batch_id", "stage", "sections", "requests", "max_concurrency")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# 错误码（R1–R10）
PLAN_MISSING = "PLAN_MISSING"
PLAN_BAD_JSON = "PLAN_BAD_JSON"
MISSING_KEY = "MISSING_KEY"
UNDER_DISPATCH = "UNDER_DISPATCH"
DUP_DISPATCH_ID = "DUP_DISPATCH_ID"
BAD_SCOPE = "BAD_SCOPE"
BAD_ROLE = "BAD_ROLE"
BAD_DIGEST = "BAD_DIGEST"
BAD_ARTIFACTS = "BAD_ARTIFACTS"
BATCH_MISMATCH = "BATCH_MISMATCH"
OVER_CONCURRENCY = "OVER_CONCURRENCY"
BAD_CONCURRENCY = "BAD_CONCURRENCY"
DUPLICATE_SCOPE = "DUPLICATE_SCOPE"
#: 任务19（C12）：执行器计划契约的字段一致性
BATCH_SIZE_MISMATCH = "BATCH_SIZE_MISMATCH"
BAD_EXECUTOR_KIND = "BAD_EXECUTOR_KIND"
BAD_DISPATCH_VERSION = "BAD_DISPATCH_VERSION"
#: R10：请求缺少/畸形的源资产计数（复盘报告 §4.3.3 内容审计前置）
BAD_SOURCE_ASSETS = "BAD_SOURCE_ASSETS"
# 防御性结构码
BAD_PLAN_STRUCTURE = "BAD_PLAN_STRUCTURE"
BAD_REQUEST = "BAD_REQUEST"

ERROR_CODES = (
    PLAN_MISSING, PLAN_BAD_JSON, MISSING_KEY, UNDER_DISPATCH, DUP_DISPATCH_ID,
    BAD_SCOPE, BAD_ROLE, BAD_DIGEST, BAD_ARTIFACTS, BATCH_MISMATCH,
    OVER_CONCURRENCY, BAD_CONCURRENCY, DUPLICATE_SCOPE, BAD_SOURCE_ASSETS,
    BAD_PLAN_STRUCTURE, BAD_REQUEST,
    BATCH_SIZE_MISMATCH, BAD_EXECUTOR_KIND, BAD_DISPATCH_VERSION,
)
# 警告码（R9 / R10，均不失败）
LOW_PARALLELISM = "LOW_PARALLELISM"
SOURCE_ASSETS_UNAVAILABLE = "SOURCE_ASSETS_UNAVAILABLE"
WARNING_CODES = (LOW_PARALLELISM, SOURCE_ASSETS_UNAVAILABLE)

#: R10 适用的计划轮次：T2 切片写作轮（stage 归一化后精确等于 "T2"）。
#: T2_CHECK（语义审读轮）/ T4（独立验收轮）等不携带源资产计数，不误伤。
SOURCE_ASSET_STAGES = ("T2",)
#: source_assets 必须为非负整数的计数字段
SOURCE_ASSET_COUNT_KEYS = ("figure", "table", "tikz")

#: 任务19（C12）：客户端并发硬上限（与 workflow/dispatch_policy.py 的
#: MAX_CONCURRENCY_LIMIT 同值；计划声明超过它即拒绝）
MAX_CONCURRENCY_LIMIT = 16
#: 执行器种类：python_registry（进程内 worker 池）| host_subagent（宿主子 Agent）
EXECUTOR_KINDS = ("python_registry", "host_subagent")
#: 受支持的派发契约版本（1=legacy 计划，2=任务19 执行器计划契约）
SUPPORTED_DISPATCH_VERSIONS = (1, 2)


def _err(code, detail):
    return {"code": code, "detail": detail}


def _is_int(value):
    """真整数判定：bool 是 int 子类，必须显式排除（True 不是并发上限）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value):
    return isinstance(value, str) and bool(value.strip())


def load_plan(plan_path):
    """读取并解析派发计划；返回 (plan, errors)。plan 为 None 表示不可用。"""
    if not os.path.isfile(plan_path):
        return None, [_err(
            PLAN_MISSING,
            "派发计划缺失: %s（流水线必须在派发 fan-out 批次前写入 %s）"
            % (plan_path, DEFAULT_PLAN_REL.replace(os.sep, "/")))]
    try:
        # utf-8-sig：透明吞掉 Windows 工具/编辑器常带的 BOM（无 BOM 时等价 utf-8）
        with open(plan_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (ValueError, UnicodeDecodeError) as exc:  # JSON 解析失败
        return None, [_err(
            PLAN_BAD_JSON, "派发计划不是合法 JSON: %s (%s)" % (plan_path, exc))]
    except OSError as exc:
        return None, [_err(PLAN_MISSING, "派发计划不可读: %s (%s)" % (plan_path, exc))]
    return data, []


def check_plan(plan):
    """对已解析的计划执行 R2–R10；返回 (errors, warnings, stats)。"""
    errors, warnings = [], []
    stats = {"sections": 0, "requests": 0}

    if not isinstance(plan, dict):
        errors.append(_err(BAD_PLAN_STRUCTURE, "派发计划根节点必须是 JSON 对象"))
        return errors, warnings, stats

    # ---- R2 必需顶层键 ----
    for key in REQUIRED_KEYS:
        if key not in plan:
            errors.append(_err(MISSING_KEY, "派发计划缺失必需顶层键: %s" % key))

    sections = plan.get("sections")
    if not isinstance(sections, list):
        if "sections" in plan:
            errors.append(_err(BAD_PLAN_STRUCTURE,
                               "sections 必须是字符串数组（当前 %r）" % (sections,)))
        sections = []
    requests = plan.get("requests")
    if not isinstance(requests, list):
        if "requests" in plan:
            errors.append(_err(BAD_PLAN_STRUCTURE,
                               "requests 必须是派发请求对象数组（当前 %r）" % (requests,)))
        requests = []

    stats["sections"] = len(sections)
    stats["requests"] = len(requests)

    batch_id = plan.get("batch_id")
    if not _nonempty_str(batch_id):
        errors.append(_err(BATCH_MISMATCH,
                           "计划 batch_id 必须是非空字符串（当前 %r），"
                           "否则无法对齐批次身份" % (batch_id,)))

    # ---- R7 并发上限（任务19：并发槽位 ≠ 排队请求数）----
    max_conc = plan.get("max_concurrency")
    if not _is_int(max_conc) or max_conc < 1:
        errors.append(_err(BAD_CONCURRENCY,
                           "max_concurrency 必须是 >= 1 的整数（当前 %r）" % (max_conc,)))
    elif max_conc > MAX_CONCURRENCY_LIMIT:
        errors.append(_err(
            BAD_CONCURRENCY,
            "max_concurrency=%d 超过客户端并发上限 %d（拒绝无上限的批次）"
            % (max_conc, MAX_CONCURRENCY_LIMIT)))
    else:
        # 排队请求数可以大于并发槽位（batch_size 是队列长度），因此这里不再
        # 用 len(requests) 与并发上限比较；改为校验声明字段之间的一致性。
        batch_size = plan.get("batch_size")
        if batch_size is not None:
            # 卡13（C5）：没有待处理节时允许零派发（requests 为空走 join），
            # 此时 batch_size 合法值为 0；其余情况 batch_size 必须 >= 1。
            min_batch = 0 if not requests else 1
            if not _is_int(batch_size) or batch_size < min_batch:
                errors.append(_err(
                    BATCH_SIZE_MISMATCH,
                    "batch_size 必须是 >= %d 的整数（当前 %r）"
                    % (min_batch, batch_size)))
            elif batch_size != len(requests):
                errors.append(_err(
                    BATCH_SIZE_MISMATCH,
                    "batch_size=%d 与实际排队请求数 %d 不一致（计划字段自相矛盾）"
                    % (batch_size, len(requests))))
        effective = plan.get("effective_concurrency")
        if effective is not None:
            if not _is_int(effective) or effective < 1:
                errors.append(_err(
                    BAD_CONCURRENCY,
                    "effective_concurrency 必须是 >= 1 的整数（当前 %r）"
                    % (effective,)))
            elif effective > max_conc:
                errors.append(_err(
                    BAD_CONCURRENCY,
                    "effective_concurrency=%d 超过 max_concurrency=%d"
                    "（有效并发不得高于执行上限）" % (effective, max_conc)))
        executor_kind = plan.get("executor_kind")
        if executor_kind is not None and executor_kind not in EXECUTOR_KINDS:
            errors.append(_err(
                BAD_EXECUTOR_KIND,
                "executor_kind=%r 非法，只能是: %s"
                % (executor_kind, " / ".join(EXECUTOR_KINDS))))
        version = plan.get("dispatch_contract_version")
        if version is not None and version not in SUPPORTED_DISPATCH_VERSIONS:
            errors.append(_err(
                BAD_DISPATCH_VERSION,
                "dispatch_contract_version=%r 不受支持，只能是: %s"
                % (version, " / ".join(str(v) for v in SUPPORTED_DISPATCH_VERSIONS))))

    # ---- R3 / R4 覆盖完整性 ----
    # 卡13（C5）：v2 计划允许「一个作者写 1–2 个相邻节」，因此不能用
    # 「请求数 == 节数」当覆盖判据——改为按所有请求 scopes 的无重叠并集判定。
    # legacy 计划（请求不带 scopes 键）保持旧口径：每节一个请求。
    required = plan.get("required_scope_count")
    single_allowed = (required == 1) or (len(sections) <= 1)
    plan_v2 = request_scopes.plan_is_v2(plan)
    if len(requests) == 0:
        # 没有待处理节 → 允许零派发走 join（不得报"一个都没派发"）
        if sections:
            errors.append(_err(UNDER_DISPATCH,
                               "派发计划未包含任何请求（requests 为空），"
                               "切片编写任务一个都没派发"))
    elif plan_v2:
        # v2：请求数 < 节数是合法的（一个作者写 1–2 个相邻节），因此这里只验
        # 「并集覆盖 + 写域唯一」，不再用 required_scope_count 要求并行请求数。
        for code, detail in request_scopes.plan_scope_errors(plan):
            errors.append(_err(code, detail))
    elif len(requests) == 1:
        if len(sections) >= 2:
            errors.append(_err(
                UNDER_DISPATCH,
                "计划声明 %d 个小节（%s）却只派发了 1 个请求，"
                "必须为每个小节派发独立编写 Agent"
                % (len(sections), ", ".join(str(s) for s in sections))))
        elif not single_allowed:
            errors.append(_err(
                UNDER_DISPATCH,
                "required_scope_count=%r 要求至少 2 个并行派发，实际只有 1 个请求"
                % (required,)))

    # ---- R5 / R6 / R8 逐请求 ----
    seen_ids = {}
    seen_scopes = {}
    # R10 只在 T2 写作轮生效（内容审计前置：切片派发单必须带源资产计数）
    stage_norm = str(plan.get("stage") or "").strip().upper()
    check_source_assets = stage_norm in SOURCE_ASSET_STAGES
    for idx, req in enumerate(requests):
        label = "requests[%d]" % idx
        if not isinstance(req, dict):
            errors.append(_err(BAD_REQUEST,
                               "%s 必须是对象（dispatch_id/scope/role/"
                               "input_digest/artifacts）" % label))
            continue

        did = req.get("dispatch_id")
        if not _nonempty_str(did):
            errors.append(_err(DUP_DISPATCH_ID,
                               "%s 的 dispatch_id 必须是非空字符串（当前 %r）"
                               % (label, did)))
        elif did in seen_ids:
            errors.append(_err(DUP_DISPATCH_ID,
                               "%s 的 dispatch_id “%s” 与 %s 重复，派发身份必须唯一"
                               % (label, did, seen_ids[did])))
        else:
            seen_ids[did] = label

        scope = req.get("scope")
        multi = request_scopes.is_multi_scope(req)
        req_scopes = request_scopes.request_scopes(req)
        if not _nonempty_str(scope):
            errors.append(_err(BAD_SCOPE,
                               "%s 的 scope 必须是非空字符串（当前 %r）"
                               % (label, scope)))
        elif sections and scope not in sections and not multi:
            errors.append(_err(BAD_SCOPE,
                               "%s 的 scope “%s” 不在计划 sections %s 中"
                               % (label, scope, ", ".join(str(s) for s in sections))))
        if multi:
            # C5：v2 请求的授权真源是 scopes——逐条校验合法性 / 唯一 / 有序 /
            # 相邻 / 写域归属，并确认 scope 兼容标签就是 scopes[0]。
            if req_scopes and scope and scope != req_scopes[0]:
                errors.append(_err(
                    BAD_SCOPE,
                    "%s 的 scope “%s” 不是 scopes 首元素 “%s”（v2 里 scope 只是"
                    " first-scope 兼容标签，必须等于 scopes[0]）"
                    % (label, scope, req_scopes[0])))
            for code, detail in request_scopes.declaration_errors(
                    req, sections or None):
                errors.append(_err(code, "%s: %s" % (label, detail)))

        if not _nonempty_str(req.get("role")):
            errors.append(_err(BAD_ROLE,
                               "%s 的 role 必须是非空字符串（当前 %r）"
                               % (label, req.get("role"))))

        digest = req.get("input_digest")
        if not (isinstance(digest, str) and DIGEST_RE.match(digest)):
            errors.append(_err(BAD_DIGEST,
                               "%s 的 input_digest 必须是 64 位小写十六进制摘要"
                               "（当前 %r）" % (label, digest)))

        artifacts = req.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts \
                or any(not _nonempty_str(a) for a in artifacts):
            errors.append(_err(BAD_ARTIFACTS,
                               "%s 的 artifacts 必须是非空字符串数组（当前 %r）"
                               % (label, artifacts)))

        if req.get("batch_id") is not None and req.get("batch_id") != batch_id:
            errors.append(_err(BATCH_MISMATCH,
                               "%s 的 batch_id “%s” 与计划 batch_id “%s” 不一致"
                               % (label, req.get("batch_id"), batch_id)))

        # 写域唯一性：v2 逐 scope 登记（一个请求可以合法认领多节），legacy 仍按
        # 单个 scope 登记。任一 scope 被两个请求同时认领即写域冲突。
        for item in (req_scopes if multi else ([scope] if _nonempty_str(scope) else [])):
            if item in seen_scopes:
                errors.append(_err(DUPLICATE_SCOPE,
                                   "scope “%s” 被 %s 与 %s 重复派发，"
                                   "同一切片不得派发多个编写 Agent"
                                   % (item, seen_scopes[item], label)))
            else:
                seen_scopes[item] = label

        # ---- R10 源资产计数（仅 T2；复盘报告 §4.3.3）----
        if check_source_assets:
            assets = req.get("source_assets")
            if not isinstance(assets, dict):
                errors.append(_err(
                    BAD_SOURCE_ASSETS,
                    "%s 缺少 source_assets 字典（源侧图形/表格/TikZ 资产计数，"
                    "切片验收 Agent 的逐项对账基线；由派发节点写入请求 "
                    "context.source_assets 并随计划落盘）" % label))
            else:
                bad = [key for key in SOURCE_ASSET_COUNT_KEYS
                       if not _is_int(assets.get(key)) or assets[key] < 0]
                if bad:
                    errors.append(_err(
                        BAD_SOURCE_ASSETS,
                        "%s 的 source_assets 字段 %s 必须是非负整数（当前 %r）"
                        % (label, "/".join(bad),
                           {key: assets.get(key) for key in bad})))
                if not assets.get("available", True):
                    warnings.append({
                        "code": SOURCE_ASSETS_UNAVAILABLE,
                        "detail": "%s 的 source_assets.available=false"
                                  "（源清单缺失，无源侧对账基线），"
                                  "切片资产守恒只能凭验收 Agent 人工核对" % label})

    # ---- R9 仅警告 ----
    # 任务21（C12.4）：低并行只记原因，不做内容硬门禁（仅剩一单 / 缓存命中 /
    # 实际依赖 / 宿主限流都能合法压低并行度），更不得触发返修重写。
    # 计划期没有运行事件，peak 一律 unknown——不得写成实测 0。
    min_par = plan.get("min_parallelism")
    if _is_int(min_par) and min_par > len(requests):
        reason_code, reason_source = _serial_reason_from_plan(plan, len(requests))
        warnings.append({"code": LOW_PARALLELISM, "detail":
                         "min_parallelism=%d 大于实际派发请求数 %d，"
                         "批次并行度低于预期，请确认是否漏派发"
                         "（原因 %s/%s；实测峰值 unknown：计划期无运行事件，"
                         "请核对 .pd/dispatch-runs/<batch_id>.json）"
                         % (min_par, len(requests), reason_code, reason_source)})

    return errors, warnings, stats


def _serial_reason_from_plan(plan, request_count):
    """计划期可判定的低并行原因（仅分类，不做实测断言）。

    返回 (reason_code, reason_source)；无法判定统一 host_queue_or_unknown
    + unknown 来源，绝不臆造数值。
    """
    if request_count <= 1:
        return "single_ready", "plan_request_count"
    effective = plan.get("effective_concurrency")
    configured = plan.get("concurrency_source") or ""
    worker_caps = plan.get("worker_capabilities") or {}
    if _is_int(effective) and effective <= 1:
        if _is_int(worker_caps.get("max_concurrency")) and worker_caps["max_concurrency"] <= 1:
            return "worker_cap", "plan_worker_capabilities"
        if str(configured).startswith(("config:", "tool:", "frozen:")):
            return "explicit_safe", str(configured)
    if plan.get("artifact_dependencies") or plan.get("dependency_map"):
        return "artifact_dependency", "plan_dependency_map"
    return "host_queue_or_unknown", "unknown:no_runtime_events"


def check_dispatch_project(workdir, plan_path=None):
    """执行派发计划门禁；返回 {"errors", "warnings", "checked", "plan"}。"""
    workdir = os.path.abspath(workdir)
    if plan_path:
        plan_path = os.path.abspath(plan_path)
    else:
        plan_path = os.path.join(workdir, DEFAULT_PLAN_REL)

    plan, load_errors = load_plan(plan_path)
    if load_errors:
        return {"errors": load_errors, "warnings": [],
                "checked": {"sections": 0, "requests": 0}, "plan": plan_path}

    errors, warnings, stats = check_plan(plan)
    return {"errors": errors, "warnings": warnings, "checked": stats,
            "plan": plan_path}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T2 切片编写 fan-out 派发计划硬门禁 (check_dispatch.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="工作区项目目录（默认读取 <workdir>/%s）"
                         % DEFAULT_PLAN_REL.replace(os.sep, "/"))
    ap.add_argument("--plan", metavar="PATH",
                    help="派发计划路径（默认 <workdir>/%s）"
                         % DEFAULT_PLAN_REL.replace(os.sep, "/"))
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    res = check_dispatch_project(args.workdir, args.plan)
    errors, warnings = res["errors"], res["warnings"]

    report = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "checked": res["checked"],
        "plan": res["plan"],
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[check_dispatch] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_dispatch] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_dispatch] 发现 %d 个问题（派发计划不足或不合规，计划: %s）:"
              % (len(errors), res["plan"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    print("[check_dispatch] 通过：派发计划合规（%d 个请求 / %d 个小节，计划: %s）。"
          % (res["checked"]["requests"], res["checked"]["sections"], res["plan"]))
    return 0


# 向后兼容别名
check_dispatch = check_dispatch_project


if __name__ == "__main__":
    sys.exit(main())
