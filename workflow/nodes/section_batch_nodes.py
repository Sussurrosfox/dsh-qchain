"""section_batch_nodes.py — T2 切片写作 fan-out 派发节点（P1 重构）。

原 T2 是串行链：node_t2_request_sections（挑一节、建派发单、pdstate start）→
node_t2_await_sections（执行）→ node_t2_submit_and_check_sections（submit 并标记
checked）→ 回到 request 节点循环，一次只推进一节。

本模块把该链条替换为「一次规划一批 → 批量派发 → 汇总回归」的三节点 fan-out：

    node_t2_plan_dispatch(state)       三岔：ADVANCE / DONE / PLAN（建批 + 硬门禁）
    node_t2_dispatch_sections(state)   整批提交（有 worker → 线程并行；否则 deferred）
    node_t2_join_sections(state)       汇总：并行结果 / manual 中断恢复 / 磁盘重推导

双模（同一套节点，两种执行形态）：
    (a) worker_registry 已注册认知 worker → WorkerPool 起真线程并行执行；
    (b) 未注册（生产默认）→ WorkerPool 立即记 deferred，本模块退化为
        「一次 interrupt 携带整批 → 人/Agent 完成后从磁盘校验」的手工模式。

断点安全：进程重启后内存池为空，join 节点用 request["before"] 快照与当前文件
sha256 的对比从磁盘重新推导结果；同一 batch_id 绝不二次 interrupt
（state 键 manual_interrupted_batch + LangGraph checkpoint 持久化）。

磁盘契约（scripts/check_dispatch.py 硬门禁 R1-R10，只校验门禁子集）：
    <self_dir>/.pd/dispatch-plan.json
派发单（完整请求，供人/Agent 阅读）：
    <self_dir>/.pd/agent-requests/<dispatch_id>.json

R10（复盘报告 §4.3.3 内容审计前置）：T2 计划（stage == "T2"）的每个请求
必须携带 ``context.source_assets`` 源侧资产计数（figure/table/tikz），
磁盘计划子集同步落盘，供 check_dispatch 逐请求对账、验收 Agent 逐项核对。
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

# 注意：本环境的 langgraph 只在 langgraph.types 下导出 interrupt（顶层无该符号），
# 与 agent_nodes.py 的导入方式保持一致；模块级导入便于测试 monkeypatch 本符号。
from langgraph.types import interrupt

from ..state import ShrinkCoursewareState, GateReport
from ..scope_selection import (
    ScopeSelectionError,
    normalize_targets,
    select_scopes,
)
from ..adapters.script_runner import ScriptRunner
from ..adapters.artifact_cache import (
    ArtifactCache, cache_key, config_digest, file_digest, prompt_version,
)
from ..adapters.worker_pool import WorkerPool
# 任务16（C7）：缓存键必须显式携带图版本/策略档位——同一份输入在 legacy 与
# faithful_fast 下是不同的执行语义，绝不能互相命中对方的派生物。
from .. import execution_policy
# 任务19（C12）：派发并发的唯一解析器（Python/JS 同规则，见
# workflow/dispatch_policy.py 与 scripts/dispatch_policy.mjs）
from .. import dispatch_policy
from ..dispatch_policy import DispatchPolicyError
from .agent_nodes import (
    REQUEST_DIR_REL,
    _write_worker_files,
    build_request,
    sha256_file,
    write_request_file,
)
from . import agent_evidence
from .section_nodes import _ensure_task_ready_and_start

# ---------------------------------------------------------------------------
# 常量：档位、默认值、磁盘相对路径
# ---------------------------------------------------------------------------

#: 派发档位 → 并发上限（agent_dispatch: interactive|safe|fast|budget；
#: audit_agent.md §12.1：safe 档并发 1 曾成生产默认，导致 fan-out 退化为串行。
#: interactive=并发1 交互微调默认；safe=并发1 调试/provider 不稳定保留；
#: fast=并发4 完整生成默认；budget=并发2 成本/限流受限）
#: 任务19（C12）：档位表与解析规则的唯一真相在 workflow/dispatch_policy.py，
#: 本模块只做别名导出（历史调用方仍按 PROFILE_CONCURRENCY 读）。
PROFILE_CONCURRENCY: Dict[str, int] = dict(dispatch_policy.PROFILE_CONCURRENCY)

#: 未配置 agent_dispatch 且 run_scope 未知时的保守回落（串行）
DEFAULT_PROFILE = dispatch_policy.DEFAULT_PROFILE
#: run_scope → 缺省档位（audit §12.1：交互微调 interactive，完整生成 fast）
RUN_SCOPE_DEFAULT_PROFILE: Dict[str, str] = dict(
    dispatch_policy.RUN_SCOPE_DEFAULT_PROFILE)
DEFAULT_BATCH_WINDOW = 2
# 2026-11-15：默认超时 900s → 3600s（生产证据：真实任务 20-50 分钟，
# 900s 斩杀合法长任务且 timeout 可重试 ⇒ 最坏 3×15min 全价盲重）。
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_RETRIES = 2

#: 派发配置文件（位于 self_dir 下，即项目 config.yaml）
DISPATCH_CONFIG_REL = "config.yaml"
#: 磁盘派发计划（check_dispatch.py 的默认读取位置）
DISPATCH_PLAN_REL = os.path.join(".pd", "dispatch-plan.json")
#: check_dispatch 门禁的结构化报告（run_gate_script 自动追加 --json）
DISPATCH_REPORT_REL = os.path.join(".pd", "reports", "check_dispatch.json")
#: 前置依赖表（由主 Agent 生成，尽力而为的拓扑排序输入）
DEPENDENCY_MAP_REL = os.path.join(".pd", "dependency-map.md")

#: 批运行摘要目录（任务21 / C12.4：单协调者写，逐 attempt 事件各自独立文件）
DISPATCH_RUNS_REL = os.path.join(".pd", "dispatch-runs")
#: 批摘要 schema 版本（v2 起才带 peak_observed_host_in_flight / 观测口径）
DISPATCH_RUN_SCHEMA_VERSION = 2
#: 串行/低并行原因词表（C12.4 机器可读口径；未知一律 host_queue_or_unknown）
SERIAL_REASON_CODES = (
    "explicit_safe",
    "single_ready",
    "cache_reuse",
    "artifact_dependency",
    "worker_cap",
    "host_queue_or_unknown",
)
UNKNOWN_SERIAL_REASON = "host_queue_or_unknown"
#: 生产内容门禁的并发告警码（只告警，绝不因此返修正文）
PARALLELISM_BELOW_DECLARED = "PARALLELISM_BELOW_DECLARED"
PARALLELISM_UNVERIFIED = "PARALLELISM_UNVERIFIED"

#: 不再进入派发批的小节状态（staged/running 由串行提交节点接管；
#: reused/skipped 是任务3 工作集外小节的终态——不得再派发，也不伪装 ok；
#: prebuild_ready 是 faithful_fast 图的「已产出并通过构建前结构/身份检查」，
#: 此时审读证据要等合并终审，故它不是 pending——否则作者循环会反复重写
#: 已经产出的切片（GRAPH-04）。
NON_PENDING_STATUSES = (
    "checked", "accepted", "staged", "running", "reused", "skipped",
    "prebuild_ready")

#: dispatch_results 单条目 output 的最大序列化长度（超过则省略，防止状态膨胀）
MAX_OUTPUT_CHARS = 4096

#: dependency-map.md 的节点编号形态（sec1 / sec0 / sec12 / secA-1 …）
RE_SECTION_NODE = re.compile(r"^sec[0-9A-Za-z_-]+$")

# ---------------------------------------------------------------------------
# 卡13（C5）：单作者多节——合批上限、暂存区、逐节提交
# ---------------------------------------------------------------------------
#: 一个作者 agent 默认最多承担的相邻节数（C5）
DEFAULT_WRITER_BATCH_MAX_SECTIONS = 2
#: 合批时逐节来源字符预算（C5；0 表示该节没有可归属的源切片载荷）
DEFAULT_WRITER_BATCH_MAX_SOURCE_CHARS = 16000
#: 逐节候选产物暂存区（相对 self_dir）。作者整批返回先落这里，通过逐节
#: 校验后才原子提升到 self_dir/<sec>.tex——这样"半批成功"不会污染正本。
WRITER_STAGING_DIR_REL = os.path.join(".pd", "writer-staging")
#: 作者返回里的文件边界标记（% FILE: sec1.tex）
RE_WRITER_FILE_BLOCK = re.compile(
    r"^[ \t]*%[ \t]*FILE:[ \t]*([0-9A-Za-z_.\-/]+)[ \t]*$", re.MULTILINE)


class WriterBatchError(Exception):
    """合批 / 逐节提交的结构性失败（code 供上层 HALT 或返修决策）。"""

    def __init__(self, code: str, detail: str, scopes: Optional[List[str]] = None):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.scopes = list(scopes or [])

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "scopes": list(self.scopes)}


class WriterParseError(WriterBatchError):
    """作者返回无法安全切分成逐节产物（不猜边界，整批失败）。"""

    def __init__(self, detail: str, scopes: Optional[List[str]] = None):
        super().__init__("WRITER_PARSE_FAILED", detail, scopes)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    """ISO8601 时间戳（UTC，带时区）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _coerce_int(value: Any, default: int) -> int:
    """把任意标量归一为 int；非法/空值回落 default（绝不抛异常）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text, 10)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return default
    return default


def _write_json_atomic(path: str, payload: Any) -> str:
    """UTF-8 JSON 原子写（tmp + os.replace）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    """兼容 dataclass / dict 两种结果形态的字段读取。"""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _shrink_output(output: Any) -> Any:
    """output 序列化后过大则整体省略（返回 None 表示省略）。"""
    if output is None:
        return None
    try:
        text = json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(output)
    if len(text) > MAX_OUTPUT_CHARS:
        return None
    return output


def _join_timeout(requests: List[Dict[str, Any]]) -> float:
    """join 的等待上界：最晚请求超时 + 5s 余量（与规格一致）。"""
    values: List[float] = []
    for req in requests:
        try:
            seconds = float(req.get("timeout_seconds"))
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            values.append(seconds)
    base = max(values) if values else float(DEFAULT_TIMEOUT_SECONDS)
    return base + 5.0


def _gate_error_codes(report: GateReport) -> str:
    """从 GateReport 提取门禁错误码串（check_dispatch 用 errors[].code/detail）。"""
    raw = report.get("raw_report") or {}
    codes: List[str] = []
    for err in (raw.get("errors") or []):
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip()
            detail = str(err.get("detail") or "").strip()
            codes.append(f"{code} {detail}".strip() if code else detail)
        else:
            codes.append(str(err))
    if not codes:
        for issue in (report.get("issues") or []):
            if isinstance(issue, dict):
                codes.append(str(issue.get("code") or issue.get("detail") or ""))
            else:
                codes.append(str(issue))
    if not codes:
        codes = [str(report.get("status") or "UNKNOWN")]
    return "; ".join(c for c in codes if c) or "未知门禁错误"


# ---------------------------------------------------------------------------
# 配置装载（templates/config.yaml 的 agent_dispatch 配置块）
# ---------------------------------------------------------------------------

def _strip_inline_comment(line: str) -> str:
    """去掉行内 `#` 注释（引号内的 # 保留）。"""
    quote: Optional[str] = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:idx]
    return line


def _parse_scalar(text: str) -> Any:
    """单行标量解析：引号字符串 / 布尔 / null / int / float / 原样字符串。"""
    text = text.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_flat_yaml(text: str) -> Dict[str, Any]:
    """容错的扁平 `key: value` 解析器（PyYAML 缺失/失败时的兜底）。

    配置块全部是单行标量，因此该解析与 yaml.safe_load 结果一致；
    畸形行（无冒号、注释、空行、列表/映射行）一律容忍跳过。
    """
    out: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = _strip_inline_comment(raw).strip()
        if not line or line.startswith(("#", "-", "%", "[", "{")):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or key.startswith(("[", "{")):
            continue
        out[key] = _parse_scalar(value)
    return out


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔 coercion：bool / true|yes|on|1 / false|no|off|0 / 数值；其余回落默认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_mapping(value: Any) -> Dict[str, Any]:
    """把 YAML 值规整为 dict（非映射返回 {}）。"""
    return dict(value) if isinstance(value, dict) else {}


