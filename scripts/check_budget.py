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

PLACEHOLDER_WORDS = {"", "-", "--", "---", "无", "待定", "暂无", "略", "none", "null"}


class BudgetResult(tuple):
    """兼顾元组解包 (problems, warnings, stats) 与属性访问/分类访问的门禁结果类。"""
    def __new__(cls, problems, warnings, stats, category="ok", estimates_ref=None):
        return super().__new__(cls, (problems, warnings, stats))

    def __init__(self, problems, warnings, stats, category="ok", estimates_ref=None):
        self.problems = problems
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
            "tool": "check_budget",
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

            stats["core_task_count"] = len(core_rows)
            stats["core_minutes"] = core_minutes
            stats["support_minutes"] = support_minutes
            stats["grand_total"] = grand_total
            stats["lesson_minutes_budget"] = lesson_minutes
            stats["delivery_profile"] = profile
            stats["estimates_ref"] = estimates_ref

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
            for r in core_rows + support_rows:
                qid = r.get("carrier_qid") or r.get("node_id")
                method = (r.get("teach_method") or "").strip().lower()
                pitfall = (r.get("pitfall") or "").strip().lower()

                if method in PLACEHOLDER_WORDS:
                    msg = f"{r.get('node_id')} ({qid}): 缺失“推荐讲法”（翻转课堂教师讲解必需支撑，严禁留空或填占位符）"
                    problems.append(msg)
                    coverage_problems.append(msg)
                if pitfall in PLACEHOLDER_WORDS:
                    msg = f"{r.get('node_id')} ({qid}): 缺失“易错点”（翻转课堂靶向突破必需支撑，严禁留空或填占位符）"
                    problems.append(msg)
                    coverage_problems.append(msg)

            # 5. 课时总时长上限 (N2 总量上限：硬约束，取消 20% 软顶豁免，修复 F5)
            if is_teaching and lesson_minutes is not None:
                if grand_total > lesson_minutes:
                    diff = round(grand_total - lesson_minutes, 2)
                    msg = (
                        f"课堂教学耗时超标（总计 {grand_total} 分钟 > 教学预算 {lesson_minutes} 分钟，"
                        f"超额 {diff} 分钟；其中 core={core_minutes}m, support_inclass={support_minutes}m）。"
                        f"必须在 coverage-map 中删减题量、拆分切片或将次要变式转入拓展题库，严禁通过压缩单题小问数来凑预算！"
                    )
                    problems.append(msg)
                    coverage_problems.append(msg)

            # 6. 核心题量上限
            if len(core_rows) > core_task_limit:
                msg = (
                    f"core 核心任务数量超标（当前 {len(core_rows)} 道 > 上限 {core_task_limit} 道）。"
                    "认知负荷过大，建议将辅助练习题转入 self-study 角色或拆课。"
                )
                problems.append(msg)
                coverage_problems.append(msg)

    # 7. Delivery 阶段扩展检查 (修复 F6, F7, F8)
    if stage == "delivery":
        # 检查 classroom-plan.md 是否已生成且填充实质内容
        plan_path = os.path.join(pdir, "classroom-plan.md")
        if not os.path.isfile(plan_path):
            msg = "缺失 classroom-plan.md 教师讲解方案，请先运行 gen_classroom_plan.py 自动生成"
            problems.append(msg)
            artifact_problems.append(msg)
        else:
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
                if pages > max_student_pages:
                    msg = (
                        f"student.pdf 实际页数超标（当前 {pages} 页 > 允许上限 {max_student_pages} 页）。"
                        "请检查排版溢出或拆减切片题目，严禁使用密集字号硬塞。"
                    )
                    problems.append(msg)
                    artifact_problems.append(msg)

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
    return BudgetResult(problems, warnings, stats, category=category, estimates_ref=estimates_ref)


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
        "tool": "check_budget",
        "tool_version": TOOL_VERSION,
        "category": category,
        "problems": problems,
        "warnings": warnings,
        "estimates_ref": estimates_ref,
        "scope": {"workdir": workdir, "stage": args.stage},
        "input_digest": digest,
        "pdf_sha256": None,
        "status": "pass" if len(problems) == 0 else "fail",
        "issues": [
            {"severity": "error", "message": p} for p in problems
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
