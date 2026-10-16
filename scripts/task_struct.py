#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
task_struct.py — 题链学案切片 TeX 结构与题目小问解析通用模块

抽离自 check_pedagogy.py 与 check_numbering.py，提供独立的结构提取能力：
1. 教师专属区过滤（solution/teacherNote 教师答案区内的 \fillin 与说明不计入学生可见部分）；
2. 逐题结构提取（line, qid, sub_items, sub_fillins, is_theorem_proof, dialogue_turns）；
3. scan_fillins 独立扫描器与括号配对扫描；
4. 定理证明长逻辑链判定。
"""

import bisect
import os
import re

# 教师专属区（内容学生版不可见）
RE_TEACHER_BLOCK = re.compile(
    r"\\begin\{(solution|teacherNote)\}.*?\\end\{\1\}", re.DOTALL
)

# 定理/引理/推导长逻辑链关键词
RE_THEOREM_KEYWORDS = re.compile(
    r"(证明|推导|定理|引理|同构|分解|正合列|分类|互反律|判定准则|判据|同余式|Routh|EFFT|Ceva)",
    re.IGNORECASE,
)

# 对话 speaker 统计与标记
RE_SPEAKER = re.compile(r"\\speaker\{")
RE_DLG_QID = re.compile(r"^\s*%\s*dialogue:\s*qid=([^\s%]+)", re.MULTILINE)
RE_EX_DLG_BLOCK = re.compile(
    r"\\begin\{exampledialogue\}(?:\[.*?\])?(.*?)\\end\{exampledialogue\}",
    re.DOTALL,
)

# 题目 qid 注释
RE_QID_LINE = re.compile(r"^[ \t]*%[ \t]*qid:[ \t]*([^\s%]+)")

# 列表环境与题目 token
LIST_ENVS = ("enumerate", "itemize", "description", "choices", "tasks")
TOKEN = re.compile(
    r"\\(begin|end)\{([a-zA-Z*]+)\}"  # 1=kind 2=env
    r"|\\item\b"  # 列表/题目项
)


def _scan_bracket(text, k):
    r"""从 text[k]=='[' 起扫描配对 ]（\ 转义与 {} 分组感知）；
    返回 (新位置, 内容, 是否闭合)。
    """
    n = len(text)
    k += 1
    buf, brace, closed = [], 0, False
    while k < n:
        c = text[k]
        if c == "\\" and k + 1 < n:
            buf.append(c)
            buf.append(text[k + 1])
            k += 2
            continue
        if c == "{":
            brace += 1
        elif c == "}" and brace > 0:
            brace -= 1
        elif c == "]" and brace == 0:
            closed = True
            k += 1
            break
        buf.append(c)
        k += 1
    return k, "".join(buf), closed


def _scan_brace(text, k):
    """从 text[k]=='{' 起扫描配对 }；返回 (新位置, 内容, 是否闭合)。"""
    n = len(text)
    k += 1
    buf, brace, closed = [], 0, False
    while k < n:
        c = text[k]
        if c == "{":
            brace += 1
        elif c == "}":
            if brace == 0:
                closed = True
                k += 1
                break
            brace -= 1
        buf.append(c)
        k += 1
    return k, "".join(buf), closed


def scan_fillins(text):
    r"""扫描 \fillin[答案]{宽度}[选项1/选项2/...]，返回
    [(start, answer, width, options)]；answer/options 可为 None，
    括号未闭合等无法解析的写法跳过。
    """
    out = []
    i, n = 0, len(text)
    while True:
        j = text.find("\\fillin", i)
        if j < 0:
            break
        k = j + len("\\fillin")
        while k < n and text[k] in " \t\n":
            k += 1
        answer = None
        if k < n and text[k] == "[":
            k, answer, ok = _scan_bracket(text, k)
            if not ok:
                i = k
                continue
            while k < n and text[k] in " \t\n":
                k += 1
        if k >= n or text[k] != "{":
            i = k
            continue
        k, width, ok = _scan_brace(text, k)
        if not ok:
            i = k
            continue
        while k < n and text[k] in " \t\n":
            k += 1
        options = None
        if k < n and text[k] == "[":
            k, options, ok = _scan_bracket(text, k)
            if not ok:
                i = k
                continue
        i = k
        out.append((j, answer, width, options))
    return out


def parse_section_structure(tex_content, expected_qids=None):
    r"""解析切片 TeX 文本中的题目结构。

    返回题目列表，每个题目为 dict：
      - line: int 大题起始行号（从 1 开始）
      - qid: str or None 稳定题 ID
      - sub_items: int 小问数
      - sub_fillins: int 小问内填空数（当无小问时取题目学生可见填空数）
      - total_fillins: int 题目内学生可见填空总数（排除 solution/teacherNote）
      - is_theorem_proof: bool 是否包含证明/推导/定理长链关键词
      - dialogue_turns: int 关联的对话轮次（speaker 句数）
    """
    if not tex_content:
        return []

    if expected_qids is None:
        expected_qids = []

    # 1. 识别教师专属区区间
    teacher_spans = [
        (m.start(), m.end()) for m in RE_TEACHER_BLOCK.finditer(tex_content)
    ]

    def _in_teacher(pos):
        return any(a <= pos < b for a, b in teacher_spans)

    # 2. 扫描所有学生可见的填空
    raw_fillins = scan_fillins(tex_content)
    student_fillins = [f for f in raw_fillins if not _in_teacher(f[0])]

    # 3. 扫描对话块 (exampledialogue)
    dlg_by_qid = {}
    dlg_by_pos = []  # (end_pos, turns)
    for m in RE_EX_DLG_BLOCK.finditer(tex_content):
        body = m.group(1)
        turns = len(RE_SPEAKER.findall(body))
        m_qid = RE_DLG_QID.search(body)
        if m_qid:
            dlg_by_qid[m_qid.group(1)] = turns
        else:
            dlg_by_pos.append((m.end(), turns))

    # 4. 预计算行首位置用于快速定位行号
    lines = tex_content.splitlines(keepends=True)
    line_starts = [0]
    for l in lines[:-1]:
        line_starts.append(line_starts[-1] + len(l))

    def get_lineno(pos):
        return bisect.bisect_right(line_starts, pos)

    # 5. 逐行与单趟合并扫描，使用状态机精准捕获 pending_qid
    has_probchain = r"\begin{probchain}" in tex_content
    stack = []
    questions = []
    cur_q = None
    pending_qid = None

    char_offset = 0
    for lineno, raw_line in enumerate(lines, 1):
        # 匹配该行是否为 % qid: 注释
        m_qid = RE_QID_LINE.match(raw_line)
        if m_qid:
            pending_qid = m_qid.group(1).strip()

        # 处理行内 token
        for m in TOKEN.finditer(raw_line):
            tok = m.group(0)
            kind = m.group(1)
            env = m.group(2)
            tok_pos = char_offset + m.start()

            if kind == "begin":
                stack.append((env, tok_pos))
            elif kind == "end":
                if env == "probchain" and cur_q:
                    cur_q["end"] = tok_pos
                    questions.append(cur_q)
                    cur_q = None
                if stack and stack[-1][0] == env:
                    stack.pop()
                else:
                    for idx in range(len(stack) - 1, -1, -1):
                        if stack[idx][0] == env:
                            del stack[idx:]
                            break
            elif tok.startswith("\\item"):
                # 判断当前 \item 是大题还是小问
                if has_probchain:
                    prob_idx = -1
                    for idx, item in enumerate(stack):
                        if item[0] == "probchain":
                            prob_idx = idx
                            break
                    if prob_idx >= 0:
                        sub_lists = [
                            e[0]
                            for e in stack[prob_idx + 1 :]
                            if e[0] in LIST_ENVS
                        ]
                        if not sub_lists:
                            # 一级大题
                            if cur_q:
                                cur_q["end"] = tok_pos
                                questions.append(cur_q)
                            cur_q = {
                                "start": tok_pos,
                                "end": len(tex_content),
                                "line": lineno,
                                "qid": pending_qid,
                                "sub_items": 0,
                            }
                            pending_qid = None
                        else:
                            # 小问
                            if cur_q:
                                cur_q["sub_items"] += 1
                else:
                    list_depth = sum(1 for e in stack if e[0] in LIST_ENVS)
                    if list_depth <= 1:
                        if cur_q:
                            cur_q["end"] = tok_pos
                            questions.append(cur_q)
                        cur_q = {
                            "start": tok_pos,
                            "end": len(tex_content),
                            "line": lineno,
                            "qid": pending_qid,
                            "sub_items": 0,
                        }
                        pending_qid = None
                    else:
                        if cur_q:
                            cur_q["sub_items"] += 1

        char_offset += len(raw_line)

    if cur_q:
        questions.append(cur_q)

    # 6. 汇总每道大题的详细指标
    results = []
    prev_end = 0

    for idx, q in enumerate(questions):
        q_start = q["start"]
        q_end = q["end"]
        matched_qid = q["qid"]

        # 若未从源码中提取出 qid，尝试按顺序从 expected_qids 取
        if not matched_qid and expected_qids and idx < len(expected_qids):
            matched_qid = expected_qids[idx]

        # 统计本题区间的学生可见填空数 (排除教师专属区)
        q_fillins = sum(
            1 for f in student_fillins if q_start <= f[0] < q_end
        )

        # 提取学生可见文本，检查定理长链/证明推导关键词
        raw_text = tex_content[q_start:q_end]
        student_text = RE_TEACHER_BLOCK.sub("", raw_text)
        is_thm = bool(RE_THEOREM_KEYWORDS.search(student_text))

        # 对话轮次关联
        turns = 0
        if matched_qid and matched_qid in dlg_by_qid:
            turns = dlg_by_qid[matched_qid]
        else:
            for d_end, d_turns in dlg_by_pos:
                if prev_end <= d_end <= q_start:
                    turns = d_turns
                    break

        results.append(
            {
                "line": q["line"],
                "qid": matched_qid,
                "sub_items": q["sub_items"],
                "sub_fillins": q_fillins,
                "total_fillins": q_fillins,
                "is_theorem_proof": is_thm,
                "dialogue_turns": turns,
            }
        )
        prev_end = q_end

    return results


def parse_section_file(file_path, expected_qids=None):
    """便捷辅助函数：从指定 TeX 文件路径解析题目结构。"""
    if not os.path.isfile(file_path):
        return []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return parse_section_structure(content, expected_qids=expected_qids)


def sec_key(name):
    """排序切片文件名：sec1.tex < sec2.tex < sec10.tex"""
    m = re.search(r"(\d+)", name)
    return (int(m.group(1)) if m else 0, name)


def scan_project_structures(project_dir):
    r"""扫描项目目录下所有的切片文件 (sec*.tex)，
    返回字典: { "sec1.tex": [task_dict, ...], ... }
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        return {}

    # 优先检查 main.tex 的 \input 顺序
    sec_files = []
    main_tex = os.path.join(project_dir, "main.tex")
    if os.path.isfile(main_tex):
        try:
            with open(main_tex, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for m in re.finditer(r"\\input\{([^{}]+)\}", content):
                fpath = m.group(1).strip()
                fname = os.path.basename(fpath)
                if not fname.endswith(".tex"):
                    fname += ".tex"
                if re.match(r"^sec.*\b", fname):
                    if os.path.isfile(os.path.join(project_dir, fname)):
                        if fname not in sec_files:
                            sec_files.append(fname)
        except Exception:
            pass

    # 若 main.tex 未找到或未声明，直接从目录枚举并按自然数排序
    if not sec_files:
        all_files = [
            f
            for f in os.listdir(project_dir)
            if re.match(r"^sec.*\.tex$", f)
            and os.path.isfile(os.path.join(project_dir, f))
        ]
        sec_files = sorted(all_files, key=sec_key)

    structures = {}
    for fname in sec_files:
        fpath = os.path.join(project_dir, fname)
        structures[fname] = parse_section_file(fpath)

    return structures


def get_qid_map(structures):
    r"""将 scan_project_structures 的返回结果转换为以 qid 为键的映射：
    { qid: {**task_dict, "section": "sec1.tex"}, ... }
    """
    qid_map = {}
    for sec_name, tasks in structures.items():
        for t in tasks:
            qid = t.get("qid")
            if qid:
                entry = dict(t)
                entry["section"] = sec_name
                qid_map[qid] = entry
    return qid_map

