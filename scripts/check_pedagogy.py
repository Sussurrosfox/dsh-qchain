#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.2"
r"""
check_pedagogy.py — 题链教学语义、认知台阶与知识自足性门禁

检查项:
  1. 强制知识框检查 (Hard Gate):
     - 每个切片必须至少包含 1 个 \begin{knowledgebox}[本节结论]（节末结论框）
     - 每个切片必须至少包含 1 个 \begin{microknowledge}[核心定义与记号]（前置知识卡）
  2. 防剧透检查 (Anti-spoiler Check):
     - 填空项 \fillin[答案] 的题干不得在填空前紧邻语境中直接泄漏目标答案
  3. 认知台阶检查 (Cognitive Stride Check):
     - 涉及核心定理、引理或大同构的题目不得直接以单步无台阶填空终结，必须提供至少包含分步小问 (a), (b), (c) 的阶梯脚手架
  4. 算例先行与直观探究 (Example-first Exploration):
     - 检查高度抽象切片是否包含具体数值或特例演练
  5. 伪自学填空与旁观者题干 (Tautological Blank & Bystander Stem):
     - 常量答案填空占比超限、宣读定理后挖空填常量、题干/小问包办中间推导均硬阻断
     - 教师专属区（solution/teacherNote）内的 \fillin 不计入学生可见统计

用法: python check_pedagogy.py [工作目录] [--json PATH]
退出码: 0=通过, 1=发现问题, 2=缺少输入。
"""
import argparse
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import check_numbering  # noqa: E402

RE_KNOWLEDGEBOX = re.compile(r"\\begin\{knowledgebox\}(?:\[([^\]]*)\])?")
RE_MICROKNOWLEDGE = re.compile(r"\\begin\{microknowledge\}(?:\[([^\]]*)\])?")
RE_KB_END = re.compile(r"\\end\{knowledgebox\}")
RE_MK_END = re.compile(r"\\end\{microknowledge\}")
RE_CORE_KEYWORDS = re.compile(r"(定理|引理|同构|分解|正合列|分类|互反律|判定准则|判据|同余式|Routh|EFFT|Ceva)")
RE_EXAMPLE_KEYWORDS = re.compile(r"(计算|验算|试算|特例|例如|实例|小素数|具体|设\s*p\s*=\s*[2357])")
RE_NUMERIC_EXAMPLE = re.compile(
    r"\(\s*-?\d+\s*[:,\s]\s*-?\d+(?:\s*[:,\s]\s*-?\d+)?\s*\)"
    r"|\b(?:3\s*,\s*4\s*,\s*5|\d+\s*,\s*\d+\s*,\s*\d+)\b"
    r"|\b[pPnN]\s*=\s*[2357]\b"
    r"|\\frac\{\s*\d+\s*\}\{\s*\d+\s*\}"
)
# 教师专属区（不计入学生可见统计与包办检测）
RE_TEACHER_BLOCK = re.compile(
    r"\\begin\{(solution|teacherNote)\}.*?\\end\{\1\}", re.S)
# "预解小问"宣布式句型：动词后同一行内紧跟等号/公式，且该小问零填空
RE_ANNOUNCE_SOLVED = re.compile(
    r"(求得|解得|算得|化简得|化简为|展开得|代入得|即为|可得|整理得出)"
    r"[^。；\n]{0,30}=")


def norm_text(s):
    return check_numbering.norm_opt(s).lower()


