"""repair_budget.py — 有限返修与无进展熔断的公共计数器（修复卡 06，DATA-CONTRACTS C3）。

区分三个互不相干的计数：

* 模型调用 attempt（运输重试，由 dispatcher/宿主管，``request["attempt"]``）；
* 内容修复轮 repair（本模块）：*每次安排一次内容返修就 +1*，且 **dispatch 返回 ok
  不清零**——只有相关门禁真正 PASS 才解除“本次未解决”标记，全局累计永不重置；
* 覆盖重规划 replan（``replan_count``，各 REPLAN 路径自管，本模块不碰）。

全部计数、前次 issue 指纹、前次产物摘要都放在 LangGraph checkpoint 状态里
（纯 dict/list/str/int，JSON 可序列化），中断恢复/进程重启后原样保留。

判定顺序（先无进展、后额度）::

    fingerprint = SHA256(sorted(code, scope, qid, normalized_evidence))
    if input_digest == last_digest[scope] and fingerprint == last_fp[scope]:
        HALT(no_progress)
    elif scope_attempts >= per_scope_limit or total_attempts >= total_limit:
        HALT(repair_budget_exhausted)
    else:
        计数 + 指纹 + 输入摘要随派发单**同一次**状态更新持久化（先落盘后派发）

上限：legacy（缺省/旧检查点）每 scope 3 次、无全局上限（保留旧约定）；
``execution_profile == "faithful_fast"``（任务10/15 才接入）每 scope 2 次、全 run 6 次。
也可经 ``state["repair_per_scope_limit"]`` / ``state["repair_total_limit"]`` 显式覆盖。
旧 state 缺任何新键都不会抛异常（全部 ``.get`` 默认）。
"""

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

NO_PROGRESS = "no_progress"
BUDGET_EXHAUSTED = "repair_budget_exhausted"

FAST_PROFILE = "faithful_fast"
LEGACY_PER_SCOPE_LIMIT = 3
LEGACY_TOTAL_LIMIT: Optional[int] = None  # legacy 无全局上限（沿用旧约定）
FAST_PER_SCOPE_LIMIT = 2
FAST_TOTAL_LIMIT = 6

#: 归一化 evidence 时丢弃的非语义字段名（时间/耗时/运行标识）。公式、qid 不在其内。
_VOLATILE_KEYS = frozenset({
    "timestamp", "time", "ts", "created_at", "updated_at", "started_at",
    "finished_at", "generated_at", "at", "elapsed", "elapsed_ms", "duration",
    "run_id", "dispatch_id", "attempt_id", "batch_id", "uuid",
})

_TS_PATTERNS = (
    # ISO 8601 / 带空格的日期时间（可含毫秒与时区）
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    # 派发单 / 批次 id（agent_nodes.build_request 的 dsp-…-<10hex>、bat-…-<8hex>）
    (re.compile(r"\bdsp-[a-z0-9_-]*?-?[0-9a-f]{10}\b"), "<dsp>"),
    (re.compile(r"\bbat-[a-z0-9_-]*?-?[0-9a-f]{8}\b"), "<bat>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # 13 位毫秒 epoch
    (re.compile(r"\b1[5-9]\d{11}\b"), "<ts>"),
    # “第 N 次”的轮次提示（随计数变化，非语义）
    (re.compile(r"第\s*\d+\s*次"), "第N次"),
)


def normalize_evidence(text: Any) -> str:
    """去掉时间戳 / 派发 id / 轮次等非语义噪声，保留公式与 qid。"""
    s = text if isinstance(text, str) else json.dumps(
        _strip_volatile(text), ensure_ascii=False, sort_keys=True)
    for pat, repl in _TS_PATTERNS:
        s = pat.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items()
                if str(k).lower() not in _VOLATILE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile(v) for v in obj]
    return obj


