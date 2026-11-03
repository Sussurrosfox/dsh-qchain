"""issues.py — 门禁问题的内部适配层与显式路由表（修复卡 05，DATA-CONTRACTS C2）。

各门禁脚本历史上报告形态不一（``{severity, message}``、``errors[].code``、
GateReport 合成的 ``{category, severity: HIGH_RISK|WARN|INFO}``）。本模块把它们
**在内存里**适配成 C2 统一 issue 对象，不改任何旧报告文件、不丢原始证据
（原条目整体放进 ``evidence.raw``）：

    {code, severity(error|warning|info), category, scope, qid, artifact, message,
     evidence, input_digest, repair_kind(none|deterministic|agent|human),
     repair_target, blocking}

铁律：
  * ``blocking`` 由显式规则表算出，不由调用方猜；
  * **未知 error 不降级**：落入 ``unclassified``（human + blocking），路由为 HALT；
  * warning / info / review 绝不自动派 Agent（review 要人工复核，info 只提示）；
  * 渲染器 / 资源 / 目录 / 引擎类缺陷绝不派写作者；
  * agent 类问题定位不到切片 → 停机，不回退到“上一节 / 第一节”盲改；
  * 基础设施失败（ERROR / 报告缺失或过期 / 门禁崩溃）→ 停机，不拿内容重试顶替。
"""

import re
from typing import Any, Dict, List, NamedTuple, Optional

SEVERITIES = ("error", "warning", "info")
REPAIR_KINDS = ("none", "deterministic", "agent", "human")

#: GateReport 合成的基础设施类 category（script_runner._synthesize_gate_report）
INFRA_CATEGORIES = ("EXECUTION_FAILURE", "REPORT_MISSING_OR_STALE")

#: 路由目标里表示“从 issue 里定位切片”的占位
SECTION = "@section"
#: 路由到 REPLAN 的地图类目标
REPLAN_TARGETS = ("source-map.md", "coverage-map.md")


class Route(NamedTuple):
    category: str
    repair_kind: str            # none | deterministic | agent | human
    repair_target: Optional[str]  # 固定产物名 / SECTION / None
    #: True=error 级必阻断；strict_blocks=warning 级仅 strict 模式阻断
    strict_blocks: bool = False
    #: agent 类定位不到切片时的回退目标（如 fidelity → source-map.md 重规划）
    fallback: Optional[str] = None


# ---------------------------------------------------------------------------
# 显式路由表（code → Route）。顺序无关；前缀表另见 PREFIX_ROUTES。
# ---------------------------------------------------------------------------
_SRC = "source-map.md"
_COV = "coverage-map.md"

