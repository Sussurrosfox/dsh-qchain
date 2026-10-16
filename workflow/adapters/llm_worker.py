"""llm_worker.py — Adapter for invoking role-based cognitive workers.
Loads system prompts from prompts/ directory.
"""

import json
import os
from typing import Dict, Any, Optional, Callable


class LLMWorkerAdapter:
    def __init__(self, prompts_dir: Optional[str] = None):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.prompts_dir = prompts_dir or os.path.abspath(os.path.join(current_dir, "..", "..", "prompts"))
        self.model_callable: Optional[Callable[[str, str], str]] = None

    def set_model_callable(self, fn: Callable[[str, str], str]) -> None:
        self.model_callable = fn

    def load_prompt(self, prompt_filename: str) -> str:
        path = os.path.join(self.prompts_dir, prompt_filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def run_worker_text(
        self,
        role: str,
        prompt_file: str,
        user_input: str,
        fallback_text: Optional[str] = None
    ) -> str:
        sys_prompt = self.load_prompt(prompt_file)
        if self.model_callable:
            return self.model_callable(sys_prompt, user_input)
        if fallback_text is not None:
            return fallback_text
        return f"<!-- Unconfigured worker for role: {role} -->"

    def run_worker_json(
        self,
        role: str,
        prompt_file: str,
        user_input: str,
        fallback_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        text = self.run_worker_text(role, prompt_file, user_input)
        try:
            clean = text.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            if clean.startswith("```"):
                clean = clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            return json.loads(clean.strip())
        except Exception as e:
            if fallback_data is not None:
                return fallback_data
            return {"_error": f"Failed to parse LLM JSON output: {e}", "raw": text}
