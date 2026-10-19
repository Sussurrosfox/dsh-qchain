"""agent_evidence.py — 多 Agent 证据链（q_audit.md 审计根因治理）。

审计发现：一次 qchain 运行中 T1 映射 / T2 切片 / T4 验收全部由主 Agent 单会话
直写磁盘，宿主无任何 subagent 调用记录——状态机只验文件哈希变化，不验证
「谁写了产物」。本模块把写入者身份变成可对账的磁盘证据：

    证据台账  <self_dir>/.pd/agent-calls/<dispatch_id>.json
    写入方    qchain_dispatch（index.js，宿主 subagent 工具派发子 Agent）
              run_dispatch（进程内认知 worker 路径，worker="llm-worker:<注册名>"）
    校验方    _verify_requests_from_disk / run_dispatch（磁盘重推导对账）
              scripts/check_agent_audit.py（交付门禁全量对账）
              T4 验收节点（INV-2 创审分离：验收者 ≠ 写作者）

证据 schema（schema_version=1，字段全集）：
    dispatch_id        派发单 ID（须与 .pd/agent-requests/<dispatch_id>.json 一致）
    batch_id           批次 ID
    worker             写入者身份："subagent:<平台子代理 ID>" 或 "llm-worker:<注册名>"
    provider / model   子 Agent 路由（可空串）
    started_at / finished_at / duration_ms
    artifacts_written  实际写入的产物相对路径列表
    output_digests     {rel: sha256hex}（写盘后立即计算）
    status             "ok"（其余值一律视为未完成派发）

check_request_attested 状态码（调用方按 allow_unattested 逃生阀判级）：
    ok                 证据在案且与磁盘变化产物逐项吻合
    ok_unattested      产物有变化但无证据文件（主线程直写嫌疑）
    attested_mismatch  证据在案但不吻合（摘要不符 / 状态非 ok / dispatch_id 不符），
                       一律失败无逃生阀
    missing_artifacts  产物缺失
    no_change          产物全部未变化
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

#: 证据台账目录（相对 self_dir）
AGENT_CALLS_REL = os.path.join(".pd", "agent-calls")

#: 证据 schema 版本
EVIDENCE_SCHEMA_VERSION = 1

#: 证据中代表「派发成功完成」的 status 值
EVIDENCE_STATUS_OK = "ok"

# ---------------------------------------------------------------------------
# 中断载荷派发协议（agent_nodes / section_batch_nodes / t4_review_batch_nodes 共用）
# ---------------------------------------------------------------------------

#: 派发协议标识：载荷顶层必须携带，恢复侧据此识别「是否经 qchain_dispatch 派发」
DISPATCH_PROTOCOL = "host-subagent-v1"

#: 唯一合法派发工具名
REQUIRED_DISPATCH_TOOL = "qchain_dispatch"

#: 载荷顶层 forbidden 清单（提示主 Agent 的禁止事项）
FORBIDDEN_DIRECT_WRITES = ["主 Agent 直写 artifacts", "同会话自行撰写产物内容"]

#: 恢复侧协议白名单：None 表示旧 checkpoint（不拦截，向后兼容）
ALLOWED_DISPATCH_PROTOCOLS = (None, DISPATCH_PROTOCOL)


def _iso_now() -> str:
    """ISO8601 UTC 时间戳（带时区）。"""
    return datetime.now(timezone.utc).isoformat()


def _artifact_list(request: Dict[str, Any]) -> List[str]:
    return [a for a in ((request or {}).get("artifacts") or []) if isinstance(a, str)]


def agent_calls_dir(self_dir: str) -> str:
    """证据台账目录（<self_dir>/.pd/agent-calls）。"""
    return os.path.join(self_dir or ".", AGENT_CALLS_REL)


def evidence_path(self_dir: str, dispatch_id: str) -> str:
    """证据文件路径；dispatch_id 含路径分隔符或是 . / .. 时返回 ""（防穿越）。"""
    did = str(dispatch_id or "").strip()
    if not did:
        return ""
    if os.sep in did or (os.altsep and os.altsep in did) or did in (".", ".."):
        return ""
    return os.path.join(agent_calls_dir(self_dir), did + ".json")


def load_evidence(self_dir: str, dispatch_id: str) -> Optional[Dict[str, Any]]:
    """读取单条证据。

    JSON 解析失败 / 文件不存在 / 根非对象 / 缺 dispatch_id /
    内容 dispatch_id 与请求不一致 → None（张冠李戴防伪造）。
    """
    path = evidence_path(self_dir, dispatch_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("dispatch_id") or "") != str(dispatch_id or "").strip():
        return None
    return data


def evidence_matches(evidence: Any, request: Dict[str, Any],
                     disk_digests: Dict[str, Optional[str]]) -> bool:
    """证据与「磁盘现状 + 派发单」三方对账。

    条件：dispatch_id 一致 + status=="ok" + output_digests 在全部「相对 before
    发生变化的产物」上逐项等于磁盘 sha256。无变化产物时返回 False（no_change
    由调用方先行判掉，此处无可对账对象）。
    """
    if not isinstance(evidence, dict) or not isinstance(request, dict):
        return False
    if str(evidence.get("dispatch_id") or "") != str(request.get("dispatch_id") or ""):
        return False
    if str(evidence.get("status") or "") != EVIDENCE_STATUS_OK:
        return False
    digests = evidence.get("output_digests")
    if not isinstance(digests, dict):
        return False
    before = dict(request.get("before") or {})
    changed = [
        rel for rel in _artifact_list(request)
        if disk_digests.get(rel) is not None and disk_digests.get(rel) != before.get(rel)
    ]
    if not changed:
        return False
    for rel in changed:
        if digests.get(rel) != disk_digests.get(rel):
            return False
    return True


def check_request_attested(self_dir: str, request: Dict[str, Any],
                           disk_digests: Dict[str, Optional[str]],
                           allow_unattested: bool = False
                           ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """对账单条派发请求的「产物-证据」一致性，返回 (status, evidence|None)。

    allow_unattested 为调用方放行口径透传：无证据文件时无论取值都稳定返回
    ok_unattested（失败码可断言）；仅在「证据在案但 worker 字段缺失」这种
    半伪造场景下区分放行（ok_unattested）与失败（attested_mismatch）。
    """
    request = request or {}
    artifacts = _artifact_list(request)
    if not artifacts:
        return "missing_artifacts", None
    missing = [rel for rel in artifacts if not disk_digests.get(rel)]
    if missing:
        return "missing_artifacts", None
    before = dict(request.get("before") or {})
    changed = [
        rel for rel in artifacts
        if disk_digests.get(rel) is not None and disk_digests.get(rel) != before.get(rel)
    ]
    if not changed:
        return "no_change", None
    evidence = load_evidence(self_dir, request.get("dispatch_id"))
    if evidence is None:
        return "ok_unattested", None
    if evidence_matches(evidence, request, disk_digests):
        if not str(evidence.get("worker") or "").strip():
            # 证据在案但无写入者身份：半伪造场景，按 allow_unattested 分流
            return ("ok_unattested" if allow_unattested else "attested_mismatch"), evidence
        return "ok", evidence
    return "attested_mismatch", evidence


def write_evidence(self_dir: str, *, dispatch_id: str, batch_id: Any = None,
                   worker: str, provider: str = "", model: str = "",
                   started_at: Optional[str] = None, finished_at: Optional[str] = None,
                   duration_ms: Optional[int] = None,
                   artifacts_written: Optional[List[str]] = None,
                   output_digests: Optional[Dict[str, str]] = None,
                   status: str = EVIDENCE_STATUS_OK) -> str:
    """原子写入证据台账（tmp + os.replace）。返回证据文件路径，失败返回 ""。"""
    path = evidence_path(self_dir, dispatch_id)
    if not path:
        return ""
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "dispatch_id": str(dispatch_id),
        "batch_id": batch_id,
        "worker": str(worker or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "started_at": started_at or _iso_now(),
        "finished_at": finished_at or _iso_now(),
        "duration_ms": int(duration_ms or 0),
        "artifacts_written": [str(a) for a in (artifacts_written or [])],
        "output_digests": {
            str(k): str(v) for k, v in (output_digests or {}).items() if v
        },
        "status": str(status or EVIDENCE_STATUS_OK),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp-%s" % (path, uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        return ""
    return path


def scan_evidence(self_dir: str) -> List[Dict[str, Any]]:
    """全量扫描证据台账（跳过坏 JSON / dispatch_id 与文件名不符的条目）。"""
    directory = agent_calls_dir(self_dir)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        did = str(data.get("dispatch_id") or "")
        if not did or did + ".json" != name:
            continue
        out.append(data)
    return out


def writing_workers(self_dir: str) -> set:
    """全部证据台账中出现的写入者身份集合（INV-2 创审分离用）。"""
    workers = set()
    for ev in scan_evidence(self_dir):
        worker = str(ev.get("worker") or "").strip()
        if worker:
            workers.add(worker)
    return workers


def load_dispatch_policy(self_dir: str) -> Dict[str, Any]:
    """派发策略（allow_unattested / model_map）。

    惰性委托 section_batch_nodes._load_dispatch_config，避免模块级循环导入
    （section_batch_nodes 在模块级导入本模块的上游 agent_nodes）。任何失败都
    回落保守默认：不放行、空路由表。
    """
    try:
        from .section_batch_nodes import _load_dispatch_config

        cfg = _load_dispatch_config(self_dir or ".")
    except Exception:
        cfg = {}
    allow = cfg.get("allow_unattested")
    model_map = cfg.get("model_map")
    return {
        "allow_unattested": bool(allow) if isinstance(allow, bool) else False,
        "model_map": dict(model_map) if isinstance(model_map, dict) else {},
    }


def dispatch_protocol_block() -> Dict[str, Any]:
    """中断载荷顶层的派发协议三件套（收到 AGENT_DISPATCH 必须调 qchain_dispatch）。"""
    return {
        "dispatch_protocol": DISPATCH_PROTOCOL,
        "required_tool": REQUIRED_DISPATCH_TOOL,
        "forbidden": list(FORBIDDEN_DIRECT_WRITES),
    }


def dispatch_protocol_violation(response: Any) -> Optional[str]:
    """恢复侧防伪造校验。

    中断响应声明的 dispatch_protocol 必须为 None（旧 checkpoint，不拦截）或
    host-subagent-v1；否则返回错误详情（调用方丢弃 files 并 HALT）。
    """
    if not isinstance(response, dict):
        return None
    protocol = response.get("dispatch_protocol")
    if protocol in ALLOWED_DISPATCH_PROTOCOLS:
        return None
    return ("派发协议校验失败 [BAD_DISPATCH_PROTOCOL]: 中断响应声明 %r，"
            "应为 %r（须经 qchain_dispatch 派发宿主子 Agent）"
            % (protocol, DISPATCH_PROTOCOL))