def _load_dispatch_config(self_dir: str, run_scope: str = "") -> Dict[str, Any]:
    """读取 <self_dir>/config.yaml 的 agent_* 派发配置。

    优先 PyYAML（``import yaml`` + ``yaml.safe_load``）；缺失或解析失败时退回
    容错的扁平 `key: value` 解析。任何畸形行都被容忍，绝不抛异常。

    ``agent_dispatch`` 键多态兼容（q_audit.md 证据链改造新增嵌套块）：
        - 标量（旧档位）：``agent_dispatch: safe``；
        - 映射（新）：``agent_dispatch: {profile: fast, allow_unattested: false,
          model_map: {...}, max_concurrency: 4}``——与既有扁平键共存，扁平键
          优先，映射键补缺。

    返回（全部 JSON 可序列化标量）：
        profile                    档位 interactive|safe|fast|budget
        max_concurrency            解析出的实际并发上限（显式覆盖 > 档位表）
        configured_max_concurrency 配置原值（0 = 按档位自动）
        batch_window               批次窗口倍数（>= 1）
        timeout_seconds            单请求超时（>= 1）
        max_retries                transient 故障重试次数（>= 0）
        allow_unattested           无代理证据产物是否放行（默认 False；逃生阀）
        model_map                  子 Agent 路由表 {阶段: {provider, model}}
                                  （需 PyYAML 解析嵌套映射；扁平兜底下为空）
    """  # noqa: D401  (任务19：解析规则见 workflow/dispatch_policy.py)

    path = os.path.join(self_dir or ".", DISPATCH_CONFIG_REL)
    text = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""

    raw: Dict[str, Any] = {}
    if text.strip():
        parsed: Any = None
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(text)
            if isinstance(loaded, dict):
                parsed = loaded
        except Exception:
            parsed = None
        raw = parsed if isinstance(parsed, dict) else _parse_flat_yaml(text)

    dispatch_map = raw.get("agent_dispatch")
    dispatch_map = dict(dispatch_map) if isinstance(dispatch_map, dict) else {}
    scope = str(run_scope or raw.get("run_scope") or "").strip().lower()

    # 任务19（C12）：解析交给唯一解析器；旧检查点已冻结的 dispatch 块优先
    # （resume 不得因当前 config.yaml 漂移把运行中的批次放大或串行化）。
    dispatch_error: Optional[Dict[str, str]] = None
    try:
        policy = dispatch_policy.resolve_dispatch_policy(
            config=raw,
            run_scope=scope,
            frozen=dispatch_policy.frozen_dispatch_block(self_dir),
        )
    except DispatchPolicyError as exc:
        dispatch_error = exc.as_dict()
        policy = _fallback_dispatch_policy(raw, scope)

    window = max(1, _coerce_int(raw.get("agent_batch_window"), DEFAULT_BATCH_WINDOW))
    timeout_seconds = max(
        1, _coerce_int(raw.get("agent_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS))
    max_retries = max(0, _coerce_int(raw.get("agent_max_retries"), DEFAULT_MAX_RETRIES))
    allow_unattested = _coerce_bool(
        dispatch_map.get("allow_unattested", raw.get("agent_allow_unattested", False)),
        default=False,
    )
    model_map = {
        str(stage).strip().upper(): _coerce_mapping(route)
        for stage, route in _coerce_mapping(dispatch_map.get("model_map")).items()
        if _coerce_mapping(route)
    }

    return {
        "profile": policy["profile"],
        "max_concurrency": policy["concurrency_limit"],
        "configured_max_concurrency": policy["configured_concurrency"],
        "batch_window": window,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "allow_unattested": allow_unattested,
        "model_map": model_map,
        # 任务19（C12）：派发契约与执行器计划的可追溯字段
        "dispatch_contract_version": policy["dispatch_contract_version"],
        "profile_source": policy["profile_source"],
        "configured_concurrency": policy["configured_concurrency"],
        "concurrency_source": policy["concurrency_source"],
        "concurrency_limit": policy["concurrency_limit"],
        "executor_kind": policy["executor_kind"],
        "worker_max_concurrency": policy["worker_max_concurrency"],
        "capability_verified": policy["capability_verified"],
        "effective_concurrency": policy["effective_concurrency"],
        "serial_reason": policy["serial_reason"],
        "diagnostics": list(policy.get("diagnostics") or []),
        "dispatch_error": dispatch_error,
        # 任务1：工作集选择（config.yaml 持久化；state 缺省时回落此处，
        # 覆盖 resume/进程重启路径。非法值由 _work_set 报错，不静默回退 full）
        "run_scope": str(raw.get("run_scope") or "").strip(),
        "target_sections": normalize_targets(raw.get("target_sections")),
        "target_qids": normalize_targets(raw.get("target_qids")),
        # 卡13（C5）：单作者多节合批上限。一个作者 agent 最多写
        # writer_batch_max_sections 个相邻节、合计来源不超过
        # writer_batch_max_source_chars 字符、最终 prompt 不超过
        # agent_prompt_max_chars（0 = 不设 prompt 上限）。
        # 三个都是 Phase 0 扁平键（gen_config.FIELDS 已声明），缺省取 C5 值。
        "writer_batch_max_sections": max(
            1, _coerce_int(raw.get("writer_batch_max_sections"),
                           DEFAULT_WRITER_BATCH_MAX_SECTIONS)),
        "writer_batch_max_source_chars": max(
            1, _coerce_int(raw.get("writer_batch_max_source_chars"),
                           DEFAULT_WRITER_BATCH_MAX_SOURCE_CHARS)),
        "agent_prompt_max_chars": max(
            0, _coerce_int(raw.get("agent_prompt_max_chars"), 0)),
    }


def _fallback_dispatch_policy(raw: Dict[str, Any],
                              run_scope: str = "") -> Dict[str, Any]:
    """配置致命错误时的最小可用策略（只为让调用方拿到错误码并 HALT）。

    做法是剔除并发相关键后重新解析一次：档位/来源仍然真实（不是编造的 2），
    并发回落到档位表。真正的错误由 ``dispatch_error`` 原样上报，绝不被这里吞掉。
    """
    sanitized = dict(raw or {})
    sanitized.pop("agent_max_concurrency", None)
    nested = sanitized.get("agent_dispatch")
    if isinstance(nested, dict):
        inner = dict(nested)
        inner.pop("max_concurrency", None)
        sanitized["agent_dispatch"] = inner
    try:
        return dispatch_policy.resolve_dispatch_policy(
            config=sanitized, run_scope=run_scope)
    except DispatchPolicyError:
        return dispatch_policy.resolve_dispatch_policy(config={}, run_scope=run_scope)


# ---------------------------------------------------------------------------
# 依赖表解析与 pending 排序（尽力而为，任何失败都回退 expected 顺序）
# ---------------------------------------------------------------------------

def _parse_dependency_map(self_dir: str) -> Dict[str, List[str]]:
    """解析 <self_dir>/.pd/dependency-map.md 的前置依赖表。

    真实格式（references/production.md:59，由主 Agent 生成的 markdown 表格）：
        节点编号 | 必须先知道什么 | 学案中首次出现位置 | 是否已闭合
    典型行：``| sec2 | sec1 | 第 2 节 | 是 |``

    返回 ``{节点: [前置节点, ...]}``；文件缺失、读失败或任何解析异常一律
    返回 ``{}``（调用方回退 expected_sections 顺序，绝不因此崩溃）。
    """
    path = os.path.join(self_dir or ".", DEPENDENCY_MAP_REL)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    deps: Dict[str, List[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 过滤表头与 |---|---| 分隔行
        cells = [c for c in cells if c and not re.match(r"^:?-{2,}:?$", c)]
        if len(cells) < 2:
            continue
        node = cells[0]
        if not RE_SECTION_NODE.match(node):
            continue
        prereq_text = cells[1]
        if not prereq_text or prereq_text in ("-", "无", "待审", "—", "－"):
            deps.setdefault(node, [])
            continue
        found: List[str] = []
        for token in re.split(r"[,，;；/、\s]+", prereq_text):
            token = token.strip()
            if RE_SECTION_NODE.match(token) and token != node:
                found.append(token)
        deps[node] = found
    return deps


def _order_pending_sections(
    self_dir: str,
    pending: List[str],
    expected_sections: List[str],
) -> List[str]:
    """pending 节的派发顺序：依赖表拓扑序（尽力而为）→ 失败回退 expected 顺序。

    - 只保留 pending 内部的前置边（已完成/未列入的节不构成约束）；
    - Kahn 拓扑排序，就绪集按 expected_sections 顺序出队（确定性输出）；
    - 依赖表缺失/不可解析/有环 → 回退 expected_sections 顺序，绝不抛异常。
    """
    pending_set = set(pending or [])
    # 基准顺序：expected_sections 中仍处于 pending 的节（缺省顺序即 expected 顺序）
    base = [sec for sec in (expected_sections or []) if sec in pending_set]
    # expected 之外的 pending 节（理论不出现）按原顺序附后，保持宽容
    for sec in (pending or []):
        if sec not in base:
            base.append(sec)
    if len(base) < 2:
        return base
    try:
        deps = _parse_dependency_map(self_dir)
        if not deps:
            return base
        rank = {sec: idx for idx, sec in enumerate(base)}
        edges: Dict[str, List[str]] = {}
        for sec in base:
            prereqs = [d for d in deps.get(sec, []) if d in pending_set and d != sec]
            edges[sec] = list(dict.fromkeys(prereqs))  # 去重保序
        if not any(edges.values()):
            return base

        indegree = {sec: 0 for sec in base}
        for sec, prereqs in edges.items():
            indegree[sec] += len(prereqs)
        ready = sorted(
            [sec for sec in base if indegree[sec] == 0],
            key=lambda s: rank.get(s, 1 << 30))

        out: List[str] = []
        while ready:
            node = ready.pop(0)
            out.append(node)
            for sec in base:
                if node in edges.get(sec, []):
                    indegree[sec] -= 1
                    if indegree[sec] == 0:
                        ready.append(sec)
            ready.sort(key=lambda s: rank.get(s, 1 << 30))
        if len(out) != len(base):
            return base
        return out
    except Exception:
        return base


# ---------------------------------------------------------------------------
# 进程级派发池单例（worker_registry 模式；池绝不进 state）
# ---------------------------------------------------------------------------

_POOL_STATE: Dict[str, Any] = {"batch_id": None, "pool": None}


def _get_pool(
    batch_id: str,
    max_concurrency: int,
    timeout_seconds: int,
    max_retries: int,
) -> WorkerPool:
    """取当前批次的派发池单例；batch_id 变化即重建（旧批结果不可混用）。

    ``executor=None`` 让 WorkerPool 自行解析执行器：worker_registry 已注册认知
    worker → 真线程并行；未注册（生产默认）→ 立即记 deferred 交给调用方。
    池对象不可 JSON 序列化，因此只活在模块级，绝不写入 LangGraph state。
    """
    pool = _POOL_STATE.get("pool")
    if pool is None or _POOL_STATE.get("batch_id") != batch_id:
        pool = WorkerPool(
            executor=None,
            max_concurrency=max_concurrency,
            default_timeout=timeout_seconds,
            max_retries=max_retries,
        )
        _POOL_STATE["pool"] = pool
        _POOL_STATE["batch_id"] = batch_id
    return pool


def _live_pool(batch_id: str) -> Optional[WorkerPool]:
    """仅当单例池属于当前 batch_id 时返回它（否则 None，例如进程重启后）。"""
    pool = _POOL_STATE.get("pool")
    if pool is not None and _POOL_STATE.get("batch_id") == batch_id:
        return pool
    return None


def _reset_pool() -> None:
    """清空派发池单例（测试隔离用）。"""
    _POOL_STATE["pool"] = None
    _POOL_STATE["batch_id"] = None


# ---------------------------------------------------------------------------
# 源资产计数（复盘报告 §4.3.3：内容审计前置）
# ---------------------------------------------------------------------------

#: Phase 0 冻结的输入清单（role=source-baseline 的原材料清单）
SOURCE_MANIFEST_REL = os.path.join(".pd", "source-manifest.json")

#: tikz 块计数口径 = check_fidelity.RE_ASSET_FIGURE 的 tikz 分支
#: （``\\begin{tikzcd}`` / ``\\begin{tikzpicture}``）。与
#: tikz_rasterize.find_blocks 等价：两者都只数剥离 TeX 注释后的 ``\\begin``，
#: 注释内的假环境不计数；未配对 ``\\end`` 的残块同样计入（源侧宁多报不漏报，
#: 由验收侧人工核对合法置换）。
RE_ASSET_TIKZ = re.compile(r"\\begin\{(?:tikzcd|tikzpicture)\}")


class SourceAssetError(RuntimeError):
    """源资产计数失败（manifest 损坏 / 源文件不可读）——fail closed。

    携带 ``code``（错误码）与 ``detail``（人类可读详情），由调用方转
    HALTED，绝不静默跳过（自报保真度等于没有保真度）。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def _unavailable_source_assets() -> Dict[str, Any]:
    """manifest 缺失时的保守返回（测试夹具 / 异常早退场景）。

    ``available=False`` 显式声明「无源侧对账基线」，绝不伪造 0 计数冒充
    「源侧确实没有图形/表格」。
    """
    return {"figure": 0, "table": 0, "tikz": 0, "available": False}


def _load_check_fidelity_module():
    """惰性导入 scripts/check_fidelity.py（复用资产计数正则口径）。

    与 mapping_nodes._load_inventory_module 同一模式：把插件根与 scripts/
    加入 sys.path 后 import，避免 workflow 包对 scripts 包的硬依赖。
    资产计数口径必须与 check_fidelity 第 8 项资产守恒完全一致，不得另写
    一套正则（否则派发单基数与门禁基数对不上，对账形同虚设）。
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for path in (root, os.path.join(root, "scripts")):
        if path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module("scripts.check_fidelity")


def _request_scopes_module():
    """惰性导入 scripts/request_scopes.py（C5 多 scope 唯一正规化口径）。

    与 ``_load_check_fidelity_module`` 同一模式：写作节点、计划门禁、来源链、
    代理审计四处消费者共用这一份实现——绝不允许各自再写一套 scope 解析
    （否则「谁被授权写 sec2」会出现多个互相矛盾的口径）。
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for path in (root, os.path.join(root, "scripts")):
        if path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module("request_scopes")


def _request_scope_list(req: Dict[str, Any]) -> List[str]:
    """请求覆盖的节列表（C5：多 scope 请求按声明顺序，legacy 请求退化为单节）。

    唯一口径来自 ``scripts/request_scopes.request_scopes``；导入失败或声明为空时
    回落到 ``req["scope"]``（legacy 单节），绝不猜测、绝不去重打乱文档顺序。
    """
    try:
        module = _request_scopes_module()
        scopes = [str(sec).strip() for sec in module.request_scopes(req)
                  if str(sec).strip()]
    except Exception:  # noqa: BLE001 - 解析器不可用时不阻断主流程，退化为单节
        scopes = []
    if scopes:
        return scopes
    scope = str(req.get("scope") or "").strip()
    return [scope] if scope else []


def _scope_input_digest(req: Dict[str, Any], scope: str) -> Optional[str]:
    """单节源码摘要（C5 ``scope_inputs``）；缺失时回落到请求级 ``input_digest``。"""
    inputs = req.get("scope_inputs")
    if isinstance(inputs, dict):
        digest = str(inputs.get(scope) or "").strip()
        if digest:
            return digest
    digest = str(req.get("input_digest") or "").strip()
    return digest or None


def _scope_artifacts(req: Dict[str, Any], scope: str) -> List[str]:
    """单节产物清单（C5 ``artifact_scopes`` 归属）；缺失时回落到 ``<scope>.tex``。"""
    try:
        module = _request_scopes_module()
        artifacts = [str(name) for name in module.artifacts_of_scope(req, scope) if name]
    except Exception:  # noqa: BLE001
        artifacts = []
    if artifacts:
        return artifacts
    return list(req.get("artifacts") or [f"{scope}.tex"])


#: 缓存键口径版本。改动键口径必须 +1（旧条目自然失效）：缓存绝不能因为
#: 「格式悄悄变了但键没变」而复用到旧语义的产物。
CACHE_KEY_TOOL_VERSION = "cache-key-v2"

#: 宏/模板语义集：变化会改变数学含义（宏定义与选项置换约定、切片模板、
#: KaTeX 转换规则与渲染 runtime 配置）→ 内容审读与作者缓存都必须失效。
MACRO_SEMANTIC_RELS = (
    "header.tex",
    "sec_template.tex",
    "html/math-config.json",
    "html/deck.template.html",
)

#: 纯渲染/版面集：变化只需重跑机械交付检查并重绑本轮实测指纹，不重写正文。
RENDER_PIPELINE_RELS = (
    "footer.tex",
    "config.tex",
    "student.tex",
    "teacher.tex",
    "html/katex/katex.min.css",
)

#: 缓存复用台账（append-only）：命中/失效的可复核证据，供 check_agent_audit
#: 与历史派发单/回执双向对账（缓存命中不得冒充一次新的 agent 调用）。
CACHE_REUSE_LEDGER_REL = ".pd/artifact-cache/reuse-ledger.json"


def _rel_digest_bundle(self_dir: str, rels) -> str:
    """按序拼接一组模板资产的摘要；缺失记 ``<rel>:missing``（不静默忽略）。"""
    parts = []
    for rel in rels:
        digest = file_digest(os.path.join(self_dir, str(rel)))
        parts.append("%s:%s" % (str(rel).replace("\\", "/"), digest or "missing"))
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def template_semantic_digest(self_dir: str) -> str:
    """宏/模板**语义**版本（C7：模板语义变化必须让过期证据失效）。"""
    return _rel_digest_bundle(self_dir, MACRO_SEMANTIC_RELS)


def render_pipeline_digest(self_dir: str) -> str:
    """渲染管线版本（纯版面变化：只重跑机械检查，不重写正文）。"""
    return _rel_digest_bundle(self_dir, RENDER_PIPELINE_RELS)


def _section_digest_context(state: ShrinkCoursewareState, self_dir: str, sec: str,
                            role: str, prompt_ver: str) -> Dict[str, Any]:
    """C7 缓存键的显式维度（经 ``build_request(digest_context=...)`` 进指纹）。

    覆盖：图版本 / 执行 schema / 执行档位 / 策略摘要 / 内容策略 / 角色 /
    提示词版本 / 模板语义版本 / 工具版本，外加**本节返修世代**——返修轮必须
    让该节缓存失效，否则「单节返修」会命中上一轮被打回的产物。

    ``build_request`` 缺省不接收 digest_context（传 None 时逐字节等于旧指纹），
    因此这里给出的维度是显式新增，不会让旧项目的缓存键悄悄变形。
    """
    self_dir = self_dir or "."
    profile = "legacy"
    graph_version = "legacy-v1"
    schema_version: Any = ""
    try:
        profile = execution_policy.resolve_profile(state.get("execution_profile"))
        graph_version = str(state.get("graph_version")
                            or execution_policy.graph_version_for_profile(profile))
        schema_version = execution_policy.EXECUTION_SCHEMA_VERSION
    except Exception:  # noqa: BLE001 - 策略档位解析失败时退化为 legacy 口径
        pass
    counts = state.get("repair_attempts_by_scope") or {}
    try:
        repair_attempts = int(counts.get(sec, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        repair_attempts = 0
    fingerprints = state.get("repair_last_fingerprint_by_scope") or {}
    try:
        repair_fingerprint = str(fingerprints.get(sec) or "")
    except AttributeError:
        repair_fingerprint = ""
    return {
        "graph_version": graph_version,
        "execution_schema_version": schema_version,
        "execution_profile": profile,
        "policy_digest": str(state.get("policy_digest") or ""),
        "content_policy": str(state.get("content_policy") or ""),
        "role": role,
        "prompt_version": prompt_ver,
        "template_semantic_digest": template_semantic_digest(self_dir),
        "tool_version": CACHE_KEY_TOOL_VERSION,
        "repair_attempts": repair_attempts,
        "repair_fingerprint": repair_fingerprint,
        # C7：本节来源文本的内容摘要必须进键——否则「只改 sec2」时 sec2 自己的
        # 单节请求指纹不变，缓存会命中上一版来源产出的旧切片（CACHE-02 违规）。
        "source_digest": hashlib.sha256(
            _section_source_text(state, sec, {}).encode("utf-8")).hexdigest(),
    }


def _cache_spec_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """缓存探针用的派发配置（timeout/retry 不进指纹，但 spec 需要它们）。"""
    # 2026-11-15：默认超时同步 3600s（不进指纹，仅保持口径一致）。
    base: Dict[str, Any] = {"timeout_seconds": 3600, "max_retries": 2}
    if isinstance(cfg, dict):
        base.update(cfg)
    return base


def load_cache_reuse_ledger(self_dir: str) -> Dict[str, Any]:
    """读复用台账；缺失/损坏返回空台账（``corrupt`` 标记供门禁判定）。"""
    path = os.path.join(self_dir or ".", *CACHE_REUSE_LEDGER_REL.split("/"))
    if not os.path.isfile(path):
        return {"schema_version": 1, "entries": [], "corrupt": False}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"schema_version": 1, "entries": [], "corrupt": True}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"schema_version": 1, "entries": [], "corrupt": True}
    return data


def record_cache_event(self_dir: str, event: Dict[str, Any]) -> bool:
    """追加一条缓存事件（命中/失效都记）。失败只返回 False，绝不打断流水线。"""
    try:
        ledger = load_cache_reuse_ledger(self_dir)
        entries = list(ledger.get("entries") or [])
        payload = dict(event)
        payload.pop("entry", None)
        payload.setdefault("recorded_at", datetime.datetime.now(
            datetime.timezone.utc).isoformat())
        entries.append(payload)
        path = os.path.join(self_dir or ".", *CACHE_REUSE_LEDGER_REL.split("/"))
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        _write_json_atomic(path, {"schema_version": 1, "entries": entries})
        return True
    except (OSError, ValueError, TypeError):
        return False


def section_cache_probe(self_dir: str, state: ShrinkCoursewareState, sec: str,
                        cfg: Optional[Dict[str, Any]] = None,
                        source_assets: Optional[Dict[str, Any]] = None,
                        blocklist: Optional[List[str]] = None,
                        record: bool = True) -> Dict[str, Any]:
    """逐节缓存探针：命中与失效都给可复核结论（C7/CACHE-04）。

    返回 ``{scope, stage, cache_key, input_digest, artifacts, hit, reason,
    entry, restored, source_assets_available}``；``reason`` 是中文失效原因，
    直接进台账与 history——「算 miss 并给出原因」不允许只留一个 None。

    源资产清单不可用时**仍然给出可验证键**（用 ``_unavailable_source_assets()``
    的显式 ``available=False`` 口径参与指纹），并在台账标注
    ``source_assets_available=False``：宁可让门禁看见「这轮没有资产基线」，
    也不能像卡13 那样静默写不进缓存让人以为命中率低是正常的。
    """
    self_dir = self_dir or state.get("self_dir") or "."
    assets = (dict(source_assets)
              if isinstance(source_assets, dict) and source_assets
              else _unavailable_source_assets())
    assets_available = bool(assets.get("available"))
    spec = _section_request_spec(state, sec, _cache_spec_cfg(cfg), assets)
    request = build_request(state, **spec)
    digest = str(request.get("input_digest") or "")
    artifacts = [str(rel) for rel in (request.get("artifacts") or [])]
    prompt_ver = prompt_version(self_dir, str(spec.get("prompt_file") or "subagent_prompt.md"))
    cfg_digest = config_digest(self_dir)
    stage = str(spec.get("stage") or "")
    result: Dict[str, Any] = {
        "scope": sec,
        "stage": stage,
        "cache_key": cache_key(stage=stage, scope=sec, input_digest=digest,
                               config_digest=cfg_digest, prompt_version=prompt_ver),
        "input_digest": digest,
        "artifacts": artifacts,
        "hit": False,
        "reason": "",
        "entry": None,
        "restored": False,
        "source_assets_available": assets_available,
    }
    blocked = [str(item) for item in (
        blocklist if blocklist is not None
        else (state.get("dispatch_blocklist") or [])) if item]
    if digest and digest in blocked:
        result["reason"] = "本轮已拉黑该摘要（%s…，上次校验失败）" % digest[:12]
        if record:
            record_cache_event(self_dir, result)
        return result
    entry, reason = ArtifactCache(self_dir).lookup_with_reason(
        stage=stage, scope=sec, input_digest=digest,
        config_digest=cfg_digest, prompt_version=prompt_ver,
        artifacts=artifacts)
    if entry is None:
        result["reason"] = reason or "缓存未命中（原因未知）"
    else:
        result["hit"] = True
        result["entry"] = entry
    if record:
        record_cache_event(self_dir, result)
    return result


def store_section_cache(self_dir: str, state: ShrinkCoursewareState, sec: str,
                        cfg: Optional[Dict[str, Any]] = None,
                        source_assets: Optional[Dict[str, Any]] = None,
                        artifacts: Optional[List[str]] = None,
                        stage: Optional[str] = None,
                        input_digest: Optional[str] = None,
                        record: bool = True) -> Dict[str, Any]:
    """把一节本轮的真实产出写入派生物缓存（键与探针同源）。

    ``stage`` / ``input_digest`` 允许调用方直接给出（多 scope 请求用请求里的
    单节摘要），其余情况按单节 spec 复算，保证「写进去的键 = 单节重跑会算出的键」。
    """
    self_dir = self_dir or state.get("self_dir") or "."
    assets = (dict(source_assets)
              if isinstance(source_assets, dict) and source_assets
              else _unavailable_source_assets())
    spec = _section_request_spec(state, sec, _cache_spec_cfg(cfg), assets)
    request = build_request(state, **spec)
    digest = str(input_digest or request.get("input_digest") or "")
    stage_name = str(stage or spec.get("stage") or "")
    arts = [str(rel) for rel in
            (artifacts if artifacts is not None else (request.get("artifacts") or []))]
    prompt_ver = prompt_version(self_dir, str(spec.get("prompt_file") or "subagent_prompt.md"))
    cfg_digest = config_digest(self_dir)
    entry = ArtifactCache(self_dir).store(
        stage=stage_name, scope=sec, input_digest=digest,
        config_digest=cfg_digest, prompt_version=prompt_ver, artifacts=arts)
    result = {
        "scope": sec,
        "stage": stage_name,
        "cache_key": str((entry or {}).get("key") or cache_key(
            stage=stage_name, scope=sec, input_digest=digest,
            config_digest=cfg_digest, prompt_version=prompt_ver)),
        "input_digest": digest,
        "artifacts": arts,
        "hit": False,
        "reason": ("写入缓存（本轮真实产出，供后续相同输入复用）" if entry
                   else "未写入缓存（产物缺失或清单越界，拒绝写入半成品）"),
        "stored": bool(entry),
        "source_assets_available": bool(assets.get("available")),
    }
    if record:
        record_cache_event(self_dir, result)
    return result


def _section_probe_digest(
    state: ShrinkCoursewareState,
    sec: str,
    source_assets: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """单节派发单的缓存键（与 plan 节点逐节探针用的 ``input_digest`` 同源）。

    C5 逐节缓存：多 scope 请求收尾时必须按节落缓存条目，且键必须是「单节重跑
    时探针会算出的那个摘要」——否则单节返修永远打不中缓存（写了也白写）。
    这里用与探针完全相同的 ``_section_request_spec`` + ``build_request`` 复算；
    ``timeout_seconds``/``max_retries`` 不进入指纹，因此给定默认值不影响结果。

    任务16（关闭卡13 保守缺口）：源资产计数缺失时**不再返回 None**，而是用
    显式的 ``available=False`` 口径参与指纹。这样得到的键仍然可验证
    （同一 self 项目同一状态必然复现同一键），且台账会记下
    ``source_assets_available=False``，门禁据此判定「本轮无资产基线」。
    """
    assets = (dict(source_assets)
              if isinstance(source_assets, dict) and source_assets
              else _unavailable_source_assets())
    try:
        # timeout_seconds/max_retries 不进入指纹，与探针同源取默认值即可
        # （2026-11-15 起默认 3600s）。
        spec = _section_request_spec(
            state, sec, {"timeout_seconds": 3600, "max_retries": 2}, assets)
        digest = str(build_request(state, **spec).get("input_digest") or "")
    except Exception:  # noqa: BLE001 - 缓存只是加速，算不出键就不写
        return None
    return digest or None


def _source_asset_counts(self_dir: str) -> Dict[str, Any]:
    """统计源侧视觉/结构资产，作为切片派发单的对账基线。

    口径与 scripts/check_fidelity.py 第 8 项资产守恒完全一致：直接复用其
    ``RE_ASSET_FIGURE`` / ``RE_ASSET_TABLE`` 正则与 ``evidence._strip_comments``
    （TeX 注释感知，``\\%`` 转义不误判）。图形类含 ``\\includegraphics`` 与
    tikz 块（tikzcd 同时计入 figure，与 check_fidelity 同口径）；表格类含
    ``tabular`` / ``tabular*``。只统计 manifest 中 role=source-baseline 且
    include=true 的条目（与 check_asset_conservation 的源侧选集一致），路径
    相对 manifest 的 project_root 解析（排除项 role=optional-problem 不计）。

    返回 ``{"figure": int, "table": int, "tikz": int, "available": bool}``：
      - manifest 缺失 → ``available=False`` 的空计数，由调用方记 history
        警告，不阻断派发（夹具/异常早退场景）；
      - manifest 损坏或源文件不可读 → SourceAssetError，由节点 HALT
        （fail closed：没有对账基线就不得假装有）。
    """
    manifest_path = os.path.join(self_dir or ".", SOURCE_MANIFEST_REL)
    if not os.path.isfile(manifest_path):
        return _unavailable_source_assets()
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as f:
            manifest = json.load(f)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceAssetError(
            "BAD_SOURCE_MANIFEST",
            "源清单不是合法 JSON: %s (%s)" % (manifest_path, exc))
    except OSError as exc:
        raise SourceAssetError(
            "SOURCE_MANIFEST_UNREADABLE",
            "源清单不可读: %s (%s)" % (manifest_path, exc))
    if not isinstance(manifest, dict):
        raise SourceAssetError(
            "BAD_SOURCE_MANIFEST", "源清单根节点必须是 JSON 对象")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise SourceAssetError(
            "BAD_SOURCE_MANIFEST",
            "源清单缺少 sources 数组（Phase 0 冻结契约不完整）")
    project_root = manifest.get("project_root") or os.path.dirname(
        os.path.abspath(self_dir or "."))

    fidelity = _load_check_fidelity_module()
    strip_comments = fidelity.evidence._strip_comments
    figure = table = tikz = 0
    for entry in sources:
        if not isinstance(entry, dict):
            raise SourceAssetError(
                "BAD_SOURCE_MANIFEST", "源清单 sources 条目必须是对象")
        if str(entry.get("role") or "").strip() != "source-baseline":
            continue  # 排除项（optional-problem / include=false）不构成对账基线
        if not entry.get("include", True):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel.strip():
            raise SourceAssetError(
                "BAD_SOURCE_MANIFEST", "source-baseline 条目缺少 path 字段")
        full = rel if os.path.isabs(rel) else os.path.join(project_root, rel)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            raise SourceAssetError(
                "SOURCE_UNREADABLE",
                "源文件 %s 不可读，无法做资产对账 (%s)" % (rel, exc))
        stripped = strip_comments(text)
        figure += len(fidelity.RE_ASSET_FIGURE.findall(stripped))
        table += len(fidelity.RE_ASSET_TABLE.findall(stripped))
        tikz += len(RE_ASSET_TIKZ.findall(stripped))
    return {"figure": figure, "table": table, "tikz": tikz, "available": True}


# ---------------------------------------------------------------------------
# 请求构造与磁盘计划
# ---------------------------------------------------------------------------

def _section_request_spec(
    state: ShrinkCoursewareState,
    sec: str,
    cfg: Dict[str, Any],
    source_assets: Dict[str, Any],
) -> Dict[str, Any]:
    """切片派发单的确定性输入（instruction/command/context/role/artifacts）。

    任务5：缓存探针与正式派发共用同一 spec，保证缓存键 ``input_digest``
    一致（探针只读不写、无 pdstate 副作用，仅用于缓存查询）。

    ``source_assets``（复盘报告 §4.3.3 内容审计前置）由调用方一次算好、
    整批逐节复用同一 dict——既保证 ``input_digest`` 确定性（同一计划内
    逐节上下文一致），也避免逐节重复读源文件。缺失时不得伪造：由
    ``_source_asset_counts`` 给出 ``available=False`` 的空计数或直接抛错。
    """
    role_name = "开篇引入专员" if sec == "sec0" else f"{sec} 切片制作专员"
    tex_filename = f"{sec}.tex"
    return {
        "stage": f"T2_{sec.upper()}",
        "role": role_name,
        "prompt_file": "subagent_prompt.md",
        "artifacts": [tex_filename],
        "instruction": (
            f"请按照 subagent_prompt.md 黄金模板撰写 {tex_filename}，"
            f"确保 qid 唯一、环境闭合、单源双输出。"
        ),
        "command": f"python scripts/pdstate.py self/ submit {sec} && "
                   f"python scripts/pdstate.py self/ mark {sec} checked",
        "context": {
            "section_id": sec,
            "user_spec": state.get("user_spec", {}),
            # 源侧资产计数：切片必须全量保留或在 gap 清单中逐项说明
            "source_assets": source_assets,
        },
        "scope": sec,
        "timeout_seconds": cfg["timeout_seconds"],
        "max_retries": cfg["max_retries"],
        "retry_class": "transient",
        # 任务16（C7）：缓存键显式携带图版本/执行档位/策略摘要/角色/提示词版本/
        # 模板语义版本/工具版本/返修世代——「相同输入」必须连执行语义一起相同。
        "digest_context": _section_digest_context(
            state, state.get("self_dir") or ".", sec, role_name,
            prompt_version(state.get("self_dir") or ".", "subagent_prompt.md")),
    }


def _group_request_spec(
    state: ShrinkCoursewareState,
    scopes: List[str],
    cfg: Dict[str, Any],
    source_assets: Dict[str, Any],
) -> Dict[str, Any]:
    """多节合批派发单的确定性输入（C5：一个作者写 1–2 个相邻节）。

    单节（``len(scopes) == 1``）**必须**退回 ``_section_request_spec``，保证
    缓存探针 / 旧单 scope 派发单 / 旧缓存键逐字节不变；只有 >= 2 节时才生成
    合批 spec（``scopes`` / ``scope_inputs`` / ``artifact_scopes``）。

    ``scope_inputs`` 逐节指纹取该节来源切片载荷的摘要；没有可归属载荷时写
    空串（未知就是未知，不伪造摘要）——它进入 ``input_digest``，因此任何一节
    来源变化都会换掉整张合批单的缓存键。
    """
    scopes = [s for s in scopes if isinstance(s, str) and s]
    if len(scopes) <= 1:
        spec = _section_request_spec(state, scopes[0] if scopes else "", cfg,
                                     source_assets)
        return spec
    artifacts = [f"{sec}.tex" for sec in scopes]
    digest_mod = _request_scopes_module()
    scope_inputs = {}
    for sec in scopes:
        text = _section_source_text(state, sec, cfg)
        # 逐节来源指纹：没有可归属载荷时取空串摘要（诚实口径，且与任何真实
        # 载荷的摘要不同），绝不用"缺省不写"掩盖来源来源不明。
        scope_inputs[sec] = hashlib.sha256(
            (text or "").encode("utf-8")).hexdigest()
    return {
        "stage": "T2_" + "_".join(sec.upper() for sec in scopes),
        "role": "%s 切片制作专员（合并撰写）" % "+".join(scopes),
        "prompt_file": "subagent_prompt.md",
        "artifacts": artifacts,
        "instruction": (
            "请按照 subagent_prompt.md 黄金模板依次撰写 %s，每个文件以独立 "
            "`%% FILE: <name>.tex` 行开头、单文件单代码块，确保 qid 唯一、"
            "环境闭合、单源双输出。" % "、".join(artifacts)),
        "command": " && ".join(
            f"python scripts/pdstate.py self/ submit {sec}" for sec in scopes),
        "context": {
            # 首元素仍写入 section_id，旧读取方（单 scope 假设）不会拿到空值
            "section_id": scopes[0],
            "section_ids": list(scopes),
            "user_spec": state.get("user_spec", {}),
            "source_assets": source_assets,
        },
        "scope": scopes[0],
        "scopes": list(scopes),
        "scope_inputs": scope_inputs,
        "artifact_scopes": {rel: sec for rel, sec in zip(artifacts, scopes)},
        "timeout_seconds": cfg["timeout_seconds"],
        "max_retries": cfg["max_retries"],
        "retry_class": "transient",
        # 任务16（C7）：合批单同样携带显式缓存维度；role 取首节口径，逐节
        # 归属仍由 scope_inputs/artifact_scopes 表达（单节返修按节落缓存）。
        "digest_context": _section_digest_context(
            state, state.get("self_dir") or ".", scopes[0],
            "%s 切片制作专员（合并撰写）" % "+".join(scopes),
            prompt_version(state.get("self_dir") or ".", "subagent_prompt.md")),
    }


def _section_source_text(state: ShrinkCoursewareState, sec: str,
                         cfg: Dict[str, Any]) -> str:
    """该节的来源切片载荷（仅用于字符预算与 scope_inputs 指纹）。"""
    scoped = state.get("section_sources")
    if isinstance(scoped, dict):
        text = scoped.get(sec)
        if isinstance(text, str):
            return text
    return ""


def plan_writer_batches(
    sections: List[str],
    cfg: Dict[str, Any],
    *,
    source_chars: Optional[Dict[str, int]] = None,
    prompt_chars: Optional[Any] = None,
) -> List[List[str]]:
    """把待处理节切成作者批次（C5 合批算法）。

    规则（顺序即优先级）：
      1. 保持**原顺序**，只有**相邻**节可以入同一批（一个作者不改跨节逻辑）；
      2. 单批最多 ``writer_batch_max_sections`` 节（缺省 2）；
      3. 单批来源字符合计 <= ``writer_batch_max_source_chars``；
      4. 最终完整 prompt 字符数 <= ``agent_prompt_max_chars``（0 = 不设限）。

    超限的处理只有「拆批」和「明确失败」两种——**绝不截断来源原文**（截断
    会让作者在看不见上下文的情况下瞎写，比失败更危险）。单节独占一批仍然
    超限时抛 ``WriterBatchError(code="WRITER_SOURCE_OVERFLOW" /
    "WRITER_PROMPT_OVERFLOW")``，由上层 HALT 或走结构化分片。

    ``prompt_chars``：``None`` 表示不做 prompt 度量；可传可调用对象
    ``f(scopes) -> int``（上层据此把真实 prompt 文本量喂进来）。
    """
    max_sections = max(1, _coerce_int(
        cfg.get("writer_batch_max_sections"), DEFAULT_WRITER_BATCH_MAX_SECTIONS))
    max_source = max(1, _coerce_int(
        cfg.get("writer_batch_max_source_chars"),
        DEFAULT_WRITER_BATCH_MAX_SOURCE_CHARS))
    max_prompt = _coerce_int(cfg.get("agent_prompt_max_chars"), 0)
    chars = {str(k): max(0, _coerce_int(v, 0))
             for k, v in (source_chars or {}).items()}

    def _over(group: List[str]) -> Optional[str]:
        total = sum(chars.get(sec, 0) for sec in group)
        if total > max_source:
            return "WRITER_SOURCE_OVERFLOW"
        if prompt_chars is not None and max_prompt > 0:
            size = prompt_chars(group) if callable(prompt_chars) else 0
            if _coerce_int(size, 0) > max_prompt:
                return "WRITER_PROMPT_OVERFLOW"
        return None

    groups: List[List[str]] = []
    current: List[str] = []
    for sec in sections:
        if not isinstance(sec, str) or not sec:
            continue
        if not current:
            current = [sec]
            continue
        candidate = current + [sec]
        adjacent = _adjacent(sec, current[-1], sections)
        if (len(candidate) > max_sections or not adjacent
                or _over(candidate) is not None):
            groups.append(current)
            current = [sec]
        else:
            current = candidate
    if current:
        groups.append(current)

    for group in groups:
        code = _over(group) if len(group) == 1 else None
        if code is not None:
            raise WriterBatchError(
                code,
                "单节 %s 独占一批仍超出预算（来源 %d 字符 / prompt 上限 %d），"
                "拆分不能解决问题——请缩小该节来源范围或提高 "
                "writer_batch_max_source_chars / agent_prompt_max_chars，"
                "绝不允许截断来源原文" % (
                    group[0], chars.get(group[0], 0), max_prompt),
                group)
    return groups


def _adjacent(sec: str, prev: str, sections: List[str]) -> bool:
    """``sec`` 是否紧邻 ``prev``（文档顺序相邻，而非"在待办列表里挨着"）。

    相邻必须按**文档顺序**判定：``sec1``/``sec3`` 即使在待处理列表里相邻，
    也可能是中间那节已完成，它们并不共享上下文，合批只会让作者跨界乱写。
    """
    def _split(name: str) -> Tuple[str, Optional[int]]:
        match = re.match(r"^(.*?)(\d+)$", name)
        if not match:
            return name, None
        return match.group(1), int(match.group(2))

    prefix_sec, num_sec = _split(sec)
    prefix_prev, num_prev = _split(prev)
    if prefix_sec != prefix_prev:
        return False
    if num_sec is not None and num_prev is not None:
        return num_sec - num_prev == 1
    # 无数字后缀时退化为"在给定顺序里紧挨着"（唯一可判定的相邻口径）
    try:
        return sections.index(sec) - sections.index(prev) == 1
    except ValueError:
        return False


def parse_writer_response(text: str, scopes: List[str]) -> Dict[str, str]:
    """把作者整批返回切分成 ``{sec: tex_text}``（C5 逐节落盘的输入）。

    * 只认 ``% FILE: <name>.tex`` 边界行；边界必须覆盖每一个 scope 且不得
      出现 scope 之外的文件；
    * 每个边界块以 ``` 收尾；**收起围栏之后、下一个边界之前的非空正文一律
      视为无法归属**（作者把两节内容写在一处时，宁可整批失败也不能猜切分）；
    * 无法安全识别边界（缺边界 / 边界外还有正文 / 重复边界 / 空块）→ 抛
      ``WriterParseError``，整批失败。
    """
    scopes = [s for s in scopes if isinstance(s, str) and s]
    wanted = {sec: f"{sec}.tex" for sec in scopes}
    by_filename = {name: sec for sec, name in wanted.items()}
    matches = list(RE_WRITER_FILE_BLOCK.finditer(text or ""))
    if not matches:
        raise WriterParseError(
            "作者返回里没有任何 `% FILE: <name>.tex` 边界行，无法安全切分逐节产物",
            scopes)
    preamble = (text[:matches[0].start()] or "").strip()
    for line in preamble.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("```"):
            raise WriterParseError(
                "作者返回在第一个文件边界之前还有正文（%r…），无法判定它属于哪一节"
                % stripped[:40], scopes)
    bodies: Dict[str, str] = {}
    unknown: List[str] = []
    for idx, match in enumerate(matches):
        filename = match.group(1).strip().split("/")[-1]
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        after_header = text[match.end():end].splitlines()
        body_lines: List[str] = []
        closed = False
        for line in after_header:
            if not closed and line.strip().startswith("```"):
                closed = True          # 该块的收尾围栏
                continue
            if closed:
                # 块已收口：后面只允许空行与代码围栏（围栏用于开启下一块的包裹），
                # 其它正文无法归属 → 整批失败
                if line.strip() and not line.strip().startswith("```"):
                    raise WriterParseError(
                        "作者返回在 %s 块收尾之后还有无法归属的正文（%r…），"
                        "猜测切分点会把两节内容搅在一起"
                        % (filename, line.strip()[:40]), scopes)
                continue
            body_lines.append(line)
        payload = "\n".join(body_lines).strip("\n")
        sec = by_filename.get(filename)
        if sec is None:
            unknown.append(filename)
            continue
        if sec in bodies:
            raise WriterParseError(
                "作者返回里 %s 出现了多次边界行，无法判定以哪一份为准" % filename,
                scopes)
        bodies[sec] = payload
    if unknown:
        raise WriterParseError(
            "作者返回里出现了本次请求未授权的文件边界: %s"
            % ", ".join(sorted(set(unknown))), scopes)
    out: Dict[str, str] = {}
    empty: List[str] = []
    for sec in scopes:
        if sec not in bodies:
            continue
        body = bodies[sec]
        if not body.strip():
            empty.append(sec)
            continue
        out[sec] = body if body.endswith("\n") else body + "\n"
    if empty:
        raise WriterParseError(
            "作者返回的 %s 块内容为空（不能用空文件冒充已写）"
            % ", ".join(empty), scopes)
    return out


def promote_writer_sections(
    self_dir: str,
    passing: List[str],
    failing: Optional[List[str]] = None,
) -> List[str]:
    """把**已通过校验**的节从暂存区原子提升到正本（C5 逐节提交）。

    事务边界是「单个节」而不是「整个批」：
      * 通过的节逐个 ``os.replace`` 原子落盘（失败重来不会留下半个文件）；
      * 失败的节**保持原状**——它在暂存区里的候选被丢弃，正本不写、不删，
        更不会因为同批另一节失败而被回滚；
      * 提升前若正本已存在同内容文件，仍走 replace（内容一致，语义不变）。

    返回真正提交成功的节（含部分成功：某个 scope 提升失败只影响它自己）。
    """
    staging = os.path.join(self_dir, WRITER_STAGING_DIR_REL)
    committed: List[str] = []
    for sec in (passing or []):
        if not isinstance(sec, str) or not RE_SECTION_NODE.match(sec):
            continue
        src = os.path.join(staging, f"{sec}.tex")
        if not os.path.isfile(src):
            continue
        dst = os.path.join(self_dir, f"{sec}.tex")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            # 单节提交失败不波及其它节（逐节事务的全部意义就在于此）
            continue
        committed.append(sec)
    # 失败节的候选一律丢弃：它们没有通过逐节校验，不得留在暂存区冒充完成
    for sec in (failing or []):
        if not isinstance(sec, str) or not RE_SECTION_NODE.match(sec):
            continue
        stale = os.path.join(staging, f"{sec}.tex")
        try:
            if os.path.isfile(stale):
                os.remove(stale)
        except OSError:
            pass
    return committed


def _build_requests(
    state: ShrinkCoursewareState,
    self_dir: str,
    batch: List[str],
    batch_id: str,
    cfg: Dict[str, Any],
    source_assets: Dict[str, Any],
    *,
    source_chars: Optional[Dict[str, int]] = None,
    prompt_chars: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[List[str]], List[str]]:
    """为批次内每一**批**节建派发单（C5：一个作者写 1–2 个相邻节）。

    返回 ``(requests, groups, history_lines)``：``groups`` 是逐请求覆盖的节
    列表（``requests`` 与之一一对应，长度相等——这就是门禁批量一致性校验
    的事实来源）。单节批次的派发单与改造前逐字段一致（走
    ``_section_request_spec``）；多节批次走 ``_group_request_spec`` 产出 v2
    派发单（``scopes`` / ``scope_inputs`` / ``artifact_scopes``）。

    ``source_chars``：逐节来源字符数（缺省按 ``state["section_sources"]``
    实测），用于 ``writer_batch_max_source_chars`` 预算；超限只拆批，不截断。
    ``prompt_chars``：可选的 prompt 预算度量回调（见 ``plan_writer_batches``）。
    """
    runner = ScriptRunner(self_dir)
    requests: List[Dict[str, Any]] = []
    lines: List[str] = []

    if source_chars is None:
        measured: Dict[str, int] = {}
        for sec in batch:
            measured[sec] = len(_section_source_text(state, sec, cfg))
        source_chars = measured

    groups = plan_writer_batches(
        batch, cfg, source_chars=source_chars, prompt_chars=prompt_chars)

    for group in groups:
        for sec in group:
            code_st, out_st, err_st = _ensure_task_ready_and_start(
                self_dir, sec, runner)
            if code_st == 0:
                lines.append(
                    f"T2 Dispatch: pdstate.py 已为 {sec} 创建独占 attempt 并就绪")
            else:
                lines.append(
                    "T2 Dispatch: pdstate.py start %s 提示: %s"
                    % (sec, (out_st or err_st).strip()))

        req = build_request(
            state,
            batch_id=batch_id,
            **_group_request_spec(state, group, cfg, source_assets),
        )
        # 并行模式下 WorkerPool 的默认执行器会把 success_stage/success_history
        # 透传给 run_dispatch，使其返回的 patch 带上正确的收尾 stage。
        req["request_ref"] = f"{REQUEST_DIR_REL}/{req['dispatch_id']}.json"
        req["success_stage"] = "T2_SECTIONS_STAGED"
        req["success_history"] = (
            "T2: %s 切片已由派发池完成" % "+".join(group))
        write_request_file(self_dir, req)
        lines.append(
            "T2 Dispatch: %s 已纳入批次 %s（派发单: %s）"
            % ("+".join(group), batch_id, req["request_ref"]))
        requests.append(req)
    return requests, groups, lines


def _plan_request_entry(req: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
    """门禁子集（.pd/dispatch-plan.json）里的一条请求投影。

    卡13（C5）：只有**多 scope** 请求才写 ``scopes`` / ``artifact_scopes``；
    单 scope 请求保持 legacy 形状（不带 scopes 键），这样旧计划与旧派发单
    继续走 check_dispatch 的 legacy 口径，不会被误当成 v2 而套用多节声明
    校验（也保证旧基线计划逐字段可比）。
    """
    scope_mod = _request_scopes_module()
    scopes = list(scope_mod.request_scopes(req))
    entry: Dict[str, Any] = {
        "dispatch_id": req.get("dispatch_id"),
        "scope": req.get("scope"),
        "role": req.get("role"),
        "input_digest": req.get("input_digest"),
        "artifacts": list(req.get("artifacts") or []),
        "source_assets": (req.get("context") or {}).get("source_assets"),
        "status": req.get("status", "pending"),
        "batch_id": batch_id,
    }
    if len(scopes) >= 2 or (scopes and req.get("schema_version") == 2):
        entry["scopes"] = scopes
        entry["artifact_scopes"] = dict(scope_mod.artifact_map(req))
    return entry


def _disk_plan(
    batch_id: str,
    batch: List[str],
    requests: List[Dict[str, Any]],
    pool_limit: int,
    effective_limit: Optional[int] = None,
    worker_capabilities: Optional[Dict[str, Any]] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造写入 .pd/dispatch-plan.json 的门禁子集（check_dispatch.py 的输入）。

    任务19（C12/PARCFG-04）：本函数过去把 ``max_concurrency`` 写成
    ``max(pool_limit, len(batch))``，用「本批请求数」冒充并发上限，导致
    check_dispatch 的「请求数 ≤ 并发上限」恒真、真实限流不可审计。现在：

    - ``batch_size``          = 本批排队请求数（可以大于并发槽位）
    - ``max_concurrency``     = 执行并发上限（兼容字段，恒等于执行上限，
                                绝不取 max(请求数, 上限)）
    - ``configured_concurrency`` = 配置原值（0 = 自动）
    - ``effective_concurrency``  = 经 worker 能力夹取后的有效并发
    - ``worker_max_concurrency`` = 已知 worker 能力；宿主通道未知时为 null
                                 （不得用 Python 保守 1 冒充）
    - ``executor_kind``       = python_registry | host_subagent
    - ``concurrency_source`` / ``profile`` / ``dispatch_contract_version`` /
      ``serial_reason``       = 生效来源与可追溯信息

    ``effective_limit``（任务8）：worker 能力声明压薄后的真实并行度
    （= min(档位并发, worker 声明的 max_concurrency)）。``min_parallelism``
    按它承诺——串行 worker（未声明能力）只承诺 1，不把「计划串行」冒充并行
    承诺；声明并行的 worker 才承诺 2（多节批次）。配合 join 节点的实际并发度
    硬门禁（audit_agent.md §6 / P1.4：实际并发度低于计划值必须失败，
    禁止伪装完成）。

    ``worker_capabilities`` 原样落盘：本轮真实并行度与取消语义的审计凭据。

    ``source_assets``（复盘报告 §4.3.3）从请求 context 原样摘入门禁子集：
    check_dispatch 的 R10 逐请求校验其 figure/table/tikz 为非负整数，
    验收 Agent 以此作源侧对账基线。
    """
    limit = effective_limit if effective_limit else pool_limit
    caps = dict(worker_capabilities or {})
    declared = caps.get("max_concurrency")
    known_cap = declared if isinstance(declared, int) and not isinstance(
        declared, bool) and declared >= 1 else None
    cfg = dict(policy or {})
    executor_kind = cfg.get("executor_kind") or (
        "python_registry" if known_cap is not None else "host_subagent")
    return {
        "batch_id": batch_id,
        "stage": "T2",
        "created_at": _iso_now(),
        "dispatch_contract_version": cfg.get("dispatch_contract_version", 1),
        "profile": cfg.get("profile") or DEFAULT_PROFILE,
        "concurrency_source": cfg.get("concurrency_source") or "auto",
        "batch_size": len(requests),
        "max_concurrency": max(1, int(pool_limit)),
        "configured_concurrency": int(
            cfg.get("configured_max_concurrency", pool_limit) or 0),
        "effective_concurrency": max(1, int(limit)),
        "worker_max_concurrency": known_cap,
        "executor_kind": executor_kind,
        "capability_verified": known_cap is not None,
        "serial_reason": cfg.get("serial_reason") or (
            "" if max(1, int(limit)) > 1 else "concurrency=1"),
        "min_parallelism": 1 if len(batch) == 1 else min(2, max(1, limit)),
        "sections": list(batch),
        "required_scope_count": len(batch),
        "worker_capabilities": caps,
        "requests": [
            # C5：门禁按 scopes 并集判定覆盖，scope 只是兼容标签。
            # 单 scope 请求**不写成 scopes 列表**：旧的「一节一请求」派发单/计划
            # 必须保持 legacy 形状（缺 scopes 键），门禁才会继续走旧口径，
            # 不会被误判成 v2 而套用多节声明校验。
            _plan_request_entry(req, batch_id)
            for req in requests
        ],
        # C5：计划级 schema 版本——有任一多 scope 请求即 v2，门禁据此走并集口径
        "schema_version": (
            2 if any(len(_request_scopes_module().request_scopes(req)) >= 2
                     for req in requests) else 1),
    }


# ---------------------------------------------------------------------------
# manual 模式中断载荷（镜像 agent_nodes._interrupt_payload 的键名 + 批次扩展）
# ---------------------------------------------------------------------------

def _batch_interrupt_payload(
    state: ShrinkCoursewareState,
    plan: Dict[str, Any],
    requests: List[Dict[str, Any]],
    batch_id: str,
) -> Dict[str, Any]:
    """manual 模式的中断载荷：一次携带整批请求。

    键名镜像 run_dispatch 的单派发载荷（type/dispatch_id/stage/role/description/
    artifacts/instruction/command/prompt_file/request_refs/before_digests/context），
    批次级字段（dispatch_id/role/instruction/command）为合并值，逐请求细节放在
    ``requests`` 列表里（每项含 dispatch_id/scope/artifacts/instruction 等）。
    """
    artifacts: List[str] = []
    before_digests: Dict[str, Optional[str]] = {}
    for req in requests:
        for art in (req.get("artifacts") or []):
            if art not in artifacts:
                artifacts.append(art)
        for rel, digest in (req.get("before") or {}).items():
            before_digests[rel] = digest

    sections = [req.get("scope") or "" for req in requests]
    instructions = [
        f"[{req.get('scope')}] {req.get('instruction')}" for req in requests
    ]
    commands = [str(req.get("command") or "") for req in requests]
    return {
        "type": "AGENT_DISPATCH",
        "batch_id": batch_id,
        # 兼容只认单派发载荷的消费者：批次级 id 即 batch_id
        "dispatch_id": batch_id,
        "stage": plan.get("stage") or "T2",
        "role": "T2 切片制作批派发（fan-out）",
        "description": (
            f"T2 并行写作批派发：一批 {len(requests)} 个作者请求 / "
            f"{len([s for s in sections if s])} 个小节切片"
            f"（{', '.join(s for s in sections if s)}）。请按各请求的 "
            f"subagent_prompt.md 黄金模板完成全部产物后恢复执行。"
        ),
        "artifacts": artifacts,
        "instruction": "\n\n".join(instructions),
        "command": " && ".join(c for c in commands if c),
        "prompt_file": "prompts/subagent_prompt.md",
        "request_refs": [
            f"{REQUEST_DIR_REL}/{req.get('dispatch_id')}.json" for req in requests
        ],
        "before_digests": before_digests,
        "context": {"sections": sections, "user_spec": state.get("user_spec", {})},
        # 派发协议三件套（q_audit.md 根因治理）：本批必须经 qchain_dispatch
        # 派发宿主子 Agent 完成，严禁主 Agent 单会话直写产物。
        # 卡20（C12.2）：批次入口附可直接执行的 dispatch_args（一次取全批）。
        **agent_evidence.dispatch_protocol_block(
            batch_id=batch_id, self_dir=state.get("self_dir") or "",
        ),
        "max_concurrency": plan.get("max_concurrency"),
        "min_parallelism": plan.get("min_parallelism"),
        # 任务19（C12）：批次计划的可追溯字段（并发槽位 ≠ 排队请求数）
        "batch_size": plan.get("batch_size"),
        "effective_concurrency": plan.get("effective_concurrency"),
        "executor_kind": plan.get("executor_kind"),
        "concurrency_source": plan.get("concurrency_source"),
        "dispatch_contract_version": plan.get("dispatch_contract_version"),
        "worker_max_concurrency": plan.get("worker_max_concurrency"),
        "requests": [
            {
                "dispatch_id": req.get("dispatch_id"),
                "scope": req.get("scope"),
                # 卡13（C5）：合批请求必须让派发方看见它覆盖的每一节，
                # 否则宿主子 Agent 只按 scope 写一节就交差（漏写无感知）。
                "scopes": [s for s in (req.get("scopes") or [req.get("scope")])
                           if s],
                "artifact_scopes": dict(req.get("artifact_scopes") or {}),
                "role": req.get("role"),
                "artifacts": list(req.get("artifacts") or []),
                "instruction": req.get("instruction"),
                "command": req.get("command"),
                "prompt_file": f"prompts/{req.get('prompt_file')}",
                "request_ref": f"{REQUEST_DIR_REL}/{req.get('dispatch_id')}.json",
                "before_digests": req.get("before") or {},
                "input_digest": req.get("input_digest"),
                "timeout_seconds": req.get("timeout_seconds"),
                "max_retries": req.get("max_retries"),
            }
            for req in requests
        ],
    }


# ---------------------------------------------------------------------------
# 磁盘重推导（断点重启 / manual 恢复的统一验证口径）
# ---------------------------------------------------------------------------

def _verify_requests_from_disk(
    self_dir: str,
    requests: List[Dict[str, Any]],
    allow_unattested: Optional[bool] = None,
) -> Tuple[List[str], Dict[str, str], Dict[str, Dict[str, Any]]]:
    """按 before 快照、当前 sha256 与代理证据台账，从磁盘重推导每个请求的结果。

    校验口径与 agent_nodes.run_dispatch 完全一致（q_audit.md 证据链治理）：
        - 任一产物缺失（sha256 为 None）→ missing_artifacts 失败；
        - 全部产物均未变化 → no_change 失败；**A16 例外**：证据台账认证过
          这次派发（子 Agent 跑了、写了盘、内容逐字节相同）时按 ok 收，
          并在 ``entry.no_change_attested_scopes`` 里点名；
        - 部分产物已变化 → 查 .pd/agent-calls/<dispatch_id>.json：
            * 证据吻合 → ok（entries.verified_via="agent"）；
            * 无证据 → ok_unattested：allow_unattested=True 时放行并打
              unattested 标记，否则失败（主线程直写嫌疑，进返修环）；
            * 证据在案但不符 → attested_mismatch，一律失败无逃生阀。

    allow_unattested 缺省时读当前 config.yaml（caller 传 checkpoint 固化的
    dispatch_config 值可避免口径漂移）。

    返回 ``(ok_scopes, failures, result_entries)``，其中
    ``failures`` 为 ``{scope: status}``，``result_entries`` 以 dispatch_id 为键。

    卡13（C5）：认证粒度是**节**，不是请求。一张合批单写 sec1+sec2 时，sec1
    成功、sec2 失败必须各自成立——因此逐 scope 单独取指纹并单独送证据核验，
    entry 里同时保留 ``scope_statuses`` / ``scope_artifacts`` /
    ``artifact_digests``，任何一节都能独立追溯与独立重派。
    """
    ok_scopes: List[str] = []
    failures: Dict[str, str] = {}
    entries: Dict[str, Dict[str, Any]] = {}
    if allow_unattested is None:
        allow_unattested = bool(
            _load_dispatch_config(self_dir).get("allow_unattested", False))
    scope_mod = _request_scopes_module()
    for req in requests:
        dispatch_id = str(req.get("dispatch_id") or "")
        scopes = scope_mod.request_scopes(req) or [str(req.get("scope") or "")]
        artifacts = [a for a in (req.get("artifacts") or []) if isinstance(a, str)]
        before = dict(req.get("before") or {})

        after: Dict[str, Optional[str]] = {}
        for rel in artifacts:
            after[rel] = _sha256_of(self_dir, rel)

        scope_statuses: Dict[str, str] = {}
        scope_artifacts: Dict[str, List[str]] = {}
        digest_by_artifact: Dict[str, Optional[str]] = {}
        entry_error: Optional[str] = None
        unattested_any = False
        no_change_scopes: List[str] = []
        for sec in scopes:
            sec_artifacts = scope_mod.artifacts_of_scope(req, sec) or [
                rel for rel in artifacts
                if str((req.get("artifact_scopes") or {}).get(rel) or sec) == sec]
            if not sec_artifacts:
                sec_artifacts = [f"{sec}.tex"]
            scope_artifacts[sec] = list(sec_artifacts)
            sec_after = {rel: after.get(rel, _sha256_of(self_dir, rel))
                         for rel in sec_artifacts}
            for rel, sha in sec_after.items():
                digest_by_artifact[rel] = sha
                after.setdefault(rel, sha)
            # 逐节投影成"单节请求"再核验：证据台账按 dispatch_id 认领，但摘要
            # 只与该节自己的产物比对——这样某节没写不会连坐已写好的节。
            sec_req = dict(req)
            sec_req["artifacts"] = list(sec_artifacts)
            sec_req["before"] = {rel: before.get(rel) for rel in sec_artifacts}
            status, _evidence = agent_evidence.check_request_attested(
                self_dir, sec_req, sec_after, allow_unattested=allow_unattested)
            # A16：全部产物未变化时先问「这次派发是否被证据台账认证过」。
            # replan 类阶段逐字重写同一份文件是正确结果，认证过就按 ok 收，
            # 不再判 no_change → HALTED（resume 从 HALTED 是 no-op）。
            no_change_attested = False
            if status == "no_change":
                attested, _nc_ev = agent_evidence.no_change_is_attested(
                    self_dir, sec_req, sec_after)
                if attested:
                    no_change_attested = True
                    status = "ok"
                    no_change_scopes.append(sec)
            scope_statuses[sec] = status
            if status == "ok":
                ok_scopes.append(sec)
            elif status == "ok_unattested" and allow_unattested:
                unattested_any = True
                ok_scopes.append(sec)
            else:
                failures[sec] = status
                if entry_error is None:
                    entry_error = _attestation_error_detail(
                        status, sec_artifacts,
                        sec_req["before"], sec_after, sec)

        # entry.scope/status 描述"这条请求的代表节"：优先成功的节（部分成功
        # 时调用方据此保留已通过节），否则取第一个失败节。逐节的真实结论
        # 始终在 scope_statuses / ok_scopes / failures 里，失败节不会被这份
        # 摘要掩盖（卡13 C5：认证粒度是节）。
        primary = next((s for s in scopes if scope_statuses.get(s) == "ok"), None)
        if primary is None:
            primary = next((s for s in scopes
                            if s in (failures or {})), scopes[0] if scopes else "")
        entry: Dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "scope": primary,
            "status": scope_statuses.get(primary, "no_change"),
            "attempts": 1,
            "error": entry_error,
            "verified_from": "disk",
            # 证据链口径：agent=子 Agent 证据吻合；unattested=放行但无证据；
            # n/a=缺失/空转/证据不符（均判失败）
            "verified_via": (
                "agent" if scope_statuses.get(primary) == "ok"
                else "unattested" if scope_statuses.get(primary) == "ok_unattested"
                else "n/a"
            ),
            # C5 逐节可追溯字段
            "scope_statuses": scope_statuses,
            "scope_artifacts": scope_artifacts,
            # 本请求里真正通过的节（entry.status 可能因某一节失败而是失败态，
            # 消费方要看"哪些节还能保住"时读这里）
            "ok_scopes": [
                s for s in scopes
                if scope_statuses.get(s) == "ok"
                or (scope_statuses.get(s) == "ok_unattested" and allow_unattested)
            ],
            # 只登记真实存在的产物摘要（缺失件在 scope_statuses 里是
            # missing_artifacts；不在这里写 None 冒充"已算出摘要"）
            "artifact_digests": {rel: sha for rel, sha in digest_by_artifact.items()
                                 if sha is not None},
        }
        if unattested_any and not entry_error:
            # 逃生阀放行：保留 unattested 标记，交付门禁 check_agent_audit 终裁
            entry["unattested"] = True
        if no_change_scopes:
            # A16：这些节的「无变化」是经证据台账认证的合法空转（子 Agent
            # 跑了、写了盘、内容逐字节相同），不是主线程留存的陈旧产物。
            entry["no_change_attested_scopes"] = no_change_scopes
        entries[dispatch_id] = entry
    return ok_scopes, failures, entries


def _attestation_error_detail(
    status: str,
    artifacts: List[str],
    before: Dict[str, Optional[str]],
    after: Dict[str, Optional[str]],
    scope: str,
) -> str:
    """磁盘重推导失败条目的人类可读详情（按状态码分流）。"""
    if status == "missing_artifacts":
        missing = [rel for rel in artifacts if after.get(rel) is None]
        if not artifacts:
            return "派发请求未声明产物"
        return f"产物未生成: {', '.join(missing)}"
    if status == "no_change":
        unchanged = [
            rel for rel in artifacts
            if after.get(rel) is not None and after.get(rel) == before.get(rel)
        ]
        return f"产物未发生变化: {', '.join(unchanged) or ', '.join(artifacts)}"
    if status == "ok_unattested":
        return (f"{scope} 产物无代理证据（main-thread 直写嫌疑），进入返修环"
                f"（config.yaml agent_dispatch.allow_unattested 可作逃生阀）")
    if status == "attested_mismatch":
        return (f"{scope} 产物与代理证据不符（证据在案但摘要对不上，"
                f"疑似证据落盘后产物被改写）")
    return status


def _plan_allow_unattested(plan: Dict[str, Any], self_dir: str) -> bool:
    """恢复路径的放行口径：优先取 checkpoint 固化的 dispatch_config（避免
    规划后改配置造成口径漂移）；旧 checkpoint 缺该键时回落当前配置。"""
    value = (plan.get("dispatch_config") or {}).get("allow_unattested")
    if isinstance(value, bool):
        return value
    return bool(_load_dispatch_config(self_dir).get("allow_unattested", False))


def _sha256_of(self_dir: str, rel: str) -> Optional[str]:
    """self_dir 下相对文件的 sha256（缺失/不可读返回 None）。"""
    return sha256_file(os.path.join(self_dir, rel.replace("/", os.sep)))


def _existing_sections(self_dir: str,
                       expected_sections: List[str],
                       statuses: Dict[str, str]) -> List[str]:
    """已存在的节 = 磁盘上已有 <sec>.tex，或状态已进入 staged/checked/accepted。

    edit 模式的工作集白名单（audit §17 任务2「edit 只允许已存在」）。
    """
    existing = []
    for sec in expected_sections:
        if statuses.get(sec) in ("staged", "checked", "accepted"):
            existing.append(sec)
        elif os.path.isfile(os.path.join(self_dir, f"{sec}.tex")):
            existing.append(sec)
    return existing


def _work_set(state: ShrinkCoursewareState,
              self_dir: str,
              expected_sections: List[str],
              statuses: Dict[str, str]) -> Tuple[List[str], Dict[str, Any]]:
    """任务3：解析本次运行的工作集（run_scope 选择）。

    优先级：state（Phase 0 规范化）> config.yaml（init_nodes 持久化）。
    返回 (selected_sections, work_set_info)；非法 run_scope / 目标不存在 /
    目标为空一律抛 ScopeSelectionError（调用方转 HALTED，不静默回退 full）。
    """
    run_scope = str(state.get("run_scope") or "").strip()
    target_sections = state.get("target_sections")
    target_qids = state.get("target_qids")
    if not run_scope or target_sections is None or target_qids is None:
        cfg = _load_dispatch_config(self_dir)
        run_scope = run_scope or cfg.get("run_scope", "")
        if target_sections is None:
            target_sections = cfg.get("target_sections", [])
        if target_qids is None:
            target_qids = cfg.get("target_qids", [])
    # 未配置（缺失/空串）→ full：保持既有全量派发默认行为；
    # 已配置但非法 → BAD_RUN_SCOPE 硬拒绝（不静默回退 full）
    run_scope = run_scope or "full"

    selected = select_scopes(
        run_scope,
        normalize_targets(target_sections),
        normalize_targets(target_qids),
        expected_sections,
        existing_sections=_existing_sections(self_dir, expected_sections, statuses),
    )
    info = {
        "run_scope": run_scope,
        "selected": list(selected),
        "target_sections": normalize_targets(target_sections),
        "target_qids": normalize_targets(target_qids),
    }
    return selected, info


# ---------------------------------------------------------------------------
# 节点 1：主控调度（三岔）
# ---------------------------------------------------------------------------

def node_t2_plan_dispatch(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2 fan-out 主控调度节点：ADVANCE / DONE / PLAN 三岔。

    (a) ADVANCE：已有 staged（已写未提交）切片 → 交给既有串行提交节点处理，
        active_section = expected 顺序首个 staged，不发起新派发；
    (b) DONE：无 pending 节 → active_section = None，PROCEED；
    (c) PLAN：按档位配置建批（依赖表拓扑序 → 批大小 → 派发单 → pdstate 起跑），
        写 .pd/dispatch-plan.json 并跑 check_dispatch 硬门禁；门禁不过即 HALTED
        （契约违规是硬停止，不静默修复）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = list(state.get("expected_sections") or [])
    statuses = dict(state.get("section_statuses", {}))
    history = list(state.get("history", []))

    # ---- (a) ADVANCE：已有暂存切片，转串行提交 --------------------------
    for sec in expected_sections:
        if statuses.get(sec) == "staged":
            history.append(
                f"T2 Dispatch: 发现已暂存切片 {sec}，转入串行提交（不重复派发）")
            return {
                "active_section": sec,
                "stage": "T2_SECTIONS_STAGED",
                "routing_action": "PROCEED",
                "history": history,
            }

    # ---- (b) DONE：没有待写切片 ---------------------------------------
    pending = [
        sec for sec in expected_sections
        if statuses.get(sec, "planned") not in NON_PENDING_STATUSES
    ]
    if not pending:
        history.append("T2 Dispatch: 全部切片均已 checked/accepted，无需再派发")
        return {
            "active_section": None,
            "stage": "T2_SECTIONS_STAGED",
            "routing_action": "PROCEED",
            "history": history,
        }

    # ---- (c) PLAN：规划并派发下一批 ------------------------------------
    # 任务3 工作集先行：_work_set 自带 config.yaml 回落解析，据此推导缺省
    # 派发档位（audit §12.1：完整生成 fast、交互微调 interactive）。
    try:
        selected, work_set = _work_set(state, self_dir, expected_sections, statuses)
    except ScopeSelectionError as exc:
        history.append(f"T2 Dispatch: 工作集选择被拒绝 [{exc.code}] {exc}")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T2 工作集选择失败 [%s]: %s" % (exc.code, exc),
            "history": history,
        }
    cfg = _load_dispatch_config(self_dir, work_set["run_scope"])
    # 任务19（C12）：配置致命错误（未知契约版本 / 并发值非法 / 嵌套与扁平冲突）
    # 必须带真实错误码 HALT——绝不带着猜出来的并发继续派发。
    if cfg.get("dispatch_error"):
        err = cfg["dispatch_error"]
        history.append(
            "T2 Dispatch: 派发契约被拒绝 [%s] %s"
            % (err.get("code"), err.get("detail")))
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T2 派发契约非法 [%s]: %s"
                             % (err.get("code"), err.get("detail")),
            "history": history,
        }

    # 复盘报告 §4.3.3 内容审计前置：源侧资产计数一次算好，整批逐节复用
    # （缓存探针与正式派发共用，保证 input_digest 确定性）。
    try:
        source_assets = _source_asset_counts(self_dir)
    except Exception as exc:  # fail closed：计数失败不得静默下发无基线派发单
        code = getattr(exc, "code", "SOURCE_ASSET_ERROR")
        detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
        history.append(f"T2 Dispatch: 源资产计数失败 [{code}] {detail}")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"T2 源资产计数失败 [{code}]: {detail}",
            "history": history,
        }
    if not source_assets.get("available"):
        history.append(
            "T2 Dispatch: 源资产清单不可用（.pd/source-manifest.json 缺失），"
            "切片派发单以 available=false 空计数下发（不阻断派发，验收侧无源侧基线）")

    if work_set["run_scope"] != "full":
        selected_set = set(selected)
        unselected = [sec for sec in pending if sec not in selected_set]
        for sec in unselected:
            if os.path.isfile(os.path.join(self_dir, f"{sec}.tex")):
                statuses[sec] = "reused"
            else:
                statuses[sec] = "skipped"
        if unselected:
            history.append(
                "T2 Dispatch: 工作集 run_scope=%s — 选中 %s；未选中 %s（标记 %s，不重复派发）"
                % (work_set["run_scope"], ",".join(selected) or "-",
                   ",".join(unselected),
                   "/".join(sorted({statuses[s] for s in unselected}))))
        pending = [sec for sec in pending if sec in selected_set]
        if not pending:
            history.append(
                "T2 Dispatch: 工作集内全部切片均已完成/复用/跳过，无需再派发")
            return {
                "active_section": None,
                "section_statuses": statuses,
                "stage": "T2_SECTIONS_STAGED",
                "routing_action": "PROCEED",
                "history": history,
            }

    ordered = _order_pending_sections(self_dir, pending, expected_sections)

    # 任务5（audit §17 任务5 / §12.3）：派生物缓存探针。
    # 命中（input_digest + config.yaml + prompt_version 全同，且缓存副本
    # digest 逐一校验通过）→ 还原切片候选并标记 staged，走既有 submit/pdstate
    # 纳管（不用 reused：缓存命中仍须提交校验，不得跳过一致性检查），
    # 且不占用批次窗口与池并发——相同输入重跑第二次 T2 worker submit 为 0。
    _cache = ArtifactCache(self_dir)
    _blocklist = [str(item) for item in (state.get("dispatch_blocklist") or []) if item]
    restored: List[str] = []
    cache_misses: Dict[str, str] = {}
    for sec in list(ordered):
        # 任务16（C7）：命中与失效都必须留下可复核结论——命中写复用台账（可与
        # 历史派发单/回执双向对上），失效写原因（损坏/缺副本/键不符/已拉黑）。
        probe = section_cache_probe(self_dir, state, sec, cfg, source_assets,
                                    blocklist=_blocklist, record=False)
        if probe["hit"] and probe.get("entry") is not None and _cache.restore(probe["entry"]):
            statuses[sec] = "staged"
            restored.append(sec)
            record_cache_event(
                self_dir,
                dict(probe, restored=True,
                     reason="缓存命中（复用历史派发产物，不新增 agent 调用）"))
            continue
        reason = ("缓存副本还原失败（写入 self 失败）" if probe["hit"]
                  else probe["reason"])
        cache_misses[sec] = reason
        record_cache_event(self_dir, dict(probe, hit=False, reason=reason))
    if restored:
        history.append(
            "T2 Dispatch: 派生物缓存命中 %s（input_digest/config/prompt 未变，"
            "还原自缓存并转入提交，不重复派发）" % ",".join(restored))
    if cache_misses:
        history.append(
            "T2 Dispatch: 缓存未命中 %s"
            % "；".join("%s=%s" % (sec, reason)
                        for sec, reason in cache_misses.items()))
    remaining = [sec for sec in ordered if sec not in set(restored)]
    if not remaining:
        first_staged = next(
            (sec for sec in expected_sections if statuses.get(sec) == "staged"),
            None)
        history.append("T2 Dispatch: 待派发节全部命中派生物缓存，直接转入串行提交")
        return {
            "active_section": first_staged,
            "section_statuses": statuses,
            "stage": "T2_SECTIONS_STAGED",
            "routing_action": "PROCEED",
            "dispatch_metrics": {
                "submitted": 0, "completed": 0, "ok": 0, "failed": 0,
                "retried": 0, "max_in_flight": 0, "deferred": 0,
                "reused": len(restored),
            },
            "history": history,
        }

    batch_size = min(cfg["max_concurrency"] * cfg["batch_window"], len(remaining))
    batch = remaining[:batch_size]
    batch_id = f"bat-t2-{uuid.uuid4().hex[:8]}"

    # 建池先于派发计划落盘：真实并行度受 worker 能力声明压薄
    # （任务8，= min(档位并发, 声明的 max_concurrency)），计划须按真实
    # 并行度承诺 min_parallelism，不得把「计划并发」冒充执行能力。
    _pool = _get_pool(
        batch_id, cfg["max_concurrency"], cfg["timeout_seconds"], cfg["max_retries"])
    _effective_limit = _pool.effective_concurrency if _pool is not None else None
    _worker_caps = _pool.worker_capabilities if _pool is not None else {}

    history.append(
        "T2 Dispatch: 规划批次 %s（%d/%d 节，档位 %s 并发 %d，窗口 %d，超时 %ds，重试 %d）"
        % (batch_id, len(batch), len(remaining), cfg["profile"],
           cfg["max_concurrency"], cfg["batch_window"],
           cfg["timeout_seconds"], cfg["max_retries"])
    )
    if cfg.get("diagnostics"):
        history.append(
            "T2 Dispatch: 派发配置诊断 %s"
            % ",".join(str(code) for code in cfg["diagnostics"]))

    # 卡13（C5）：_build_requests 现在按「一个作者写 1–2 个相邻节」合批，返回
    # (requests, groups, history_lines)。groups 与 requests 一一对应，是门禁
    # 「请求数 = 声明批次」的事实来源。
    try:
        requests, writer_groups, build_lines = _build_requests(
            state, self_dir, batch, batch_id, cfg, source_assets)
    except WriterBatchError as exc:
        # 单节就超预算时精确失败：绝不截断原文凑进一次请求
        detail = exc.detail or str(exc)
        history.append(
            "T2 Dispatch: 作者合批预算无法满足 [%s] %s" % (exc.code, detail))
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "T2 作者合批预算无法满足 [%s]: %s"
                             % (exc.code, detail),
            "history": history,
        }
    history.extend(build_lines)
    history.append(
        "T2 Dispatch: 本批 %d 节合批为 %d 个作者请求（%s）"
        % (len(batch), len(requests),
           " / ".join("+".join(group) for group in writer_groups)))

    plan_disk = _disk_plan(
        batch_id, batch, requests, cfg["max_concurrency"],
        effective_limit=_effective_limit, worker_capabilities=_worker_caps,
        policy=cfg)
    # 任务3：工作集留痕（focused/edit 的选中/复用/跳过集合，供度量与审计）
    plan_disk["work_set"] = work_set
    _write_json_atomic(os.path.join(self_dir, DISPATCH_PLAN_REL), plan_disk)

    runner = ScriptRunner(self_dir)
    report = runner.run_gate_script(
        gate_name="check_dispatch",
        script_name="check_dispatch.py",
        args=[self_dir],
        report_rel_path=DISPATCH_REPORT_REL,
        run_id=state.get("run_id", ""),
    )
    history.append(
        "T2 Dispatch: check_dispatch 门禁状态 %s（退出 %s，报告 %s）"
        % (report.get("status"), report.get("exit_code"), report.get("report_path"))
    )
    if report.get("status") != "PASS":
        # 契约违规 = 硬停止：不静默修复、不降级放行
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"派发门禁未通过: {_gate_error_codes(report)}",
            "history": history,
        }

    # 卡20（C12.2）：门禁通过后写批次索引（.pd/dispatch-batches/<batch_id>.json），
    # 供 index.js 的 batch_id 解析一次取全本批请求并核对 run/stage/请求集合/计划摘要。
    # 放在门禁 PASS 之后，避免 HALT 时留下孤儿索引。
    try:
        agent_evidence.write_batch_index(
            self_dir,
            batch_id=batch_id,
            run_id=state.get("run_id", ""),
            stage="T2",
            request_ids=[req.get("dispatch_id") for req in requests],
            plan_rel=DISPATCH_PLAN_REL.replace("\\", "/"),
            plan_digest=sha256_file(os.path.join(self_dir, DISPATCH_PLAN_REL)),
            sections=list(batch),
            required_scope_count=plan_disk.get("required_scope_count"),
            dispatch_contract_version=plan_disk.get("dispatch_contract_version"),
        )
        history.append(f"T2 Dispatch: 已写批次索引（batch_id={batch_id}）")
    except ValueError as exc:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"批次索引写入失败: {exc}",
            "history": history,
        }

    # 归档上一轮，重置本轮度量
    dispatch_history = list(state.get("dispatch_history") or [])
    previous_plan = state.get("dispatch_plan") or {}
    if previous_plan.get("batch_id"):
        dispatch_history.append({
            "batch_id": previous_plan.get("batch_id"),
            "sections": list(previous_plan.get("sections") or []),
            "metrics": dict(state.get("dispatch_metrics") or {}),
        })
    dispatch_metrics = {
        "submitted": len(batch),
        "completed": 0,
        "ok": 0,
        "failed": 0,
        "retried": 0,
        "max_in_flight": 0,
        "deferred": 0,
        "reused": len(restored),
    }

    # state 保留完整派发单（requests_full）；磁盘只保留门禁子集
    state_plan = dict(plan_disk)
    state_plan["requests_full"] = requests
    state_plan["dispatch_config"] = cfg

    statuses.update({sec: "running" for sec in batch})
    history.append(
        f"T2 Dispatch: 批次 {batch_id} 门禁通过，{len(batch)} 个切片进入并行派发")

    return {
        "dispatch_plan": state_plan,
        "batch_id": batch_id,
        "section_statuses": statuses,
        "active_section": None,
        "pending_dispatch": None,
        "stage": "T2_SECTIONS_PENDING",
        "routing_action": "PROCEED",
        "dispatch_metrics": dispatch_metrics,
        "dispatch_history": dispatch_history,
        "agent_dispatch_profile": cfg["profile"],
        "retry_count": 0,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 2：批量派发
# ---------------------------------------------------------------------------

def node_t2_dispatch_sections(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2 fan-out 派发节点：整批提交到进程级 WorkerPool。

    - plan/batch_id 不一致（stale plan）→ HALTED；
    - 有认知 worker → 真线程并行；无 worker → 全部请求立即记 deferred；
    - 只根据 pool.stats()["deferred"] 判定 dispatch_mode
      （"manual" / "parallel"），不在此等待真实工作收尾；
    - 池对象绝不写入返回 patch（不可 JSON 序列化）。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")
    history = list(state.get("history", []))

    if not plan or plan.get("batch_id") != batch_id:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划与当前批次不符（stale plan）",
            "history": history,
        }

    requests = list(plan.get("requests_full") or [])
    if not requests:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    cfg = plan.get("dispatch_config") or _load_dispatch_config(self_dir)
    pool = _get_pool(
        batch_id,
        cfg["max_concurrency"],
        cfg["timeout_seconds"],
        cfg["max_retries"],
    )
    for req in requests:
        pool.submit(req)

    # 任务4：dispatch 节点只做校验/提交/记录，绝不调 join——
    # join 是 join 节点（fan-in）的专属职责，每 batch 只允许一次。
    # 旧实现在此 join(timeout=最长请求超时+5s≈905s) 会阻塞到全部收尾，
    # 使 parallel 模式退化为串行等待且近 15 分钟无阶段反馈（q_audit §12.2）。
    # deferred 计数在 submit 时即已确定（无执行器的请求不起线程），
    # 因此判定 dispatch_mode 无需等待。
    stats = pool.stats()
    deferred = int(stats.get("deferred", 0))
    mode = "manual" if deferred > 0 else "parallel"
    history.append(
        "T2 Dispatch: 批次 %s 已提交 %d 个请求（模式 %s，deferred=%d）"
        % (batch_id, len(requests), mode, deferred)
    )

    return {
        "stage": "T2_SECTIONS_RUNNING",
        "dispatch_mode": mode,
        "dispatch_metrics": stats,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 节点 3：汇总回归
# ---------------------------------------------------------------------------

def node_t2_join_sections(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2 fan-out 汇总节点：并行结果 / manual 中断恢复 / 磁盘重推导。

    三种入口：
        1. PARALLEL：本进程有活池且全部请求已有真实结果 → 按 DispatchResult 汇总；
        2. MANUAL（未中断）：无活池/deferred → interrupt 一次携带整批，返回
           manual_interrupted_batch 后由图级回边再次进入本节点；
        3. MANUAL（已中断 / 进程重启）：按 before 快照与当前 sha256 从磁盘重推导，
           同一 batch_id 绝不二次 interrupt。

    硬门禁：parallel 模式实际并发度（max_in_flight）低于计划 min_parallelism
     时 HALTED（禁止伪装完成，audit_agent.md §6 / P1.4；manual 路径已由
     dispatch_mode="manual" 标记，豁免该校验）。
     收尾：任一失败 → REPAIR_SECTION（repair_target = expected 顺序首个失败节）； → REPAIR_SECTION（repair_target = expected 顺序首个失败节）；
    全部 ok → active_section = expected 顺序首个 staged，PROCEED。
    幂等：dispatch_results 以 dispatch_id 为键，重复调用不重复 interrupt、不重复结果。
    """
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = list(state.get("expected_sections") or [])
    statuses = dict(state.get("section_statuses", {}))
    staged = dict(state.get("staged_sections", {}))
    results = dict(state.get("dispatch_results") or {})
    history = list(state.get("history", []))
    plan = state.get("dispatch_plan") or {}
    batch_id = state.get("batch_id")
    requests = list(plan.get("requests_full") or [])

    if not plan or plan.get("batch_id") != batch_id:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划与当前批次不符（stale plan）",
            "history": history,
        }
    if not requests:
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": "派发计划缺少完整派发单（requests_full 为空）",
            "history": history,
        }

    pool = _live_pool(batch_id)
    join_timeout = _join_timeout(requests)
    raw_results: Dict[str, Any] = {}
    stats: Dict[str, int] = {}
    if pool is not None:
        raw_results = pool.join(timeout=join_timeout)
        stats = pool.stats()
        deferred = int(stats.get("deferred", 0))
        has_all = len(raw_results) >= len(requests)
    else:
        deferred = 1
        has_all = False

    ok_scopes: List[str] = []
    failures: Dict[str, str] = {}

    parallel_mode = pool is not None and deferred == 0 and has_all
    if parallel_mode:
        # ---- 1) PARALLEL：活池真实结果 --------------------------------
        # C5 逐节事务：一个作者请求可覆盖 1–2 节，结果的接受/失败必须按节结算，
        # 因此 ok/失败都展开到该请求的每一个 scope 上（单节请求行为不变）。
        for req in requests:
            dispatch_id = str(req.get("dispatch_id") or "")
            scope = str(req.get("scope") or "")
            req_scopes = _request_scope_list(req) or ([scope] if scope else [])
            res = raw_results.get(dispatch_id)
            if res is None:
                for sec in req_scopes or [scope]:
                    failures[sec] = "missing_result"
                results[dispatch_id] = {
                    "dispatch_id": dispatch_id,
                    "scope": scope,
                    "scopes": list(req_scopes),
                    "status": "missing_result",
                    "attempts": 0,
                    "error": "派发池未返回该请求的结果",
                }
                continue
            status = str(_result_field(res, "status", "unknown") or "unknown")
            entry: Dict[str, Any] = {
                "dispatch_id": dispatch_id,
                "batch_id": batch_id,
                "scope": scope,
                "scopes": list(req_scopes),
                "status": status,
                "attempts": _result_field(res, "attempts", 0),
                "error": _result_field(res, "error"),
            }
            output = _shrink_output(_result_field(res, "output"))
            if output is not None:
                entry["output"] = output
            results[dispatch_id] = entry
            if status == "ok":
                for sec in req_scopes:
                    if sec not in ok_scopes:
                        ok_scopes.append(sec)
            else:
                for sec in req_scopes or [scope]:
                    failures[sec] = status
        metrics = _pool_metrics_attribution(
            stats, requests, plan,
            reused=int((state.get("dispatch_metrics") or {}).get("reused", 0) or 0))
        history.append(
            "T2 Dispatch: 批次 %s 并行收尾（ok=%d，失败=%d）"
            % (batch_id, len(ok_scopes), len(failures))
        )
    else:
        # ---- 2)/3) MANUAL：中断一次 → 磁盘重推导 -----------------------
        # 命中重推导（不 interrupt）的两种情形：
        #  a) manual_interrupted_batch==batch_id：人工/Antigravity 批次已中断过
        #     （resume 或进程重启后 checkpoint 仍在）；
        #  b) dispatch_mode=="parallel"：自动并行批次执行中进程崩溃→重启→resume，
        #     活池已随进程消失（_live_pool 为 None），但 checkpoint 固化了她曾
        #     并行派发——此时同样只验证不中断：已写完的节按磁盘 sha256 判 ok，
        #     未写完的判 no_change/missing 交由返修环重派，绝不退化为人工 interrupt
        #     （audit_agent.md §5「恢复复用已成功 result」的确定性等价实现）。
        if (state.get("manual_interrupted_batch") == batch_id
                or state.get("dispatch_mode") == "parallel"):
            # 已中断过（resume 或进程重启后 checkpoint 仍在）：只验证，不中断
            history.append(
                f"T2 Dispatch: 批次 {batch_id} 已派发过（人工中断恢复或并行批次进程重启），"
                f"从磁盘重推导结果（不重复 interrupt）")
            ok_scopes, failures, entries = _verify_requests_from_disk(
                self_dir, requests,
                allow_unattested=_plan_allow_unattested(plan, self_dir))
            results.update(entries)
            metrics = _disk_metrics(
                requests, ok_scopes, failures,
                reused=int((state.get("dispatch_metrics") or {}).get("reused", 0) or 0),
                self_dir=self_dir, batch_id=batch_id)
            history.append(
                "T2 Dispatch: 批次 %s 磁盘重推导完成（ok=%d，失败=%d）"
                % (batch_id, len(ok_scopes), len(failures))
            )
            # 任务21（C12.4）：并发峰值只能来自本批真实批摘要；读不到就是
            # unknown(None) + 诊断来源，绝不回落到磁盘重推导的固定 0。
            if metrics.get("max_in_flight") is None:
                history.append(
                    "T2 Dispatch: 本批并发峰值 unknown（%s，来源 %s），不据未知判定"
                    "「伪装完成」，也不返修正文；如需确证请核对宿主 run 与 "
                    "%s" % (metrics.get("diagnostics") or "no_run_record",
                            metrics.get("max_in_flight_source"),
                            metrics.get("batch_run_rel") or dispatch_run_rel(batch_id)))
            else:
                history.append(
                    "T2 Dispatch: 本批并发观测 峰值=%d（口径 %s，来源 %s，原因 %s/%s）"
                    % (int(metrics["max_in_flight"]),
                       metrics.get("observed_scope"),
                       metrics.get("max_in_flight_source"),
                       metrics.get("serial_reason_code"),
                       metrics.get("serial_reason_source")))
            held = metrics.get("slots_held_unconfirmed")
            if isinstance(held, int) and held > 0:
                history.append(
                    "T2 Dispatch: 有 %d 个宿主槽位取消未获确认（持槽未释放），"
                    "相关 scope 结果按未完成/unknown 处理，需人工核对宿主 run 后再接管"
                    % held)
            for scope, status in failures.items():
                if status == "ok_unattested":
                    history.append(
                        f"Agent[T2]: {scope} 产物无代理证据（main-thread 直写嫌疑），"
                        f"进入返修环")
        else:
            payload = _batch_interrupt_payload(state, plan, requests, batch_id)
            history.append(
                f"Agent[T2]: 挂起等待人工 / Antigravity 整批产出 "
                f"{', '.join(str(r.get('scope')) for r in requests)}"
            )
            response = interrupt(payload)
            if not isinstance(response, dict):
                response = {}
            # 恢复侧防伪造：中断响应必须经 qchain_dispatch（host-subagent-v1）
            # 回传；旧 checkpoint 无该字段（None）不拦截。
            violation = agent_evidence.dispatch_protocol_violation(response)
            if violation:
                history.append(f"Agent[T2]: {violation}")
                return {
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": violation,
                    "dispatch_results": results,
                    "history": history,
                }
            files = response.get("files") or {}
            written: List[str] = []
            for req in requests:
                # _write_worker_files 按各自 artifacts 白名单过滤，逐请求落盘
                written.extend(_write_worker_files(self_dir, req, files))
            if written:
                history.append(
                    f"Agent[T2]: 已从中断响应落盘产物 {', '.join(sorted(set(written)))}")
            return {
                "manual_interrupted_batch": batch_id,
                "dispatch_mode": "manual",
                "stage": "T2_SECTIONS_RUNNING",
                "dispatch_results": results,
                "history": history,
            }

    # ---- 并发门禁（任务21 / C12.4）：越界才阻断，低峰值只记原因 ----------
    # 生产内容门禁不设「峰值必须 >= 声明并行度」的硬阈值：仅剩一单、缓存命中、
    # 实际依赖、极短请求、宿主限流都能合法压低峰值，此时必须给原因 + 警告，
    # 绝不允许因此返修/重写学案；而峰值 > 计划上限属正确性阻断（cap 越界）。
    parallelism = _parallelism_check(plan, metrics, batch_id)
    if parallelism["halt"]:
        history.append("T2 Dispatch: 批次 %s %s" % (batch_id, parallelism["halt"]))
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": parallelism["halt"],
            "dispatch_results": results,
            "dispatch_metrics": metrics,
            "history": history,
        }
    if parallelism["warning"]:
        warning = parallelism["warning"]
        metrics = dict(metrics or {})
        metrics["parallelism_warning"] = warning
        history.append(
            "T2 Dispatch: 批次 %s 并发告警 %s（声明 %d，实测 %s，原因 %s/%s）："
            "仅记录原因，不返修正文"
            % (batch_id, warning["code"], warning["declared"],
               warning["observed"], warning["serial_reason_code"],
               warning["serial_reason_source"]))

    # ---- 收尾：失败 → 返修；全 ok → 串行提交 ---------------------------
    for scope in ok_scopes:
        statuses[scope] = "staged"
        staged[scope] = os.path.join(self_dir, f"{scope}.tex")

    # 任务5：本批 ok 切片写入派生物缓存（下次相同 input_digest/config/prompt
    # 直接还原，worker submit 为 0；store 前逐一校验产物 digest，缺失不写）
    # C5 逐节：多 scope 请求按节分别落缓存条目（每节用自己的源码摘要与产物清单），
    # 这样单节返修重跑时其余节仍能命中缓存，不会因整批摘要不同而全量重做。
    _ok_set = set(ok_scopes)
    cache_skipped: List[str] = []
    for req in requests:
        req_scopes = _request_scope_list(req) or [str(req.get("scope") or "")]
        multi = len(req_scopes) >= 2
        assets = (req.get("context") or {}).get("source_assets")
        for sec in req_scopes:
            if not sec or sec not in _ok_set:
                continue
            if multi:
                # 多 scope：按节重算单节探针摘要（单节返修时能直接命中）
                digest = _section_probe_digest(state, sec, assets)
            else:
                digest = str(req.get("input_digest") or "").strip() or None
            try:
                event = store_section_cache(
                    self_dir, state, sec, None, assets,
                    artifacts=_scope_artifacts(req, sec),
                    stage=str(req.get("stage") or "") or None,
                    input_digest=digest, record=False)
            except OSError:
                history.append("T2 Dispatch: 派生物缓存写入失败（忽略，不影响暂存）")
                continue
            if not digest or not event.get("stored"):
                # 卡13 保守缺口：算不出键/写不进条目时过去是静默 continue，
                # 现在必须留痕——「不缓存」是显式事实，不是无声的空洞。
                cache_skipped.append(sec)
                event = dict(event, stored=False,
                             reason=event.get("reason")
                             or "未写入缓存（本节无法复算派发单摘要）")
            record_cache_event(self_dir, event)
    if cache_skipped:
        history.append(
            "T2 Dispatch: 以下节未能写入派生物缓存（无可用键/产物缺失）%s"
            % ",".join(cache_skipped))

    if failures:
        repair_target = next(
            (sec for sec in expected_sections if sec in failures), None)
        detail = "; ".join(
            f"{scope}={status}" for scope, status in failures.items())
        history.append(
            f"T2 Dispatch: 批次 {batch_id} 存在失败切片（{detail}），转入定点返修")
        return {
            "routing_action": "REPAIR_SECTION",
            "repair_target": repair_target,
            "repair_reason": f"T2 并行派发未全部完成: {detail}",
            "stage": "REPAIR_LOOP",
            "section_statuses": statuses,
            "staged_sections": staged,
            "dispatch_results": results,
            "dispatch_metrics": metrics,
            "history": history,
        }

    active_section = next(
        (sec for sec in expected_sections if statuses.get(sec) == "staged"), None)
    history.append(
        f"T2 Dispatch: 批次 {batch_id} 全部完成，{len(ok_scopes)} 个切片已暂存")
    return {
        "active_section": active_section,
        "routing_action": "PROCEED",
        "stage": "T2_SECTIONS_STAGED",
        "section_statuses": statuses,
        "staged_sections": staged,
        "dispatch_results": results,
        "dispatch_metrics": metrics,
        "dispatch_mode": state.get("dispatch_mode"),
        "history": history,
    }


def dispatch_run_rel(batch_id: str) -> str:
    """批运行摘要的相对路径（与 index.js 单协调者落盘位置一致）。"""
    return f"{DISPATCH_RUNS_REL.replace(os.sep, '/')}/{batch_id}.json"


def _unknown_run(diagnostic: str, rel: str = "") -> Dict[str, Any]:
    """批摘要不可用时的统一口径：峰值/是否/计数全部 unknown（None），带来源。"""
    source = f"unknown:{diagnostic}"
    return {
        "available": False,
        "valid": False,
        "diagnostic": diagnostic,
        "rel": rel,
        "record": None,
        "peak": None,
        "peak_source": source,
        "peak_ready": None,
        "observed_scope": None,
        "real_starts": None,
        "cache_reuse": None,
        "receipt_refs": [],
        "queue_wait_ms": None,
        "vendor_queue_ms": None,
        "vendor_first_token_ms": None,
        "inference_ms": None,
        "serial_reason_code": UNKNOWN_SERIAL_REASON,
        "serial_reason_source": source,
        "effective_concurrency": None,
        "slots_held_unconfirmed": None,
        "undispatched": None,
        "cap_respected": None,
        "elapsed_ms": None,
    }


def _serial_reason_of(record: Dict[str, Any]) -> Tuple[str, str]:
    """批摘要里的串行原因：必须落在 C12.4 词表内，否则统一记 unknown。"""
    code = str(record.get("serial_reason_code") or "").strip()
    source = str(record.get("serial_reason_source") or "").strip()
    if code not in SERIAL_REASON_CODES:
        return UNKNOWN_SERIAL_REASON, f"unknown:{code or 'reason_missing'}"
    return code, source or "unknown:reason_source_missing"


def _load_dispatch_run(
    self_dir: Optional[str],
    batch_id: Optional[str],
    request_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """读本批次的真实批摘要（.pd/dispatch-runs/<batch_id>.json）。

    零信任校验（C12.4）：
      * 文件缺失 → run_record_missing；
      * 非法 JSON / 非对象 → run_record_unreadable；
      * 文件内 batch_id 与请求批次不符 → run_record_batch_mismatch；
      * request_ids 与本批请求集合不符（旧批摘要留在盘上）→ run_record_request_mismatch；
      * schema_version < 2 或没有整型峰值 → run_record_peak_unknown。

    任何一种不通过都返回 unknown（None + 来源），绝不回落到「磁盘重推导的 0」。
    """
    bid = str(batch_id or "").strip()
    if not self_dir or not bid:
        return _unknown_run("run_record_missing")
    rel = dispatch_run_rel(bid)
    path = os.path.join(self_dir, DISPATCH_RUNS_REL, f"{bid}.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            record = json.load(f)
    except FileNotFoundError:
        return _unknown_run("run_record_missing", rel)
    except (OSError, ValueError):
        return _unknown_run("run_record_unreadable", rel)
    if not isinstance(record, dict):
        return _unknown_run("run_record_unreadable", rel)

    out = _unknown_run("run_record_peak_unknown", rel)
    out["record"] = {
        "batch_id": record.get("batch_id"),
        "schema_version": record.get("schema_version"),
        "request_count": record.get("request_count"),
        "serial_reason_code": record.get("serial_reason_code"),
        "executor_kind": record.get("executor_kind"),
    }
    if str(record.get("batch_id") or "") != bid:
        out["diagnostic"] = "run_record_batch_mismatch"
        out["peak_source"] = f"unknown:{out['diagnostic']}"
        out["serial_reason_source"] = out["peak_source"]
        return out

    recorded_ids = [str(i) for i in (record.get("request_ids") or [])]
    if request_ids is not None and recorded_ids:
        if set(recorded_ids) != {str(i) for i in request_ids}:
            out["diagnostic"] = "run_record_request_mismatch"
            out["peak_source"] = f"unknown:{out['diagnostic']}"
            out["serial_reason_source"] = out["peak_source"]
            return out

    try:
        schema_version = int(record.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    peak = record.get("peak_observed_host_in_flight",
                      record.get("observed_host_in_flight"))
    peak_ok = isinstance(peak, int) and not isinstance(peak, bool) and peak >= 0
    if schema_version < DISPATCH_RUN_SCHEMA_VERSION or not peak_ok:
        out["diagnostic"] = "run_record_peak_unknown"
        out["peak_source"] = f"unknown:{out['diagnostic']}"
        out["serial_reason_source"] = out["peak_source"]
        return out

    code, code_source = _serial_reason_of(record)
    peak_ready = record.get("peak_ready")
    out.update({
        "available": True,
        "valid": True,
        "diagnostic": None,
        "peak": int(peak),
        "peak_source": "dispatch_run",
        "peak_ready": peak_ready if isinstance(peak_ready, int) else None,
        "observed_scope": str(record.get("observed_scope") or "unknown"),
        "real_starts": _optional_int(record.get("real_start_count")),
        "cache_reuse": _optional_int(record.get("cache_reuse_count")),
        "receipt_refs": [str(r) for r in (record.get("receipt_refs") or [])],
        "queue_wait_ms": record.get("queue_wait_ms") or None,
        # 供应商侧时间：只有真实拿到值才写，否则保持 null（未知 ≠ 实测 0）
        "vendor_queue_ms": record.get("vendor_queue_ms"),
        "vendor_first_token_ms": record.get("vendor_first_token_ms"),
        "inference_ms": record.get("inference_ms"),
        "serial_reason_code": code,
        "serial_reason_source": code_source,
        "effective_concurrency": _optional_int(record.get("effective_concurrency")),
        "slots_held_unconfirmed": _optional_int(record.get("slots_held_unconfirmed")),
        "undispatched": _optional_int(record.get("undispatched")),
        "cap_respected": record.get("cap_respected"),
        "elapsed_ms": _optional_int(record.get("elapsed_ms")),
    })
    return out


def _optional_int(value: Any) -> Optional[int]:
    """整型透传，其余一律 None（绝不用 0 冒充未知）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _parallelism_check(
    plan: Dict[str, Any],
    metrics: Dict[str, Any],
    batch_id: str = "",
) -> Dict[str, Any]:
    """生产内容门禁的并发判定（C12.4）。

    返回 ``{"halt": str|None, "warning": dict|None}``：
      * 峰值 unknown（None）→ 只给 PARALLELISM_UNVERIFIED 警告，不阻断；
      * 峰值 > 计划并发上限 → 正确性阻断（halt）；
      * 峰值 < 声明并行度 → 只给 PARALLELISM_BELOW_DECLARED 警告 + 原因码，
        绝不因此让正文返修（单请求/缓存命中/实际依赖/宿主限流都合法）；
      * 达标 → 无告警。
    """
    plan = plan or {}
    metrics = dict(metrics or {})
    declared = int(plan.get("min_parallelism") or 1)
    cap = _coerce_int(plan.get("max_concurrency"), 0) or None
    observed_source = str(metrics.get("max_in_flight_source") or "unknown:no_observation")
    reason_code = str(metrics.get("serial_reason_code") or UNKNOWN_SERIAL_REASON)
    reason_source = str(metrics.get("serial_reason_source") or "unknown:no_observation")
    raw_peak = metrics.get("max_in_flight")
    if isinstance(raw_peak, bool) or not isinstance(raw_peak, int):
        return {
            "halt": None,
            "warning": {
                "code": PARALLELISM_UNVERIFIED,
                "batch_id": batch_id,
                "declared": declared,
                "observed": None,
                "observed_source": observed_source,
                "serial_reason_code": reason_code,
                "serial_reason_source": reason_source,
                "content_rework_allowed": False,
                "note": (
                    "峰值并发未观测到（unknown，不是实测 0）：缺批摘要或旧摘要不受信。"
                    "不据此返修正文，需人工核对宿主 run 后再接管"),
            },
        }
    if cap is not None and raw_peak > cap:
        return {
            "halt": (
                f"并发越界：本批实测宿主在飞 {raw_peak} > 计划并发上限 {cap}"
                "（cap 越界属正确性阻断，必须人工排查派发实现）"),
            "warning": None,
        }
    if raw_peak < declared:
        return {
            "halt": None,
            "warning": {
                "code": PARALLELISM_BELOW_DECLARED,
                "batch_id": batch_id,
                "declared": declared,
                "observed": raw_peak,
                "observed_source": observed_source,
                "serial_reason_code": reason_code,
                "serial_reason_source": reason_source,
                "content_rework_allowed": False,
                "note": (
                    "峰值低于声明并行度，但低峰值本身合法（仅剩一单/缓存命中/"
                    "实际依赖/极短请求/宿主限流）；只记原因与警告，不重写学案"),
            },
        }
    return {"halt": None, "warning": None}


def _pool_metrics_attribution(
    stats: Dict[str, Any],
    requests: List[Dict[str, Any]],
    plan: Dict[str, Any],
    reused: int = 0,
) -> Dict[str, Any]:
    """活池并行路径的度量 + 观测口径/原因归属（任务21）。

    活池的 ``max_in_flight`` 是同进程真实宿主调用计数（口径 host_call），
    但供应商排队/首 token/推理时间仍未观测 → 一律 None。
    """
    metrics: Dict[str, Any] = dict(stats or {})
    peak = metrics.get("max_in_flight")
    peak_i = peak if isinstance(peak, int) and not isinstance(peak, bool) else None
    metrics["max_in_flight"] = peak_i
    metrics["max_in_flight_source"] = (
        "worker_pool_stats" if peak_i is not None else "unknown:pool_stats_missing")
    metrics["observed_scope"] = "host_call"
    metrics["vendor_queue_ms"] = None
    metrics["vendor_first_token_ms"] = None
    metrics["inference_ms"] = None
    plan = plan or {}
    declared = int(plan.get("min_parallelism") or 1)
    count = len(requests or [])
    cache_reuse = int(reused or 0)
    if count <= 1:
        code, source = "single_ready", "observed_request_count"
    elif cache_reuse >= count:
        code, source = "cache_reuse", "observed_claim_reuse"
    elif peak_i is None:
        code, source = UNKNOWN_SERIAL_REASON, "unknown:pool_stats_missing"
    elif peak_i < min(declared, count):
        code, source = UNKNOWN_SERIAL_REASON, "observed_low_peak"
    else:
        code, source = "", "observed_parallel"
    metrics["serial_reason_code"] = code
    metrics["serial_reason_source"] = source
    metrics.setdefault("reused", cache_reuse)
    metrics.setdefault("cache_reuse", cache_reuse)
    return metrics


def _disk_metrics(
    requests: List[Dict[str, Any]],
    ok_scopes: List[str],
    failures: Dict[str, str],
    reused: int = 0,
    self_dir: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """manual/磁盘重推导路径的度量（无活池时的确定性推导）。

    ``reused`` 透传计划节点记录的缓存命中数（audit §17 任务5：cache hit
    必须写 dispatch_metrics.reused，重推导路径不得把它抹掉）。

    任务21（C12.4）：并发峰值只能来自本批真实批摘要
    （``.pd/dispatch-runs/<batch_id>.json``）。没有可信摘要时
    ``max_in_flight`` 返回 None 并带 ``max_in_flight_source=unknown:<原因>``，
    绝不用磁盘重推导的固定 0 冒充实测。
    """
    statuses = list(failures.values())
    request_ids = [str(r.get("dispatch_id") or "") for r in (requests or [])]
    bid = str(batch_id or "").strip()
    if not bid and requests:
        bid = str((requests[0] or {}).get("batch_id") or "")
    run = _load_dispatch_run(self_dir, bid, request_ids)
    cache_reuse_param = int(reused or 0)
    cache_reuse = cache_reuse_param or int(run.get("cache_reuse") or 0)
    return {
        "submitted": len(requests),
        "completed": len(requests),
        "ok": len(ok_scopes),
        "failed": sum(1 for s in statuses if s not in ("no_change",)),
        "no_change": sum(1 for s in statuses if s == "no_change"),
        "timeout": 0,
        "deferred": 0,
        "retried": 0,
        # 并发观测：unknown=None（绝不写 0），来源可追溯
        "max_in_flight": run["peak"],
        "max_in_flight_source": run["peak_source"],
        "observed_scope": run["observed_scope"] or "unknown",
        "peak_ready": run["peak_ready"],
        "real_starts": run["real_starts"],
        "cache_reuse": cache_reuse,
        "serial_reason_code": run["serial_reason_code"],
        "serial_reason_source": run["serial_reason_source"],
        "receipt_refs": run["receipt_refs"],
        "queue_wait_ms": run["queue_wait_ms"],
        "vendor_queue_ms": run["vendor_queue_ms"],
        "vendor_first_token_ms": run["vendor_first_token_ms"],
        "inference_ms": run["inference_ms"],
        "cap_respected": run["cap_respected"],
        "slots_held_unconfirmed": run["slots_held_unconfirmed"],
        "undispatched": run["undispatched"],
        "batch_run_rel": run["rel"] or None,
        "diagnostics": run["diagnostic"],
        "orphans": 0,
        "reused": cache_reuse,
    }
