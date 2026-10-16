#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""tex2cards.py — 自学案 TeX 源 → HTML 卡片牌组 (cards.json) 解析器

qchain-html（题链 HTML）流水线的“填写规范”机器侧：把 sec*.tex 的每个组件解析为一张
卡片（card），一卡对应 HTML 的一页。本模块只做字面量解析（与 check_numbering.py
同一套注释/括号判定口径），不做数学求值；解析不了的写法记入 issues，绝不静默
丢弃内容。

卡片类型（kind）与页面对应关系见 references/html-interface.md：
  section    节标题链 HTML（\section* + 前置导语）
  dialogue   对话卡（sectiondialogue / exampledialogue，一条 \speaker 一行台词）
  question   题目卡（probchain 一级 \item；qid 为稳定身份）
  knowledge   微型知识点卡（microknowledge）
  conclusion 节末结论卡（knowledgebox）

选择题化填空 \fillin[答案]{宽度}[选项1/选项2/...] 解析为交互槽位（slot）：
学生版渲染为可点击选项按钮（点击即时红/绿判定），答案文本不进入可见文本。

用法（库）:
  from tex2cards import parse_project
  deck, issues = parse_project("self")

命令行自检:
  python tex2cards.py <project> [--json cards.json] [--pretty]
退出码: 0=解析完成（issues 可能含提示）, 1=解析失败（输入缺失/结构错误）。
"""
import argparse
import json
import os
import re
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_numbering  # noqa: E402

DECK_SCHEMA = "qchain-html-deck/1"

# 对话环境：渲染为对话卡（一条 \speaker 一行台词）
DIALOG_ENVS = ("sectiondialogue", "exampledialogue")
# 知识卡环境
KNOWLEDGE_ENVS = ("microknowledge", "knowledgebox", "hintbox")
# 教师专属环境：内容仅教师版默认可见（卡片字段名映射）
TEACHER_ENVS = {"solution": "solution_html",
                "teacherNote": "teacher_note_html"}
# 列表环境
LIST_ENVS = ("enumerate", "itemize", "description", "choices", "tasks")
BLOCK_ENVS = set(LIST_ENVS) | set(DIALOG_ENVS) | set(KNOWLEDGE_ENVS) | set(TEACHER_ENVS.keys()) | {"probchain"}
_BLOCK_ENVS_RE = "|".join(re.escape(e) for e in sorted(BLOCK_ENVS))

RE_INPUT = re.compile(r"\\input\s*\{([^{}]+)\}")
RE_QID_META = re.compile(r"^\s*%\s*qid:\s*([^\s%|]+)\s*(?:\|(.*))?$")
RE_DIALOGUE_LINE = re.compile(r"^\s*%\s*dialogue:\s*qid=([^\s%]+)")
RE_MAIN_TITLE = re.compile(r"(?m)^\s*main_title\s*:\s*(.+?)\s*$")
RE_TOKEN = re.compile(
    r"\\begin\s*\{(" + _BLOCK_ENVS_RE + r")\}"       # 1=begin 环境
    r"|\\end\s*\{(" + _BLOCK_ENVS_RE + r")\}"        # 2=end 环境
    r"|(\\item\b)"                                    # 3=列表项/题目
    r"|\\section\s*\*\s*\{([^{}]*)\}"                # 4=节标题
    r"|\\qtype\s*\{([^{}]*)\}"                       # 5=题型
    r"|\\practicemode\s*\{([^{}]*)\}"                # 6=练习模式
    r"|(\\speaker)(?=\s*\{)"                        # 7=对话台词（参数另行扫描）
    r"|\\input\s*\{([^{}]*)\}"                       # 8=引用（备用）
)
RE_CTRL = re.compile(r"\\([a-zA-Z]+)\*?")


# ---------------------------------------------------------------------------
# 基础扫描工具（与 check_numbering.py / compare_versions.py 同口径）
# ---------------------------------------------------------------------------
def strip_comment(line):
    r"""去掉行内注释；\% 是转义字符，不作为注释起点。"""
    i = 0
    while True:
        j = line.find("%", i)
        if j < 0:
            return line
        k = j - 1
        while k >= 0 and line[k] == "\\":
            k -= 1
        if (j - 1 - k) % 2 == 1:  # 奇数个反斜杠 → \% 转义
            i = j + 1
        else:
            return line[:j]


def mask_comments(lines):
    """逐行把注释部分替换为等长空格（保持偏移与行号），供 token 定位。"""
    out = []
    for line in lines:
        s = strip_comment(line)
        if len(s) < len(line):
            out.append(s + " " * (len(line) - len(s)))
        else:
            out.append(s)
    return out


def scan_brace(text, k):
    r"""从 text[k]=='{' 起扫描配对 }；返回 (新位置, 内容, 是否闭合)。"""
    i, buf, brace, closed = k + 1, [], 0, False
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            brace += 1
        elif c == "}":
            if brace == 0:
                closed = True
                i += 1
                break
            brace -= 1
        buf.append(c)
        i += 1
    return i, "".join(buf), closed


def scan_bracket(text, k):
    r"""从 text[k]=='[' 起扫描配对 ]（\\ 转义与 {} 分组感知）；
    返回 (新位置, 内容, 是否闭合)。"""
    i, buf, brace, closed = k + 1, [], 0, False
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(text[i + 1])
            i += 2
            continue
        if c == "{":
            brace += 1
        elif c == "}" and brace > 0:
            brace -= 1
        elif c == "]" and brace == 0:
            closed = True
            i += 1
            break
        buf.append(c)
        i += 1
    return i, "".join(buf), closed


def norm_opt(s):
    """选项/答案归一化：去空白、$、{} 与反斜杠（与 check_numbering 同口径）。"""
    return check_numbering.norm_opt(s)


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# 行内 TeX → HTML 转换（数学段原样保留，由前端 KaTeX 渲染）
# ---------------------------------------------------------------------------
SIMPLE_MACROS = {
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ", ":": " ",
    " ": " ", "par": " ", "noindent": " ", "small": "", "normalsize": "",
    "large": "", "Large": "", "huge": "", "Huge": "", "centering": "",
    "raggedcolumns": "", "newpage": "", "bigskip": "", "medskip": "",
    "hfill": " ", "vfill": " ", "strut": "",
}
# 文本样式宏 → HTML 标签
STYLE_MACROS = {
    "textbf": "strong", "textit": "em", "emph": "em", "underline": "u",
    "textsl": "em", "texttt": "code", "textsc": "span",
}
MATHISH_MACROS = ("mathbf", "mathrm", "mathbb", "mathcal", "mathfrak",
                  "mathsf", "mathtt", "boldsymbol", "operatorname", "bm")


def _find_math_end(text, i):
    r"""text[i]=='$'；返回匹配的结束 '$' 位置（无匹配返回 -1）。

    \$ 为转义美元符，不算定界；$$ 为显示公式定界。
    """
    n = len(text)
    display = text[i:i + 2] == "$$"
    j = i + (2 if display else 1)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if display and text[j:j + 2] == "$$":
            return j
        if not display and c == "$":
            return j
        j += 1
    return -1


class InlineCtx:
    """行内转换上下文：登记槽位/图片/提示。"""

    def __init__(self, slot_prefix, slot_offset=0):
        self.slot_prefix = slot_prefix
        self.slot_offset = slot_offset
        self.slot_count = 0
        self.slots = []
        self.images = set()
        self.issues = []
        self.teacher_only = False  # 当前段落是否含 \dlgteacher


def inline_to_html(text, ctx):
    r"""把行内 TeX 转为 HTML。

    - $...$ / $$...$$ 原样保留（外层包 qc-math，前端 KaTeX 渲染；
      加载失败时退化为源码文本，不影响翻页与交互）；
    - \fillin[答案]{宽度}[选项...] → 交互槽位按钮组（登记到 ctx.slots）；
    - \ansspace{高度} → 答题留白占位；
    - \includegraphics{路径} → <img>（图片资产由渲染层复制到 assets/）；
    - 已知文本宏转 HTML 标签；未知宏原样保留并记 issues（内容不丢失）。
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "$":
            j = _find_math_end(text, i)
            if j < 0:
                out.append(html_escape(text[i:]))
                ctx.issues.append("未闭合的数学定界符 $（按源码文本呈现）")
                break
            inner = text[i:j + 1]
            cls = "qc-math"
            if inner.startswith("$$"):
                cls += " qc-math-display"
            out.append('<span class="%s">%s</span>' % (cls, html_escape(inner)))
            i = j + 1
            continue
        if c == "\\":
            # LaTeX 定界数学：\[ ... \] 与 \( ... \)
            if text[i:i + 2] == "\\[":
                j = text.find("\\]", i + 2)
                if j >= 0:
                    out.append('<span class="qc-math qc-math-display">$$%s$$</span>'
                               % html_escape(text[i + 2:j]))
                    i = j + 2
                    continue
            if text[i:i + 2] == "\\(":
                j = text.find("\\)", i + 2)
                if j >= 0:
                    out.append('<span class="qc-math">$%s$</span>'
                               % html_escape(text[i + 2:j]))
                    i = j + 2
                    continue
            m = RE_CTRL.match(text, i)
            if not m:
                two = text[i:i + 2]
                if two == r"\\":
                    out.append("<br>")
                    i += 2
                    continue
                if two in (r"\{", r"\}", r"\%", r"\$", r"\&", r"\#", r"\_"):
                    out.append(html_escape(two[1]))
                    i += 2
                    continue
                if two in (r"\,", r"\;", r"\!", r"\ "):
                    out.append(" ")
                    i += 2
                    continue
                if two in (r"\(", r"\)", r"\[", r"\]"):
                    # 未闭合的 LaTeX 数学定界：按空格呈现（内容不丢失）
                    out.append(" ")
                    i += 2
                    continue
                out.append(html_escape(text[i]))
                i += 1
                continue
            name = m.group(1)
            i = m.end()
            if name in SIMPLE_MACROS:
                out.append(SIMPLE_MACROS[name])
                continue
            if name == "selfcheck":
                out.append('<div class="qc-selfcheck" style="margin-top:0.4em;padding:4px 8px;border:1px solid #aaa;border-radius:3px;font-size:0.85em;color:#444;">'
                           '<strong>【自学完成度自评】</strong> '
                           '<label style="margin-left:8px;"><input type="checkbox" disabled> 独立完成</label> '
                           '<label style="margin-left:8px;"><input type="checkbox" disabled> 借助提示完成</label> '
                           '<label style="margin-left:8px;"><input type="checkbox" disabled> 遇到卡点</label></div>')
                continue
            if name == "hintref":
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "{":
                    k, href, ok = scan_brace(text, k)
                    if ok:
                        out.append('<span class="qc-hintref" style="color:#0284c7;font-size:0.85em;">（受阻卡点？参考节末提示 %s）</span>'
                                   % html_escape(href.strip()))
                        i = k
                        continue
                ctx.issues.append(r"\hintref 缺少提示编号参数")
                continue
            if name == "fillin":
                i, slot_html = _convert_fillin(text, i, ctx)
                out.append(slot_html)
                continue
            if name == "ansspace":
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "{":
                    k, height, ok = scan_brace(text, k)
                    if ok:
                        out.append('<span class="qc-ansspace" data-h="%s"></span>'
                                   % html_escape(height.strip()))
                        i = k
                        continue
                ctx.issues.append("\\ansspace 缺少高度参数")
                continue
            if name == "includegraphics":
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "[":
                    k, _opt, ok = scan_bracket(text, k)
                    if not ok:
                        ctx.issues.append("\\includegraphics 选项未闭合")
                        i = k
                        continue
                    while k < n and text[k] in " \t\n":
                        k += 1
                if k < n and text[k] == "{":
                    k, path, ok = scan_brace(text, k)
                    if ok:
                        ctx.images.add(path.strip())
                        base = os.path.basename(path.strip())
                        out.append('<img class="qc-img" src="assets/%s" '
                                   'alt="%s" loading="lazy">'
                                   % (html_escape(base), html_escape(base)))
                        i = k
                        continue
                ctx.issues.append("\\includegraphics 缺少路径参数")
                continue
            if name in STYLE_MACROS:
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "{":
                    k, inner, ok = scan_brace(text, k)
                    if ok:
                        tag = STYLE_MACROS[name]
                        out.append("<%s>%s</%s>"
                                   % (tag, inline_to_html(inner, ctx), tag))
                        i = k
                        continue
                ctx.issues.append("\\%s 缺少花括号参数" % name)
                continue
            if name in MATHISH_MACROS:
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "{":
                    k, inner, ok = scan_brace(text, k)
                    if ok:
                        out.append('<span class="qc-math">$\\%s{%s}$</span>'
                                   % (name, html_escape(inner)))
                        i = k
                        continue
                ctx.issues.append("\\%s 缺少花括号参数" % name)
                continue
            if name == "dlgteacher":
                k = i
                while k < n and text[k] in " \t\n":
                    k += 1
                if k < n and text[k] == "{":
                    k, inner, ok = scan_brace(text, k)
                    if ok:
                        ctx.teacher_only = True
                        out.append('<span class="qc-dlgteacher">%s</span>'
                                   % inline_to_html(inner, ctx))
                        i = k
                        continue
                ctx.issues.append("\\dlgteacher 缺少花括号参数")
                continue
            if name == "hintline":
                out.append('<span class="qc-hintline" data-hint="1"></span>')
                continue
            if name in ("qtype", "practicemode", "speaker", "section",
                        "begin", "end", "item"):
                # 块级宏不应出现在行内流；忽略本体（参数由块级解析处理）
                continue
            if name in ("newcommand", "renewcommand", "providecommand", "DeclareMathOperator"):
                for _ in range(3):
                    while i < n and text[i] in " \t\n":
                        i += 1
                    if i < n and text[i] == "[":
                        i, _, _ = scan_bracket(text, i)
                    elif i < n and text[i] == "{":
                        i, _, _ = scan_brace(text, i)
                    else:
                        break
                continue
            if name in ("label", "ref", "pageref"):
                while i < n and text[i] in " \t\n":
                    i += 1
                if i < n and text[i] == "{":
                    i, _, _ = scan_brace(text, i)
                continue
            # 未知宏：保留源码文本，保证内容不丢失
            out.append(html_escape("\\" + name))
            ctx.issues.append("未转换的行内宏 \\%s（按源码文本呈现）" % name)
            continue
        if c == "{":
            i += 1
            continue  # 文本分组：透明
        if c == "}":
            i += 1
            continue
        if c == "&":
            out.append("&amp;")
            i += 1
            continue
        if c == "<":
            out.append("&lt;")
            i += 1
            continue
        if c == ">":
            out.append("&gt;")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _convert_fillin(text, i, ctx):
    r"""解析 \fillin[答案]{宽度}[选项...] 并生成交互槽位 HTML。

    学生版可见形态：下划线空位 + 选项按钮组（点击即时红/绿）。
    正确性以 data-correct 标记（学生版可见文本不含答案文本）。
    """
    n = len(text)
    k = i
    while k < n and text[k] in " \t\n":
        k += 1
    answer = None
    if k < n and text[k] == "[":
        k, answer, ok = scan_bracket(text, k)
        if not ok:
            ctx.issues.append("\\fillin 答案参数未闭合")
            return k, '<span class="qc-slot qc-slot-broken" data-broken="1"></span>'
        while k < n and text[k] in " \t\n":
            k += 1
    width = ""
    if k < n and text[k] == "{":
        k, width, ok = scan_brace(text, k)
        if not ok:
            ctx.issues.append("\\fillin 宽度参数未闭合")
            return k, '<span class="qc-slot qc-slot-broken" data-broken="1"></span>'
        while k < n and text[k] in " \t\n":
            k += 1
    options = None
    if k < n and text[k] == "[":
        k, options, ok = scan_bracket(text, k)
        if not ok:
            ctx.issues.append("\\fillin 选项参数未闭合")
    ctx.slot_count += 1
    slot_id = "%s-s%d" % (ctx.slot_prefix, ctx.slot_offset + ctx.slot_count)
    opts = []
    if options is not None:
        opts = [o.strip() for o in check_numbering.split_options(options, "/")]
    slot = {"id": slot_id, "answer": (answer or "").strip(),
            "width": width.strip(), "options": opts}
    ctx.slots.append(slot)
    parts = ['<span class="qc-slot" data-slot="%s">' % html_escape(slot_id)]
    parts.append('<span class="qc-blank" style="min-width:%s"></span>'
                 % html_escape(width.strip() or "3em"))
    if opts:
        parts.append('<span class="qc-options" role="group" '
                     'aria-label="选择题选项">')
        ans_norm = norm_opt(answer)
        for o in opts:
            correct = "true" if (ans_norm and norm_opt(o) == ans_norm) else "false"
            parts.append(
                '<button type="button" class="qc-option" data-correct="%s">%s'
                '</button>' % (correct, inline_to_html(o, ctx)))
        parts.append("</span>")
    parts.append("</span>")
    return k, "".join(parts)


