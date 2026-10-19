"""worker_pool.py — 并发派发执行器（WorkerPool）for the QChain (题链) plugin.

把 FIXED-P0 派发请求（``workflow.nodes.agent_nodes.build_request`` 产出的 dict，
携带 dispatch_id / scope / timeout_seconds / max_retries / retry_class /
attempt / status 等硬合同键）放到有界线程池上并发执行，并按
「瞬时故障可重试、契约/内容故障不重试」的分类策略收敛结果。

模块级仅依赖标准库：严禁在模块顶部 import langgraph 或 workflow.graph。
默认执行器（``workflow.nodes.agent_nodes.run_dispatch``）与
``workflow.adapters.worker_registry`` 单例均在方法体内惰性 import，以避免
adapters -> nodes -> graph 的导入环，并让本模块在未安装 langgraph 的环境里
依然可导入。

红线：本模块绝不调用 ``interrupt()`` —— 人工 / Antigravity 挂起路径由调用方
负责；当既没有显式 executor、worker_registry 也未注册认知 worker 时，请求被
记为 ``deferred`` 交还调用方，而不是偷偷走人工中断。
"""

import importlib
import inspect
import os
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

__all__ = ["DispatchResult", "WorkerPool"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

THREAD_NAME_PREFIX = "qchain-worker"

#: 状态取值（DispatchResult.status）
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_TIMEOUT_DEFERRED = "timeout_deferred"
STATUS_CANCELLED = "cancelled"
STATUS_NO_CHANGE = "no_change"
STATUS_DEFERRED = "deferred"

#: 被视为「空转 / 无变更」的结果状态
NO_CHANGE_STATUSES = frozenset({"no_change", "unchanged"})
#: 错误文案中命中即判定为「无变更」的子串（末两项是 agent_nodes.run_dispatch
#: 实际使用的中文措辞："派发 {id} 的产物未发生变化: ..."）
NO_CHANGE_MARKERS = ("unchanged", "no change", "未发生变化", "未变化")
#: 结果 dict 中可能承载错误文案的键（按优先级；"_error" 单独先判定）
ERROR_TEXT_KEYS = ("_error", "error", "error_message", "detail", "message")

#: 只有 transient 类故障允许重试；content / contract 类一律单发
TRANSIENT_RETRY_CLASS = "transient"

RETRY_BACKOFF_BASE_SECONDS = 0.05
RETRY_BACKOFF_MAX_SECONDS = 1.0

#: stats() 快照的键序（completed / max_in_flight / orphans 由现值推导）
STAT_KEYS: Tuple[str, ...] = (
    "submitted", "completed", "ok", "failed", "timeout", "no_change",
    "deferred", "retried", "max_in_flight", "orphans", "cancelled",
)

_SOFT_TIMEOUT_ERROR = (
    "执行耗时超过软时限（结果已产出但迟到，按分类规则保留并由调用方决定重派）"
)
_JOIN_TIMEOUT_ERROR = (
    "派发在 join 截止时刻仍在执行（worker 未声明可取消，线程不可抢占，"
    "结果延后记入 orphans；调用方须按失败 scope 重派，不得复用旧产物）"
)
_CANCEL_REQUESTED_ERROR = (
    "派发在 join 截止时刻仍在执行（worker 声明支持取消，已请求取消；"
    "迟到结果若抵达仍记入 orphans）"
)

#: 能力声明缺省值。显式传入 executor 而未声明能力时信任调用方给出的并发
#: 上限（max_concurrency=None 表示不额外节流）；走 worker_registry 而未
#: 声明能力时按保守默认串行执行（audit §16：不得让多线程共享未确认线程
#: 安全的同步 worker）。
_EXPLICIT_EXECUTOR_CAPABILITIES: Dict[str, Any] = {
    "sync": True, "async": False, "cancellable": False, "max_concurrency": None,
}
_REGISTRY_FALLBACK_CAPABILITIES: Dict[str, Any] = {
    "sync": True, "async": False, "cancellable": False, "max_concurrency": 1,
}


#: 能力声明的键集（与 worker_registry._CAP_KEYS 同口径，重复定义避免循环导入）
_CAP_KEYS: Tuple[str, ...] = ("sync", "async", "cancellable", "max_concurrency")


def _normalize_capabilities(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化 worker 能力声明（本模块自洽实现，max_concurrency 允许 None）。

    ``max_concurrency=None`` 表示「不额外节流」（显式 executor 且未声明时）；
    其余畸形值（布尔、非数值、<1）一律 ValueError，不静默回退。
    """
    caps: Dict[str, Any] = {
        "sync": True, "async": False, "cancellable": False,
        "max_concurrency": None,
    }
    if raw is None:
        return caps
    if not isinstance(raw, dict):
        raise TypeError(
            "worker capabilities 必须是 dict（当前 %s）" % type(raw).__name__)
    unknown = sorted(set(raw) - set(_CAP_KEYS))
    if unknown:
        raise ValueError(
            "worker capabilities 含未知键 %s（已知键: %s）"
            % (", ".join(unknown), ", ".join(_CAP_KEYS)))
    for key in ("sync", "async", "cancellable"):
        value = raw.get(key, caps[key])
        if not isinstance(value, bool):
            raise ValueError(
                "worker capabilities %s 必须是布尔值（当前 %r）" % (key, value))
        caps[key] = value
    declared = raw.get("max_concurrency")
    if declared is None:
        caps["max_concurrency"] = None
    elif isinstance(declared, bool) or not isinstance(declared, (int, float)) \
            or int(declared) < 1:
        raise ValueError(
            "worker capabilities max_concurrency 必须是 >= 1 的整数或 None"
            "（当前 %r）" % (declared,))
    else:
        caps["max_concurrency"] = int(declared)
    return caps


# ---------------------------------------------------------------------------
# 纯函数助手（无状态、可单测）
# ---------------------------------------------------------------------------

def _plugin_root() -> str:
    """插件根目录（workflow/adapters 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_plugin_root_on_path() -> None:
    """保证 ``import workflow.*`` 可用（worker_pool 被按文件路径直载时必需）。"""
    root = _plugin_root()
    if root not in sys.path:
        sys.path.insert(0, root)


def _scope_of(request: Dict[str, Any]) -> str:
    """派发范围：优先 scope，退化到 stage / role / batch_id。"""
    for key in ("scope", "stage", "role", "batch_id"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coerce_seconds(value: Any, fallback: float) -> float:
    """把 timeout_seconds 归一为正浮点秒；非法/非正值回落到 fallback。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(fallback)
    seconds = float(value)
    return seconds if seconds > 0 else float(fallback)


def _format_exc(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    return "%s: %s" % (name, text) if text else name


def _has_no_change_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in NO_CHANGE_MARKERS)


def _result_error_text(result: Dict[str, Any]) -> Optional[str]:
    """从结果 dict 中取出第一段非空错误文案。"""
    for key in ERROR_TEXT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_no_change(result: Any) -> bool:
    """结果是否表示「空转 / 无变更」。

    覆盖三种形态：
    1. 顶层 ``status`` ∈ {"no_change", "unchanged"}；
    2. ``dispatch_results`` 嵌套条目中的 status（run_dispatch 的真实返回形态）；
    3. 任意错误文案里含 unchanged / no change / 未发生变化。
    """
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    if isinstance(status, str) and status.strip().lower() in NO_CHANGE_STATUSES:
        return True
    nested = result.get("dispatch_results")
    if isinstance(nested, dict):
        for entry in nested.values():
            if isinstance(entry, dict):
                inner = entry.get("status")
                if isinstance(inner, str) and inner.strip().lower() in NO_CHANGE_STATUSES:
                    return True
    for key in ERROR_TEXT_KEYS:
        text = result.get(key)
        if isinstance(text, str) and _has_no_change_marker(text):
            return True
    return False


def _is_halt(result: Dict[str, Any]) -> bool:
    """run_dispatch 的失败收尾形态（stage=HALTED / routing_action=HALT）。"""
    routing = result.get("routing_action")
    stage = result.get("stage")
    return (isinstance(routing, str) and routing.strip().upper() == "HALT") or \
           (isinstance(stage, str) and stage.strip().upper() == "HALTED")


def _classify(result: Any) -> Tuple[str, Any, Optional[str]]:
    """把执行器返回值映射为 (status, output, error)。

    规则顺序（与规格一致）：
    1. dict 含 ``_error`` 键 → failed（LLM JSON 解析失败，瞬时，可重试）；
    2. 无变更 → no_change（不重试）；
    3. HALT 收尾 → failed（run_dispatch 的 missing_artifacts / worker_error）；
    4. 其余 → ok。
    """
    if isinstance(result, dict) and result.get("_error"):
        return STATUS_FAILED, None, str(result.get("_error"))
    if _is_no_change(result):
        return STATUS_NO_CHANGE, result, None
    if isinstance(result, dict) and _is_halt(result):
        return STATUS_FAILED, result, _result_error_text(result) or "派发被判定为 HALT"
    return STATUS_OK, result, None


def _first_positional_param(fn: Any) -> str:
    """取可调用对象的第一个位置参数名；``*args`` 或无参时返回 ""。"""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return ""
    for name, param in signature.parameters.items():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            return name
        if param.kind == param.VAR_POSITIONAL:
            return ""
    return ""


def _backoff_seconds(retry_index: int) -> float:
    """第 retry_index 次重试前的退避（0.05s、0.1s、……封顶 1s）。"""
    exponent = max(0, retry_index - 1)
    return min(RETRY_BACKOFF_MAX_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2 ** exponent))


def _worker_registry_available() -> bool:
    """worker_registry 单例是否已注册认知 worker 回调。

    惰性 import：既避开导入环，也让本模块在 langgraph 缺失时仍可导入——
    import 失败一律视为「不可用」（调用方随后会收到 deferred）。
    """
    try:
        _ensure_plugin_root_on_path()
        module = importlib.import_module("workflow.adapters.worker_registry")
        registry = getattr(module, "worker_registry", None)
    except Exception:
        return False
    return bool(getattr(registry, "available", False))


def _worker_registry_capabilities() -> Optional[Dict[str, Any]]:
    """读取 worker_registry 上已注册 worker 的能力声明。

    未注册 / 无声明 / 读取失败一律返回 None（调用方按保守默认串行处理）。
    """
    try:
        _ensure_plugin_root_on_path()
        module = importlib.import_module("workflow.adapters.worker_registry")
        registry = getattr(module, "worker_registry", None)
    except Exception:
        return None
    if registry is None or not getattr(registry, "available", False):
        return None
    declared = getattr(registry, "capabilities", None)
    if callable(declared):
        try:
            declared = declared()
        except Exception:
            return None
    return declared if isinstance(declared, dict) else None


def _default_dispatch_callable(request: Dict[str, Any]) -> Any:
    """默认执行器：惰性 import ``run_dispatch`` 并适配其真实签名。

    ``agent_nodes.run_dispatch(state, request, *, success_stage,
    success_history)`` 并非规格字面的单参 ``run_dispatch(request)`` 形态；
    这里按签名自适应：首参名为 ``state`` 时补齐最小 state 与两个关键字参数，
    否则按 ``run_dispatch(request)`` 直调（测试替身 / 未来重构均兼容）。
    """
    _ensure_plugin_root_on_path()
    from workflow.nodes.agent_nodes import run_dispatch

    if _first_positional_param(run_dispatch) == "state":
        state = {
            "self_dir": request.get("self_dir") or request.get("project_root") or ".",
            "project_root": request.get("project_root") or ".",
            "run_id": request.get("run_id", ""),
            "history": [],
            "dispatch_results": {},
        }
        return run_dispatch(
            state,
            request,
            success_stage=request.get("success_stage") or "DISPATCH_COMPLETED",
            success_history=request.get("success_history") or "",
        )
    return run_dispatch(request)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    """单个派发请求的最终结果。"""

    dispatch_id: str
    scope: str
    status: str  # ok | failed | timeout | no_change | deferred | error
    output: Any = None
    error: Optional[str] = None
    attempts: int = 0
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# WorkerPool
# ---------------------------------------------------------------------------

class WorkerPool:
    """有界并发派发执行器。

    线程安全：所有计数器与结果表由单一 ``threading.Lock`` 保护；
    ``max_in_flight`` 记录峰值并发执行数。

    能力感知（audit §17 任务8）：真实并行度 = min(档位并发, worker 声明的
    ``max_concurrency``)；未声明的 registry worker 按串行执行。join 截止
    时刻仍在执行的请求按 worker 是否声明 ``cancellable`` 分流为
    ``cancelled`` / ``timeout_deferred``——线程软超时不是取消，迟到结果
    一律进 orphans，调用方按失败 scope 重派。
    """

    def __init__(
        self,
        executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        max_concurrency: int = 4,
        default_timeout: int = 900,
        max_retries: int = 2,
        worker_capabilities: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) \
                or max_concurrency < 1:
            raise ValueError("max_concurrency 必须是 >= 1 的整数（当前 %r）" % (max_concurrency,))
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) \
                or max_retries < 0:
            raise ValueError("max_retries 必须是 >= 0 的整数（当前 %r）" % (max_retries,))
        if not isinstance(default_timeout, (int, float)) or isinstance(default_timeout, bool) \
                or float(default_timeout) <= 0:
            raise ValueError("default_timeout 必须是正数（当前 %r）" % (default_timeout,))

        self._executor = executor
        self._max_concurrency = int(max_concurrency)
        self._default_timeout = float(default_timeout)
        self._max_retries = int(max_retries)
        self._worker_capabilities_raw = (
            None if worker_capabilities is None
            else _normalize_capabilities(worker_capabilities))
        self._capabilities: Optional[Dict[str, Any]] = None

        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._generation = 0

        self._known: Set[str] = set()
        self._futures: Dict[str, Future] = {}
        self._results: Dict[str, DispatchResult] = {}
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._submitted_at: Dict[str, float] = {}
        self._deadlines: Dict[str, float] = {}
        self._attempts: Dict[str, int] = {}
        self._orphans: List[Dict[str, Any]] = []

        self._counts: Dict[str, int] = {key: 0 for key in STAT_KEYS}
        self._in_flight = 0
        self._max_in_flight = 0

    # -- 公开 API ----------------------------------------------------------

    @property
    def worker_capabilities(self) -> Dict[str, Any]:
        """归一化能力声明副本（首次 submit 前返回保守默认）。

        优先级（与 _resolve_capabilities 一致）：已解析 > 显式传入 > 显式
        executor 信任默认 > registry 声明 > 保守串行默认。计划节点建池后
        即查询此属性，因此 registry 声明必须在 submit 前可见。
        """
        if self._capabilities is not None:
            return dict(self._capabilities)
        if self._worker_capabilities_raw is not None:
            # 显式传入但尚未 submit：直接以显式声明作答（plan 节点建池后
            # 立即查询 effective_concurrency，不得被保守默认掩盖）
            return dict(self._worker_capabilities_raw)
        if self._executor is not None:
            return _normalize_capabilities(dict(_EXPLICIT_EXECUTOR_CAPABILITIES))
        registry_caps = _worker_registry_capabilities()
        if registry_caps is not None:
            return registry_caps
        return _normalize_capabilities(dict(_REGISTRY_FALLBACK_CAPABILITIES))

    @property
    def effective_concurrency(self) -> int:
        """真实并行度 = min(档位并发, worker 声明的 max_concurrency)。"""
        declared = self.worker_capabilities.get("max_concurrency")
        if declared is None:
            return self._max_concurrency
        return max(1, min(self._max_concurrency, int(declared)))

    def submit(self, request: Dict[str, Any]) -> str:
        """提交一个派发请求，返回 dispatch_id。

        - 无可用执行器（executor 为 None 且 worker_registry 未注册）→ 立即记为
          ``deferred``，不起线程；
        - 重复 dispatch_id → 不重复调度，保留首个，直接返回该 id。
        """
        if not isinstance(request, dict):
            raise TypeError("派发请求必须是 dict（当前 %s）" % type(request).__name__)

        dispatch_id = str(request.get("dispatch_id") or "").strip()
        if not dispatch_id:
            dispatch_id = "dsp-pool-%s" % uuid.uuid4().hex[:10]
        snapshot = dict(request)
        snapshot["dispatch_id"] = dispatch_id

        # 执行器与能力声明解析放在锁外：首次惰性 import 可能耗时（langgraph 链路）
        executor = self._resolve_executor()
        self._resolve_capabilities(executor)

        with self._lock:
            if dispatch_id in self._known:
                return dispatch_id  # 重复派发：保留首个，不重复调度
            self._known.add(dispatch_id)
            self._counts["submitted"] += 1
            now = time.monotonic()
            self._submitted_at[dispatch_id] = now
            self._requests[dispatch_id] = snapshot
            self._deadlines[dispatch_id] = now + _coerce_seconds(
                snapshot.get("timeout_seconds"), self._default_timeout)

            if executor is None:
                self._results[dispatch_id] = DispatchResult(
                    dispatch_id=dispatch_id,
                    scope=_scope_of(snapshot),
                    status=STATUS_DEFERRED,
                    error="无可用执行器（executor=None 且 worker_registry 未注册认知 worker），"
                          "已挂起交还调用方；WorkerPool 绝不调用 interrupt()",
                    attempts=0,
                    duration_ms=0,
                )
                self._counts["deferred"] += 1
                return dispatch_id

            future = self._ensure_pool_locked().submit(self._run, snapshot)
            self._futures[dispatch_id] = future
        return dispatch_id

    def join(self, timeout: Optional[float] = None) -> Dict[str, DispatchResult]:
        """等待派发收尾，返回 {dispatch_id: DispatchResult} 快照。

        ``timeout`` 为 None 时取「最晚的请求截止时刻」；截止时刻仍在执行的请求
        按 worker 能力声明标记为 ``cancelled``（声明可取消）或
        ``timeout_deferred``（线程不可抢占），其迟到结果被忽略并记入 orphans。
        在 ``reset()`` 之前可重复调用且结果一致（幂等）。
        """
        deadline = self._deadline_for(timeout)

        with self._lock:
            pending_futures = {
                did: future for did, future in self._futures.items()
                if did not in self._results
            }

        while pending_futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, not_done = wait(
                set(pending_futures.values()), timeout=remaining,
                return_when=FIRST_COMPLETED)
            if not done:
                break
            pending_futures = {
                did: future for did, future in pending_futures.items()
                if future in not_done
            }
            if not pending_futures:
                break

        self._finalize_timeouts(pending_futures)

        with self._lock:
            self._futures = {
                did: future for did, future in self._futures.items()
                if did not in self._results
            }
            return dict(self._results)

    def pending(self) -> int:
        """已提交但尚未定稿（无结果）的请求数。"""
        with self._lock:
            return max(0, len(self._known) - len(self._results))

    def stats(self) -> Dict[str, int]:
        """计数器快照。"""
        with self._lock:
            snapshot = dict(self._counts)
            snapshot["completed"] = len(self._results)
            snapshot["max_in_flight"] = self._max_in_flight
            snapshot["orphans"] = len(self._orphans)
        return snapshot

    def orphans(self) -> List[Dict[str, Any]]:
        """迟到 / 被 reset 丢弃的结果记录（附加观测接口）。"""
        with self._lock:
            return [dict(item) for item in self._orphans]

    def reset(self) -> None:
        """清空全部状态（executor 以 wait=False 关闭）。"""
        with self._lock:
            pool = self._pool
            self._pool = None
            self._generation += 1
            self._known.clear()
            self._futures.clear()
            self._results.clear()
            self._requests.clear()
            self._submitted_at.clear()
            self._deadlines.clear()
            self._attempts.clear()
            self._orphans.clear()
            self._counts = {key: 0 for key in STAT_KEYS}
            self._in_flight = 0
            self._max_in_flight = 0
            self._capabilities = None
        if pool is not None:
            pool.shutdown(wait=False)

    # -- 内部实现 ----------------------------------------------------------

    def _resolve_executor(self) -> Optional[Callable[[Dict[str, Any]], Any]]:
        """解析本次提交要用的执行器；无可用执行器返回 None（→ deferred）。"""
        if self._executor is not None:
            return self._executor
        if not _worker_registry_available():
            return None
        return _default_dispatch_callable

    def _ensure_pool_locked(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.effective_concurrency,
                thread_name_prefix=THREAD_NAME_PREFIX)
        return self._pool

    def _resolve_capabilities(
        self, executor: Optional[Callable[[Dict[str, Any]], Any]]
    ) -> Dict[str, Any]:
        """解析并缓存能力声明（首次 submit 时定稿；reset 后重新解析）。

        优先级：显式 ``worker_capabilities`` 参数 > 显式 executor 的信任默认
        （不节流）> worker_registry 声明 > 保守默认（串行）。
        """
        if self._capabilities is not None:
            return self._capabilities
        if self._worker_capabilities_raw is not None:
            caps = self._worker_capabilities_raw
        elif executor is not None:
            caps = _normalize_capabilities(dict(_EXPLICIT_EXECUTOR_CAPABILITIES))
        else:
            declared = _worker_registry_capabilities()
            caps = _normalize_capabilities(
                dict(declared) if declared is not None
                else dict(_REGISTRY_FALLBACK_CAPABILITIES))
        self._capabilities = caps
        return caps

    def _deadline_for(self, timeout: Optional[float]) -> float:
        if timeout is not None:
            return time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            deadlines = list(self._deadlines.values())
        return max(deadlines) if deadlines else time.monotonic()

    def _enter(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._in_flight += 1
            if self._in_flight > self._max_in_flight:
                self._max_in_flight = self._in_flight

    def _leave(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._in_flight > 0:
                self._in_flight -= 1

    def _note_attempt(self, generation: int, dispatch_id: str, attempts: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._attempts[dispatch_id] = attempts

    def _invoke(self, request: Dict[str, Any]) -> Any:
        executor = self._resolve_executor()
        if executor is None:
            raise RuntimeError(
                "WorkerPool 无可用执行器（executor=None 且 worker_registry 未注册）")
        return executor(request)

    def _run(self, request: Dict[str, Any]) -> None:
        """线程体：带瞬时重试策略地执行一个派发请求并定稿结果。"""
        dispatch_id = str(request.get("dispatch_id") or "")
        scope = _scope_of(request)
        generation = self._generation
        started = self._submitted_at.get(dispatch_id, time.monotonic())
        attempts = 0
        retries = 0
        status, output, error = STATUS_FAILED, None, None

        self._enter(generation)
        try:
            retry_class = str(request.get("retry_class") or TRANSIENT_RETRY_CLASS).strip().lower()
            attempts_allowed = (
                1 + self._max_retries if retry_class == TRANSIENT_RETRY_CLASS else 1
            )
            soft_timeout = _coerce_seconds(
                request.get("timeout_seconds"), self._default_timeout)

            while True:
                attempts += 1
                self._note_attempt(generation, dispatch_id, attempts)
                try:
                    result = self._invoke(request)
                except Exception as exc:  # 瞬时故障：可重试
                    status, output, error = STATUS_FAILED, None, _format_exc(exc)
                else:
                    status, output, error = _classify(result)
                # ok / no_change 立即收敛；否则在 attempts_allowed 用尽前重试
                if status in (STATUS_OK, STATUS_NO_CHANGE) \
                        or attempts >= attempts_allowed:
                    break
                retries += 1
                with self._lock:
                    self._counts["retried"] += 1
                time.sleep(_backoff_seconds(retries))

            # 软时限：线程无法被抢占，只做标记与计数，分类结果照旧保留；
            # 真正的截止由 join(timeout) 强制。
            if status != STATUS_NO_CHANGE and (time.monotonic() - started) > soft_timeout:
                status = STATUS_TIMEOUT
                error = error or _SOFT_TIMEOUT_ERROR
        except Exception as exc:  # 池自身故障：不得静默丢失
            status, output, error = STATUS_ERROR, None, _format_exc(exc)
        finally:
            self._leave(generation)

        duration_ms = int((time.monotonic() - started) * 1000)
        self._record(generation, DispatchResult(
            dispatch_id=dispatch_id,
            scope=scope,
            status=status,
            output=output,
            error=error,
            attempts=attempts,
            duration_ms=duration_ms,
        ))

    def _record(self, generation: int, result: DispatchResult) -> None:
        """定稿结果；reset 之后或已定稿的迟到结果转入 orphans。"""
        dispatch_id = result.dispatch_id
        with self._lock:
            stale = generation != self._generation
            already = dispatch_id in self._results
            if stale or already:
                self._orphans.append({
                    "dispatch_id": dispatch_id,
                    "scope": result.scope,
                    "status": result.status,
                    "attempts": result.attempts,
                    "duration_ms": result.duration_ms,
                    "reason": "reset" if stale else "already_finalized",
                    "error": result.error,
                })
                return
            self._results[dispatch_id] = result
            self._bump_locked(result.status)

    def _finalize_timeouts(self, pending_futures: Dict[str, Future]) -> None:
        """把截止时刻仍在执行的请求标记为终态（迟到结果稍后进 orphans）。

        超时语义按 worker 能力声明分流（audit §17 任务8 / §12.2）：
        声明 ``cancellable`` → ``cancelled``（worker 契约性停止，调用方重派）；
        未声明 → ``timeout_deferred``（线程不可抢占，结果延后；迟到结果仍按
        ``already_finalized`` 进 orphans，绝不被静默复用）。
        """
        now = time.monotonic()
        cancellable = bool((self._capabilities or {}).get("cancellable"))
        for dispatch_id, future in pending_futures.items():
            future.cancel()
            with self._lock:
                if dispatch_id in self._results:
                    continue
                started = self._submitted_at.get(dispatch_id, now)
                if cancellable:
                    status, error = STATUS_CANCELLED, _CANCEL_REQUESTED_ERROR
                else:
                    status, error = STATUS_TIMEOUT_DEFERRED, _JOIN_TIMEOUT_ERROR
                self._results[dispatch_id] = DispatchResult(
                    dispatch_id=dispatch_id,
                    scope=_scope_of(self._requests.get(dispatch_id, {})),
                    status=status,
                    error=error,
                    attempts=self._attempts.get(dispatch_id, 0),
                    duration_ms=int((now - started) * 1000),
                )
                self._bump_locked(status)

    def _bump_locked(self, status: str) -> None:
        if status in (STATUS_FAILED, STATUS_ERROR):
            self._counts["failed"] += 1
        elif status == STATUS_OK:
            self._counts["ok"] += 1
        elif status in (STATUS_TIMEOUT, STATUS_TIMEOUT_DEFERRED):
            self._counts["timeout"] += 1
        elif status == STATUS_CANCELLED:
            self._counts["cancelled"] += 1
        elif status == STATUS_NO_CHANGE:
            self._counts["no_change"] += 1
        elif status == STATUS_DEFERRED:
            self._counts["deferred"] += 1
