#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.3"
r"""
make_review_pack.py — 生成学生审读包（独立教学验收阶段一的受控输入）

验收者不得直接读含答案的 TeX 源。本脚本在构建出草稿学生版后，生成
<project>/.pd/review/：
  student-pages.txt   按页分隔的学生版可见文本（页首标注页码）
  manifest.json       审读包清单：内容摘要 content_digest（sec*.tex +
                      模板 + 配置 + 版本开关文件）、student.pdf 摘要、
                      引用图片清单及摘要、生成时间，以及 smoke 区
                     （render_smoke.py 渲染冒烟的结果指针）

content_digest 是验收记录（.pd/acceptance/<scope>.md）与 deliver.py
判定“旧 PASS 是否仍有效”的共同依据：修改受审内容后摘要改变，旧验收
结论自动失效。**摘要只覆盖 evidence.collect_inputs 收集的受审输入**
（ENTRY_FILES + \input/\includegraphics 静态目标）；.pd/ 下的派生产物
（本 manifest.json、smoke/ 截图、.pd/reports/*.json）不在输入集内，
因此写入 smoke 区不会使 content_digest 漂移。

冒烟联动（复盘 §4.2.3）：manifest.json 写完后，若 self 目录存在
student.pdf / teacher.pdf，则以子进程最佳努力调用 render_smoke.py
（--pdfs 按实际存在的 PDF）。缺 fitz / pdftoppm / 渲染失败一律不
让本脚本失败——manifest.json 的 smoke 区记 status=fail 与 note，
交由人工或 deliver.py 的硬门槛处置。

注意：pdftotext 的抽取顺序对双栏/图文混排可能不可靠；顺序存疑时验收者
应直接审读渲染页（student.pdf 或 smoke/ 截图），本包只提供受控、可追溯
的文本视图。

用法: python make_review_pack.py <project>
退出码: 0=成功, 2=缺少依赖或输入。
"""
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402

IMG_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
#: 与 render_smoke.py 的目录约定保持一致（冒烟截图 / 统一证据报告）
SMOKE_DIR_REL = os.path.join(".pd", "review", "smoke")
SMOKE_REPORT_REL = os.path.join(".pd", "reports", "render_smoke.json")


def _smoke_pdfs(pdir):
    """self 目录中实际存在的 PDF 基名（render_smoke --pdfs 的取值）。"""
    return [n for n in ("student", "teacher")
            if os.path.isfile(os.path.join(pdir, n + ".pdf"))]


def _smoke_entry(status, png_count=0, note=None):
    """manifest.json 的 smoke 区（字段固定，缺一不可）。"""
    entry = {
        "status": status,              # pass / fail / skipped
        "smoke_dir": SMOKE_DIR_REL.replace(os.sep, "/"),
        "png_count": png_count,
        "report": SMOKE_REPORT_REL.replace(os.sep, "/"),
    }
    if note:
        entry["note"] = note
    return entry