# ---------------------------------------------------------------------------
# 块级解析：sec*.tex → 卡片列表
# ---------------------------------------------------------------------------
class SecParser:
    """单个切片文件的卡片解析器（栈式状态机）。"""

    def __init__(self, sec_name, lines, seq_base=0):
        self.sec = sec_name
        self.lines = lines
        self.masked = mask_comments(lines)
        self.cards = []
        self.issues = []
        self.in_probchain = False
        self.cur_question = None
        self.cur_dialogue = None
        self.section_card = None
        self.pending_lead = []
        self.seq_base = seq_base
        self._card_no = 0
        self.stem_open = False   # 当前文本是否属于题目题干区
        self.between_buf = []    # 题间区域文本（下一个题目的引导）

    # ---- 卡片工厂 ----
    def new_card(self, kind, line):
        self._card_no += 1
        card = {"id": "%s-%02d" % (self.sec, self._card_no),
                "kind": kind, "section": self.sec,
                "seq": self.seq_base + len(self.cards) + 1,
                "source": "%s:%d" % (self.sec, line)}
        if self.pending_lead:
            lead = "".join(self.pending_lead).strip()
            if lead:
                card["lead_html"] = lead
            self.pending_lead = []
        self.cards.append(card)
        return card

    def finalize_question(self, end_line):
        q = self.cur_question
        if q is None:
            return
        where = q.get("source", q["id"])
        qid = q.get("qid")
        if not qid:
            self.issues.append(
                "%s: 题目块缺少 `%% qid: <稳定题ID>` 注释（卡片身份退化为 %s）"
                % (where, q["id"]))
        elif qid in getattr(self, "_qid_seen", {}):
            self.issues.append(
                "%s: qid “%s” 与 %s 重复（卡片身份必须全章唯一）"
                % (where, qid, self._qid_seen[qid]))
        else:
            if not hasattr(self, "_qid_seen"):
                self._qid_seen = {}
            self._qid_seen[qid] = where
        for env, field in TEACHER_ENVS.items():
            if not q.get(field):
                self.issues.append(
                    "%s: 题目 %s 缺少 %s 环境（该卡%s为空）"
                    % (where, qid or q["id"], env,
                       "参考解答" if env == "solution" else "教学要点"))
        # 题干缓冲 → stem_html（槽位编号接续已登记的槽位，保证全题唯一）
        if q.get("_stem"):
            ctx = InlineCtx(q["id"], len(q.get("slots", [])))
            stem = inline_to_html("".join(q["_stem"]).strip(), ctx)
            for msg in ctx.issues:
                self.issues.append("%s: %s" % (where, msg))
            q["stem_html"] = stem
            q["slots"].extend(ctx.slots)
            del q["_stem"]
        self.cur_question = None

    def parse(self):
        text = "\n".join(self.masked)
        line_starts = []
        pos = 0
        for ln in self.masked:
            line_starts.append(pos)
            pos += len(ln) + 1

        def line_of(idx):
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= idx:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1

        # ---- 注释标记预扫描（qid / 难度 / dialogue 绑定）----
        meta_at_line = {}
        for lineno, raw in enumerate(self.lines, 1):
            s = raw.strip()
            if not s.startswith("%"):
                continue
            m = RE_QID_META.match(s)
            if m:
                meta = {"qid": m.group(1)}
                rest = m.group(2) or ""
                md = re.search(r"难度:\s*([^\s|]+)", rest)
                if md:
                    meta["difficulty"] = md.group(1)
                meta_at_line[lineno] = meta
                continue
            mdd = RE_DIALOGUE_LINE.match(s)
            if mdd:
                meta_at_line[lineno] = {"dialogue_qid": mdd.group(1)}

        stack = []   # 帧: [env, start_line, buf(list), meta(dict)]

        def top():
            return stack[-1] if stack else None

        def route_append(s):
            """文本路由：子题/列表项 → 题干/题间 → 导语缓冲区。"""
            fr = top()
            if fr is None:
                self.pending_lead.append(s)
                return
            meta = fr[3]
            if meta.get("cur_part") is not None:
                meta["cur_part"]["buf"].append(s)
            elif meta.get("cur_item") is not None:
                meta["cur_item"]["buf"].append(s)
            elif meta.get("probchain"):
                # probchain 直属文本：题干开放期归题干，否则归题间引导
                if self.stem_open and self.cur_question is not None:
                    self.cur_question.setdefault("_stem", []).append(s)
                elif self.cur_question is not None and "\\selfcheck" in s:
                    # 容错支持：若 \selfcheck 写在 \end{teacherNote} 之后，仍正确归属于当前题目卡片
                    self.cur_question.setdefault("_stem", []).append(s)
                else:
                    self.between_buf.append(s)
            else:
                fr[2].append(s)

        def close_cur_part(fr, lineno):
            meta, q = fr[3], self.cur_question
            part = meta.get("cur_part")
            if part is None:
                return
            meta["cur_part"] = None
            if q is None:
                return
            ctx = InlineCtx(q["id"], len(q.get("slots", [])))
            html = inline_to_html("".join(part["buf"]).strip(), ctx)
            for msg in ctx.issues:
                self.issues.append("%s:%d: %s" % (self.sec, lineno, msg))
            q["parts"].append({"label": part["label"], "html": html})
            for s in ctx.slots:
                q["slots"].append(s)

        def close_cur_item(fr):
            meta = fr[3]
            item = meta.get("cur_item")
            if item is None:
                return
            meta["cur_item"] = None
            card = meta.get("card") or {}
            ctx = InlineCtx(card.get("id", "item"))
            html = inline_to_html("".join(item["buf"]).strip(), ctx)
            for msg in ctx.issues:
                self.issues.append("%s:%s(item): %s" % (self.sec, item.get(
                    "line", "?"), msg))
            meta.setdefault("items", []).append(html)

        prev_end = 0
        for m in RE_TOKEN.finditer(text):
            idx = m.start()
            if idx > prev_end:
                route_append(text[prev_end:idx])
            prev_end = m.end()
            g = m.lastindex

            if g == 1:  # \begin{env}
                env = m.group(1)
                if env not in BLOCK_ENVS:
                    route_append(m.group(0))
                    continue
                k = m.end()
                opt = None
                while k < len(text) and text[k] in " \t\n":
                    k += 1
                if k < len(text) and text[k] == "[":
                    k, opt, _ok = scan_bracket(text, k)
                fr = [env, line_of(idx), [], {}]
                stack.append(fr)
                meta = fr[3]
                if env == "probchain":
                    self.in_probchain = True
                    meta["probchain"] = True
                elif env in LIST_ENVS:
                    parent = stack[-2][3] if len(stack) >= 2 else {}
                    legacy_series = bool(opt and "series=probchain" in opt)
                    legacy_resume = bool(opt and "resume=probchain" in opt)
                    if parent.get("items_mode"):
                        # 知识卡/结论卡内的列表：条目直接进入卡片
                        meta["items_mode"] = True
                        meta["card"] = parent.get("card")
                    elif legacy_series:
                        # 旧写法题链起点：等价 probchain
                        self.in_probchain = True
                        meta["probchain"] = True
                    elif legacy_resume or parent.get("probchain"):
                        # probchain 直系列表（或旧写法续号）→ 子题收集
                        q = self.cur_question
                        if q is not None and not q.get("parts_opened"):
                            q["parts_opened"] = True
                            meta["parts"] = True
                        else:
                            meta["list"] = True
                    else:
                        meta["list"] = True
                elif env in DIALOG_ENVS:
                    card = self.new_card("dialogue", fr[1])
                    if opt and opt.strip():
                        card["title"] = opt.strip()
                    if env == "exampledialogue":
                        # 绑定注释在 \begin 之后数行内（% dialogue: qid=...）
                        for ln in range(fr[1], min(fr[1] + 3,
                                                    len(self.lines)) + 1):
                            mm = meta_at_line.get(ln)
                            if mm and "dialogue_qid" in mm:
                                card["bind_qid"] = mm["dialogue_qid"]
                                break
                    meta["card"] = card
                    self.cur_dialogue = card
                elif env in KNOWLEDGE_ENVS:
                    card_type = "knowledge" if env == "microknowledge" else ("hint" if env == "hintbox" else "conclusion")
                    card = self.new_card(card_type, fr[1])
                    if opt and opt.strip():
                        card["title"] = opt.strip()
                    elif env == "hintbox":
                        card["title"] = "解题指引与按需提示"
                    card.setdefault("items_html", [])
                    meta["card"] = card
                    meta["items_mode"] = True
                elif env in TEACHER_ENVS:
                    meta["teacher_env"] = env
                prev_end = max(prev_end, k)

            elif g == 2:  # \end{env}
                env = m.group(2)
                if env not in BLOCK_ENVS:
                    route_append(m.group(0))
                    continue
                k = len(stack) - 1
                while k >= 0 and stack[k][0] != env:
                    k -= 1
                if k < 0:
                    self.issues.append("%s:%d: \\end{%s} 没有配对的 \\begin"
                                       % (self.sec, line_of(idx), env))
                    continue
                while len(stack) - 1 > k:
                    fr = stack.pop()
                    if fr[3].get("parts"):
                        close_cur_part(fr, fr[1])
                    elif fr[3].get("items_mode") or fr[3].get("list"):
                        close_cur_item(fr)
                        self._flush_list(fr, stack)
                fr = stack.pop()
                meta = fr[3]
                content = "".join(fr[2])
                if meta.get("teacher_env"):
                    q = self.cur_question
                    field = TEACHER_ENVS[meta["teacher_env"]]
                    self.stem_open = False  # 教师内容结束 → 题干区关闭
                    if q is None:
                        self.issues.append(
                            "%s:%d: %s 出现在题目块之外（内容已忽略）"
                            % (self.sec, fr[1], env))
                    else:
                        ctx = InlineCtx(q["id"], len(q.get("slots", [])))
                        q[field] = inline_to_html(content.strip(), ctx)
                        for msg in ctx.issues:
                            self.issues.append("%s:%d: %s"
                                               % (self.sec, fr[1], msg))
                        for s in ctx.slots:
                            q["slots"].append(s)
                elif meta.get("parts"):
                    close_cur_part(fr, fr[1])
                    self.stem_open = False  # 子题列表结束 → 题干区关闭
                elif meta.get("items_mode") or meta.get("list"):
                    close_cur_item(fr)
                    self._flush_list(fr, stack)
                    # 知识/结论卡的非列表直排内容 → body_html
                    card = meta.get("card")
                    if card is not None and not card.get("items_html") \
                            and content.strip():
                        ctx = InlineCtx(card["id"])
                        card["body_html"] = inline_to_html(content.strip(),
                                                          ctx)
                        for msg in ctx.issues:
                            self.issues.append("%s:%d: %s"
                                               % (self.sec, fr[1], msg))
                elif meta.get("probchain"):
                    self.finalize_question(fr[1])
                    self.in_probchain = False
                    self.stem_open = False
                    if self.between_buf:
                        self.pending_lead.append("".join(self.between_buf))
                        self.between_buf = []
                # dialogue/knowledge 卡：speakers/items 已增量写入

            elif g == 3:  # \item
                fr = top()
                if fr is None:
                    self.issues.append("%s:%d: \\item 出现在任何环境之外"
                                       % (self.sec, line_of(idx)))
                elif fr[3].get("probchain"):
                    self.finalize_question(line_of(idx))
                    q = {"id": None, "kind": "question", "section": self.sec,
                         "parts": [], "slots": [], "ansspace_count": 0,
                         "practice_mode": "guided"}
                    q["source_line"] = line_of(idx)
                    q["source"] = "%s:%d" % (self.sec, q["source_line"])
                    mm = meta_at_line.get(q["source_line"])
                    if mm and "qid" in mm:
                        q["qid"] = mm["qid"]
                        q["difficulty"] = mm.get("difficulty")
                    else:
                        for ln in range(q["source_line"] - 1,
                                        max(0, q["source_line"] - 8), -1):
                            mm2 = meta_at_line.get(ln)
                            if mm2 and "qid" in mm2:
                                q["qid"] = mm2["qid"]
                                q["difficulty"] = mm2.get("difficulty")
                                break
                    self._card_no += 1
                    q["id"] = "%s-%02d" % (self.sec, self._card_no)
                    q["seq"] = self.seq_base + len(self.cards) + 1
                    lead_parts = []
                    if self.pending_lead:
                        lead_parts.append("".join(self.pending_lead).strip())
                        self.pending_lead = []
                    if self.between_buf:
                        lead_parts.append("".join(self.between_buf).strip())
                        self.between_buf = []
                    lead = " ".join(x for x in lead_parts if x)
                    if lead:
                        q["lead_html"] = lead
                    self.stem_open = True
                    self.cards.append(q)
                    self.cur_question = q
                elif fr[3].get("parts"):
                    q = self.cur_question
                    close_cur_part(fr, line_of(idx))
                    if q is not None:
                        fr[3]["cur_part"] = {
                            "label": _part_label(len(q["parts"])),
                            "buf": [], "line": line_of(idx)}
                elif fr[3].get("items_mode") or fr[3].get("list"):
                    close_cur_item(fr)
                    fr[3]["cur_item"] = {"buf": [], "line": line_of(idx)}
                else:
                    route_append("\\item ")

            elif g == 4:  # \section*{title}
                self.finalize_question(line_of(idx))
                ctx = InlineCtx("sec")
                card = self.new_card("section", line_of(idx))
                card["title_html"] = inline_to_html(m.group(4).strip(), ctx)
                for msg in ctx.issues:
                    self.issues.append("%s:%d: %s"
                                       % (self.sec, line_of(idx), msg))
                self.section_card = card

            elif g == 5:  # \qtype{...}
                if self.cur_question is not None:
                    self.cur_question["qtype"] = m.group(5).strip()

            elif g == 6:  # \practicemode{...}
                if self.cur_question is not None:
                    self.cur_question["practice_mode"] = m.group(6).strip()

            elif g == 7:  # \speaker{R}{T}
                card = self.cur_dialogue
                k = m.end()
                role = body = ""
                ok1 = ok2 = False
                while k < len(text) and text[k] in " \t\n":
                    k += 1
                if k < len(text) and text[k] == "{":
                    k, role, ok1 = scan_brace(text, k)
                    while k < len(text) and text[k] in " \t\n":
                        k += 1
                    if k < len(text) and text[k] == "{":
                        k, body, ok2 = scan_brace(text, k)
                prev_end = max(prev_end, k)
                if not (ok1 and ok2):
                    self.issues.append("%s:%d: \\speaker 参数不完整"
                                       % (self.sec, line_of(idx)))
                    continue
                ctx = InlineCtx("dlg")
                html = inline_to_html(body.strip(), ctx)
                for msg in ctx.issues:
                    self.issues.append("%s:%d: %s"
                                       % (self.sec, line_of(idx), msg))
                entry = {"role_html": inline_to_html(role.strip(), ctx),
                         "html": html, "teacher_only": ctx.teacher_only}
                if card is not None:
                    card.setdefault("speakers", []).append(entry)
                else:
                    self.issues.append("%s:%d: \\speaker 出现在对话环境之外"
                                       % (self.sec, line_of(idx)))

        if prev_end < len(text):
            route_append(text[prev_end:])
        self.finalize_question(len(self.lines) or 0)
        for fr in stack:
            self.issues.append("%s:%d: 环境 %s 到文件末尾未闭合"
                               % (self.sec, fr[1], fr[0]))
        # 残留导语挂到节卡（或生成尾卡）
        if self.pending_lead:
            lead = "".join(self.pending_lead).strip()
            if lead:
                if self.section_card is not None and \
                        not self.section_card.get("lead_html"):
                    self.section_card["lead_html"] = lead
                else:
                    card = self.new_card("section", len(self.lines) or 0)
                    card["lead_html"] = lead
        return self.cards

    def _flush_list(self, fr, stack):
        """列表帧关闭：把 items 渲染为 <ol>，路由到父层文本流。"""
        meta = fr[3]
        items = meta.get("items", [])
        if not items:
            return
        html = '<ol class="qc-sublist">%s</ol>' % "".join(
            "<li>%s</li>" % it for it in items)
        card = meta.get("card")
        if meta.get("items_mode") and card is not None:
            card["items_html"].extend(items)
            return
        parent = stack[-1] if stack else None
        if parent is not None:
            pmeta = parent[3]
            if pmeta.get("cur_part") is not None:
                pmeta["cur_part"]["buf"].append(html)
            elif pmeta.get("cur_item") is not None:
                pmeta["cur_item"]["buf"].append(html)
            else:
                parent[2].append(html)
        else:
            self.pending_lead.append(html)


