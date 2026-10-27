"""section_nodes.py — T2 Section authoring, attempt contract, and assembly.
Enforces the 'one section, one author' boundary, pdstate.py start/submit/mark flow,
and main.tex assembly.
"""

import json
import os
import re
from typing import Dict, Any, List, Optional
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner
from .agent_nodes import build_request, write_request_file, run_dispatch


#: 任务3（audit §17 任务3）：工作集外小节的终态标记。reused=本轮复用
#: 已有产物，skipped=本轮跳过（未生成也不伪装完成）；两者都不是 pending，
#: 不得再进入派发/提交路径（否则 focused/edit 选择会被串行路径覆盖）。
SKIPPED_STATUSES = ("reused", "skipped")


def _get_next_pending_section(
    expected_sections: List[str],
    section_statuses: Dict[str, str]
) -> Optional[str]:
    """Find the first section that is not yet 'checked' or 'accepted'."""
    for sec in expected_sections:
        status = section_statuses.get(sec, "planned")
        if status not in ("checked", "accepted") and status not in SKIPPED_STATUSES:
            return sec
    return None


def _ensure_task_ready_and_start(
    self_dir: str,
    active_sec: str,
    runner: ScriptRunner
) -> tuple[int, str, str]:
    """确保切片在 run.json 中注册且处于 ready 状态，然后启动独占 attempt。"""
    run_json_path = os.path.join(self_dir, ".pd", "run.json")
    if os.path.isfile(run_json_path):
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                run_data = json.load(f)

            if "tasks" in run_data and active_sec not in run_data["tasks"]:
                if "slices" in run_data and active_sec not in run_data["slices"]:
                    run_data["slices"].append(active_sec)
                run_data["tasks"][active_sec] = {
                    "task_id": active_sec,
                    "status": "planned",
                    "attempts": [],
                    "current_attempt": None,
                    "input_sources": None,
                    "input_digest": None,
                    "usage": {}
                }
                with open(run_json_path, "w", encoding="utf-8") as f:
                    json.dump(run_data, f, ensure_ascii=False, indent=2)

            cur_status = run_data.get("tasks", {}).get(active_sec, {}).get("status")
            if cur_status != "ready":
                mc, mo, me = runner.run_script("pdstate.py", [self_dir, "mark", active_sec, "ready"])
                if mc != 0:
                    pass
        except Exception:
            pass

    return runner.run_script("pdstate.py", [self_dir, "start", active_sec])


