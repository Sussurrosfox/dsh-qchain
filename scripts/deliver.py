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
   5.5 check_render.py 渲染质量门禁（HTML 乱码/源码泄漏/数学段损坏/选项汉字
      丢失、PDF 缺字/空选项组——结构门禁覆盖不到的渲染级失败，须在 build 之后）；
  6. 核对教学验收（acceptance/<scope>.json）、材料链（materials.json）与疑似项处置（review-notes.json）；
  7. 渲染冒烟硬门槛（render_smoke.py：首页 + 选项页渲染 PNG，与门禁全绿并列，
     缺一不交付；--skip-smoke 可显式跳过并须在交付说明记录原因）；
  8. 全部通过后原子发布至 release/ 目录。

用法: python deliver.py <project> [--skip-checks] [--format {both,pdf,html}]
      [--skip-smoke] [--dry-run]
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
import asset_bundle  # noqa: E402
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
    # §7 scope_gate：生效配置与 Phase 0 冻结合同零漂移（模式/课时/策略/
    # 范围变更/节数规划），任何未经人工确认的范围变更在此阻断
    ("check_scope.py", "check_scope.json"),
    ("check_pedagogy.py", "check_pedagogy.json"),
    ("check_dialogue.py", "check_dialogue.json"),
    ("check_fidelity.py", "check_fidelity.json"),
    ("check_semantic.py", "check_semantic.json"),
    ("build.py", "build.json", ["--max-overfull-pt", "300", "--allow-mdframed-breaks", "--max-underfull-teacher", "20"]),
    # §19 渲染质量门禁（audit：GR.4 交付后人工发现 HTML 乱码/PDF 选项空白，
    # 而彼时全部结构门禁均 PASS）。必须在 build 之后：查的是渲染产物本身
    # （*.html / *.pdf / *.log），{fmt} 在运行期替换为实际交付格式。
    ("check_render.py", "check_render.json", ["--format", "{fmt}"]),
    ("check_budget.py", "check_budget.json", ["--stage", "delivery"]),
    ("compare_versions.py", "compare.json"),
    ("check_agent_audit.py", "check_agent_audit.json"),
    # §7 provenance_gate：每节可回溯 writer attempt（认证当前内容摘要）/
    # checker 结果 / qid 规划登记，写域白名单外路径一律拒绝
    ("check_provenance.py", "check_provenance.json"),
]
ALL_CHECK_REPORTS = [e[1] for e in PRE_CHECKS] + [e[1] for e in CHECKS]
# 教学语义、对话层规范、来源忠实性与预算教案完备性门禁同样是一票否决项；
# §6 语义审读门禁（Writer→Checker→Gate）一票否决：缺审读记录/摘要过期/
# 存在 blocking issue 的小节不得交付；
# §7 scope_gate 与 provenance_gate 一票否决：范围/预算漂移或血缘链断裂
# （无 writer attempt 认证当前切片、checker 缺/过期、qid 未登记）不得交付；
# 多 Agent 证据链审计（q_audit.md）同为一票否决：派发单无证据台账
# （主 Agent 单会话直写嫌疑）/ 证据失配 / 创审分离违规一律拒绝发布。
# §19 渲染质量门禁（check_render.py）同为一票否决：HTML 双重转义/源码泄漏/
# 数学段 $ 不成对/选项汉字丢失/KaTeX 不自足、PDF 缺字/空选项组一律拒绝发布。
# §4.4.1 渲染冒烟硬门槛（render_smoke.py）与门禁全绿**并列**，缺一不交付：
# 结构门禁全绿 ≠ 排版正确（曾出现 PDF 选项整段空白而 17 项门禁全绿），
# 首页 + 选项页必须真渲染出 PNG 供真人过目（--skip-smoke 显式跳过并留痕）。
MUST_PASS = ("shuffle_options.json", "check_numbering.json",
             "check_scope.json",
             "check_pedagogy.json", "check_dialogue.json",
             "check_fidelity.json", "check_semantic.json",
             "check_budget.json", "build.json", "check_render.json",
             "check_agent_audit.json", "check_provenance.json")