def check_pedagogy_slice(name, text, problems, warnings):
    """单切片教学语义与认知结构质检。"""
    # 教师专属区（solution/teacherNote）内的内容属教师可见，
    # 不参与学生可见统计、知识框/卡存在性与闭合性检测、填空剧透/包办检测。
    teacher_spans = [(m.start(), m.end())
                     for m in RE_TEACHER_BLOCK.finditer(text)]

    def _in_teacher(pos):
        return any(a <= pos < b for a, b in teacher_spans)

    # 1. 强制知识框与前置微知识卡（仅保留不在教师专属区内的实例）
    kb_matches = [m for m in RE_KNOWLEDGEBOX.finditer(text)
                  if not _in_teacher(m.start())]
    mk_matches = [m for m in RE_MICROKNOWLEDGE.finditer(text)
                  if not _in_teacher(m.start())]

    if not kb_matches:
        problems.append(
            "%s: 缺失章节末知识总结框（每节末必须包含 \\begin{knowledgebox}[本节结论]...\\end{knowledgebox}）"
            % name
        )
    if not mk_matches:
        problems.append(
            "%s: 缺失前置微知识卡（题目前必须提供所需的对象、运算与记号，至少包含一个 \\begin{microknowledge}...\\end{microknowledge} 实线框）"
            % name
        )

    # 闭合性检查：确保每个学生可见的 \begin{knowledgebox} 与 \begin{microknowledge} 均有闭合且在学生可见区的 \end
    def _check_env_closure(env_name, begin_re, end_re):
        begins = [(m.start(), "begin", text.count("\n", 0, m.start()) + 1)
                  for m in begin_re.finditer(text) if not _in_teacher(m.start())]
        ends = [(m.start(), "end", text.count("\n", 0, m.start()) + 1)
                for m in end_re.finditer(text) if not _in_teacher(m.start())]
        events = sorted(begins + ends, key=lambda x: x[0])
        stack = []
        for pos, kind, lineno in events:
            if kind == "begin":
                stack.append((pos, lineno))
            else:
                if not stack:
                    problems.append(
                        "%s:%d: 孤立的 \\end{%s}（缺少对应的 \\begin{%s}）"
                        % (name, lineno, env_name, env_name)
                    )
                else:
                    stack.pop()
        for pos, lineno in stack:
            after_ends = [m for m in end_re.finditer(text) if m.start() > pos]
            if after_ends and _in_teacher(after_ends[0].start()):
                err_line = text.count("\n", 0, after_ends[0].start()) + 1
                problems.append(
                    "%s:%d: %s 环境未闭合（其对应的 \\end{%s} 在第 %d 行错误落入教师区，导致学生版不可见）"
                    % (name, lineno, env_name, env_name, err_line)
                )
            else:
                problems.append(
                    "%s:%d: %s 环境未闭合（缺少对应的学生可见 \\end{%s}）"
                    % (name, lineno, env_name, env_name)
                )

    _check_env_closure("knowledgebox", RE_KNOWLEDGEBOX, RE_KB_END)
    _check_env_closure("microknowledge", RE_MICROKNOWLEDGE, RE_MK_END)

    # 相对位置检查：
    # - 第一个 microknowledge 必须出现在第一个 \begin{probchain} 之前
    # - 最后一个 \end{probchain} 之后必须能找到 knowledgebox（节末总结）
    first_prob = text.find(r"\begin{probchain}")
    last_prob = text.rfind(r"\end{probchain}")

    if mk_matches and first_prob >= 0:
        first_mk = min(m.start() for m in mk_matches)
        if first_mk > first_prob:
            problems.append(
                "%s: 所有前置知识卡均出现在题目之后（首个 microknowledge 在第 %d 行，"
                "首个 probchain 在第 %d 行），学生无法自学！"
                % (name, text.count("\n", 0, first_mk) + 1,
                   text.count("\n", 0, first_prob) + 1)
            )

    if kb_matches and last_prob >= 0:
        last_prob_end = last_prob + len(r"\end{probchain}")
        has_kb_after = any(m.start() >= last_prob_end for m in kb_matches)
        if not has_kb_after:
            first_kb = min(m.start() for m in kb_matches)
            problems.append(
                "%s: knowledgebox 出现在题链中间而非节末（首个在第 %d 行），"
                "请将总结框移至最后一个 probchain 之后（第 %d 行后）"
                % (name, text.count("\n", 0, first_kb) + 1,
                   text.count("\n", 0, last_prob) + 1)
            )
        elif any(m.start() < last_prob for m in kb_matches):
            problems.append(
                "%s: 发现 knowledgebox 出现在题链中间而非节末，"
                "请统一将节末总结框置于最后一个 \\end{probchain} 之后（题目前置知识请使用 microknowledge）"
                % name
            )

    # 2. 检查题块中的认知台阶与防剧透
    fillins = [f for f in check_numbering.scan_fillins(text)
               if not _in_teacher(f[0])]
    constant_ans_count = 0
    total_fillins = len(fillins)

    for pos, answer, width, options in fillins:
        if not answer:
            continue
        ans_norm = norm_text(answer)
        lineno = text.count("\n", 0, pos) + 1
        where = "%s:%d" % (name, lineno)

        if ans_norm in ("0", "$0$", "1", "$1$", "-1", "$-1$"):
            constant_ans_count += 1

        # 防剧透检测：检查 \fillin 之前紧邻的字符内是否出现显式陈述结论并要求填相同内容
        prev_chunk = text[max(0, pos - 80):pos]
        spoiler_patterns = [
            r"(?:答案为|结果为|记作|形式为|即|即为|结论为)\s*[:：]?\s*[\$]?([^$:：\n]+)[\$]?\s*[:：=]\s*$",
        ]
        for sp in spoiler_patterns:
            m = re.search(sp, prev_chunk)
            if m:
                spoiled_word = norm_text(m.group(1))
                if spoiled_word and spoiled_word == ans_norm:
                    problems.append(
                        "%s: 填空疑似题干直接剧透目标答案（题干出现“%s”，直接要求填写相同内容）"
                        % (where, m.group(1).strip())
                    )

        # 伪自学宣读式填空拦截：题干宣读定理，末尾只挖空填常量或定理名
        tautology_patterns = [
            r"(?:当且仅当|充要条件为|等价于|判定条件为|互相垂直当且仅当|三点共线当且仅当|三线共点当且仅当).*?(?:等于|为)\s*[:：]?\s*$",
        ]
        for tp in tautology_patterns:
            if re.search(tp, prev_chunk) and ans_norm in ("0", "$0$", "1", "$1$", "-1", "$-1$"):
                problems.append(
                    "%s: 伪自学宣读式填空（题干宣读性质后仅挖空填常数 %s，缺乏动手计算量，应改为求解具体代数表达式或数值）"
                    % (where, answer)
                )

    # 常量填空率上限检测（学生可见填空）
    if total_fillins >= 4:
        ratio = constant_ans_count / total_fillins
        if ratio > 0.20:
            problems.append(
                "%s: 常量类填空（0/1/-1）占比过高（%d/%d = %.1f%% > 20%%），存在看词猜答案的伪自学倾向，必须替换为具体代数式或数值运算结果"
                % (name, constant_ans_count, total_fillins, ratio * 100)
            )
    elif 2 <= total_fillins <= 3 and constant_ans_count == total_fillins:
        # 小切片全常量兜底：2~3 个填空全是 0/1/-1 同样属伪自学
        problems.append(
            "%s: 全部 %d 个学生可见填空答案均为常量（0/1/-1），属看词猜答案的伪自学填空，必须替换为具体代数式或数值运算结果"
            % (name, total_fillins)
        )

    # 3. 提取一级题目块，检查认知步长与旁观者题干
    RE_BEGIN_END = re.compile(r"\\(begin|end)\{([a-zA-Z*]+)\}|(\\item\b)")
    stack = []
    questions = []
    cur_q = None

    for m in RE_BEGIN_END.finditer(text):
        kind = m.group(1)
        env = m.group(2)
        is_item = bool(m.group(3))

        if kind == "begin":
            stack.append(env)
        elif kind == "end":
            if env == "probchain" and cur_q:
                cur_q["end"] = m.start()
                questions.append(cur_q)
                cur_q = None
            if stack and stack[-1] == env:
                stack.pop()
        elif is_item:
            in_probchain = "probchain" in stack
            nested_lists = [
                e for e in stack
                if e in ("enumerate", "itemize", "description", "choices", "tasks")
            ]
            if in_probchain and not nested_lists:
                if cur_q:
                    cur_q["end"] = m.start()
                    questions.append(cur_q)
                cur_q = {
                    "start": m.start(),
                    "end": len(text),
                    "line": text.count("\n", 0, m.start()) + 1,
                    "sub_items": 0,
                    "sub_pos": [],
                    "sub_fillins": 0,
                }
            elif cur_q and nested_lists and in_probchain:
                cur_q["sub_items"] += 1
                cur_q["sub_pos"].append(m.start())

    if cur_q:
        questions.append(cur_q)

    slice_has_core_theorems = False
    for q in questions:
        q_text = text[q["start"]:q["end"]]
        # 仅取学生可见的正文部分（题干与小问列表，排除 solution/teacherNote 教师答案区）
        student_part = q_text.split("\\begin{solution}")[0] if "\\begin{solution}" in q_text else q_text
        has_core_kw = bool(RE_CORE_KEYWORDS.search(student_part))

        # 旁观者题干检测：检查 \begin{enumerate} 之前的题干是否过于冗长且包揽运算
        stem_part = student_part.split("\\begin{enumerate}")[0] if "\\begin{enumerate}" in student_part else student_part
        stem_clean = re.sub(r"\\[a-zA-Z]+", " ", stem_part)
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", stem_clean))
        eq_count = stem_part.count("=") + stem_part.count("\\det") + stem_part.count("\\times")

        if chinese_count > 120 and eq_count >= 3:
            problems.append(
                "%s:%d: 题干存在包办推导嫌疑（题干汉字数 %d 且包含 %d 处等号/公式，包揽了过多中间推导），严禁题干自言自语，强制将推导过程拆解为多步独立小问由学生动笔！"
                % (name, q["line"], chinese_count, eq_count)
            )

        # 旁观者中间小问检测：小问内部零填空却直接宣布已算好的中间结果
        # （如"联立求解叉乘，求得第一分量为 u1 = ..."），同样剥夺学生动笔环节
        sub_positions = [p - q["start"] for p in q.get("sub_pos", [])]
        limit = len(student_part)
        for idx, sp in enumerate(sub_positions):
            if sp >= limit:
                continue
            ep = sub_positions[idx + 1] if idx + 1 < len(sub_positions) else limit
            ep = min(ep, limit)
            sub_text = student_part[sp:ep]
            if "\\fillin" in sub_text:
                continue  # 有学生动作的小问不算旁观
            m_ann = RE_ANNOUNCE_SOLVED.search(sub_text)
            if m_ann:
                problems.append(
                    "%s:%d: 第 %s 个小问存在预解包办嫌疑（该小问未设置任何填空，却直接宣布算得中间结果“%s”），关键中间推导必须改为由学生动笔计算，严禁把解答印在题干里！"
                    % (name, q["line"], "abcdefghijklmnopqrstuvwxyz"[idx] if idx < 26 else str(idx + 1),
                       sub_text[m_ann.end():m_ann.end() + 24].strip())
                )

        # 核心定理/引理题认知步长检测：涉及核心定理证明/推导的大题必须拆解为至少 4~7 步台阶
        # 触发条件收紧：核心词出现在一级题干，或全题累计出现 ≥2 次（避免"二项式定理"等
        # 顺带提及触发误拦；纯证明任务仍能稳定命中）
        core_hits = RE_CORE_KEYWORDS.findall(student_part)
        kw_in_stem = bool(RE_CORE_KEYWORDS.search(stem_part))
        if has_core_kw and (kw_in_stem or len(core_hits) >= 2):
            slice_has_core_theorems = True
            kw = RE_CORE_KEYWORDS.search(student_part).group(0)
            if q["sub_items"] < 4:
                problems.append(
                    "%s:%d: 核心定理/引理大题认知步长过大（涉及“%s”但仅配置了 %d 个小问台阶），长逻辑链核心推导必须拆解为 (a) 到 (f) 至少 4~7 个连续小问！"
                    % (name, q["line"], kw, max(1, q["sub_items"]))
                )

    # 4. 数值算例先行检测（核心定理切片强制硬阻断，其他切片提示警告）
    has_numeric = bool(
        RE_NUMERIC_EXAMPLE.search(text)
        or (RE_EXAMPLE_KEYWORDS.search(text) and re.search(r"\d", text))
    )
    if slice_has_core_theorems and not has_numeric:
        problems.append(
            "%s: 核心定理切片缺少具体数值/低阶特例算例演练（全篇均为抽象字母参数），严重缺失算例先行认知脚手架！核心大定理推导前必须配置具体数值或低阶特例验算。"
            % name
        )
    elif not RE_NUMERIC_EXAMPLE.search(text):
        warnings.append(
            "%s: 切片缺少具体数值/坐标演练算例（全篇均为抽象字母参数），缺乏算例先行脚手架，建议增加一道包含具体整数坐标或数值三角形边长的代入演练题"
            % name
        )

    return len(kb_matches), len(mk_matches)


