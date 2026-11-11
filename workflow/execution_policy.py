#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""execution_policy.py — 执行契约（legacy | faithful_fast）的解析、冻结与恢复判定。

任务10（DATA-CONTRACTS C1）：把「内容策略 content_policy」与「执行图
execution_profile」彻底分开：

  - content_policy=faithful|authoring 只描述内容忠实度，**不再隐含换图**；
  - execution_profile=legacy|faithful_fast 决定编译哪一张 StateGraph；
  - 旧检查点/旧契约没有执行版本 -> legacy-v1，绝不静默换图、绝不重写旧 SQLite；
  - 冻结版本未知或与契约冲突 -> 明确报错，不猜测回退。

设计约束：
  * 本模块不编译任何图，也不读写检查点，只做纯函数解析（便于单测）。
  * 冻结元数据只进 state 与 intent-contract，**不进 TeX 版式宏**
    （gen_config.generate 的 text_keys / dialogue_* 都不含这些键）。
  * 布尔读取复用字符串规范化后显式比较（`bool('false')` 是错误实现）。
  * 整数排除 bool（`isinstance(True, int)` 为真，必须显式拒绝）。
"""

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

LEGACY_PROFILE = "legacy"
FAST_PROFILE = "faithful_fast"
EXECUTION_PROFILES = (LEGACY_PROFILE, FAST_PROFILE)

#: 新冻结元数据版本；旧契约没有该字段 -> 按 legacy 解释（不写回旧契约）
EXECUTION_SCHEMA_VERSION = 2
LEGACY_EXECUTION_SCHEMA_VERSION = 1

GRAPH_VERSION_LEGACY = "legacy-v1"
GRAPH_VERSION_FAST = "faithful-fast-v1"
GRAPH_VERSIONS = (GRAPH_VERSION_LEGACY, GRAPH_VERSION_FAST)

PROFILE_TO_GRAPH_VERSION = {
    LEGACY_PROFILE: GRAPH_VERSION_LEGACY,
    FAST_PROFILE: GRAPH_VERSION_FAST,
}
GRAPH_VERSION_TO_PROFILE = {
    GRAPH_VERSION_LEGACY: LEGACY_PROFILE,
    GRAPH_VERSION_FAST: FAST_PROFILE,
}

#: 已真正实现（可运行）的执行图版本；faithful-fast-v1 目前只登记版本与显式
#: 未实现报错，节点由后续轻量图任务接通 —— 绝不静默回退 legacy。
IMPLEMENTED_GRAPH_VERSIONS = (GRAPH_VERSION_LEGACY,)

#: C1 新增扁平配置键的工程初始值（faithful_fast 建议值，不是已验证最佳值）
EXECUTION_DEFAULTS = {
    "pedagogy_policy": "advisory",
    "budget_policy": "advisory",
    "require_classroom_plan": False,
    "writer_batch_max_sections": 2,
    "writer_batch_max_source_chars": 16000,
    "agent_prompt_max_chars": 32000,
    "repair_max_per_scope": 2,
    "repair_max_total": 6,
}
#: 执行相关扁平键（execution_profile 单独参与图选择，不进入 execution_config）
EXECUTION_KEYS = tuple(EXECUTION_DEFAULTS)
OVERRIDE_KEYS = EXECUTION_KEYS + ("execution_profile",)

#: legacy 旧默认值的描述性矩阵（C1 规则5：缺新字段的旧配置按旧默认解释）。
#: 这些值只用于说明与断言，legacy 图不读取新键；None = 旧行为不设上限。
LEGACY_EXECUTION_DEFAULTS = {
    "pedagogy_policy": "strict",
    "budget_policy": "strict",
    "require_classroom_plan": True,
    "writer_batch_max_sections": 1,
    "writer_batch_max_source_chars": None,
    "agent_prompt_max_chars": None,
    "repair_max_per_scope": 3,
    "repair_max_total": None,
}

EXECUTION_ENUMS = {
    "pedagogy_policy": ("advisory", "strict"),
    "budget_policy": ("advisory", "strict"),
}
EXECUTION_BOOL_KEYS = ("require_classroom_plan",)
EXECUTION_INT_KEYS = (
    "writer_batch_max_sections",
    "writer_batch_max_source_chars",
    "agent_prompt_max_chars",
    "repair_max_per_scope",
    "repair_max_total",
)

#: 影响内容的既有键（含对话设置）——一并纳入有效策略摘要 policy_digest
CONTENT_AFFECTING_EXTRA_KEYS = (
    "dialogue_enabled",
    "dialogue_section_opening",
    "dialogue_example_opening",
    "dialogue_max_section_turns",
    "dialogue_max_example_turns",
    "dialogue_student_reveal",
    "dialogue_require_coverage_hook",
    "dialogue_sections",
    "delivery_profile",
    "lesson_minutes",
    "core_task_limit",
    "max_student_pages",
    "must_cover",
    "student_level",
    "allowed_extensions",
    "reorder_allowed",
    "source_files",
    "excluded_sources",
    "agent_dispatch",
    "agent_max_concurrency",
)

#: 冻结元数据必备字段（顺序即 C1 声明顺序）
FROZEN_FIELDS = (
    "execution_schema_version",
    "execution_profile",
    "graph_version",
    "policy_digest",
    "execution_config",
)

_TRUE_STRS = ("true", "1", "yes", "on")
_FALSE_STRS = ("false", "0", "no", "off")


class ExecutionPolicyError(ValueError):
    """执行契约解析/冻结/恢复判定失败（显式错误码，不静默回退）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__("%s: %s" % (self.code, self.detail) if self.detail else self.code)

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "detail": self.detail}


