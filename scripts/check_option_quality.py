#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""
check_option_quality.py — 选择题选项质量门禁（F2 语义与可计算核验）

检查项：
  Q1. 选项表面归一化去重（超越纯文本 norm_opt）：
      - 递归剥离 $...$、\text{}、\mathrm{}、\mathbf{}、\mathit{} 等格式包裹
      - 将 \frac{a}{b} / \dfrac{a}{b} 与 a/b 规范化为同形
      - 将 \quotgrp{A}{B} / \quotring{A}{B} 规范化为 A/B
      - 将 -(-x) 与 x、--x 与 x 规范化为同形
      - 归一化后存在重复项 → 硬阻断 (problems)
  Q2. 纯数值选项可计算验证（基于 fractions.Fraction 精确有理数）：
      - 支持整数、小数、分数（\frac{a}{b}、a/b）、可精确开方的根式（如 \sqrt{4}）
      - 检查选项中是否存在数值等价项（如 2 与 \frac{4}{2}、0.5 与 \frac{1}{2}）→ 硬阻断 (problems)
      - 当全部选项均为纯数值时，核验参考答案是否可解析为数值，且在选项中唯一对应 → 硬阻断 (problems)
  Q3. 选项多样性检查：
      - 所有选项均为单字母/单符号（如 A/B/C/D）→ 告警 (warnings)
      - 非纯数值题中，选项剥离公式后非数学文本完全相同 → 告警 (warnings)

用法:
  python check_option_quality.py [工作目录或单切片] [--json PATH]
