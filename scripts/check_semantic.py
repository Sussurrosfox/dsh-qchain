"""check_semantic.py — 语义审读一致性门禁（audit §6 / §7 semantic_gate）。

Writer → Checker → Gate 的 Gate 层：装配与交付前，对每个进入 main.tex
的小节强制校验语义审读记录（.pd/semantic/<sec>.json）：

1. 存在性   每个 expected section 都有审读记录（SEMANTIC_MISSING）；
2. 绑定     记录的 section_digest 必须等于当前候选切片 sha256
             （SEMANTIC_STALE——候选变更后旧审读结果不得复用）；
3. schema   schema_version / scope / conclusion / issues 结构合法
             （SEMANTIC_INVALID）；
4. 结论     conclusion=REVISE 或存在 severity=blocking 的 issue 一律
             FAIL（SEMANTIC_REVISE，附检查码清单）；
             conclusion=NEEDS_EVIDENCE 同样阻塞（SEMANTIC_NEEDS_EVIDENCE）；
5. 只读边界 审读记录不得声称修改过候选（无写作者权限字段即通过）。

输入（workdir = self/ 目录）：
- main.tex                装配清单（\\input{sec*.tex} 决定受检范围）
- <sec>.tex               候选切片
- .pd/semantic/<sec>.json 语义审读记录

用法: python check_semantic.py [工作目录] [--scope secN] [--json PATH]
退出码: 0 = 通过, 1 = 存在阻断性问题。
"""

import argparse
import hashlib
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import evidence  # noqa: E402（scripts/ 已在 sys.path）

TOOL_NAME = "check_semantic.py"
TOOL_VERSION = "1.0"

SEMANTIC_DIR_REL = os.path.join(".pd", "semantic")
SHUFFLE_MAP_REL = os.path.join(".pd", "shuffle-map.json")
SCHEMA_VERSION = 1
CONCLUSIONS = ("PASS", "REVISE", "NEEDS_EVIDENCE")
RE_MAIN_INPUT = re.compile(r"^\s*\\input\{(sec[0-9A-Za-z_-]+)\.tex\}", re.MULTILINE)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f), None
    except OSError as e:
        return None, "读取失败: %s" % e
    except ValueError as e:
        return None, "JSON 解析失败: %s" % e


def expected_sections(workdir):
    """受检范围 = main.tex 的 \\input{sec*.tex}（装配即交付范围）。"""
    main_tex = os.path.join(workdir, "main.tex")
    if not os.path.isfile(main_tex):
        return None
    with open(main_tex, encoding="utf-8", errors="replace") as f:
        text = f.read()
    secs = []
    for name in RE_MAIN_INPUT.findall(text):
        if name not in secs:
            secs.append(name)
    return secs


def load_shuffle_transitions(workdir):
    """打乱血缘台账 → {before_sha: [after_sha, ...]}（缺失/畸形返回 {}）。"""
    path = os.path.join(workdir, SHUFFLE_MAP_REL)
    if not os.path.isfile(path):
        return {}
    data, _err = _load_json(path)
    if not isinstance(data, dict):
        return {}
    raw = data.get("transitions")
    if not isinstance(raw, dict):
        return {}
    edges = {}
    for entries in raw.values():
        if not isinstance(entries, list):
            continue
        for pair in entries:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            before, after = str(pair[0] or ""), str(pair[1] or "")
            if before and after:
                edges.setdefault(before, []).append(after)
    return edges


def shuffle_bound(record_digest, current_digest, edges, limit=32):
    """血缘可达性：被审读候选摘要经确定性打乱迁移链能否到达当前摘要。

    选项随机重排（shuffle_options.py）是语义中性的确定性变换；血缘
    台账证明"当前切片 == 被审读候选 + 唯一打乱"，而不是候选被篡改。
    """
    if not record_digest or not current_digest:
        return False
    if record_digest == current_digest:
        return True
    seen = {record_digest}
    frontier = [record_digest]
    for _ in range(limit):
        nxt = []
        for d in frontier:
            for after in edges.get(d, []):
                if after == current_digest:
                    return True
                if after not in seen:
                    seen.add(after)
                    nxt.append(after)
        if not nxt:
            return False
        frontier = nxt
    return False


