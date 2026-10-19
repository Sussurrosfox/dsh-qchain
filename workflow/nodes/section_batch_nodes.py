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

磁盘契约（scripts/check_dispatch.py 硬门禁 R1-R9，只校验门禁子集）：
    <self_dir>/.pd/dispatch-plan.json
派发单（完整请求，供人/Agent 阅读）：
    <self_dir>/.pd/agent-requests/<dispatch_id>.json
"""

from __future__ import annotations

import datetime
import json
import os
import re
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
    ArtifactCache, config_digest, prompt_version,
)
from ..adapters.worker_pool import WorkerPool
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
PROFILE_CONCURRENCY: Dict[str, int] = {"interactive": 1, "safe": 1, "fast": 4, "budget": 2}

#: 未配置 agent_dispatch 且 run_scope 未知时的保守回落（串行）
DEFAULT_PROFILE = "safe"
#: run_scope → 缺省档位（audit §12.1：交互微调 interactive，完整生成 fast）
RUN_SCOPE_DEFAULT_PROFILE: Dict[str, str] = {
    "full": "fast",
    "focused": "interactive",
    "edit": "interactive",
}
DEFAULT_BATCH_WINDOW = 2
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_RETRIES = 2

#: 派发配置文件（位于 self_dir 下，即项目 config.yaml）
DISPATCH_CONFIG_REL = "config.yaml"
#: 磁盘派发计划（check_dispatch.py 的默认读取位置）
DISPATCH_PLAN_REL = os.path.join(".pd", "dispatch-plan.json")
#: check_dispatch 门禁的结构化报告（run_gate_script 自动追加 --json）
DISPATCH_REPORT_REL = os.path.join(".pd", "reports", "check_dispatch.json")
#: 前置依赖表（由主 Agent 生成，尽力而为的拓扑排序输入）
DEPENDENCY_MAP_REL = os.path.join(".pd", "dependency-map.md")

#: 不再进入派发批的小节状态（staged/running 由串行提交节点接管；
#: reused/skipped 是任务3 工作集外小节的终态——不得再派发，也不伪装 ok）
NON_PENDING_STATUSES = (
    "checked", "accepted", "staged", "running", "reused", "skipped")

#: dispatch_results 单条目 output 的最大序列化长度（超过则省略，防止状态膨胀）
MAX_OUTPUT_CHARS = 4096

#: dependency-map.md 的节点编号形态（sec1 / sec0 / sec12 / secA-1 …）
RE_SECTION_NODE = re.compile(r"^sec[0-9A-Za-z_-]+$")


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
    """
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

    raw_dispatch = raw.get("agent_dispatch")
    if isinstance(raw_dispatch, dict):
        dispatch_map = dict(raw_dispatch)
        profile = str(dispatch_map.get("profile") or "").strip().lower()
    else:
        dispatch_map = {}
        profile = str(raw_dispatch or "").strip().lower()
    if not profile:
        # 未显式配置 → 按 run_scope 推导缺省档位（audit §12.1）
        profile = RUN_SCOPE_DEFAULT_PROFILE.get(
            str(run_scope or raw.get("run_scope") or "").strip().lower(), DEFAULT_PROFILE)
    if profile not in PROFILE_CONCURRENCY:
        profile = DEFAULT_PROFILE

    configured = _coerce_int(raw.get("agent_max_concurrency"), 0)
    if configured <= 0:
        configured = _coerce_int(dispatch_map.get("max_concurrency"), 0)
    concurrency = configured if configured > 0 else PROFILE_CONCURRENCY[profile]
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
        "profile": profile,
        "max_concurrency": max(1, concurrency),
        "configured_max_concurrency": configured,
        "batch_window": window,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "allow_unattested": allow_unattested,
        "model_map": model_map,
        # 任务1：工作集选择（config.yaml 持久化；state 缺省时回落此处，
        # 覆盖 resume/进程重启路径。非法值由 _work_set 报错，不静默回退 full）
        "run_scope": str(raw.get("run_scope") or "").strip(),
        "target_sections": normalize_targets(raw.get("target_sections")),
        "target_qids": normalize_targets(raw.get("target_qids")),
    }


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
# 请求构造与磁盘计划
# ---------------------------------------------------------------------------