CODE_ROUTES: Dict[str, Route] = {
    # --- 来源忠实性：正文级（漏题/公式/答案/图丢失）→ 派 Agent 修具体切片 ---
    "FIDELITY_UNGROUNDED_QID": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_UNGROUNDED_BLOCK": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_EXCLUDED_REFERENCED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_BRIDGE_UNAPPROVED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_ASSET_LOST": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_SOURCE_NOT_COVERED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    # --- 映射不一致 → 最小重规划映射 ---
    "FIDELITY_COVERAGE_MISSING": Route("mapping", "agent", _COV),
    "FIDELITY_COVERAGE_FORMAT": Route("mapping", "agent", _COV),
    # --- 范围/契约漂移 → 人工 ---
    "SCOPE_CONTRACT_MISSING": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_CONTRACT_TAMPERED": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_PROFILE_DRIFT": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_MINUTES_DRIFT": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_UNAUTHORIZED_DOWNGRADE": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_POLICY_DRIFT": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_CHANGE_UNAPPROVED": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_UNPLANNED_SECTION": Route("scope_contract", "human", "scope-contract"),
    "SCOPE_CORE_BUDGET_OVERFLOW": Route("scope_contract", "human", "scope-contract",
                                        strict_blocks=True),
    "SCOPE_MUST_COVER_EMPTY": Route("scope_advisory", "none", None, strict_blocks=True),
    # --- 语义审读 / 血缘：定位到切片才派，定位不到停 ---
    "SEMANTIC_MISSING": Route("semantic", "agent", SECTION),
    "SEMANTIC_STALE": Route("semantic", "agent", SECTION),
    "SEMANTIC_INVALID": Route("semantic", "agent", SECTION),
    "SEMANTIC_SECTION_MISSING": Route("semantic", "agent", SECTION),
    "SEMANTIC_REVISE": Route("semantic", "agent", SECTION),
    "SEMANTIC_NEEDS_EVIDENCE": Route("semantic", "agent", SECTION),
    "SEMANTIC_MAIN_MISSING": Route("assembly", "human", "main.tex"),
    "PROV_NO_DISPATCH_CHAIN": Route("provenance", "agent", SECTION),
    "PROV_SECTION_MISSING": Route("provenance", "agent", SECTION),
    "PROV_WRITER_DIGEST_MISSING": Route("provenance", "agent", SECTION),
    "PROV_PATH_OUTSIDE_ALLOWLIST": Route("provenance", "agent", SECTION),
    "PROV_QID_UNPLANNED": Route("provenance", "agent", SECTION),
    "PROV_CHECKER_INVALID": Route("provenance", "agent", SECTION),
    "PROV_CHECKER_MISSING": Route("provenance", "agent", SECTION),
    "PROV_CHECKER_NEEDS_EVIDENCE": Route("provenance", "agent", SECTION),
    "PROV_CHECKER_REVISE": Route("provenance", "agent", SECTION),
    "PROV_CHECKER_STALE": Route("provenance", "agent", SECTION),
    # --- 卡 07：check_budget 精确问题码 + 产物。课堂计划缺失/占位 → 确定性生成
    # （gen_classroom_plan.py，不派作者）；学生 PDF 页数超标 → 版式问题，人工，
    # 绝不回头改课堂计划 ---
    "CLASSROOM_PLAN_MISSING": Route("classroom_plan", "deterministic", "classroom-plan.md"),
    "CLASSROOM_PLAN_INVALID": Route("classroom_plan", "deterministic", "classroom-plan.md"),
    "STUDENT_PDF_PAGE_OVERFLOW": Route("pdf_page_overflow", "human", "pdf-layout",
                                       strict_blocks=True),
}

#: 前缀兜底（按顺序匹配；精确码优先）。FIDELITY_* 其余均为映射/契约/清单类 →
#: 重规划 source-map.md（与 node_t3_checks 原行为一致）。
PREFIX_ROUTES = (
    ("FIDELITY_COVERAGE_", Route("mapping", "agent", _COV)),
    ("FIDELITY_", Route("mapping", "agent", _SRC)),
    ("SCOPE_", Route("scope_contract", "human", "scope-contract")),
)

#: category 级路由（GateReport 合成类别 / check_budget category / 通用）
CATEGORY_ROUTES: Dict[str, Route] = {
    "EXECUTION_FAILURE": Route("infrastructure", "human", "gate-execution"),
    "REPORT_MISSING_OR_STALE": Route("infrastructure", "human", "gate-report"),
    "config": Route("config", "human", "config.yaml"),
    "coverage": Route("budget_coverage", "agent", _COV),
    "section": Route("budget_section", "agent", SECTION),
    # 卡 07：不再有 "artifact" → classroom-plan.md 的“按类别猜文件”；
    # artifact 类问题必须靠 code / artifact 名 / 精确消息定位真正目标，
    # 定位不到 → unknown_target（见 _lookup_route）。
}

#: 无 code 的门禁，error 级默认归类
GATE_ERROR_ROUTES: Dict[str, Route] = {
    "check_pedagogy": Route("pedagogy", "agent", SECTION),
    "check_numbering": Route("numbering", "agent", SECTION),
    "check_dialogue": Route("dialogue", "agent", SECTION),
    "compare_versions": Route("compare_leak", "agent", SECTION),
    "build": Route("build_latex", "agent", SECTION),
}

#: 这些门禁的 warning 在 strict 下才阻断（教学建议 / 限额类）
STRICT_WARNING_GATES = {
    "check_pedagogy": Route("pedagogy_advisory", "agent", SECTION, strict_blocks=True),
}

