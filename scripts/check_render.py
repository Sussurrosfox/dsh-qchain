#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.3"
r"""
check_render.py — 渲染质量门禁（防“乱码 / 选项空白”复发，qchain 专用）

背景（2024 缺陷复盘，GR.4 正规子群与商群）：
  交付后用户报告「HTML 出现大量乱码、PDF 选项空白」。两项缺陷此前
  **全部**结构门禁都显示 PASS——因为它们只校验结构契约（卡片数、槽位
  数、版本纪律），从不校验“渲染出来的字是不是字”。根因：

  * PDF：\fillin 的答案/选项参数被 \ensuremath{} 强制进入数学模式，
    汉字在数学字体中无字形（"Missing character"）→ 整段留白。
    构建日志早有 300 处缺字告警，却因只判 Overfull 而被放行。
  * HTML：① 子列表 <ol> 已渲染 HTML 回灌文本缓冲区后被二次转义，
    页面上出现 "&lt;span class="qc-math"&gt;" 可见乱码；② \fillin
    落在 $...$ 内被整体当数学段；③ tabular/center 无 HTML 映射被压平；
    ④ 数学公式从 jsDelivr CDN 加载，内网/断网时整段以 $...$ 源码显示。

本门禁把上述四类“渲染级”失败变成确定性断言（无需浏览器、无网络）：

  H1 双重转义：HTML 可见区不得出现 &amp;lt; / &amp;amp; / &amp;quot;
  H2 源码泄漏：HTML 不得残留 \qrawhtml、\begin{tabular}、\hline、
     \begin{center} 等未转换记号；v1.1 追加 TikZ 泄漏检测
     （\begin{tikzcd}/\begin{tikzpicture}/\usetikzlibrary/\arrow[ 等）——
     TikZ 必须栅格化为 PNG 经 \includegraphics 进 sec*.tex，HTML 中出现
     原始 tikz 源码永远是泄漏，命中即 FAIL
  H3 数学段完整性：每个 <span class="qc-math"> 内 $ 定界符成对且非空
  H4 选项汉字守恒：sec*.tex 中 \fillin 选项/答案里的汉字必须原样出现在
     HTML 选项按钮与 PDF 文本中（防止被 \ensuremath 吞掉或渲染留白）
  H5 KaTeX 自足：HTML 不得引用外网 CDN；katex/ 本地资源齐备
  H6 数学宏白名单：.qc-math 内的 \命令必须落在 KaTeX 支持集合内
     （否则浏览器里 KaTeX 报错、公式变红/不渲染）
  H7 cards.json 槽位守恒（v1.1 新增）：tex2cards 牌组 cards.json 的槽位
     总数必须 == sec*.tex 的 \fillin 数（RENDER_SLOT_LOST），选项按钮总数
     必须 == 源级选项数（RENDER_OPTION_LOST，精确相等，比 H4 的 < 更严）；
     学生/教师版 HTML 的 qc-slot span 数亦须与源级一致；cards.json 缺失或
     schema 不符预期即 problem（门禁输入不完整，不许静默跳过）
  P1 缺字：*.log 中 "Missing character" 必须为 0；v1.1 增强为按字符聚合
     报告（collections.Counter top 10，如 必×12, 然×8；未识别字符计入
     （未识别）），阈值不变但报告可操作
  P2 不可见字形：PDF 抽取文本不得含 U+FFFD / U+FFFF
  P3 空选项组：PDF 文本不得出现空的 （） 对

用法: python check_render.py [工作目录] [--json PATH]
退出码: 0=通过, 1=发现问题, 2=缺少输入。
"""
import argparse
import collections
import io
import json
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import math_fragments  # noqa: E402

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tex2cards  # noqa: E402
    _HAVE_TEX2CARDS = True
except Exception:  # pragma: no cover - 退化路径仍可跑 HTML/PDF 静态检查
    tex2cards = None
    _HAVE_TEX2CARDS = False

try:
    import check_numbering  # noqa: E402
    _HAVE_NUMBERING = True
except Exception:  # pragma: no cover - 退化路径仍可跑 HTML/PDF 静态检查
    check_numbering = None
    _HAVE_NUMBERING = False

try:
    import fitz  # PyMuPDF
    _HAVE_FITZ = True
except Exception:  # pragma: no cover
    fitz = None
    _HAVE_FITZ = False

