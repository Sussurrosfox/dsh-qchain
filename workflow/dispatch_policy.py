#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch_policy.py — 派发并发配置的**唯一**解析器（任务19 / DATA-CONTRACTS C12）。

## 为什么要有这个模块

审计（audit_agent.md §12.1 / q_audit.md / 复盘报告 §4.3）确认了三处混淆：

1. ``_disk_plan`` 把 ``max_concurrency`` 写成 ``max(pool_limit, len(batch))``，
   即「一批 8 个请求、并发上限 4」被记成并发 8；而 check_dispatch 的 R7 又
   要求 ``len(requests) <= max_concurrency``——请求数被当成并发上限使用，
   计划因此永远无法诚实声明「队列 8 / 并发 4」。
2. ``agent_dispatch`` 的嵌套块 ``max_concurrency`` 与扁平键
   ``agent_max_concurrency`` 谁优先没有单一真相（旧实现扁平键覆盖嵌套块）。
3. index.js 读配置失败时 ``catch (_) {}`` 静默回落并发 2，真实原因被吞。

本模块把「档位 / 配置并发 / 宿主或 worker 能力 / 真实生效并发」拆成独立字段，
并由 Python（本模块）与 JS（``scripts/dispatch_policy.mjs``）实现**同一套规则**：
两边的单元测试共享 ``scripts/fixtures/dispatch-policy-cases.json``，逐例比对
结果必须完全一致（PARCFG-06）。

## 规则（v2 契约，``dispatch_contract_version: 2``）

优先级（从高到低）：

1. 本次工具调用显式正整数覆盖 ``tool_override``（1..16）；
2. 配置嵌套块 ``agent_dispatch.max_concurrency``（正整数）；
3. 配置扁平键 ``agent_max_concurrency``（正整数）；
4. 档位表 ``PROFILE_CONCURRENCY[profile]``。

* ``0`` 一律表示「自动」（按档位表），不产生任何正向覆盖效果；
* 负数 / 小数 / NaN / 布尔 / ``> 16`` 显式拒绝（不是静默夹取）；
* 嵌套与扁平**同时为正且不相等** → ``CONFIG_CONCURRENCY_CONFLICT``，
  点名两个键，绝不静默挑一个。

档位（profile）解析顺序：显式 ``agent_dispatch.profile``（映射块）→ 显式
``agent_dispatch`` 标量 → ``run_scope`` 缺省档位 → ``execution_profile``
缺省（仅 v2：``faithful_fast`` 未显式选择时用 ``fast``）→ ``safe``。
模板**不得预填** ``profile``，否则「模板默认」与「用户显式选择」无法区分。

## 旧契约（``dispatch_contract_version`` 缺省 = 1）

旧工程/旧检查点没有这个契约，必须保持旧行为与旧优先级（扁平键优先），
只额外记录 ``concurrency_source`` 与冲突诊断，**不改配置、不改检查点**。

## 执行器与真实并发

* ``executor_kind`` ∈ ``python_registry``（进程内 WorkerPool 已注册 worker）/
  ``host_subagent``（宿主子 Agent 通道，无 Python worker）；
* 已知 worker 能力 → ``effective = max(1, min(limit, worker_cap))``；
* **能力未知 → ``worker_max_concurrency = null``**（绝不用 Python 延迟池的
  保守 1 冒充宿主并发），仍按配置上限执行，并标记 ``capability_verified``
  = false。
