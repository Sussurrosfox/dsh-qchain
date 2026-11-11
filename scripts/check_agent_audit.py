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
  REVIEWER_ROUTE_NOT_SEPARATED 验收与写作的**生效模型路由**完全相同（worker 不同、
                               模型相同）——host 默认让子 Agent 继承父会话路由，
                               未配 model_map 时这是默认形态，独立审读在模型层不存在
  REVIEWER_ROUTE_UNRECORDED    （WARN）验收证据没记录生效路由，无法核对模型层分离

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
import hashlib
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
import request_scopes  # noqa: E402  C5：多 scope 授权唯一口径
from check_semantic import load_shuffle_transitions, shuffle_bound  # noqa: E402

from workflow.nodes import agent_evidence  # noqa: E402

TOOL_NAME = "check_agent_audit.py"
TOOL_VERSION = "1.0"

REQUESTS_DIR_REL = ".pd/agent-requests"

#: 派生物缓存目录与复用台账（任务16/C7：缓存命中也是需要审计的事实）
CACHE_DIR_REL = ".pd/artifact-cache"
CACHE_REUSE_LEDGER_REL = ".pd/artifact-cache/reuse-ledger.json"

#: 缓存复用台账对账失败码（任务16/C7）
CACHE_REUSE_UNVERIFIED = "CACHE_REUSE_UNVERIFIED"
CACHE_REUSE_DRIFT = "CACHE_REUSE_DRIFT"
CACHE_LEDGER_CORRUPT = "CACHE_LEDGER_CORRUPT"

#: T4 验收批次 ID 前缀（创审分离比对时排除验收阶段自身证据）
T4_BATCH_PREFIX = "bat-t4"
#: 合并终审批次 ID 前缀（卡14：一次独立审读覆盖多节，仍是审读角色）
T4_COMBINED_BATCH_PREFIX = "bat-t4c"


def _is_reviewer_evidence(self_dir, ev):
    """该条证据是否属于「审读角色」（卡14）。

    角色真源 = 派发单声明的 stage（``T4_COMBINED_REVIEW`` /
    ``T4_REVIEW_<SEC>``），批次前缀仅作兼容兜底（请求文件可能已被清理）。
    实现落在 :mod:`review_projection`，与投影/认证侧共用同一口径，避免两处
    判定漂移。导入失败时退化为旧的前缀判定（绝不因此把审读误记为写作者）。
    """
    try:
        import review_projection

        return bool(review_projection.is_reviewer_evidence(self_dir, ev))
    except Exception:
        batch = str((ev or {}).get("batch_id") or "")
        return batch.startswith(T4_BATCH_PREFIX) or batch.startswith(
            T4_COMBINED_BATCH_PREFIX)


def _evidence_route(ev):
    """证据台账里的模型路由 (provider, model)；未知返回 None。

    N3 起台账分列 requested_*（请求的）与 effective_*（子 Agent 会话实际用的）。
    判定创审是否真的换了模型必须用生效值；旧台账没有该字段时退回
    provider/model（那时它就是请求值，且多半为 null → 视为未知）。
    """
    if not isinstance(ev, dict):
        return None
    for key in (("effective_provider", "effective_model"),
                ("provider", "model")):
        provider = ev.get(key[0])
        model = ev.get(key[1])
        if provider and model:
            return (str(provider), str(model))
    return None


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


def _load_cache_ledger(self_dir):
    """读缓存复用台账；返回 {entries, corrupt}（缺失=空台账，损坏=corrupt）。"""
    path = os.path.join(self_dir, *CACHE_REUSE_LEDGER_REL.split("/"))
    if not os.path.isfile(path):
        return {"entries": [], "corrupt": False}
    data = _load_json(path)
    if data is None or not isinstance(data.get("entries"), list):
        return {"entries": [], "corrupt": True}
    return {"entries": [e for e in data["entries"] if isinstance(e, dict)],
            "corrupt": False}