def scan_project_pedagogy(workdir):
    """扫描项目各切片教学语义。"""
    inputs = check_numbering.sec_inputs(workdir)
    if inputs is None:
        inputs = sorted((f for f in os.listdir(workdir)
                         if re.fullmatch(r"sec.*\.tex", f)),
                        key=check_numbering.sec_key)
    files = [f for f in inputs if os.path.exists(os.path.join(workdir, f))]
    problems = []
    warnings = []
    stats = {}
    for f in files:
        path = os.path.join(workdir, f)
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                txt = fp.read()
        except OSError as e:
            problems.append("%s: 无法读取切片: %s" % (f, e))
            continue
        kb_n, mk_n = check_pedagogy_slice(f, txt, problems, warnings)
        stats[f] = {"knowledgebox_count": kb_n, "microknowledge_count": mk_n}
    return files, problems, warnings, stats


def main():
    ap = argparse.ArgumentParser(description="题链教学语义与认知结构门禁 (check_pedagogy.py)")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args()
    workdir = os.path.abspath(args.workdir)

    manifest = evidence.collect_inputs(workdir)
    digest = evidence.content_digest(manifest)
    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "check_pedagogy.py",
        "tool_version": TOOL_VERSION,
        "scope": {"workdir": workdir, "files": []},
        "input_digest": digest,
        "pdf_sha256": None,
        "status": "pass",
        "issues": [],
        "evidence": {}
    }

    files, problems, warnings, stats = scan_project_pedagogy(workdir)
    report["scope"]["files"] = files
    report["evidence"]["pedagogy_stats"] = stats

    for p in problems:
        report["issues"].append({"severity": "error", "message": p})
    for w in warnings:
        report["issues"].append({"severity": "warning", "message": w})

    passed = len(problems) == 0
    report["status"] = "pass" if passed else "fail"

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        evidence.write_report(args.json, report)
        print("[check_pedagogy] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_pedagogy] [警告] %s" % w)
    if problems:
        print("[check_pedagogy] 发现 %d 个教学语义门禁问题:" % len(problems))
        for p in problems:
            print("  - [FAIL] %s" % p)
        return 1

    print("[check_pedagogy] 通过：%d 个切片文件教学语义完整（前置知识卡与节末结论框完备，认知阶梯合规）。" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
