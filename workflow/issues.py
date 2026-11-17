"""issues.py — 门禁问题的内部适配层与显式路由表（修复卡 05，DATA-CONTRACTS C2）。

各门禁脚本历史上报告形态不一（``{severity, message}``、``errors[].code``、
GateReport 合成的 ``{category, severity: HIGH_RISK|WARN|INFO}``）。本模块把它们
**在内存里**适配成 C2 统一 issue 对象，不改任何旧报告文件、不丢原始证据
（原条目整体放进 ``evidence.raw``）：

    {code, severity(error|warning|info), category, scope, qid, artifact, message,
     evidence, input_digest, repair_kind(none|deterministic|environment|asset|agent|human),
     repair_target, blocking}

铁律：
  * ``blocking`` 由显式规则表算出，不由调用方猜；
  * **未知 error 不降级**：落入 ``unclassified``（human + blocking），路由为 HALT；
  * warning / info / review 绝不自动派 Agent（review 要人工复核，info 只提示）；
  * 渲染器 / 资源 / 目录 / 引擎类缺陷绝不派写作者；
  * **唯一判据**：修复动作是否落在 ``sec*.tex`` 之内。不在就绝不派 author
    （resource/environment/deterministic 三类都走这条），不靠门禁名猜；
  * agent 类问题定位不到切片 → 停机，不回退到“上一节 / 第一节”盲改；
  * 基础设施失败（ERROR / 报告缺失或过期 / 门禁崩溃）→ 停机，不拿内容重试顶替；
  * 兜底必须**收敛**：没有前缀大锤——未登记码 HALT 并如实报缺口
    （``route_table_coverage``），绝不把它放大成全链重跑。
"""

import re
from typing import Any, Dict, List, NamedTuple, Optional

SEVERITIES = ("error", "warning", "info")
#: ``environment`` = 缺 TeX 工具/字体/引擎能力；``asset`` = 缺图片等资源文件。
#: 两者都不是内容问题——派写作者改 sec*.tex 永远修不好，只会白烧返修预算。
REPAIR_KINDS = ("none", "deterministic", "environment", "asset",
                "agent", "human")

#: GateReport 合成的基础设施类 category（script_runner._synthesize_gate_report）
INFRA_CATEGORIES = ("EXECUTION_FAILURE", "REPORT_MISSING_OR_STALE")

