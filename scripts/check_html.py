#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""
check_html.py — HTML 卡片交付物结构门禁（qchain-html 专用）

对构建产物 student.html / teacher.html 做结构与契约校验（不依赖浏览器）：
  1. 产物存在且非空；模版占位符全部填充（无 {{...}} 残留）；
  2. 卡片页集合与牌组一致：数量、id、qid 集合与 sec*.tex 源逐项对齐；
  3. 交互槽位契约：每个选项组至少 3 个选项、恰有一个 data-correct="true"；
  4. 版本纪律：
     - 学生版：所有参考揭示区必须带 hidden 属性；DOM 不得包含教学要点块、
       题型/难度标签；隐藏揭示区数量 == 源 solution 数；
     - 教师版：参考揭示区默认可见（无 hidden），数量 == 源 solution 数；
       教学要点块数量 == 源 teacherNote 数；
  5. 翻页/答案/目录交互控件齐备（按钮 id 与关键 JS 契约）；
  6. 图片资产：HTML 引用的 assets/* 均已生成。

用法: python check_html.py [工作目录] [--json PATH]
退出码: 0=通过, 1=发现问题, 2=缺少输入。
"""
import argparse
import json
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import tex2cards  # noqa: E402

RE_CARD = re.compile(r'<section class="qc-card" id="([^"]+)"([^>]*)>')
RE_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
RE_OPTION_GROUP = re.compile(
    r'<span class="qc-options"[^>]*>(.*?)</span></span>', re.S)
RE_OPTION = re.compile(r'<button[^>]*class="qc-option"[^>]*>')
RE_QID_SRC = re.compile(r"(?m)^\s*%\s*qid:\s*([^\s%|]+)")

REQUIRED_IDS = ("qc-btn-prev", "qc-btn-next", "qc-btn-answer",
                "qc-btn-toc", "qc-indicator", "qc-stage", "qc-progress")
REQUIRED_JS = ("qc-option", "data-correct", "hidden", "renderMathInElement")


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def check_file(html_path, deck, version, problems):
    """校验单个版本 HTML；问题追加到 problems。"""
    html = read(html_path)
    if html is None:
        problems.append("缺少 %s" % os.path.basename(html_path))
        return
    name = os.path.basename(html_path)

    # 1. 占位符
    left = RE_PLACEHOLDER.findall(html)
    if left:
        problems.append("%s: 存在未填充占位符 %s" % (name, sorted(set(left))))

    # 2. 卡片集合（HTML id 带 qc-card- 前缀）
    cards = RE_CARD.findall(html)
    ids = [c[0].replace("qc-card-", "", 1) for c in cards]
    deck_ids = [c["id"] for c in deck.get("cards", [])]
    if ids != deck_ids:
        problems.append("%s: 卡片页与牌组不一致（HTML %d 页 / 牌组 %d 卡）"
                        % (name, len(ids), len(deck_ids)))
    qid_attr = {}
    for cid, attrs in cards:
        m = re.search(r'data-qid="([^"]*)"', attrs)
        qid_attr[cid] = m.group(1) if m else ""
    deck_qids = [c.get("qid") for c in deck.get("cards", [])
                 if c["kind"] == "question"]
    html_qids = [q for q in qid_attr.values() if q]
    if html_qids != deck_qids:
        problems.append("%s: 题目 qid 序列与牌组不一致" % name)

    # 3. 槽位契约
    n_groups = 0
    for m in RE_OPTION_GROUP.finditer(html):
        n_groups += 1
        body = m.group(1)
        n_opt = len(RE_OPTION.findall(body))
        n_correct = len(re.findall(r'data-correct="true"', body))
        if n_opt < 3:
            problems.append("%s: 选项组不足 3 个选项（%d 个）" % (name, n_opt))
        if n_correct != 1:
            problems.append("%s: 选项组 data-correct=true 数量为 %d（应为 1）"
                            % (name, n_correct))
    deck_slots = sum(len(c.get("slots", []))
                     for c in deck.get("cards", []))
    if n_groups != deck_slots:
        problems.append("%s: 选项组 %d 个与牌组槽位 %d 个不一致"
                        % (name, n_groups, deck_slots))

    # 4. 版本纪律
    n_sol = sum(1 for c in deck.get("cards", [])
                if c.get("solution_html"))
    n_note = sum(1 for c in deck.get("cards", [])
                 if c.get("teacher_note_html"))
    reveal_total = len(re.findall(r'class="qc-reveal qc-solution"', html))
    reveal_hidden = len(re.findall(r'class="qc-reveal qc-solution" hidden',
                                   html))
    note_blocks = len(re.findall(r'class="qc-teacher-note"', html))
    meta_blocks = len(re.findall(r'class="qc-card-meta">', html))
    if version == "student":
        if reveal_total != reveal_hidden:
            problems.append("%s: 学生版有 %d/%d 个参考揭示区未隐藏"
                            % (name, reveal_total - reveal_hidden,
                               reveal_total))
        if reveal_total != n_sol:
            problems.append("%s: 学生版揭示区 %d 个与源 solution %d 个不一致"
                            % (name, reveal_total, n_sol))
        if note_blocks:
            problems.append("%s: 学生版 DOM 含 %d 处教学要点块（教师专属内容"
                            "不得进入学生版）" % (name, note_blocks))
        if meta_blocks:
            problems.append("%s: 学生版含 %d 处题型/难度标签（应隐藏）"
                            % (name, meta_blocks))
    else:
        if reveal_total != n_sol or reveal_hidden != 0:
            problems.append("%s: 教师版可见参考解答 %d/%d 与源 solution %d "
                            "不一致" % (name, reveal_total - reveal_hidden,
                                        reveal_total, n_sol))
        if note_blocks != n_note:
            problems.append("%s: 教师版教学要点块 %d 个与源 teacherNote %d 个"
                            "不一致" % (name, note_blocks, n_note))

    # 5. 交互控件契约
    for rid in REQUIRED_IDS:
        if ('id="%s"' % rid) not in html:
            problems.append("%s: 缺少交互控件 #%s" % (name, rid))
    for token in REQUIRED_JS:
        if token not in html:
            problems.append("%s: 缺少交互契约 %r" % (name, token))
    if 'data-version="%s"' % version not in html:
        problems.append("%s: data-version 不是 %s" % (name, version))

    # 6. 图片资产
    outdir = os.path.dirname(os.path.abspath(html_path))
    for src in re.findall(r'src="assets/([^"]+)"', html):
        if not os.path.exists(os.path.join(outdir, "assets", src)):
            problems.append("%s: 引用图片资产缺失 assets/%s" % (name, src))


def main(argv=None):
    ap = argparse.ArgumentParser(description="HTML 卡片交付物结构门禁")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查结果")
    args = ap.parse_args(argv)
    workdir = os.path.abspath(args.workdir)

    deck, _issues = tex2cards.parse_project(workdir)
    problems = []
    for version in ("student", "teacher"):
        check_file(os.path.join(workdir, version + ".html"), deck, version,
                   problems)

    report = {"schema_version": evidence.SCHEMA_VERSION,
              "tool": "check_html.py", "tool_version": TOOL_VERSION,
              "scope": {"workdir": workdir},
              "input_digest": evidence.content_digest(
                  evidence.collect_inputs(workdir)),
              "html_sha256": {
                  v: evidence.sha256_file(os.path.join(workdir,
                                                       v + ".html"))
                  for v in ("student", "teacher")},
              "status": "pass", "issues": [], "evidence": {}}

    if not deck.get("cards"):
        problems.append("牌组为空（未发现任何卡片）")

    for p in problems:
        print("[check_html] [问题] %s" % p)
    if problems:
        print("[check_html] 发现 %d 个问题:" % len(problems))
        report["status"] = "fail"
        report["issues"] = [{"severity": "error", "message": p}
                            for p in problems]
    else:
        print("[check_html] 通过：双版 HTML 结构、槽位契约与版本纪律全部"
              "满足（%d 张卡片）。" % len(deck.get("cards", [])))
    if args.json:
        evidence.write_report(args.json, report)
        print("[check_html] 结构化报告已写入 %s" % args.json)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
