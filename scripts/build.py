#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "3.0"
r"""
build.py — 题链统一构建器：XeLaTeX PDF 编译 + HTML 卡片牌组渲染 + 零信任质量验收

支持三种输出格式模式（由 --format 或 config.yaml 中的 output_format 决定）：
  1. pdf   — 仅编译 XeLaTeX 双版 PDF (student.pdf & teacher.pdf) 并校验排版日志与防泄露；
  2. html  — 仅渲染 HTML 一页一卡卡片牌组 (student.html & teacher.html) 并校验卡片结构与防泄露；
  3. both  — 同时构建双版 PDF 与 HTML 卡片牌组（默认）。

用法:
  python build.py [工作目录] [--format {both,pdf,html}] [选项]
退出码: 0=通过, 1=编译或渲染验收失败, 2=缺少依赖或输入。
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time

# 启用确定性构建（Reproducible Builds），固定时间戳
os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import gen_config  # noqa: E402
import tex2cards  # noqa: E402
import render_html  # noqa: E402

# --- LaTeX 日志扫描正则 ---
RE_UNDEF_REF = re.compile(
    r"LaTeX Warning: (?:Reference|Citation) `[^']*' .*undefined|"
    r"There were undefined (?:references|citations)")
RE_PD_WARNING = re.compile(r"Package problem-driven Warning")
RE_MDFRAMED = re.compile(r"Package mdframed Warning: You got a bad break")
RE_OVERFULL = re.compile(r"Overfull \\hbox \(([\d.]+)pt too wide\)")
RE_UNDERFULL = re.compile(r"Underfull \\[hv]box")
RE_FONT_WARN = re.compile(r"(?:LaTeX )?Font Warning|Missing character:")
RE_MULTICOL_1COL = re.compile(r"Package multicol[cs]? Warning: Using `1' columns")
RE_SUMMARY = re.compile(
    r"\[problem-driven\] SUMMARY solution=(\d+),\s*"
    r"teacherNote=(\d+),\s*fillin=(\d+)")
RE_STRIPPED = re.compile(r"\[problem-driven\] 学生版已剥离")

# --- HTML 防泄露与结构正则 ---
FORBID_MARKERS = ("【解】", "【证明】", "【答】", "参考解答", "参考答案",
                  "教学要点", "易错分析", "评分", "教师用书", "无提示练习")
RE_FORBID_EN = re.compile(r"\b(Solution|Answer|Proof)\b")

RE_OPTIONS_OPEN = re.compile(r'<span class="qc-options"[^>]*>')
RE_HTML_TOKEN = re.compile(r"<span\b[^>]*>|</span>|<button\b[^>]*>")
RE_DATA_CORRECT = re.compile(r'data-correct="(true|false)"')
MIN_GROUPS_FOR_POSITION_GATE = 3


def write_json(path, report):
    """写结构化检查结果（统一交付证据格式）。"""
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    evidence.write_report(path, report)
    print("[build] 结构化报告已写入 %s" % path)


def collect_issue(report, target, kind, msg):
    report["issues"].append({"target": target, "severity": kind, "message": msg})


def check_env(format_choice="both"):
    """环境预检：列出缺失项与版本。"""
    ok = True
    print("[env] Python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 8):
        print("[env] 需要 Python 3.8+")
        ok = False

    if format_choice in ("html", "both"):
        tpl = render_html.DEFAULT_TEMPLATE
        if os.path.exists(tpl):
            print("[env] HTML 模版: %s" % tpl)
        else:
            print("[env] 缺少 HTML 模版: %s" % tpl)
            ok = False

    if format_choice in ("pdf", "both"):
        for tool, ver_args in (("xelatex", ["--version"]),
                               ("pdftotext", ["-v"]),
                               ("kpsewhich", ["--version"])):
            if not shutil.which(tool):
                print("[env] 缺少 %s (PDF 编译需要)" % tool)
                ok = False
                continue
            r = subprocess.run([tool] + ver_args, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            out = (r.stdout or r.stderr or "").splitlines()
            print("[env] %s: %s" % (tool, out[0].strip() if out else "可用"))
        if shutil.which("kpsewhich"):
            for pkg in ("ctexart.cls", "exam-zh-choices.sty", "mdframed.sty", "multicol.sty"):
                r = subprocess.run(["kpsewhich", pkg], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
                if r.returncode != 0 or not r.stdout.strip():
                    print("[env] 缺少宏包/类文件: %s" % pkg)
                    ok = False
                else:
                    print("[env] %s: %s" % (pkg, r.stdout.strip()))

    print("[env] 预检%s" % ("通过" if ok else "未通过"))
    return 0 if ok else 2


# =============================================================================
#  PDF 构建与质检逻辑 (XeLaTeX)
# =============================================================================

def scan_log(log_text, target, args):
    """扫描单个 LaTeX 日志，返回 (errors, warnings) 两个消息列表。"""
    errors, warnings = [], []
    n_undef = len(RE_UNDEF_REF.findall(log_text))
    if n_undef:
        errors.append("未定义引用/文献 %d 处（交叉引用未闭合或编译遍数不足）" % n_undef)

    n_pd = len(RE_PD_WARNING.findall(log_text))
    if n_pd:
        errors.append("Package problem-driven 警告 %d 处（\\fillin 缺少宽度、参考答案或选项）" % n_pd)

    n_md = len(RE_MDFRAMED.findall(log_text))
    if n_md:
        msg = "mdframed bad break %d 处（knowledgebox 跨页断裂）" % n_md
        if getattr(args, "allow_mdframed_breaks", False):
            warnings.append(msg)
        else:
            errors.append(msg)

    overs = [float(x) for x in RE_OVERFULL.findall(log_text)]
    max_over = getattr(args, "max_overfull_pt", 10.0)
    serious = [x for x in overs if x > max_over]
    if serious:
        errors.append("严重 Overfull \\hbox %d 处（最大 %.1fpt > 阈值 %.1fpt）"
                      % (len(serious), max(serious), max_over))
    minor = len(overs) - len(serious)
    if minor:
        warnings.append("轻微 Overfull \\hbox %d 处（均 ≤ %.1fpt）" % (minor, max_over))

    n_under = len(RE_UNDERFULL.findall(log_text))
    stu_under = getattr(args, "max_underfull_student", None)
    tea_under = getattr(args, "max_underfull_teacher", 3)
    limit = (stu_under if target == "student" else tea_under)
    if n_under:
        if limit is not None and n_under > limit:
            errors.append("Underfull \\hbox/\\vbox %d 处，超过阈值 %d" % (n_under, limit))
        else:
            note = "（学生版留白属正常，仅诊断）" if target == "student" else ""
            warnings.append("Underfull \\hbox/\\vbox %d 处%s" % (n_under, note))

    n_font = len(RE_FONT_WARN.findall(log_text))
    if n_font:
        msg = "字体警告/缺字 %d 处（字体回退可能导致字形替换）" % n_font
        (errors if getattr(args, "strict", False) else warnings).append(msg)

    n_mc = len(RE_MULTICOL_1COL.findall(log_text))
    if n_mc:
        errors.append("multicol 收到 1 栏并自动改为双栏 %d 处" % n_mc)

    return errors, warnings


def parse_summary(log_text):
    m = RE_SUMMARY.search(log_text)
    if not m:
        return None
    return {"solution": int(m.group(1)),
            "teacherNote": int(m.group(2)),
            "fillin": int(m.group(3))}


def run_xelatex(cmd, cwd, timeout=300):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + ("\n[build] 编译超时（超过 %d 秒）" % timeout)
        r = subprocess.CompletedProcess(cmd, 124, out)
        return r, True


def compile_pdf_target(workdir, target, args, report=None):
    tex = os.path.join(workdir, target + ".tex")
    pdf = os.path.join(workdir, target + ".pdf")
    log = os.path.join(workdir, target + ".log")
    if not os.path.exists(tex):
        print("[%s] 缺少 %s.tex" % (target, target))
        if report is not None:
            collect_issue(report, target, "error", "缺少 %s.tex" % target)
        return False, None

    timeout = getattr(args, "timeout", 300)
    max_retry = getattr(args, "max_retry", 1)
    cmd = ["xelatex", "-halt-on-error", "-interaction=nonstopmode", target + ".tex"]
    good = False
    passes = 0
    t0 = time.time()
    for attempt in range(max_retry + 1):
        r, timed_out = run_xelatex(cmd, workdir, timeout)
        passes += 1
        if r.returncode != 0:
            if timed_out:
                continue
            print("[%s] 第 %d 次编译第一遍失败：" % (target, attempt + 1))
            for line in r.stdout.splitlines():
                if line.startswith("!"):
                    print("    " + line)
                    break
            break
        t0 = time.time()
        r2, timed_out2 = run_xelatex(cmd, workdir, timeout)
        passes += 1
        if r2.returncode == 0:
            good = True
            break
        if timed_out2:
            continue
        print("[%s] 第 %d 次编译第二遍失败：" % (target, attempt + 1))
        for line in r2.stdout.splitlines():
            if line.startswith("!"):
                print("    " + line)
                break
        break

    print("[%s] PDF 编译%s（%d 遍）" % (target, "成功" if good else "失败", passes))
    if not good:
        if report is not None:
            collect_issue(report, target, "error", "PDF 编译失败")
        return False, None

    stale = []
    for path, what in ((pdf, "PDF"), (log, "日志")):
        if not os.path.exists(path):
            stale.append("%s 未生成（%s）" % (what, os.path.basename(path)))
        elif os.path.getmtime(path) < t0 - 1:
            stale.append("%s 不是本次编译产物" % what)
    for msg in stale:
        print("[%s] [错误] %s" % (target, msg))
        if report is not None:
            collect_issue(report, target, "error", msg)
    if stale:
        return False, None

    with open(log, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    errors, warnings = scan_log(log_text, target, args)
    for msg in warnings:
        print("[%s] [警告] %s" % (target, msg))
        if report is not None:
            collect_issue(report, target, "warning", msg)
    for msg in errors:
        print("[%s] [错误] %s" % (target, msg))
        if report is not None:
            collect_issue(report, target, "error", msg)

    summary = parse_summary(log_text)
    if summary is None:
        print("[%s] [错误] 日志缺少 [problem-driven] SUMMARY 行" % target)
        errors.append("缺少 SUMMARY")
    else:
        print("[%s] SUMMARY solution=%d, teacherNote=%d, fillin=%d"
              % (target, summary["solution"], summary["teacherNote"], summary["fillin"]))
        if target == "teacher" and summary["solution"] == 0:
            print("[%s] [错误] 教师版 solution=0，参考解答缺失" % target)
            errors.append("教师版无 solution")
        if target == "student" and not RE_STRIPPED.search(log_text):
            print("[%s] [错误] 学生版日志缺少“已剥离”标记" % target)
            errors.append("学生版剥离标记缺失")

    return not errors, summary


compile_target = compile_pdf_target


# =============================================================================
#  HTML 构建与质检逻辑 (tex2cards + render_html)
# =============================================================================

def count_visible_solutions(html_path):
    """HTML 中可见的参考解答块数量（隐藏揭示区不计）。"""
    try:
        with open(html_path, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError:
        return 0
    blocks = re.findall(r'<div class="[^"]*\bqc-solution\b[^"]*"([^>]*)>', html)
    return sum(1 for attrs in blocks if "hidden" not in attrs)


def check_forbidden_student_markers(html_path):
    """学生版可见文本中教师专属标记硬校验。"""
    text = evidence.html_visible_text(html_path)
    if text is None:
        return ["无法提取学生版 HTML 可见文本: %s" % html_path]
    problems = []
    for w in FORBID_MARKERS:
        if w in text:
            i = text.find(w)
            problems.append("学生版可见文本出现教师标记 “%s”: …%s…"
                            % (w, text[max(0, i - 15):i + len(w) + 15]))
    for m in RE_FORBID_EN.finditer(text):
        problems.append("学生版可见文本出现英文教师标记: …%s…"
                        % text[max(0, m.start() - 15):m.end() + 15])
    return problems


def scan_answer_positions(html_path):
    """抽取 HTML 中每个 qc-options 选项组的正确答案下标分布。"""
    problems = []
    positions = []
    try:
        with open(html_path, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError:
        return 0, [], ["无法读取 %s" % os.path.basename(html_path)]
    n_groups = 0
    m = RE_OPTIONS_OPEN.search(html)
    while m:
        depth = 1
        flags = []
        tok = RE_HTML_TOKEN.search(html, m.end())
        while tok and depth > 0:
            t = tok.group(0)
            if t == "</span>":
                depth -= 1
            elif t.startswith("<span"):
                depth += 1
            else:  # <button ...>
                attrs = t[len("<button"):].rstrip(">")
                if "qc-option" in attrs:
                    mc = RE_DATA_CORRECT.search(attrs)
                    if mc:
                        flags.append(mc.group(1))
            tok = RE_HTML_TOKEN.search(html, tok.end())
        n_groups += 1
        if not flags:
            problems.append("第 %d 个 qc-options 选项组未解析到 qc-option 按钮" % n_groups)
        else:
            n_true = flags.count("true")
            if n_true != 1:
                problems.append("第 %d 个 qc-options 选项组的 data-correct=\"true\" 数量为 %d" % (n_groups, n_true))
            else:
                positions.append(flags.index("true"))
        m = RE_OPTIONS_OPEN.search(html, tok.end() if tok else m.end())
    return n_groups, positions, problems


def check_answer_positions(rendered, report):
    ok = True
    stats = {}
    for target in ("student", "teacher"):
        path = rendered.get(target)
        if not path or not os.path.exists(path):
            continue
        n_groups, positions, problems = scan_answer_positions(path)
        stats[target] = {
            "groups": n_groups,
            "positions": {str(i): positions.count(i) for i in sorted(set(positions))},
        }
        for p in problems:
            print("[build] [错误] [%s] %s" % (target, p))
            collect_issue(report, target, "error", "%s: %s" % (target, p))
            ok = False
        if n_groups >= MIN_GROUPS_FOR_POSITION_GATE and positions and all(p == 0 for p in positions):
            msg = ("%s.html 全部 %d 个选择题的正确答案都位于选项首位（疑似未执行随机打乱）"
                   % (target, n_groups))
            print("[build] [错误] [%s] %s" % (target, msg))
            collect_issue(report, target, "error", msg)
            ok = False
    if stats:
        report["evidence"]["answer_positions"] = stats
        for target, st in stats.items():
            print("[build] [%s] 答案位置分布：%d 个选项组 %s" % (target, st["groups"], st["positions"]))
    return ok


def check_shuffle_applied(workdir, report):
    """选择题选项随机打乱机制校验。"""
    try:
        import shuffle_options
        problems = list(shuffle_options.verify_project(workdir))
        results, errors = shuffle_options.scan_project(workdir, apply_changes=False)
        problems.extend(errors)
        records = [r for res in results for r in res["records"]]
        dist = {}
        for r in records:
            idx = r.get("answer_position_before") if "answer_position_before" in r else r.get("correct_index", 0)
            dist[str(idx)] = dist.get(str(idx), 0) + 1
        for p in problems:
            print("[build] [错误] %s" % p)
            collect_issue(report, "shuffle", "error", p)
        if problems:
            return False
        if dist:
            report["evidence"]["shuffle_distribution"] = dist
            report["evidence"]["shuffle_answer_positions"] = {"slots": len(records), "positions": dist}
            print("[build] 源级选项正确项分布：%s" % dist)
        return True
    except Exception as e:
        # C1：fail-closed —— 打乱核对异常一律视为未通过（旧实现 except 后
        # return True，import/解析异常即整体放行，门禁形同虚设）。
        msg = "随机打乱核对异常（按未通过处理）: %s" % e
        print("[build] [错误] %s" % msg)
        collect_issue(report, "shuffle", "error", msg)
        return False


# =============================================================================
#  主流程
# =============================================================================

def resolve_output_format(workdir, cli_format=None):
    if cli_format:
        return cli_format.strip().lower()
    val = render_html.load_config_value(workdir, "output_format", "both")
    val = val.strip().lower()
    if val in ("pdf", "html", "both"):
        return val
    return "both"


def main():
    ap = argparse.ArgumentParser(description="题链统一双源构建器 (XeLaTeX PDF / HTML 卡片)")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--format", choices=["both", "pdf", "html"], default=None,
                    help="输出格式：both(全部输出，默认) | pdf(仅 XeLaTeX PDF) | html(仅 HTML 一页一卡)")
    ap.add_argument("--targets", nargs="+", default=["student", "teacher"])
    ap.add_argument("--template", default=render_html.DEFAULT_TEMPLATE, help="HTML 模版路径")
    ap.add_argument("--max-retry", type=int, default=1)
    ap.add_argument("--max-overfull-pt", type=float, default=10.0)
    ap.add_argument("--max-underfull-teacher", type=int, default=3)
    ap.add_argument("--max-underfull-student", type=int, default=None)
    ap.add_argument("--allow-mdframed-breaks", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--json", metavar="PATH", help="结构化结果写入路径")
    ap.add_argument("--check-env", action="store_true", help="只做环境预检")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    fmt = resolve_output_format(workdir, args.format)

    if args.check_env:
        return check_env(fmt)

    t0 = time.time()
    print("=" * 64)
    print(f"[*] 题链构建启动: 目录={workdir}, 目标格式={fmt}")
    print("=" * 64)

    # 1. 确定性生成配置 TeX
    gen_rc = gen_config.generate(workdir)
    manifest = evidence.collect_inputs(workdir)
    pre_digest = evidence.content_digest(manifest)

    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "build.py",
        "tool_version": TOOL_VERSION,
        "scope": {"workdir": workdir, "targets": args.targets, "format": fmt},
        "input_digest": pre_digest,
        "output_format": fmt,
        "pdf_sha256": None,
        "html_sha256": None,
        "status": "pass",
        "issues": [],
        "evidence": {}
    }

    if gen_rc != 0:
        report["status"] = "error"
        report["issues"].append({"severity": "error", "message": "配置生成失败，构建中止"})
        write_json(args.json, report)
        return 2

    # 2. 随机打乱机制校验
    if not check_shuffle_applied(workdir, report):
        report["status"] = "fail"
        write_json(args.json, report)
        return 1

    overall_ok = True

    # 3. PDF 构建 (若格式包含 pdf)
    if fmt in ("pdf", "both"):
        print("\n--- [1/2] XeLaTeX 双版 PDF 编译 ---")
        if not shutil.which("xelatex"):
            print("error: 未找到 xelatex 编译器")
            collect_issue(report, "pdf", "error", "缺少 xelatex 依赖")
            overall_ok = False
        else:
            pdf_ok = True
            summaries = {}
            for t in args.targets:
                t_ok, t_summary = compile_target(workdir, t, args, report)
                pdf_ok &= t_ok
                if t_summary:
                    summaries[t] = t_summary

            if "student" in summaries and "teacher" in summaries:
                s, t = summaries["student"], summaries["teacher"]
                for key in ("solution", "teacherNote", "fillin"):
                    if s[key] != t[key]:
                        print("[一致性] [错误] 学生版 %s=%d 与教师版 %s=%d 不一致" % (key, s[key], key, t[key]))
                        collect_issue(report, "pdf", "error", "双版 %s 计数不一致 (%d != %d)" % (key, s[key], t[key]))
                        pdf_ok = False
                        break
                else:
                    print("[一致性] 双版 SUMMARY 计数一致: solution=%d, teacherNote=%d, fillin=%d"
                          % (s["solution"], s["teacherNote"], s["fillin"]))

            report["pdf_sha256"] = {
                t: evidence.sha256_file(os.path.join(workdir, t + ".pdf"))
                for t in args.targets
            }
            report["evidence"]["log_sha256"] = {
                t: evidence.sha256_file(os.path.join(workdir, t + ".log"))
                for t in args.targets
            }
            report["evidence"]["summaries"] = summaries
            if not pdf_ok:
                overall_ok = False

    # 4. HTML 构建 (若格式包含 html)
    if fmt in ("html", "both"):
        print("\n--- [2/2] HTML 卡片牌组双版渲染 ---")
        deck, issues = tex2cards.parse_project(workdir)
        for msg in issues:
            print("[build] [解析提示] %s" % msg)
        problems = tex2cards.validate_deck(deck)
        counts = deck.get("counts", {})
        print("[build] 牌组：%d 张卡片（题目 %d，槽位 %d，解答 %d，要点 %d）"
              % (counts.get("cards", 0), counts.get("questions", 0),
                 counts.get("slots", 0), counts.get("solutions", 0),
                 counts.get("teacher_notes", 0)))
        import json as _json
        with open(os.path.join(workdir, "cards.json"), "w", encoding="utf-8") as f:
            _json.dump(deck, f, ensure_ascii=False, indent=2)

        for p in problems:
            print("[build] [错误] %s" % p)
            collect_issue(report, "deck", "error", p)

        if problems:
            overall_ok = False
        else:
            html_ok = True
            rendered = {}
            for target in args.targets:
                out_path = os.path.join(workdir, target + ".html")
                good, msg = render_html.render_version(deck, workdir, target, out_path, args.template)
                print("[build] [%s] %s" % (target, msg))
                rendered[target] = out_path
                if not good:
                    collect_issue(report, target, "error", msg)
                    html_ok = False

            for target, path in rendered.items():
                if not os.path.exists(path):
                    collect_issue(report, target, "error", "缺少 %s.html" % target)
                    html_ok = False
                elif os.path.getmtime(path) < t0 - 1:
                    collect_issue(report, target, "error", "%s.html 不是本次构建产物" % target)
                    html_ok = False

            # HTML 双版一致性核对
            if "teacher" in rendered and os.path.exists(rendered["teacher"]):
                t_vis = count_visible_solutions(rendered["teacher"])
                exp_sol = counts.get("solutions", 0)
                if exp_sol > 0 and t_vis != exp_sol:
                    collect_issue(report, "teacher", "error",
                                  "教师版可见解答数 (%d) 与源 solution 数 (%d) 不一致" % (t_vis, exp_sol))
                    html_ok = False

            if "student" in rendered and os.path.exists(rendered["student"]):
                s_vis = count_visible_solutions(rendered["student"])
                if s_vis != 0:
                    collect_issue(report, "student", "error",
                                  "学生版可见解答数为 %d（应为 0，隐藏区外泄）" % s_vis)
                    html_ok = False
                for p in check_forbidden_student_markers(rendered["student"]):
                    print("[build] [错误] [student] %s" % p)
                    collect_issue(report, "student", "error", p)
                    html_ok = False

            if not check_answer_positions(rendered, report):
                html_ok = False

            report["html_sha256"] = {
                t: evidence.sha256_file(os.path.join(workdir, t + ".html"))
                for t in args.targets
            }
            if not html_ok:
                overall_ok = False

    # 5. 构建期间输入防篡改
    post_digest = evidence.content_digest(evidence.collect_inputs(workdir))
    report["evidence"]["post_build_input_digest"] = post_digest
    if post_digest != pre_digest:
        print("[build] [错误] 构建期间受审输入发生变化")
        collect_issue(report, "both", "error", "构建期间受审输入变化")
        overall_ok = False

    print("\n" + "=" * 64)
    print("[build] 总体构建结果: %s" % ("通过 (ALL PASS)" if overall_ok else "存在未通过项 (FAIL)"))
    print("=" * 64)
    report["status"] = "pass" if overall_ok else "fail"
    write_json(args.json, report)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
