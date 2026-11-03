#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.1"
r"""render_smoke.py — 双模视觉冒烟：把最易出渲染事故的页面渲染成 PNG 供真人复核

背景（§19 事故复盘）：结构门禁全绿 ≠ 排版正确。曾出现 PDF 选项整段空白、
HTML 乱码而 17 项门禁全绿的事故。本脚本在构建后运行，把 student.pdf /
teacher.pdf 的"第 1 页 + 第一个含选项组（（ … ／ … ）模式）的页"用
pdftoppm 渲染成 PNG，落入审读包目录的 smoke/ 子目录，并打印真人复核清单。

定位约定：
  - 输入  <self>/student.pdf、<self>/teacher.pdf，由 --pdfs 选择
            （逗号分隔，缺省 "student,teacher"）。只有被选中且实际存在
            的 PDF 参与冒烟；**缺 PDF 不再记 problem**——是否可交付由
            调用方决定（deliver.py 的硬门槛自行核对产物存在性与 PNG 数）。
  - 选项页  PyMuPDF(fitz) 逐页文本搜索：第一页含选项组模式（全角左括号
            "（" + 全角 "／" 或半角 "/" 分隔符）的页视为填空选项页
            （找不到则只渲染第 1 页并记 problem）
  - 渲染  pdftoppm -r 150 -png -f N -l N（MiKTeX 自带；找不到 → problem）
  - 输出  <self>/.pd/review/smoke/（沿用 make_review_pack.py 的 .pd/review
          审读包目录约定）：student-p1.png、student-opts-p3.png 等，
          以及 smoke-manifest.json（PNG 清单 / 页码 / 命中模式）
  - 报告  统一证据格式（schema_version 2）经 evidence.write_report 落盘，
          默认 <self>/.pd/reports/render_smoke.json（--json 可改）

性质：建议性（advisory）。渲染成功即 exit 0，即使发现可疑也不替人判断；
真人过目清单见终端输出。硬门槛判定在 deliver.py（复盘 §4.4.1：与门禁
全绿并列，缺一不交付）。

用法:
  python render_smoke.py --self-dir <self> [--format {text,json}] [--json PATH]
                         [--pdfs student,teacher]
退出码: 0=冒烟完成（已生成 PNG 与清单）, 1=存在 problem（缺依赖/渲染失败/
         未选择任何已知 PDF）。
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import evidence  # noqa: E402

TOOL_NAME = "render_smoke.py"
LOG = "[render_smoke]"

#: 选项组模式：全角括号对内出现斜杠分隔（exam-zh 风格填空选项"（A／B／C）"）。
#: 实测 XeLaTeX 抽取文本中分隔符常为半角 "/"（如 "（H−1K/KH/HK）"），
#: 故全角 "／" 与半角 "/" 均视为命中；要求斜杠落在全角括号对内，
#: 避免数学文本中孤立的 "（" 或 "G/N" 造成整卷误命中。
OPTION_GROUP_RE = re.compile(r"（[^）]*[/／][^）]*）")
OPTION_PATTERN_LABEL = "（…／或/…）"

#: 审读包目录约定（与 make_review_pack.py 的 .pd/review 一致）
REVIEW_DIR_REL = os.path.join(".pd", "review")
SMOKE_DIR_REL = os.path.join(REVIEW_DIR_REL, "smoke")
DEFAULT_REPORT_REL = os.path.join(".pd", "reports", "render_smoke.json")

#: 已知 PDF 基名白名单与 --pdfs 缺省值（双模交付的固定文件名）
PDF_NAMES = ("student", "teacher")
DEFAULT_PDFS = "student,teacher"

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - 环境应具备，防御性降级
    fitz = None


def log(msg):
    print("%s %s" % (LOG, msg))


# ---------------------------------------------------------------------------
# 纯函数：页文本搜索（可单测，不依赖真实 PDF）
# ---------------------------------------------------------------------------
def page_has_option_group(text):
    """单页文本是否含选项组模式（全角括号对内出现全/半角斜杠）。"""
    if not text:
        return False
    return OPTION_GROUP_RE.search(text) is not None


def find_option_page(page_texts):
    """返回第一个含选项组模式的页码（1 基）；未命中返回 None。纯函数。"""
    for i, text in enumerate(page_texts, 1):
        if page_has_option_group(text):
            return i
    return None


def parse_pdfs_arg(value):
    """解析 --pdfs 逗号分隔参数 → 去重有序的已知 PDF 基名列表。纯函数。

    value 为 None 时取缺省 "student,teacher"；未知基名（拼写错误）被
    忽略——调用方误输入不构成"缺 PDF"，只缩小冒烟范围。
    """
    if value is None:
        value = DEFAULT_PDFS
    names = []
    for raw in str(value).split(","):
        n = raw.strip()
        if not n or n not in PDF_NAMES:
            continue
        if n not in names:
            names.append(n)
    return names


def extract_page_texts(pdf_path):
    """用 PyMuPDF 抽取逐页文本；fitz 不可用或文件打不开时抛 RuntimeError。"""
    if fitz is None:
        raise RuntimeError(
            "PyMuPDF(fitz) 导入失败：请安装 pymupdf（pip install pymupdf）后重试")
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise RuntimeError("fitz 无法打开 %s: %s" % (pdf_path, e))
    try:
        return [page.get_text() for page in doc]
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 渲染（pdftoppm 子进程）
# ---------------------------------------------------------------------------
def render_page(pdftoppm, pdf_path, page, out_prefix):
    """渲染单页为 PNG。返回 (最终 PNG 路径 或 None, 错误信息)。"""
    cmd = [pdftoppm, "-r", "150", "-png",
           "-f", str(page), "-l", str(page), pdf_path, out_prefix]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        return None, "pdftoppm 启动失败: %s" % e
    if r.returncode != 0:
        return None, "pdftoppm 退出码 %d: %s" % (
            r.returncode, (r.stderr or r.stdout or "").strip()[:200])
    # pdftoppm 输出命名为 <prefix>-<页码>.png（位数随总页数补齐）
    produced = sorted(glob.glob(out_prefix + "-*.png"))
    final = out_prefix + ".png"
    if not produced:
        if os.path.isfile(final):
            produced = [final]
        else:
            return None, "pdftoppm 未产生输出文件（前缀 %s）" % out_prefix
    if produced[0] != final:
        os.replace(produced[0], final)
    return final, ""


def smoke_one_pdf(pdftoppm, self_dir, name, smoke_dir, problems):
    """对 student/teacher 单份 PDF 执行冒烟。返回 manifest items 列表。"""
    pdf_path = os.path.join(self_dir, name + ".pdf")
    items = []
    try:
        texts = extract_page_texts(pdf_path)
    except RuntimeError as e:
        problems.append(str(e))
        return items
    if not texts:
        problems.append("%s.pdf 无任何页面" % name)
        return items

    opts_page = find_option_page(texts)
    if opts_page is None:
        problems.append("%s.pdf 未找到选项组模式页（%s+%s），只渲染第 1 页"
                        % (name, OPTION_PATTERN_LABEL))

    targets = [("p1", 1, None)]
    if opts_page is not None and opts_page != 1:
        targets.append(("opts-p%d" % opts_page, opts_page,
                        OPTION_PATTERN_LABEL))
    elif opts_page == 1:
        targets[0] = ("p1", 1, OPTION_PATTERN_LABEL)

    for kind, page, pattern in targets:
        prefix = os.path.join(smoke_dir, "%s-%s" % (name, kind))
        png, err = render_page(pdftoppm, pdf_path, page, prefix)
        if png is None:
            problems.append("%s.pdf 第 %d 页渲染失败: %s" % (name, page, err))
            continue
        items.append({
            "pdf": name + ".pdf",
            "png": os.path.relpath(png, self_dir).replace(os.sep, "/"),
            "page": page,
            "kind": "first_page" if kind == "p1" else "option_page",
            "pattern": pattern,
        })
    return items


def print_checklist(items, smoke_dir):
    """打印真人复核清单（终端文本）。"""
    print("=" * 64)
    log("===== 真人复核清单（视觉冒烟，建议性） =====")
    log("请人工过目以下截图（目录: %s）:" % smoke_dir)
    for it in items:
        log("  - %s  (%s 第 %d 页, %s)"
            % (it["png"], it["pdf"], it["page"],
               "选项页" if it["kind"] == "option_page" else "首页"))
    log("复核要点:")
    log("  ① 选项组是否全部有字（无整段空白的选项括号）")
    log("  ② 有无乱码/双重转义（如 \\\\fillin、\\\\textbackslash 字面泄漏）")
    log("  ③ 图片是否在位（无缺失图占位/排版坍塌）")
    log("说明：冒烟为建议性检查——渲染成功即 PASS，可疑与否由真人判断。")
    print("=" * 64)


def run_smoke(self_dir, pdfs=None, report_path=None):
    r"""执行一次双模视觉冒烟。返回 (report dict, exit_code)。可复用入口。

    self_dir     题链 self 工作区目录（不存在 → problem，退出码 1）。
    pdfs         --pdfs 取值：逗号分隔字符串或基名序列；None = 缺省
                  "student,teacher"。只有被选中且实际存在的 PDF 才参与
                  冒烟；缺 PDF 不记 problem（由调用方决定如何处理）。
    report_path  统一证据报告写入路径（None = <self_dir>/.pd/reports/
                  render_smoke.json）。
    """
    self_dir = os.path.abspath(self_dir)
    requested = parse_pdfs_arg(pdfs)
    problems = []
    if not requested:
        problems.append("未选择任何已知 PDF（--pdfs 仅支持 %s）"
                        % "/".join(PDF_NAMES))
    if not os.path.isdir(self_dir):
        problems.append("self 工作区不存在: %s" % self_dir)

    # 1. 依赖定位（缺依赖仍是 problem：无法渲染却报告 pass 会误导调用方）
    if fitz is None:
        problems.append("PyMuPDF(fitz) 导入失败：请安装 pymupdf "
                        "（pip install pymupdf）后重试")
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        problems.append("未找到 pdftoppm（MiKTeX 应自带 poppler 工具）；"
                        "无法进行 PNG 渲染")

    # 2. 输入定位：被选中且存在的 PDF 才参与冒烟（缺 PDF 不记 problem）
    pdfs_present = {}
    for name in requested:
        path = os.path.join(self_dir, name + ".pdf")
        if os.path.isfile(path):
            pdfs_present[name] = path

    smoke_dir = os.path.join(self_dir, SMOKE_DIR_REL)
    items = []
    if os.path.isdir(self_dir) and pdfs_present and not problems:
        os.makedirs(smoke_dir, exist_ok=True)
        log("冒烟开始: self=%s, pdftoppm=%s, pdfs=%s"
            % (self_dir, pdftoppm, ",".join(pdfs_present)))
        for name in pdfs_present:
            items.extend(smoke_one_pdf(pdftoppm, self_dir, name,
                                       smoke_dir, problems))

    # 3. smoke-manifest.json（渲染成功才写；目录约定沿用 make_review_pack）
    manifest_path = None
    if items:
        manifest = {
            "schema_version": evidence.SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "self_dir": self_dir,
            "dpi": 150,
            "pattern": OPTION_PATTERN_LABEL,
            "pdfs": sorted(pdfs_present),
            "png_count": len(items),
            "items": items,
            "advisory": True,
        }
        manifest_path = os.path.join(smoke_dir, "smoke-manifest.json")
        evidence.write_report(manifest_path, manifest)

    # 4. 统一证据报告（write_report 落盘）
    if report_path is None:
        report_path = os.path.join(self_dir, DEFAULT_REPORT_REL)
    rdir = os.path.dirname(os.path.abspath(report_path))
    if rdir:
        os.makedirs(rdir, exist_ok=True)
    try:
        digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    except Exception:
        digest = None
    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": {"workdir": self_dir, "kind": "visual-smoke"},
        "input_digest": digest,
        "status": "pass" if not problems else "fail",
        "issues": [{"severity": "error", "message": p} for p in problems],
        "evidence": {
            "smoke_dir": os.path.relpath(smoke_dir, self_dir).replace(os.sep, "/"),
            "manifest": (os.path.relpath(manifest_path, self_dir).replace(os.sep, "/")
                         if manifest_path else None),
            "png_count": len(items),
            "requested_pdfs": requested,
            "pdfs": sorted(pdfs_present),
            "items": items,
            "advisory": True,
        },
    }
    evidence.write_report(report_path, report)
    return report, (0 if not problems else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="双模视觉冒烟：关键页面渲染为 PNG 供真人复核")
    ap.add_argument("--self-dir", default=".",
                    help="题链 self 工作区目录（默认当前目录）")
    ap.add_argument("--format", choices=["text", "json"], default="text",
                    help="终端输出格式（默认 text；json 输出机器可读摘要）")
    ap.add_argument("--json", metavar="PATH", default=None,
                    help="结构化报告写入路径（默认 <self-dir>/.pd/reports/"
                         "render_smoke.json）")
    ap.add_argument("--pdfs", metavar="NAMES", default=DEFAULT_PDFS,
                    help="参与冒烟的 PDF 基名（逗号分隔，缺省 "
                         "%s）；只有被选中且存在的 PDF 才参与，缺 PDF "
                         "不记 problem" % DEFAULT_PDFS)
    args = ap.parse_args(argv)

    self_dir = os.path.abspath(args.self_dir)
    if not os.path.isdir(self_dir):
        print("%s self 工作区不存在: %s" % (LOG, self_dir))
        return 1

    report, code = run_smoke(self_dir, pdfs=args.pdfs, report_path=args.json)
    problems = [i["message"] for i in report["issues"]]
    items = report["evidence"]["items"]
    smoke_dir = os.path.join(self_dir, SMOKE_DIR_REL)
    report_path = args.json or os.path.join(self_dir, DEFAULT_REPORT_REL)

    # 5. 终端输出
    if args.format == "json":
        print(json.dumps({"status": report["status"], "problems": problems,
                          "items": items,
                          "manifest": report["evidence"]["manifest"]},
                         ensure_ascii=False, indent=2))
    else:
        if items:
            print_checklist(items, smoke_dir)
        for p in problems:
            log("[problem] " + p)
        log("结构化报告已写入 %s" % report_path)
        log("冒烟%s（生成 PNG %d 张，problem %d 项）"
            % ("完成" if not problems else "存在问题", len(items), len(problems)))

    return code


if __name__ == "__main__":
    sys.exit(main())
