#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "2.0"
r"""
check_budget.py — 题链教学预算、长链密度与翻转教案完备性门禁

检查项:
  1. 确认态对账 (修复 F2):
     - 校验 config.yaml 与 .pd/confirmed-spec.json 是否一致，防止课时/交付配置篡改；
  2. 推荐讲法与易错点支撑 (N1 教师侧支撑，硬阻断):
     - coverage-map.md 中每个 core/support 任务的“推荐讲法”与“易错点”必须明确填写，不得留空或填占位符；
  3. 结构化时长估算与硬预算核验 (修复 F4, F5):
     - 调用 estimate_minutes 结构化估算；
     - 公式为 grand_total = opening + closing + buffer + core + support_inclass；
     - teaching / classroom 模式下，grand_total 不得超过教学预算，取消 20% 软顶豁免（硬阻断）；
     - self_study 自学模式下不设时长上限；
  4. 核心任务数量上限 (Core Task Limit):
     - core 任务数量不得超过限制（默认 12 道）；
  5. 翻转教案与页数交付检查 (Stage: delivery，修复 F6, F7, F8):
     - classroom-plan.md 必须已生成且填充实质内容（非空白占位模板，长度 >= 200）；
     - student.pdf 页数不得超过上限（默认 6 页），无法解析时显式记录 SKIPPED；
  6. 结构化分类输出 (修复 F9):
     - category: ok | coverage | section | config | artifact，严格符合 C3 契约。

用法: python check_budget.py [工作目录] [--stage {planning,delivery}] [--json PATH]
退出码: 0=通过, 1=发现问题, 2=缺少输入。
"""
import argparse
import hashlib
import json
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import evidence  # noqa: E402
import gen_config  # noqa: E402
import gen_classroom_plan  # noqa: E402
import estimate_minutes  # noqa: E402
# 卡 11：预算/教案策略复用 C1 的解析器（check_pedagogy.load_policy →
# workflow/execution_policy），不另造开关。
import check_pedagogy  # noqa: E402

PLACEHOLDER_WORDS = {"", "-", "--", "---", "无", "待定", "暂无", "略", "none", "null"}

#: C10 教师课堂选用范围文件（相对项目目录）
SELECTION_REL = os.path.join(".pd", "classroom-selection.json")
#: 固定开场/收束/缓冲预算（与 estimate_minutes 的 DEFAULT_OPENING/CLOSING/BUFFER 一致）
DEFAULT_SCOPE_OVERHEAD = 15.0