#: 路由目标里表示“从 issue 里定位切片”的占位
SECTION = "@section"
#: 路由到 REPLAN 的地图类目标
REPLAN_TARGETS = ("source-map.md", "coverage-map.md")
#: 非内容类修复目标（停机后由环境/资源侧处理，绝不派写作者）
ASSET_TARGET = "asset-harvest"
ENV_TARGET = "tex-environment"


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
    # 未登记的扩写/新增/顺序变动：修动作落在 sec*.tex 内（补登记或删改），派作者
    "FIDELITY_EXTEND_NOT_ALLOWED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_EXTEND_PENDING": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_EXTEND_UNAPPROVED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_NOVELTY_UNAPPROVED": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    "FIDELITY_ORDER_VIOLATION": Route("fidelity_section", "agent", SECTION, fallback=_SRC),
    # --- 映射契约（source-map.md）→ 最小重规划映射，绝不回头改切片 ---
    "FIDELITY_MAP_MISSING": Route("mapping", "agent", _SRC),
    "FIDELITY_MAP_NO_ROWS": Route("mapping", "agent", _SRC),
    "FIDELITY_MAP_ROW_FORMAT": Route("mapping", "agent", _SRC),
    "FIDELITY_MAP_BAD_ACTION": Route("mapping", "agent", _SRC),
    "FIDELITY_MAP_BAD_STATUS": Route("mapping", "agent", _SRC),
    "FIDELITY_MANIFEST_MISSING": Route("mapping", "agent", _SRC),
    "FIDELITY_INVENTORY_MISSING": Route("mapping", "agent", _SRC),
    "FIDELITY_CONTRACT_MISSING": Route("mapping", "agent", _SRC),
    "FIDELITY_SOURCE_MISSING": Route("mapping", "agent", _SRC),
    "FIDELITY_SOURCE_DIGEST_MISMATCH": Route("mapping", "agent", _SRC),
    "FIDELITY_UNKNOWN_SOURCE_ID": Route("mapping", "agent", _SRC),
    "FIDELITY_EXCLUDED_NOT_RECORDED": Route("mapping", "agent", _SRC),
    "FIDELITY_EXCLUSION_MISMATCH": Route("mapping", "agent", _SRC),
    # --- 资源定位：找不到资源本体 → 资源收割，不是内容问题 ---
    "FIDELITY_ASSET_SOURCE_UNREADABLE": Route("asset_source", "asset", ASSET_TARGET),
    "FIDELITY_ASSET_LOCATOR_UNKNOWN": Route("asset_locator", "asset", ASSET_TARGET),
    # --- 映射不一致 → 最小重规划映射 ---
    "FIDELITY_COVERAGE_MISSING": Route("mapping", "agent", _COV),
    "FIDELITY_COVERAGE_FORMAT": Route("mapping", "agent", _COV),
    # 缺口登记（写进 coverage-map.md 的缺口表）→ 只改覆盖表
    "FIDELITY_GAP_LOCATOR_UNKNOWN": Route("mapping_gap", "agent", _COV),
    "FIDELITY_GAP_NO_REASON": Route("mapping_gap", "agent", _COV),
    "FIDELITY_GAP_NO_SUCH_ASSET": Route("mapping_gap", "agent", _COV),
    "FIDELITY_GAP_REDUNDANT": Route("mapping_gap", "agent", _COV),
    "FIDELITY_GAP_STALE": Route("mapping_gap", "agent", _COV),
    "FIDELITY_GAP_UNKNOWN_SOURCE": Route("mapping_gap", "agent", _COV),
    # --- LaTeX 构建：把「缺资源 / 缺环境 / 内容语法」彻底分开 ---
    # 缺图：资源问题 → 收割资源，绝不派作者（本次 run 白烧一轮返修的直接原因）
    "LATEX_ASSET_MISSING": Route("latex_asset", "asset", ASSET_TARGET),
    "LATEX_ASSET_OPAQUE": Route("latex_asset", "asset", ASSET_TARGET),
    # 缺字体/缺工具/编译环境 → 环境问题 → 停机交环境侧
    "LATEX_FONT_MISSING": Route("latex_env", "environment", ENV_TARGET),
    "LATEX_ENV_MISSING": Route("latex_env", "environment", ENV_TARGET),
    "LATEX_STALE_OUTPUT": Route("latex_env", "environment", ENV_TARGET),
    "LATEX_CONFIG_FAILED": Route("latex_env", "environment", ENV_TARGET),
    "LATEX_SOURCE_MISSING": Route("latex_env", "environment", ENV_TARGET),
    "LATEX_COUNTER_MISMATCH": Route("latex_engine", "environment", ENV_TARGET),
    "LATEX_INPUT_CHANGED": Route("latex_engine", "environment", ENV_TARGET),
    # 真正的语法错 → 作者可修
    "LATEX_SYNTAX_ERROR": Route("latex_syntax", "agent", SECTION),
    # 卡片解析/校验失败 = 渲染器自身缺陷（如节标题正则不容嵌套花括号）→ 修渲染器
    "LATEX_DECK_PARSE": Route("render_engine", "deterministic", "renderer"),
    # 选项打乱问题来自 \fillin 选项 → 内容
    "LATEX_SHUFFLE": Route("latex_shuffle", "agent", SECTION),
    # 版式溢出：作者改不动版式，环境侧处理
    "LATEX_OVERFULL": Route("latex_overfull", "environment", "pdf-layout"),
    "LATEX_SUMMARY_MISSING": Route("latex_engine", "environment", ENV_TARGET),
    # --- 编号结构（check_numbering）：全部落在 sec*.tex 内，除装配问题 ---
    "NUMBERING_FILLIN_OPTIONS": Route("numbering_fillin", "agent", SECTION),
    "NUMBERING_ENV_UNBALANCED": Route("numbering_env", "agent", SECTION),
    "NUMBERING_TEACHER_ENV_COUNT": Route("numbering_teacher", "agent", SECTION),
    "NUMBERING_HINT_VISIBLE": Route("numbering_hint", "agent", SECTION),
    "NUMBERING_DUPLICATE_QID": Route("numbering_qid", "agent", SECTION),
    "NUMBERING_PRACTICEMODE": Route("numbering_mode", "agent", SECTION),
    "NUMBERING_PROBCHAIN_NESTED": Route("numbering_chain", "agent", SECTION),
    "NUMBERING_FORBIDDEN_GLOBAL": Route("numbering_global", "agent", SECTION),
    "NUMBERING_KNOWLEDGEBOX_MISSING": Route("numbering_box", "agent", SECTION),
    "NUMBERING_MICROKNOWLEDGE_MISSING": Route("numbering_pre", "agent", SECTION),
    "NUMBERING_PROBCHAIN_EMPTY": Route("numbering_chain", "agent", SECTION),
    "NUMBERING_LEGACY_SERIES": Route("numbering_legacy", "agent", SECTION),
    # 兜底码：编号类问题除装配外一律落在 sec*.tex 内（仍需定位成功，否则 HALT）
    "NUMBERING_OTHER": Route("numbering_generic", "agent", SECTION),
    # 装配问题由引擎自己的 t2_assemble_main 产生，作者改不了
    "NUMBERING_ASSEMBLY": Route("numbering_assembly", "environment", ENV_TARGET),
    # --- 对话层（check_dialogue）：主体是内容，配置/覆盖表分离出来 ---
    "DIALOGUE_CONFIG_ERROR": Route("dialogue_config", "environment", ENV_TARGET),
    "DIALOGUE_COVERAGE_MISSING": Route("dialogue_coverage", "agent", _COV),
    "DIALOGUE_OPTOUT_MISSING": Route("dialogue_optout", "agent", SECTION),
    "DIALOGUE_STRUCTURE": Route("dialogue_section", "agent", SECTION),
    # 兜底码：对话层结构问题一律落在 sec*.tex 内（仍需定位成功）
    "DIALOGUE_OTHER": Route("dialogue_generic", "agent", SECTION),
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