def issue_fingerprint(scope: str, issues: Optional[List[Dict[str, Any]]] = None,
                      reason: str = "") -> str:
    """SHA256(sorted([code, scope, qid, normalized_evidence]))。

    ``issues`` 为 C2 统一 issue 列表（code/scope/qid/evidence/message）；缺省时用
    ``reason`` 整串作单条 evidence（旧回路 state 只有 repair_reason）。
    """
    items: List[List[str]] = []
    for it in (issues or []):
        if not isinstance(it, dict):
            continue
        ev = it.get("evidence")
        if ev in (None, "", {}, []):
            ev = it.get("message", "")
        items.append([
            str(it.get("code") or ""),
            str(it.get("scope") or scope or ""),
            str(it.get("qid") or ""),
            normalize_evidence(ev),
        ])
    if not items:
        items.append(["", str(scope or ""), "", normalize_evidence(reason or "")])
    items.sort()
    payload = json.dumps(items, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_limits(state: Dict[str, Any],
                   config: Optional[Dict[str, Any]] = None
                   ) -> Tuple[int, Optional[int], str]:
    """(per_scope_limit, total_limit|None, profile)。

    F4（冻结优先）：Phase 0 把 ``repair_max_per_scope`` / ``repair_max_total``
    冻结进 ``state["execution_config"]``，但旧实现只读 profile 默认值与顶层
    ``repair_per_scope_limit``/``repair_total_limit``——冻结的 1/1 被读成 2/6，
    预算上限形同虚设。优先级：
      冻结 execution_config > 显式 state 覆盖 > config.yaml > profile 默认。

    A5：新增 ``config`` 一层。此前想放宽上限只有一条路——拿 msgpack 二进制改
    checkpoint 里的 ``repair_per_scope_limit``（写 ASCII 会损坏状态、填 str 会崩
    ``apply_writes``），等于把运维动作降级成逆向工程。现在 ``self/config.yaml``
    的扁平键 ``repair_per_scope_limit`` / ``repair_total_limit`` 就是正式通道，
    返修节点每轮现读现用（运行中改一行 YAML 下一轮即生效，不必动 checkpoint）。
    """
    profile = "fast" if state.get("execution_profile") == FAST_PROFILE else "legacy"
    per_scope = FAST_PER_SCOPE_LIMIT if profile == "fast" else LEGACY_PER_SCOPE_LIMIT
    total: Optional[int] = FAST_TOTAL_LIMIT if profile == "fast" else LEGACY_TOTAL_LIMIT
    frozen = state.get("execution_config")
    frozen = frozen if isinstance(frozen, dict) else {}
    cfg = config if isinstance(config, dict) else {}

    def _limit(key: str) -> Optional[int]:
        value = frozen.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _as_int(value: Any) -> Optional[int]:
        """config.yaml 的取值可能是 int 或字符串；非法/负数一律视为未设置。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            n = int(value.strip())
            return n if n >= 0 else None
        return None

    frozen_scope = _limit("repair_max_per_scope")
    frozen_total = _limit("repair_max_total")
    if frozen_scope is not None:
        per_scope = frozen_scope
    if frozen_total is not None:
        total = frozen_total
    # 顶层显式覆盖只在没有冻结值时才生效（旧检查点 / 无执行契约的工程）
    ov_scope = state.get("repair_per_scope_limit")
    if frozen_scope is None and isinstance(ov_scope, int) \
            and not isinstance(ov_scope, bool) and ov_scope >= 0:
        per_scope = ov_scope
    ov_total = state.get("repair_total_limit")
    if frozen_total is None and isinstance(ov_total, int) \
            and not isinstance(ov_total, bool) and ov_total >= 0:
        total = ov_total
    # A5：config.yaml 通道（弱于 state 显式覆盖、强于 profile 默认）
    cfg_scope = _as_int(cfg.get("repair_per_scope_limit"))
    if frozen_scope is None and not isinstance(ov_scope, int) and cfg_scope is not None:
        per_scope = cfg_scope
    cfg_total = _as_int(cfg.get("repair_total_limit"))
    if frozen_total is None and not isinstance(ov_total, int) and cfg_total is not None:
        total = cfg_total
    # 运维覆盖（ops_override）：冻结执行契约之上的唯一显式放宽通道（见
    # ``load_ops_override``）。优先级：ops_override > frozen > state > config > profile。
    ops = load_ops_override(state.get("self_dir") or state.get("project_root"))
    if "repair_per_scope_limit" in ops:
        per_scope = ops["repair_per_scope_limit"]
    if "repair_total_limit" in ops:
        total = ops["repair_total_limit"]  # None = 显式解除全局上限（合法取值）
    return per_scope, total, profile


def limits_source(state: Dict[str, Any],
                  config: Optional[Dict[str, Any]] = None) -> str:
    """返修上限的生效来源：ops_override / frozen / state / config / profile（排障用，进 HALT 证据）。"""
    ops = load_ops_override(state.get("self_dir") or state.get("project_root"))
    if ops:
        return "ops_override"
    frozen = state.get("execution_config")
    frozen = frozen if isinstance(frozen, dict) else {}
    for key in ("repair_max_per_scope", "repair_max_total"):
        value = frozen.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return "frozen"
    for key in ("repair_per_scope_limit", "repair_total_limit"):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return "state"
    cfg = config if isinstance(config, dict) else {}
    for key in ("repair_per_scope_limit", "repair_total_limit"):
        value = cfg.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return "config"
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return "config"
    return "profile"


def load_repair_overrides(self_dir: Optional[str]) -> Dict[str, Any]:
    """A5：从 ``self/config.yaml`` 读返修上限的运维覆盖（正式通道）。

    认两种写法（扁平优先，其次 ``agent_dispatch`` 子块）::

        repair_per_scope_limit: 12
        repair_total_limit: 40
        # 或
        agent_dispatch:
          repair_per_scope_limit: 12

    读不到 / 文件不存在 / YAML 损坏一律返回 ``{}``（按 profile 默认），绝不抛错
    ——返修回路不能因为一行配置写错就整体崩掉。非法取值（负数/非数字）同样视为
    未设置，由 ``resolve_limits`` 统一解释。
    """
    out: Dict[str, Any] = {}
    if not self_dir:
        return out
    cfg_path = os.path.join(self_dir, "config.yaml")
    if not os.path.isfile(cfg_path):
        return out
    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return out
    data: Any = None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except Exception:
        data = None
    if not isinstance(data, dict):
        # 无 PyYAML 或 YAML 损坏：退回扁平 `key: value` 行解析
        data = {}
        for line in text.splitlines():
            if line[:1] in (" ", "\t", "#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()
    for key in ("repair_per_scope_limit", "repair_total_limit"):
        raw = data.get(key)
        if raw is None:
            nested = data.get("agent_dispatch")
            if isinstance(nested, dict):
                raw = nested.get(key)
        if raw is not None:
            out[key] = raw
    return out


def load_ops_override(self_dir: Optional[str]) -> Dict[str, Any]:
    """工作区级运维覆盖：``self/.pd/repair-ops-override.json``。

    Phase 0 冻结的返修额度是**会话级执行契约**，``config.yaml`` 无法覆盖已冻结
    值（F4）。当冻结额度在运行中被正当耗尽、但仍需安排已规划的内容返修
    （例：T2 阶段的语义返修用满预算后，T3 本地门禁又发现对话悬置原则违规），
    运维人员可在本文件显式放宽——这是 frozen 之上的唯一例外通道，**由人落盘、
    当场生效、逐工作区隔离、不进 checkpoint**::

        {
          "repair_per_scope_limit": 12,
          "repair_total_limit": null,      // null = 显式解除全局上限
          "approved_by": "<真人>",
          "reason": "...",
          "applied_at": "..."
        }

    ``approved_by`` / ``reason`` 仅作审计记录、不参与判定；非法取值 / 文件缺失 /
    JSON 损坏一律视为未设置（返回 ``{}``），绝不抛错。
    """
    out: Dict[str, Any] = {}
    if not self_dir:
        return out
    path = os.path.join(self_dir, ".pd", "repair-ops-override.json")
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for key in ("repair_per_scope_limit", "repair_total_limit"):
        raw = data.get(key)
        if key == "repair_total_limit" and raw is None and key in data:
            out[key] = None  # 显式 null = 解除全局上限
            continue
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw >= 0:
            out[key] = raw
        elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            n = int(raw.strip())
            if n >= 0:
                out[key] = n
    return out


def plan_repair(state: Dict[str, Any], scope: str, reason: str, input_digest: Optional[str],
                *, issues: Optional[List[Dict[str, Any]]] = None,
                artifact: Optional[str] = None,
                config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """决定“安排下一次内容返修”还是 HALT；纯函数，不读写磁盘。

    返回::

        {"action": "SCHEDULE", "attempt_no": n, "fingerprint": fp, "patch": {...}}
        {"action": "HALT", "halt_reason": NO_PROGRESS|BUDGET_EXHAUSTED,
         "detail": str, "halt": {...可审计证据...}, "fingerprint": fp}

    ``patch`` 是须随派发单同一次 checkpoint 写入的全部持久字段（完整新 dict，
    不依赖 reducer）。HALT 不增加任何计数。
    """
    per_scope, total_limit, profile = resolve_limits(state, config)
    by_scope = dict(state.get("repair_attempts_by_scope") or {})
    total = int(state.get("repair_attempts_total") or 0)
    last_fp = dict(state.get("repair_last_fingerprint_by_scope") or {})
    last_digest = dict(state.get("repair_last_digest_by_scope") or {})
    log = list(state.get("repair_attempt_log") or [])
    scope_attempts = int(by_scope.get(scope) or 0)
    fp = issue_fingerprint(scope, issues, reason)

    halt_ctx = {
        "scope": scope,
        "profile": profile,
        "limits_source": limits_source(state, config),
        "scope_attempts": scope_attempts,
        "total_attempts": total,
        "per_scope_limit": per_scope,
        "total_limit": total_limit,
        "unresolved_reason": reason,
        "fingerprint": fp,
        "last_artifact": {"path": artifact, "digest": input_digest},
        # attempt 来源：此前每次返修派发的 dispatch_id/指纹/输入摘要
        "attempts": [e for e in log if e.get("scope") == scope] or list(log[-6:]),
    }

    if scope in last_fp and scope in last_digest \
            and last_digest[scope] == input_digest and last_fp[scope] == fp:
        return {
            "action": "HALT", "halt_reason": NO_PROGRESS, "fingerprint": fp,
            "detail": (f"返修无进展: {scope} 上次返修后产物摘要与问题指纹均未变化 "
                       f"（已修 {scope_attempts} 次）"),
            "halt": {**halt_ctx, "halt_reason": NO_PROGRESS},
        }
    if scope_attempts >= per_scope or (total_limit is not None and total >= total_limit):
        which = (f"scope {scope} 已达 {scope_attempts}/{per_scope}"
                 if scope_attempts >= per_scope
                 else f"全局累计已达 {total}/{total_limit}")
        return {
            "action": "HALT", "halt_reason": BUDGET_EXHAUSTED, "fingerprint": fp,
            "detail": f"返修预算耗尽: {which}",
            "halt": {**halt_ctx, "halt_reason": BUDGET_EXHAUSTED},
        }

    by_scope[scope] = scope_attempts + 1
    last_fp[scope] = fp
    last_digest[scope] = input_digest
    return {
        "action": "SCHEDULE", "attempt_no": by_scope[scope], "fingerprint": fp,
        "patch": {
            "repair_attempts_by_scope": by_scope,
            "repair_attempts_total": total + 1,
            "repair_last_fingerprint_by_scope": last_fp,
            "repair_last_digest_by_scope": last_digest,
            "repair_attempt_log": log,  # 调用方追加本次 dispatch_id 后再写回
        },
    }


def attempt_log_entry(scope: str, attempt_no: int, total_no: int, dispatch_id: str,
                      fingerprint: str, input_digest: Optional[str], reason: str
                      ) -> Dict[str, Any]:
    return {
        "scope": scope, "attempt": attempt_no, "total": total_no,
        "dispatch_id": dispatch_id, "fingerprint": fingerprint,
        "input_digest": input_digest, "reason": normalize_evidence(reason)[:300],
    }


def resolved_patch(state: Dict[str, Any], scopes: Optional[List[str]] = None
                   ) -> Dict[str, Any]:
    """相关门禁真正 PASS：只解除“本次未解决”标记（前次指纹/摘要）。

    **计数（by_scope / total / log）永不在此重置。** ``scopes=None`` 表示全部
    scope（如 t3_checks 全 PASS）。没有任何未解决标记时返回 {}（不制造空写）。
    """
    last_fp = dict(state.get("repair_last_fingerprint_by_scope") or {})
    last_digest = dict(state.get("repair_last_digest_by_scope") or {})
    targets = list(last_fp) if scopes is None else [s for s in scopes if s in last_fp]
    if not targets and not (scopes is None and last_digest):
        return {}
    for s in targets:
        last_fp.pop(s, None)
        last_digest.pop(s, None)
    if scopes is None:
        last_digest = {}
    return {
        "repair_last_fingerprint_by_scope": last_fp,
        "repair_last_digest_by_scope": last_digest,
    }