def _part_label(n):
    """二级子题标签：(a), (b), ...（与 LaTeX enumerate,2 一致）。"""
    label = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        label = chr(ord("a") + r) + label
    return "(%s)" % label


# ---------------------------------------------------------------------------
# 项目级装配
# ---------------------------------------------------------------------------
def sec_inputs(project):
    r"""按 main.tex 的 \input 顺序返回切片文件名；无 main.tex 时按编号排序。"""
    project = os.path.abspath(project)
    main = os.path.join(project, "main.tex")
    names = []
    if os.path.exists(main):
        with open(main, encoding="utf-8", errors="replace") as f:
            for m in RE_INPUT.finditer(strip_comment(f.read())):
                name = m.group(1).strip()
                if re.match(r"sec[^/\\]*(\.tex)?$", name):
                    names.append(name if name.endswith(".tex")
                                 else name + ".tex")
    if not names and os.path.isdir(project):
        names = sorted((f for f in os.listdir(project)
                        if re.fullmatch(r"sec.*\.tex", f)
                        and f != "sec_template.tex"),
                       key=lambda n: (int(re.search(r"(\d+)", n).group(1))
                                      if re.search(r"(\d+)", n) else 0, n))
    return [n for n in names if os.path.exists(os.path.join(project, n))]


def load_main_title(project):
    """从 config.yaml 读取 main_title（无则退化学案）。"""
    cfg = os.path.join(project, "config.yaml")
    if os.path.exists(cfg):
        with open(cfg, encoding="utf-8", errors="replace") as f:
            m = RE_MAIN_TITLE.search(f.read())
        if m:
            v = m.group(1).strip()
            if v[:1] in ('"', "'") and v.rfind(v[0]) > 0:
                v = v[1:v.rfind(v[0])]
            return v.strip()
    return "自学案"


