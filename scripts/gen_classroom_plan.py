#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "3.0"
r"""
gen_classroom_plan.py — 由 coverage-map.md 与 config.yaml 自动装配/核验翻转课堂教案 (classroom-plan.md)

功能:
  1. 解析 coverage-map.md 中的原文节点覆盖与预算清单；
  2. 提取 core/support 任务、est_minutes、推荐讲法与易错点；
  3. 装配生成面向翻转课堂的高效教师讲解方案（classroom-plan.md），包含：
     - 课前预习体量与课堂讲解预算；
     - 核心题精讲/略讲优先级与时间分配表；
     - 学生 \selfcheck 卡点自评回收登记位；
     - 逐题易错点与推荐讲法速查表。
  4. 人手编辑保护机制（防覆盖）：头部嵌入 sha256 签名，未带 --force 时严禁覆盖手工修改文件；
  5. --check 严格只读门禁检查：验证教案存在性、占位符残留、篇幅及关键章节标记。

用法:
  生成/更新: python gen_classroom_plan.py [工作目录] [--force]
  门禁核验:   python gen_classroom_plan.py [工作目录] --check [--json 报告路径]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import gen_config  # noqa: E402

MARKER_PATTERN = re.compile(
    r"<!--\s*generated-by:\s*gen_classroom_plan(?:\s+v\d+)?\s+digest=([0-9a-fA-F]+)\s*-->\r?\n?"
)


def compute_digest(text: str) -> str:
    """对内容统一规范化换行后计算 SHA256 摘要。"""
    norm = text.replace("\r\n", "\n")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest().lower()


def parse_coverage_map(coverage_path):
    """解析 coverage-map.md 的 Markdown 表格，提取教学规划节点。"""
    if not os.path.isfile(coverage_path):
        return []

    with open(coverage_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    header_idx = -1
    col_names = []
    rows = []

    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and ("原文节点" in s or "delivery_role" in s or "planned_minutes" in s or "est_minutes" in s):
            header_idx = i
            raw_cols = [c.strip() for c in s.split("|")[1:-1]]
            col_names = raw_cols
            break

    if header_idx == -1 or not col_names:
        return []

    # 规范化列名映射
    col_map = {}
    for idx, c in enumerate(col_names):
        norm = c.lower().replace(" ", "").replace("_", "").replace("-", "")
        if "原文节点" in c or "node" in norm:
            col_map["node_id"] = idx
        elif "学案位置" in c or "section" in norm:
            col_map["section"] = idx
        elif "学习目标" in c or "objective" in norm or "goal" in norm:
            col_map["objective"] = idx
        elif "对应问题" in c or "材料" in c or "problem" in norm:
            col_map["problem"] = idx
        elif "deliveryrole" in norm or "role" in norm or "角色" in c:
            col_map["delivery_role"] = idx
        elif "practicemode" in norm or "mode" in norm or "模式" in c:
            col_map["practice_mode"] = idx
        elif "carrierqid" in norm or "carrier" in norm:
            col_map["carrier_qid"] = idx
        elif "estminutes" in norm or "plannedminutes" in norm or "minutes" in norm or "预计分钟" in c or "预算" in c or "用时" in c:
            col_map["planned_minutes"] = idx
        elif "推荐讲法" in c or "讲法" in c or "method" in norm:
            col_map["teach_method"] = idx
        elif "易错点" in c or "易错" in c or "pitfall" in norm:
            col_map["pitfall"] = idx

    for line in lines[header_idx + 1:]:
        s = line.strip()
        if not s.startswith("|"):
            if rows:  # 表格结束
                break
            continue
        cells = [c.strip() for c in s.split("|")[1:-1]]
        if not cells or all(c.replace("-", "").replace(":", "") == "" for c in cells):
            continue  # 分割线
        if len(cells) < len(col_names):
            cells.extend([""] * (len(col_names) - len(cells)))

        def get_val(key, default=""):
            idx = col_map.get(key)
            if idx is not None and idx < len(cells):
                return cells[idx]
            return default

        node_id = get_val("node_id")
        if not node_id or node_id.startswith("#"):
            continue

        raw_min = get_val("planned_minutes", "5")
        m_num = re.search(r"\d+", raw_min)
        minutes = int(m_num.group(0)) if m_num else 5

        row_data = {
            "node_id": node_id,
            "section": get_val("section"),
            "objective": get_val("objective"),
            "problem": get_val("problem"),
            "delivery_role": get_val("delivery_role", "core").lower(),
            "practice_mode": get_val("practice_mode", "guided"),
            "carrier_qid": get_val("carrier_qid", node_id),
            "planned_minutes": minutes,
            "teach_method": get_val("teach_method"),
            "pitfall": get_val("pitfall"),
        }
        rows.append(row_data)

    return rows


def generate_plan_markdown(pdir, rows, cfg):
    """装配翻转课堂教案 markdown 正文内容（不含头部 digest 注释）。"""
    title = cfg.get("main_title", "学案")
    profile = str(cfg.get("delivery_profile", "self_study")).lower()
    is_teaching = profile in ("teaching", "classroom")
    task_limit = int(cfg.get("core_task_limit", 12))

    core_rows = [r for r in rows if r["delivery_role"] == "core"]
    support_rows = [r for r in rows if r["delivery_role"] == "support"]
    self_study_rows = [r for r in rows if r["delivery_role"] in ("self-study", "reference")]

    # 汇总时长
    core_minutes = sum(r["planned_minutes"] for r in core_rows)
    support_minutes = sum(r["planned_minutes"] for r in support_rows)

    opening_min = int(cfg.get("opening_minutes", 5))
    closing_min = int(cfg.get("closing_minutes", 5))
    buffer_min = int(cfg.get("buffer_minutes", 5))
    support_inclass = support_minutes
    total_classroom_est = opening_min + closing_min + buffer_min + core_minutes + support_inclass

    if is_teaching:
        lesson_min = int(cfg.get("lesson_minutes", 90))
        over_budget = total_classroom_est > lesson_min
        status_badge = "⚠️ 课时耗时偏高（需拆课或精简）" if over_budget else "✅ 课时体量适中"
        budget_line = f"- **课时预算 (`lesson_minutes`)**：`{lesson_min}` 分钟"
        formula_line = (
            f"- **标准预算公式 (C1)**：`opening` ({opening_min}m) + `closing` ({closing_min}m) + "
            f"`buffer` ({buffer_min}m) + `Σcore` ({core_minutes}m) + `Σsupport_inclass` ({support_inclass}m) "
            f"= **{total_classroom_est}** 分钟 <= `lesson_minutes` ({lesson_min} 分钟)"
        )
        eval_line = f"- **总预算评估**：{'⚠️ 课堂总时长超出课时预算，请依据下方表格精讲核心长题并考虑删减次要题' if over_budget else '✅ 课时负荷处于安全阈值内，可在计划时间内顺利完成授课'}\n"
    else:
        lesson_min = None
        over_budget = False
        status_badge = "✅ 知识链自足（自学无时长上限）"
        budget_line = "- **时长上限**：自主学习模式不设时长上限（以认知自足与台阶完备为准）"
        formula_line = (
            f"- **标准预算公式 (C1)**：`opening` ({opening_min}m) + `closing` ({closing_min}m) + "
            f"`buffer` ({buffer_min}m) + `Σcore` ({core_minutes}m) + `Σsupport_inclass` ({support_inclass}m) "
            f"= **{total_classroom_est}** 分钟（自学参考；无上限）"
        )
        eval_line = f"- **总预算评估**：✅ 自学模式不设时长上限，学生可按自身认知节奏沉浸探究（当前 core 预计总耗时 **{core_minutes}** 分钟，包含 {len(core_rows)} 个核心节点）\n"

    md = []
    md.append(f"# 《{title}》翻转课堂教师讲解方案 (Classroom Plan)\n")
    md.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M')} | 交付模式: `{profile}` | 状态: {status_badge}\n")
    md.append("---\n")

    md.append("## 一、 课前预习体量与课堂讲解预算\n")
    md.append(f"- **学案主标题**：{title}")
    md.append(f"- **交付模式**：{'课堂教学模式 (teaching)' if is_teaching else '自主学习模式 (self_study)'}")
    md.append(f"- **本课核心结果**：完成全部 {len(core_rows)} 道核心题链推演与易错点清障，达成教学目标与认知闭环")
    md.append(budget_line)
    md.append(formula_line)
    md.append(f"- **Core 核心任务总耗时 (`Σcore`)**：**{core_minutes}** 分钟（共 {len(core_rows)} 个核心节点，上限 {task_limit} 道）")
    md.append(f"- **Support 辅助阅读耗时 (`Σsupport_inclass`)**：{support_minutes} 分钟（共 {len(support_rows)} 个阅读/引言节点）")
    md.append(f"- **课后拓展 (Self-study) 题数**：{len(self_study_rows)} 道（不占用课堂讲解预算）")
    md.append(eval_line)

    md.append("## 二、 核心题讲解策略与节点清单 (Core Tasks & Stumbling Points)\n")
    md.append("本表整合自 `coverage-map.md`。翻转课堂场景下，教师**严禁从头逐题宣读**，应重点回收学生在学案末尾 `\\selfcheck` 标记的真实卡点，按需实施靶向突破：\n")

    md.append("| 序号 | qid / 题位 | 学习目标与考查动作 | est_minutes (估算/教师可调) | 教学模式 | 翻转精讲/略讲建议 | 推荐讲法速查 | 学生典型易错点 | 课前自评卡点统计（教师课前填写） |")
    md.append("|---|---|---|---:|---|---|---|---|---|")

    for idx, r in enumerate(core_rows, 1):
        pm = r["planned_minutes"]
        is_long = pm >= 10
        advice = "🔥 **重点精讲**（大定理/长逻辑链）" if is_long else "⚡ **快速核对**（基础算例/概念）"
        qid = r["carrier_qid"] or r["node_id"]
        sec = r["section"]
        loc = f"`{qid}` ({sec})" if sec else f"`{qid}`"
        method = r["teach_method"] or "引导学生回顾微知识卡展开推演"
        pitfall = r["pitfall"] or "符号混淆或计算粗心"
        md.append(f"| {idx} | {loc} | {r['objective']} | {pm} min | {r['practice_mode']} | {advice} | {method} | {pitfall} | □ 全员通过 □ 卡点待核 |")

    md.append("\n---\n")

    md.append("## 三、 课堂讲解时间分配模型 (45 分钟标准微翻转课)\n")
    md.append("| 环节 | 建议用时 | 核心动作 | 对应题号 / 模块 | 实施要点 |")
    md.append("|---|---:|---|---|---|")
    md.append("| **1. 疑难聚焦与卡点摸排** | 5 分钟 | 回收学生学案末尾 `\\selfcheck`，统计卡点集中的题号 | 全章 \\selfcheck 汇总 | 识别共性卡点，确定今日必讲题目 |")
    md.append("| **2. 基础算例与概念快检** | 10 分钟 | 抽查概念夯实题与数值算例，验证基本手感 | 短题（≤6分钟） | 学生报答案，教师点拨易错点 |")
    md.append("| **3. 核心大定理长链攻坚** | 20 分钟 | 重点拆解长链推导的关键转折问（如 EFFT、Routh 等） | 长题（≥10分钟） | 邀请受阻学生板演断点，全班推导收敛 |")
    md.append("| **4. 变式迁移与总结收束** | 10 分钟 | 结合节末 knowledgebox，点拨方法论与思想闭环 | 节末结论框与拓展题 | 梳理整体代数/几何脉络，布置课后题 |")

    md.append("\n---\n")

    md.append("## 四、 逐题教学要点与避坑指南 (Teacher's Guide by Question)\n")
    for idx, r in enumerate(core_rows, 1):
        qid = r["carrier_qid"] or r["node_id"]
        sec = r["section"]
        md.append(f"### Q{idx}. `{qid}`：{r['objective']}")
        md.append(f"- **学案位置**：{sec} | **估算用时 (`est_minutes`)**：{r['planned_minutes']} 分钟 | **题型角色**：`{r['delivery_role']}` (`{r['practice_mode']}`)")
        md.append(f"- **任务描述**：{r['problem']}")
        md.append(f"- **推荐讲法**：{r['teach_method'] or '引导学生动笔验算，对照前置定义卡展开。'}")
        md.append(f"- **易错警示**：{r['pitfall'] or '关注计算符号与定义域边界。'}")
        md.append(f"- **讲解支架与对白承接**：若学生在草稿演算中受阻，指引其查看节末 `\\hint` 提示，或回看微知识卡定义。\n")

    if support_rows:
        md.append("## 五、 辅助阅读与背景引入节点\n")
        md.append("| 原文节点 | 位置 | 学习目标 | 对应材料 | est_minutes (估算/教师可调) | 课堂处理建议 |")
        md.append("|---|---|---|---|---:|---|")
        for r in support_rows:
            md.append(f"| {r['node_id']} | {r['section']} | {r['objective']} | {r['problem']} | {r['planned_minutes']} min | 课前自读，课上仅点题引入 |")
        md.append("")

    if self_study_rows:
        md.append("## 六、 课后自测与拓展练习清单 (Self-Study / Homework)\n")
        md.append("| 序号 | 题目 / 节点 | 目标 | 题型模式 | 来源/说明 |")
        md.append("|---|---|---|---|---|")
        for idx, r in enumerate(self_study_rows, 1):
            md.append(f"| {idx} | `{r['carrier_qid']}` | {r['objective']} | {r['practice_mode']} | 供学有余力学生自主选做 |")
        md.append("")

    return "\n".join(md)


def generate(pdir, force=False):
    """由项目目录的 coverage-map.md 与 config.yaml 生成/更新 classroom-plan.md。"""
    pdir = os.path.abspath(pdir)
    cov_path = os.path.join(pdir, "coverage-map.md")
    if not os.path.isfile(cov_path):
        print(f"[gen_classroom_plan] 未找到 {cov_path}，无法生成课堂计划")
        return 2

    rows = parse_coverage_map(cov_path)
    if not rows:
        print(f"[gen_classroom_plan] {cov_path} 中未解析到有效的规划节点表格")
        return 1

    cfg_path = os.path.join(pdir, "config.yaml")
    cfg = {}
    if os.path.isfile(cfg_path):
        try:
            cfg = gen_config.load_config(cfg_path)
        except Exception:
            pass

    body = generate_plan_markdown(pdir, rows, cfg)
    body_norm = body.replace("\r\n", "\n")
    digest = compute_digest(body_norm)
    marker = f"<!-- generated-by: gen_classroom_plan v3 digest={digest} -->\n"
    full_content = marker + body_norm
    out_path = os.path.join(pdir, "classroom-plan.md")

    # 人手编辑保护机制（防覆盖）：
    if os.path.isfile(out_path):
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                existing_content = f.read()
        except Exception:
            existing_content = ""

        m = MARKER_PATTERN.search(existing_content)
        if not m:
            is_human_edited = True
        else:
            recorded_digest = m.group(1).lower()
            existing_body = existing_content[:m.start()] + existing_content[m.end():]
            actual_digest = compute_digest(existing_body)
            is_human_edited = (recorded_digest != actual_digest)

        if is_human_edited and not force:
            print("[gen_classroom_plan] 检测到 classroom-plan.md 包含手工修改内容，为保护教案不被覆盖已跳过写入。如需覆盖请使用 --force。")
            return 0

        # 如果未被手工修改，且内容无实质变化（忽略生成时间），提示无变化
        if not is_human_edited and not force:
            norm_old = re.sub(r"生成时间: [^\n]+", "", existing_content)
            norm_new = re.sub(r"生成时间: [^\n]+", "", full_content)
            if norm_old == norm_new:
                print(f"[gen_classroom_plan] {out_path} 内容无变化")
                return 0

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(full_content)

    print(f"[gen_classroom_plan] 已成功装配翻转课堂计划卡: {out_path}（共 {len(rows)} 个节点）")
    return 0


def check_classroom_plan(project, json_path=None):
    """严格只读门禁检查模式（绝不创建、写入或覆盖任何教案文件）。"""
    pdir = os.path.abspath(project)
    plan_path = os.path.join(pdir, "classroom-plan.md")
    errors = []
    placeholders_found = []
    missing_markers = []
    stripped_len = 0

    if not os.path.isfile(plan_path):
        err = f"未找到教案文件: {plan_path}"
        print(f"[gen_classroom_plan --check] 错误: {err}", file=sys.stderr)
        errors.append(err)
        if json_path:
            write_json_report(json_path, {
                "ok": False,
                "file": plan_path,
                "errors": errors,
            })
        return 1

    try:
        with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        err = f"读取教案文件失败: {e}"
        print(f"[gen_classroom_plan --check] 错误: {err}", file=sys.stderr)
        errors.append(err)
        if json_path:
            write_json_report(json_path, {
                "ok": False,
                "file": plan_path,
                "errors": errors,
            })
        return 1

    # 1. 检查是否残留未填写的占位符下划线: 正则 r"_{2,}"
    lines = text.splitlines()
    for line_idx, line in enumerate(lines, start=1):
        for m in re.finditer(r"_{2,}", line):
            placeholders_found.append(f"第 {line_idx} 行 '{m.group(0)}'")

    if placeholders_found:
        detail = ", ".join(placeholders_found[:10])
        if len(placeholders_found) > 10:
            detail += f" ...等共 {len(placeholders_found)} 处"
        err = f"教案存在未替换的占位符: {detail}"
        print(f"[gen_classroom_plan --check] 错误: {err}", file=sys.stderr)
        errors.append(err)

    # 2. 检查字符长度（去空白后）是否 >= 200
    stripped_text = re.sub(r"\s+", "", text)
    stripped_len = len(stripped_text)
    if stripped_len < 200:
        err = f"教案实质内容过短（去空白后 {stripped_len} 字符 < 200 字符），缺少实质内容"
        print(f"[gen_classroom_plan --check] 错误: {err}", file=sys.stderr)
        errors.append(err)

    # 3. 检查必需的关键章节标记
    REQUIRED_MARKERS = [
        ("本课核心结果", ["本课核心结果", "核心结果"]),
        ("时间分配", ["时间分配"]),
        ("节点清单", ["节点清单", "核心题清单"]),
    ]
    for label, candidates in REQUIRED_MARKERS:
        if not any(cand in text for cand in candidates):
            missing_markers.append(label)

    if missing_markers:
        err = f"教案缺失必需的关键章节标记: {', '.join(missing_markers)}"
        print(f"[gen_classroom_plan --check] 错误: {err}", file=sys.stderr)
        errors.append(err)

    ok = (len(errors) == 0)

    if json_path:
        write_json_report(json_path, {
            "ok": ok,
            "file": plan_path,
            "char_count": stripped_len,
            "placeholders_found": placeholders_found,
            "missing_markers": missing_markers,
            "errors": errors,
        })

    if not ok:
        return 1

    print(f"[gen_classroom_plan --check] ✅ 教案门禁校验通过: {plan_path} (去空白字符数: {stripped_len})")
    return 0


def write_json_report(json_path, data):
    """将校验结果结构化输出到指定 JSON 文件。"""
    try:
        jdir = os.path.dirname(os.path.abspath(json_path))
        if jdir and not os.path.exists(jdir):
            os.makedirs(jdir, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[gen_classroom_plan --check] 写入 JSON 报告失败: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="自动装配与核验翻转课堂教案 (gen_classroom_plan.py)")
    ap.add_argument("project", nargs="?", default=".", help="工作区项目目录（包含 coverage-map.md / classroom-plan.md）")
    ap.add_argument("--check", action="store_true", help="严格只读门禁检查模式（不创建、写入或覆盖任何教案文件）")
    ap.add_argument("--json", metavar="PATH", help="结构化结果输出文件路径（配合 --check 使用）")
    ap.add_argument("--force", action="store_true", help="强制覆盖已手工修改的 classroom-plan.md")
    args = ap.parse_args()

    if args.check:
        sys.exit(check_classroom_plan(args.project, json_path=args.json))
    else:
        sys.exit(generate(args.project, force=args.force))


# 向后兼容与便捷别名
check_plan = check_classroom_plan


if __name__ == "__main__":
    main()
