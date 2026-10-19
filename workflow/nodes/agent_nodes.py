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
from . import agent_evidence

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
    context: Optional[Dict[str, Any]] = None,
    scope: str = "",
    input_digest: Optional[str] = None,
    timeout_seconds: int = 900,
    max_retries: int = 2,
    retry_class: str = "transient",
    batch_id: Optional[str] = None,
    status: str = "pending",
    attempt: int = 0
) -> Dict[str, Any]:
    """Build a dispatch request carrying the FIXED P0 contract.

    Every request dict gains: batch_id, scope, input_digest, timeout_seconds,
    max_retries, retry_class, attempt, status (alongside the pre-existing
    dispatch_id/stage/role/artifacts/before/instruction/command/context/
    prompt_file/run_id keys). All new params are keyword-only with defaults so
    existing callers keep working unchanged.
    """
    root = state.get("self_dir") or state.get("project_root", ".")
    stage_slug = stage.lower().replace("_", "-")
    if input_digest is None:
        # 确定性输入指纹：同一组 (stage, role, scope, instruction, command,
        # context, artifacts, prompt_file) 必须得到同一 digest；
        # context 经 sort_keys 归一化，artifacts 保序序列化。
        digest_payload = json.dumps(
            [
                stage,
                role,
                scope,
                instruction,
                command,
                json.dumps(context or {}, sort_keys=True, ensure_ascii=False),
                list(artifacts),
                prompt_file,
            ],
            ensure_ascii=False
        )
        input_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    if batch_id is None:
        batch_id = f"bat-{stage_slug}-{uuid.uuid4().hex[:8]}"
    scope_suffix = f"-{scope}" if scope else ""
    dispatch_id = f"dsp-{stage_slug}{scope_suffix}-{uuid.uuid4().hex[:10]}"
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
        "batch_id": batch_id,
        "scope": scope,
        "input_digest": input_digest,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "retry_class": retry_class,
        "attempt": attempt,
        "status": status,
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


def _record_dispatch_evidence(
    root: str,
    request: Dict[str, Any],
    *,
    worker: str,
    written: List[str],
) -> str:
    """派发完成时原子落一份证据台账（.pd/agent-calls/<dispatch_id>.json）。

    产物写盘后立即计算 sha256；dispatch_id 与 .pd/agent-requests/<id>.json
    一致，供 _verify_requests_from_disk / check_agent_audit.py 对账。写入失败
    不阻断派发（返回 ""），由验证侧按 ok_unattested 判级。
    """
    digests = snapshot(root, written)
    return agent_evidence.write_evidence(
        root,
        dispatch_id=request["dispatch_id"],
        batch_id=request.get("batch_id"),
        worker=worker,
        artifacts_written=list(written),
        output_digests=digests,
    )


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
        # 派发协议三件套（q_audit.md 根因治理）：收到 AGENT_DISPATCH 必须调
        # qchain_dispatch 派发宿主子 Agent，严禁主 Agent 单会话直写产物。
        **agent_evidence.dispatch_protocol_block(),
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
    req_disk_path = os.path.join(root, ".pd", "agent-requests", f"{dispatch_id}.json")
    if os.path.isfile(req_disk_path):
        try:
            with open(req_disk_path, "r", encoding="utf-8") as f:
                disk_req = json.load(f)
                if isinstance(disk_req, dict) and disk_req.get("artifacts"):
                    request = disk_req
        except Exception:
            pass
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
            written = _write_worker_files(root, request, output.get("files") or {})
            # 进程内认知 worker 路径自证：worker 身份 = llm-worker:<注册名>
            # （宿主 subagent 路径的证据由 qchain_dispatch 落盘）。
            if written:
                _record_dispatch_evidence(
                    root, request,
                    worker=f"llm-worker:{worker_registry.name}", written=written)
    else:
        payload = _interrupt_payload(request)
        history.append(
            f"Agent[{request['stage']}]: 挂起等待人工 / Antigravity 产出 {', '.join(artifacts)}"
        )
        response = interrupt(payload)
        if not isinstance(response, dict):
            response = {}
        # 恢复侧防伪造：中断响应必须经 qchain_dispatch（host-subagent-v1）回传；
        # 旧 checkpoint 无该字段（None）不拦截。
        violation = agent_evidence.dispatch_protocol_violation(response)
        if violation:
            results[dispatch_id] = {
                "status": "bad_dispatch_protocol", "dispatched_via": dispatched_via
            }
            history.append(f"Agent[{request['stage']}]: {violation}")
            return _dispatch_failure(state, results, history, success_stage, violation)
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

    # 证据链对账（q_audit.md 根因治理）：产物变化必须由
    # .pd/agent-calls/<dispatch_id>.json 的写入者身份背书；无证据 = 主线程
    # 直写嫌疑，按 allow_unattested 逃生阀判级（attested_mismatch 无逃生阀）。
    policy = agent_evidence.load_dispatch_policy(root)
    attest_status, _evidence = agent_evidence.check_request_attested(
        root, request, after, allow_unattested=policy["allow_unattested"])
    if attest_status == "attested_mismatch":
        detail = (f"派发 {dispatch_id} 的产物与代理证据不符（证据在案但摘要对不上，"
                  f"疑似证据落盘后产物被改写）")
        results[dispatch_id] = {
            "status": "attested_mismatch", "dispatched_via": dispatched_via
        }
        history.append(f"Agent[{request['stage']}]: {detail}")
        return _dispatch_failure(state, results, history, success_stage, detail)
    if attest_status == "ok_unattested" and not policy["allow_unattested"]:
        detail = (f"派发 {dispatch_id} 的产物缺少代理证据（应由 qchain_dispatch "
                  f"派发子 Agent 完成）")
        results[dispatch_id] = {
            "status": "ok_unattested", "dispatched_via": dispatched_via
        }
        history.append(f"Agent[{request['stage']}]: {detail}")
        return _dispatch_failure(state, results, history, success_stage, detail)

    entry: Dict[str, Any] = {
        "status": "success",
        "produced": [rel for rel in artifacts if after[rel] is not None],
        "dispatched_via": dispatched_via,
    }
    if attest_status == "ok":
        entry["verified_via"] = "agent"
    elif attest_status == "ok_unattested":
        # allow_unattested=True 放行：保留 unattested 标记供交付门禁终裁
        entry["unattested"] = True
    results[dispatch_id] = entry
    history.append(success_history)
    return {
        "stage": success_stage,
        "routing_action": "PROCEED",
        "dispatch_results": results,
        "history": history,
        "pending_dispatch": None,
    }