def _section_request_spec(
    state: ShrinkCoursewareState,
    sec: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """切片派发单的确定性输入（instruction/command/context/role/artifacts）。

    任务5：缓存探针与正式派发共用同一 spec，保证缓存键 ``input_digest``
    一致（探针只读不写、无 pdstate 副作用，仅用于缓存查询）。
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
        "context": {"section_id": sec, "user_spec": state.get("user_spec", {})},
        "scope": sec,
        "timeout_seconds": cfg["timeout_seconds"],
        "max_retries": cfg["max_retries"],
        "retry_class": "transient",
    }


def _build_requests(
    state: ShrinkCoursewareState,
    self_dir: str,
    batch: List[str],
    batch_id: str,
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """为批次内每一节建派发单（pdstate 起跑 + build_request + 落盘）。

    返回 ``(requests, history_lines)``；派发单字段与串行链
    node_t2_request_sections 完全一致，另加 batch_id / scope / 档位超时与重试。
    """
    runner = ScriptRunner(self_dir)
    requests: List[Dict[str, Any]] = []
    lines: List[str] = []
    for sec in batch:
        code_st, out_st, err_st = _ensure_task_ready_and_start(self_dir, sec, runner)
        if code_st == 0:
            lines.append(f"T2 Dispatch: pdstate.py 已为 {sec} 创建独占 attempt 并就绪")
        else:
            lines.append(f"T2 Dispatch: pdstate.py start {sec} 提示: {(out_st or err_st).strip()}")

        req = build_request(
            state,
            batch_id=batch_id,
            **_section_request_spec(state, sec, cfg),
        )
        # 并行模式下 WorkerPool 的默认执行器会把 success_stage/success_history
        # 透传给 run_dispatch，使其返回的 patch 带上正确的收尾 stage。
        req["request_ref"] = f"{REQUEST_DIR_REL}/{req['dispatch_id']}.json"
        req["success_stage"] = "T2_SECTIONS_STAGED"
        req["success_history"] = f"T2: {sec} 切片已由派发池完成"
        write_request_file(self_dir, req)
        lines.append(f"T2 Dispatch: {sec} 已纳入批次 {batch_id}（派发单: {req['request_ref']}）")
        requests.append(req)
    return requests, lines


def _disk_plan(
    batch_id: str,
    batch: List[str],
    requests: List[Dict[str, Any]],
    pool_limit: int,
    effective_limit: Optional[int] = None,
    worker_capabilities: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造写入 .pd/dispatch-plan.json 的门禁子集（check_dispatch.py 的输入）。

    ``max_concurrency`` 取 ``max(pool_limit, len(batch))``：批次窗口
    （并发*窗口）允许一批请求数超过档位并发上限，而 R7 要求
    ``len(requests) <= max_concurrency``，因此这里诚实声明「本批并行上限」；
    真正的限流仍由 WorkerPool 按 pool_limit 执行。

    ``effective_limit``（任务8）：worker 能力声明压薄后的真实并行度
    （= min(档位并发, worker 声明的 max_concurrency)）。``min_parallelism``
    按它承诺——串行 worker（未声明能力）只承诺 1，不把「计划串行」冒充并行
    承诺；声明并行的 worker 才承诺 2（多节批次）。配合 join 节点的实际并发度
    硬门禁（audit_agent.md §6 / P1.4：实际并发度低于计划值必须失败，
    禁止伪装完成）。

    ``worker_capabilities`` 原样落盘：本轮真实并行度与取消语义的审计凭据。
    """
    limit = effective_limit if effective_limit else pool_limit
    return {
        "batch_id": batch_id,
        "stage": "T2",
        "created_at": _iso_now(),
        "max_concurrency": max(pool_limit, len(batch)),
        "effective_concurrency": limit,
        "min_parallelism": 1 if len(batch) == 1 else min(2, max(1, limit)),
        "sections": list(batch),
        "required_scope_count": len(batch),
        "worker_capabilities": dict(worker_capabilities or {}),
        "requests": [
            {
                "dispatch_id": req.get("dispatch_id"),
                "scope": req.get("scope"),
                "role": req.get("role"),
                "input_digest": req.get("input_digest"),
                "artifacts": list(req.get("artifacts") or []),
                "status": req.get("status", "pending"),
                "batch_id": batch_id,
            }
            for req in requests
        ],
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
            f"T2 并行写作批派发：一批 {len(requests)} 个小节切片"
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
        **agent_evidence.dispatch_protocol_block(),
        "max_concurrency": plan.get("max_concurrency"),
        "min_parallelism": plan.get("min_parallelism"),
        "requests": [
            {
                "dispatch_id": req.get("dispatch_id"),
                "scope": req.get("scope"),
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
        - 全部产物均未变化 → no_change 失败；
        - 部分产物已变化 → 查 .pd/agent-calls/<dispatch_id>.json：
            * 证据吻合 → ok（entries.verified_via="agent"）；
            * 无证据 → ok_unattested：allow_unattested=True 时放行并打
              unattested 标记，否则失败（主线程直写嫌疑，进返修环）；
            * 证据在案但不符 → attested_mismatch，一律失败无逃生阀。

    allow_unattested 缺省时读当前 config.yaml（caller 传 checkpoint 固化的
    dispatch_config 值可避免口径漂移）。

    返回 ``(ok_scopes, failures, result_entries)``，其中
    ``failures`` 为 ``{scope: status}``，``result_entries`` 以 dispatch_id 为键。
    """
    ok_scopes: List[str] = []
    failures: Dict[str, str] = {}
    entries: Dict[str, Dict[str, Any]] = {}
    if allow_unattested is None:
        allow_unattested = bool(
            _load_dispatch_config(self_dir).get("allow_unattested", False))
    for req in requests:
        dispatch_id = str(req.get("dispatch_id") or "")
        scope = str(req.get("scope") or "")
        artifacts = [a for a in (req.get("artifacts") or []) if isinstance(a, str)]
        before = dict(req.get("before") or {})
        after = {}
        for rel in artifacts:
            after[rel] = _sha256_of(self_dir, rel)

        status, _evidence = agent_evidence.check_request_attested(
            self_dir, req, after, allow_unattested=allow_unattested)

        entry: Dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "scope": scope,
            "status": status,
            "attempts": 1,
            "error": None,
            "verified_from": "disk",
            # 证据链口径：agent=子 Agent 证据吻合；unattested=放行但无证据；
            # n/a=缺失/空转/证据不符（均判失败）
            "verified_via": (
                "agent" if status == "ok"
                else "unattested" if status == "ok_unattested"
                else "n/a"
            ),
        }
        if status == "ok":
            ok_scopes.append(scope)
        elif status == "ok_unattested" and allow_unattested:
            # 逃生阀放行：保留 unattested 标记，交付门禁 check_agent_audit 终裁
            entry["unattested"] = True
            ok_scopes.append(scope)
        else:
            failures[scope] = status
            entry["error"] = _attestation_error_detail(
                status, artifacts, before, after, scope)
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
    _cfg_digest = config_digest(self_dir)
    _prompt_ver = prompt_version(self_dir, "subagent_prompt.md")
    restored: List[str] = []
    for sec in list(ordered):
        probe = build_request(state, **_section_request_spec(state, sec, cfg))
        entry = _cache.lookup(
            stage=probe["stage"], scope=sec,
            input_digest=probe["input_digest"],
            config_digest=_cfg_digest, prompt_version=_prompt_ver,
            artifacts=probe["artifacts"],
        )
        if entry is not None and _cache.restore(entry):
            statuses[sec] = "staged"
            restored.append(sec)
    if restored:
        history.append(
            "T2 Dispatch: 派生物缓存命中 %s（input_digest/config/prompt 未变，"
            "还原自缓存并转入提交，不重复派发）" % ",".join(restored))
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

    requests, build_lines = _build_requests(state, self_dir, batch, batch_id, cfg)
    history.extend(build_lines)

    plan_disk = _disk_plan(
        batch_id, batch, requests, cfg["max_concurrency"],
        effective_limit=_effective_limit, worker_capabilities=_worker_caps)
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
        for req in requests:
            dispatch_id = str(req.get("dispatch_id") or "")
            scope = str(req.get("scope") or "")
            res = raw_results.get(dispatch_id)
            if res is None:
                failures[scope] = "missing_result"
                results[dispatch_id] = {
                    "dispatch_id": dispatch_id,
                    "scope": scope,
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
                "status": status,
                "attempts": _result_field(res, "attempts", 0),
                "error": _result_field(res, "error"),
            }
            output = _shrink_output(_result_field(res, "output"))
            if output is not None:
                entry["output"] = output
            results[dispatch_id] = entry
            if status == "ok":
                ok_scopes.append(scope)
            else:
                failures[scope] = status
        metrics = stats
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
                reused=int((state.get("dispatch_metrics") or {}).get("reused", 0) or 0))
            history.append(
                "T2 Dispatch: 批次 %s 磁盘重推导完成（ok=%d，失败=%d）"
                % (batch_id, len(ok_scopes), len(failures))
            )
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

    # ---- 硬门禁：实际并发度不得低于计划值（audit_agent.md §6 / P1.4） -----
    # 仅 parallel 模式校验：manual 路径是人工/Antigravity 批次，已由
    # dispatch_mode="manual" 显式标记，不属于「伪装并行」。
    if parallel_mode:
        min_parallel = int(plan.get("min_parallelism") or 1)
        actual = int((metrics or {}).get("max_in_flight", 0) or 0)
        if actual < min_parallel:
            history.append(
                "T2 Dispatch: 批次 %s 实际并发度 %d 低于计划值 %d，判定伪装完成"
                % (batch_id, actual, min_parallel))
            return {
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": (
                    f"实际并发度 {actual} 低于计划值 {min_parallel}，"
                    "禁止伪装完成（audit_agent.md §6）"),
                "dispatch_results": results,
                "dispatch_metrics": metrics,
                "history": history,
            }

    # ---- 收尾：失败 → 返修；全 ok → 串行提交 ---------------------------
    for scope in ok_scopes:
        statuses[scope] = "staged"
        staged[scope] = os.path.join(self_dir, f"{scope}.tex")

    # 任务5：本批 ok 切片写入派生物缓存（下次相同 input_digest/config/prompt
    # 直接还原，worker submit 为 0；store 前逐一校验产物 digest，缺失不写）
    _ok_set = set(ok_scopes)
    _cache = ArtifactCache(self_dir)
    _cfg_digest = config_digest(self_dir)
    for req in requests:
        if req.get("scope") not in _ok_set or not req.get("input_digest"):
            continue
        try:
            _cache.store(
                stage=str(req.get("stage") or ""),
                scope=str(req.get("scope") or ""),
                input_digest=req["input_digest"],
                config_digest=_cfg_digest,
                prompt_version=prompt_version(self_dir, "subagent_prompt.md"),
                artifacts=list(req.get("artifacts") or []),
            )
        except OSError:
            history.append("T2 Dispatch: 派生物缓存写入失败（忽略，不影响暂存）")

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


def _disk_metrics(
    requests: List[Dict[str, Any]],
    ok_scopes: List[str],
    failures: Dict[str, str],
    reused: int = 0,
) -> Dict[str, int]:
    """manual/磁盘重推导路径的度量（无活池时的确定性推导）。

    ``reused`` 透传计划节点记录的缓存命中数（audit §17 任务5：cache hit
    必须写 dispatch_metrics.reused，重推导路径不得把它抹掉）。
    """
    statuses = list(failures.values())
    return {
        "submitted": len(requests),
        "completed": len(requests),
        "ok": len(ok_scopes),
        "failed": sum(1 for s in statuses if s not in ("no_change",)),
        "no_change": sum(1 for s in statuses if s == "no_change"),
        "timeout": 0,
        "deferred": 0,
        "retried": 0,
        "max_in_flight": 0,
        "orphans": 0,
        "reused": int(reused or 0),
    }