#: 渲染门禁（check_render）message → 路由；先匹配先得。
RENDER_MESSAGE_ROUTES = (
    # 资源/目录/产物缺失：机械问题，绝不派作者
    (re.compile(r"缺少 \S+\.html|缺少本地 KaTeX|外网 CDN|图片资产缺失|PDF 无法解析|目录|TOC", re.I),
     Route("render_resource", "deterministic", "render-resources")),
    # 渲染器自身缺陷（转义/占位符/定界符/空段/选项数）：修渲染器，不派作者
    (re.compile(r"双重转义|残留未转换记号|定界符不成对|\.qc-math 段为空|选项按钮"),
     Route("render_engine", "deterministic", "renderer")),
    # 作者源码缺陷（汉字写进数学模式、KaTeX 不支持的宏）：可定位到切片才派
    (re.compile(r"KaTeX 不支持的宏|Missing character|选项丢失汉字|空选项组|丢失 \\fillin|不可见字形"),
     Route("render_source", "agent", SECTION)),
)

#: 限额类 message（时间/页数/题量）。默认只提示，strict 才阻断。
_LIMIT_RE = re.compile(r"课时|分钟|超时|题量|题数|题目数|time budget|question limit", re.I)
_PAGE_RE = re.compile(r"页数|超出.{0,6}页|超过.{0,6}页|溢出.{0,6}页|page.{0,10}(overflow|limit)", re.I)

_PDF_PAGE_ROUTE = Route("pdf_page_overflow", "human", "pdf-layout", strict_blocks=True)
_LIMIT_ROUTE = Route("limit_advisory", "human", None, strict_blocks=True)
_UNCLASSIFIED = Route("unclassified", "human", "unclassified")
_PLAN_ROUTE = Route("classroom_plan", "deterministic", "classroom-plan.md")
#: artifact 类问题定位不到真正产物 → 停机并如实报告 unknown_target
_UNKNOWN_TARGET = Route("unknown_target", "human", "unknown_target")
_PLAN_MSG_RE = re.compile(r"classroom-plan\.md|课堂计划|教师讲解方案")

_SEC_FILE_RE = re.compile(r"\b(sec[0-9A-Za-z]+)\.tex\b")
_SEC_ID_RE = re.compile(r"^(sec[0-9A-Za-z]+)(?:[-_.#].*)?$")
_SEC_WORD_RE = re.compile(r"\b(sec[0-9A-Za-z_-]*[0-9A-Za-z])\b")


# ---------------------------------------------------------------------------
# 适配
# ---------------------------------------------------------------------------

def _severity(raw: Any, default: str = "error") -> str:
    """原始严重度 → error|warning|info。无法识别 → 默认 error（绝不静默降级）。"""
    s = str(raw or "").strip().lower()
    if s in ("error", "high_risk", "fatal", "critical", "reject", "blocking", "err"):
        return "error"
    if s in ("warning", "warn", "review"):
        return "warning"
    if s in ("info", "note", "advisory", "remark"):
        return "info"
    return default


def locate_section(issue: Dict[str, Any]) -> Optional[str]:
    """从 scope / qid / artifact / message 定位切片（sec*）；定位不到返回 None。"""
    for key in ("scope", "qid", "artifact"):
        val = str(issue.get(key) or "")
        m = _SEC_ID_RE.match(val)
        if m:
            return m.group(1)
        m = _SEC_FILE_RE.search(val)
        if m:
            return m.group(1)
    msg = str(issue.get("message") or "")
    m = _SEC_FILE_RE.search(msg) or _SEC_WORD_RE.search(msg)
    return m.group(1) if m else None


