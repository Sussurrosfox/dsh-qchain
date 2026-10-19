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
  R7 len(requests) > max_concurrency → OVER_CONCURRENCY；max_concurrency
     必须是 int >= 1（BAD_CONCURRENCY）。
  R8 同一 scope 被多个请求重复派发 → DUPLICATE_SCOPE。
  R9 仅警告（不失败）：min_parallelism > len(requests) → LOW_PARALLELISM。

防御性结构码（超出 R1–R9 的兜底，避免计划畸形时抛栈而非干净失败）：
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

TOOL_NAME = "check_dispatch.py"
DEFAULT_PLAN_REL = os.path.join(".pd", "dispatch-plan.json")

REQUIRED_KEYS = ("batch_id", "stage", "sections", "requests", "max_concurrency")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# 错误码（R1–R9）
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
# 警告码（R9，不失败）
LOW_PARALLELISM = "LOW_PARALLELISM"
# 防御性结构码
BAD_PLAN_STRUCTURE = "BAD_PLAN_STRUCTURE"
BAD_REQUEST = "BAD_REQUEST"

ERROR_CODES = (
    PLAN_MISSING, PLAN_BAD_JSON, MISSING_KEY, UNDER_DISPATCH, DUP_DISPATCH_ID,
    BAD_SCOPE, BAD_ROLE, BAD_DIGEST, BAD_ARTIFACTS, BATCH_MISMATCH,
    OVER_CONCURRENCY, BAD_CONCURRENCY, DUPLICATE_SCOPE,
    BAD_PLAN_STRUCTURE, BAD_REQUEST,
)
WARNING_CODES = (LOW_PARALLELISM,)


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
    """对已解析的计划执行 R2–R9；返回 (errors, warnings, stats)。"""
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

    # ---- R7 并发上限 ----
    max_conc = plan.get("max_concurrency")
    if not _is_int(max_conc) or max_conc < 1:
        errors.append(_err(BAD_CONCURRENCY,
                           "max_concurrency 必须是 >= 1 的整数（当前 %r）" % (max_conc,)))
    elif len(requests) > max_conc:
        errors.append(_err(
            OVER_CONCURRENCY,
            "派发请求数 %d 超过 max_concurrency=%d（批次并发上限被突破）"
            % (len(requests), max_conc)))

    # ---- R3 / R4 并行度硬规则 ----
    required = plan.get("required_scope_count")
    single_allowed = (required == 1) or (len(sections) <= 1)
    if len(requests) == 0:
        errors.append(_err(UNDER_DISPATCH,
                           "派发计划未包含任何请求（requests 为空），"
                           "切片编写任务一个都没派发"))
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
        if not _nonempty_str(scope):
            errors.append(_err(BAD_SCOPE,
                               "%s 的 scope 必须是非空字符串（当前 %r）"
                               % (label, scope)))
        elif sections and scope not in sections:
            errors.append(_err(BAD_SCOPE,
                               "%s 的 scope “%s” 不在计划 sections %s 中"
                               % (label, scope, ", ".join(str(s) for s in sections))))

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

        if _nonempty_str(scope):
            if scope in seen_scopes:
                errors.append(_err(DUPLICATE_SCOPE,
                                   "scope “%s” 被 %s 与 %s 重复派发，"
                                   "同一切片不得派发多个编写 Agent"
                                   % (scope, seen_scopes[scope], label)))
            else:
                seen_scopes[scope] = label

    # ---- R9 仅警告 ----
    min_par = plan.get("min_parallelism")
    if _is_int(min_par) and min_par > len(requests):
        warnings.append({"code": LOW_PARALLELISM, "detail":
                         "min_parallelism=%d 大于实际派发请求数 %d，"
                         "批次并行度低于预期，请确认是否漏派发"
                         % (min_par, len(requests))})

    return errors, warnings, stats


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
