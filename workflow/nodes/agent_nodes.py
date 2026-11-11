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
    attempt: int = 0,
    digest_context: Optional[Dict[str, Any]] = None,
    scopes: Optional[List[str]] = None,
    scope_inputs: Optional[Dict[str, str]] = None,
    artifact_scopes: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Build a dispatch request carrying the FIXED P0 contract.

    Every request dict gains: batch_id, scope, input_digest, timeout_seconds,
    max_retries, retry_class, attempt, status (alongside the pre-existing
    dispatch_id/stage/role/artifacts/before/instruction/command/context/
    prompt_file/run_id keys). All new params are keyword-only with defaults so
    existing callers keep working unchanged.

    digest_context (C7): 可选指纹补料。仅当调用方显式传入时追加进 input_digest
    的载荷（图版本 / 执行策略摘要 / 来源清单摘要 / 提示词版本等"请求文本之外
    但决定产物"的键）；传 None 时载荷逐字节与旧实现一致，旧缓存键不变。

    scopes (C5)：一个作者 agent 允许一次写 1–2 个相邻节。传 ``scopes=[sec1,
    sec2]`` 时产出 v2 派发单（``schema_version=2`` / ``scopes`` /
    ``scope_inputs`` / ``artifact_scopes``），``scope`` 退化为 first-scope
    兼容标签；**不传或只有一个 scope 时逐字节保持旧单 scope 契约**（旧派发单、
    旧缓存键、T2_CHECK / T4 一节一请求全部不受影响）。
    """
    root = state.get("self_dir") or state.get("project_root", ".")
    stage_slug = stage.lower().replace("_", "-")

    # --- C5：多 scope 正规化（整条链路唯一真源见 scripts/request_scopes.py）--
    scope_list: List[str] = []
    for item in (scopes or []):
        if isinstance(item, str) and item.strip() and item.strip() not in scope_list:
            scope_list.append(item.strip())
    multi_scope = len(scope_list) >= 2
    if multi_scope and not scope:
        scope = scope_list[0]      # first-scope 兼容标签
    if multi_scope:
        inputs = {str(k): str(v) for k, v in (scope_inputs or {}).items()
                  if isinstance(k, str) and isinstance(v, str) and v}
        mapping = {str(k): str(v) for k, v in (artifact_scopes or {}).items()
                   if isinstance(k, str) and isinstance(v, str) and k and v}
        if not mapping and len(artifacts or []) == len(scope_list):
            mapping = {rel: sec for rel, sec in zip(list(artifacts), scope_list)}
        missing_inputs = [s for s in scope_list if s not in inputs]
        if missing_inputs:
            # C5：v2 派发单必须逐节携带来源指纹。缺失时宁可炸掉，也不能签发
            # 一张"无法追溯是拿哪版来源写的"的合批单（也避免伪造空摘要）。
            raise ValueError(
                "多 scope 派发单缺少 scope_inputs: %s（C5 要求逐节来源指纹）"
                % ",".join(missing_inputs))

    if input_digest is None:
        # 确定性输入指纹：同一组 (stage, role, scope, instruction, command,
        # context, artifacts, prompt_file) 必须得到同一 digest；
        # context 经 sort_keys 归一化，artifacts 保序序列化。
        digest_fields: List[Any] = [
            stage,
            role,
            scope,
            instruction,
            command,
            json.dumps(context or {}, sort_keys=True, ensure_ascii=False),
            list(artifacts),
            prompt_file,
        ]
        if digest_context is not None:
            # C7：决定产物但不体现在请求文本里的键（图版本、策略摘要、来源清单
            # 摘要、提示词版本…）必须进入指纹，否则"换图/换来源却撞旧缓存"。
            digest_fields.append(
                json.dumps(digest_context, sort_keys=True, ensure_ascii=False))
        if multi_scope:
            # C5：合批请求的来源切片必须进入指纹——否则只改第二节的来源却撞
            # 旧缓存键，会把上一版的产物当成这一版的结果。
            digest_fields.append(json.dumps(scope_list, ensure_ascii=False))
            digest_fields.append(
                json.dumps(inputs, sort_keys=True, ensure_ascii=False))
            digest_fields.append(
                json.dumps(mapping, sort_keys=True, ensure_ascii=False))
        digest_payload = json.dumps(digest_fields, ensure_ascii=False)
        input_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    if batch_id is None:
        batch_id = f"bat-{stage_slug}-{uuid.uuid4().hex[:8]}"
    scope_suffix = f"-{scope}" if scope else ""
    if multi_scope:
        scope_suffix = "-" + "".join(s[3:] or s for s in scope_list)
    dispatch_id = f"dsp-{stage_slug}{scope_suffix}-{uuid.uuid4().hex[:10]}"
    request: Dict[str, Any] = {
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
    if multi_scope:
        request["schema_version"] = 2
        request["scopes"] = list(scope_list)
        request["scope_inputs"] = inputs
        request["artifact_scopes"] = mapping
    return request


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
        # 逐尝试调度策略（卡09）：单请求派发必须与批次载荷
        # （_batch_interrupt_payload / _t4_batch_interrupt_payload）同键，
        # 否则宿主侧拿不到超时与重试分型，只能吃默认值。
        "input_digest": request.get("input_digest"),
        "timeout_seconds": request.get("timeout_seconds"),
        "max_retries": request.get("max_retries"),
        "retry_class": request.get("retry_class"),
        # 派发协议三件套（q_audit.md 根因治理）：收到 AGENT_DISPATCH 必须调
        # qchain_dispatch 派发宿主子 Agent，严禁主 Agent 单会话直写产物。
        # 卡20：单请求入口附 request_ref 调用参数（不含 batch_id）。
        **agent_evidence.dispatch_protocol_block(
            request_ref=f"{REQUEST_DIR_REL}/{request['dispatch_id']}.json",
        ),
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
    #
    # A16 例外：子 Agent 确实跑了、也写了盘，只是写出来的内容和原来逐字节相同
    # （dependency replan 逐字重写同一份 dependency-map.md 就是这种）。此时
    # 「无变化」是本次派发的真实结果而不是空转，按证据台账认证后放行——否则
    # 会被判 no_change → HALTED，而 resume 从 HALTED 是 no-op，只能删 checkpoint。
    if unchanged and len(unchanged) == len(artifacts):
        attested, evidence = agent_evidence.no_change_is_attested(root, request, after)
        if not attested:
            detail = f"派发 {dispatch_id} 的产物未发生变化: {', '.join(unchanged)}"
            results[dispatch_id] = {
                "status": "no_change", "unchanged": unchanged, "dispatched_via": dispatched_via
            }
            history.append(f"Agent[{request['stage']}]: {detail}")
            return _dispatch_failure(state, results, history, success_stage, detail)
        worker = str((evidence or {}).get("worker") or "").strip() or "unknown"
        history.append(
            f"Agent[{request['stage']}]: 产物无变化，但已由 {worker} 认证"
            f"（{', '.join(unchanged)} 逐字节重写，replan 类合法空转）"
        )
        entry_no_change: Dict[str, Any] = {
            "status": "success",
            "produced": [rel for rel in artifacts if after[rel] is not None],
            "dispatched_via": dispatched_via,
            "no_change": True,
            "no_change_attested_by": worker,
            "unchanged": list(unchanged),
        }
        results[dispatch_id] = entry_no_change
        return {
            "stage": success_stage,
            "routing_action": "PROCEED",
            "dispatch_results": results,
            "history": history + [success_history],
            "pending_dispatch": None,
        }

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