def run_smoke_best_effort(pdir):
    """构建后最佳努力渲染冒烟（子进程调 render_smoke.py）。

    返回 manifest.json 的 smoke 区 dict。**任何失败都不得让调用方失败**：
    子进程缺失 / 缺 fitz·pdftoppm / 渲染失败 / 输出不可解析，一律记
    status=fail（或 skipped）与 note，由人工或 deliver.py 硬门槛处置。
    """
    pdfs = _smoke_pdfs(pdir)
    if not pdfs:
        return _smoke_entry(
            "skipped", 0,
            "self 目录无 student.pdf/teacher.pdf，未执行渲染冒烟")

    cmd = [sys.executable, os.path.join(SCRIPTS, "render_smoke.py"),
           "--self-dir", pdir, "--format", "json", "--pdfs", ",".join(pdfs)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return _smoke_entry("fail", 0, "render_smoke.py 子进程启动失败: %s" % e)

    payload = None
    try:
        payload = json.loads((r.stdout or "").strip() or "{}")
        if not isinstance(payload, dict):
            payload = None
    except ValueError:
        payload = None
    if payload is None:
        # stdout 不可解析时退回读统一证据报告（render_smoke 的落盘契约）
        try:
            with open(os.path.join(pdir, SMOKE_REPORT_REL),
                      encoding="utf-8") as f:
                rep = json.load(f)
            payload = {"status": rep.get("status"),
                       "items": (rep.get("evidence") or {}).get("items") or []}
        except (OSError, ValueError):
            payload = None

    if payload is None:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-200:]
        return _smoke_entry(
            "fail", 0,
            "render_smoke.py 输出不可解析（退出码 %s）: %s"
            % (r.returncode, tail))

    status = payload.get("status")
    if status not in ("pass", "fail"):
        status = "fail" if r.returncode else "pass"
    items = payload.get("items")
    png_count = len(items) if isinstance(items, list) else 0
    if status == "pass":
        note = None
    else:
        problems = payload.get("problems")
        if isinstance(problems, list) and problems:
            note = "渲染冒烟未通过: %s" % "; ".join(str(p) for p in problems)[:300]
        else:
            note = "渲染冒烟未通过（render_smoke.py 退出码 %s）" % r.returncode
    return _smoke_entry(status, png_count, note)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    pdir = os.path.abspath(args[0] if args else ".")
    if not shutil.which("pdftotext"):
        print("[review-pack] 未找到 pdftotext（poppler）。")
        return 2
    pdf = os.path.join(pdir, "student.pdf")
    if not os.path.exists(pdf):
        print("[review-pack] 缺少 student.pdf，请先用 build.py 构建草稿学生版。")
        return 2
    r = subprocess.run(["pdftotext", pdf, "-"], capture_output=True)
    if r.returncode != 0:
        print("[review-pack] pdftotext 抽取 student.pdf 失败。")
        return 2
    text = r.stdout.decode("utf-8", errors="replace")
    pages = text.split("\x0c")

    # ---- F05：共享证据模块统一收集输入清单与摘要（含装配顺序与图片资产） ----
    in_manifest = evidence.collect_inputs(pdir)
    digest = evidence.content_digest(in_manifest)
    secs = [f for f in in_manifest["assembly_order"]
            if re.match(r"sec", os.path.basename(f))]
    if not secs:
        secs = sorted(f for f in os.listdir(pdir)
                      if re.fullmatch(r"sec.*\.tex", f))
    images = {}
    for name in secs:
        path = os.path.join(pdir, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for img in IMG_RE.findall(f.read()):
                ip = os.path.join(pdir, img)
                images[img] = evidence.sha256_file(ip) or "MISSING"

    outdir = os.path.join(pdir, ".pd", "review")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "student-pages.txt"), "w",
              encoding="utf-8") as f:
        for i, page in enumerate(pages, 1):
            if not page.strip():
                continue
            f.write("===== 第 %d 页 =====\n%s\n" % (i, page.strip()))

    manifest = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "make_review_pack.py", "tool_version": TOOL_VERSION,
        "scope": {"sections": secs},
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input_digest": digest,
        "content_digest": digest,  # 兼容字段：验收记录 digest 行引用此值
        "pdf_sha256": {"student": evidence.sha256_file(pdf)},
        "student_pdf_sha256": evidence.sha256_file(pdf),
        "sections": secs,
        "images": images,
        "status": "pass",
        "issues": [],
        "evidence": {"missing_inputs": in_manifest["missing"],
                     "unresolved_inputs": in_manifest["unresolved"],
                     "card_refs": evidence.card_usages(pdir, secs)},
        "note": "文本视图仅供受控审读；阅读顺序存疑时以渲染页/smoke 截图为准。",
    }

    # ---- 冒烟联动（复盘 §4.2.3）：最佳努力，绝不让审读包失败 ----
    # digest 已在上面由 collect_inputs 算出；.pd/ 派生产物不在其输入集内，
    # 故追加 smoke 区不会使 content_digest 漂移（验收记录引用值不变）。
    smoke = run_smoke_best_effort(pdir)
    manifest["smoke"] = smoke

    with open(os.path.join(outdir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("[review-pack] 审读包已生成: %s" % outdir)
    print("[review-pack] content_digest=%s（验收记录须引用此摘要）" % digest)
    print("[review-pack] 渲染冒烟: status=%s, png_count=%d%s"
          % (smoke["status"], smoke["png_count"],
             ("（%s）" % smoke["note"]) if smoke.get("note") else ""))
    if smoke["status"] != "pass":
        print("[review-pack] [提示] 渲染冒烟未通过或已跳过：请人工过目 "
              "%s/ 截图；确认工具缺失时在交付说明中记录原因。"
              % SMOKE_DIR_REL.replace(os.sep, "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