def verify_cache_reuse_ledger(self_dir, attested_current=None):
    """对账缓存复用台账：命中必须能和真实历史派发双向对得上（任务16/C7）。

    C7 红线「缓存绝不能伪造一个从未运行过的 agent」。每条 ``hit=True`` 的台账
    必须同时满足：

    1. 引用的缓存条目真实存在，且条目里的 ``input_digest`` 与台账写的一致
       （台账不能自说自话）；
    2. 存在一份**历史派发单/回执**带同一 ``input_digest`` —— 证明这份产物确实
       由某个 agent 产出过，而不是凭空补出来的；
    3. 条目记录的产物摘要与盘面现值一致（命中后又被手改 → 复用不成立）。

    任一不满足 → error。命中条目**绝不**计入 ``dispatches``（它不是一次新的
    调用，也不能把命中数伪装成派发数）。
    """
    self_dir = os.path.abspath(self_dir or ".")
    ledger = _load_cache_ledger(self_dir)
    reuses, errors = [], []
    if ledger.get("corrupt"):
        errors.append({"code": CACHE_LEDGER_CORRUPT,
                       "detail": "缓存复用台账不可解析（%s）" % CACHE_REUSE_LEDGER_REL,
                       "dispatch_id": ""})
        return reuses, errors

    # 历史派发单/回执摘要索引：input_digest -> {dispatch_id, ...}
    historical = {}
    for path in sorted(glob.glob(os.path.join(self_dir, REQUESTS_DIR_REL, "*.json"))):
        req = _load_json(path)
        if not req:
            continue
        digest = str(req.get("input_digest") or "")
        if digest:
            historical.setdefault(digest, set()).add(
                str(req.get("dispatch_id") or ""))
    try:
        for ev in agent_evidence.scan_evidence(self_dir):
            digest = str((ev or {}).get("input_digest") or "")
            if digest:
                historical.setdefault(digest, set()).add(
                    str((ev or {}).get("dispatch_id") or ""))
    except Exception:  # noqa: BLE001 - 回执索引只是佐证，缺失不掩盖派发单索引
        pass

    for entry in ledger["entries"]:
        if not entry.get("hit"):
            continue
        scope = str(entry.get("scope") or "")
        stage = str(entry.get("stage") or "")
        digest = str(entry.get("input_digest") or "")
        key = str(entry.get("cache_key") or "")
        cache_entry = None
        if key:
            cache_entry = _load_json(os.path.join(
                self_dir, *CACHE_DIR_REL.split("/"), key, "entry.json"))
        problems = []
        if not key or cache_entry is None:
            problems.append("台账引用的缓存条目不存在（key=%s）" % (key or "<空>"))
        else:
            if str(cache_entry.get("key") or "") != key:
                problems.append("缓存条目键自相矛盾")
            if str(cache_entry.get("input_digest") or "") != digest:
                problems.append(
                    "台账摘要与缓存条目摘要不一致（台账 %s vs 条目 %s）"
                    % (digest[:12] or "<空>",
                       str(cache_entry.get("input_digest") or "")[:12] or "<空>"))
        if not digest:
            problems.append("台账未记录输入摘要")
        elif digest not in historical:
            problems.append("没有任何历史派发单/回执带同一 input_digest"
                            "（疑似伪造 agent 调用）")
        if problems:
            errors.append({
                "code": CACHE_REUSE_UNVERIFIED,
                "detail": "缓存复用台账 %s/%s（scope=%s）无法复核：%s"
                          % (stage, scope, scope, "；".join(problems)),
                "dispatch_id": str(entry.get("historical_dispatch_id") or ""),
            })
        drifts = []
        if cache_entry is not None:
            for rel, expect in (cache_entry.get("artifacts") or {}).items():
                disk = evidence.sha256_file(os.path.join(self_dir, rel))
                if disk is None:
                    drifts.append("%s（缺失）" % rel)
                elif str(disk) != str(expect):
                    drifts.append("%s（摘要不符）" % rel)
        if drifts:
            errors.append({
                "code": CACHE_REUSE_DRIFT,
                "detail": "缓存命中后盘面产物已改变：%s（scope=%s）"
                          % (", ".join(sorted(drifts)), scope),
                "dispatch_id": "",
            })
        reuses.append({
            "stage": stage,
            "scope": scope,
            "cache_key": key,
            "input_digest": digest,
            "historical_dispatch_id": sorted(historical.get(digest, set())),
            "source_assets_available": bool(entry.get("source_assets_available", True)),
            "reason": str(entry.get("reason") or ""),
        })
    return reuses, errors