def _lookup_route(code: str, category: str, gate: str, message: str,
                  severity: str, review: bool, report_category: str,
                  artifact: str = "") -> Route:
    """显式规则表查路由；查不到 → unclassified（human）。"""
    if review:
        # compare 的“疑似泄露”：必须人工复核（写 review-notes），绝不派 Agent
        return Route("compare_review", "human", "review-notes")
    if code:
        if code in CODE_ROUTES:
            return CODE_ROUTES[code]
        for prefix, route in PREFIX_ROUTES:
            if code.startswith(prefix):
                return route
    if category in CATEGORY_ROUTES:
        return CATEGORY_ROUTES[category]
    if gate == "check_render":
        for rx, route in RENDER_MESSAGE_ROUTES:
            if rx.search(message):
                return route
    if gate == "check_budget":
        # 卡 07：按“产物名 / 精确消息”分流 artifact 类问题，而非按 category 猜文件
        art_l = artifact.replace("\\", "/").lower()
        if art_l.endswith("classroom-plan.md"):
            return _PLAN_ROUTE
        if art_l.endswith("student.pdf"):
            return _PDF_PAGE_ROUTE
    if _PAGE_RE.search(message):
        return _PDF_PAGE_ROUTE
    if gate == "check_budget":
        if severity != "error" and _LIMIT_RE.search(message):
            return _LIMIT_ROUTE
        if _PLAN_MSG_RE.search(message):
            return _PLAN_ROUTE
        if report_category in CATEGORY_ROUTES:
            return CATEGORY_ROUTES[report_category]
        if severity == "error" and (report_category == "artifact"
                                    or category == "artifact"):
            return _UNKNOWN_TARGET
    if severity != "error":
        if gate in STRICT_WARNING_GATES:
            return STRICT_WARNING_GATES[gate]
        return Route("advisory", "none", None)
    if gate in GATE_ERROR_ROUTES:
        return GATE_ERROR_ROUTES[gate]
    return _UNCLASSIFIED


def normalize_issue(raw: Any, *, gate: str = "", scope: str = "",
                    input_digest: str = "", artifact: str = "",
                    default_severity: str = "error", report_category: str = "",
                    strict: bool = False) -> Dict[str, Any]:
    """单条原始问题 → C2 issue 对象。原始条目整体保存在 ``evidence['raw']``。"""
    if isinstance(raw, str):
        raw_d: Dict[str, Any] = {"message": raw}
    elif isinstance(raw, dict):
        raw_d = raw
    else:
        raw_d = {"message": repr(raw)}
    raw_sev = raw_d.get("severity")
    review = str(raw_sev or "").strip().lower() == "review"
    severity = _severity(raw_sev, default_severity)
    code = str(raw_d.get("code") or "")
    message = str(raw_d.get("message") or raw_d.get("detail") or "")
    category_in = str(raw_d.get("category") or "")
    qid = str(raw_d.get("qid") or "")
    art = str(raw_d.get("artifact") or raw_d.get("file") or artifact or "")

    route = _lookup_route(code, category_in, gate, message, severity, review,
                          report_category, art)
    issue: Dict[str, Any] = {
        "code": code or route.category.upper(),
        "severity": severity,
        "category": route.category,
        "scope": str(raw_d.get("scope") or scope or ""),
        "qid": qid,
        "artifact": art,
        "message": message,
        "evidence": {"gate": gate, "raw": raw_d,
                     "raw_severity": str(raw_sev) if raw_sev is not None else None},
        "input_digest": input_digest,
        "repair_kind": route.repair_kind,
        "repair_target": route.repair_target,
        "blocking": False,
    }
    issue["blocking"] = _is_blocking(issue, route, strict, review)
    if route.repair_target == SECTION:
        sec = locate_section(issue)
        issue["repair_target"] = sec  # None = 定位不到 → decide_route 停机
        if sec is None and route.fallback:
            issue["evidence"]["fallback_target"] = route.fallback
    if route.category == "pdf_page_overflow":
        issue["artifact"] = issue["artifact"] or "student.pdf"
    if route.category == "classroom_plan":
        issue["artifact"] = issue["artifact"] or "classroom-plan.md"
    # warning/info 非阻断时不得带着“待派发”的修复种类（防止被误当作待办）
    if not issue["blocking"] and route.repair_kind in ("agent", "deterministic"):
        issue["repair_kind"] = "none"
        issue["repair_target"] = None if route.repair_target == SECTION \
            else issue["repair_target"]
    return issue


def _is_blocking(issue: Dict[str, Any], route: Route, strict: bool,
                 review: bool) -> bool:
    if review:
        return True  # 待复核项在人工写入有效 review-notes 前一律不放行
    if issue["severity"] == "error":
        return True  # 未知/已知 error 都阻断；唯一例外见 normalize_report 的退出码回声
    if issue["severity"] == "warning":
        return bool(strict and route.strict_blocks)
    return False