# ---------------------------------------------------------------------------
# 标量规范化（复用「字符串规范化 + 显式比较」策略）
# ---------------------------------------------------------------------------

def normalize_bool(value: Any, key: str = "") -> bool:
    """把 true/false 的各种书写规范化为 bool；其它取值显式拒绝。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # int 只能是 0/1；bool 已在上面处理，这里不会把 True 当 1
        if value in (0, 1):
            return bool(value)
        raise ExecutionPolicyError(
            "BAD_BOOL", "%s 取值 “%s” 非法，只能是 true / false" % (key or "bool", value))
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRS:
            return True
        if low in _FALSE_STRS:
            return False
    raise ExecutionPolicyError(
        "BAD_BOOL", "%s 取值 “%s” 非法，只能是 true / false" % (key or "bool", value))


def coerce_int(value: Any, key: str = "") -> int:
    """正整数解析：排除 bool（`isinstance(True, int)` 为真，必须显式拒绝）。"""
    if isinstance(value, bool):
        raise ExecutionPolicyError(
            "BAD_INT", "%s 不能是布尔值（收到 %r）；布尔只能用于 bool 字段"
            % (key or "int", value))
    if isinstance(value, int):
        num = value
    elif isinstance(value, str) and re.match(r"^\s*\d+\s*$", value):
        num = int(value.strip())
    else:
        raise ExecutionPolicyError(
            "BAD_INT", "%s 必须是正整数（收到 %r）" % (key or "int", value))
    if num < 1:
        raise ExecutionPolicyError(
            "BAD_INT", "%s 必须是正整数（收到 %r）" % (key or "int", value))
    return num


def coerce_enum(value: Any, key: str, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    text = str(value).strip().lower() if value is not None else ""
    if text not in allowed:
        raise ExecutionPolicyError(
            "BAD_ENUM", "%s 取值 “%s” 非法，只能是: %s"
            % (key, value, " / ".join(allowed)))
    return text


def resolve_profile(value: Any) -> str:
    """解析 execution_profile；缺省（None/空串）= legacy。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return LEGACY_PROFILE
    return coerce_enum(value, "execution_profile", EXECUTION_PROFILES)


