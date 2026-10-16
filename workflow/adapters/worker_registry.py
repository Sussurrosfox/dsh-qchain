"""worker_registry.py — Process-wide registry for the cognitive worker callable.
"""

from typing import Callable, Dict, Any, Optional


class WorkerRegistry:
    def __init__(self) -> None:
        self._model_callable: Optional[Callable[[str, str], str]] = None
        self._worker_name: Optional[str] = None

    def set(self, fn: Callable[[str, str], str], name: str = "external-llm") -> None:
        """Register a callable (system_prompt, user_content) -> response_text."""
        if not callable(fn):
            raise TypeError("worker callable must be callable")
        self._model_callable = fn
        self._worker_name = name

    def clear(self) -> None:
        self._model_callable = None
        self._worker_name = None

    @property
    def available(self) -> bool:
        return self._model_callable is not None

    @property
    def name(self) -> str:
        return self._worker_name or "none"

    def call(self, system_prompt: str, user_content: str) -> str:
        if self._model_callable is None:
            raise RuntimeError("no cognitive worker registered")
        return self._model_callable(system_prompt, user_content)

    def describe(self) -> Dict[str, Any]:
        return {"worker_available": self.available, "worker_name": self.name}


worker_registry = WorkerRegistry()