def _iter_raw(report: Dict[str, Any]):
    """门禁报告里的原始问题 → [(raw_item, default_severity)]。"""
    raw = report.get("raw_report")
    out = []
    if isinstance(raw, dict):
        for it in raw.get("issues") or []:
            out.append((it, "error"))
        for it in raw.get("errors") or []:
            out.append((it, "error"))
        for it in raw.get("warnings") or []:
            out.append((it, "warning"))
    if out:
        return out
    # 无 raw_report（合成/旧形态）：退回 GateReport.issues
    for it in report.get("issues") or []:
        if isinstance(it, dict) and str(it.get("category") or "") in INFRA_CATEGORIES:
            continue
        out.append((it, "error"))
    return out


def normalize_report(report: Optional[Dict[str, Any]], gate: str = "", *,
                     scope: str = "", strict: bool = False,
                     content_exit_failure: bool = False) -> List[Dict[str, Any]]:
    """GateReport（含 raw_report）→ C2 issue 列表；原报告对象不被修改。

    规则：
      * ERROR / 报告缺失或过期 → 阻断的基础设施 issue（human）；
      * 门禁以非 0 退出码结束但报告里已有 error 级条目 → EXECUTION_FAILURE 只是
        退出码回声（blocking=False，保留证据），真正的阻断由原条目承担；
      * 状态非 PASS 却解释不出任何问题 → ``unclassified``（缺证据即停机）。
    """
    if not report:
        return [normalize_issue(
            {"severity": "error", "category": "REPORT_MISSING_OR_STALE",
             "message": f"门禁 {gate or '?'} 无报告"}, gate=gate, scope=scope,
            strict=strict)]
    gate = gate or str(report.get("gate_name") or "")
    digest = str(report.get("input_digest") or report.get("batch_input_digest") or "")
    raw_rep = report.get("raw_report") if isinstance(report.get("raw_report"), dict) else {}
    rep_cat = str(report.get("category") or raw_rep.get("category") or "").lower().strip()
    status = str(report.get("status") or "").upper()

    out: List[Dict[str, Any]] = []
    for item, dflt in _iter_raw(report):
        out.append(normalize_issue(item, gate=gate, scope=scope, input_digest=digest,
                                   default_severity=dflt, report_category=rep_cat,
                                   strict=strict))
    has_real_error = any(i["severity"] == "error" and i["blocking"] for i in out)

    for it in report.get("issues") or []:
        if isinstance(it, dict) and str(it.get("category") or "") in INFRA_CATEGORIES:
            if (content_exit_failure and it.get("category") == "EXECUTION_FAILURE"
                    and status != "ERROR" and gate in GATE_ERROR_ROUTES):
                # 编译器类门禁（build）：非 0 退出本身就是内容失败，按门禁默认路由
                # （定位到切片才派 Agent）；status=ERROR 仍是基础设施失败。
                issue = normalize_issue({"severity": "error", "message": str(it.get("message") or ""),
                                         "category": ""}, gate=gate, scope=scope,
                                        input_digest=digest, strict=strict)
                issue["evidence"]["raw"] = dict(it)
                out.append(issue)
                continue
            issue = normalize_issue({**it, "severity": "error"}, gate=gate, scope=scope,
                                    input_digest=digest, strict=strict)
            if has_real_error and it.get("category") == "EXECUTION_FAILURE" \
                    and status != "ERROR":
                issue["blocking"] = False
                issue["category"] = "exit_code_echo"
                issue["repair_kind"] = "none"
                issue["repair_target"] = None
            out.append(issue)

    passed = bool(report.get("passed")) and status in ("", "PASS")
    has_blocking = any(i["blocking"] for i in out)
    if not passed and not has_blocking:
        if status == "ERROR":
            out.append(normalize_issue(
                {"severity": "error", "category": "EXECUTION_FAILURE",
                 "message": f"门禁 {gate} 执行错误（status=ERROR）"},
                gate=gate, scope=scope, input_digest=digest, strict=strict))
        else:
            out.append(normalize_issue(
                {"severity": "error", "code": "GATE_UNEXPLAINED_FAILURE",
                 "message": f"门禁 {gate} 状态 {status or 'UNKNOWN'} 但无可解释问题（缺证据）"},
                gate=gate, scope=scope, input_digest=digest, strict=strict))
    return out


# ---------------------------------------------------------------------------
# 路由裁决
# ---------------------------------------------------------------------------

