"""shuffle_nodes.py — T2 末段（所有题目制成并装配 main.tex 之后）的
选择题选项随机打乱节点。

修复缺陷：LLM 切片专员习惯把参考答案写在 \\fillin 选项首位，LaTeX 版
\\fillin 宏按源码顺序把选项附加在下划线之后——学生版 PDF 中每个选择题
的第一个候选项永远正确（“所有答案都是 A”）。本节点在所有题目制成之后、
T3 检查/编译之前运行 scripts/shuffle_options.py，对全部切片的选择题选项
做确定性随机重排（幂等：重复执行结果不变），并以 shuffle_gate 证据绑定
打乱后的受审输入摘要。
"""

import os
import re
from typing import Dict, Any
from ..state import ShrinkCoursewareState
from ..adapters.script_runner import ScriptRunner

RE_SEC_STEM = re.compile(r"^(sec[0-9A-Za-z_-]+)$")
# 打乱脚本的问题消息以 "<切片>:<行>: " 或 "<切片>.tex: " 开头
RE_MSG_SEC = re.compile(r"^(sec[0-9A-Za-z_-]+)[.:]")


def _repair_target_for(report: Dict[str, Any], state: ShrinkCoursewareState):
    """从报告证据推断首个出错切片（作为定点返修目标）。"""
    raw = (report or {}).get("raw_report") or {}
    for msg in (raw.get("issues") or []):
        if isinstance(msg, dict):
            text = str(msg.get("message", ""))
        else:
            text = str(msg)
        m = RE_MSG_SEC.match(text.strip())
        if m:
            return m.group(1)
    for f in ((raw.get("evidence") or {}).get("files") or []):
        name = os.path.basename(str((f or {}).get("path", "")))
        stem = os.path.splitext(name)[0]
        if RE_SEC_STEM.match(stem):
            return stem
    return state.get("active_section") or "sec1"


def node_t2_shuffle_options(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T2: 所有题目制成后，对选择题选项执行随机打乱（shuffle_options 门禁）。"""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    report = runner.run_gate_script(
        gate_name="shuffle_options",
        script_name="shuffle_options.py",
        args=[self_dir],
        report_rel_path=".pd/reports/shuffle_options.json",
        run_id=state.get("run_id")
    )

    raw = report.get("raw_report") or {}
    totals = (raw.get("evidence") or {}).get("totals") or {}
    history.append(
        "T2: 选择题选项随机打乱(shuffle_options) 状态: %s (退出 %s；"
        "槽位 %s，重排 %s，答案位置分布 %s → %s)"
        % (report["status"], report["exit_code"],
           totals.get("slots", 0), totals.get("changed", 0),
           totals.get("answer_positions_before", {}),
           totals.get("answer_positions_after", {})))

    if not report["passed"]:
        target = _repair_target_for(report, state)
        history.append("T2: 打乱机制未通过，定点返修 %s" % target)
        return {
            "shuffle_gate": report,
            "shuffle_passed": False,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": target,
            "repair_reason": "选择题选项随机打乱未通过: %s"
                             % (report["status"]),
            "history": history
        }

    return {
        "shuffle_gate": report,
        "shuffle_passed": True,
        "stage": "T2_SHUFFLED",
        "routing_action": "PROCEED",
        "history": history
    }