#: KaTeX 0.16 支持的函数/符号（覆盖题链常用子集 + 本模版 macros 映射）
KATEX_MACROS = {
    "linf", "bm", "quotgrp", "quotring",
    "text", "mathrm", "mathit", "mathbf", "mathbb", "mathcal", "mathfrak",
    "mathsf", "mathtt", "operatorname", "boldsymbol", "bm",
    "frac", "dfrac", "tfrac", "binom", "sqrt", "left", "right",
    "cdot", "cdots", "ldots", "dots", "vdots", "ddots",
    "le", "leq", "ge", "geq", "ne", "neq", "equiv", "sim", "simeq",
    "subset", "subseteq", "supset", "supseteq", "subsetneq", "supsetneq",
    "notin", "propto", "cong", "approx", "asymp",
    "bmod", "iff", "implies", "impliedby", "mapsto",
    "rtimes", "ltimes", "trianglelefteq", "triangleright",
    "lhd", "rhd", "unlhd", "unrhd",
    "colon", "mid", "backslash", "setminus",
    "in", "ni", "notin", "propto", "cong", "approx", "asymp",
    "cap", "cup", "setminus", "emptyset", "varnothing",
    "wedge", "vee", "neg", "land", "lor", "forall", "exists",
    "mapsto", "to", "rightarrow", "leftarrow", "Rightarrow", "Leftarrow",
    "leftrightarrow", "Leftrightarrow", "Rightarrow", "Rightarrow",
    "xrightarrow", "hookrightarrow", "circ", "bullet", "star", "ast",
    "pm", "mp", "times", "div", "ast", "oplus", "ominus", "otimes",
    "bigcirc", "angle", "perp", "parallel", "mid", "nmid",
    "prime", "deg", "dim", "ker", "hom", "arg", "det", "gcd", "lcm",
    "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "ln", "log", "exp", "lim", "min", "max", "sup", "inf",
    "sum", "prod", "int", "iint", "oint", "bigcup", "bigcap", "bigoplus",
    "infty", "partial", "nabla", "ell", "imath", "jmath", "aleph",
    "hbar", "Re", "Im", "wp", "surd", "triangle", "square", "diamond",
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta",
    "eta", "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu",
    "xi", "pi", "rho", "sigma", "tau", "upsilon", "phi", "varphi",
    "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi",
    "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega",
    "overline", "underline", "widehat", "widetilde", "vec", "hat", "bar",
    "dot", "ddot", "tilde", "check", "acute", "grave", "breve",
    "overleftrightarrow", "underbrace", "overbrace", "substack",
    "mathbb{R}", "mathbb{Z}", "mathbb{N}", "mathbb{Q}", "mathbb{C}",
    "langle", "rangle", "lfloor", "rfloor", "lceil", "rceil",
    "begin", "end", "quad", "qquad", "hspace", "phantom", "boxed",
    "color", "displaystyle", "textstyle", "scriptstyle", "limits",
    "nolimits", "nonumber", "notag", "tag", "label", "mathbb",
    "DeclareMathOperator", "operatorname",
    "overline", "underline", "widehat", "widetilde",
    "overset", "underset", "stackrel", "substack",
    # --- KaTeX 定界符族（已在随包 katex.min.js 中逐一核验存在）---
    # 此前缺失导致 \lvert/\rvert/\vert/\Vert/\pmod/\not/\bigsqcup 等
    # 标准记号被误判为“KaTeX 不支持”而反复返修（GR.5 假阳性事故）。
    "lvert", "rvert", "lVert", "rVert", "vert", "Vert",
    "lgroup", "rgroup", "lbrace", "rbrace", "lbrack", "rbrack",
    # --- KaTeX 取模族 ---
    "pmod", "pod", "mod", "bmod",
    # --- KaTeX 否定前缀（\not\equiv / \not\mid 等）---
    "not",
    # --- KaTeX 大型算子（已在随包中核验）---
    "bigsqcup", "bigvee", "bigwedge", "bigodot", "bigotimes", "biguplus",
}
# 白名单判定：剥掉反斜杠后的命令名（可能带 * 后缀）
KATEX_EXTRA = {"qmathlit", "qrawhtml"}

RE_BUTTON = re.compile(
    r'<button[^>]*class="qc-option"[^>]*>(.*?)</button>', re.S)
