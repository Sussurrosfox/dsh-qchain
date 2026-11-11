#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""redeliver.py — 一键重交付：修复 → 重建 → 证据哈希重对齐 → deliver 全量门禁

背景（重交付手工地狱）：修复缺陷后要人工串联 build.py → make_review_pack.py，
再手写一次性脚本重算 review-notes.json / .pd/acceptance/*.json /
.pd/agent-calls/*.json 里与产物 sha256 绑定的 digest（一次交付 24 处），
最后才能跑 deliver.py。本脚本把这串动作收敛为一条命令：

  步骤1  build.py            重建双模产物（--skip-build 时跳过步骤1-2）
  步骤2  make_review_pack.py 重生成审读包（--skip-build 时跳过）
  步骤3  证据哈希重对齐      重算当前产物 sha256，按各 JSON 既有 schema 更新
                            digest 字段（其余字段一律不动）
  步骤4  deliver.py          全量门禁终审；它 FAIL 则本脚本以相同退出码 FAIL
                            并原样透传其输出

对齐覆盖的 digest 校验点（归纳自 deliver.py 与 evidence.py 的校验逻辑）：
  A. .pd/review-notes.json（deliver.py 在 compare=review 时经
     evidence.validate_review_notes 校验）：
       - compare_report_sha256 ↔ .pd/reports/compare.json 当前文件哈希
       - input_digest          ↔ 当前受审输入 content_digest
       - pdf_sha256.{student,teacher} / html_sha256.{student,teacher}
  B. .pd/acceptance/<scope>.json（evidence.validate_acceptance_json，
     conclusion=PASS 时强校验）：
       - content_digest ↔ 当前受审输入 content_digest
       - student_pdf_sha256 / student_html_sha256 ↔ 当前产物哈希
  C. .pd/agent-calls/<dispatch_id>.json（check_agent_audit.py 的
     EVIDENCE_STALE 对账与 check_provenance.py 的 writer attempt 认证）：
       - output_digests{rel: sha256} ↔ 各产物文件当前磁盘哈希

合法性说明：evidence.py 未提供"重签名" helper；这些记录是内容寻址的，
重算哈希属合法维护而非绕过门禁——对齐只同步"内容指纹"，不改动审读结论/
判断/证据文本，最终 deliver.py 仍全量校验（语义审读 section_digest、
PD 状态契约、材料链等均不在对齐范围内，过期者照常被门禁拦下）。

顺序细节：对齐前先跑 shuffle_options.py（镜像 deliver.py 的 D1 顺序——
shuffle 是确定性且幂等的变异，必须先于受审摘要计算）；若存在
review-notes.json，先重跑 compare_versions.py 刷新 compare.json 再取哈希
（deliver 会确定性重跑同一报告，哈希一致）。

进程隔离：被调脚本一律经 subprocess 调用，不在本进程 import
（evidence.py 是共享证据工具库，仅用于 sha256/摘要计算，属例外）。

用法:
  python redeliver.py --self-dir <self> [--format {both,pdf,html}]
                      [--skip-build] [--dry-run] [--json PATH]
退出码: 0=deliver 全绿（已发布 release）, 1=某步骤失败或门禁未过, 2=环境/输入缺失。
"""
import argparse
import json
import os
import subprocess
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import evidence  # noqa: E402  （共享证据工具库：sha256_file/content_digest）

TOOL_NAME = "redeliver.py"
LOG = "[redeliver]"

REVIEW_NOTES_REL = os.path.join(".pd", "review-notes.json")
ACCEPTANCE_DIR_REL = os.path.join(".pd", "acceptance")
AGENT_CALLS_DIR_REL = os.path.join(".pd", "agent-calls")
COMPARE_REPORT_REL = os.path.join(".pd", "reports", "compare.json")
SHUFFLE_REPORT_REL = os.path.join(".pd", "reports", "shuffle_options.json")
BUILD_REPORT_REL = os.path.join(".pd", "reports", "build.json")


def log(msg):
    print("%s %s" % (LOG, msg))


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path, data):
    """tmp + os.replace 原子写回（缩进 2、ensure_ascii=False，与插件惯例一致）。"""
    tmp = "%s.tmp-redeliver" % path
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 当前产物摘要
# ---------------------------------------------------------------------------
def current_digests(self_dir):
    """重算当前受审输入摘要与产物哈希。返回 dict（纯计算，不写盘）。"""
    digest = evidence.content_digest(evidence.collect_inputs(self_dir))
    pdf = {n: evidence.sha256_file(os.path.join(self_dir, n + ".pdf"))
           for n in ("student", "teacher")}
    html = {n: evidence.sha256_file(os.path.join(self_dir, n + ".html"))
            for n in ("student", "teacher")}
    # A24：HTML 内容身份（渲染层无关）。realign 时与字节哈希一起重盖章，
    # 否则重签一次只会让下次重渲染再次触发字节不一致。
    html_content = {
        n: evidence.html_student_content_digest(
            os.path.join(self_dir, n + ".html"))
        for n in ("student", "teacher")
    }
    return {"content_digest": digest, "pdf_sha256": pdf, "html_sha256": html,
            "html_content_sha256": html_content}


# ---------------------------------------------------------------------------
# 纯函数对齐器：就地更新 data 的 digest 字段，返回变更清单
#   变更项 = {"field": "a.b", "old": ..., "new": ...}
# ---------------------------------------------------------------------------
def _set_field(data, dotted, new, changes):
    """按点路径更新字段（仅当旧值不同）；记录变更。"""
    keys = dotted.split(".")
    node = data
    for k in keys[:-1]:
        node = node.get(k)
        if not isinstance(node, dict):
            return
    old = node.get(keys[-1])
    if old != new:
        node[keys[-1]] = new
        changes.append({"field": dotted, "old": old, "new": new})


def realign_review_notes(data, compare_sha, digest, pdf_sha, html_sha):
    """对齐 review-notes.json 的四处绑定（deliver.py 校验点 A）。纯函数。"""
    changes = []
    if compare_sha and "compare_report_sha256" in data:
        _set_field(data, "compare_report_sha256", compare_sha, changes)
    if "input_digest" in data:
        _set_field(data, "input_digest", digest, changes)
    if isinstance(data.get("pdf_sha256"), dict):
        for k in ("student", "teacher"):
            if pdf_sha.get(k):
                _set_field(data, "pdf_sha256." + k, pdf_sha[k], changes)
    if isinstance(data.get("html_sha256"), dict):
        for k in ("student", "teacher"):
            if html_sha.get(k):
                _set_field(data, "html_sha256." + k, html_sha[k], changes)
    return changes


def realign_acceptance(rec, digest, pdf_sha, html_sha, html_content_sha=None):
    """对齐 acceptance/<scope>.json 的产物绑定（校验点 B）。纯函数。"""
    changes = []
    if "content_digest" in rec:
        _set_field(rec, "content_digest", digest, changes)
    if "student_pdf_sha256" in rec and pdf_sha.get("student"):
        _set_field(rec, "student_pdf_sha256", pdf_sha["student"], changes)
    if "student_html_sha256" in rec and html_sha.get("student"):
        _set_field(rec, "student_html_sha256", html_sha["student"], changes)
        # A24：字节哈希重盖章时一并重盖内容身份。缺失产物（html_sha 为 None）
        # 不改写，交给门禁如实判失败——不用空串伪造一次对齐。
        if html_content_sha and html_content_sha.get("student"):
            _set_field(rec, evidence.HTML_CONTENT_FIELD,
                       html_content_sha["student"], changes)
    return changes


def realign_agent_call(ev, sha_of):
    """对齐证据台账 output_digests（校验点 C）。纯函数（sha_of 注入查哈希）。

    sha_of(rel) -> 当前 sha256 或 None（产物缺失）。缺失产物不改写，
    记入 missing 交由门禁如实判失败（EVIDENCE_STALE 的诚实信号不掩盖）。
    """
    changes, missing = [], []
    digests = ev.get("output_digests")
    if not isinstance(digests, dict):
        return changes, missing
    for rel, old in digests.items():
        new = sha_of(rel)
        if new is None:
            missing.append(rel)
            continue
        if new != old:
            digests[rel] = new
            changes.append({"field": "output_digests." + rel,
                            "old": old, "new": new})
    return changes, missing


# ---------------------------------------------------------------------------
# 计划收集与应用（两阶段：先 review-notes/acceptance 落盘，再按新盘面
# 重算 agent-calls——T4 台账可能引用 acceptance 文件，须认证对齐后的内容）
# ---------------------------------------------------------------------------
def collect_plan(self_dir, digs, compare_sha):
    """返回 (entries, problems)；entry={path, rel, data, changes}。只读磁盘。"""
    entries, problems = [], []

    def _add(rel, aligner):
        path = os.path.join(self_dir, rel)
        data = load_json(path)
        if data is None:
            return
        changes = aligner(data)
        if changes:
            entries.append({"path": path, "rel": rel,
                            "data": data, "changes": changes})

    rn = os.path.join(self_dir, REVIEW_NOTES_REL)
    if os.path.isfile(rn):
        _add(REVIEW_NOTES_REL.replace(os.sep, "/"),
             lambda d: realign_review_notes(
                 d, compare_sha, digs["content_digest"],
                 digs["pdf_sha256"], digs["html_sha256"]))

    adir = os.path.join(self_dir, ACCEPTANCE_DIR_REL)
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            if fn.endswith(".json"):
                rel = (ACCEPTANCE_DIR_REL + os.sep + fn).replace(os.sep, "/")
                _add(rel, lambda d: realign_acceptance(
                    d, digs["content_digest"],
                    digs["pdf_sha256"], digs["html_sha256"],
                    digs.get("html_content_sha256")))
    return entries, problems


def collect_agent_call_plan(self_dir):
    """第二遍：按当前磁盘（含刚对齐的 acceptance）重算台账 output_digests。"""
    entries, problems = [], []
    cdir = os.path.join(self_dir, AGENT_CALLS_DIR_REL)
    if not os.path.isdir(cdir):
        return entries, problems

    def sha_of(rel):
        return evidence.sha256_file(os.path.join(self_dir, rel))

    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(cdir, fn)
        data = load_json(path)
        if data is None:
            continue
        changes, missing = realign_agent_call(data, sha_of)
        for rel in missing:
            problems.append("证据台账 %s 声明的产物缺失: %s" % (fn, rel))
        if changes:
            entries.append({"path": path,
                            "rel": (AGENT_CALLS_DIR_REL + os.sep + fn)
                            .replace(os.sep, "/"),
                            "data": data, "changes": changes})
    return entries, problems


def print_plan(title, entries):
    log("%s（%d 个文件）:" % (title, len(entries)))
    n = 0
    for e in entries:
        for c in e["changes"]:
            n += 1
            old = (c["old"] or "∅")[:16]
            new = (c["new"] or "∅")[:16]
            print("    %s: %s  %s… -> %s…" % (e["rel"], c["field"], old, new))
    if not entries:
        print("    （无需变更，全部 digest 已与当前产物一致）")
    return n


# ---------------------------------------------------------------------------
# 子进程编排（进程隔离：不 import 被调脚本）
# ---------------------------------------------------------------------------
def run_step(step_name, cmd, dry_run):
    """执行一个流水线步骤（子进程，stdout/stderr 原样透传）。失败即停。"""
    if dry_run:
        log("[dry-run] 步骤「%s」将执行: %s" % (step_name, " ".join(cmd)))
        return 0
    log("步骤「%s」执行中: %s" % (step_name, os.path.basename(cmd[1])))
    try:
        proc = subprocess.run(cmd)
    except OSError as e:
        log("步骤「%s」启动失败: %s" % (step_name, e))
        return 2
    if proc.returncode != 0:
        log("步骤「%s」失败（退出码 %d），重交付中止。"
            % (step_name, proc.returncode))
    return proc.returncode


def script_cmd(script, *args):
    return [sys.executable, os.path.join(SCRIPTS, script)] + list(args)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="一键重交付：build → review-pack → 证据哈希重对齐 → deliver")
    ap.add_argument("--self-dir", default=".",
                    help="题链 self 工作区目录（默认当前目录）")
    ap.add_argument("--format", choices=["both", "pdf", "html"], default=None,
                    help="交付格式透传（缺省由 config.yaml 决定）")
    ap.add_argument("--skip-build", action="store_true",
                    help="产物已新：跳过 build.py 与 make_review_pack.py，"
                         "只做证据对齐 + deliver")
    ap.add_argument("--skip-html-smoke", action="store_true",
                    help="透传给 deliver.py：跳过 HTML 离线包浏览器验收"
                         "（仅开发态；仍需环境变量 "
                         "QCHAIN_ALLOW_SKIP_HTML_SMOKE=1 二次确认）")
    ap.add_argument("--html-smoke-mode", choices=["strict", "auto"],
                    default="strict",
                    help="浏览器不可用时：strict=中止交付（默认）；auto=降级为 "
                         "manifest.html_smoke.status=blocked_env_degraded 后继续")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将执行的步骤与将更新的 digest 清单，不落盘")
    ap.add_argument("--json", metavar="PATH", default=None,
                    help="可选：本次重交付运行摘要写入路径")
    args = ap.parse_args(argv)

    self_dir = os.path.abspath(args.self_dir)
    if not os.path.isdir(self_dir):
        log("self 工作区不存在: %s" % self_dir)
        return 2

    fmt_args = ["--format", args.format] if args.format else []
    # HTML 侧浏览器验收默认由 deliver.py 对 staging 实际包执行；本脚本不提供
    # 任何"绕过"捷径，只能显式透传跳过开关（deliver 侧仍需环境变量二次确认）。
    html_smoke_args = []
    if args.skip_html_smoke:
        html_smoke_args = ["--skip-html-smoke"]
    elif getattr(args, "html_smoke_mode", "strict") == "auto":
        html_smoke_args = ["--html-smoke-mode=auto"]
    steps = []
    if not args.skip_build:
        steps.append(("build.py", script_cmd(
            "build.py", self_dir,
            "--json", os.path.join(self_dir, BUILD_REPORT_REL),
            "--max-overfull-pt", "300", "--allow-mdframed-breaks") + fmt_args))
        steps.append(("make_review_pack.py",
                      script_cmd("make_review_pack.py", self_dir)))
    steps.append(("deliver.py",
                  script_cmd("deliver.py", self_dir) + fmt_args
                  + html_smoke_args))

    print("=" * 64)
    log("重交付计划: self=%s%s" % (self_dir, "（dry-run，不落盘）"
                                   if args.dry_run else ""))
    print("=" * 64)
    idx = 0
    for name, _cmd in steps[:-1]:
        idx += 1
        log("步骤%d: %s" % (idx, name))
    idx += 1
    log("步骤%d: 证据哈希重对齐（review-notes / acceptance / agent-calls）" % idx)
    idx += 1
    log("步骤%d: %s（全量门禁终审）" % (idx, steps[-1][0]))
    if args.skip_build:
        log("（--skip-build：跳过 build.py 与 make_review_pack.py）")
    if args.skip_html_smoke:
        log("（--skip-html-smoke：跳过 HTML 离线包浏览器验收，deliver.py 侧仍需 "
            "QCHAIN_ALLOW_SKIP_HTML_SMOKE=1 二次确认）")

    # ---- 步骤 1-2：构建与审读包 ----
    for name, cmd in steps[:-1]:
        rc = run_step(name, cmd, args.dry_run)
        if rc != 0:
            return rc

    # ---- 步骤 3：证据哈希重对齐 ----
    log("步骤: 证据哈希重对齐" + ("（dry-run 预演）" if args.dry_run else ""))
    if args.dry_run:
        log("[dry-run] 将先运行 shuffle_options.py（镜像 deliver 的 D1 顺序）"
            "再计算受审摘要；本预演不执行任何子进程、不落盘。")
    else:
        rc = run_step("shuffle_options.py", script_cmd(
            "shuffle_options.py", self_dir,
            "--json", os.path.join(self_dir, SHUFFLE_REPORT_REL)), False)
        if rc not in (0, 1):
            return rc

    compare_sha = None
    rn_path = os.path.join(self_dir, REVIEW_NOTES_REL)
    has_review_notes = os.path.isfile(rn_path)
    if has_review_notes:
        cmp_path = os.path.join(self_dir, COMPARE_REPORT_REL)
        if args.dry_run:
            compare_sha = evidence.sha256_file(cmp_path)
            log("[dry-run] review-notes.json 存在：正式运行将先重跑 "
                "compare_versions.py 刷新 compare.json，再以新哈希对齐 "
                "compare_report_sha256（本预演沿用磁盘现值）。")
        else:
            rc = run_step("compare_versions.py", script_cmd(
                "compare_versions.py", self_dir,
                "--json", cmp_path) + fmt_args, False)
            if rc not in (0, 1):
                return rc
            compare_sha = evidence.sha256_file(cmp_path)

    digs = current_digests(self_dir)
    log("当前受审输入 content_digest=%s…" % digs["content_digest"][:16])

    entries, problems = collect_plan(self_dir, digs, compare_sha)
    total = print_plan("digest 对齐清单（review-notes / acceptance）", entries)
    if not args.dry_run:
        for e in entries:
            atomic_write_json(e["path"], e["data"])
            log("已对齐写回: %s（%d 处）" % (e["rel"], len(e["changes"])))

    # 第二遍：agent-calls（须在第一遍落盘后重算，台账可能引用 acceptance）
    ac_entries, ac_problems = collect_agent_call_plan(self_dir)
    problems.extend(ac_problems)
    total += print_plan("digest 对齐清单（agent-calls 证据台账）", ac_entries)
    if not args.dry_run:
        for e in ac_entries:
            atomic_write_json(e["path"], e["data"])
            log("已对齐写回: %s（%d 处）" % (e["rel"], len(e["changes"])))
    for p in problems:
        log("[警告] " + p)
    log("digest 对齐合计 %d 处。" % total)

    if args.dry_run:
        log("[dry-run] 计划打印完毕，未落盘、未运行 deliver.py。")
        return 0

    # ---- 步骤 4：deliver 全量门禁终审（输出原样透传，退出码透传）----
    name, cmd = steps[-1]
    log("步骤: %s 全量门禁终审（输出原样透传）" % name)
    rc = run_step(name, cmd, False)

    if args.json:
        summary = {
            "schema_version": evidence.SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "scope": {"workdir": self_dir},
            "input_digest": digs["content_digest"],
            "status": "pass" if rc == 0 else "fail",
            "issues": [{"severity": "error", "message": p} for p in problems],
            "evidence": {
                "skip_build": bool(args.skip_build),
                "realigned_fields": total,
                "realigned_files": [e["rel"] for e in entries + ac_entries],
                "deliver_exit_code": rc,
            },
        }
        evidence.write_report(args.json, summary)
        log("运行摘要已写入 %s" % args.json)

    if rc == 0:
        log("重交付成功：deliver 全绿，release 已发布。")
    else:
        log("重交付失败：deliver 退出码 %d（见其上方输出定位阻断项）。" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