def audit_agent_evidence(workdir, allow_unattested=None):
    """对账全部派发单与证据台账，返回 {errors, warnings, checked, dispatches}。"""
    self_dir = os.path.abspath(workdir)
    if allow_unattested is None:
        allow_unattested = _load_allow_unattested(self_dir)

    requests_dir = os.path.join(self_dir, REQUESTS_DIR_REL)
    request_paths = sorted(glob.glob(os.path.join(requests_dir, "*.json")))
    evidence_files = agent_evidence.scan_evidence(self_dir)

    # 盘面当前产物已被哪些 status=ok 证据认证：rel -> set of dispatch_ids where ev sha matches disk sha
    edges = load_shuffle_transitions(self_dir)

    def _is_classroom_plan_valid(abs_path):
        if not os.path.isfile(abs_path):
            return False
        try:
            with open(abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
            import re
            m = re.search(r"<!--\s*generated-by:\s*gen_classroom_plan\s+v\d+\s+digest=([0-9a-fA-F]+)\s*-->\n?", content)
            if not m:
                return False
            recorded = m.group(1).lower()
            body = content[:m.start()] + content[m.end():]
            actual = hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest().lower()
            return recorded == actual
        except Exception:
            return False

    def _acceptance_stripped_match(ev_sha, abs_path):
        if not os.path.isfile(abs_path):
            return False
        try:
            with open(abs_path, "r", encoding="utf-8-sig") as f:
                rec = json.load(f)
            if not isinstance(rec, dict):
                return False
            # 主控确定性盖章字段（t4_review_batch_nodes._stamp_review_provenance
            # 与 _rebind_reused_delivery 追加）：仅追加字段、不改结论与 issues；
            # 剥离后与子 Agent 落盘原文对账，否则逐轮 T4 验收记录全被判 STALE。
            stamped_keys = (
                "scope_content_digest",          # 本节内容摘要（主控盖章）
                "chapter_digest",                # 章节级上下文摘要（主控盖章）
                "review_rule_version",           # 复审规则版本（主控盖章）
                "macro_semantic_digest",         # 宏/模板语义版本（主控盖章）
                "delivery_check_ref",            # 复用旧审读时重测的本轮交付指纹
                "content_review_reused",         # 复用标记（旧审读指纹≠本轮）
                "reuse_binding",                 # scope 级复用证据
            )
            if not any(k in rec for k in stamped_keys):
                return False
            cleaned = dict(rec)
            for k in stamped_keys:
                cleaned.pop(k, None)
            raw = json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8")
            return hashlib.sha256(raw).hexdigest() == ev_sha
        except Exception:
            return False

    def _is_digest_match(ev_sha, disk_sha, rel=""):
        if not ev_sha or not disk_sha:
            return False
        if ev_sha == disk_sha:
            return True
        if shuffle_bound(ev_sha, disk_sha, edges):
            return True
        norm_rel = rel.replace("\\", "/")
        if norm_rel.startswith(".pd/acceptance/") and norm_rel.endswith(".json"):
            abs_p = os.path.join(self_dir, rel)
            if _acceptance_stripped_match(ev_sha, abs_p):
                return True
        if norm_rel == "classroom-plan.md":
            abs_p = os.path.join(self_dir, rel)
            if _is_classroom_plan_valid(abs_p):
                return True
        return False

    disk_cache = {}
    attested_current = {}
    for ev in evidence_files:
        if str(ev.get("status") or "") != agent_evidence.EVIDENCE_STATUS_OK:
            continue
        eid = str(ev.get("dispatch_id") or "")
        for rel, ev_sha in (ev.get("output_digests") or {}).items():
            if rel not in disk_cache:
                disk_cache[rel] = evidence.sha256_file(os.path.join(self_dir, rel))
            if _is_digest_match(ev_sha, disk_cache[rel], rel):
                attested_current.setdefault(rel, set()).add(eid)

    errors, warnings = [], []
    dispatches = []
    checked = {
        "dispatches": 0,
        "evidence_files": len(evidence_files),
        "attested": 0,
        "unattested": 0,
        "superseded": 0,
        "cached": 0,
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
        # 卡13（C5）：一个作者可以一次写 1–2 个相邻节，所以证据台账必须能被
        # 这一批里的**每一个** scope 认领（保留 scope 首元素做兼容标签）。
        req_scopes = request_scopes.request_scopes(request)
        artifacts = [a for a in (request.get("artifacts") or [])
                     if isinstance(a, str) and a]
        checked["dispatches"] += 1

        ev = agent_evidence.load_evidence(self_dir, dispatch_id)
        disk = _artifact_digests(self_dir, artifacts)
        missing = sorted(rel for rel, sha in disk.items() if sha is None)
        stale = sorted(rel for rel, sha in disk.items()
                       if sha is not None and ev is not None
                       and not _is_digest_match((ev.get("output_digests") or {}).get(rel), sha, rel))
        superseded = (
            all(bool(attested_current.get(rel, set()) - {dispatch_id})
                for rel in stale)
            if stale else False
        )
        if ev is None and artifacts:
            # 产物是否已被另一条证据台账认证（返修重派场景）。
            # 卡13：必须是 all，且按**声明的全部产物**遍历——缺一件盘面产物、
            # 或有一件产物没有别的台账认证，这条派发单就不算 superseded；
            # 绝不允许 any(...) 用一个成功产物给整批背书。
            superseded = all(
                bool(disk.get(rel))
                and bool(attested_current.get(rel, set()) - {dispatch_id})
                for rel in artifacts
            )

        if ev is None:
            detail = (f"派发单 {dispatch_id}（{stage}/{scope}）无证据台账"
                      f"（.pd/agent-calls/{dispatch_id}.json），产物由主 Agent "
                      f"单会话直写的嫌疑无法排除")
            if len(req_scopes) > 1:
                detail += f"；多 scope 请求覆盖 {','.join(req_scopes)}，需整批产物都有认证"
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

    # ---- INV-2 创审分离：审读 worker 不得与写作阶段 worker 同体 ----------
    # 卡14：审读角色**不能**只靠 batch_id 字符串区分——合并终审批次是
    # ``bat-t4c-*``，若仍用单一前缀判定，它带的 worker 会被算进「写作者」集合，
    # 于是真正的审读员反而被误判同体、而同体的审读员反被放过。角色真源是派发
    # 单声明的 stage（T4_COMBINED_REVIEW / T4_REVIEW_<SEC>），批次前缀仅作兼容
    # 兜底（请求文件可能已被清理的旧台账）。两遍扫描保证判定与遍历顺序无关。
    writers = set()
    reviewers = []
    for ev in evidence_files:
        worker = str(ev.get("worker") or "").strip()
        if not worker:
            continue
        if _is_reviewer_evidence(self_dir, ev):
            reviewers.append((str(ev.get("dispatch_id") or ""), worker))
        else:
            writers.add(worker)
    for dispatch_id, worker in sorted(reviewers):
        if worker in writers:
            _record("REVIEWER_IDENTITY_VIOLATION",
                    f"验收证据 {dispatch_id} 的 worker={worker} 与写作阶段"
                    f"（T1/T2）证据同体，违反 INV-2 创审分离", dispatch_id)

    # ---- INV-2 路由层：审读与写作不得用同一模型路由（N4）-----------------
    # worker id 不同只证明「会话是另起的」。宿主默认让子 Agent 继承父会话
    # 当前的 provider/model（dsh-subagent resolveChildAgentOptions），config.yaml
    # 的 model_map 又默认整块注释掉——于是「不同 worker、同一个模型」成为默认
    # 形态：审读方与写作方共享同样的偏差，独立审读在模型层根本不存在。
    # 这里读台账里的生效路由（effective_*，N3 起记录；旧台账回落到 provider/model）：
    #   - 两侧都已知且完全相同 → REVIEWER_ROUTE_NOT_SEPARATED（FAIL）
    #   - 存在未知（null）    → REVIEWER_ROUTE_UNRECORDED（WARN，提示补 model_map）
    writer_routes = {}
    for ev in evidence_files:
        if not str(ev.get("worker") or "").strip():
            continue
        if _is_reviewer_evidence(self_dir, ev):
            continue
        route = _evidence_route(ev)
        if route is not None:
            writer_routes.setdefault(route, str(ev.get("dispatch_id") or ""))
    unrecorded = 0
    for ev in evidence_files:
        if not str(ev.get("worker") or "").strip():
            continue
        if not _is_reviewer_evidence(self_dir, ev):
            continue
        dispatch_id = str(ev.get("dispatch_id") or "")
        route = _evidence_route(ev)
        if route is None:
            unrecorded += 1
            continue
        if route in writer_routes:
            _record("REVIEWER_ROUTE_NOT_SEPARATED",
                    f"验收证据 {dispatch_id} 与写作证据 {writer_routes[route]}"
                    f" 使用同一模型路由 {route[0]}/{route[1]}：worker 不同但模型相同，"
                    f"独立审读在模型层不存在（请在 config.yaml 的 "
                    f"agent_dispatch.model_map 为 T4 配置与 T2 不同的路由）",
                    dispatch_id)
    if unrecorded:
        _record("REVIEWER_ROUTE_UNRECORDED",
                f"{unrecorded} 条验收证据未记录生效路由（provider/model 均为空）："
                f"无法核对创审在模型层是否分离；升级后新派发会记录 effective_*",
                "", "warning")

    # ---- 缓存复用台账对账（任务16/C7）-----------------------------------
    # 缓存命中的 scope 没有本轮派发单，因此**不会**进入上面的遍历；这既避免
    # 误判 FAIL，也意味着「命中」本身没有任何审计。这里补上双向对账：命中必须
    # 能指向真实缓存条目 + 真实历史派发（同一 input_digest），且盘面产物仍是
    # 条目记录的那份。命中数单独记在 checked["cached"]，绝不混进 dispatches。
    cache_reuses, cache_errors = verify_cache_reuse_ledger(
        self_dir, attested_current=attested_current)
    errors.extend(cache_errors)
    checked["cached"] = len(cache_reuses)

    return {"errors": errors, "warnings": warnings, "checked": checked,
            "dispatches": dispatches, "cache_reuses": cache_reuses}


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
            "cache_reuses": res.get("cache_reuses", []),
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