RE_TEXCMD = re.compile(r"\\([a-zA-Z]+)\*?")
RE_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
RE_FILLIN = re.compile(r"\\fillin")
RE_MISSING_CHAR = re.compile(r"Missing character")
# P1 聚合：日志行形如
#   Missing character: There is no 必 (U+5FC5) in font ...
# 提取缺失字符本身（汉字/符号），按字符 Counter 聚合出可操作报告
RE_MISSING_DETAIL = re.compile(
    r"Missing character: There is no (.) \(U\+[0-9A-Fa-f]+\)")
RE_SUMMARY = re.compile(r"SUMMARY solution=(\d+),\s*teacherNote=(\d+),"
                        r"\s*fillin=(\d+)")
CDN_RE = re.compile(r"(cdn\.jsdelivr|unpkg\.com|cdnjs\.|"
                    r"https?://[^\"'\s]*katex)", re.I)
RESIDUAL = ("\\qrawhtml", "\\qmathlit", "\\begin{tabular}", "\\hline",
            "\\begin{center}", "\\end{tabular}", "\\end{center}",
            # v1.1 H2 扩充：TikZ 必须栅格化为 PNG（\includegraphics）后才能
            # 进 sec*.tex；HTML 中出现原始 tikz 源码永远是泄漏，命中即 FAIL
            "\\begin{tikzcd}", "\\end{tikzcd}",
            "\\begin{tikzpicture}", "\\end{tikzpicture}",
            "\\usetikzlibrary", "\\arrow[")
# H7：HTML 渲染出的槽位 span（每张学生/教师卡每槽位恰一个）
RE_SLOT_SPAN = re.compile(r'class="qc-slot"')


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def scan_optional(text, pos):
    r"""扫描 \fillin 的可选/必选参数组，返回 (答案, 宽度, 选项串, 结束位)。

    形如 \fillin[答案]{宽度}[选项1/选项2/选项3]；缺省组返回 None。
    借用 tex2cards.scan_brace / scan_bracket 以正确处理嵌套花括号。
    """
    if not _HAVE_TEX2CARDS:
        return None, None, None, pos
    k = pos
    ans = width = opts = None
    while k < len(text) and text[k] in " \t\n\r":
        k += 1
    if k < len(text) and text[k] == "[":
        k2, val, ok = tex2cards.scan_bracket(text, k)
        if ok:
            ans, k = val, k2
    while k < len(text) and text[k] in " \t\n\r":
        k += 1
    if k < len(text) and text[k] == "{":
        k2, val, ok = tex2cards.scan_brace(text, k)
        if ok:
            width, k = val, k2
    while k < len(text) and text[k] in " \t\n\r":
        k += 1
    if k < len(text) and text[k] == "[":
        k2, val, ok = tex2cards.scan_bracket(text, k)
        if ok:
            opts, k = val, k2
    return ans, width, opts, k


def collect_sec_fillin(workdir):
    r"""从 sec*.tex 汇总 \fillin 的可选参数（答案 + 选项）。"""
    ans_all, opt_all, n = [], [], 0
    if not (_HAVE_TEX2CARDS and _HAVE_NUMBERING):
        return ans_all, opt_all, n
    for name in sorted(os.listdir(workdir)):
        if not re.fullmatch(r"sec[0-9A-Za-z_-]*\.tex", name):
            continue
        if name == "sec_template.tex":
            continue
        text = read(os.path.join(workdir, name)) or ""
        for m in RE_FILLIN.finditer(text):
            ans, _w, opts, _k = scan_optional(text, m.end())
            n += 1
            if ans:
                ans_all.append(ans)
            if opts:
                # 与 tex2cards._convert_fillin 同口径：顶层 / 切分，
                # \frac{1}{2}、$...$ 内的斜杠不是选项分隔
                opt_all.extend(p for p in check_numbering.split_options(
                    opts, "/") if p.strip())
    return ans_all, opt_all, n