退出码: 0=通过, 1=发现问题, 2=缺少依赖或输入。
"""
import argparse
from fractions import Fraction
import math
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import check_numbering  # noqa: E402

WRAPPER_MACROS = (
    "text", "mathrm", "mathbf", "mathit", "textbf", "textit",
    "textnormal", "operatorname", "symbf", "bm", "rm", "sf",
    "tt", "bf", "it",
)
WRAPPER_RE = re.compile(r"\\(" + "|".join(WRAPPER_MACROS) + r")\s*\{")
FRAC_RE = re.compile(r"\\(?:frac|dfrac|tfrac)\s*\{")


def _find_matching_brace(s, start_idx):
    """从 start_idx（即左花括号 '{' 的下一个位置）向后寻找匹配的 '}'。

    返回匹配的闭花括号下标；若未闭合返回 -1。
    """
    depth = 1
    i = start_idx
    n = len(s)
    while i < n and depth > 0:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i - 1 if depth == 0 else -1


def unwrap_latex_wrappers(s):
    """递归剥离 \\text{}、\\mathrm{} 等包裹宏。"""
    while True:
        m = WRAPPER_RE.search(s)
        if not m:
            break
        close_idx = _find_matching_brace(s, m.end())
        if close_idx < 0:
            break
        inner = s[m.end():close_idx]
        s = s[:m.start()] + inner + s[close_idx + 1:]
    return s


def normalize_latex_surface(s):
    r"""LaTeX 表面归一化（去包裹、展开宏、同形符号映射、去空白）。"""
    if not s:
        return ""
    s = s.strip()

    # 1. 剥离外层数学定界符 $...$ 或 \(...\)
    while True:
        if s.startswith("$") and s.endswith("$") and len(s) >= 2:
            s = s[1:-1].strip()
            continue
        if s.startswith(r"\(") and s.endswith(r"\)") and len(s) >= 4:
            s = s[2:-2].strip()
            continue
        break

    # 2. 特殊结构展开：\quotgrp{A}{B} / \quotring{A}{B} -> A/B
    s = re.sub(r"\\quot(?:grp|ring)\{([^}]+)\}\{([^}]+)\}", r"\1/\2", s)

    # 3. 递归剥离包裹宏（\text, \mathrm, \mathbf...）
    s = unwrap_latex_wrappers(s)

    # 4. 移除尺寸与排版控制标记（\left, \right, \displaystyle...）
    s = re.sub(
        r"\\(?:left|right|displaystyle|textstyle|scriptstyle|limits|nolimits)\b",
        "", s)
    # 移除间距与转义字符
    s = re.sub(r"\\[,;! ]|\\quad\b|\\qquad\b|~", "", s)
    s = s.replace(r"\/", "/")

    # 5. 常见等价数学符号归一化
    s = re.sub(r"\\le\b", r"\\leq", s)
    s = re.sub(r"\\ge\b", r"\\geq", s)
    s = re.sub(r"\\ne\b", r"\\neq", s)

    # 6. 分数宏转为 A/B 结构：\frac{A}{B} -> A/B 或 (A)/(B)
    while True:
        m = FRAC_RE.search(s)
        if not m:
            break
        start1 = m.end()
        end1 = _find_matching_brace(s, start1)
        if end1 < 0:
            break
        num = s[start1:end1].strip()

        # 查找分母的花括号
        j = end1 + 1
        while j < len(s) and s[j] in " \t\n":
            j += 1
        if j < len(s) and s[j] == "{":
            start2 = j + 1
            end2 = _find_matching_brace(s, start2)
            if end2 >= 0:
                den = s[start2:end2].strip()
                # 简单原子不用括号包裹，复合表达式加括号
                norm_num = num
                if not re.fullmatch(r"^[a-zA-Z0-9]+$", norm_num) and not (
                        norm_num.startswith("(") and norm_num.endswith(")")):
                    norm_num = f"({norm_num})"
                norm_den = den
                if not re.fullmatch(r"^[a-zA-Z0-9]+$", norm_den) and not (
                        norm_den.startswith("(") and norm_den.endswith(")")):
                    norm_den = f"({norm_den})"
                s = s[:m.start()] + f"{norm_num}/{norm_den}" + s[end2 + 1:]
                continue
        break

    # 7. 剥离外层冗余花括号
    while s.startswith("{") and s.endswith("}"):
        if _find_matching_brace(s, 1) == len(s) - 1:
            s = s[1:-1].strip()
        else:
            break

    # 8. 符号与双重负号化简：-(-x) -> x, --x -> x
    s = re.sub(r"-\s*\(\s*-\s*([a-zA-Z0-9]+)\s*\)", r"\1", s)
    s = re.sub(r"--([a-zA-Z0-9]+)", r"\1", s)

    # 9. 剥离全部空白符
    s = re.sub(r"\s+", "", s)
    return s


def clean_for_parse(s):
    """预清洗 LaTeX 字符串以便进行纯数值解析。"""
    if not s:
        return ""
    s = s.strip()
    while True:
        if s.startswith("$") and s.endswith("$") and len(s) >= 2:
            s = s[1:-1].strip()
            continue
        if s.startswith(r"\(") and s.endswith(r"\)") and len(s) >= 4:
            s = s[2:-2].strip()
            continue
        break
    s = unwrap_latex_wrappers(s)
    s = re.sub(r"\\(?:left|right|displaystyle|textstyle|scriptstyle)\b", "", s)
    s = re.sub(r"\\[,;! ]|\\quad\b|\\qquad\b|~", "", s)
    s = s.strip()
    while s.startswith("{") and s.endswith("}"):
        if _find_matching_brace(s, 1) == len(s) - 1:
            s = s[1:-1].strip()
        else:
            break
    return s


def parse_fraction_value(s):
    r"""精确解析纯数值表达式为 fractions.Fraction。

    支持：
      - 整数: 2, -3, +5
      - 小数: 0.5, -1.25, .25, 2.
      - 分数宏: \frac{4}{2}, \dfrac{1}{2}, -\frac{3}{5}
      - 文本斜杠分数: 3/4, -1/2, {1/2}
      - 根式精确有理数: \sqrt{4} -> 2, \sqrt{9/4} -> 3/2
      - 括号与一元符号: -(3), (-1/2), -(-3)
    无法解析为精确有理数（如含变量 x、无理数 \sqrt{2}、文本）时返回 None。
    """
    s = clean_for_parse(s)
    if not s:
        return None

    # 处理外层平衡括号
    while s.startswith("(") and s.endswith(")"):
        if _find_matching_paren(s, 1) == len(s) - 1:
            s = clean_for_parse(s[1:-1])
        else:
            break

    # 处理一元正负号
    sign = 1
    while s.startswith("+") or s.startswith("-"):
        if s.startswith("-"):
            sign = -sign
        s = clean_for_parse(s[1:])
        while s.startswith("(") and s.endswith(")"):
            if _find_matching_paren(s, 1) == len(s) - 1:
                s = clean_for_parse(s[1:-1])
            else:
                break

    if not s:
        return None

    # 1. 匹配 \frac / \dfrac / \tfrac
    frac_match = FRAC_RE.match(s)
    if frac_match:
        start1 = frac_match.end()
        end1 = _find_matching_brace(s, start1)
        if end1 >= 0:
            num_str = s[start1:end1]
            j = end1 + 1
            while j < len(s) and s[j] in " \t\n":
                j += 1
            if j < len(s) and s[j] == "{":
                start2 = j + 1
                end2 = _find_matching_brace(s, start2)
                if end2 == len(s) - 1:
                    den_str = s[start2:end2]
                    num_val = parse_fraction_value(num_str)
                    den_val = parse_fraction_value(den_str)
                    if num_val is not None and den_val is not None and den_val != 0:
                        return sign * (num_val / den_val)

    # 2. 匹配 \sqrt{...} 精确有理数
    sqrt_match = re.match(r"^\\sqrt\s*\{", s)
    if sqrt_match:
        start = sqrt_match.end()
        end = _find_matching_brace(s, start)
        if end == len(s) - 1:
            inner_str = s[start:end]
            inner_val = parse_fraction_value(inner_str)
            if inner_val is not None and inner_val >= 0:
                p = inner_val.numerator
                q = inner_val.denominator
                sq_p = math.isqrt(p)
                sq_q = math.isqrt(q)
                if sq_p * sq_p == p and sq_q * sq_q == q and sq_q != 0:
                    return sign * Fraction(sq_p, sq_q)

    # 3. 匹配行内斜杠分数 a / b
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 2:
            left_val = parse_fraction_value(parts[0])
            right_val = parse_fraction_value(parts[1])
            if left_val is not None and right_val is not None and right_val != 0:
                return sign * (left_val / right_val)

    # 4. 匹配纯小数与整数
    if s.endswith(".") and re.fullmatch(r"^[+-]?\d+\.$", s):
        s = s[:-1]
    if re.fullmatch(r"^\d+(?:\.\d+)?$", s) or re.fullmatch(r"^\.\d+$", s):
        try:
            return sign * Fraction(s)
        except (ValueError, ZeroDivisionError):
            return None

    return None


def _find_matching_paren(s, start_idx):
    """从 start_idx（即 '(' 的下一个位置）寻找匹配的 ')'。"""
    depth = 1
    i = start_idx
    n = len(s)
    while i < n and depth > 0:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i - 1 if depth == 0 else -1


def is_pure_numeric(s):
    """判断字符串是否为可精确解析的纯数值。"""
    return parse_fraction_value(s) is not None


def check_fillin_options_quality(name, lineno, identity, answer, width,
                                 options_raw, problems, warnings):
    r"""单个 \fillin 选择题选项组的质量核验。"""
    if options_raw is None:
        return

    # 切分选项：支持 / 分隔（及逗号容错）
    if "/" in options_raw:
        opts = check_numbering.split_options(options_raw, "/")
    elif "," in options_raw:
        opts = check_numbering.split_options(options_raw, ",")
    else:
        opts = [options_raw]

    opts = [o.strip() for o in opts if o.strip()]
    if len(opts) < 2:
        return

    ans = (answer or "").strip()

    # ---- Q1: LaTeX 表面归一化去重 ----
    norm_opts = [normalize_latex_surface(o) for o in opts]
    seen_norms = {}
    q1_dups = set()
    for idx, (opt, norm) in enumerate(zip(opts, norm_opts)):
        if not norm:
            continue
        if norm in seen_norms:
            prev_idx, prev_opt = seen_norms[norm]
            q1_dups.add(norm)
            problems.append(
                "%s:%d: 槽位 %s 的选项表面归一化后存在重复（第 %d 项 '%s' 与第 %d 项 '%s' 均等价于 '%s'）"
                % (name, lineno, identity, prev_idx + 1, prev_opt, idx + 1, opt, norm)
            )
        else:
            seen_norms[norm] = (idx, opt)

    # ---- Q2: 纯数值选项精确计算与唯一性核验 ----
    num_vals = [parse_fraction_value(o) for o in opts]
    all_numeric = all(v is not None for v in num_vals)

    # 检查数值等价项
    val_seen = {}
    for idx, (opt, val) in enumerate(zip(opts, num_vals)):
        if val is not None:
            if val in val_seen:
                prev_idx, prev_opt = val_seen[val]
                # 若 Q1 已因表面完全归一化相同报错，此处不重复报数值相同，避免双重惩罚
                if norm_opts[prev_idx] != norm_opts[idx]:
                    problems.append(
                        "%s:%d: 槽位 %s 的选项存在数值等价项（第 %d 项 '%s' 与第 %d 项 '%s' 数值均为 %s）"
                        % (name, lineno, identity, prev_idx + 1, prev_opt, idx + 1, opt, val)
                    )
            else:
                val_seen[val] = (idx, opt)

    # 当选项均为纯数值时，核验参考答案的对应关系
    if all_numeric:
        if ans:
            ans_val = parse_fraction_value(ans)
            if ans_val is None:
                problems.append(
                    "%s:%d: 槽位 %s 的全部选项均为纯数值，但参考答案 '%s' 无法解析为数值"
                    % (name, lineno, identity, ans)
                )
            else:
                matches = [i for i, v in enumerate(num_vals) if v == ans_val]
                if len(matches) == 0:
                    ans_norm = normalize_latex_surface(ans)
                    if ans_norm not in seen_norms:
                        problems.append(
                            "%s:%d: 槽位 %s 的参考答案数值 %s 不在选项中（选项数值: %s）"
                            % (name, lineno, identity, ans_val,
                               ", ".join(str(v) for v in num_vals))
                        )
                elif len(matches) > 1:
                    dup_items = [opts[i] for i in matches]
                    problems.append(
                        "%s:%d: 槽位 %s 的参考答案数值 %s 在选项中匹配到 %d 个等价项（%s），答案无法唯一确定"
                        % (name, lineno, identity, ans_val, len(matches), ", ".join(dup_items))
                    )

    # ---- Q3: 选项多样性检查（告警级别）----
    # 1. 全部选项均为单个字母/符号
    is_all_single = all(len(re.sub(r"[\s${}\\]", "", o)) <= 1 for o in opts)
    if is_all_single:
        warnings.append(
            "%s:%d: 槽位 %s 的所有选项均为单字母/单符号（%s），缺乏语境辨识度与台阶深度"
            % (name, lineno, identity, "/".join(opts))
        )

    # 2. 剥离数学公式后文字完全相同（非纯数值题）
    if not all_numeric:
        non_math_texts = [
            re.sub(r"\s+", "", re.sub(r"\$[^$]*\$", "", o)) for o in opts
        ]
        if all(len(t) > 0 for t in non_math_texts) and len(set(non_math_texts)) == 1:
            warnings.append(
                "%s:%d: 槽位 %s 的选项剥离公式后文字完全相同（'%s'），建议丰富题干表述或差异化选项设计"
                % (name, lineno, identity, non_math_texts[0])
            )


def check_slice_option_quality(name, text, problems, warnings):
    r"""单个切片内所有 \fillin 选项的质量核验。"""
    # 逐行去除注释（保留换行符，使行号与原文严格一致）
    clean_lines = [check_numbering.strip_comment(line) for line in text.splitlines()]
    clean_text = "\n".join(clean_lines)

    fillins = check_numbering.scan_fillins(clean_text)
    for idx, (pos, ans, width, options_raw) in enumerate(fillins):
        if options_raw is None:
            continue
        lineno = text.count("\n", 0, pos) + 1
        sec_stem = os.path.splitext(name)[0]
        identity = "%s#%d" % (sec_stem, idx + 1)
        check_fillin_options_quality(
            name, lineno, identity, ans, width, options_raw, problems, warnings
        )


def scan_project(workdir):
    """扫描项目各切片选择题质量，统一输出 (problems, warnings)。"""
    workdir = os.path.abspath(workdir)
    problems = []
    warnings = []

    if os.path.isfile(workdir):
        files = [os.path.basename(workdir)]
        base_dir = os.path.dirname(workdir) or "."
    elif os.path.isdir(workdir):
        base_dir = workdir
        inputs = check_numbering.sec_inputs(base_dir)
        if inputs is None:
            inputs = sorted(
                (f for f in os.listdir(base_dir) if re.fullmatch(r"sec.*\.tex", f)),
                key=check_numbering.sec_key,
            )
        files = [f for f in inputs if os.path.exists(os.path.join(base_dir, f))]
    else:
        return [f"项目目录不存在: {workdir}"], []

    for f in files:
        fpath = os.path.join(base_dir, f)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fp:
                txt = fp.read()
        except OSError as e:
            problems.append("%s: 无法读取切片: %s" % (f, e))
            continue
        check_slice_option_quality(f, txt, problems, warnings)

    return problems, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="选择题选项质量门禁 (check_option_quality.py)"
    )
    ap.add_argument("workdir", nargs="?", default=".",
                    help="工程目录或切片文件路径（缺省当前目录）")
    ap.add_argument("--json", metavar="PATH",
                    help="写入统一 evidence 结构化报告")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.workdir)
    if not os.path.exists(workdir):
        print("[check_option_quality] 路径不存在: %s" % workdir)
        return 2

    problems, warnings = scan_project(workdir)

    base_dir = os.path.dirname(workdir) if os.path.isfile(workdir) else workdir
    manifest = evidence.collect_inputs(base_dir) if os.path.isdir(base_dir) else None
    digest = evidence.content_digest(manifest) if manifest else None

    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "check_option_quality.py",
        "tool_version": TOOL_VERSION,
        "scope": {"workdir": workdir},
        "input_digest": digest,
        "pdf_sha256": None,
        "status": "pass" if len(problems) == 0 else "fail",
        "issues": (
            [{"severity": "error", "message": p} for p in problems]
            + [{"severity": "warning", "message": w} for w in warnings]
        ),
        "evidence": {
            "problems_count": len(problems),
            "warnings_count": len(warnings),
        },
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        evidence.write_report(args.json, report)
        print("[check_option_quality] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_option_quality] [警告] %s" % w)
    for p in problems:
        print("[check_option_quality] [错误] %s" % p)

    if problems:
        print("[check_option_quality] 发现 %d 处选项质量门禁问题 (FAIL)" % len(problems))
        return 1

    print("[check_option_quality] 通过：选择题选项质量合规（零重复、数值等价核验通过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
