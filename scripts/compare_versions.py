#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "3.0"
r"""
compare_versions.py — 学生版/教师版一致性与防泄露对比检查 (PDF / HTML 双引擎支持)

检查项:
  1. 泄露扫描（硬失败）：学生版文本中不得出现教师版专属标记（【解】、【证明】、参考解答、教学要点等）。
  2. 疑似数学答案泄露（警告/人工复核）：从 sec*.tex 源中提取 \fillin 答案，检查是否在选项外泄露。
  3. 双版一致性（硬失败）：
     - PDF 模式：源文件 solution/teacherNote 数量与 student.log/teacher.log 的 SUMMARY 计数交叉核对。
     - HTML 模式：两版卡片页集合一致，教师版可见解答数 == 源计数，学生版可见解答数 == 0。
  4. 文本量与页数统计（诊断信息）。

用法: python compare_versions.py [工作目录] [--format {both,pdf,html}] [选项]
退出码: 0=通过（含结构通过但有疑似答案项待复核）, 1=发现硬失败, 2=缺少依赖或输入。
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import render_html  # noqa: E402
import check_numbering  # noqa: E402

DEFAULT_FORBID_CN = [
    "【解】", "【证明】", "【答】", "参考解答", "参考答案",
    "教学要点", "易错分析", "评分", "教师用书", "无提示练习",
]
DEFAULT_FORBID_EN = re.compile(r"\b(Solution|Answer|Proof)\b")
RE_SUMMARY = re.compile(r"\[problem-driven\] SUMMARY solution=(\d+),\s*teacherNote=(\d+),\s*fillin=(\d+)")
RE_ENV_TITLE = re.compile(r"\\begin\{(?:solution|teacherNote)\}\[([^\]]+)\]")
RE_QID = re.compile(r"(?m)^\s*%\s*qid:\s*([^\s%|]+)")


def mask_comments(text):
    out = []
    for line in text.splitlines():
        i = 0
        while True:
            j = line.find("%", i)
            if j < 0:
                out.append(line)
                break
            k = j - 1
            while k >= 0 and line[k] == "\\":
                k -= 1
            if (j - 1 - k) % 2 == 1:
                i = j + 1
            else:
                out.append(line[:j] + " " * (len(line) - j))
                break
    return "\n".join(out)


def suspect_issue_id(qid_or_source, norm, kind="fillin-answer-leak-suspect"):
    raw = "2|%s|%s|%s" % (kind, qid_or_source, norm)
    return "CMP-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def extract_fillin_answers(text):
    answers = []
    pos = 0
    while True:
        idx = text.find(r"\fillin", pos)
        if idx < 0:
            break
        i = idx + len(r"\fillin")
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == "[":
            depth = 1
            j = i + 1
            while j < len(text) and depth > 0:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                j += 1
            if depth == 0:
                answers.append(text[i + 1:j - 1])
                pos = j
                continue
        pos = idx + len(r"\fillin")
    return answers


def locate_fillin_answers(raw_text):
    masked = mask_comments(raw_text)
    qid_marks = [(m.start(), m.group(1)) for m in RE_QID.finditer(raw_text)]
    answers = extract_fillin_answers(masked)
    out = []
    pos = 0
    for raw in answers:
        i = masked.find(raw, pos)
        if i < 0:
            i = masked.find(raw)
        qid = None
        if i >= 0:
            pos = i + len(raw)
            before = [q for off, q in qid_marks if off <= i]
            qid = before[-1] if before else None
        out.append((raw, qid))
    return out


def strip_comments(text):
    out = []
    for line in text.splitlines():
        i = 0
        while True:
            j = line.find("%", i)
            if j < 0:
                out.append(line)
                break
            k = j - 1
            while k >= 0 and line[k] == "\\":
                k -= 1
            if (j - 1 - k) % 2 == 1:
                i = j + 1
            else:
                out.append(line[:j])
                break
    return "\n".join(out)


def normalize_answer(ans):
    s = strip_comments(ans)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = re.sub(r"[\s${}\[\]()_^\\]", "", s)
    return s


def extract_bracket_arg(text, start):
    if start >= len(text) or text[start] != "[":
        return None, start
    depth = 1
    j = start + 1
    while j < len(text) and depth > 0:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
        j += 1
    if depth == 0:
        return text[start + 1:j - 1], j
    return None, start


def extract_brace_arg(text, start):
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 1
    j = start + 1
    while j < len(text) and depth > 0:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        j += 1
    if depth == 0:
        return text[start + 1:j - 1], j
    return None, start


def extract_fillin_entries(raw_text):
    text = mask_comments(raw_text)
    entries = []
    pos = 0
    while True:
        idx = text.find(r"\fillin", pos)
        if idx < 0:
            break
        i = idx + len(r"\fillin")
        while i < len(text) and text[i].isspace():
            i += 1
        ans_raw, i = extract_bracket_arg(text, i)
        while i < len(text) and text[i].isspace():
            i += 1
        w_raw, i = extract_brace_arg(text, i)
        while i < len(text) and text[i].isspace():
            i += 1
        opts_raw, i = extract_bracket_arg(text, i)
        entries.append((ans_raw, w_raw, opts_raw))
        pos = max(i, idx + len(r"\fillin"))
    return entries


def option_sequences(raw_text):
    entries = extract_fillin_entries(raw_text)
    seqs = []
    for ans_raw, _w, opts_raw in entries:
        if opts_raw:
            items = [re.sub(r"\s+", "", normalize_answer(x))
                     for x in check_numbering.split_options(opts_raw, "/") if x.strip()]
            if items:
                seq = "/".join(items)
                if len(seq) >= 2:
                    seqs.append(seq)
            # C2：单个选项只剥离"干扰项"，绝不剥离本空正确答案——
            # 否则与正确选项同形的泄露文本会被一并从页面抹除（防泄露盲区）。
            ans_norm = re.sub(r"\s+", "", normalize_answer(ans_raw or ""))
            for item in items:
                if len(item) >= 2 and item != ans_norm:
                    seqs.append(item)
    return seqs


def sec_sources(workdir):
    try:
        names = sorted(os.listdir(workdir))
    except OSError:
        return []
    return [n for n in names
            if re.fullmatch(r"sec.*\.tex", n) and n != "sec_template.tex"]


def find_hits(pages, needle):
    hits = []
    for pno, text in enumerate(pages, 1):
        idx = text.find(needle)
        if idx >= 0:
            start = max(0, idx - 20)
            end = min(len(text), idx + len(needle) + 20)
            hits.append((pno, text[start:end]))
    return hits


def find_hits_re(pages, pattern):
    hits = []
    for pno, text in enumerate(pages, 1):
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            hits.append((pno, text[start:end]))
    return hits


def count_visible_blocks(html_text, cls_fragment):
    blocks = re.findall(r'<div class="[^"]*%s[^"]*"([^>]*)>' % re.escape(cls_fragment), html_text)
    return sum(1 for attrs in blocks if "hidden" not in attrs)


# =============================================================================
#  PDF 检查器 (依赖 pdftotext)
# =============================================================================

def pdf_pages_text(pdf):
    r = subprocess.run(["pdftotext", pdf, "-"], capture_output=True)
    if r.returncode != 0:
        return None
    raw = r.stdout.decode("utf-8", errors="replace")
    pages = raw.split("\x0c")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def compare_pdf_version(workdir, args, report, src_texts, raw_texts, forbid):
    stu_pdf = os.path.join(workdir, "student.pdf")
    tea_pdf = os.path.join(workdir, "teacher.pdf")
    if not (os.path.exists(stu_pdf) and os.path.exists(tea_pdf)):
        return ["缺少 student.pdf 或 teacher.pdf"]

    report["pdf_sha256"] = {
        "student": evidence.sha256_file(stu_pdf),
        "teacher": evidence.sha256_file(tea_pdf)
    }

    if not shutil.which("pdftotext"):
        if args.diagnostic:
            print("[compare] [跳过] 缺少 pdftotext，跳过 PDF 文本提取对比")
            return []
        return ["系统未安装 pdftotext，无法执行 PDF 文本抽取对比"]

    stu_pages = pdf_pages_text(stu_pdf)
    tea_pages = pdf_pages_text(tea_pdf)
    if stu_pages is None or tea_pages is None:
        return ["pdftotext 抽取文本失败"]

    report["evidence"]["pdf_text_sha256"] = {
        "student": hashlib.sha256("\n".join(stu_pages).encode("utf-8")).hexdigest(),
        "teacher": hashlib.sha256("\n".join(tea_pages).encode("utf-8")).hexdigest()
    }

    problems = []

    # 1. 禁用词检查
    for w in forbid:
        hits = find_hits(stu_pages, w)
        for pno, ctx in hits:
            problems.append("学生版 PDF 第 %d 页出现禁用标记 “%s”: …%s…" % (pno, w, ctx))

    if "EN" not in args.allow:
        for pno, ctx in find_hits_re(stu_pages, DEFAULT_FORBID_EN):
            problems.append("学生版 PDF 第 %d 页出现英文教师标记: …%s…" % (pno, ctx))

    # 2. 答案泄漏检查
    if not args.no_answer_check:
        seen, answers = set(), []
        # C2：短答案不再静默跳过——单独记账，默认记 info（短串误报率高），
        # --strict-answers 下升级为 review 待复核，绝不不留痕迹。
        seen_short, short_answers = set(), []
        opt_seqs = set()
        for fname in src_texts:
            for raw, qid in locate_fillin_answers(raw_texts[fname]):
                norm = normalize_answer(raw)
                if norm and len(norm) < args.answer_min_len:
                    if norm not in seen_short:
                        seen_short.add(norm)
                        short_answers.append((raw.strip(), norm, qid, fname))
                    continue
                if len(norm) >= args.answer_min_len and norm not in seen:
                    seen.add(norm)
                    answers.append((raw.strip(), norm, qid, fname))
            opt_seqs.update(option_sequences(raw_texts[fname]))

        pages = [re.sub(r"\s+", "", p) for p in stu_pages]
        for seq in sorted(opt_seqs, key=len, reverse=True):
            pages = [p.replace(seq, "") for p in pages]

        for raw, norm, qid, fname in short_answers:
            report["issues"].append({
                "severity": "review" if args.strict_answers else "info",
                "issue_id": suspect_issue_id(qid or fname, norm, "pdf-short-answer-unchecked"),
                "kind": "pdf-short-answer-unchecked",
                "qid": qid,
                "location": None,
                "message": ("答案 “%s” 归一化后仅 %d 字符，低于 answer-min-len=%d，"
                            "未参与泄露全文比对（短串误报率高）；--strict-answers "
                            "可将其升级为待复核疑似项" % (raw[:30], len(norm), args.answer_min_len))
            })

        for raw, norm, qid, fname in answers:
            hits = find_hits(pages, norm)
            for pno, ctx in hits:
                msg = ("学生版 PDF 第 %d 页疑似泄露 \\fillin 答案 “%s” (需人工复核): …%s…"
                       % (pno, raw[:30], ctx[:40]))
                report["issues"].append({
                    "severity": "review",
                    "issue_id": suspect_issue_id(qid or fname, norm, "pdf-fillin-leak"),
                    "kind": "pdf-fillin-leak",
                    "qid": qid,
                    "location": {"page": pno, "context": re.sub(r"\s+", " ", ctx)[:80]},
                    "message": msg
                })
                break

    # 3. 日志 SUMMARY 计数交叉核对
    missing_evidence = []

    def parse_log(tname):
        lp = os.path.join(workdir, tname + ".log")
        if not os.path.exists(lp):
            return None
        with open(lp, encoding="utf-8", errors="replace") as f:
            m = RE_SUMMARY.search(f.read())
            return {"solution": int(m.group(1)), "teacherNote": int(m.group(2)), "fillin": int(m.group(3))} if m else None

    s_sum = parse_log("student")
    t_sum = parse_log("teacher")
    if s_sum is None:
        missing_evidence.append("student.log 缺失或不含 [problem-driven] SUMMARY 行")
    if t_sum is None:
        missing_evidence.append("teacher.log 缺失或不含 [problem-driven] SUMMARY 行")

    if missing_evidence:
        for msg in missing_evidence:
            print("[compare] [证据缺失] %s" % msg)
        if not args.diagnostic:
            msg = ("[compare] 计数核对所需证据缺失（%s），无法完成检查；"
                   "证据缺失不得按通过处理。若仅作诊断，请加 --diagnostic。"
                   % "；".join(missing_evidence))
            print(msg)
            report["status"] = "error"
            report["evidence"]["missing_evidence"] = missing_evidence
            report["issues"].append({"severity": "error", "message": msg})
            return problems, True
        else:
            print("[compare] --diagnostic 模式：已跳过源级计数与日志核对。")
            report["evidence"]["skipped"] = missing_evidence
    elif s_sum and t_sum:
        for k in ("solution", "teacherNote", "fillin"):
            if s_sum[k] != t_sum[k]:
                problems.append("PDF 日志双版 %s 计数不一致 (%d != %d)" % (k, s_sum[k], t_sum[k]))

    return problems, False


# =============================================================================
#  HTML 检查器
# =============================================================================

def compare_html_version(workdir, args, report, src_texts, raw_texts, forbid):
    stu_html = os.path.join(workdir, "student.html")
    tea_html = os.path.join(workdir, "teacher.html")
    if not (os.path.exists(stu_html) and os.path.exists(tea_html)):
        return ["缺少 student.html 或 teacher.html"]

    report["html_sha256"] = {
        "student": evidence.sha256_file(stu_html),
        "teacher": evidence.sha256_file(tea_html)
    }

    stu_pages_tuples = evidence.html_card_pages(stu_html)
    tea_pages_tuples = evidence.html_card_pages(tea_html)
    if stu_pages_tuples is None or tea_pages_tuples is None:
        return ["解析 student.html 或 teacher.html 失败"]

    stu_pages = [t for _cid, t in stu_pages_tuples]
    tea_pages = [t for _cid, t in tea_pages_tuples]

    problems = []

    # 1. 禁用词检查
    for w in forbid:
        hits = find_hits(stu_pages, w)
        for pno, ctx in hits:
            problems.append("学生版 HTML 第 %d 个卡片页出现禁用标记 “%s”: …%s…" % (pno, w, ctx))

    if "EN" not in args.allow:
        for pno, ctx in find_hits_re(stu_pages, DEFAULT_FORBID_EN):
            problems.append("学生版 HTML 第 %d 个卡片页出现英文教师标记: …%s…" % (pno, ctx))

    # 2. 答案泄漏检查
    if not args.no_answer_check:
        seen, answers = set(), []
        # C2：短答案不再静默跳过——单独记账，默认记 info（短串误报率高），
        # --strict-answers 下升级为 review 待复核，绝不不留痕迹。
        seen_short, short_answers = set(), []
        opt_seqs = set()
        for fname in src_texts:
            for raw, qid in locate_fillin_answers(raw_texts[fname]):
                norm = normalize_answer(raw)
                if norm and len(norm) < args.answer_min_len:
                    if norm not in seen_short:
                        seen_short.add(norm)
                        short_answers.append((raw.strip(), norm, qid, fname))
                    continue
                if len(norm) >= args.answer_min_len and norm not in seen:
                    seen.add(norm)
                    answers.append((raw.strip(), norm, qid, fname))
            opt_seqs.update(option_sequences(raw_texts[fname]))

        pages = [re.sub(r"\s+", "", p) for p in stu_pages]
        for seq in sorted(opt_seqs, key=len, reverse=True):
            pages = [p.replace(seq, "") for p in pages]

        for raw, norm, qid, fname in short_answers:
            report["issues"].append({
                "severity": "review" if args.strict_answers else "info",
                "issue_id": suspect_issue_id(qid or fname, norm, "html-short-answer-unchecked"),
                "kind": "html-short-answer-unchecked",
                "qid": qid,
                "location": None,
                "message": ("答案 “%s” 归一化后仅 %d 字符，低于 answer-min-len=%d，"
                            "未参与泄露全文比对（短串误报率高）；--strict-answers "
                            "可将其升级为待复核疑似项" % (raw[:30], len(norm), args.answer_min_len))
            })

        for raw, norm, qid, fname in answers:
            hits = find_hits(pages, norm)
            for pno, ctx in hits:
                msg = ("学生版 HTML 第 %d 个卡片页疑似泄露 \\fillin 答案 “%s” (需人工复核): …%s…"
                       % (pno, raw[:30], ctx[:40]))
                report["issues"].append({
                    "severity": "review",
                    "issue_id": suspect_issue_id(qid or fname, norm, "html-fillin-leak"),
                    "kind": "html-fillin-leak",
                    "qid": qid,
                    "location": {"page": pno, "context": re.sub(r"\s+", " ", ctx)[:80]},
                    "message": msg
                })
                break

    # 3. 卡片一致性核对
    n_sol_src = sum(len(re.findall(r"\\begin\{solution\}", t)) for t in src_texts.values())
    n_note_src = sum(len(re.findall(r"\\begin\{teacherNote\}", t)) for t in src_texts.values())
    stu_ids = [cid for cid, _ in stu_pages_tuples]
    tea_ids = [cid for cid, _ in tea_pages_tuples]
    if stu_ids != tea_ids:
        problems.append("HTML 双版卡片页不一致: 学生版 %d 页 / 教师版 %d 页" % (len(stu_ids), len(tea_ids)))

    with open(tea_html, encoding="utf-8", errors="replace") as f:
        t_raw = f.read()
    with open(stu_html, encoding="utf-8", errors="replace") as f:
        s_raw = f.read()

    tea_vis_sol = count_visible_blocks(t_raw, "qc-reveal qc-solution")
    stu_vis_sol = count_visible_blocks(s_raw, "qc-reveal qc-solution")
    tea_vis_note = count_visible_blocks(t_raw, "qc-teacher-note")
    stu_vis_note = count_visible_blocks(s_raw, "qc-teacher-note")

    if tea_vis_sol != n_sol_src:
        problems.append("教师版 HTML 可见参考解答 %d 与源 solution=%d 不一致" % (tea_vis_sol, n_sol_src))
    if stu_vis_sol != 0:
        problems.append("学生版 HTML 存在 %d 处可见参考解答（应收入隐藏区）" % stu_vis_sol)
    if stu_vis_note != 0:
        problems.append("学生版 HTML 存在 %d 处可见教学要点" % stu_vis_note)

    return problems


def main():
    ap = argparse.ArgumentParser(description="学生版/教师版一致性与防泄露对比检查")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--format", choices=["both", "pdf", "html"], default=None)
    ap.add_argument("--forbid", nargs="*", default=[])
    ap.add_argument("--allow", nargs="*", default=[])
    ap.add_argument("--answer-min-len", type=int, default=4)
    ap.add_argument("--no-answer-check", action="store_true")
    ap.add_argument("--strict-answers", action="store_true")
    ap.add_argument("--diagnostic", action="store_true")
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    fmt = args.format or render_html.load_config_value(workdir, "output_format", "both")
    fmt = fmt.strip().lower()
    if fmt not in ("pdf", "html", "both"):
        fmt = "both"

    manifest = evidence.collect_inputs(workdir)
    digest = evidence.content_digest(manifest)

    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "compare_versions.py",
        "tool_version": TOOL_VERSION,
        "scope": {"workdir": workdir, "format": fmt, "diagnostic": args.diagnostic},
        "input_digest": digest,
        "pdf_sha256": None,
        "html_sha256": None,
        "status": "pass",
        "issues": [],
        "evidence": {}
    }

    # 收集源文件
    src_texts = {}
    raw_texts = {}
    for name in sec_sources(workdir):
        with open(os.path.join(workdir, name), encoding="utf-8", errors="replace") as f:
            raw_texts[name] = f.read()
        src_texts[name] = strip_comments(raw_texts[name])

    forbid = [w for w in DEFAULT_FORBID_CN + args.forbid if w not in args.allow]
    for text in src_texts.values():
        for title in RE_ENV_TITLE.findall(text):
            bracketed = title if title.startswith("【") else f"【{title}】"
            if bracketed not in forbid and bracketed not in args.allow:
                forbid.append(bracketed)

    problems = []

    has_pdf = os.path.exists(os.path.join(workdir, "student.pdf")) and os.path.exists(os.path.join(workdir, "teacher.pdf"))
    has_html = os.path.exists(os.path.join(workdir, "student.html")) and os.path.exists(os.path.join(workdir, "teacher.html"))

    missing_evidence_fatal = False
    # C2：输出格式要求的产物缺失即证据缺失（旧实现在 both 下缺 PDF 时
    # 静默跳过全部 PDF 侧防泄露检查，与"证据缺失不得按通过处理"纪律相悖）。
    if fmt in ("pdf", "both") and not has_pdf:
        problems.append("输出格式为 %s，但缺少 student.pdf/teacher.pdf，"
                        "PDF 侧防泄露检查无法执行（证据缺失不得按通过处理）" % fmt)
    if fmt in ("html", "both") and not has_html:
        problems.append("输出格式为 %s，但缺少 student.html/teacher.html，"
                        "HTML 侧防泄露检查无法执行（证据缺失不得按通过处理）" % fmt)
    if not has_pdf and not has_html:
        problems.append("缺少构建产物（student.pdf/teacher.pdf 或 student.html/teacher.html），请先完成构建。")
    else:
        # 1. 运行 PDF 对比
        if fmt in ("pdf", "both") and has_pdf:
            p_res, has_missing = compare_pdf_version(workdir, args, report, src_texts, raw_texts, forbid)
            problems.extend(p_res)
            if has_missing:
                missing_evidence_fatal = True

        # 2. 运行 HTML 对比
        if fmt in ("html", "both") and has_html:
            h_res = compare_html_version(workdir, args, report, src_texts, raw_texts, forbid)
            problems.extend(h_res)

    for p in problems:
        print("[compare] [错误] %s" % p)
        report["issues"].append({"severity": "error", "message": p})

    if missing_evidence_fatal:
        if args.json:
            evidence.write_report(args.json, report)
        return 2

    # 判定状态
    has_error = bool(problems)
    has_review = any(i.get("severity") == "review" for i in report["issues"])
    if args.strict_answers and has_review:
        has_error = True

    if has_error:
        report["status"] = "fail"
    elif has_review:
        report["status"] = "review"
    else:
        report["status"] = "pass"

    print("[compare] 对比结果: %s (错误 %d, 待复核 %d)"
          % (report["status"].upper(), len(problems),
             sum(1 for i in report["issues"] if i.get("severity") == "review")))

    if args.json:
        evidence.write_report(args.json, report)
        print("[compare] 结构化报告已写入 %s" % args.json)

    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
