#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""progress.py — 派发进度快照（audit §14 UI 进度指标）

动机：UI 需要每 1-2 秒可轮询的确定性进度视图（阶段 / scope / 队列 /
运行中 / 缓存命中 / 预计剩余），而不是猜。本脚本只读盘面证据：

  .pd/run.json                     切片任务状态（pdstate）
  .pd/dispatch-plan.json           写作轮派发计划
  .pd/dispatch-plan-semantic.json  语义审读轮派发计划（§6）
  .pd/agent-requests/*.json        每张派发单的状态
  .pd/agent-calls/*.json           证据台账（duration_ms 用于 ETA）
  .pd/artifact-cache/*.json        派生物缓存条目（缓存命中面）

输出为单行进度摘要 + 可选 --json 结构化报告；零副作用、可并发轮询。

用法: python progress.py [工作目录] [--json PATH]
"""
import argparse
import glob
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pdstate  # noqa: E402（scripts/ 已在 sys.path）

RUN_REL = os.path.join(".pd", "run.json")
REQUESTS_DIR_REL = os.path.join(".pd", "agent-requests")
CALLS_DIR_REL = os.path.join(".pd", "agent-calls")
CACHE_DIR_REL = os.path.join(".pd", "artifact-cache")
AUTHOR_PLAN_REL = os.path.join(".pd", "dispatch-plan.json")
CHECK_PLAN_REL = os.path.join(".pd", "dispatch-plan-semantic.json")

#: 任务终态（pdstate 状态机）
DONE_STATUSES = ("checked", "submitted", "accepted", "staged", "reused", "skipped")
RUNNING_STATUSES = ("running", "produced")
QUEUE_STATUSES = ("planned", "ready", "pending")

#: 派发计划 → 人类可读阶段名
ROUND_LABELS = {
    "semantic": "T2 语义审读轮",
    "author": "T2 写作轮",
    "acceptance": "T4 验收轮",
}


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_plan(self_dir, rel):
    return _load_json(os.path.join(self_dir, rel))


def _request_statuses(self_dir):
    out = {}
    for path in sorted(glob.glob(os.path.join(self_dir, REQUESTS_DIR_REL,
                                              "*.json"))):
        req = _load_json(path)
        if not isinstance(req, dict):
            continue
        did = str(req.get("dispatch_id") or "")
        if did:
            out[did] = str(req.get("status") or "pending")
    return out


def _avg_duration_ms(self_dir):
    """已完成派发的平均耗时（ETA 基准；无证据返 None）。"""
    durations = []
    for path in sorted(glob.glob(os.path.join(self_dir, CALLS_DIR_REL,
                                              "*.json"))):
        ev = _load_json(path)
        if not isinstance(ev, dict):
            continue
        if str(ev.get("status") or "") != "ok":
            continue
        try:
            ms = int(ev.get("duration_ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms > 0:
            durations.append(ms)
    if not durations:
        return None
    return int(sum(durations) / len(durations))


def _cache_entries(self_dir):
    return len(glob.glob(os.path.join(self_dir, CACHE_DIR_REL, "*.json")))


def _stage_label(run, plans):
    """从盘面推断当前阶段（无 LangGraph 状态可达时的确定性近似）。"""
    if not plans:
        return "T1 规划/派发前"
    # 后写的计划优先：语义审读轮在写作轮之后
    if CHECK_PLAN_REL in plans and plans[CHECK_PLAN_REL]:
        return ROUND_LABELS.get("semantic", "T2 语义审读轮")
    plan = plans.get(AUTHOR_PLAN_REL) or {}
    stage = str(plan.get("stage") or "")
    if stage.startswith("T4"):
        return ROUND_LABELS.get("acceptance", "T4 验收轮")
    return ROUND_LABELS.get(str(plan.get("round") or "author"), "T2 写作轮")


def progress_snapshot(workdir):
    self_dir = os.path.abspath(workdir)
    run = pdstate.load_run(self_dir)[0] if os.path.isfile(
        os.path.join(self_dir, RUN_REL)) else None

    plans = {}
    for rel in (AUTHOR_PLAN_REL, CHECK_PLAN_REL):
        plan = _load_plan(self_dir, rel)
        if plan:
            plans[rel] = plan

    req_status = _request_statuses(self_dir)

    scopes = {"total": 0, "queued": 0, "running": 0, "done": 0, "reused": 0}
    if run:
        for tid in run.get("slices") or []:
            task = (run.get("tasks") or {}).get(tid) or {}
            status = str(task.get("status") or "planned")
            scopes["total"] += 1
            if status in DONE_STATUSES:
                scopes["done"] += 1
                if status in ("reused", "skipped"):
                    scopes["reused"] += 1
            elif status in RUNNING_STATUSES:
                scopes["running"] += 1
            else:
                scopes["queued"] += 1

    in_flight = sum(1 for s in req_status.values() if s == "running")
    queue_depth = sum(1 for s in req_status.values() if s == "pending")

    remaining = max(scopes["queued"] + scopes["running"], 0)
    avg_ms = _avg_duration_ms(self_dir)
    eta_seconds = None
    if avg_ms and remaining:
        # 计划并发度（保守取 1 时退化为串行估计）
        limit = 1
        for plan in plans.values():
            try:
                limit = max(limit, int(plan.get("max_concurrency") or 1))
            except (TypeError, ValueError):
                pass
        eta_seconds = round(remaining * avg_ms / 1000.0 / max(limit, 1), 1)

    return {
        "run_id": (run or {}).get("run_id"),
        "stage": _stage_label(run, plans),
        "scopes": scopes,
        "queue_depth": queue_depth,
        "in_flight": in_flight,
        "cache": {"entries": _cache_entries(self_dir)},
        "avg_dispatch_ms": avg_ms,
        "eta_seconds": eta_seconds,
        "plans": {
            os.path.basename(rel): {
                "batch_id": plan.get("batch_id"),
                "round": plan.get("round"),
                "stage": plan.get("stage"),
                "sections": len(plan.get("sections") or []),
                "requests": len(plan.get("requests") or []),
                "max_concurrency": plan.get("max_concurrency"),
            }
            for rel, plan in plans.items()
        },
        "workdir": self_dir,
    }


def format_line(snap):
    s = snap["scopes"]
    eta = ("%ss" % snap["eta_seconds"]) if snap["eta_seconds"] is not None else "未知"
    return ("[进度] 阶段=%s | scope %d（完成 %d / 运行 %d / 排队 %d / 复用 %d）"
            " | 队列=%d 运行中=%d | 缓存条目=%d | 预计剩余≈%s"
            % (snap["stage"], s["total"], s["done"], s["running"],
               s["queued"], s["reused"], snap["queue_depth"],
               snap["in_flight"], snap["cache"]["entries"], eta))


def main(argv=None):
    ap = argparse.ArgumentParser(description="派发进度快照 (progress.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化快照")
    args = ap.parse_args(argv)

    snap = progress_snapshot(args.workdir)
    if not os.path.isfile(os.path.join(snap["workdir"], RUN_REL)):
        print("[进度] 未找到 .pd/run.json（流水线尚未初始化派发状态）")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
                f.write("\n")
        return 0

    print(format_line(snap))
    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[进度] 结构化快照已写入 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