def decide_route(issues: List[Dict[str, Any]], *,
                 located_target: Optional[str] = None) -> Dict[str, Any]:
    """issue 列表 → 路由裁决（不改 routing_action 枚举，只落到既有四个动作）。

    返回 ``{action, repair_kind, repair_target, dispatch_agent, reason,
    blocking_issues, unclassified}``：
      * 无阻断 → PROCEED（warning/info 只作提示）；
      * 任一阻断 issue 为 human（含 unclassified/基础设施/范围契约）→ HALT；
      * agent 且定位到切片 → REPAIR_SECTION；agent 且目标是地图类 → REPLAN；
        agent 定位不到（且无 fallback）→ HALT（不盲改）；
      * 仅 deterministic 阻断 → HALT（由确定性工具/人修，绝不派 Agent）。
    ``located_target`` 是调用方用既有定位器（日志/宏回扫）已确认的切片，
    仅对 agent 类且 issue 自身定位不到时作补充。
    """
    blocking = [i for i in issues if i.get("blocking")]
    if not blocking:
        return _decision("PROCEED", "none", None, "", [], [])
    unclassified = [i for i in blocking if i["category"] == "unclassified"
                    or i["code"] == "GATE_UNEXPLAINED_FAILURE"]
    humans = [i for i in blocking if i["repair_kind"] == "human"]
    if humans:
        first = humans[0]
        return _decision("HALT", "human", first["repair_target"],
                         f"需人工处理（{first['category']}）: {first['message'][:120]}",
                         blocking, unclassified)
    agents = [i for i in blocking if i["repair_kind"] == "agent"]
    dets = [i for i in blocking if i["repair_kind"] == "deterministic"]
    if agents:
        for i in agents:
            tgt = i["repair_target"] or located_target
            if tgt is None and i["evidence"].get("fallback_target"):
                tgt = i["evidence"]["fallback_target"]
            if tgt is None:
                return _decision("HALT", "human", None,
                                 f"无法定位到具体切片（{i['category']}），停止而非盲改: "
                                 f"{i['message'][:120]}", blocking, unclassified)
            action = "REPLAN" if tgt in REPLAN_TARGETS else "REPAIR_SECTION"
            return _decision(action, "agent", tgt, f"{i['category']}: {i['message'][:120]}",
                             blocking, unclassified)
    if dets:
        i = dets[0]
        return _decision("HALT", "deterministic", i["repair_target"],
                         f"需确定性修复（{i['category']}→{i['repair_target']}），不派 Agent: "
                         f"{i['message'][:120]}", blocking, unclassified)
    # 阻断但 repair_kind=none：规则表缺口，按未知 error 停机
    first = blocking[0]
    return _decision("HALT", "human", None,
                     f"阻断项缺少修复分类（{first['category']}）", blocking, blocking)


def _decision(action: str, kind: str, target: Optional[str], reason: str,
              blocking: List[Dict[str, Any]], unclassified: List[Dict[str, Any]]
              ) -> Dict[str, Any]:
    return {
        "action": action,
        "repair_kind": kind,
        "repair_target": target,
        "dispatch_agent": action in ("REPAIR_SECTION", "REPLAN"),
        "reason": reason,
        "blocking_issues": blocking,
        "unclassified": unclassified,
    }


def route_gate_report(report: Optional[Dict[str, Any]], gate: str = "", *,
                      located_target: Optional[str] = None, scope: str = "",
                      strict: bool = False, content_exit_failure: bool = False) -> Dict[str, Any]:
    """便捷入口：GateReport → 路由裁决（附 ``issues``）。"""
    issues = normalize_report(report, gate, scope=scope, strict=strict,
                              content_exit_failure=content_exit_failure)
    decision = decide_route(issues, located_target=located_target)
    decision["issues"] = issues
    return decision


def infrastructure_failure(report: Optional[Dict[str, Any]]) -> Optional[str]:
    """门禁基础设施失败（崩溃/报告缺失或过期/ERROR）→ 原因串；否则 None。

    这类失败与内容无关，派作者重写任何切片都不可能修好。
    """
    if not report:
        return "门禁无报告"
    if str(report.get("status") or "").upper() == "ERROR":
        return f"门禁 {report.get('gate_name') or '?'} 执行错误（status=ERROR）"
    for it in report.get("issues") or []:
        if isinstance(it, dict) and it.get("category") == "REPORT_MISSING_OR_STALE":
            return str(it.get("message") or "门禁报告缺失或过期")
    return None