"""

from typing import Any, Dict, List, Optional, Tuple

#: 新契约版本（独立于 C5 请求 schema_version，二者互不代用）
DISPATCH_CONTRACT_VERSION = 2
#: 旧契约版本（配置/检查点缺省）
LEGACY_DISPATCH_CONTRACT_VERSION = 1
SUPPORTED_DISPATCH_CONTRACT_VERSIONS = (
    LEGACY_DISPATCH_CONTRACT_VERSION, DISPATCH_CONTRACT_VERSION)

#: 派发档位 → 并发上限（audit_agent.md §12.1）
DISPATCH_PROFILES = ("interactive", "safe", "fast", "budget")
PROFILE_CONCURRENCY: Dict[str, int] = {
    "interactive": 1, "safe": 1, "fast": 4, "budget": 2}
#: 未显式配置且 run_scope 未知时的回落：并行档 fast(4)。
#: 2026-11-15 改动：原为 "safe"(1)（全串行）。JS 侧派发工具读不到 state 里的
#: run_scope（只在 self/config.yaml 里找），导致"未配置工作区"实际永远串行，
#: 与"同批独立小节一次 batch_id 进同一并发池"的设计意图相反。要串行必须显式：
#: agent_dispatch: safe（或 profile: safe），run_scope=focused/edit 仍 interactive(1)。
DEFAULT_PROFILE = "fast"
#: run_scope → 缺省档位（full=完整生成 → fast；focused/edit=交互微调）
RUN_SCOPE_DEFAULT_PROFILE: Dict[str, str] = {
    "full": "fast", "focused": "interactive", "edit": "interactive"}
#: 并发上限的硬边界（宿主 subagent 通道的客户端上限）
MAX_CONCURRENCY_LIMIT = 16

#: 执行器种类（C12）
EXECUTOR_PYTHON = "python_registry"
EXECUTOR_HOST = "host_subagent"
EXECUTOR_KINDS = (EXECUTOR_PYTHON, EXECUTOR_HOST)

#: 配置里与本契约相关的扁平键（迁移诊断用）
DISPATCH_CONFIG_KEYS = (
    "dispatch_contract_version", "agent_dispatch", "agent_max_concurrency",
    "run_scope", "execution_profile")

#: 内容策略档位名（与 workflow/execution_policy.py 的 FAST_PROFILE 同值）
FAST_EXECUTION_PROFILE = "faithful_fast"

#: 错误码
INVALID_DISPATCH_PROFILE = "INVALID_DISPATCH_PROFILE"
CONFIG_CONCURRENCY_INVALID = "CONFIG_CONCURRENCY_INVALID"
CONFIG_CONCURRENCY_OUT_OF_RANGE = "CONFIG_CONCURRENCY_OUT_OF_RANGE"
CONFIG_CONCURRENCY_CONFLICT = "CONFIG_CONCURRENCY_CONFLICT"
TOOL_CONCURRENCY_INVALID = "TOOL_CONCURRENCY_INVALID"
TOOL_CONCURRENCY_OUT_OF_RANGE = "TOOL_CONCURRENCY_OUT_OF_RANGE"
DISPATCH_CONTRACT_VERSION_UNSUPPORTED = "DISPATCH_CONTRACT_VERSION_UNSUPPORTED"
#: 诊断（不失败）码
CONFIG_CONCURRENCY_SHADOWED = "CONFIG_CONCURRENCY_SHADOWED"


class DispatchPolicyError(ValueError):
    """派发并发配置拒绝：``code`` 为稳定错误码，``detail`` 为人可读原因。"""

    def __init__(self, code: str, detail: str):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "detail": self.detail}


# ---------------------------------------------------------------------------
# 标量归一化（bool 必须显式排除：bool 是 int 子类，True 不是并发上限）
# ---------------------------------------------------------------------------

def _int_like(value: Any) -> Tuple[Optional[int], str]:
    """把配置标量归一为整数；返回 ``(值, 拒绝原因)``，原因为空串表示成功。"""
    if isinstance(value, bool):
        return None, "布尔值"
    if isinstance(value, int):
        return int(value), ""
    if isinstance(value, float):
        if value != value:  # NaN
            return None, "NaN"
        if float(value).is_integer():
            return int(value), ""
        return None, "小数"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, ""
        try:
            return int(text), ""
        except ValueError:
            pass
        try:
            as_float = float(text)
        except ValueError:
            return None, "非数值"
        if as_float != as_float:
            return None, "NaN"
        if float(as_float).is_integer():
            return int(as_float), ""
        return None, "小数"
    if value is None:
        return None, ""
    return None, "类型 %s" % type(value).__name__


def _coerce_config_concurrency(value: Any, *, key: str, strict: bool,
                               diagnostics: List[Dict[str, str]]) -> int:
    """配置侧并发值归一：``0``/缺省 = 自动；v2 严格拒绝非法值。"""
    number, reason = _int_like(value)
    if reason:
        detail = ("%s=%r 非法（%s）：并发必须是 1..%d 的整数，0 表示按档位自动"
                  % (key, value, reason, MAX_CONCURRENCY_LIMIT))
        if strict:
            raise DispatchPolicyError(CONFIG_CONCURRENCY_INVALID, detail)
        diagnostics.append({"code": CONFIG_CONCURRENCY_INVALID, "detail": detail})
        return 0
    if number is None:
        return 0
    if number < 0:
        detail = ("%s=%r 非法：不允许负数（0 表示按档位自动）" % (key, value))
        if strict:
            raise DispatchPolicyError(CONFIG_CONCURRENCY_INVALID, detail)
        diagnostics.append({"code": CONFIG_CONCURRENCY_INVALID, "detail": detail})
        return 0
    if number > MAX_CONCURRENCY_LIMIT:
        detail = ("%s=%r 超出上限 %d（宿主/worker 并发不得虚报）"
                  % (key, value, MAX_CONCURRENCY_LIMIT))
        if strict:
            raise DispatchPolicyError(CONFIG_CONCURRENCY_OUT_OF_RANGE, detail)
        # 旧契约不夹取配置原值（保持旧行为），只留诊断
        diagnostics.append(
            {"code": CONFIG_CONCURRENCY_OUT_OF_RANGE, "detail": detail})
    return number


def _coerce_tool_override(value: Any) -> int:
    """本次工具调用覆盖：``0``/缺省 = 自动；非法一律显式拒绝（不夹取）。"""
    number, reason = _int_like(value)
    if reason:
        raise DispatchPolicyError(
            TOOL_CONCURRENCY_INVALID,
            "max_concurrency=%r 非法（%s）：必须是 1..%d 的整数，0 表示按配置自动"
            % (value, reason, MAX_CONCURRENCY_LIMIT))
    if number is None:
        return 0
    if number < 0:
        raise DispatchPolicyError(
            TOOL_CONCURRENCY_INVALID,
            "max_concurrency=%r 非法：不允许负数（0 表示按配置自动）" % (value,))
    if number > MAX_CONCURRENCY_LIMIT:
        raise DispatchPolicyError(
            TOOL_CONCURRENCY_OUT_OF_RANGE,
            "max_concurrency=%r 超出上限 %d（宿主 subagent 通道的客户端上限）"
            % (value, MAX_CONCURRENCY_LIMIT))
    return number


def resolve_contract_version(value: Any) -> int:
    """``dispatch_contract_version`` 归一；未知版本一律硬拒绝（不猜）。"""
    number, reason = _int_like(value)
    if reason or number is None:
        if isinstance(value, str) and not value.strip():
            return LEGACY_DISPATCH_CONTRACT_VERSION
        if value is None:
            return LEGACY_DISPATCH_CONTRACT_VERSION
        raise DispatchPolicyError(
            DISPATCH_CONTRACT_VERSION_UNSUPPORTED,
            "dispatch_contract_version=%r 不受支持（%s）：已知版本 %s；"
            "拒绝按猜测的版本解析派发并发"
            % (value, reason or "非整数",
               " / ".join(str(v) for v in SUPPORTED_DISPATCH_CONTRACT_VERSIONS)))
    if number not in SUPPORTED_DISPATCH_CONTRACT_VERSIONS:
        raise DispatchPolicyError(
            DISPATCH_CONTRACT_VERSION_UNSUPPORTED,
            "dispatch_contract_version=%r 不受支持：已知版本 %s；"
            "升级请先读 MIGRATION 说明，不允许静默按最新版本解释旧计划"
            % (value, " / ".join(str(v) for v in SUPPORTED_DISPATCH_CONTRACT_VERSIONS)))
    return number


def _execution_profile_of(config: Dict[str, Any],
                          explicit: Optional[str] = None) -> str:
    """归一执行档位（复用卡10 execution_policy.resolve_profile）。"""
    value = explicit if explicit is not None else config.get("execution_profile")
    try:  # 惰性 import：本模块要能在 workflow 包被部分导入时也可用
        from . import execution_policy  # noqa: E402

        return execution_policy.resolve_profile(value)
    except Exception:
        return str(value or "").strip().lower() or "legacy"


# ---------------------------------------------------------------------------
# 配置读取（JS 侧也走这里，保证「同一份输入」）
# ---------------------------------------------------------------------------

def _parse_flat_yaml(text: str) -> Dict[str, Any]:
    """容错扁平解析：仅顶层 ``key: value``，缩进行（嵌套块）跳过。"""
    parsed: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if raw[:1] in (" ", "\t"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.split("#", 1)[0].strip().strip("\"'")
        key = key.strip()
        if not key:
            continue
        if value == "":
            parsed[key] = None
            continue
        lowered = value.lower()
        if lowered in ("true", "false"):
            parsed[key] = lowered == "true"
            continue
        try:
            parsed[key] = int(value)
            continue
        except ValueError:
            pass
        parsed[key] = value
    return parsed


def load_raw_config(self_dir: str) -> Dict[str, Any]:
    """读取 ``<self_dir>/config.yaml`` 原始配置（PyYAML 优先，扁平兜底）。

    任何畸形内容都被容忍（返回空 dict），绝不抛异常——解析失败的真实原因由
    调用方按「配置缺失/不可读」处理，不允许静默回落某个并发数字。
    """
    import os

    path = os.path.join(self_dir or ".", "config.yaml")
    text = ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return _parse_flat_yaml(text)


def read_dispatch_inputs(self_dir: str, run_scope: str = "") -> Dict[str, Any]:
    """给 JS 派发器与 Python 节点共用的**解析输入**（不含任何解析结果）。"""
    config = load_raw_config(self_dir)
    dispatch = config.get("agent_dispatch")
    model_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(dispatch, dict):
        raw_map = dispatch.get("model_map")
        if isinstance(raw_map, dict):
            for stage, route in raw_map.items():
                if isinstance(route, dict) and route:
                    model_map[str(stage).strip().upper()] = dict(route)
    return {
        "config": config,
        "run_scope": str(run_scope or config.get("run_scope") or "").strip(),
        "execution_profile": _execution_profile_of(config),
        "model_map": model_map,
        "frozen": frozen_dispatch_block(self_dir),
    }


def frozen_dispatch_block(self_dir: str) -> Optional[Dict[str, Any]]:
    """读 ``.pd/intent-contract.json`` 里冻结的派发契约块（旧契约无 → None）。"""
    import json
    import os

    path = os.path.join(self_dir or ".", ".pd", "intent-contract.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            contract = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict):
        return None
    block = contract.get("dispatch")
    return dict(block) if isinstance(block, dict) else None


# ---------------------------------------------------------------------------
# 唯一解析入口
# ---------------------------------------------------------------------------

def resolve_dispatch_policy(
    *,
    config: Optional[Dict[str, Any]] = None,
    run_scope: str = "",
    execution_profile: Optional[str] = None,
    tool_override: Any = None,
    worker_capabilities: Optional[Dict[str, Any]] = None,
    frozen: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """解析一次派发批次的并发策略（C12）。

    参数
        config              原始配置 dict（``load_raw_config`` 的结果）
        run_scope           工作集（full/focused/edit；空=从 config 读）
        execution_profile   执行档位（legacy/faithful_fast；空=从 config 读）
        tool_override       本次工具调用显式并发（None/0 = 不覆盖）
        worker_capabilities 已知 worker 能力声明（None = 未知 → 记 null）
        frozen              已冻结的派发契约块（resume 时优先，PARCFG-03）

    返回（JSON 可序列化；逐批次计划直接落盘这些字段）：
        dispatch_contract_version  1|2
        legacy_contract            bool
        profile / profile_source   档位与来源
        profile_concurrency        档位表值
        configured_concurrency     配置侧并发（0=自动；含工具覆盖）
        concurrency_source         取值来源（config:*/tool:*/frozen:*/auto）
        concurrency_limit          执行上限（configured>0 ? configured : 档位表）
        executor_kind              python_registry | host_subagent
        worker_max_concurrency     已知能力（未知 = None，绝不假装 1）
        capability_verified        能力是否已验证
        effective_concurrency      真实生效并发
        serial_reason              串行原因（>1 时为空串）
        diagnostics                不失败的问题清单
    """
    cfg = dict(config or {})
    diagnostics: List[Dict[str, str]] = []
    frozen_block = dict(frozen or {}) if frozen else None

    # ---- 契约版本 ----
    if frozen_block and frozen_block.get("dispatch_contract_version") is not None:
        version = resolve_contract_version(
            frozen_block.get("dispatch_contract_version"))
    else:
        version = resolve_contract_version(cfg.get("dispatch_contract_version"))
    legacy = version < DISPATCH_CONTRACT_VERSION

    dispatch_raw = cfg.get("agent_dispatch")
    dispatch_map = dispatch_raw if isinstance(dispatch_raw, dict) else {}

    # ---- 档位 ----
    if frozen_block and str(frozen_block.get("profile") or "").strip():
        profile = str(frozen_block["profile"]).strip().lower()
        if profile not in PROFILE_CONCURRENCY:
            raise DispatchPolicyError(
                INVALID_DISPATCH_PROFILE,
                "冻结的派发档位 %r 非法（只能是: %s）"
                % (frozen_block.get("profile"), " / ".join(DISPATCH_PROFILES)))
        profile_source = "frozen:dispatch.profile"
    else:
        explicit, explicit_source = "", ""
        candidates = ((dispatch_map.get("profile"), "config:agent_dispatch.profile"),
                      (dispatch_raw if isinstance(dispatch_raw, str) else None,
                       "config:agent_dispatch"))
        for raw_value, source in candidates:
            if raw_value is None:
                continue
            text = str(raw_value).strip().lower()
            if not text:
                continue
            if text not in PROFILE_CONCURRENCY:
                detail = ("%s=%r 不是合法派发档位（只能是: %s）"
                          % (source, raw_value, " / ".join(DISPATCH_PROFILES)))
                if not legacy:
                    raise DispatchPolicyError(INVALID_DISPATCH_PROFILE, detail)
                diagnostics.append(
                    {"code": INVALID_DISPATCH_PROFILE,
                     "detail": detail + "；旧契约回落 %s" % DEFAULT_PROFILE})
                continue
            explicit, explicit_source = text, source
            break
        if explicit:
            profile, profile_source = explicit, explicit_source
        else:
            scope = str(run_scope or cfg.get("run_scope") or "").strip().lower()
            scope_profile = RUN_SCOPE_DEFAULT_PROFILE.get(scope)
            exec_profile = _execution_profile_of(cfg, execution_profile)
            if scope_profile:
                profile, profile_source = scope_profile, "run_scope:%s" % scope
            elif (not legacy) and exec_profile == FAST_EXECUTION_PROFILE:
                # C12.1：新 faithful_fast 未显式选择档位 → fast（目标并发 4）
                profile, profile_source = (
                    "fast", "execution_profile:%s" % FAST_EXECUTION_PROFILE)
            else:
                profile, profile_source = DEFAULT_PROFILE, "default"

    # ---- 配置侧并发 ----
    nested = _coerce_config_concurrency(
        dispatch_map.get("max_concurrency"),
        key="agent_dispatch.max_concurrency", strict=not legacy,
        diagnostics=diagnostics)
    flat = _coerce_config_concurrency(
        cfg.get("agent_max_concurrency"), key="agent_max_concurrency",
        strict=not legacy, diagnostics=diagnostics)

    if frozen_block and frozen_block.get("configured_concurrency") is not None:
        configured = int(frozen_block.get("configured_concurrency") or 0)
        concurrency_source = str(
            frozen_block.get("concurrency_source") or "frozen:dispatch")
    elif not legacy:
        if nested > 0 and flat > 0 and nested != flat:
            raise DispatchPolicyError(
                CONFIG_CONCURRENCY_CONFLICT,
                "agent_dispatch.max_concurrency=%d 与 agent_max_concurrency=%d "
                "同时为正且不一致：请只保留一处（嵌套块优先，扁平键仅作兼容），"
                "拒绝静默挑一个" % (nested, flat))
        if nested > 0:
            configured, concurrency_source = (
                nested, "config:agent_dispatch.max_concurrency")
        elif flat > 0:
            configured, concurrency_source = flat, "config:agent_max_concurrency"
        else:
            configured, concurrency_source = 0, "auto"
    else:
        # 旧契约保持旧优先级：扁平键优先于嵌套块
        if flat > 0:
            configured, concurrency_source = flat, "config:agent_max_concurrency"
        elif nested > 0:
            configured, concurrency_source = (
                nested, "config:agent_dispatch.max_concurrency")
        else:
            configured, concurrency_source = 0, "auto"
        if nested > 0 and flat > 0 and nested != flat:
            diagnostics.append({
                "code": CONFIG_CONCURRENCY_SHADOWED,
                "detail": ("旧契约：扁平 agent_max_concurrency=%d 覆盖嵌套 "
                           "agent_dispatch.max_concurrency=%d（保持旧优先级）"
                           % (flat, nested))})

    # ---- 本次调用覆盖（最高优先级）----
    override = _coerce_tool_override(tool_override)
    if override > 0:
        configured, concurrency_source = override, "tool:max_concurrency"

    concurrency_limit = configured if configured > 0 else PROFILE_CONCURRENCY[profile]

    # ---- 执行器能力 ----
    if isinstance(worker_capabilities, dict):
        executor_kind = EXECUTOR_PYTHON
        raw_cap = worker_capabilities.get("max_concurrency")
        if isinstance(raw_cap, bool) or raw_cap is None:
            worker_cap: Optional[int] = None
        else:
            try:
                worker_cap = int(raw_cap)
            except (TypeError, ValueError):
                worker_cap = None
            if worker_cap is not None and worker_cap < 1:
                worker_cap = None
    else:
        # 宿主 subagent 通道：没有 Python worker，能力未知（绝不假装 1）
        executor_kind, worker_cap = EXECUTOR_HOST, None

    if worker_cap is None:
        effective = concurrency_limit
        capability_verified = False
    else:
        effective = max(1, min(concurrency_limit, worker_cap))
        capability_verified = True

    if effective > 1:
        serial_reason = ""
    elif concurrency_limit <= 1:
        serial_reason = ("并发上限为 1（来源 %s，档位 %s）"
                         % (concurrency_source, profile))
    elif worker_cap == 1:
        serial_reason = "worker 声明的 max_concurrency=1（串行执行器）"
    else:
        serial_reason = "生效并发为 1（上限 %d）" % concurrency_limit

    return {
        "dispatch_contract_version": version,
        "legacy_contract": legacy,
        "profile": profile,
        "profile_source": profile_source,
        "profile_concurrency": PROFILE_CONCURRENCY[profile],
        "configured_concurrency": configured,
        "concurrency_source": concurrency_source,
        "concurrency_limit": concurrency_limit,
        "executor_kind": executor_kind,
        "worker_max_concurrency": worker_cap,
        "capability_verified": capability_verified,
        "effective_concurrency": effective,
        "serial_reason": serial_reason,
        "diagnostics": diagnostics,
    }


def describe_for_phase0(config: Optional[Dict[str, Any]] = None, *,
                        run_scope: str = "",
                        execution_profile: Optional[str] = None) -> Dict[str, Any]:
    """Phase 0 展示用摘要（含冻结块，供意图契约落盘）。"""
    policy = resolve_dispatch_policy(
        config=config, run_scope=run_scope,
        execution_profile=execution_profile)
    return {
        "dispatch_contract_version": policy["dispatch_contract_version"],
        "profile": policy["profile"],
        "profile_source": policy["profile_source"],
        "configured_concurrency": policy["configured_concurrency"],
        "concurrency_source": policy["concurrency_source"],
        "concurrency_limit": policy["concurrency_limit"],
        "executor_kind": policy["executor_kind"],
        "effective_concurrency": policy["effective_concurrency"],
        "serial_reason": policy["serial_reason"],
        "diagnostics": policy["diagnostics"],
    }
