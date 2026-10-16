"""agent_nodes.py — Cognitive-stage dispatch utilities for Shrink Courseware Pipeline.
Manages dispatch requests, snapshots, worker invocation, and LangGraph native interrupts.
"""

import hashlib
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from langgraph.types import interrupt

from ..state import ShrinkCoursewareState
from ..adapters.llm_worker import LLMWorkerAdapter
from ..adapters.worker_registry import worker_registry

REQUEST_DIR_REL = ".pd/agent-requests"


def sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def snapshot(root: str, rel_paths: List[str]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for rel in rel_paths:
        out[rel] = sha256_file(os.path.join(root, rel.replace("/", os.sep)))
    return out


def _safe_join(root: str, rel: str) -> Optional[str]:
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    abs_path = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), abs_path]) != os.path.abspath(root):
        return None
    return abs_path


def build_request(
    state: ShrinkCoursewareState,
    *,
    stage: str,
    role: str,
    prompt_file: str,
    artifacts: List[str],
    instruction: str,
    command: str,
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    root = state.get("self_dir") or state.get("project_root", ".")
    dispatch_id = f"dsp-{stage.lower().replace('_', '-')}-{uuid.uuid4().hex[:10]}"
    return {
        "dispatch_id": dispatch_id,
        "stage": stage,
        "role": role,
        "prompt_file": prompt_file,
        "artifacts": list(artifacts),
        "instruction": instruction,
        "command": command,
        "context": context or {},
        "run_id": state.get("run_id", ""),
        "before": snapshot(root, artifacts),
    }


def write_request_file(root: str, request: Dict[str, Any]) -> str:
    rel = f"{REQUEST_DIR_REL}/{request['dispatch_id']}.json"
    abs_path = _safe_join(root, rel)
    if abs_path is None:
        return ""
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return rel


def _write_worker_files(root: str, request: Dict[str, Any], files: Dict[str, Any]) -> List[str]:
    written: List[str] = []
    allowed = set(request.get("artifacts", []))
    if not isinstance(files, dict):
        return written
    for rel, content in files.items():
        if rel not in allowed:
            continue
        abs_path = _safe_join(root, rel)
        if abs_path is None:
            continue
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        tmp = f"{abs_path}.tmp-{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, abs_path)
        written.append(rel)
    return written


def _interrupt_payload(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "AGENT_DISPATCH",
        "dispatch_id": request["dispatch_id"],
        "stage": request["stage"],
        "role": request["role"],
        "description": f"认知工位派发：{request['instruction']}。请生成或编辑目标产物后继续执行。",
        "artifacts": request["artifacts"],
        "instruction": request["instruction"],
        "command": request["command"],
        "prompt_file": f"prompts/{request['prompt_file']}",
        "request_ref": f"{REQUEST_DIR_REL}/{request['dispatch_id']}.json",
        "before_digests": request["before"],
        "context": request.get("context", {}),
    }


def _dispatch_failure(
    state: ShrinkCoursewareState,
    results: Dict[str, Any],
    history: List[str],
    stage: str,
    detail: str
) -> Dict[str, Any]:
    return {
        "stage": "HALTED",
        "routing_action": "HALT",
        "error_message": detail,
        "dispatch_results": results,
        "history": history,
    }


def run_dispatch(
    state: ShrinkCoursewareState,
    request: Dict[str, Any],
    *,
    success_stage: str,
    success_history: str
) -> Dict[str, Any]:
    root = state.get("self_dir") or state.get("project_root", ".")
    dispatch_id = request["dispatch_id"]
    artifacts: List[str] = request["artifacts"]
    history = list(state.get("history", []))
    results = dict(state.get("dispatch_results", {}))

    dispatched_via = "worker" if worker_registry.available else "interrupt"

    if worker_registry.available:
        adapter = LLMWorkerAdapter()
        adapter.set_model_callable(worker_registry.call)
        user_content = (
            f"{request['instruction']}\n\n## 项目路径\n{root}\n\n## 派发前产物摘要\n"
            f"{json.dumps(request['before'], ensure_ascii=False, indent=2)}\n\n"
            f"## 附加上下文\n{json.dumps(request.get('context', {}), ensure_ascii=False, indent=2)}\n\n"
            "请以 JSON 返回：{\"files\": {\"<相对路径>\": \"<完整文件内容>\"}}，只允许写入上面声明的产物。"
        )
        output = adapter.run_worker_json(
            request["role"], request["prompt_file"], user_content, fallback_data=None
        )
        if isinstance(output, dict) and output.get("_error"):
            err_msg = output["_error"]
            results[dispatch_id] = {
                "status": "worker_error", "detail": err_msg, "dispatched_via": dispatched_via
            }
            history.append(f"Agent[{request['stage']}]: worker 调用失败: {err_msg}")
            return _dispatch_failure(state, results, history, success_stage, f"认知 worker 调用失败: {err_msg}")
        if isinstance(output, dict):
            _write_worker_files(root, request, output.get("files") or {})
    else:
        payload = _interrupt_payload(request)
        history.append(
            f"Agent[{request['stage']}]: 挂起等待人工 / Antigravity 产出 {', '.join(artifacts)}"
        )
        response = interrupt(payload)
        if not isinstance(response, dict):
            response = {}
        if response.get("files"):
            _write_worker_files(root, request, response["files"])

    # Verify artifacts
    after = snapshot(root, artifacts)
    missing = [rel for rel in artifacts if after[rel] is None]
    unchanged = [
        rel for rel in artifacts
        if after[rel] is not None and after[rel] == request["before"].get(rel)
    ]

    if missing:
        detail = f"派发 {dispatch_id} 的产物未生成: {', '.join(missing)}"
        results[dispatch_id] = {
            "status": "missing_artifacts", "missing": missing, "dispatched_via": dispatched_via
        }
        history.append(f"Agent[{request['stage']}]: {detail}")
        return _dispatch_failure(state, results, history, success_stage, detail)

    # 无变更空转校验全模式生效（B2）：worker 与 interrupt 两种派发路径统一执行。
    # interrupt 模式是生产唯一真实路径，旧产物不得被静默接受；
    # 全部 artifacts 未变更即判失败，部分变更仍放行。
    if unchanged and len(unchanged) == len(artifacts):
        detail = f"派发 {dispatch_id} 的产物未发生变化: {', '.join(unchanged)}"
        results[dispatch_id] = {
            "status": "no_change", "unchanged": unchanged, "dispatched_via": dispatched_via
        }
        history.append(f"Agent[{request['stage']}]: {detail}")
        return _dispatch_failure(state, results, history, success_stage, detail)

    results[dispatch_id] = {
        "status": "success",
        "produced": [rel for rel in artifacts if after[rel] is not None],
        "dispatched_via": dispatched_via,
    }
    history.append(success_history)
    return {
        "stage": success_stage,
        "routing_action": "PROCEED",
        "dispatch_results": results,
        "history": history,
        "pending_dispatch": None,
    }