def cards_slot_stats(workdir):
    r"""H7：读 cards.json 统计牌组槽位与选项按钮总数。

    cards.json 是 tex2cards 产出的单一牌组（schema qchain-html-deck/1），
    学生版与教师版 HTML 均由它渲染，故对账在牌组层做一次即可覆盖两版。
    返回 (n_slots, n_options, err)；err 非 None 表示门禁输入不完整
    （文件缺失 / 非法 JSON / schema 不符预期），调用方必须记 problem，
    不许静默跳过。
    """
    raw = read(os.path.join(workdir, "cards.json"))
    if raw is None:
        return None, None, ("cards.json 缺失（tex2cards 牌组产物，H7 槽位"
                            "守恒对账输入不完整）")
    try:
        deck = json.loads(raw)
    except ValueError as exc:
        return None, None, "cards.json 不是合法 JSON: %s" % exc
    cards = deck.get("cards") if isinstance(deck, dict) else None
    if not isinstance(cards, list):
        return None, None, "cards.json schema 不符预期（缺少 cards 数组）"
    n_slots, n_opts, bad = 0, 0, 0
    for card in cards:
        slots = card.get("slots") if isinstance(card, dict) else None
        if slots is None:
            continue  # section/dialogue/knowledge 等卡本就没有 slots
        if not isinstance(slots, list):
            bad += 1
            continue
        for s in slots:
            n_slots += 1
            opts = s.get("options") if isinstance(s, dict) else None
            if isinstance(opts, list):
                n_opts += len(opts)
    if bad:
        return None, None, ("cards.json schema 不符预期（%d 张卡片的 slots "
                            "字段非数组）" % bad)
    return n_slots, n_opts, None


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
          .replace("&#39;", "'"))
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description="渲染质量门禁（乱码/空白防复发）")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查结果")
    ap.add_argument("--format", dest="fmt", default="auto",
                    choices=["auto", "both", "pdf", "html"],
                    help="交付格式：按格式跳过未构建的产物类别"
                         "（默认 auto=按盘上实际存在的产物判定）")
    args = ap.parse_args(argv)
    workdir = os.path.abspath(args.workdir)
    fmt = args.fmt

    problems = []      # 硬失败（status=fail）
    warnings = []      # 软告警（status=review）
    notes = []         # 说明性跳过（不影响判定，仅留痕）
    evidence_blob = {"format": fmt}

    ans_sec, opts_sec, n_fillin = collect_sec_fillin(workdir)
    cjk_fillin = set(RE_CJK.findall("".join(ans_sec + opts_sec)))
    evidence_blob["sec_fillin_count"] = n_fillin
    evidence_blob["sec_option_count"] = len(opts_sec)
    evidence_blob["cjk_in_fillin"] = "".join(sorted(cjk_fillin))

    # 产物类别存在性：--format pdf 的项目本就不产 HTML，不得以“缺少
    # *.html”制造假失败（反之亦然）；单侧缺失才是真问题。
    have_html = any(os.path.exists(os.path.join(workdir, v + ".html"))
                    for v in ("student", "teacher"))
    have_pdf = any(os.path.exists(os.path.join(workdir, v + ".pdf"))
                   for v in ("student", "teacher"))
    # 显式要求 HTML（--format html/both）时，缺少 student/teacher.html 必须
    # 失败（逐版本循环里记“缺少 X.html”），不得再因“盘上没有 HTML”静默跳过；
    # 仅 auto 下按盘上实际产物判定。pdf-only 项目不检查 HTML，也不需要 node。
    check_html = fmt in ("both", "html") or (fmt == "auto" and have_html)
    check_pdf = have_pdf and fmt in ("auto", "both", "pdf")
    evidence_blob["check_html"] = check_html
    evidence_blob["check_pdf"] = check_pdf
    if not check_html and not have_html:
        notes.append("未发现 *.html（format=%s），跳过 HTML 检查" % fmt)
    if not check_pdf and not have_pdf:
        notes.append("未发现 *.pdf（format=%s），跳过 PDF 检查" % fmt)

    # ---------------- HTML 检查 ----------------
    html_cjk_buttons = set()
    html_buttons_total = 0
    math_fragments_all = []
    for version in ("student", "teacher"):
        p = os.path.join(workdir, version + ".html")
        html = read(p)
        if html is None:
            if check_html:
                problems.append("缺少 %s.html" % version)
            continue
        name = version + ".html"

        # H1 双重转义（只查可见正文，head 内联样式不含实体）
        body = html.split("</head>", 1)[-1]
        for bad in ("&amp;lt;", "&amp;amp;", "&amp;quot;", "&amp;gt;"):
            if bad in body:
                problems.append("%s: 可见区存在双重转义 %s（HTML 被二次转义，"
                                "页面将显示 &lt;span class=\"qc-math\"&gt; "
                                "这类乱码）" % (name, bad))

        # H2 源码泄漏
        for tok in RESIDUAL:
            if tok in body:
                problems.append("%s: 残留未转换记号 %r（center/tabular 或占位符"
                                "未被转换为 HTML）" % (name, tok))

        # H3 数学段完整性 + H6 宏白名单
        # 按 class token 提取全部最外层 qc-math（含 qc-math qc-math-display 等
        # 类变体与跨行/嵌套内容），不再用只认精确 class 的正则。
        spans = math_fragments.extract_math_fragments(html, source=name)
        math_fragments_all.extend(spans)
        empty_span = 0
        odd_dollar = 0
        bad_macros = set()
        for fr in spans:
            s = fr.text
            if s.count("$") % 2 != 0:
                odd_dollar += 1
            if not s.strip():
                empty_span += 1
            for cmd in RE_TEXCMD.findall(s):
                if cmd not in KATEX_MACROS and cmd not in KATEX_EXTRA:
                    bad_macros.add("\\" + cmd)
        if odd_dollar:
            problems.append("%s: %d 个 .qc-math 段 $ 定界符不成对（数学段被"
                            "截断，KaTeX 将报错）" % (name, odd_dollar))
        if empty_span:
            problems.append("%s: %d 个 .qc-math 段为空" % (name, empty_span))
        if bad_macros:
            problems.append("%s: .qc-math 含 KaTeX 不支持的宏 %s"
                            % (name, ", ".join(sorted(bad_macros)[:12])))
        evidence_blob.setdefault("math_spans", {})[version] = len(spans)

        # H4 选项汉字守恒（HTML 侧）
        btns = RE_BUTTON.findall(html)
        html_buttons_total += len(btns)
        for b in btns:
            html_cjk_buttons |= set(RE_CJK.findall(strip_tags(b)))
        if opts_sec and len(btns) < len(opts_sec):
            problems.append("%s: 选项按钮 %d 个少于源级选项 %d 个（选项丢失）"
                            % (name, len(btns), len(opts_sec)))

        # H7b 渲染层槽位守恒：本版 HTML 的 qc-slot span 数须 == 源级
        # \fillin 数（事故形态：tex2cards/render 丢编号列表槽位，HTML
        # 实际 42 槽 vs PDF 66 槽，粗粒度检查抓不住）
        if _HAVE_TEX2CARDS and _HAVE_NUMBERING and n_fillin:
            n_spans = len(RE_SLOT_SPAN.findall(html))
            evidence_blob.setdefault("html_slot_spans", {})[version] = n_spans
            if n_spans != n_fillin:
                problems.append(
                    "RENDER_SLOT_LOST: %s 槽位 span %d 个 != sec*.tex "
                    "\\fillin %d 个（槽位丢失，学生将看不到填空/选项位）"
                    % (name, n_spans, n_fillin))

        # H5 KaTeX 自足
        m = CDN_RE.search(html)
        if m:
            problems.append("%s: 仍引用外网 CDN %r（内网/断网时公式整段以 "
                            "$...$ 源码显示，表现为“乱码”）"
                            % (name, m.group(0)[:60]))
        kdir = os.path.join(os.path.dirname(os.path.abspath(p)), "katex")
        for need in ("katex.min.js", "auto-render.min.js", "katex.min.css"):
            if not os.path.exists(os.path.join(kdir, need)):
                problems.append("%s: 缺少本地 KaTeX 资源 katex/%s"
                                % (name, need))

        # H2b 图片资产
        for src in re.findall(r'src="(assets/[^"]+)"', html):
            if not os.path.exists(os.path.join(os.path.dirname(
                    os.path.abspath(p)), src)):
                problems.append("%s: 引用图片资产缺失 %s" % (name, src))

    evidence_blob["html_option_buttons"] = html_buttons_total

    # ---------------- H3b 严格数学校验（真实 KaTeX，一次批量） ----------------
    # 两版 HTML 的全部 qc-math 一次送入随包 KaTeX（throwOnError=true、
    # trust=false、配置与浏览器共用 math-config.json）。引擎不可用 = 失败，
    # 绝不当作通过。
    if check_html and math_fragments_all:
        mres = math_fragments.validate_fragments(math_fragments_all)
        eng = mres.get("engine") or {}
        evidence_blob["math_strict"] = {
            "status": mres["status"], "total": mres["total"],
            "checked": mres["checked"], "failed": len(mres["failures"]),
            "engine_version": eng.get("version"),
            "engine": eng.get("name"),
            "failures": mres["failures"][:50],
            "env_error": mres.get("env_error"),
        }
        if mres["status"] == "env_error":
            problems.append("MATH_ENGINE_UNAVAILABLE: 无法完成严格公式校验"
                            "（%s）；引擎不可用不得视为通过" % mres["env_error"])
        for f in mres["failures"][:20]:
            problems.append(
                "%s: KaTeX 不支持的宏/语法（严格校验失败）: 公式「%s」— %s%s"
                % (f["location"], f["formula"], f["message"].replace("\n", " "),
                   "" if f["position"] is None
                   else "（位置 %s）" % f["position"]))
        if len(mres["failures"]) > 20:
            problems.append("KaTeX 严格校验另有 %d 处失败未逐条列出（见 --json "
                            "math_strict.failures）" % (len(mres["failures"]) - 20))
    elif check_html:
        evidence_blob["math_strict"] = {
            "status": "ok", "total": 0, "checked": 0, "failed": 0,
            "engine_version": None, "engine": None, "failures": [],
            "env_error": None}
    if check_html and opts_sec and (cjk_fillin - html_cjk_buttons):
        problems.append("HTML 选项丢失汉字：%s（\\ensuremath 强制数学模式会吞掉"
                        "汉字，须以 \\text{} 包裹）"
                        % "".join(sorted(cjk_fillin - html_cjk_buttons)))

    # ---------------- H7 cards.json 槽位守恒 ----------------
    # 牌组层对账：cards.json 是学生/教师两版 HTML 的共同数据源，槽位数必须
    # == sec*.tex 的 \fillin 数（一个不能丢），选项按钮总数必须 ==
    # 源级选项数（精确相等，比 H4 的 < 更严）。cards.json 缺失或 schema
    # 不符预期 = 门禁输入不完整，记 problem，不许静默跳过。
    if check_html:
        n_slots_deck, n_opts_deck, deck_err = cards_slot_stats(workdir)
        if deck_err is not None:
            problems.append(deck_err)
        else:
            evidence_blob["cards_slots"] = n_slots_deck
            evidence_blob["cards_options"] = n_opts_deck
            if _HAVE_TEX2CARDS and _HAVE_NUMBERING:
                if n_slots_deck != n_fillin:
                    problems.append(
                        "RENDER_SLOT_LOST: cards.json 槽位 %d 个 != sec*.tex "
                        "\\fillin %d 个（编号列表等处槽位被 tex2cards 丢失）"
                        % (n_slots_deck, n_fillin))
                if n_opts_deck != len(opts_sec):
                    problems.append(
                        "RENDER_OPTION_LOST: cards.json 选项按钮 %d 个 != "
                        "sec*.tex 源级选项 %d 个（选项精确守恒被破坏）"
                        % (n_opts_deck, len(opts_sec)))
            else:
                notes.append("tex2cards/check_numbering 不可用，H7 仅登记 "
                             "cards.json 计数（槽位 %d / 选项 %d），跳过对账"
                             % (n_slots_deck, n_opts_deck))

    # ---------------- PDF 检查 ----------------
    pdf_cjk = set()
    if check_pdf:
        for version in ("student", "teacher"):
            log = read(os.path.join(workdir, version + ".log"))
            if log is None:
                warnings.append("缺少 %s.log（跳过 PDF 缺字检查）" % version)
            else:
                # P1 增强：按缺失字符聚合（行形如
                # "Missing character: There is no 必 (U+5FC5) in font ..."），
                # top 10 字符写入 problem 消息与 evidence，报告可操作；
                # 判定阈值不变（>0 即 problem）
                miss = collections.Counter()
                for line in log.splitlines():
                    if RE_MISSING_CHAR.search(line):
                        m = RE_MISSING_DETAIL.search(line)
                        miss[m.group(1) if m else "（未识别）"] += 1
                n_miss = sum(miss.values())
                if n_miss:
                    top = ", ".join("%s×%d" % (ch, cnt)
                                    for ch, cnt in miss.most_common(10))
                    problems.append("%s.log: 存在 %d 处 Missing character（汉字进入"
                                    "数学字体无字形，页面对应位置留白）；缺失字符 "
                                    "top%d: %s"
                                    % (version, n_miss,
                                       min(10, len(miss)), top))
                    evidence_blob.setdefault("missing_chars", {})[version] = \
                        dict(miss.most_common(10))
                m = RE_SUMMARY.search(log)
                if m:
                    evidence_blob.setdefault("log_summary", {})[version] = {
                        "solution": int(m.group(1)),
                        "teacherNote": int(m.group(2)),
                        "fillin": int(m.group(3))}
                else:
                    warnings.append("%s.log: 未解析到 SUMMARY 行" % version)

            pdf = os.path.join(workdir, version + ".pdf")
            if not os.path.exists(pdf):
                warnings.append("缺少 %s.pdf（跳过 PDF 文本检查）" % version)
                continue
            if _HAVE_FITZ:
                try:
                    doc = fitz.open(pdf)
                    text = "\n".join(pg.get_text() for pg in doc)
                    pages = doc.page_count
                    doc.close()
                except Exception as exc:  # pragma: no cover
                    problems.append("%s.pdf 无法解析: %s" % (version, exc))
                    continue
                bad = sum(text.count(c) for c in ("\ufffd", "\uffff"))
                if bad:
                    problems.append("%s.pdf: 抽取文本含 %d 个不可见字形 U+FFFD/"
                                    "U+FFFF（渲染失败占位符）" % (version, bad))
                empty_paren = len(re.findall(r"（\s*）", text))
                if empty_paren:
                    problems.append("%s.pdf: 存在 %d 处空选项组 （）（选项/答案成片"
                                    "空白）" % (version, empty_paren))
                pdf_cjk |= set(RE_CJK.findall(text))
                evidence_blob.setdefault("pdf", {})[version] = {
                    "pages": pages, "chars": len(text), "bad_glyphs": bad,
                    "empty_option_groups": empty_paren}
            else:
                # 环境缺 PyMuPDF：降级为说明（P1 日志级缺字检查仍然生效），
                # 不因运行环境缺失判定内容不合格。
                notes.append("PyMuPDF 不可用，跳过 %s.pdf 文本级检查"
                             "（P1 日志缺字检查仍生效）" % version)

        if cjk_fillin and (cjk_fillin - pdf_cjk):
            problems.append("PDF 文本丢失 \\fillin 汉字：%s（答案/选项空白）"
                            % "".join(sorted(cjk_fillin - pdf_cjk)))

    report = {"schema_version": evidence.SCHEMA_VERSION,
              "tool": "check_render.py", "tool_version": TOOL_VERSION,
              "scope": {"workdir": workdir},
              "input_digest": evidence.content_digest(
                  evidence.collect_inputs(workdir)),
              "pdf_sha256": {
                  v: evidence.sha256_file(os.path.join(workdir,
                                                       v + ".pdf"))
                  for v in ("student", "teacher")},
              "html_sha256": {
                  v: evidence.sha256_file(os.path.join(workdir,
                                                       v + ".html"))
                  for v in ("student", "teacher")},
              "status": "pass", "issues": [], "evidence": evidence_blob}

    for p in problems:
        print("[check_render] [问题] %s" % p)
    for w in warnings:
        print("[check_render] [警告] %s" % w)
    for n in notes:
        print("[check_render] [说明] %s" % n)
    if problems:
        print("[check_render] 发现 %d 个渲染质量问题 (FAIL)" % len(problems))
        report["status"] = "fail"
        report["issues"] = [{"severity": "error", "message": p}
                            for p in problems]
        report["issues"] += [{"severity": "warning", "message": w}
                             for w in warnings]
        report["issues"] += [{"severity": "info", "message": n}
                             for n in notes]
        rc = 1
    elif warnings:
        print("[check_render] 通过（含 %d 条警告）" % len(warnings))
        report["status"] = "review"
        report["issues"] = [{"severity": "warning", "message": w}
                            for w in warnings]
        rc = 0
    else:
        scope = []
        if check_html:
            scope.append("HTML 双重转义/源码泄漏/数学段完整性/选项汉字守恒/"
                         "KaTeX 自足")
        if check_pdf:
            scope.append("PDF 缺字/不可见字形/空选项组")
        print("[check_render] 通过：%s。" % "，".join(scope))
        report["issues"] = [{"severity": "info", "message": n}
                            for n in notes]
        rc = 0
    if args.json:
        evidence.write_report(args.json, report)
        print("[check_render] 结构化报告已写入 %s" % args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