def graph_version_for_profile(profile: Any) -> str:
    prof = resolve_profile(profile)
    return PROFILE_TO_GRAPH_VERSION[prof]


def suggest_profile(content_policy: Any) -> str:
    """C1 规则4：content_policy=faithful 建议 faithful_fast；authoring 默认 legacy。

    注意这是**建议**：content_policy 本身绝不决定执行图。
    """
    policy = str(content_policy or "faithful").strip().lower()
    return FAST_PROFILE if policy == "faithful" else LEGACY_PROFILE


def cache_namespace(profile: Any) -> str:
    """新旧缓存命名空间：legacy 与 faithful_fast 必须分离，避免串用派生物。"""
    return graph_version_for_profile(profile)


# ---------------------------------------------------------------------------
# 有效执行配置
# ---------------------------------------------------------------------------

def resolve_execution_config(values: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """按 C1 把扁平配置解析为规范化 execution_config（缺省填工程初始值）。"""
    values = values or {}
    out: Dict[str, Any] = {}
    for key in EXECUTION_KEYS:
        raw = values.get(key)
        if raw is None:
            out[key] = EXECUTION_DEFAULTS[key]
            continue
        if key in EXECUTION_ENUMS:
            out[key] = coerce_enum(raw, key, EXECUTION_ENUMS[key])
        elif key in EXECUTION_BOOL_KEYS:
            out[key] = normalize_bool(raw, key)
        else:
            out[key] = coerce_int(raw, key)
    return out


def read_execution_overrides(config_path: Optional[str]) -> Dict[str, Any]:
    """从 config.yaml 读取执行相关键的原始取值（只认这 9 个键）。

    与 scripts/gen_config.py 的扁平语义一致：优先 PyYAML，缺失时按
    `key: value` 行解析；只返回执行键，不解释其它配置。
    """
    out: Dict[str, Any] = {}
    if not config_path or not os.path.isfile(config_path):
        return out
    try:
        with open(config_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return out
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            for key in OVERRIDE_KEYS:
                if key in data and data[key] is not None:
                    out[key] = data[key]
            return out
    except Exception:
        pass
    for line in text.splitlines():
        if line[:1] in (" ", "\t"):
            continue  # 缩进行 = 嵌套块，扁平解析器不识别
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        if key not in OVERRIDE_KEYS:
            continue
        val = val.strip()
        if val[:1] in ('"', "'"):
            quote = val[0]
            end = val.find(quote, 1)
            val = val[1:end] if end > 0 else val[1:]
        else:
            val = val.split(" #", 1)[0].strip()
        if val != "":
            out[key] = val
    return out


def _canonical_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def policy_digest(execution_config: Mapping[str, Any], *, execution_profile: str,
                  content_policy: Any = None,
                  extra_config: Optional[Mapping[str, Any]] = None) -> str:
    """有效策略摘要：内容策略 + 执行配置 + 影响内容的既有键（含对话设置）。"""
    payload: Dict[str, Any] = {
        "content_policy": str(content_policy or "faithful").strip().lower(),
        "execution_profile": str(execution_profile),
        "execution_config": resolve_execution_config(execution_config),
    }
    if extra_config:
        payload["content_affecting"] = {
            key: _canonical_scalar(extra_config.get(key))
            for key in CONTENT_AFFECTING_EXTRA_KEYS
            if key in extra_config
        }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 冻结元数据
# ---------------------------------------------------------------------------

def build_frozen_metadata(*, profile: Any, execution_config: Mapping[str, Any],
                          content_policy: Any = None,
                          extra_config: Optional[Mapping[str, Any]] = None
                          ) -> Dict[str, Any]:
    """构造 C1 冻结元数据块（写入 state 与 intent-contract，不输出到 TeX）。"""
    prof = resolve_profile(profile)
    cfg = resolve_execution_config(execution_config)
    return {
        "execution_schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_profile": prof,
        "graph_version": graph_version_for_profile(prof),
        "policy_digest": policy_digest(
            cfg, execution_profile=prof, content_policy=content_policy,
            extra_config=extra_config),
        "execution_config": cfg,
    }


def resolve_new_run(spec: Optional[Mapping[str, Any]],
                    *, config_path: Optional[str] = None) -> Dict[str, Any]:
    """新 run：按 C1 解析有效执行配置并冻结（spec 覆盖 config.yaml）。

    execution_profile 的优先级：Phase 0 确认的 spec > config.yaml 声明 > legacy。
    缺省（两处都没有）= legacy：C1 规则5「缺新字段的旧配置按旧默认解释」，
    也让旧工程在不改任何配置的情况下行为完全不变。
    """
    spec = spec or {}
    overrides = dict(read_execution_overrides(config_path))
    merged: Dict[str, Any] = dict(overrides)
    for key in EXECUTION_KEYS:
        if key in spec and spec[key] is not None:
            merged[key] = spec[key]
    profile = spec.get("execution_profile")
    if profile is None or (isinstance(profile, str) and not profile.strip()):
        profile = overrides.get("execution_profile")
    return build_frozen_metadata(
        profile=profile,
        execution_config=merged,
        content_policy=spec.get("content_policy"),
        extra_config=spec,
    )


def _extract_frozen(source: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """从契约/状态里取冻结块；完全没有执行字段 -> None（legacy）。"""
    if not isinstance(source, Mapping):
        return None
    if not any(source.get(field) not in (None, "") for field in FROZEN_FIELDS):
        return None
    missing = [f for f in FROZEN_FIELDS if source.get(f) in (None, "")]
    if missing:
        raise ExecutionPolicyError(
            "EXECUTION_METADATA_INCOMPLETE",
            "执行契约字段不完整，缺少: %s（拒绝按猜测的默认值恢复）"
            % ", ".join(missing))
    graph_version = str(source.get("graph_version")).strip()
    if graph_version not in GRAPH_VERSIONS:
        raise ExecutionPolicyError(
            "UNKNOWN_GRAPH_VERSION",
            "冻结的执行图版本 “%s” 未知（已知: %s）；拒绝猜测回退"
            % (graph_version, " / ".join(GRAPH_VERSIONS)))
    profile = resolve_profile(source.get("execution_profile"))
    if PROFILE_TO_GRAPH_VERSION.get(profile) != graph_version:
        raise ExecutionPolicyError(
            "EXECUTION_PROFILE_GRAPH_MISMATCH",
            "执行模式 “%s” 与冻结图版本 “%s” 不匹配"
            % (profile, graph_version))
    return {
        "execution_schema_version": int(source.get("execution_schema_version")),
        "execution_profile": profile,
        "graph_version": graph_version,
        "policy_digest": str(source.get("policy_digest")),
        "execution_config": resolve_execution_config(
            source.get("execution_config") or {}),
    }


def frozen_from_contract(contract: Optional[Mapping[str, Any]]
                         ) -> Optional[Dict[str, Any]]:
    """从 intent-contract 取冻结块；旧契约（无执行字段）返回 None。"""
    return _extract_frozen(contract or {})


def frozen_from_state(values: Optional[Mapping[str, Any]]
                      ) -> Optional[Dict[str, Any]]:
    """从检查点 channel_values 取冻结块；旧检查点返回 None。"""
    return _extract_frozen(values or {})


def resolve_resume(values: Optional[Mapping[str, Any]],
                   contract: Optional[Mapping[str, Any]] = None
                   ) -> Dict[str, Any]:
    """恢复判定：先看检查点冻结版本，再看契约，最后才谈当前 YAML。

    返回 {"execution_profile", "graph_version", "legacy", "frozen"}；
    冲突/未知版本抛 ExecutionPolicyError，绝不猜测回退。
    """
    state_frozen = frozen_from_state(values)
    contract_frozen = frozen_from_contract(contract)
    if state_frozen is None and contract_frozen is None:
        # C1 规则2：旧检查点/旧契约没有执行版本 -> legacy-v1。
        # 注意 content_policy=faithful 不得在此改变判定。
        return {
            "execution_profile": LEGACY_PROFILE,
            "graph_version": GRAPH_VERSION_LEGACY,
            "legacy": True,
            "frozen": None,
        }
    if state_frozen is not None and contract_frozen is None:
        # F1：Phase 0 还没确认——新建的 run 已经把执行契约写进 checkpoint
        # （runner.init_state），但 intent-contract.json 要等 Phase 0 确认才落盘。
        # 这个窗口里 checkpoint 才是唯一已冻结的真源，绝不能因"契约一方缺失"
        # 就把会话判成 legacy（那会让 resume 编译 legacy 图，与首次运行实际
        # 使用的图不一致）。
        return {
            "execution_profile": state_frozen["execution_profile"],
            "graph_version": state_frozen["graph_version"],
            "legacy": False,
            "frozen": state_frozen,
            "contract_pending": True,
        }
    if state_frozen is None or contract_frozen is None:
        raise ExecutionPolicyError(
            "EXECUTION_CONTRACT_CONFLICT",
            "检查点与 intent-contract 的执行契约不一致（一方缺失）："
            "checkpoint=%s, contract=%s"
            % ("有" if state_frozen else "缺", "有" if contract_frozen else "缺"))
    for field in FROZEN_FIELDS:
        if field == "execution_config":
            # F3：旧实现直接 continue，冻结的有效执行配置（策略/预算/批次上限）
            # 与契约不一致时无人发现。逐键比对，任何漂移都显式拒绝。
            if resolve_execution_config(state_frozen.get(field) or {}) != \
                    resolve_execution_config(contract_frozen.get(field) or {}):
                raise ExecutionPolicyError(
                    "EXECUTION_CONTRACT_CONFLICT",
                    "检查点与 intent-contract 的执行配置冲突："
                    "checkpoint=%r contract=%r"
                    % (state_frozen.get(field), contract_frozen.get(field)))
            continue
        if state_frozen.get(field) != contract_frozen.get(field):
            raise ExecutionPolicyError(
                "EXECUTION_CONTRACT_CONFLICT",
                "检查点与 intent-contract 的执行契约冲突：%s checkpoint=%r contract=%r"
                % (field, state_frozen.get(field), contract_frozen.get(field)))
    return {
        "execution_profile": state_frozen["execution_profile"],
        "graph_version": state_frozen["graph_version"],
        "legacy": False,
        "frozen": state_frozen,
    }


def assert_config_not_overriding(self_dir: Optional[str], policy: Mapping[str, Any]
                                 ) -> None:
    """C1 规则3/6：当前 config.yaml 不得覆盖已确认的执行版本。

    legacy 会话同样是「已冻结策略」：config.yaml 突然写 faithful_fast 必须显式
    重新确认（新 thread），否则旧会话会被当前 YAML 静默换图。

    F3：不只比 execution_profile——``pedagogy_policy`` / ``budget_policy`` /
    ``require_classroom_plan`` / ``repair_max_*`` 这些同样在 Phase 0 冻结，
    改 YAML 让已开始的会话被放宽（strict→advisory、必需教案→可选）同样属于
    「静默改写已确认策略」，必须显式拒绝。
    """
    if not self_dir:
        return
    frozen_profile = resolve_profile((policy or {}).get("execution_profile"))
    declared = read_execution_overrides(os.path.join(self_dir, "config.yaml"))
    profile = declared.get("execution_profile")
    if profile is not None:
        declared_profile = coerce_enum(profile, "execution_profile", EXECUTION_PROFILES)
        if declared_profile != frozen_profile:
            raise ExecutionPolicyError(
                "EXECUTION_PROFILE_OVERRIDE_REJECTED",
                "config.yaml 的 execution_profile=“%s” 不能覆盖已冻结的 “%s”；"
                "改冻结策略必须新 thread 或显式重新确认"
                % (declared_profile, frozen_profile))
    frozen_config = resolve_execution_config(
        (policy or {}).get("frozen", {}).get("execution_config")
        if isinstance((policy or {}).get("frozen"), Mapping) else {})
    if not any(key in declared for key in EXECUTION_KEYS):
        return
    merged = dict(frozen_config)
    for key in EXECUTION_KEYS:
        if key in declared and declared[key] is not None:
            merged[key] = declared[key]
    drift = {
        key: (frozen_config.get(key), corrected)
        for key, corrected in resolve_execution_config(merged).items()
        if frozen_config.get(key) != corrected
    } if frozen_config else {}
    if drift:
        raise ExecutionPolicyError(
            "EXECUTION_CONFIG_OVERRIDE_REJECTED",
            "config.yaml 的执行策略不能覆盖已冻结值: %s；"
            "改策略必须新 thread 或显式重新确认"
            % ", ".join("%s 冻结=%r 当前=%r" % (k, v[0], v[1])
                        for k, v in sorted(drift.items())))


def assert_profile_not_overriding(policy: Mapping[str, Any],
                                  requested_profile: Any) -> None:
    """C1 规则3/6：显式请求的 execution_profile 不得覆盖已冻结的执行图。

    恢复时用户可以重复声明同一个值（幂等），但换图必须新 thread 或显式
    重新确认，绝不静默改写旧会话。
    """
    if requested_profile is None or (isinstance(requested_profile, str)
                                     and not requested_profile.strip()):
        return
    frozen_profile = resolve_profile((policy or {}).get("execution_profile"))
    requested = resolve_profile(requested_profile)
    if requested != frozen_profile:
        raise ExecutionPolicyError(
            "EXECUTION_PROFILE_OVERRIDE_REJECTED",
            "请求的 execution_profile=“%s” 不能覆盖已冻结的 “%s”；"
            "改冻结策略必须新 thread 或显式重新确认"
            % (requested, frozen_profile))


def describe_for_phase0(spec: Optional[Mapping[str, Any]],
                        *, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Phase 0 展示用：有效/建议执行策略与默认值矩阵（只读，不冻结）。"""
    spec = spec or {}
    merged: Dict[str, Any] = dict(read_execution_overrides(config_path))
    for key in EXECUTION_KEYS:
        if key in spec and spec[key] is not None:
            merged[key] = spec[key]
    try:
        execution_config = resolve_execution_config(merged)
    except ExecutionPolicyError as exc:
        execution_config = dict(EXECUTION_DEFAULTS)
        execution_error = exc.as_dict()
    else:
        execution_error = None
    try:
        declared = spec.get("execution_profile")
        if declared is None or (isinstance(declared, str) and not declared.strip()):
            declared = merged.get("execution_profile")
        profile = resolve_profile(declared)
    except ExecutionPolicyError as exc:
        profile = LEGACY_PROFILE
        execution_error = exc.as_dict()
    return {
        "effective_profile": profile,
        "effective_graph_version": graph_version_for_profile(profile),
        "recommended_profile": suggest_profile(spec.get("content_policy")),
        "implemented_graph_versions": list(IMPLEMENTED_GRAPH_VERSIONS),
        "content_policy": str(spec.get("content_policy") or "faithful").strip().lower(),
        "execution_config": execution_config,
        "defaults": dict(EXECUTION_DEFAULTS),
        "legacy_defaults": dict(LEGACY_EXECUTION_DEFAULTS),
        "error": execution_error,
        "note": (
            "执行图与内容策略分离：content_policy 不决定执行图；"
            "新 run 需显式确认 execution_profile（faithful 建议 faithful_fast，"
            "authoring 默认 legacy）。faithful_fast 轻量图由任务15 接通，"
            "接通前不会静默回退 legacy。"),
    }
