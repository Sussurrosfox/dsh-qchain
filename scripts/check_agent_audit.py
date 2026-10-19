#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""check_agent_audit.py — 多 Agent 证据链门禁审计（q_audit.md 根因治理）。

背景：一次 qchain 运行中 T1 映射 / T2 切片 / T4 验收全部由主 Agent 单会话
直写磁盘，宿主无任何 subagent 调用记录。根因是状态机只验文件哈希变化、
不验证写入者身份。本门禁把「谁写的」变成可审计的硬约束：

逐单对账 ``<workdir>/.pd/agent-requests/*.json`` 与证据台账
``<workdir>/.pd/agent-calls/<dispatch_id>.json``：

  EVIDENCE_MISSING             派发单无证据台账（主 Agent 单会话直写产物嫌疑）
  EVIDENCE_NOT_OK              证据在案但 ``status != "ok"``（子 Agent 执行失败）
  EVIDENCE_STALE               证据与盘面产物摘要不符（证据落盘后产物被改写）
  REVIEWER_IDENTITY_VIOLATION  T4 验收 worker 与写作阶段 worker 同体（违反
                               INV-2 创审分离：验收必须由未参与写作的子 Agent 完成）

superseded 规则：旧派发单的产物若已由另一条证据台账（不同 dispatch_id、
status ok、摘要与盘面一致）重新认证（典型场景：返修环重派），则该旧派发单
的缺失/失配只记 WARNING——历史台账不再与当前盘面对应不是违规。

逃生阀：``config.yaml`` 的 ``agent_dispatch.allow_unattested: true`` 时，
EVIDENCE_MISSING 降级为 warning（只 WARN 不 FAIL）；EVIDENCE_NOT_OK /
EVIDENCE_STALE / REVIEWER_IDENTITY_VIOLATION 任何配置下一律 FAIL。

报告 schema 与 check_budget.py 对齐（schema_version 2、tool/tool_version/
input_digest 绑定），可被 deliver.py 的 CHECKS/MUST_PASS 纳管。
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import evidence  # noqa: E402

from workflow.nodes import agent_evidence  # noqa: E402

TOOL_NAME = "check_agent_audit.py"
TOOL_VERSION = "1.0"

REQUESTS_DIR_REL = ".pd/agent-requests"

#: T4 验收批次 ID 前缀（创审分离比对时排除验收阶段自身证据）
T4_BATCH_PREFIX = "bat-t4"


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _artifact_digests(self_dir, artifacts):
    """盘面现值摘要 {rel: sha256|None}。"""
    digests = {}
    for rel in artifacts or []:
        if not isinstance(rel, str) or not rel:
            continue
        digests[rel] = evidence.sha256_file(os.path.join(self_dir, rel))
    return digests


def _load_allow_unattested(self_dir):
    """读 config.yaml 的 agent_dispatch.allow_unattested（逃生阀）。

    惰性导入 workflow 加载器（PyYAML 可用时支持嵌套映射块）；导入或解析失败
    时保守回落 False——门禁侧默认从严。
    """
    try:
        from workflow.nodes.section_batch_nodes import _load_dispatch_config

        return bool(_load_dispatch_config(self_dir).get("allow_unattested"))
    except Exception:
        pass
    path = os.path.join(self_dir or ".", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("allow_unattested:"):
            return stripped.split(":", 1)[1].strip().lower() in (
                "true", "yes", "on", "1")
    return False


def audit_agent_evidence(workdir, allow_unattested=None):
    """对账全部派发单与证据台账，返回 {errors, warnings, checked, dispatches}。"""
    self_dir = os.path.abspath(workdir)
    if allow_unattested is None:
        allow_unattested = _load_allow_unattested(self_dir)

    requests_dir = os.path.join(self_dir, REQUESTS_DIR_REL)
    request_paths = sorted(glob.glob(os.path.join(requests_dir, "*.json")))
    evidence_files = agent_evidence.scan_evidence(self_dir)

    # 盘面当前产物已被哪些 status=ok 证据认证：rel -> set of dispatch_ids where ev sha matches disk sha
    disk_cache = {}
    attested_current = {}
    for ev in evidence_files:
        if str(ev.get("status") or "") != agent_evidence.EVIDENCE_STATUS_OK:
            continue
        eid = str(ev.get("dispatch_id") or "")
        for rel, ev_sha in (ev.get("output_digests") or {}).items():
            if rel not in disk_cache:
                disk_cache[rel] = evidence.sha256_file(os.path.join(self_dir, rel))
            if ev_sha and disk_cache[rel] and ev_sha == disk_cache[rel]:
                attested_current.setdefault(rel, set()).add(eid)

    errors, warnings = [], []
    dispatches = []
    checked = {
        "dispatches": 0,
        "evidence_files": len(evidence_files),
        "attested": 0,
        "unattested": 0,
        "superseded": 0,
    }

    def _record(code, detail, dispatch_id, severity="error"):
        item = {"code": code, "detail": detail, "dispatch_id": dispatch_id}
        (errors if severity == "error" else warnings).append(item)

    for req_path in request_paths:
        request = _load_json(req_path)
        if request is None:
            _record("EVIDENCE_MISSING",
                    f"派发单不可解析或缺失: {os.path.basename(req_path)}", "")
            continue
        dispatch_id = str(request.get("dispatch_id") or "")
        scope = str(request.get("scope") or "")
        stage = str(request.get("stage") or "")
        artifacts = [a for a in (request.get("artifacts") or [])
                     if isinstance(a, str) and a]
        checked["dispatches"] += 1

        ev = agent_evidence.load_evidence(self_dir, dispatch_id)
        disk = _artifact_digests(self_dir, artifacts)
        missing = sorted(rel for rel, sha in disk.items() if sha is None)
        stale = sorted(rel for rel, sha in disk.items()
                       if sha is not None and ev is not None
                       and str((ev.get("output_digests") or {}).get(rel) or "")
                       != sha)
        superseded = (
            all(bool(attested_current.get(rel, set()) - {dispatch_id})
                for rel in stale)
            if stale else False
        )
        if ev is None and artifacts:
            # 产物是否已被另一条证据台账认证（返修重派场景）
            superseded = any(
                bool(attested_current.get(rel, set())) for rel in disk if disk.get(rel)
            )

        if ev is None:
            detail = (f"派发单 {dispatch_id}（{stage}/{scope}）无证据台账"
                      f"（.pd/agent-calls/{dispatch_id}.json），产物由主 Agent "
                      f"单会话直写的嫌疑无法排除")
            if allow_unattested and not superseded:
                _record("EVIDENCE_MISSING", detail, dispatch_id, "warning")
                checked["unattested"] += 1
            elif superseded:
                _record("EVIDENCE_MISSING", detail + "（已被后续派发 superseded）",
                        dispatch_id, "warning")
                checked["superseded"] += 1
            else:
                _record("EVIDENCE_MISSING", detail, dispatch_id)
            dispatches.append({"dispatch_id": dispatch_id, "stage": stage,
                               "scope": scope, "evidence": None,
                               "status": "missing"})
            continue

        status = str(ev.get("status") or "")
        if status != agent_evidence.EVIDENCE_STATUS_OK:
            _record("EVIDENCE_NOT_OK",
                    f"证据台账 {dispatch_id}（{stage}/{scope}）status={status!r} "
                    f"而非 'ok'（子 Agent 执行未成功）", dispatch_id)
            dispatches.append({"dispatch_id": dispatch_id, "stage": stage,
                               "scope": scope, "evidence": status,
                               "status": "not_ok"})
            continue

        if missing:
            _record("EVIDENCE_STALE",
                    f"派发单 {dispatch_id}（{stage}/{scope}）声明的产物已不存在: "
                    + ", ".join(missing), dispatch_id)
        if stale:
            detail = (f"派发单 {dispatch_id}（{stage}/{scope}）的证据摘要与盘面"
                      f"不符（证据落盘后产物被改写）: " + ", ".join(stale))
            if superseded:
                _record("EVIDENCE_STALE", detail + "（已被后续派发 superseded）",
                        dispatch_id, "warning")
                checked["superseded"] += 1
            else:
                _record("EVIDENCE_STALE", detail, dispatch_id)
        if not missing and not stale:
            checked["attested"] += 1
        dispatches.append({"dispatch_id": dispatch_id, "stage": stage,
                           "scope": scope,
                           "evidence": str(ev.get("worker") or ""),
                           "status": "ok" if not (missing or stale) else "stale"})

    # ---- INV-2 创审分离：T4 验收 worker 不得与写作阶段 worker 同体 ----------
    writers = set()
    reviewers = []
    for ev in evidence_files:
        worker = str(ev.get("worker") or "").strip()
        if not worker:
            continue
        if str(ev.get("batch_id") or "").startswith(T4_BATCH_PREFIX):
            reviewers.append((str(ev.get("dispatch_id") or ""), worker))
        else:
            writers.add(worker)
    for dispatch_id, worker in sorted(reviewers):
        if worker in writers:
            _record("REVIEWER_IDENTITY_VIOLATION",
                    f"验收证据 {dispatch_id} 的 worker={worker} 与写作阶段"
                    f"（T1/T2）证据同体，违反 INV-2 创审分离", dispatch_id)

    return {"errors": errors, "warnings": warnings, "checked": checked,
            "dispatches": dispatches}


def check_agent_audit_project(workdir):
    """向后兼容别名（与 check_dispatch_project 同族命名）。"""
    return audit_agent_evidence(workdir)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多 Agent 证据链门禁审计 (check_agent_audit.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="工作区项目目录（默认读取 <workdir>/.pd/agent-requests/）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    ap.add_argument("--allow-unattested", action="store_true",
                    help="无证据台账的派发单只记 WARN 不 FAIL（等价于 config 逃生阀）")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.workdir)
    manifest = evidence.collect_inputs(workdir)
    digest = evidence.content_digest(manifest)

    allow_unattested = True if args.allow_unattested else None
    res = audit_agent_evidence(workdir, allow_unattested=allow_unattested)
    errors, warnings = res["errors"], res["warnings"]
    checked = res["checked"]

    issues = ([{"severity": "error", "code": e["code"], "message": e["detail"],
                "dispatch_id": e.get("dispatch_id", "")} for e in errors]
              + [{"severity": "warning", "code": w["code"],
                  "message": w["detail"], "dispatch_id": w.get("dispatch_id", "")}
                 for w in warnings])

    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "category": "agent-evidence",
        "problems": [f"{e['code']}: {e['detail']}" for e in errors],
        "warnings": [f"{w['code']}: {w['detail']}" for w in warnings],
        "scope": {"workdir": workdir, "requests_dir": REQUESTS_DIR_REL},
        "input_digest": digest,
        "pdf_sha256": None,
        "status": "pass" if not errors else "fail",
        "issues": issues,
        "evidence": {
            "checked": checked,
            "allow_unattested": bool(allow_unattested),
            "dispatches": res["dispatches"],
        },
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        evidence.write_report(args.json, report)
        print("[check_agent_audit] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_agent_audit] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_agent_audit] 发现 %d 个证据链问题（%d 个派发单 / %d 份证据台账）:"
              % (len(errors), checked["dispatches"], checked["evidence_files"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    print("AGENT AUDIT PASS (%d dispatches, %d evidence files)"
          % (checked["dispatches"], checked["evidence_files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