def load_classroom_selection(pdir):
    """读取 C10 教师课堂选用范围。

    - 文件不存在 → ``state="unspecified"``（**不是**零题）：报告全量容量；
    - 空数组 → ``state="specified"`` 且 0 题（教师明确选择不授课）；
    - 只认 schema_version=1 的 ``selected_carriers[{section,qid}]``；
    - 重复条目去重后计入 ``duplicates``；不存在项由调用方拒绝并定位。
    """
    path = os.path.join(pdir, SELECTION_REL)
    info = {"state": "unspecified", "entries": [], "duplicates": 0,
            "schema_version": None, "digest": None, "error": None}
    if not os.path.isfile(path):
        return info
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        info["error"] = "读取失败: %s" % e
        return info
    if not isinstance(data, dict):
        info["error"] = "顶层必须是 JSON 对象"
        return info
    info["state"] = "specified"
    info["schema_version"] = data.get("schema_version")
    if info["schema_version"] != 1:
        info["error"] = "schema_version 必须为 1（收到 %r）" % (info["schema_version"],)
        return info
    raw = data.get("selected_carriers")
    if not isinstance(raw, list):
        info["error"] = "缺少 selected_carriers 数组"
        return info
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            info["error"] = "selected_carriers 条目必须是 {section, qid} 对象"
            return info
        sec = str(item.get("section") or "").strip()
        qid = str(item.get("qid") or "").strip()
        if not sec or not qid:
            info["error"] = "selected_carriers 条目缺少 section 或 qid"
            return info
        if (sec, qid) in seen:
            info["duplicates"] += 1
            continue
        seen.add((sec, qid))
        info["entries"].append((sec, qid))
    canon = json.dumps({"schema_version": 1,
                        "selected_carriers": [{"section": s, "qid": q}
                                              for s, q in info["entries"]]},
                       ensure_ascii=False, sort_keys=True)
    info["digest"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return info


def carrier_key(row):
    """课堂任务的唯一标识：(section, carrier_qid)。"""
    return (str(row.get("section") or "").strip(),
            str(row.get("carrier_qid") or row.get("node_id") or "").strip())


def unique_carrier_rows(rows):
    """按 (section, carrier_qid) 去重，返回 (唯一载体行, 原文行数)。"""
    seen = set()
    out = []
    for r in rows:
        k = carrier_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, len(rows)


class BudgetResult(tuple):
    """兼顾元组解包 (problems, warnings, stats) 与属性访问/分类访问的门禁结果类。"""
    def __new__(cls, problems, warnings, stats, category="ok", estimates_ref=None,
                issue_meta=None):
        return super().__new__(cls, (problems, warnings, stats))

    def __init__(self, problems, warnings, stats, category="ok", estimates_ref=None,
                 issue_meta=None):
        self.problems = problems
        # 卡 07：{problem 文本: {"code","artifact"}}——精确问题码 + 产物名，
        # 供路由按“码 + 产物”分流，而不是按 category 猜文件。
        self.issue_meta = dict(issue_meta or {})
        self.warnings = warnings
        self.stats = stats
        self.category = category
        self.estimates_ref = estimates_ref

    @property
    def passed(self):
        return len(self.problems) == 0

    def to_dict(self):
        return {
            "schema_version": 2,
            "tool": "check_budget.py",
            "tool_version": "2.0",
            "status": "pass" if self.passed else "fail",
            "category": self.category,
            "estimates_ref": self.estimates_ref,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }


def count_pdf_pages(pdf_path):
    """尝试获取 PDF 实际页数。"""
    if not os.path.isfile(pdf_path):
        return None
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        return len(reader.pages)
    except Exception:
        pass
    log_path = os.path.splitext(pdf_path)[0] + ".log"
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log_text = f.read()
            m = re.search(r"Output written on .*?\((\d+)\s+pages?", log_text)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def check_budget_project(pdir, stage="planning"):
    """执行预算、密度与教案门禁校验。"""
    pdir = os.path.abspath(pdir)
    cov_path = os.path.join(pdir, "coverage-map.md")
    problems = []
    warnings = []
    stats = {}

    config_problems = []
    coverage_problems = []
    section_problems = []
    artifact_problems = []
    issue_meta = {}

    def _tag(msg, code, artifact):
        issue_meta[msg] = {"code": code, "artifact": artifact}

    # 卡 11：预算/教案策略（faithful_fast 默认 advisory + 不要求教案；
    # 缺新策略的旧项目 → legacy：strict + 要求教案，行为一字不变）。
    try:
        policy = check_pedagogy.load_policy(pdir)
    except Exception as e:
        policy = {"execution_profile": "legacy", "pedagogy_policy": "strict",
                  "budget_policy": "strict", "require_classroom_plan": True,
                  "sources": {}}
        warnings.append(f"读取执行策略失败，按 legacy 严格语义处理: {e}")
    advisory_budget = str(policy.get("budget_policy", "strict")).lower() != "strict"
    stats["execution_profile"] = policy.get("execution_profile", "legacy")
    stats["pedagogy_policy"] = policy.get("pedagogy_policy", "strict")
    stats["budget_policy"] = policy.get("budget_policy", "strict")
    stats["require_classroom_plan"] = bool(policy.get("require_classroom_plan", True))
    stats["policy_sources"] = dict(policy.get("sources") or {})

    def _limit(msg):
        """容量/偏好类限制：advisory → 仅提示（教师自选范围）；strict → 阻断。"""
        if advisory_budget:
            warnings.append(msg)
        else:
            problems.append(msg)
            coverage_problems.append(msg)

    # 1. 确认态对账（修复 F2）
    cfg_path = os.path.join(pdir, "config.yaml")
    cfg = {}
    if os.path.isfile(cfg_path):
        try:
            cfg = gen_config.load_config(cfg_path)
        except Exception as e:
            msg = f"加载 config.yaml 异常: {e}"
            problems.append(msg)
            config_problems.append(msg)

    try:
        spec_errors = gen_config.verify_confirmed_spec(pdir, cfg)
        if spec_errors:
            for err in spec_errors:
                msg = f"确认态对账失败: {err}"
                problems.append(msg)
                config_problems.append(msg)
    except Exception as e:
        msg = f"确认态对账执行异常: {e}"
        problems.append(msg)
        config_problems.append(msg)

    profile = cfg.get("delivery_profile", "self_study").lower()
    is_teaching = profile in ("teaching", "classroom")

    # self_study 模式不再设时长上限；teaching 模式保留课时预算上限（默认 90 分钟）
    if is_teaching:
        default_budget = 90
        try:
            lesson_minutes = int(cfg.get("lesson_minutes", default_budget))
        except (ValueError, TypeError):
            lesson_minutes = default_budget
    else:
        lesson_minutes = None

    try:
        core_task_limit = int(cfg.get("core_task_limit", 12))
    except (ValueError, TypeError):
        core_task_limit = 12

    try:
        max_student_pages = int(cfg.get("max_student_pages", 6))
    except (ValueError, TypeError):
        max_student_pages = 6

    # 与 lesson_minutes 同理：self_study 模式不设学生版页面上限（自主学习
    # 没有课时墙，排版微调——如 GR.4 修复 CJK 混排后 6→7 页——不应阻断交付）；
    # teaching/classroom 模式保留 6 页硬顶（课时内必须发完）。
    if not is_teaching:
        max_student_pages = None

    # 2. coverage-map 检查
    estimates_ref = None
    if not os.path.isfile(cov_path):
        if stage == "planning":
            msg = "缺失 coverage-map.md，无法开展教学预算与规划检查"
            problems.append(msg)
            coverage_problems.append(msg)
        else:
            warnings.append("未发现 coverage-map.md，跳过预算与规划一致性核验")
        core_rows = []
        support_rows = []
    else:
        rows = gen_classroom_plan.parse_coverage_map(cov_path)
        if not rows:
            msg = "coverage-map.md 表格为空或格式未包含有效节点"
            problems.append(msg)
            coverage_problems.append(msg)
            core_rows = []
            support_rows = []
        else:
            core_rows = [r for r in rows if r.get("delivery_role") == "core"]
            support_rows = [r for r in rows if r.get("delivery_role") == "support"]

            # 3. 集成结构化估算（修复 F4）
            est_json_path = os.path.join(pdir, ".pd", "reports", "estimate.json")
            estimates_ref = ".pd/reports/estimate.json"
            est_data = {}
            try:
                try:
                    est_data = estimate_minutes.estimate_project(pdir, cfg=cfg, json_path=est_json_path)
                except TypeError:
                    est_data = estimate_minutes.estimate_project(pdir, cfg=cfg)

                if est_data:
                    try:
                        os.makedirs(os.path.dirname(est_json_path), exist_ok=True)
                        with open(est_json_path, "w", encoding="utf-8") as f:
                            json.dump(est_data, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
            except Exception as e:
                warnings.append(f"调用 estimate_minutes 失败: {e}")

            totals = est_data.get("totals", {}) if est_data else {}
            grand_total = totals.get("grand_total")
            core_minutes = totals.get("core")
            support_minutes = totals.get("support_inclass", 0.0)

            # Fallback 容错
            if core_minutes is None:
                core_minutes = sum(r.get("planned_minutes", 0) for r in core_rows)
            if grand_total is None:
                grand_total = core_minutes + support_minutes

            # 卡 11：课堂任务数按唯一 (section, carrier_qid) 统计——13 个原文
            # 节点共用同一载体只算 1 道任务；不同 section 的同名 qid 算 2 道。
            for r in rows:
                if not r.get("carrier_explicit", True):
                    msg = (
                        f"coverage-map 节点 {r.get('node_id')} 的 carrier_qid 单元格为空："
                        f"无法判定该节点对应哪一道课堂任务（数据不足，不能默认按独立任务计）"
                    )
                    problems.append(msg)
                    coverage_problems.append(msg)
                    _tag(msg, "BUDGET_CARRIER_MISSING", "coverage-map.md")
            unique_core, core_row_count = unique_carrier_rows(core_rows)
            unique_all, _all_row_count = unique_carrier_rows(core_rows + support_rows)

            stats["core_task_count"] = len(unique_core)
            stats["core_task_rows"] = core_row_count
            stats["core_minutes"] = core_minutes
            stats["support_minutes"] = support_minutes
            stats["grand_total"] = grand_total
            stats["lesson_minutes_budget"] = lesson_minutes
            stats["delivery_profile"] = profile
            stats["estimates_ref"] = estimates_ref

            # 卡 11 / C10：教师课堂选用范围。文件缺失 ≠ 零题——缺失时报告
            # 全量容量并标记“未指定课堂选择”，绝不臆测 optional 就是未选。
            selection = load_classroom_selection(pdir)
            row_keys = {carrier_key(r) for r in rows}
            stats["classroom_selection"] = selection["state"]
            stats["classroom_selection_duplicates"] = selection["duplicates"]
            stats["selection_digest"] = selection["digest"]
            if selection.get("error"):
                msg = (f"课堂选用范围 {SELECTION_REL.replace(os.sep, '/')} 无效："
                       f"{selection['error']}；请由教师重新选择授课范围")
                problems.append(msg)
                coverage_problems.append(msg)
                _tag(msg, "SELECTION_INVALID", "classroom-selection.json")
            selected_keys = set()
            for sec, qid in selection["entries"]:
                if (sec, qid) in row_keys:
                    selected_keys.add((sec, qid))
                else:
                    msg = (
                        f"课堂选用范围引用了不存在的载体 (section={sec}, qid={qid})："
                        f"coverage-map 中无此 (section, qid)，拒绝按该范围计算预算"
                    )
                    problems.append(msg)
                    coverage_problems.append(msg)
                    _tag(msg, "SELECTION_CARRIER_UNKNOWN", "classroom-selection.json")
            stats["classroom_selection_tasks"] = len(selected_keys)

            # 容量基线：coverage-map 的教师计划分钟 + 固定开场/收束/缓冲
            planned_core = sum(float(r.get("planned_minutes") or 0) for r in unique_core)
            planned_support = sum(float(r.get("planned_minutes") or 0)
                                  for r in unique_carrier_rows(support_rows)[0])
            overhead = round(float(grand_total or 0) - float(core_minutes or 0)
                             - float(support_minutes or 0), 2)
            if overhead <= 0:
                overhead = DEFAULT_SCOPE_OVERHEAD
            full_capacity = round(planned_core + planned_support + overhead, 2)
            if grand_total is not None:
                full_capacity = max(full_capacity, round(float(grand_total), 2))
            stats["full_capacity_minutes"] = full_capacity
            stats["scope_overhead_minutes"] = overhead

            selected_capacity = None
            if selection["state"] == "specified":
                sel_rows = [r for r in unique_all if carrier_key(r) in selected_keys]
                selected_capacity = round(
                    sum(float(r.get("planned_minutes") or 0) for r in sel_rows) + overhead, 2)
                stats["selected_minutes"] = selected_capacity
            else:
                warnings.append(
                    f"未指定课堂选择（{SELECTION_REL.replace(os.sep, '/')} 不存在）："
                    f"按全部材料统计课堂容量 {full_capacity} 分钟"
                    f"（core={planned_core}m, support={planned_support}m, 固定开场收束缓冲={overhead}m）。"
                    f"请教师在真实 UI 中选择授课范围；未选内容一律保留，不得为凑预算删减原文。"
                )

            # 3.1 覆盖身份精确匹配与防静默 fallback 拦截（修复 F4）
            if est_data:
                fallbacks = est_data.get("_fallback_matches", [])
                if fallbacks:
                    for fb in fallbacks:
                        msg = (
                            f"coverage-map 节点 {fb['node_id']} 的 qid "
                            f"'{fb['expected_qid']}' 在 {fb['section']} 中"
                            f"未精确匹配，顺序兜底到第 {fb['fallback_index']+1} "
                            f"道题（qid={fb['fallback_qid']}）；"
                            f"请检查 qid 拼写或补充 carrier_qid 显式复用"
                        )
                        problems.append(msg)
                        coverage_problems.append(msg)

                # TeX 侧多余题目检查：TeX 中有 qid 但 coverage-map 未列出
                tex_qids = set()
                sec_cache = {}
                for sec_name in {r.get("section") for r in rows if r.get("section")}:
                    for t in estimate_minutes.get_section_tasks(pdir, sec_name, sec_cache):
                        if t.get("qid"):
                            tex_qids.add(t["qid"])
                cov_qids = {r.get("qid") or r.get("carrier_qid")
                            for r in rows} - {None, ""}
                orphan_tex = tex_qids - cov_qids
                if orphan_tex:
                    msg = (
                        f"TeX 切片中存在 coverage-map 未列出的题目 qid：{sorted(orphan_tex)}；"
                        f"请在 coverage-map 中补充这些题目的角色分配，或确认是否为废弃题目"
                    )
                    warnings.append(msg)

            # 4. 推荐讲法与易错点非空校验 (N1 教师侧支撑)
            # 卡 11：讲法/易错点属教学偏好——faithful_fast + advisory 只提示。
            for r in core_rows + support_rows:
                qid = r.get("carrier_qid") or r.get("node_id")
                method = (r.get("teach_method") or "").strip().lower()
                pitfall = (r.get("pitfall") or "").strip().lower()

                if method in PLACEHOLDER_WORDS:
                    msg = f"{r.get('node_id')} ({qid}): 缺失“推荐讲法”（翻转课堂教师讲解必需支撑，严禁留空或填占位符）"
                    _limit(msg)
                if pitfall in PLACEHOLDER_WORDS:
                    msg = f"{r.get('node_id')} ({qid}): 缺失“易错点”（翻转课堂靶向突破必需支撑，严禁留空或填占位符）"
                    _limit(msg)

            # 5. 课时总时长上限 (N2 总量上限：硬约束，取消 20% 软顶豁免，修复 F5)
            # 卡 11：advisory 只提示教师缩小范围/调整预算，strict 才按已选范围阻断。
            if is_teaching and lesson_minutes is not None:
                scope_minutes = selected_capacity if selected_capacity is not None else full_capacity
                scope_desc = ("已选课堂范围" if selected_capacity is not None
                              else "全部材料（尚未指定课堂选择）")
                if scope_minutes > lesson_minutes:
                    diff = round(scope_minutes - lesson_minutes, 2)
                    if advisory_budget:
                        tail = ("建议缩小授课范围或在 Phase 0 调整明确预算；原文与未选内容必须保留，"
                                "不得为凑预算删减原文或压缩单题小问数。")
                    else:
                        tail = ("必须在 coverage-map 中删减题量、拆分切片或将次要变式转入拓展题库，"
                                "严禁通过压缩单题小问数来凑预算！")
                    msg = (
                        f"课堂教学耗时超标（{scope_desc}合计 {scope_minutes} 分钟 > 教学预算 "
                        f"{lesson_minutes} 分钟，超额 {diff} 分钟；其中 core={core_minutes}m, "
                        f"support_inclass={support_minutes}m）。{tail}"
                    )
                    _limit(msg)

            # 6. 核心题量上限（按唯一 (section, carrier_qid) 计，不按原文节点行数）
            if len(unique_core) > core_task_limit:
                msg = (
                    f"core 核心任务数量超标（当前 {len(unique_core)} 道 > 上限 {core_task_limit} 道）。"
                    "认知负荷过大，建议将辅助练习题转入 self-study 角色或拆课。"
                )
                _limit(msg)

    # 7. Delivery 阶段扩展检查 (修复 F6, F7, F8)
    if stage == "delivery":
        # 检查 classroom-plan.md 是否已生成且填充实质内容
        # 卡 11：教案是可选交付物——require_classroom_plan=false 时不要求额外
        # 教案，也不因缺教案阻断；true 时走“先生成后检查”（build_nodes 已先跑
        # gen_classroom_plan.py），仅模板转换失败才停。
        plan_path = os.path.join(pdir, "classroom-plan.md")
        require_plan = bool(policy.get("require_classroom_plan", True))
        if not require_plan:
            stats["classroom_plan"] = "not-required"
            warnings.append(
                "未要求课堂计划（require_classroom_plan=false）：跳过 classroom-plan.md 的生成与"
                "实质内容核验；教案属可选交付物，来源、数学与交付门禁不受影响。"
            )
        elif not os.path.isfile(plan_path):
            stats["classroom_plan"] = "missing"
            msg = "缺失 classroom-plan.md 教师讲解方案，请先运行 gen_classroom_plan.py 自动生成"
            problems.append(msg)
            artifact_problems.append(msg)
            _tag(msg, "CLASSROOM_PLAN_MISSING", "classroom-plan.md")
        else:
            stats["classroom_plan"] = "present"
            plan_ok = True
            plan_errs = []
            if hasattr(gen_classroom_plan, "check_plan_file"):
                try:
                    res = gen_classroom_plan.check_plan_file(plan_path)
                    if isinstance(res, tuple):
                        plan_ok, plan_errs = res
                    elif isinstance(res, list):
                        plan_ok = (len(res) == 0)
                        plan_errs = res
                    elif isinstance(res, bool):
                        plan_ok = res
                        if not plan_ok:
                            plan_errs = ["classroom-plan.md 检查未通过"]
                except Exception as e:
                    plan_ok = False
                    plan_errs = [f"调用 gen_classroom_plan.check_plan_file 失败: {e}"]
            else:
                try:
                    with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    lines = text.splitlines()
                    placeholders = []
                    for line_idx, line in enumerate(lines, start=1):
                        for m in re.finditer(r"_{2,}", line):
                            placeholders.append(f"第 {line_idx} 行 '{m.group(0)}'")
                    if placeholders:
                        plan_ok = False
                        detail = ", ".join(placeholders[:10])
                        if len(placeholders) > 10:
                            detail += f" ...等共 {len(placeholders)} 处"
                        plan_errs.append(f"classroom-plan.md 存在未填写的占位符: {detail}")
                    stripped = re.sub(r"\s+", "", text)
                    if len(stripped) < 200:
                        plan_ok = False
                        plan_errs.append(f"classroom-plan.md 实质内容过短（去空白后 {len(stripped)} < 200 字符），缺少实质内容")
                except Exception as e:
                    plan_ok = False
                    plan_errs.append(f"读取 classroom-plan.md 失败: {e}")

            if not plan_ok:
                for err in plan_errs:
                    problems.append(err)
                    artifact_problems.append(err)
                    _tag(err, "CLASSROOM_PLAN_INVALID", "classroom-plan.md")

        # 检查 student.pdf 页数上限显式化 (修复 F8)
        pdf_path = os.path.join(pdir, "student.pdf")
        if os.path.isfile(pdf_path):
            pages = count_pdf_pages(pdf_path)
            if pages is None:
                stats["student_pdf_pages"] = "SKIPPED"
                warnings.append("无法解析 student.pdf 页数 (SKIPPED)，跳过页数上限核验")
            else:
                stats["student_pdf_pages"] = pages
                stats["student_pages"] = pages
                if max_student_pages is None:
                    stats["student_pages_limit"] = "unlimited"
                    if pages > 6:
                        warnings.append(
                            f"self_study 模式不设学生版页面上限：当前 {pages} 页"
                            "（仅提示，不阻断交付）。"
                        )
                elif pages > max_student_pages:
                    msg = (
                        f"student.pdf 实际页数超标（当前 {pages} 页 > 允许上限 {max_student_pages} 页）。"
                        "请检查排版溢出或拆减切片题目，严禁使用密集字号硬塞。"
                    )
                    problems.append(msg)
                    artifact_problems.append(msg)
                    _tag(msg, "STUDENT_PDF_PAGE_OVERFLOW", "student.pdf")

    # 确定 category 分类（优先级: config > coverage > section > artifact > ok）
    if config_problems:
        category = "config"
    elif coverage_problems:
        category = "coverage"
    elif section_problems:
        category = "section"
    elif artifact_problems:
        category = "artifact"
    else:
        category = "ok"

    stats["category"] = category
    return BudgetResult(problems, warnings, stats, category=category,
                        estimates_ref=estimates_ref, issue_meta=issue_meta)


def main(argv=None):
    ap = argparse.ArgumentParser(description="教学预算、长链密度与翻转教案门禁 (check_budget.py)")
    ap.add_argument("workdir", nargs="?", default=".", help="工作区项目目录")
    ap.add_argument("--stage", choices=["planning", "delivery"], default="planning",
                    help="门禁执行阶段：planning(规划期验证) | delivery(交付期汇总)")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.workdir)
    manifest = evidence.collect_inputs(workdir)
    digest = evidence.content_digest(manifest)

    res = check_budget_project(workdir, stage=args.stage)
    problems, warnings, stats = res.problems, res.warnings, res.stats
    category = res.category
    estimates_ref = res.estimates_ref

    report = {
        "schema_version": 2,
        "tool": "check_budget.py",
        "tool_version": TOOL_VERSION,
        "category": category,
        "problems": problems,
        "warnings": warnings,
        "estimates_ref": estimates_ref,
        "scope": {"workdir": workdir, "stage": args.stage},
        "policy": {
            "execution_profile": stats.get("execution_profile", "legacy"),
            "pedagogy_policy": stats.get("pedagogy_policy", "strict"),
            "budget_policy": stats.get("budget_policy", "strict"),
            "require_classroom_plan": stats.get("require_classroom_plan", True),
            "sources": stats.get("policy_sources", {}),
        },
        "input_digest": digest,
        "pdf_sha256": None,
        "status": "pass" if len(problems) == 0 else "fail",
        "issues": [
            dict({"severity": "error", "message": p}, **res.issue_meta.get(p, {}))
            for p in problems
        ] + [
            {"severity": "warning", "message": w} for w in warnings
        ],
        "evidence": {"budget_stats": stats}
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        evidence.write_report(args.json, report)
        print(f"[check_budget] 结构化报告已写入 {args.json}")

    for w in warnings:
        print(f"[check_budget] [警告] {w}")

    if problems:
        print(f"[check_budget] 发现 {len(problems)} 个问题 (阶段: {args.stage}, 类别: {category}):")
        for p in problems:
            print(f"  - [FAIL] {p}")
        return 1

    budget_desc = (
        f"教学预算={stats.get('lesson_minutes_budget')} 分钟"
        if stats.get("lesson_minutes_budget") is not None
        else "自主学习模式 (无时长上限)"
    )
    print(
        f"[check_budget] 通过：教学预算与长链密度合规（阶段: {args.stage}，"
        f"类别: {category}，模式: {stats.get('delivery_profile', 'self_study')}，{budget_desc}，"
        f"core 任务={stats.get('core_task_count', 0)} 道，总时长={stats.get('grand_total', stats.get('core_minutes', 0))} 分钟）。"
    )
    return 0


# 向后兼容与便捷别名
check_budget = check_budget_project


if __name__ == "__main__":
    sys.exit(main())
