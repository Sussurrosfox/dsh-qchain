#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "3.0"
r"""
deliver.py — 题链交付汇总门禁（F08：只有当前完整证据能生成新的 release）

支持 PDF / HTML / 双输出交付模式。
执行流程:
  1. gen_config.generate 确定性配置生成；
  2. shuffle_options.py 选择题选项随机打乱；
  3. check_numbering.py 切片与编号结构检查；
  4. build.py 双源构建（XeLaTeX PDF 及/或 HTML 卡片牌组）；
  5. compare_versions.py 双版一致性与防泄露对比；
  6. 核对教学验收（acceptance/<scope>.json）、材料链（materials.json）与疑似项处置（review-notes.json）；
  7. 全部通过后原子发布至 release/ 目录。

用法: python deliver.py <project> [--skip-checks] [--format {both,pdf,html}]
退出码: 0=可交付（已原子切换 release）, 1=门禁未通过或待验收, 2=缺少依赖或输入。
"""
import argparse
import hashlib
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
import gen_config  # noqa: E402
import render_html  # noqa: E402

EXPECTED_SCHEMA = 2
# D1：检查编排分两段——shuffle（apply，会变异源文件）必须在受审摘要
# 计算之前完成；build 必须早于 budget(delivery)（预算/页数门禁依赖
# 当轮新 PDF，旧顺序检查的是上一轮陈旧产物）。
PRE_CHECKS = [
    ("shuffle_options.py", "shuffle_options.json"),
]
CHECKS = [
    ("check_numbering.py", "check_numbering.json"),
    ("check_pedagogy.py", "check_pedagogy.json"),
    ("check_dialogue.py", "check_dialogue.json"),
    ("build.py", "build.json"),
    ("check_budget.py", "check_budget.json", ["--stage", "delivery"]),
    ("compare_versions.py", "compare.json")
]
ALL_CHECK_REPORTS = [e[1] for e in PRE_CHECKS] + [e[1] for e in CHECKS]
# 教学语义、对话层规范与预算教案完备性门禁同样是一票否决项
MUST_PASS = ("shuffle_options.json", "check_numbering.json",
             "check_pedagogy.json", "check_dialogue.json",
             "check_budget.json", "build.json")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_check(script, pdir, report_path, extra_args=None):
    """执行单个检查脚本，只信本次产出的新报告。"""
    if os.path.exists(report_path):
        os.remove(report_path)
    cmd = [sys.executable, os.path.join(SCRIPTS, script), pdir, "--json", report_path]
    if extra_args:
        cmd.extend(extra_args)
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(script, 2, "", str(e))


def pdf_text_sha(pdf):  # 保留接口占位（历史遗留，当前无调用方）
    if not shutil.which("pdftotext"):
        return None
    r = subprocess.run(["pdftotext", pdf, "-"], capture_output=True)
    if r.returncode != 0:
        return None
    text = r.stdout.decode("utf-8", errors="replace")
    return hashlib.sha256("\n".join(text.split("\x0c")).encode("utf-8")).hexdigest()


def verify_report(script, fname, rep, pdir, digest, pdf_sha, blockers, html_sha=None, fmt="both"):
    if rep is None:
        blockers.append("缺少或无法解析报告 %s" % fname)
        return
    if rep.get("schema_version") != EXPECTED_SCHEMA:
        blockers.append("报告 %s 的 schema_version=%s，只接受 %d"
                        % (fname, rep.get("schema_version"), EXPECTED_SCHEMA))
        return
    if rep.get("tool") != script:
        blockers.append("报告 %s 的 tool 字段为 %r，应为 %s" % (fname, rep.get("tool"), script))
    if not rep.get("tool_version"):
        blockers.append("报告 %s 缺少 tool_version 字段" % fname)
    if rep.get("input_digest") != digest:
        blockers.append("报告 %s 绑定的输入摘要与当前不一致" % fname)

    # 验证 PDF 哈希绑定
    if fmt in ("pdf", "both") and pdf_sha and rep.get("pdf_sha256"):
        bound = rep.get("pdf_sha256")
        if isinstance(bound, dict):
            for n in ("student", "teacher"):
                if bound.get(n) and bound.get(n) != pdf_sha.get(n):
                    blockers.append("报告 %s 绑定的 %s.pdf 哈希与当前产物不一致" % (fname, n))

    # 验证 HTML 哈希绑定
    if fmt in ("html", "both") and html_sha and rep.get("html_sha256"):
        bound = rep.get("html_sha256")
        if isinstance(bound, dict):
            for n in ("student", "teacher"):
                if bound.get(n) and bound.get(n) != html_sha.get(n):
                    blockers.append("报告 %s 绑定的 %s.html 哈希与当前产物不一致" % (fname, n))