def parse_project(project):
    """解析项目目录 → (deck dict, issues list)。"""
    project = os.path.abspath(project)
    secs = sec_inputs(project)
    issues = []
    if not secs:
        issues.append("未发现任何 sec*.tex（main.tex 未装配切片）")
    cards = []
    seq = 0
    for name in secs:
        with open(os.path.join(project, name), encoding="utf-8",
                  errors="replace") as f:
            lines = f.read().splitlines()
        parser = SecParser(os.path.splitext(name)[0], lines, seq_base=seq)
        sec_cards = parser.parse()
        seq += len(sec_cards)
        cards.extend(sec_cards)
        issues.extend(parser.issues)
    deck = {
        "schema": DECK_SCHEMA,
        "title": load_main_title(project),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_files": secs,
        "cards": cards,
    }
    deck["counts"] = _deck_counts(cards)
    return deck, issues


def _deck_counts(cards):
    return {
        "cards": len(cards),
        "sections": sum(1 for c in cards if c["kind"] == "section"),
        "questions": sum(1 for c in cards if c["kind"] == "question"),
        "dialogues": sum(1 for c in cards if c["kind"] == "dialogue"),
        "knowledge": sum(1 for c in cards
                         if c["kind"] in ("knowledge", "conclusion")),
        "slots": sum(len(c.get("slots", [])) for c in cards),
        "solutions": sum(1 for c in cards if c.get("solution_html")),
        "teacher_notes": sum(1 for c in cards if c.get("teacher_note_html")),
    }