def node_t2_request_sections(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2: Identify next pending section and dispatch authoring task with pdstate start."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = state.get("expected_sections", ["sec1"])
    statuses = dict(state.get("section_statuses", {}))
    history = list(state.get("history", []))
    runner = ScriptRunner(self_dir)

    active_sec = _get_next_pending_section(expected_sections, statuses)
    if active_sec is None:
        # All sections done!
        history.append("T2: 所有小节切片均已完成审核，准备装配 main.tex")
        return {
            "active_section": None,
            "stage": "T2_SECTIONS_STAGED",
            "routing_action": "PROCEED",
            "history": history
        }

    # Ensure ready and start pdstate attempt for active_sec
    code_st, out_st, err_st = _ensure_task_ready_and_start(self_dir, active_sec, runner)
    if code_st == 0:
        history.append(f"T2: pdstate.py 已为 {active_sec} 创建独占 attempt 并就绪")
    else:
        history.append(f"T2: pdstate.py start {active_sec} 提示: {(out_st or err_st).strip()}")

    # Build dispatch request
    tex_filename = f"{active_sec}.tex"
    role_name = "开篇引入专员" if active_sec == "sec0" else f"{active_sec} 切片制作专员"
    prompt_file = "subagent_prompt.md"

    req = build_request(
        state,
        stage=f"T2_{active_sec.upper()}",
        role=role_name,
        prompt_file=prompt_file,
        artifacts=[tex_filename],
        instruction=f"请按照 subagent_prompt.md 黄金模板撰写 {tex_filename}，确保 qid 唯一、环境闭合、单源双输出。",
        command=f"python scripts/pdstate.py self/ submit {active_sec} && python scripts/pdstate.py self/ mark {active_sec} checked",
        context={
            "section_id": active_sec,
            "user_spec": state.get("user_spec", {})
        }
    )
    req_ref = write_request_file(self_dir, req)
    history.append(f"T2: 派发 {active_sec} 切片编写任务 (派发单: {req_ref})")

    statuses[active_sec] = "running"

    result: Dict[str, Any] = {
        "active_section": active_sec,
        "section_statuses": statuses,
        "pending_dispatch": req,
        "stage": "T2_SECTIONS_PENDING",
        "history": history
    }
    # 熔断计数器仅在推进到新切片时重置（B1）：repair→submit→gate 失败
    # 的同一切片循环不得重置 retry_count，否则熔断器永不触发。
    if active_sec != state.get("active_section"):
        result["retry_count"] = 0
    return result


def node_t2_await_sections(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2: Wait for active section artifact to be generated."""
    req = state.get("pending_dispatch")
    active_sec = state.get("active_section")
    if not req or not active_sec:
        return {"stage": "T2_SECTIONS_STAGED", "routing_action": "PROCEED"}

    return run_dispatch(
        state,
        req,
        success_stage="T2_SECTIONS_STAGED",
        success_history=f"T2: 切片 {active_sec} 候选文件已就绪"
    )


def _sync_required_acceptance_scopes(self_dir: str, expected_sections: List[str], history: List[str]) -> None:
    """提取各切片中真实的 qid 并同步登记至 .pd/run.json 的 required_acceptance_scopes (E6)。"""
    qids = []
    for sec in expected_sections:
        sec_path = os.path.join(self_dir, f"{sec}.tex")
        if os.path.isfile(sec_path):
            try:
                with open(sec_path, encoding="utf-8", errors="replace") as f:
                    qids.extend(re.findall(r"^\s*%\s*qid:\s*([^\s%]+)", f.read(), re.M))
            except OSError:
                pass
    run_json_path = os.path.join(self_dir, ".pd", "run.json")
    if qids and os.path.isfile(run_json_path):
        try:
            with open(run_json_path, encoding="utf-8-sig") as f:
                run_data = json.load(f)
            run_data["required_acceptance_scopes"] = {"chapter": qids}
            tmp = run_json_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(run_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, run_json_path)
            history.append(f"T2: 已同步登记必需验收范围 required_acceptance_scopes（chapter 覆盖 {len(qids)} 个 qid）")
        except Exception as e:
            history.append(f"T2: 登记 required_acceptance_scopes 失败: {e}")


def _import_direct_candidate(self_dir: str, active_sec: str, runner: ScriptRunner,
                             history: List[str]) -> bool:
    """把写作者直接落在 self/ 根目录的 <sec>.tex 正式纳入 pdstate 契约：
    将其作为当前 attempt 的候选文件搬入 .pd/candidates/<sec>/attempt-NNNN/，
    并按冻结身份生成 result.json（复述 run_id/task_id/attempt_id/input_digest
    与输出 SHA256）。纳管后由 submit 走完整身份/新鲜度/结构校验——
    这是"直接落盘"写法与独占 attempt 契约之间的唯一合法桥梁，
    取代旧版绕过契约的"直接就绪模式"。契约任一环节不符仍由 submit 拒绝。
    """
    import hashlib
    import shutil as _shutil

    run_json_path = os.path.join(self_dir, ".pd", "run.json")
    direct_path = os.path.join(self_dir, f"{active_sec}.tex")
    if not (os.path.isfile(run_json_path) and os.path.isfile(direct_path)):
        return False
    try:
        with open(run_json_path, "r", encoding="utf-8-sig") as f:
            run_data = json.load(f)
        task = (run_data.get("tasks") or {}).get(active_sec) or {}
        aid = task.get("current_attempt")
        if not aid:
            return False
        aid_dir_name = ("attempt-%04d" % aid) if isinstance(aid, int) else str(aid)
        adir = os.path.join(self_dir, ".pd", "candidates", active_sec, aid_dir_name)
        tj_path = os.path.join(adir, "task.json")
        if not os.path.isfile(tj_path):
            return False
        with open(tj_path, "r", encoding="utf-8") as f:
            frozen = json.load(f)

        with open(direct_path, "rb") as f:
            data = f.read()
        out_name = f"{active_sec}.tex"
        cand_path = os.path.join(adir, out_name)
        tmp = cand_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cand_path)

        result_doc = {
            "schema_version": 2,
            "run_id": frozen.get("run_id"),
            "task_id": frozen.get("task_id"),
            "attempt_id": frozen.get("attempt_id"),
            "input_digest": frozen.get("input_digest"),
            "output": out_name,
            "output_sha256": hashlib.sha256(data).hexdigest(),
            "imported_from": "self/" + out_name,
            "note": "写作者直接落盘产物经节点纳管为当前 attempt 候选",
        }
        rj_tmp = os.path.join(adir, "result.json.tmp")
        with open(rj_tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result_doc, f, ensure_ascii=False, indent=2)
        os.replace(rj_tmp, os.path.join(adir, "result.json"))
        history.append(
            f"T2: 已将直接落盘的 {out_name} 纳管为 {aid_dir_name} 候选（待 submit 契约校验）"
        )
        return True
    except Exception as e:
        history.append(f"T2: 候选纳管失败: {e}")
        return False


def node_t2_submit_and_check_sections(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2: Submit candidate through pdstate contract and mark as checked."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    active_sec = state.get("active_section")
    expected_sections = state.get("expected_sections", ["sec1"])
    statuses = dict(state.get("section_statuses", {}))
    history = list(state.get("history", []))
    runner = ScriptRunner(self_dir)

    if active_sec and active_sec not in expected_sections:
        history.append(f"T2: {active_sec} 为全局文件（非切片任务），跳过切片提交")
        active_sec = None

    if active_sec:
        # §6：语义审读硬闸门——checker 结果是 submit 的硬前置。
        # 缺结果 / 候选摘要过期 / schema 非法 → 补派审读（semantic_repair_target）；
        # REVISE / NEEDS_EVIDENCE → 重派写作者返修。任何情形不得静默提交。
        # （惰性 import：semantic_nodes → section_batch_nodes → section_nodes
        #   构成环，模块级 import 会在 workflow 包初始化时断裂）
        from .semantic_nodes import semantic_gate_verdict

        sec_tex = os.path.join(self_dir, f"{active_sec}.tex")
        if not os.path.isfile(sec_tex):
            history.append(f"T2: 切片 {active_sec}.tex 缺失，无法进入语义审读")
            return {
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": active_sec,
                "semantic_repair_target": None,
                "repair_reason": f"切片 {active_sec}.tex 缺失，需重新生成后才能提交",
                "history": history
            }
        verdict, reason = semantic_gate_verdict(self_dir, active_sec)
        if verdict != "PASS":
            history.append(f"T2: 切片 {active_sec} 语义审读硬闸门未通过: {reason}")
            return {
                "stage": "REPAIR_LOOP",
                "routing_action": "REPAIR_SECTION",
                "repair_target": active_sec,
                # BLOCK=补派审读（写作者无过错）；REVISE=写作者返修
                "semantic_repair_target": active_sec if verdict == "BLOCK" else None,
                "repair_reason": reason,
                "history": history
            }
        run_json_path = os.path.join(self_dir, ".pd", "run.json")
        cur_status = None
        if os.path.isfile(run_json_path):
            try:
                with open(run_json_path, "r", encoding="utf-8-sig") as f:
                    rdata = json.load(f)
                cur_status = ((rdata.get("tasks") or {}).get(active_sec) or {}).get("status")
            except Exception:
                pass

        if cur_status in ("produced", "checked"):
            code_sub = 0
            history.append(f"T2: 切片 {active_sec} 状态已为 {cur_status}，跳过重复 submit")
        else:
            # 1. Try submit
            code_sub, out_sub, err_sub = runner.run_script("pdstate.py", [self_dir, "submit", active_sec])
            if code_sub == 0:
                history.append(f"T2: pdstate.py submit {active_sec} 成功提升正式切片")
            else:
                # B3+: 契约旁路（直接就绪模式）已移除。写作者直接把 <sec>.tex
                # 落在 self/ 根目录是 interrupt 模式的常态路径——将其正式纳管为
                # 当前 attempt 候选后重试 submit；契约校验（身份/新鲜度/结构）
                # 仍由 pdstate 完整执行，纳管失败或重试失败一律定点返修。
                if cur_status is None or cur_status in ("stale", "failed", "blocked", "planned"):
                    _ensure_task_ready_and_start(self_dir, active_sec, runner)
                if _import_direct_candidate(self_dir, active_sec, runner, history):
                    code_sub, out_sub, err_sub = runner.run_script(
                        "pdstate.py", [self_dir, "submit", active_sec])
                    if code_sub == 0:
                        history.append(f"T2: pdstate.py submit {active_sec} 纳管后成功提升正式切片")
                if code_sub != 0:
                    err_msg = (err_sub or out_sub or "submit failed").strip()
                    history.append(f"T2: 切片 {active_sec} 提交失败: {err_msg}")
                    return {
                        "stage": "REPAIR_LOOP",
                        "routing_action": "REPAIR_SECTION",
                        "repair_target": active_sec,
                        "repair_reason": f"切片提交失败: {err_msg}",
                        "history": history
                    }

        # 2. Mark checked（B3：退出码必须检查，非 0 一律返修，不得继续置 checked）
        if cur_status == "checked":
            code_mark = 0
            history.append(f"T2: 切片 {active_sec} 状态已为 checked，跳过重复 mark checked")
        else:
            code_mark, out_mark, err_mark = runner.run_script("pdstate.py", [self_dir, "mark", active_sec, "checked"])
            if code_mark != 0:
                err_msg = (err_mark or out_mark or "mark checked failed").strip()
                history.append(f"T2: 切片 {active_sec} 标记 checked 失败 (exit {code_mark}): {err_msg}")
                return {
                    "stage": "REPAIR_LOOP",
                    "routing_action": "REPAIR_SECTION",
                    "repair_target": active_sec,
                    "repair_reason": f"pdstate mark checked 失败 (exit {code_mark}): {err_msg}",
                    "history": history
                }
        statuses[active_sec] = "checked"
        history.append(f"T2: 切片 {active_sec} 已标记为 checked")

        # 同步最新 qid 范围至 run.json
        _sync_required_acceptance_scopes(self_dir, expected_sections, history)

    return {
        "section_statuses": statuses,
        # B1: submit/check 节点不再重置 retry_count；计数器仅由
        # node_t2_plan_dispatch（P1 fan-out 主控）在规划新批次时清零，
        # 保证熔断可触发。
        "stage": "T2_SECTIONS_STAGED",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t2_assemble_main(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2: Assemble all expected sections into main.tex."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    expected_sections = state.get("expected_sections", ["sec1"])
    history = list(state.get("history", []))

    main_tex_path = os.path.join(self_dir, "main.tex")
    if os.path.isfile(main_tex_path):
        with open(main_tex_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        inputs_to_insert = [f"\\input{{{sec}.tex}}\n" for sec in expected_sections]

        new_lines = []
        replaced = False
        for line in lines:
            # 兼容 sec3a/sec0 等字母后缀节名（与 mapping_nodes 的自适应命名一致）；
            # 旧正则 sec\d+ 会漏掉字母后缀，导致陈旧输入残留或替换失效
            if re.match(r"^\s*\\input\{sec[0-9A-Za-z]*\.tex\}", line):
                if not replaced:
                    new_lines.extend(inputs_to_insert)
                    replaced = True
            else:
                new_lines.append(line)

        if not replaced:
            # 兜底：插到 \input{footer.tex} 之前；无 footer 输入时退到
            # \end{document} 之前；两者都无则追加到文件末尾
            # （main.tex 模板把 \end{document} 放在 footer.tex 里，旧兜底分支
            # 永远匹配不到，会静默不装配）
            final_lines = []
            for line in new_lines:
                if not replaced and "\\input{footer.tex}" in line:
                    final_lines.extend(inputs_to_insert)
                    replaced = True
                final_lines.append(line)
            if not replaced:
                for idx, line in enumerate(final_lines):
                    if "\\end{document}" in line:
                        final_lines[idx:idx] = inputs_to_insert
                        replaced = True
                        break
            if not replaced:
                final_lines.extend(inputs_to_insert)
            new_lines = final_lines

        with open(main_tex_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    # 登记最终必需验收范围：deliver.py 依据 run.json 的
    # required_acceptance_scopes 强制 chapter 级 PASS 且 reviewed_qids 全覆盖。
    # 此前无任何节点写入该键，交付层的验收强制校验形同虚设（BUG #3b）。
    qids = []
    for sec in expected_sections:
        sec_path = os.path.join(self_dir, f"{sec}.tex")
        if os.path.isfile(sec_path):
            try:
                with open(sec_path, encoding="utf-8", errors="replace") as f:
                    qids.extend(re.findall(r"^\s*%\s*qid:\s*([^\s%]+)",
                                           f.read(), re.M))
            except OSError:
                pass
    run_json_path = os.path.join(self_dir, ".pd", "run.json")
    if qids and os.path.isfile(run_json_path):
        try:
            with open(run_json_path, encoding="utf-8-sig") as f:
                run_data = json.load(f)
            run_data["required_acceptance_scopes"] = {"chapter": qids}
            tmp = run_json_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(run_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, run_json_path)
            history.append("T2: 已登记最终必需验收范围 required_acceptance_scopes"
                           f"（chapter 覆盖 {len(qids)} 个 qid）")
        except Exception as e:
            history.append(f"T2: 登记 required_acceptance_scopes 失败: {e}")

    history.append(f"T2: main.tex 已成功装配全部小节 ({', '.join(expected_sections)})")

    return {
        "assembled_main_tex": main_tex_path,
        "stage": "T2_ASSEMBLED",
        "routing_action": "PROCEED",
        "history": history
    }