def atomic_release(pdir, manifest, fmt="both"):
    """先写临时目录，全部成功后原子切换 release。"""
    rel = os.path.join(pdir, "release")
    staging = os.path.join(pdir, ".release-staging")
    backup = os.path.join(pdir, ".release-prev")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    try:
        if fmt in ("pdf", "both"):
            for name in ("student.pdf", "teacher.pdf"):
                src = os.path.join(pdir, name)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(staging, name))

        if fmt in ("html", "both"):
            for name in ("student.html", "teacher.html", "cards.json"):
                src = os.path.join(pdir, name)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(staging, name))

        for doc in ("classroom-plan.md", "error-log.md"):
            src = os.path.join(pdir, doc)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(staging, doc))

        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if os.path.isdir(rel):
        if os.path.exists(backup):
            shutil.rmtree(backup)
        os.rename(rel, backup)
    try:
        os.rename(staging, rel)
    except Exception:
        if os.path.isdir(backup):
            os.rename(backup, rel)
        raise
    if os.path.isdir(backup):
        shutil.rmtree(backup)
    return rel


def main():
    ap = argparse.ArgumentParser(description="交付汇总门禁")
    ap.add_argument("project")
    ap.add_argument("--skip-checks", action="store_true",
                    help="复用已有报告（危险：需同时设置环境变量 "
                         "QCHAIN_ALLOW_SKIP_CHECKS=1 才生效）")
    ap.add_argument("--format", choices=["both", "pdf", "html"], default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="干跑模式：执行全部门禁校验并输出结论，但不发布 release/")
    args = ap.parse_args()

    pdir = os.path.abspath(args.project)
    if not os.path.isdir(pdir):
        print("[deliver] 项目目录不存在: %s" % pdir)
        return 2

    # D1：--skip-checks 会绕过"本次新证据"保证，必须显式二次确认
    if args.skip_checks and os.environ.get("QCHAIN_ALLOW_SKIP_CHECKS") != "1":
        print("[deliver] 拒绝执行：--skip-checks 需设置环境变量 "
              "QCHAIN_ALLOW_SKIP_CHECKS=1 显式确认（复用旧报告存在伪造面）。")
        return 2
    if args.skip_checks:
        print("[deliver] [警告] --skip-checks 已启用：将复用磁盘上的既有报告，"
              "请确认其来源可信。")

    fmt = args.format or render_html.load_config_value(pdir, "output_format", "both")
    fmt = fmt.strip().lower()
    if fmt not in ("pdf", "html", "both"):
        fmt = "both"

    print("=" * 64)
    print(f"[*] 题链最终交付门禁启动: 工作区={pdir}, 交付格式={fmt}"
          + ("（干跑模式，不发布）" if args.dry_run else ""))
    print("=" * 64)

    # 1. 确定性配置生成与确认态对账（C2 契约防篡改）
    if gen_config.generate(pdir) != 0:
        print("[deliver] 配置生成失败，停止放行。")
        return 2

    spec_errs = gen_config.verify_confirmed_spec(pdir)
    if spec_errs:
        print("[deliver] 确认态规范对账失败，禁止违规交付:")
        for err in spec_errs:
            print("  - " + err)
        return 1

    blockers = []
    accept_blockers = []
    rdir = os.path.join(pdir, ".pd", "reports")
    os.makedirs(rdir, exist_ok=True)

    reports = {}

    # 2. 前置变异步骤（D1）：shuffle(apply) 必须先于受审摘要计算，
    #    否则内容一变全部报告的 input_digest 绑定失配，首次 deliver 必失败。
    for entry in PRE_CHECKS:
        script, report_name = entry[0], entry[1]
        rep_path = os.path.join(rdir, report_name)
        if not args.skip_checks:
            print("[deliver] 运行前置门禁: %s ..." % script)
            proc = run_check(script, pdir, rep_path)
            if proc.returncode not in (0, 1):
                blockers.append("%s 执行异常 (退出码 %d): %s"
                                % (script, proc.returncode,
                                   (proc.stderr or proc.stdout).strip()[:150]))
        reports[report_name] = load_json(rep_path)

    # 3. 受审输入摘要（在全部内容变异之后计算）
    manifest_inputs = evidence.collect_inputs(pdir)
    digest = evidence.content_digest(manifest_inputs)

    if manifest_inputs["missing"]:
        blockers.append("输入清单存在缺失文件: %s" % manifest_inputs["missing"])
    if manifest_inputs["unresolved"]:
        blockers.append("输入清单存在未解析引用: %s"
                        % [u["target"] for u in manifest_inputs["unresolved"]])

    secs = [os.path.basename(f["path"]) for f in manifest_inputs["files"]
            if re.fullmatch(r"sec.*\.tex", os.path.basename(f["path"]))
            and os.path.basename(f["path"]) != "sec_template.tex"]

    # 4. 依次运行或验证其余门禁检查（build 早于 budget(delivery)）
    for entry in CHECKS:
        script = entry[0]
        report_name = entry[1]
        extra_args = entry[2] if len(entry) > 2 else None
        rep_path = os.path.join(rdir, report_name)
        if not args.skip_checks:
            print("[deliver] 运行门禁: %s ..." % script)
            proc = run_check(script, pdir, rep_path, extra_args=extra_args)
            if proc.returncode not in (0, 1):
                blockers.append("%s 执行异常 (退出码 %d): %s"
                                % (script, proc.returncode, (proc.stderr or proc.stdout).strip()[:150]))
        rep = load_json(rep_path)
        reports[report_name] = rep

    # 计算产物哈希
    pdf_sha = {}
    if fmt in ("pdf", "both"):
        pdf_sha = {n: evidence.sha256_file(os.path.join(pdir, n + ".pdf")) for n in ("student", "teacher")}

    html_sha = {}
    if fmt in ("html", "both"):
        html_sha = {n: evidence.sha256_file(os.path.join(pdir, n + ".html")) for n in ("student", "teacher")}

    # 验证各报告有效性（含前置 shuffle 报告）
    for entry in PRE_CHECKS + CHECKS:
        script, report_name = entry[0], entry[1]
        rep = reports.get(report_name)
        verify_report(script, report_name, rep, pdir, digest, pdf_sha, blockers, html_sha=html_sha, fmt=fmt)
        if report_name in MUST_PASS:
            if (rep or {}).get("status") != "pass":
                blockers.append("%s 未通过 (status=%s, issues=%d)"
                                % (script, (rep or {}).get("status"), len((rep or {}).get("issues", []))))

    # D1：pdstate 运行契约状态校验——交付要求全部切片任务已 checked/accepted，
    # 堵住"绕过 attempt 契约直接落盘"的路径（图内状态与 run.json 分裂检测）。
    run_json = load_json(os.path.join(pdir, ".pd", "run.json"))
    if run_json is None:
        blockers.append("缺少 .pd/run.json 运行契约（pdstate init 未执行）")
    else:
        tasks = run_json.get("tasks") or {}
        for s in (run_json.get("slices") or sorted(tasks)):
            st = (tasks.get(s) or {}).get("status")
            if st not in ("checked", "accepted"):
                blockers.append("切片任务 %s 的 pdstate 状态为 %r，"
                                "交付要求 checked/accepted" % (s, st))

    cmp_rep = reports.get("compare.json") or {}
    cmp_status = cmp_rep.get("status")
    if cmp_status not in ("pass", "review"):
        blockers.append("compare_versions.py 未通过 (status=%s)" % cmp_status)

    # 疑似项处置 (review-notes.json)
    if cmp_status == "review":
        notes_json = load_json(os.path.join(pdir, ".pd", "review-notes.json"))
        if notes_json is None:
            blockers.append("compare_versions.py 有疑似答案项待复核，但缺少 .pd/review-notes.json")
        else:
            cmp_sha = evidence.sha256_file(os.path.join(rdir, "compare.json"))
            for p in evidence.validate_review_notes(notes_json, cmp_rep, cmp_sha, digest,
                                                  pdf_sha256=pdf_sha if fmt in ("pdf", "both") else None,
                                                  html_sha256=html_sha if fmt in ("html", "both") else None):
                blockers.append(p)

    # 材料链核对
    used_cards = evidence.card_usages(pdir, secs)
    if used_cards:
        idx_data = load_json(os.path.join(pdir, ".pd", "materials.json"))
        if idx_data is None:
            blockers.append("正文引用卡片 %s，但缺少 .pd/materials.json" % ", ".join(sorted(used_cards)))
        else:
            for p in evidence.validate_material_chain(pdir, idx_data, secs):
                blockers.append(p)

    # 教学验收
    adir = os.path.join(pdir, ".pd", "acceptance")
    run_json = load_json(os.path.join(pdir, ".pd", "run.json")) or {}
    required = run_json.get("required_acceptance_scopes") or {}
    ajsons = {}
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            if fn.endswith(".json"):
                rec = load_json(os.path.join(adir, fn))
                if rec:
                    ajsons[fn[:-5]] = rec
                    for p in evidence.validate_acceptance_json(
                        rec, current_digest=digest,
                        student_pdf_sha=pdf_sha.get("student") if fmt in ("pdf", "both") else None,
                        student_html_sha=html_sha.get("student") if fmt in ("html", "both") else None
                    ):
                        accept_blockers.append(p)

    if required:
        for scope, qids in required.items():
            rec = ajsons.get(scope)
            if rec is None:
                accept_blockers.append("缺少必需验收范围 %s 的 JSON 记录" % scope)
                continue
            if rec.get("conclusion") != "PASS":
                accept_blockers.append("必需验收范围 %s 的结论非 PASS (%s)" % (scope, rec.get("conclusion")))
            missing_qids = sorted(set(qids) - set(rec.get("reviewed_qids", [])))
            if missing_qids:
                accept_blockers.append("必需验收范围 %s 的 PASS 未覆盖全部必需 qid（缺 %d 个: %s…）"
                                       % (scope, len(missing_qids), ", ".join(missing_qids[:5])))

    if blockers:
        print("[deliver] 门禁阻断，发现 %d 个阻断项:" % len(blockers))
        for b in blockers:
            print("  - " + b)
        return 1

    if accept_blockers:
        print("[deliver] 待验收：技术检查通过，教学验收未完成 (%d 项):" % len(accept_blockers))
        for b in accept_blockers:
            print("  - " + b)
        return 1

    # 产物存在性核对（D1：格式缺失硬失败，严禁静默降级交付范围——
    # 旧实现 both 缺 PDF 时降级为 html-only 且照常放行）
    _REQUIRED_ARTIFACTS = {
        "pdf": ("student.pdf", "teacher.pdf"),
        "html": ("student.html", "teacher.html"),
        "both": ("student.pdf", "teacher.pdf", "student.html", "teacher.html"),
    }
    missing_artifacts = [n for n in _REQUIRED_ARTIFACTS[fmt]
                         if not os.path.exists(os.path.join(pdir, n))]
    if missing_artifacts:
        print("[deliver] 交付格式 %s 要求产物 %s，缺失: %s；"
              "请先完成构建（不得以降级格式交付）。"
              % (fmt, ", ".join(_REQUIRED_ARTIFACTS[fmt]),
                 ", ".join(missing_artifacts)))
        return 2

    # 执行原子发布
    evidence_hashes = {
        "reports": {entry[1]: evidence.sha256_file(os.path.join(rdir, entry[1]))
                    for entry in PRE_CHECKS + CHECKS},
        "materials.json": evidence.sha256_file(os.path.join(pdir, ".pd", "materials.json")),
        "review-notes.json": evidence.sha256_file(os.path.join(pdir, ".pd", "review-notes.json")),
    }
    spec_f = os.path.join(pdir, ".pd", "confirmed-spec.json")
    if os.path.isfile(spec_f):
        evidence_hashes["confirmed-spec.json"] = evidence.sha256_file(spec_f)
    deliverables = []
    if os.path.isfile(os.path.join(pdir, "student.pdf")):
        deliverables.append({"file": "student.pdf", "audience": "student", "description": "学生自学与作答案（剥离解答与教师注记）"})
    if os.path.isfile(os.path.join(pdir, "teacher.pdf")):
        deliverables.append({"file": "teacher.pdf", "audience": "teacher", "description": "教师备课参考案（含完整解答与设计意图）"})
    if os.path.isfile(os.path.join(pdir, "classroom-plan.md")):
        deliverables.append({"file": "classroom-plan.md", "audience": "teacher", "description": "翻转课堂教师讲解方案与卡点回收表"})
    if os.path.isfile(os.path.join(pdir, "student.html")):
        deliverables.append({"file": "student.html", "audience": "student", "description": "学生版自适应探究卡片牌组"})
    if os.path.isfile(os.path.join(pdir, "teacher.html")):
        deliverables.append({"file": "teacher.html", "audience": "teacher", "description": "教师版卡片牌组（含对白与解答抽屉）"})

    manifest = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "deliver.py",
        "tool_version": TOOL_VERSION,
        "released": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input_digest": digest,
        "content_digest": digest,
        "output_format": fmt,
        "sections": secs,
        "pdf_sha256": pdf_sha,
        "pdfs": pdf_sha,
        "html_sha256": html_sha,
        "deliverables": deliverables,
        "status": "pass",
        "issues": [],
        "reports": {entry[1]: (reports.get(entry[1]) or {}).get("status")
                    for entry in PRE_CHECKS + CHECKS},
        "tool_versions": {(reports.get(entry[1]) or {}).get("tool", entry[0]):
                          (reports.get(entry[1]) or {}).get("tool_version")
                          for entry in PRE_CHECKS + CHECKS},
        "evidence": {"evidence_hashes": evidence_hashes}
    }

    # D1：干跑模式——全部校验通过后不发布，如实报告结论
    if args.dry_run:
        print("=" * 64)
        print("[deliver] 干跑模式（--dry-run）：全部门禁校验通过，"
              "未发布 release/（如需交付请去掉 --dry-run 重跑）。")
        print("=" * 64)
        return 0

    try:
        rel = atomic_release(pdir, manifest, fmt=fmt)
    except Exception as e:
        print("[deliver] 发布失败: %s" % e)
        return 2

    print("=" * 64)
    print("[deliver] 全部门禁核准通过！已发布至: %s" % rel)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
