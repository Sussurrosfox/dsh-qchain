#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
estimate_minutes.py — 题链学案课堂耗时估算器核心

依据 Lead 冻结契约 references/contracts/estimate_schema.json，提供确定性课时估算：
1. 读取 coverage-map.md 解析各行节点、角色、模式与题目；
2. 读取各切片 TeX 文件，使用 task_struct 提取真实题目结构；
3. 读取 config.yaml 对话层与开篇/收官/缓冲常数配置；
4. 综合小问数、填空数、定理长链加成、对话轮次加成与教师覆盖，计算每题耗时与课堂总计；
5. 自动输出结构化报告 estimate.json。
"""

import argparse
import json
import os
import re
import sys

# 导入同目录下 task_struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import task_struct  # noqa: E402

# 默认系数配置（完全对齐 estimate_schema.json）
DEFAULT_COEFFICIENTS = {
    "base_reading": {"core": 1.0, "support": 2.0},
    "per_sub_item": 2.0,
    "per_fillin": 1.0,
    "proof_bonus": 2.0,
    "per_dialogue_turn": 0.5,
}

# 默认课堂时间常量（分钟）
DEFAULT_OPENING = 5.0
DEFAULT_CLOSING = 5.0
DEFAULT_BUFFER = 5.0


def load_config_file(project_dir):
    """尝试读取 project_dir 下的 config.yaml。"""
    cfg_paths = [
        os.path.join(project_dir, "config.yaml"),
        os.path.join(project_dir, "self", "config.yaml"),
    ]
    for p in cfg_paths:
        if os.path.isfile(p):
            try:
                import yaml

                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
    return {}


def is_dialogue_enabled_for_sec(section_name, cfg):
    """判断指定切片是否启用了对话层。"""
    if not cfg:
        return False
    raw_en = cfg.get("dialogue_enabled")
    if raw_en is None and isinstance(cfg.get("dialogue"), dict):
        raw_en = cfg["dialogue"].get("enabled")
    if raw_en is not None:
        if isinstance(raw_en, bool):
            return raw_en
        s_en = str(raw_en).strip().lower()
        if s_en in ("true", "all", "yes", "1"):
            return True
        if s_en in ("false", "no", "0", ""):
            return False
        sec_clean = (section_name or "").replace(".tex", "").strip()
        enabled_secs = {
            s.strip().replace(".tex", "") for s in s_en.split(",") if s.strip()
        }
        return sec_clean in enabled_secs

    # 若配置了 dialogue_max_section_turns > 0，默认启用对话
    if cfg.get("dialogue_max_section_turns", 0) > 0:
        return True

    return False


def parse_coverage_map(coverage_path):
    """解析 coverage-map.md 的 Markdown 表格，提取每行任务信息。"""
    if not os.path.isfile(coverage_path):
        return []

    with open(coverage_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    header_idx = -1
    col_names = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and (
            "原文节点" in s
            or "node" in s.lower()
            or "delivery_role" in s.lower()
            or "planned_minutes" in s.lower()
        ):
            header_idx = i
            col_names = [c.strip() for c in s.split("|")[1:-1]]
            break

    if header_idx == -1 or not col_names:
        return []

    col_map = {}
    for idx, c in enumerate(col_names):
        norm = c.lower().replace(" ", "").replace("_", "").replace("-", "")
        if "原文节点" in c or norm in ("nodeid", "node", "id"):
            col_map["node_id"] = idx
        elif "学案位置" in c or norm in ("section", "sec"):
            col_map["section"] = idx
        elif "学习目标" in c or norm in ("objective", "goal"):
            col_map["objective"] = idx
        elif "对应问题" in c or norm in ("problem", "qid", "题号"):
            col_map["problem"] = idx
        elif "deliveryrole" in norm or norm in ("role", "角色"):
            col_map["delivery_role"] = idx
        elif "practicemode" in norm or norm in ("mode", "模式", "练习模式"):
            col_map["practice_mode"] = idx
        elif "carrierqid" in norm or norm in ("carrier", "载体题号"):
            col_map["carrier_qid"] = idx
        elif "override" in norm or "覆盖时间" in c:
            col_map["override_minutes"] = idx
        elif (
            "plannedminutes" in norm
            or norm in ("minutes", "planned")
            or "预计" in c
            or "时间" in c
            or "预算" in c
        ):
            col_map["planned_minutes"] = idx

    rows = []
    for line in lines[header_idx + 1 :]:
        s = line.strip()
        if not s.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in s.split("|")[1:-1]]
        if not cells or all(
            c.replace("-", "").replace(":", "") == "" for c in cells
        ):
            continue
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

        # 忽略非题目任务行
        if node_id.lower().startswith("dialogue-hook"):
            continue

        raw_role = get_val("delivery_role", "core").strip().lower()
        if "core" in raw_role or "核心" in raw_role:
            delivery_role = "core"
        elif "support" in raw_role or "支撑" in raw_role:
            delivery_role = "support"
        elif "self" in raw_role or "自学" in raw_role:
            delivery_role = "self-study"
        elif "ref" in raw_role or "参考" in raw_role:
            delivery_role = "reference"
        else:
            delivery_role = raw_role if raw_role else "core"

        carrier_qid = get_val("carrier_qid")
        prob_val = get_val("problem")
        qid = carrier_qid or prob_val or node_id

        rows.append(
            {
                "node_id": node_id,
                "section": get_val("section"),
                "qid": qid,
                "carrier_qid": carrier_qid,
                "delivery_role": delivery_role,
                "practice_mode": get_val("practice_mode", "guided"),
                "raw_planned_minutes": get_val("planned_minutes"),
                "raw_override_minutes": get_val("override_minutes"),
            }
        )

    return rows


def parse_positive_number(val_str):
    """尝试从字符串中提取正数（正整数或浮点数）。若无或非数字返回 None。"""
    if val_str is None:
        return None
    s = str(val_str).strip()
    if not s:
        return None
    # 提取数字部分
    m = re.search(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:min|m|分钟)?\s*$", s, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1))
            if val > 0:
                return val
        except ValueError:
            return None
    return None


def get_section_tasks(project_dir, sec_name, cache=None):
    """从 project_dir 中解析指定切片的题目结构（支持缓存）。"""
    if not sec_name:
        return []
    if cache is not None and sec_name in cache:
        return cache[sec_name]

    candidates = [
        os.path.join(
            project_dir,
            sec_name if sec_name.endswith(".tex") else sec_name + ".tex",
        ),
        os.path.join(project_dir, sec_name),
        os.path.join(
            project_dir,
            "self",
            sec_name if sec_name.endswith(".tex") else sec_name + ".tex",
        ),
        os.path.join(project_dir, "self", sec_name),
    ]
    found_file = None
    for c in candidates:
        if os.path.isfile(c):
            found_file = c
            break

    if found_file:
        tasks = task_struct.parse_section_file(found_file)
    else:
        tasks = []
    if cache is not None:
        cache[sec_name] = tasks
    return tasks


def estimate_project(project_dir, cfg=None, json_path=None):
    """对指定工程执行课堂耗时确定性估算。

    返回 dict，符合 references/contracts/estimate_schema.json 规范。
    """
    project_dir = os.path.abspath(project_dir)

    # 1. 整合配置
    merged_cfg = load_config_file(project_dir)
    if cfg:
        merged_cfg.update(cfg)

    opening = float(
        merged_cfg.get("opening_minutes", merged_cfg.get("opening", DEFAULT_OPENING))
    )
    closing = float(
        merged_cfg.get("closing_minutes", merged_cfg.get("closing", DEFAULT_CLOSING))
    )
    buffer = float(
        merged_cfg.get("buffer_minutes", merged_cfg.get("buffer", DEFAULT_BUFFER))
    )

    # 2. 读取 coverage-map.md
    cov_path = os.path.join(project_dir, "coverage-map.md")
    if not os.path.isfile(cov_path):
        cov_path = os.path.join(project_dir, "self", "coverage-map.md")

    cov_rows = parse_coverage_map(cov_path)

    # 3. 扫描各 section 的 TeX 结构
    sec_tasks_cache = {}

    # 4. 逐题计算估算耗时
    tasks_output = []
    sec_matched_idx = {}
    fallback_used = []

    for row in cov_rows:
        node_id = row["node_id"]
        sec_name = row["section"]
        req_qid = row["qid"]
        role = row["delivery_role"]
        practice_mode = row["practice_mode"]

        sec_tasks = get_section_tasks(project_dir, sec_name, sec_tasks_cache)
        dlg_enabled = is_dialogue_enabled_for_sec(sec_name, merged_cfg)

        # 尝试匹配 TeX 中的真实结构
        matched_tex_task = None
        # 1. 优先按 qid 匹配
        if req_qid:
            for t in sec_tasks:
                if t.get("qid") and t["qid"] == req_qid:
                    matched_tex_task = t
                    break
        # 2. 其次按 carrier_qid 或 node_id 匹配
        if not matched_tex_task:
            for candidate_id in (row.get("carrier_qid"), node_id):
                if candidate_id:
                    for t in sec_tasks:
                        if t.get("qid") and t["qid"] == candidate_id:
                            matched_tex_task = t
                            break
                    if matched_tex_task:
                        break
        # 3. 顺序 fallback 匹配
        if not matched_tex_task and sec_tasks:
            curr_idx = sec_matched_idx.get(sec_name, 0)
            if curr_idx < len(sec_tasks):
                matched_tex_task = sec_tasks[curr_idx]
                sec_matched_idx[sec_name] = curr_idx + 1
                fallback_used.append(
                    {
                        "node_id": node_id,
                        "section": sec_name,
                        "expected_qid": req_qid,
                        "fallback_qid": matched_tex_task.get("qid"),
                        "fallback_index": curr_idx,
                    }
                )

        if matched_tex_task:
            sub_items = int(matched_tex_task.get("sub_items", 0))
            sub_fillins = int(matched_tex_task.get("sub_fillins", 0))
            is_thm = bool(matched_tex_task.get("is_theorem_proof", False))
            dlg_turns = int(matched_tex_task.get("dialogue_turns", 0))
            actual_qid = matched_tex_task.get("qid") or req_qid
        else:
            # 规则要求：若对应题目的结构解析不到，回退默认：sub_items=1, sub_fillins=1
            sub_items = 1
            sub_fillins = 1
            is_thm = False
            dlg_turns = 0
            actual_qid = req_qid

        # 检查是否为 reference 角色（无学生动作，est_minutes=0.0）
        if role == "reference":
            tasks_output.append(
                {
                    "node_id": node_id,
                    "qid": actual_qid,
                    "delivery_role": role,
                    "sub_items": sub_items,
                    "sub_fillins": sub_fillins,
                    "est_minutes": 0.0,
                    "override_minutes": None,
                    "source": "estimated",
                    "_practice_mode": practice_mode,
                }
            )
            continue

        # 算法估算逻辑
        # base_reading = 1.0 (若 delivery_role=="core") else 2.0 (若 "support") else 0.0
        if role == "core":
            base_reading = DEFAULT_COEFFICIENTS["base_reading"]["core"]
        elif role == "support":
            base_reading = DEFAULT_COEFFICIENTS["base_reading"]["support"]
        else:
            base_reading = 0.0

        proof_bonus = (
            DEFAULT_COEFFICIENTS["proof_bonus"]
            if (is_thm and sub_items >= 4)
            else 0.0
        )
        dlg_bonus = (
            dlg_turns * DEFAULT_COEFFICIENTS["per_dialogue_turn"]
            if dlg_enabled
            else 0.0
        )

        calc_minutes = (
            base_reading
            + DEFAULT_COEFFICIENTS["per_sub_item"] * sub_items
            + DEFAULT_COEFFICIENTS["per_fillin"] * sub_fillins
            + proof_bonus
            + dlg_bonus
        )

        # 检查是否存在 teacher_override 覆盖
        # 规则：
        # 1. override_minutes 列具有显式最高优先级；
        # 2. planned_minutes 列在非 "5" 占位符且 >= 结构估算 calc_minutes 时视为调高研讨时长的教师覆盖；
        #    若 < calc_minutes 则触发防机械压缩保护（不予覆盖，保持结构估算下限）。
        override_num = parse_positive_number(row["raw_override_minutes"])
        planned_num = parse_positive_number(row["raw_planned_minutes"])

        is_override = False
        override_val = None

        if override_num is not None:
            is_override = True
            override_val = override_num
        elif planned_num is not None:
            raw_s = str(row["raw_planned_minutes"]).strip().lower()
            if raw_s not in ("5", "5.0", "5min", "5 min", "5.0min", "5.0 min"):
                if planned_num >= calc_minutes:
                    is_override = True
                    override_val = planned_num

        if is_override:
            est_minutes = float(override_val)
            tasks_output.append(
                {
                    "node_id": node_id,
                    "qid": actual_qid,
                    "delivery_role": role,
                    "sub_items": sub_items,
                    "sub_fillins": sub_fillins,
                    "est_minutes": round(est_minutes, 2),
                    "override_minutes": round(override_val, 2),
                    "source": "teacher_override",
                    "_practice_mode": practice_mode,
                }
            )
        else:
            tasks_output.append(
                {
                    "node_id": node_id,
                    "qid": actual_qid,
                    "delivery_role": role,
                    "sub_items": sub_items,
                    "sub_fillins": sub_fillins,
                    "est_minutes": round(calc_minutes, 2),
                    "override_minutes": None,
                    "source": "estimated",
                    "_practice_mode": practice_mode,
                }
            )

    # 5. 计算 totals 聚合总计
    # core = sum(task["est_minutes"] for task in tasks if task["delivery_role"] == "core")
    core_total = sum(
        t["est_minutes"] for t in tasks_output if t["delivery_role"] == "core"
    )
    # support_inclass = sum(task["est_minutes"] for task in tasks if task["delivery_role"] == "support"
    # and ("课内" in task.get("practice_mode", "") or "inclass" in task.get("practice_mode", "").lower()))
    support_inclass_total = sum(
        t["est_minutes"]
        for t in tasks_output
        if t["delivery_role"] == "support"
        and (
            "课内" in str(t.get("_practice_mode", ""))
            or "inclass" in str(t.get("_practice_mode", "")).lower()
        )
    )
    grand_total = opening + closing + buffer + core_total + support_inclass_total

    totals = {
        "core": round(core_total, 2),
        "support_inclass": round(support_inclass_total, 2),
        "opening": round(opening, 2),
        "closing": round(closing, 2),
        "buffer": round(buffer, 2),
        "grand_total": round(grand_total, 2),
    }

    # 清理内部辅助字段
    clean_tasks = []
    for t in tasks_output:
        item = {
            "node_id": t["node_id"],
            "qid": t["qid"],
            "delivery_role": t["delivery_role"],
            "sub_items": t["sub_items"],
            "sub_fillins": t["sub_fillins"],
            "est_minutes": t["est_minutes"],
            "override_minutes": t["override_minutes"],
            "source": t["source"],
        }
        clean_tasks.append(item)

    report_data = {
        "schema_version": 1,
        "tool": "estimate_minutes",
        "coefficients": DEFAULT_COEFFICIENTS,
        "tasks": clean_tasks,
        "totals": totals,
        "_fallback_matches": fallback_used,
    }

    # 6. 自动写入 .pd/reports/estimate.json（如果 .pd/reports 目录存在）或指定的 json_path
    target_json_path = json_path
    if not target_json_path:
        pd_reports_dir = os.path.join(project_dir, ".pd", "reports")
        if os.path.isdir(pd_reports_dir):
            target_json_path = os.path.join(pd_reports_dir, "estimate.json")

    if target_json_path:
        try:
            os.makedirs(
                os.path.dirname(os.path.abspath(target_json_path)),
                exist_ok=True,
            )
            with open(target_json_path, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    return report_data


def main():
    parser = argparse.ArgumentParser(
        description="题链学案课堂耗时估算器 (estimate_minutes.py)"
    )
    parser.add_argument("project_dir", nargs="?", default=".", help="学案工程目录")
    parser.add_argument("--json", dest="output_json", help="指定输出 JSON 报告路径")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    res = estimate_project(project_dir)

    if args.output_json:
        out_dir = os.path.dirname(os.path.abspath(args.output_json))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        print(f"估算报告已输出至: {args.output_json}")
    else:
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