#: 前缀兜底（按顺序匹配；精确码优先）。
#:
#: N0 起 **不再有 ``FIDELITY_`` 整族前缀兜底**：那条兜底会把任何未登记的
#: fidelity 码升级成 "REPLAN source-map.md"（重跑整个 T1 来源映射链）。本次 run
#: 里一个 coverage-map 单元格内的裸竖线就是这么把一次单字符修复放大成 T1 全链
#: 重跑的。现在 fidelity 的每个真实码都在 CODE_ROUTES 里逐条登记；未登记的码
#: 落到 ``_UNCLASSIFIED`` → HALT + 如实报缺口（收敛式兜底，见 check_route_coverage）。
PREFIX_ROUTES = (
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

#: 无 code 的门禁报告 → 用消息特征补出等价 code（N0：分类学完备性最后一道网）。
#: 只在报告确实没给 code 时使用；给出 code 的报告一律以 code 为准。
MESSAGE_CODE_ROUTES = (
    # ---- build / LaTeX：先判资源，再判环境，最后才是内容语法 ----
    (re.compile(r"Unable to load picture|File `[^`']+\.(?:png|jpg|jpeg|pdf|eps|svg)' "
                r"not found|图片资产缺失|graphics file .* not found", re.I),
     "LATEX_ASSET_MISSING"),
    (re.compile(r"Font .* not (?:found|loadable)|Missing character|fontspec.*font", re.I),
     "LATEX_FONT_MISSING"),
    (re.compile(r"缺少 xelatex|缺少 latexmk|xelatex 依赖|env.*missing|未安装", re.I),
     "LATEX_ENV_MISSING"),
    (re.compile(r"双版 .* 计数不一致|SUMMARY 行", re.I), "LATEX_COUNTER_MISMATCH"),
    (re.compile(r"不是本次编译产物|不是本次构建产物", re.I), "LATEX_STALE_OUTPUT"),
    (re.compile(r"配置生成失败", re.I), "LATEX_CONFIG_FAILED"),
    (re.compile(r"缺少 \S+\.tex$", re.I), "LATEX_SOURCE_MISSING"),
    (re.compile(r"Overfull \\[hv]box|版式溢出", re.I), "LATEX_OVERFULL"),
    (re.compile(r"(^|\s)!.*|Undefined control sequence|Missing \$|Extra \}|Runaway argument",
                re.I), "LATEX_SYNTAX_ERROR"),
    # ---- check_numbering ----
    (re.compile(r"\\fillin.*(选项|参考答案|空项)", re.I), "NUMBERING_FILLIN_OPTIONS"),
    (re.compile(r"\\item 出现在列表环境之外|没有配对的 \\begin|交叉闭合|未闭合"
                r"|环境内没有任何 \\item", re.I), "NUMBERING_ENV_UNBALANCED"),
    (re.compile(r"恰好含一个 .* 环境", re.I), "NUMBERING_TEACHER_ENV_COUNT"),
    (re.compile(r"unprompted 题目块内出现学生版可见提示标记", re.I),
     "NUMBERING_HINT_VISIBLE"),
    (re.compile(r"qid .* 重复", re.I), "NUMBERING_DUPLICATE_QID"),
    (re.compile(r"\\practicemode", re.I), "NUMBERING_PRACTICEMODE"),
    (re.compile(r"probchain 嵌套", re.I), "NUMBERING_PROBCHAIN_NESTED"),
    (re.compile(r"不得改写全局状态", re.I), "NUMBERING_FORBIDDEN_GLOBAL"),
    (re.compile(r"缺失章节末知识总结框", re.I), "NUMBERING_KNOWLEDGEBOX_MISSING"),
    (re.compile(r"缺失前置知识卡", re.I), "NUMBERING_MICROKNOWLEDGE_MISSING"),
    (re.compile(r"probchain 内没有可见题目", re.I), "NUMBERING_PROBCHAIN_EMPTY"),
    (re.compile(r"series=probchain|resume=probchain", re.I), "NUMBERING_LEGACY_SERIES"),
    (re.compile(r"未在 main\.tex 中装配|重复装配|切片编号 .* 同时占用"
                r"|切片引入顺序", re.I), "NUMBERING_ASSEMBLY"),
    # ---- check_dialogue ----    (re.compile(r"config\.yaml 解析失败|工作目录不存在", re.I), "DIALOGUE_CONFIG_ERROR"),
    (re.compile(r"缺少 coverage-map\.md", re.I), "DIALOGUE_COVERAGE_MISSING"),
    (re.compile(r"已声明 .*off-(section|example)", re.I), "DIALOGUE_OPTOUT_MISSING"),
    (re.compile(r"sectiondialogue|exampledialogue|\\speaker|对话块|dialogue:", re.I),
     "DIALOGUE_STRUCTURE"),
    # ---- check_fidelity（历史报告只给中文描述，不给 code）----
    (re.compile(r"原文主体未覆盖|来源未覆盖|未被覆盖|未覆盖.*来源", re.I),
     "FIDELITY_SOURCE_NOT_COVERED"),
    (re.compile(r"未登记的扩写|未批准.*扩展|扩展未经批准|EXTEND", re.I),
     "FIDELITY_EXTEND_UNAPPROVED"),
    (re.compile(r"新增了原文没有|原文没有的新对象", re.I), "FIDELITY_NOVELTY_UNAPPROVED"),
    (re.compile(r"映射行格式|映射表", re.I), "FIDELITY_MAP_ROW_FORMAT"),
    (re.compile(r"来源 ID .*未登记|未知来源 ID", re.I), "FIDELITY_UNKNOWN_SOURCE_ID"),
)


def infer_code_from_message(gate: str, message: str) -> Optional[str]:
    """报告没给 code 时，用消息特征补出等价 code；补不出返回 None。"""
    if not message:
        return None
    msg = str(message)
    for rx, code in MESSAGE_CODE_ROUTES:
        if rx.search(msg):
            # 只允许补出本门禁确有资格产出的码，避免跨门禁误判
            if code in CODE_ROUTES:
                return code
    return None

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
    if not code and category not in INFRA_CATEGORIES:
        # 报告没给 code：用消息特征补出等价 code（分类学完备性的最后一道网）。
        # 位置在门禁专属 message 规则之后——有专属规则的门禁保持既有优先级。
        inferred = infer_code_from_message(gate, message)
        if inferred:
            return CODE_ROUTES[inferred]
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
        "code": code or route.category.upper(),        "severity": severity,
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
    if not issue["blocking"] and route.repair_kind in (
            "agent", "deterministic", "environment", "asset"):
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
    if unclassified:
        # N0：兜底必须收敛。未登记的错误（既没有 code 路由、也没有类别路由、
        # 门禁还没给可推断的消息特征）不再被前缀/类目大锤放大成整链重跑——
        # 而是停机并把「缺口」原样写进 error_message，促使第一次遇到就补表。
        first = unclassified[0]
        gap = first.get("code") or first.get("category") or "unclassified"
        return _decision("HALT", "human", first["repair_target"] or "unclassified",
                         f"未登记的错误（路由表缺口，需补 CODE_ROUTES 后重跑）: "
                         f"{gap} → {first['message'][:120]}",
                         blocking, unclassified)
    if humans:
        first = humans[0]
        return _decision("HALT", "human", first["repair_target"],
                         f"需人工处理（{first['category']}）: {first['message'][:120]}",
                         blocking, unclassified)
    agents = [i for i in blocking if i["repair_kind"] == "agent"]
    dets = [i for i in blocking if i["repair_kind"] == "deterministic"]
    # 资源 / 环境类优先于内容类：编译环境或资源缺口没补齐时，任何内容返修都是空转
    # （本次 run 的缺图返修就是这样白烧的）。派写作者改 sec*.tex 永远修不好它们。
    for kind, label in (("asset", "需补齐资源"), ("environment", "需环境侧处理")):
        hits = [i for i in blocking if i["repair_kind"] == kind]
        if hits:
            first = hits[0]
            return _decision("HALT", kind, first["repair_target"],
                             f"{label}（{first['category']}→"
                             f"{first['repair_target'] or kind}），不派 Agent: "
                             f"{first['message'][:120]}", blocking, unclassified)
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
        # RC-2：确定性修复不再是"死标签 + HALT"。动作是 REPAIR_DETERMINISTIC，
        # 由 repair_deterministic 节点执行 scripts/patch_artifact.py（白名单 op +
        # CAS）。仍然 dispatch_agent=False——确定性修复绝不派内容写作者。
        return _decision("REPAIR_DETERMINISTIC", "deterministic", i["repair_target"],
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
        # 确定性修复由引擎执行（repair_deterministic 节点），不是 Agent
        "engine_action": action if action == "REPAIR_DETERMINISTIC" else "",
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


# ---------------------------------------------------------------------------
# 覆盖门禁（ROUTE_TABLE_COVERAGE）
# ---------------------------------------------------------------------------

def route_table_coverage(codes) -> Dict[str, List[str]]:
    """给定「门禁实际会产出的 code 全集」，返回路由表缺口。

    返回 ``{known_count, missing}``：``missing`` 非空即说明有门禁会产出本表查不到
    的码——那类问题最后只能落到 ``unclassified``（HALT + 报缺口）。宁可停机报缺口，
    也不要再出现「未登记码被前缀兜底放大成全链重跑」。

    注意：``SCOPE_`` 前缀仍在 PREFIX_ROUTES 里，故 SCOPE_* 不算缺口。
    """
    known = set(CODE_ROUTES)
    prefixes = tuple(p for p, _ in PREFIX_ROUTES)
    covered = {str(c) for c in codes if c and any(str(c).startswith(p)
                                                  for p in prefixes)}
    missing = sorted({str(c) for c in codes if c} - known - covered)
    return {"known_count": len(known), "missing": missing}


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