#: 渲染冒烟硬门槛的盘面证据路径（与 render_smoke.py / make_review_pack.py 约定一致）
SMOKE_REPORT_REL = os.path.join(".pd", "reports", "render_smoke.json")
SMOKE_MANIFEST_REL = os.path.join(".pd", "review", "smoke",
                                  "smoke-manifest.json")
#: 跳过冒烟硬门槛时的可操作提示（显式跳过必须留痕）
SMOKE_SKIP_HINT = ("如确认本机缺 PyMuPDF(fitz)/pdftoppm 等渲染工具，可显式 "
                   "--skip-smoke 跳过，但必须在交付说明中记录跳过原因")

# ---------------------------------------------------------------------------
# HTML 离线交付包浏览器验收（契约 C8 / 任务卡 04）
# ---------------------------------------------------------------------------
#: HTML 侧验收证据路径（与 html_smoke.py 约定一致）
HTML_SMOKE_REPORT_REL = os.path.join(".pd", "reports", "html_smoke.json")
#: 显式跳过 HTML 侧验收所需的环境变量确认
HTML_SMOKE_SKIP_ENV = "QCHAIN_ALLOW_SKIP_HTML_SMOKE"
HTML_SMOKE_SKIP_HINT = (
    "HTML 侧验收用本机真实 Chrome/Chromium 打开包内 student.html/teacher.html"
    "（file:// 断网、逐卡 desktop 与 390px、逐节点公式状态）。开发态确需跳过时"
    "设置 %s=1 并加 --skip-html-smoke；正式交付不得跳过。" % HTML_SMOKE_SKIP_ENV)


class HtmlSmokeError(Exception):
    """HTML 离线交付包浏览器验收未通过（含浏览器不可用的 BLOCKED_ENV）。

    发布必须中止：由 atomic_release 的 except 分支清 staging，旧 release 不动。
    """



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


def smoke_gate_status(self_dir, fmt, skip=False):
    r"""渲染冒烟硬门槛的纯函数判定（复盘 §4.4.1）。返回 (ok, reason)。

    与 MUST_PASS 门禁全绿并列，缺一不交付：
      - skip=True              → (True,  已跳过…须记录原因)
      - fmt 不含 PDF（html）   → (True,  跳过…HTML 侧冒烟另论，不在本次范围）
      - 报告缺失               → (False, …)
      - 报告 status != "pass"  → (False, …)
      - smoke-manifest 缺失    → (False, …)
      - png_count < 1          → (False, …)
      - 其余                   → (True,  通过…PNG N 张）
    只读盘面文件，不跑子进程，便于单测。
    """
    if skip:
        return True, ("已跳过渲染冒烟硬门槛（--skip-smoke）：须在交付说明中"
                      "记录跳过原因")
    if fmt not in ("pdf", "both"):
        return True, ("交付格式 %s 不含 PDF：跳过渲染冒烟硬门槛"
                      "（HTML 侧冒烟另论，不在本次范围）" % fmt)

    rep = load_json(os.path.join(self_dir, SMOKE_REPORT_REL))
    if rep is None:
        return False, ("缺少渲染冒烟报告 %s：请先运行 "
                       "python scripts/render_smoke.py <self>"
                       "（或 scripts/make_review_pack.py <self> 自动联动）"
                       % SMOKE_REPORT_REL.replace(os.sep, "/"))
    if rep.get("status") != "pass":
        issues = rep.get("issues") or []
        msgs = "; ".join(str(i.get("message", "")) for i in issues).strip()
        return False, ("渲染冒烟报告 status=%s（%d 项问题）%s；详见 %s"
                       % (rep.get("status"), len(issues),
                          ("：" + msgs[:200]) if msgs else "",
                          SMOKE_REPORT_REL.replace(os.sep, "/")))

    man = load_json(os.path.join(self_dir, SMOKE_MANIFEST_REL))
    if man is None:
        return False, ("缺少冒烟截图清单 %s：请先运行 "
                       "python scripts/render_smoke.py <self>"
                       % SMOKE_MANIFEST_REL.replace(os.sep, "/"))
    png_count = man.get("png_count")
    if not isinstance(png_count, int):
        png_count = len(man.get("items") or [])
    if png_count < 1:
        return False, ("冒烟截图清单 %s 的 png_count=%d（未产出任何 PNG）："
                       "请检查 PDF 是否为空或损坏"
                       % (SMOKE_MANIFEST_REL.replace(os.sep, "/"), png_count))
    return True, ("渲染冒烟硬门槛通过（%s，PNG %d 张）；真人须过目 "
                  ".pd/review/smoke/ 截图后方可交付"
                  % (SMOKE_REPORT_REL.replace(os.sep, "/"), png_count))