def validate_deck(deck):
    """牌组结构校验（卡片级硬约束）；返回问题列表（空 = 通过）。

    规则与 check_numbering.py 的切片级检查对应：
      1. qid 全章唯一（题目卡缺失 qid 记问题——卡片身份依赖 qid）；
      2. 每个题目卡恰好一个 solution 与一个 teacherNote；
      3. 每个槽位至少 3 个选项、参考答案（归一化后）是选项之一、选项不重复；
      4. 卡片 id 唯一；question 卡必须出现在某个 section 卡之后。
    """
    problems = []
    qid_seen = {}
    id_seen = set()
    seen_section = False
    for c in deck.get("cards", []):
        cid = c.get("id")
        where = c.get("source", cid)
        if not cid or cid in id_seen:
            problems.append("卡片 id 缺失或重复: %r" % cid)
        id_seen.add(cid)
        if c["kind"] == "question":
            if not seen_section:
                problems.append("%s: 题目卡出现在任何节标题链 HTML 之前（缺 \\section*）"
                                % where)
            qid = c.get("qid")
            if not qid:
                problems.append("%s: 题目卡缺少 qid（卡片稳定身份缺失）" % where)
            elif qid in qid_seen:
                problems.append("%s: qid “%s” 与 %s 重复"
                                % (where, qid, qid_seen[qid]))
            else:
                qid_seen[qid] = where
            for field, label in (("solution_html", "参考解答"),
                                 ("teacher_note_html", "教学要点")):
                if not c.get(field):
                    problems.append("%s: 题目 %s 缺少%s"
                                    % (where, qid or cid, label))
            for s in c.get("slots", []):
                opts = s.get("options") or []
                if not s.get("answer"):
                    problems.append("%s: 槽位 %s 缺少参考答案"
                                    % (where, s.get("id")))
                    continue
                if len(opts) < 3:
                    problems.append("%s: 槽位 %s 只有 %d 个选项（至少 3 个）"
                                    % (where, s.get("id"), len(opts)))
                    continue
                norms = [norm_opt(o) for o in opts]
                if norm_opt(s["answer"]) not in norms:
                    problems.append("%s: 槽位 %s 的参考答案不在选项中"
                                    % (where, s.get("id")))
                dup = sorted({x for x in norms if norms.count(x) > 1})
                if dup:
                    problems.append("%s: 槽位 %s 的选项重复（%s）"
                                    % (where, s.get("id"), ", ".join(dup)))
        elif c["kind"] == "section":
            seen_section = True
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="TeX 源 → 卡片牌组解析")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--json", metavar="PATH", help="把牌组写入 PATH")
    ap.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.project):
        print("[tex2cards] 项目目录不存在: %s" % args.project)
        return 1
    deck, issues = parse_project(args.project)
    problems = validate_deck(deck)
    text = json.dumps(deck, ensure_ascii=False,
                      indent=2 if args.pretty else None)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
        print("[tex2cards] 牌组已写入 %s（%d 张卡片）"
              % (args.json, len(deck["cards"])))
    else:
        print(text)
    for msg in issues:
        print("[tex2cards] [提示] %s" % msg)
    for p in problems:
        print("[tex2cards] [问题] %s" % p)
    if problems:
        print("[tex2cards] 牌组校验未通过（%d 个问题）" % len(problems))
        return 1
    print("[tex2cards] 通过：%d 张卡片（题目 %d，槽位 %d）"
          % (deck["counts"]["cards"], deck["counts"]["questions"],
             deck["counts"]["slots"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
