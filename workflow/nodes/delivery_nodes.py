"""delivery_nodes.py — T4 Review pack, pedagogical acceptance, and final delivery.
Executes make_review_pack.py, dispatches independent acceptance, and runs deliver.py
to atomically publish to release/.
"""

import json
import os
import sys
from typing import Dict, Any, List, Optional
from ..state import ShrinkCoursewareState, GateReport
from ..adapters.script_runner import ScriptRunner

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import evidence


def node_t4_make_review_pack(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Generate student review pack for controlled acceptance and populate classroom-plan.md."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    # 1. 自动装配/更新翻转课堂教案 (classroom-plan.md，N1/N2)
    cov_path = os.path.join(self_dir, "coverage-map.md")
    if os.path.isfile(cov_path):
        code_plan, out_plan, err_plan = runner.run_script("gen_classroom_plan.py", [self_dir])
        if code_plan == 0:
            history.append("T4: gen_classroom_plan.py 已自动装配翻转课堂计划卡 (classroom-plan.md)")
        else:
            history.append(f"T4: gen_classroom_plan 提示 (exit {code_plan}): {(err_plan or out_plan).strip()}")

    # 2. 生成学生审读包
    code, out, err = runner.run_script("make_review_pack.py", [self_dir])
    if code != 0:
        history.append(f"T4: make_review_pack 提示 (exit {code}): {(err or out).strip()}")
    else:
        history.append("T4: make_review_pack.py 已生成学生审读包 (.pd/review/)")

    return {
        "review_pack_ready": (code == 0),
        "stage": "T4_ACCEPTANCE_PENDING",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t4_verify_acceptance(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Verify pedagogical acceptance report status with evidence binding (B4)."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    history = list(state.get("history", []))
    report_path = os.path.join(self_dir, ".pd", "acceptance", "chapter.json")

    # 当前证据基线：内容摘要 + 按 output_format 对应存在的学生版产物哈希
    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"
    current_digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    student_pdf_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.pdf"))
        if fmt in ("pdf", "both") else None
    )
    student_html_sha = (
        evidence.sha256_file(os.path.join(self_dir, "student.html"))
        if fmt in ("html", "both") else None
    )
    # A24：学生版 HTML 的**内容**身份。字节哈希每次构建都因
    # render_html.py:356 的 GENERATED_AT 时间戳而变，验收若只认字节就会
    # 每次重渲染都整体过期；内容身份一致时只记账，不判 REVISE。
    student_html_content_sha = (
        evidence.html_student_content_digest(
            os.path.join(self_dir, "student.html"))
        if fmt in ("html", "both") else None
    )
    a24_warnings: List[str] = []

    passed = False
    status = "REVISE"
    issues = []
    conclusion = "MISSING"
    data = None

    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            status_val = str(data.get("conclusion") or data.get("status") or "").upper()
            conclusion = status_val or "UNKNOWN"
            issues = data.get("issues", [])
        except Exception as e:
            issues = [{"category": "PARSE_ERROR", "message": str(e)}]
            conclusion = "PARSE_ERROR"
    # 注意：缺失 / 无法解析验收记录一律不得视为通过（BUG #3a）。
    # 旧实现在文件缺失时直接置 PASS，使独立教学验收层可被整体绕过。

    # B4: 验收证据强校验 —— chapter.json 必须通过全部字段校验、结论为 PASS、
    # content_digest 绑定当前 evidence.content_digest、学生版产物哈希绑定当前构建。
    binding_problems: List[str] = []
    if data is not None and conclusion != "PARSE_ERROR":
        binding_problems = evidence.validate_acceptance_json(
            data,
            current_digest=current_digest,
            student_pdf_sha=student_pdf_sha,
            student_html_sha=student_html_sha,
            student_html_content_sha=student_html_content_sha,
            warnings=a24_warnings
        )
        if binding_problems:
            history.append(f"T4: 验收证据绑定校验未通过（{len(binding_problems)} 项）")
    for w in a24_warnings:
        history.append(f"T4: {w}")

    if not binding_problems and conclusion == "PASS":
        passed = True
        status = "PASS"

    gate = GateReport(
        gate_name="pedagogical_acceptance",
        status=status,
        passed=passed,
        issues=issues,
        report_path=report_path,
        exit_code=0 if passed else 1
    )

    def _redispatch(reason_label: str) -> Dict[str, Any]:
        """重新派发独立教学验收（带轮次熔断，防死循环）。"""
        rounds = state.get("acceptance_rounds", 0) + 1
        if rounds > 3:
            history.append(f"T4: 验收证据{reason_label}，重新派发轮次超限（{rounds} > 3），熔断停止")
            return {
                "acceptance_gate": gate,
                "acceptance_rounds": rounds,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"独立教学验收证据{reason_label}且重新派发超限",
                "history": history
            }
        history.append(
            f"T4: 验收证据{reason_label}，重新派发独立教学验收（第 {rounds} 轮，缺证据不得放行）"
        )
        return {
            "acceptance_gate": gate,
            "acceptance_rounds": rounds,
            "stage": "T4_ACCEPTANCE_PENDING",
            "routing_action": "REDISPATCH_ACCEPTANCE",
            "history": history
        }

    if not passed and conclusion in ("MISSING", "PARSE_ERROR", "UNKNOWN"):
        # 缺证据/坏记录：重新派发独立教学验收
        return _redispatch(conclusion)

    if not passed and (binding_problems or conclusion == "NEEDS_EVIDENCE"):
        # 记录可解析但字段/摘要/哈希绑定不满足：旧证据不得冒充当前结果
        return _redispatch("绑定校验失败" if binding_problems else conclusion)

    if not passed:
        # REVISE：验收专员要求修改 —— 同样消耗验收轮次（修复 REVISE 无限循环）
        rounds = state.get("acceptance_rounds", 0) + 1
        if rounds > 3:
            history.append(f"T4: 验收结论持续为 {conclusion}，轮次超限（{rounds} > 3），熔断停止")
            return {
                "acceptance_gate": gate,
                "acceptance_rounds": rounds,
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": f"独立教学验收结论持续为 {conclusion} 且轮次超限",
                "history": history
            }
        target_sec = None
        expected = state.get("expected_sections") or ["sec1", "sec2", "sec3"]
        for iss in issues:
            if str(iss.get("status") or "").lower() == "fixed":
                continue
            q = str(iss.get("qid") or "")
            for s in expected:
                if q.startswith(s + "-") or q == s:
                    target_sec = s
                    break
            if target_sec:
                break
        if not target_sec and expected:
            target_sec = expected[0]

        return {
            "acceptance_gate": gate,
            "acceptance_rounds": rounds,
            "stage": "REPAIR_LOOP",
            "routing_action": "REPAIR_SECTION",
            "repair_target": target_sec,
            "repair_reason": f"教学验收未通过: {status}",
            "history": history
        }

    history.append("T4: 教学验收核准通过")
    return {
        "acceptance_gate": gate,
        "acceptance_rounds": state.get("acceptance_rounds", 0),
        "stage": "T4_ACCEPTED",
        "routing_action": "PROCEED",
        "history": history
    }


def node_t4_promote_and_deliver(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T4: Execute deliver.py for final gate synthesis and atomic release."""
    self_dir = state.get("self_dir") or state.get("project_root", ".")
    runner = ScriptRunner(self_dir)
    history = list(state.get("history", []))

    fmt = state.get("output_format") or (state.get("user_spec") or {}).get("output_format") or "both"
    # Run deliver.py
    # 沙箱/无浏览器环境放行：Playwright 经命名管道与 Chrome 通信，受限沙箱下
    # named pipe 打开被拒（EPERM）→ html_smoke 探测起不来（实测：launch 不报错
    # 只是挂死 >24 分钟零输出，现由 html_smoke.probe_browser 的子进程+超时兜住）。
    # 处置分两档，语义严格分开：
    #   ① auto 模式（流水线默认）：浏览器不可用时自动降级——
    #      manifest.html_smoke.status=blocked_env_degraded 后继续交付，留痕可查；
    #      HTML 内容缺陷仍照旧中止。html_smoke 现在能自己识别"起不来"，不再
    #      依赖人工放环境变量/落标记文件。
    #   ② 显式跳过（更强）：工作区有标记文件 .pd/allow-skip-html-smoke（内容=
    #      跳过原因，落盘即留痕）时透传 --skip-html-smoke。这是人工决定"这一版
    #      不验收"，优先级高于自动降级。
    # 旧实现在本节点无条件写 os.environ["QCHAIN_ALLOW_SKIP_HTML_SMOKE"]="1"，
    # 该进程级污染会让同进程后续工作区（无标记文件）也被一并跳过，已删除。
    deliver_args = [self_dir, "--format", fmt]
    if fmt in ("html", "both"):
        deliver_args.append("--html-smoke-mode=auto")
    skip_marker = os.path.join(self_dir, ".pd", "allow-skip-html-smoke")
    marker_reason = ""
    if os.path.isfile(skip_marker):
        try:
            with open(skip_marker, "r", encoding="utf-8") as fh:
                marker_reason = fh.read().strip()
        except OSError:
            marker_reason = ""
    if os.environ.get("QCHAIN_ALLOW_SKIP_HTML_SMOKE") == "1" or os.path.isfile(skip_marker):
        deliver_args.append("--skip-html-smoke")
        # deliver.py 的 resolve_html_smoke_skip 要求环境变量显式确认才放行，
        # 标记文件路径同样视为显式确认（原因已落盘），此处同步注入环境变量。
        # 进程级变量用后即还原：同一进程内的后续工作区（无标记文件）不得
        # 因前一个工作区的显式跳过而被一并放过（E1/A12 的进程污染）。
        _prev_skip_env = os.environ.get("QCHAIN_ALLOW_SKIP_HTML_SMOKE")
        os.environ["QCHAIN_ALLOW_SKIP_HTML_SMOKE"] = "1"
        if marker_reason:
            history.append(f"T4: HTML 侧浏览器验收按标记文件跳过，原因: {marker_reason[:200]}")
    else:
        _prev_skip_env = None
        history.append("T4: HTML 侧浏览器验收按 auto 模式执行（浏览器不可用则降级为 blocked_env_degraded）")
    try:
        code, out, err = runner.run_script("deliver.py", deliver_args)
    finally:
        if _prev_skip_env is None:
            os.environ.pop("QCHAIN_ALLOW_SKIP_HTML_SMOKE", None)
        else:
            os.environ["QCHAIN_ALLOW_SKIP_HTML_SMOKE"] = _prev_skip_env
    history.append(f"T4: deliver.py 汇总门禁执行 (退出 {code}): {(out or err).strip()[:200]}")

    manifest_path = os.path.join(self_dir, "release", "manifest.json")
    manifest_data = None
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                manifest_data = json.load(f)
        except Exception:
            pass

    promoted = (code == 0) and (manifest_data is not None)

    deliver_gate = GateReport(
        gate_name="deliver",
        status="PASS" if promoted else "REJECT",
        passed=promoted,
        exit_code=code,
        report_path=manifest_path,
        raw_report=manifest_data or {}
    )

    if not promoted:
        return {
            "deliver_gate": deliver_gate,
            "promoted": False,
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"最终交付门禁未通过 (退出 {code})",
            "history": history
        }

    history.append(f"T4: 恭喜！学案已完成原子发布至 {os.path.join(self_dir, 'release')}")

    return {
        "deliver_gate": deliver_gate,
        "delivery_manifest": manifest_data,
        "promoted": True,
        "stage": "T4_DELIVERED",
        "routing_action": "PROCEED",
        "history": history
    }