def run_smoke_check(pdir, fmt):
    """构建后执行渲染冒烟（子进程，--pdfs 按实际存在的 PDF）。返回 (ok, reason)。

    只负责"跑起来"；硬校验由 smoke_gate_status 按盘面证据判定
    （退出码 1 = 有问题，交由硬门槛报告为失败）。
    """
    pdfs = [n for n in ("student", "teacher")
            if os.path.isfile(os.path.join(pdir, n + ".pdf"))]
    if not pdfs:
        return False, ("交付格式 %s 要求 PDF，但 self 目录未找到 "
                       "student.pdf/teacher.pdf（无法冒烟）" % fmt)
    cmd = [sys.executable, os.path.join(SCRIPTS, "render_smoke.py"),
           "--self-dir", pdir,
           "--format", "json", "--pdfs", ",".join(pdfs)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return False, "render_smoke.py 执行异常: %s" % e
    if proc.returncode not in (0, 1):
        return False, ("render_smoke.py 执行异常 (退出码 %d): %s"
                       % (proc.returncode,
                          (proc.stderr or proc.stdout or "").strip()[:200]))
    return True, ("render_smoke.py 已完成（退出码 %d，pdfs=%s）"
                  % (proc.returncode, ",".join(pdfs)))


def resolve_html_smoke_skip(skip_html_smoke, env=None):
    """解析 HTML 侧冒烟跳过策略。返回 (skip, reason)。

    skip is False → 正常执行；
    skip is True  → 已用环境变量显式确认跳过（开发态）；
    skip is None  → **被拒绝**：既没跑也没得到确认，调用方必须中止。

    注意：``--skip-smoke`` 只豁免 PDF 侧（render_smoke.py），**不**自动豁免
    HTML 侧——两者是两件不同的事，HTML 包必须用真实浏览器验收。
    """
    env = os.environ if env is None else env
    if not skip_html_smoke:
        return False, ("HTML 侧验收默认执行：对 staging 实际包做 file:// 断网"
                       "浏览器验收（html/both 格式），--skip-smoke 不豁免此门禁")
    if env.get(HTML_SMOKE_SKIP_ENV) != "1":
        return None, ("拒绝执行：--skip-html-smoke 需设置环境变量 %s=1 显式确认"
                      "（正式交付不得跳过 HTML 侧浏览器验收）。"
                      % HTML_SMOKE_SKIP_ENV)
    return True, ("[记录] 本次交付跳过 HTML 侧浏览器验收（--skip-html-smoke）"
                  "——跳过原因必须写入交付说明")


def run_html_smoke_check(package_dir, report_path=None, runner=None):
    """对**实际交付包目录**做 HTML 浏览器验收，返回 (ok, reason)。

    验收对象必须是 staging/release 里那份包（写完 manifest 之后），不是工作区
    原版 HTML；浏览器不可用时返回 ok=False（BLOCKED_ENV 绝不等于通过）。
    """
    if runner is None:
        try:
            import html_smoke
        except Exception as exc:  # noqa: BLE001
            return False, "无法导入 scripts/html_smoke.py：%s" % exc
        runner = html_smoke.run_smoke
    try:
        report, code = runner(package_dir, report_path)
    except Exception as exc:  # noqa: BLE001
        return False, "HTML 冒烟执行异常：%s" % exc
    report = report or {}
    status = report.get("status")
    evidence_blob = report.get("evidence") or {}
    if code == 0 and status == "pass":
        counts = evidence_blob.get("counts") or {}
        return True, (
            "HTML 离线包浏览器验收通过（%s：卡片 %s / 公式节点 %s（ok %s）"
            "/ 目录点击 %s / 选项组 %s / 图片 %s）"
            % (os.path.basename(package_dir), counts.get("cards"),
               counts.get("math_nodes"), counts.get("math_ok"),
               counts.get("toc_clicked"), counts.get("option_groups_clicked"),
               counts.get("images")))
    if status == "blocked_env":
        dep = evidence_blob.get("dependency") or {}
        return False, ("HTML 离线包验收无法进行（BLOCKED_ENV）：%s"
                       % (dep.get("reason") or "本机无可用浏览器"))
    problems = [i.get("message") for i in (report.get("issues") or [])]
    return False, ("HTML 离线包浏览器验收未通过（%d 项问题）：%s"
                   % (len(problems), "; ".join(problems[:6])))


def _agent_calls_digest(pdir):
    """.pd/agent-calls/ 证据台账目录摘要（q_audit.md 多 Agent 证据链）。

    对全部证据文件按文件名排序后逐文件计算 sha256，汇总为确定性目录摘要；
    无台账时返回 None（不写入 manifest，保持旧版交付清单形态）。
    """
    calls_dir = os.path.join(pdir, ".pd", "agent-calls")
    if not os.path.isdir(calls_dir):
        return None
    lines = []
    for name in sorted(os.listdir(calls_dir)):
        path = os.path.join(calls_dir, name)
        if not os.path.isfile(path) or not name.endswith(".json"):
            continue
        lines.append("%s:%s" % (name, evidence.sha256_file(path) or ""))
    if not lines:
        return None
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def atomic_release(pdir, manifest, fmt="both", html_smoke=None):
    """先写临时目录，全部成功后原子切换 release。

    发布顺序（契约 C8 固定）：构建 staging → 写 manifest.json → 对 staging 做
    格式对应的完整性/浏览器验收 → 成功才把旧 release 改名 backup → staging
    改名 release → 清理 backup。验收失败保留原 release，不吞错。

    html_smoke:
        None     用随包 scripts/html_smoke.py 做真实浏览器验收（发布路径默认）
        callable 自定义验收器 f(package_dir, report_path) -> (report, exit_code)
        False    显式跳过（仅限资源/回滚类测试与开发态，调用方自负）
    """
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

        # 多 Agent 证据链台账（q_audit.md）：.pd/agent-calls/<dispatch_id>.json
        # 随交付物发布至 release/agents/，使"谁写了什么"可被第三方复核
        agents_src = os.path.join(pdir, ".pd", "agent-calls")
        if os.path.isdir(agents_src):
            ledger = sorted(f for f in os.listdir(agents_src)
                            if f.endswith(".json")
                            and os.path.isfile(os.path.join(agents_src, f)))
            if ledger:
                agents_dst = os.path.join(staging, "agents")
                os.makedirs(agents_dst)
                for name in ledger:
                    shutil.copy2(os.path.join(agents_src, name),
                                 os.path.join(agents_dst, name))

        # 契约 C8：release 自足——HTML 所需 katex/ 整树与被引用的 assets/ 一并入
        # staging，并在切换前校验（缺失/越界/外网/篡改 → 抛 BundleError，
        # 由下方 except 清理 staging，旧 release 原样保留）
        manifest = dict(manifest)
        if fmt in ("html", "both"):
            sources = asset_bundle.stage_html_resources(
                pdir, staging, katex_expected=render_html.KATEX_SRC)
            if sources:
                manifest["asset_sources"] = sources
        # manifest.json 写在最后；files 清单覆盖其余全部实际文件（不含自身）
        manifest["files"] = asset_bundle.build_file_manifest(staging)
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 契约 C8 发布顺序：manifest 已写 → 对 staging 实际包做浏览器验收 →
        # 验收通过才允许切换。资源/公式/交互/布局任一失败或浏览器不可用都抛错，
        # 由下方 except 清 staging，旧 release 完全不动（绝不吞错继续发布）。
        if fmt in ("html", "both") and html_smoke is not False:
            report_path = os.path.join(pdir, HTML_SMOKE_REPORT_REL)
            print("[deliver] 对 staging 实际包做 HTML 离线验收: %s"
                  % os.path.basename(staging))
            ok, reason = run_html_smoke_check(staging, report_path,
                                             runner=html_smoke or None)
            print("[deliver]   %s" % reason)
            if not ok:
                raise HtmlSmokeError(reason)
            manifest["html_smoke"] = {
                "status": "pass",
                "report": HTML_SMOKE_REPORT_REL.replace(os.sep, "/"),
                "note": reason,
            }
            ev = manifest.setdefault("evidence", {})
            hashes = ev.setdefault("evidence_hashes", {})
            reports = hashes.setdefault("reports", {})
            if os.path.isfile(report_path):
                reports["html_smoke.json"] = evidence.sha256_file(report_path)
            # manifest 自身不在 files 清单内，验收结果回写不破坏包内哈希一致性
            with open(os.path.join(staging, "manifest.json"), "w",
                      encoding="utf-8") as f:
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


#: report 文件名 → 生成它的脚本（阻断文案用，避免报错只说报告名）
SCRIPT_BY_REPORT = {entry[1]: entry[0] for entry in PRE_CHECKS + CHECKS}


def must_pass_blockers(reports):
    """卡15（C6）：MUST_PASS 门禁逐项裁决——**只有 status=="pass" 才放行**。

    这层闸门独立于执行图：faithful_fast 图把逐节语义审读改成「构建前只做确定性
    检查 + 合并终审统一审读」，构建前那一趟语义报告记 status="pending_review"。
    pending_review 既不是通过（不得交付），也不是普通失败（不是让作者重写），
    所以单独给出明确阻断文案；MUST_PASS 清单一项不少，legacy 图语义不变。
    """
    blockers = []
    for report_name in MUST_PASS:
        rep = reports.get(report_name)
        status = (rep or {}).get("status")
        if status == "pass":
            continue
        if status == "pending_review":
            blockers.append(
                "%s 仍为 pending_review（最终审读尚未通过），"
                "交付要求 status=pass" % report_name)
            continue
        script = SCRIPT_BY_REPORT.get(report_name, report_name)
        blockers.append("%s 未通过 (status=%s, issues=%d)"
                        % (script, status, len((rep or {}).get("issues", []))))
    return blockers


def main():
    ap = argparse.ArgumentParser(description="交付汇总门禁")
    ap.add_argument("project")
    ap.add_argument("--skip-checks", action="store_true",
                    help="复用已有报告（危险：需同时设置环境变量 "
                         "QCHAIN_ALLOW_SKIP_CHECKS=1 才生效）")
    ap.add_argument("--format", choices=["both", "pdf", "html"], default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="干跑模式：执行全部门禁校验并输出结论，但不发布 release/")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="跳过 PDF 侧渲染冒烟硬门槛（不推荐；需在交付说明记录"
                         "原因；**不**豁免 HTML 侧浏览器验收）")
    ap.add_argument("--skip-html-smoke", action="store_true",
                    help="跳过 HTML 侧离线包浏览器验收（仅开发态；需同时设置 "
                         "环境变量 QCHAIN_ALLOW_SKIP_HTML_SMOKE=1）")
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

    # D2：HTML 侧浏览器验收默认执行；显式跳过需环境变量二次确认（--skip-smoke
    # 只豁免 PDF 侧，不自动豁免 HTML 侧——两者是两件不同的事）
    skip_html_smoke, html_smoke_note = resolve_html_smoke_skip(
        args.skip_html_smoke)
    if skip_html_smoke is None:
        print("[deliver] %s" % html_smoke_note)
        return 2

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
        if extra_args:
            extra_args = [a.replace("{fmt}", fmt) for a in extra_args]
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

    # 卡15（C6）：MUST_PASS 一票否决（status 必须为 pass；pending_review 显式阻断）
    blockers.extend(must_pass_blockers(reports))

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

    # 渲染冒烟硬门槛（复盘 §4.4.1）：与门禁全绿并列，缺一不交付。
    # 位置：全部门禁（含 build）跑完、产物存在性核对之后——只对真实存在
    # 的 PDF 冒烟，也不把渲染浪费在已知失败的门禁上。html-only 无 PDF，
    # 跳过该门槛（HTML 侧冒烟另论，不在本次范围）。
    smoke_skipped = bool(args.skip_smoke or fmt not in ("pdf", "both"))
    if not smoke_skipped:
        print("[deliver] 运行渲染冒烟硬门槛: render_smoke.py ...")
        ran_ok, ran_reason = run_smoke_check(pdir, fmt)
        print("[deliver]   %s" % ran_reason)
        if not ran_ok:
            print("[deliver] 渲染冒烟无法执行: %s" % ran_reason)
            print("  提示: %s。" % SMOKE_SKIP_HINT)
            return 1
    smoke_ok, smoke_reason = smoke_gate_status(pdir, fmt, skip=args.skip_smoke)
    if not smoke_ok:
        print("[deliver] 渲染冒烟硬门槛未通过: %s" % smoke_reason)
        print("  提示: 修复后运行 python scripts/render_smoke.py <self>"
              "（或 scripts/make_review_pack.py <self> 自动联动）重跑本门槛；"
              "%s。" % SMOKE_SKIP_HINT)
        return 1
    print("[deliver] 渲染冒烟硬门槛: %s" % smoke_reason)
    if args.skip_smoke:
        print("[deliver] [记录] 本次交付跳过渲染冒烟硬门槛（--skip-smoke）"
              "——跳过原因必须写入交付说明。")
    smoke_status = "skipped" if smoke_skipped else "pass"
    smoke_png_count = None
    if not smoke_skipped:
        smoke_png_count = ((load_json(os.path.join(pdir, SMOKE_REPORT_REL)) or {})
                           .get("evidence") or {}).get("png_count")

    # HTML 侧离线包浏览器验收（任务卡 04）：PDF 冒烟管不到 HTML 资源是否随包
    # 落地，必须在 staging 写完 manifest 之后用真实浏览器打开包内页面验收。
    print("[deliver] HTML 侧验收策略: %s" % html_smoke_note)

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
    # 渲染冒烟硬门槛证据（复盘 §4.4.1）：报告哈希随交付物留痕，跳过时无文件
    smoke_report_path = os.path.join(rdir, "render_smoke.json")
    if os.path.isfile(smoke_report_path):
        evidence_hashes["reports"]["render_smoke.json"] = \
            evidence.sha256_file(smoke_report_path)
    # 多 Agent 证据链台账目录摘要（q_audit.md）：.pd/agent-calls/ 全量文件哈希
    agent_calls_digest = _agent_calls_digest(pdir)
    if agent_calls_digest:
        evidence_hashes["agent-calls"] = agent_calls_digest
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
    if os.path.isdir(os.path.join(pdir, ".pd", "agent-calls")) and \
            any(f.endswith(".json") for f in os.listdir(os.path.join(pdir, ".pd", "agent-calls"))):
        deliverables.append({"file": "agents/", "audience": "auditor", "description": "多 Agent 派发证据台账（每次子 Agent 派发的 worker 身份与产物 sha256，q_audit.md 证据链）"})

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
        "smoke": {
            "status": smoke_status,            # pass / skipped
            "skipped": smoke_skipped,
            "png_count": smoke_png_count,
            "report": SMOKE_REPORT_REL.replace(os.sep, "/"),
            "note": smoke_reason,
        },
        # HTML 侧浏览器验收（html/both）：由 atomic_release 在 staging 上实际
        # 执行后回填 status=pass（含报告路径与统计）；跳过时如实记 skipped。
        "html_smoke": {
            "status": ("skipped" if skip_html_smoke else
                       ("not_applicable" if fmt not in ("html", "both")
                        else "pending")),
            "report": HTML_SMOKE_REPORT_REL.replace(os.sep, "/"),
            "note": html_smoke_note,
        },
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
        rel = atomic_release(pdir, manifest, fmt=fmt,
                             html_smoke=(False if skip_html_smoke else None))
    except HtmlSmokeError as e:
        print("[deliver] 发布中止（HTML 离线包浏览器验收未通过）: %s" % e)
        print("  提示: %s" % HTML_SMOKE_SKIP_HINT)
        return 2
    except Exception as e:
        print("[deliver] 发布失败: %s" % e)
        return 2

    print("=" * 64)
    print("[deliver] 全部门禁核准通过！已发布至: %s" % rel)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
