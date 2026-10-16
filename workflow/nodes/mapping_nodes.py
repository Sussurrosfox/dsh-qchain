"""mapping_nodes.py — T1 Mapping & Material Index nodes.
Generates and verifies source-map.md, dependency-map.md, coverage-map.md,
and builds .pd/materials.json machine index.
"""

import os
import re
from typing import Dict, Any, List
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner
from .agent_nodes import build_request, write_request_file, run_dispatch


def _extract_expected_sections(self_dir: str, coverage_map_path: str) -> List[str]:
    """Extract section list from coverage-map.md table rows or fallback to sec*.tex found or default ['sec1'].
    Supports adaptive alphanumeric naming like sec0, sec1, sec3a, sec3b, sec4.
    """
    def sec_sort_key(s: str):
        m = re.match(r"^sec(\d+)(.*)$", s)
        if m:
            return (int(m.group(1)), m.group(2))
        return (999, s)

    sections = []
    if os.path.isfile(coverage_map_path):
        try:
            with open(coverage_map_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 严格从表格行中提取小节代号（避免正文/说明中提及已废弃切片导致复活幽灵切片）
            found = set()
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("|") and not line.startswith("|---"):
                    for match in re.finditer(r"\b(sec\d+[a-z]?)\b", line):
                        found.add(match.group(1))
            if found:
                sections = sorted(list(found), key=sec_sort_key)
        except Exception:
            pass

    if not sections:
        # Check if sec*.tex files already exist in self_dir
        sec_files = [f[:-4] for f in os.listdir(self_dir)
                     if f.startswith("sec") and f.endswith(".tex") and f != "sec_template.tex"]
        if sec_files:
            sections = sorted(sec_files, key=sec_sort_key)

    if not sections:
        sections = ["sec1"]

    return sections


def node_t1_request_mapping(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T1: Dispatch request for 3 mapping tables with knowledge density weighted adaptive planning."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    artifacts = ["source-map.md", "dependency-map.md", "coverage-map.md"]

    user_spec = state.get("user_spec") or {}
    lesson_minutes = user_spec.get("lesson_minutes", 90) or 90

    instruction = (
        "请根据原材料建立 source-map.md（原文骨架）、dependency-map.md（前置依赖）与 coverage-map.md（覆盖与课堂预算）。\n\n"
        "【知识密度自适应规划规范（废除机械等额分配）】：\n"
        "1. 严禁死板机械均分为 4 节、每节 5 题、每题 3 问的均分定额制；\n"
        "2. 基础概念/计算引入型内容：知识密度较低，配置 3–4 道直观探索题，不生硬凑题；\n"
        "3. 核心定理/同构分类驱动型内容：知识密度极高，必须允许拆分为更细粒度小节（如 sec3a 与 sec3b）或扩展为 6–8 道阶梯式引理题，"
        "必须配置小素数/低阶特例算例演练与充分的中间脚手架，严禁为凑 5 题上限跳过证明关键步骤；\n"
        "4. 保证每个小节以 microknowledge 前置卡开头，以 knowledgebox 节末结论框收束；\n"
        "5. coverage-map.md 中必须包含『推荐讲法』与『易错点』两列且每行实质填写，不得留空或填占位符（此为翻转课堂教师讲解与卡点突破的必备输入）；\n"
        f"6. 双向认知预算规范：teaching/classroom 模式下 core 任务总耗时受课时预算严格约束（预算 {lesson_minutes} 分钟，超标需删减次要题量）；"
        "self_study 模式不设时长上限，鼓励构建自足完备的认知链路；无论何种模式，核心定理/引理推导长链题 planned_minutes 必须 >= 12 分钟，严禁削减长题推导台阶！"
    )

    # 自学模式（self_study）切片规划硬红线（report.md 模块三 3.1）：
    # 仅靠 research_prompt.md 提示词不够，规划派发指令必须按模式注入可执行约束。
    prof = str(user_spec.get("delivery_profile", "self_study")).lower()
    if prof == "self_study":
        instruction += (
            "\n\n【self_study 自学模式硬红线（必须遵守）】：\n"
            "1. 自学模式不设总时长上限，以认知台阶完备、逻辑严谨自足为唯一准绳，切勿为凑时间压缩题目推导；\n"
            "2. 严禁将原材料末尾的“习题选讲/压轴题精讲/例题讨论/例题课”单独划为独立切片小节；"
            "此类内容若适合探究，必须拆解为工具应用题并入前述理论小节，否则在 source-map.md 中登记移入课后自测题库；\n"
            "3. 单模块切片严格限制为“概念引入节 → 核心工具推导节 → 度量与应用节”的 3 节精炼自足体系"
            "（确有必要时可用 sec3a/sec3b 细分，但不得增设习题选讲节）；\n"
            "4. 大定理长链推导题（如 Routh、EFFT 类）必须在 coverage-map.md 登记 planned_minutes: 12~15，"
            "在切片题量统计中计为 2 道标准题的认知权重，废除每题均分 6~7 分钟的机械表格；\n"
            "5. coverage-map.md 中只允许登记计划生成的 sec 节名，不得提及已删除/不存在的节。"
        )

    req = build_request(
        state,
        stage="T1_MAPPING",
        role="教学设计与映射专员",
        prompt_file="research_prompt.md",
        artifacts=artifacts,
        instruction=instruction,
        command="python scripts/evidence.py materials-index self/ --out self/.pd/materials.json",
        context={
            "input_path": state.get("input_path", ""),
            "user_spec": user_spec
        }
    )
    req_ref = write_request_file(self_dir, req)
    history = list(state.get("history", []))
    history.append(f"T1: 派发三张映射表构建任务 (知识密度自适应模式"
                   + ("，self_study 自学红线已注入" if user_spec.get("delivery_profile") == "self_study" else "")
                   + f"，派发单: {req_ref})")

    return {
        "pending_dispatch": req,
        "stage": "T1_MAPPING_PENDING",
        "history": history
    }


def node_t1_await_mapping(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T1: Wait for mapping artifacts to be created or modified."""
    req = state.get("pending_dispatch")
    if not req:
        return {"stage": "T1_MAPPED", "routing_action": "PROCEED"}

    return run_dispatch(
        state,
        req,
        success_stage="T1_MAPPED",
        success_history="T1: 三张映射表已产出并完成校验"
    )


def node_t1_verify_mapping(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T1: Verify mapping integrity, build materials.json index, and determine expected sections."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    coverage_path = os.path.join(self_dir, "coverage-map.md")
    expected_sections = _extract_expected_sections(self_dir, coverage_path)

    # Optional: build materials index if research materials exist
    materials_out = os.path.join(self_dir, ".pd", "materials.json")
    code_mat, out_mat, err_mat = runner.run_script(
        "evidence.py", ["materials-index", self_dir, "--out", materials_out]
    )
    if code_mat == 0:
        history.append("T1: materials.json 机器索引已同步")

    history.append(f"T1: 确定切片生成清单: {', '.join(expected_sections)}")

    # 验证规划期预算、长链密度与教师支撑门禁
    if os.path.isfile(coverage_path):
        bud_report = runner.run_gate_script(
            gate_name="check_budget",
            script_name="check_budget.py",
            args=[self_dir, "--stage", "planning"],
            report_rel_path=".pd/reports/check_budget_planning.json",
            run_id=state.get("run_id")
        )
        history.append(f"T1: check_budget (planning) 状态: {bud_report['status']} (退出 {bud_report['exit_code']})")
        if not bud_report["passed"]:
            # B1: REPLAN 重规划计数，防 T1 映射死循环（>3 熔断 HALT）
            replan_count = state.get("replan_count", 0) + 1
            if replan_count > 3:
                history.append(f"T1: 映射重规划次数超限 ({replan_count} > 3)，熔断停止")
                return {
                    "expected_sections": expected_sections,
                    "budget_gate": bud_report,
                    "replan_count": replan_count,
                    "stage": "HALTED",
                    "routing_action": "HALT",
                    "error_message": f"T1 映射重规划次数超限: {bud_report['status']}",
                    "history": history
                }
            return {
                "expected_sections": expected_sections,
                "budget_gate": bud_report,
                "replan_count": replan_count,
                "stage": "REPAIR_LOOP",
                "routing_action": "REPLAN",
                "repair_target": "coverage-map.md",
                "repair_reason": f"T1 规划期教学预算或长链密度不合规: {bud_report['status']}",
                "history": history
            }

    return {
        "expected_sections": expected_sections,
        "mapping_frozen": True,
        "stage": "T1_MAPPED",
        "routing_action": "PROCEED",
        "history": history
    }