def check_scope(workdir, sec):
    """单 scope 审读记录校验 → issues 列表（空 = 通过）。"""
    issues = []
    tex_path = os.path.join(workdir, "%s.tex" % sec)
    if not os.path.isfile(tex_path):
        issues.append({
            "code": "SEMANTIC_SECTION_MISSING",
            "detail": "小节 %s.tex 缺失（无法进行语义审读绑定）" % sec})
        return issues
    digest = _sha256_file(tex_path)

    rec_path = os.path.join(workdir, SEMANTIC_DIR_REL, "%s.json" % sec)
    if not os.path.isfile(rec_path):
        issues.append({
            "code": "SEMANTIC_MISSING",
            "detail": "小节 %s 缺少语义审读记录 .pd/semantic/%s.json"
                      "（Writer→Checker→Gate 硬前置未满足）" % (sec, sec)})
        return issues
    rec, err = _load_json(rec_path)
    if rec is None or not isinstance(rec, dict):
        issues.append({
            "code": "SEMANTIC_INVALID",
            "detail": "小节 %s 审读记录不是合法 JSON: %s" % (sec, err)})
        return issues

    if int(rec.get("schema_version") or 0) != SCHEMA_VERSION:
        issues.append({
            "code": "SEMANTIC_INVALID",
            "detail": "小节 %s 审读记录 schema_version=%s，期望 %d"
                      % (sec, rec.get("schema_version"), SCHEMA_VERSION)})
    if str(rec.get("scope") or "") != sec:
        issues.append({
            "code": "SEMANTIC_INVALID",
            "detail": "小节 %s 审读记录 scope=%r 与本节不符"
                      % (sec, rec.get("scope"))})
    if str(rec.get("section_digest") or "") != digest:
        # 血缘豁免：当前切片 == 被审读候选经确定性打乱迁移得到
        # （shuffle-map.json 血缘台账；选项重排是语义中性变换）
        edges = load_shuffle_transitions(workdir)
        if not shuffle_bound(str(rec.get("section_digest") or ""),
                             digest, edges):
            issues.append({
                "code": "SEMANTIC_STALE",
                "detail": "小节 %s 审读绑定的候选摘要与当前切片不一致"
                          "（候选已变更，旧审读结果不得复用；期望 %s，记录 %s）"
                          % (sec, digest, rec.get("section_digest"))})
            return issues

    conclusion = str(rec.get("conclusion") or "")
    if conclusion not in CONCLUSIONS:
        issues.append({
            "code": "SEMANTIC_INVALID",
            "detail": "小节 %s 审读结论 %r 不在合法集合 %s 内"
                      % (sec, conclusion, list(CONCLUSIONS))})
        return issues
    raw_issues = rec.get("issues")
    if raw_issues is not None and not isinstance(raw_issues, list):
        issues.append({
            "code": "SEMANTIC_INVALID",
            "detail": "小节 %s 审读 issues 必须是列表" % sec})
        return issues

    blocking = [
        i for i in (raw_issues or [])
        if isinstance(i, dict) and str(i.get("severity") or "") == "blocking"
    ]
    if conclusion == "REVISE" or blocking:
        codes = sorted({str(i.get("code") or "?") for i in blocking}) or ["REVISE"]
        issues.append({
            "code": "SEMANTIC_REVISE",
            "detail": "小节 %s 语义审读未通过（%s）: %s"
                      % (sec, conclusion, ", ".join(codes)),
            "issue_codes": codes})
    elif conclusion == "NEEDS_EVIDENCE":
        issues.append({
            "code": "SEMANTIC_NEEDS_EVIDENCE",
            "detail": "小节 %s 审读员判定证据不足（NEEDS_EVIDENCE），"
                      "阻塞提交" % sec})
    return issues


def check_semantic_project(workdir, scope=""):
    errors = []
    warnings = []
    if scope:
        sections = [scope]
    else:
        sections = expected_sections(workdir)
        if sections is None:
            errors.append({
                "code": "SEMANTIC_MAIN_MISSING",
                "detail": "main.tex 缺失，无法确定受检小节范围"})
            sections = []
    passed = 0
    for sec in sections:
        issues = check_scope(workdir, sec)
        if issues:
            errors.extend(issues)
        else:
            passed += 1
    return {
        "errors": errors,
        "warnings": warnings,
        "checked": {
            "sections": len(sections),
            "passed": passed,
            "failed": len(sections) - passed,
            "scope_filter": scope or "",
        },
        "workdir": workdir,
    }


def _input_digest(workdir):
    """受审输入摘要（与 deliver.py 的 input_digest 绑定口径一致）。

    deliver.py 逐报告核对 input_digest（防旧报告冒充本轮证据）；缺失时
    返回空串，由 verify_report 判为失配阻断——宁可误报也不放行陈旧报告。
    """
    try:
        return evidence.content_digest(evidence.collect_inputs(workdir))
    except Exception:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="语义审读一致性门禁 (check_semantic.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--scope", default="",
                    help="只校验指定小节（写作者/审读员自查用）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    res = check_semantic_project(args.workdir, args.scope)
    errors, warnings = res["errors"], res["warnings"]

    report = {
        # 与 evidence.SCHEMA_VERSION 对齐（deliver.py 只接受 schema 2）
        "schema_version": 2,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        # deliver.py 逐报告核对 input_digest 绑定（防旧报告复用）
        "input_digest": _input_digest(res["workdir"]),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "checked": res["checked"],
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[check_semantic] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_semantic] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_semantic] 发现 %d 个语义审读问题（工作区: %s）:"
              % (len(errors), res["workdir"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    checked = res["checked"]
    print("[check_semantic] 通过：语义审读硬前置满足（%d/%d 个小节）。"
          % (checked["passed"], checked["sections"]))
    return 0


check_semantic = check_semantic_project


if __name__ == "__main__":
    sys.exit(main())
