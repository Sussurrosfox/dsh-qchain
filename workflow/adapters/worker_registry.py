"""worker_registry.py — Process-wide registry for the cognitive worker callable.

能力声明（audit §17 任务8 / §16）：
注册认知 worker 时必须（或可以）声明其并发能力，供 WorkerPool 决定
真实并行度与超时语义——

    {
        "sync": True,            # 同步调用（线程池直接执行）
        "async": False,          # 异步调用（由宿主自行调度）
        "cancellable": False,    # 是否支持协作式取消
        "max_concurrency": 1,    # 同一时刻可并行的最大调用数
    }

红线：未声明能力的 worker 一律按「同步、不可取消、最大并发 1」处理——
不得让多线程共享一个未确认线程安全的同步 worker（audit §16）；
只有声明 ``max_concurrency > 1`` 的 worker 才真正获得并行派发。
"""

from typing import Any, Callable, Dict, Optional

__all__ = ["DEFAULT_CAPABILITIES", "normalize_capabilities", "WorkerRegistry",
           "worker_registry"]

#: 未声明能力时的保守默认值（线程安全未确认 → 串行）。
DEFAULT_CAPABILITIES: Dict[str, Any] = {
    "sync": True,
    "async": False,
    "cancellable": False,
    "max_concurrency": 1,
}

_CAP_KEYS = ("sync", "async", "cancellable", "max_concurrency")


def _as_bool(value: Any, key: str, fallback: bool) -> bool:
    """布尔能力声明：bool 原样、0/1 与真值串 coercion，其余一律 ValueError。

    与 worker_pool._normalize_capabilities 同口径：畸形值不静默回落
    （audit §10：不得以「宽松解析」掩盖错误的并行度承诺）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ValueError(
        "worker capabilities %s 必须是布尔值（当前 %r）" % (key, value))


def _as_concurrency(value: Any, fallback: int) -> int:
    """并发度声明：必须是 >= 1 的整数；bool/非数值/<1 一律 ValueError。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            "worker capabilities max_concurrency 必须是 >= 1 的整数"
            "（当前 %r）" % (value,))
    return value


def normalize_capabilities(
    raw: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """归一化能力声明：缺键回落保守默认值，畸形值不静默放行。"""
    caps = dict(DEFAULT_CAPABILITIES)
    if raw is None:
        return caps
    if not isinstance(raw, dict):
        raise TypeError(
            "worker capabilities 必须是 dict（当前 %s）" % type(raw).__name__)
    unknown = sorted(set(raw) - set(_CAP_KEYS))
    if unknown:
        raise ValueError(
            "worker capabilities 含未知键: %s（合法键 %s）"
            % (",".join(unknown), ",".join(_CAP_KEYS)))
    for key in ("sync", "async", "cancellable"):
        if key in raw:
            caps[key] = _as_bool(raw[key], key, caps[key])
    if "max_concurrency" in raw:
        caps["max_concurrency"] = _as_concurrency(
            raw["max_concurrency"], caps["max_concurrency"])
    return caps


class WorkerRegistry:
    def __init__(self) -> None:
        self._model_callable: Optional[Callable[[str, str], str]] = None
        self._worker_name: Optional[str] = None
        self._capabilities: Dict[str, Any] = dict(DEFAULT_CAPABILITIES)

    def set(
        self,
        fn: Callable[[str, str], str],
        name: str = "external-llm",
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a callable (system_prompt, user_content) -> response_text.

        ``capabilities`` 见模块 docstring；缺省按保守默认（同步/不可取消/
        最大并发 1）——并行派发是 worker 显式声明的能力，不是默认赠礼。
        """
        if not callable(fn):
            raise TypeError("worker callable must be callable")
        self._model_callable = fn
        self._worker_name = name
        self._capabilities = normalize_capabilities(capabilities)

    def clear(self) -> None:
        self._model_callable = None
        self._worker_name = None
        self._capabilities = dict(DEFAULT_CAPABILITIES)

    @property
    def available(self) -> bool:
        return self._model_callable is not None

    @property
    def name(self) -> str:
        return self._worker_name or "none"

    @property
    def capabilities(self) -> Dict[str, Any]:
        """当前 worker 的能力声明（未注册时返回保守默认值的副本）。"""
        return dict(self._capabilities)

    def call(self, system_prompt: str, user_content: str) -> str:
        if self._model_callable is None:
            raise RuntimeError("no cognitive worker registered")
        return self._model_callable(system_prompt, user_content)

    def describe(self) -> Dict[str, Any]:
        return {
            "worker_available": self.available,
            "worker_name": self.name,
            "worker_capabilities": self.capabilities,
        }


worker_registry = WorkerRegistry()
