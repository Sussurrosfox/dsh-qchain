"""check_provenance.py — 派发血缘门禁（audit §7 provenance_gate）。

背景：审计要求「每 section/qid 可回溯 source claim / planner request /
writer attempt / checker result」，且旧结果不得因文件存在而被复用。
本门禁把交付前的血缘链钉死：

  PROV_WRITER_MISSING          该小节没有任何写作派发单
  PROV_WRITER_DIGEST_MISSING   有派发单，但无一条证据台账认证当前切片内容
                               （产物被改写/旧 attempt 冒充当前候选）
  PROV_CHECKER_MISSING         .pd/semantic/<sec>.json 缺失
  PROV_CHECKER_INVALID         schema/scope 非法
  PROV_CHECKER_STALE           审读绑定摘要与当前切片不符（血缘台账也无效）
  PROV_CHECKER_REVISE          存在 blocking issue（不得交付）
  PROV_CHECKER_NEEDS_EVIDENCE  审读结论为证据不足
  PROV_PATH_OUTSIDE_ALLOWLIST  证据台账写入了写域白名单之外的路径
  PROV_QID_UNPLANNED           正文 qid 未登记于 coverage-map.md

写域白名单（audit §5.2）：子 Agent 只能写自己的 secN.tex 候选与 .pd/ 下
的派发/证据/报告文件；main.tex / student.tex / teacher.tex / 其他 section /
release/ 一律拒绝。

降级口径：整个工程没有任何派发单（.pd/agent-requests/ 为空）时，写作
attempt 与写域核对记 WARNING 跳过——该交付路径未经多 Agent 流水线
（与 check_agent_audit.py 的 0/0 放行同口径）；审读结果核对只在
.pd/semantic/ 存在记录时执行（check_semantic.py 才是其硬门禁）。

输入（workdir = self/ 目录）：
- main.tex / sec*.tex          受审切片
- .pd/agent-requests/*.json    派发单（writer attempt 链）
- .pd/agent-calls/*.json       子 Agent 证据台账（output_digests）
- .pd/semantic/<sec>.json      语义审读结果（checker 链）
- .pd/shuffle-map.json         打乱血缘台账（摘要迁移豁免）
- coverage-map.md              规划登记（qid 覆盖核对）

用法: python check_provenance.py [工作目录] [--json PATH]
退出码: 0 = 通过, 1 = 存在阻断性问题。
"""

import argparse
import glob
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
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import evidence  # noqa: E402

TOOL_NAME = "check_provenance.py"
TOOL_VERSION = "1.0"

REQUESTS_DIR_REL = os.path.join(".pd", "agent-requests")
CALLS_DIR_REL = os.path.join(".pd", "agent-calls")
SEMANTIC_DIR_REL = os.path.join(".pd", "semantic")
COVERAGE_MAP_REL = "coverage-map.md"

RE_MAIN_INPUT = re.compile(r"^\s*\\input\{(sec[0-9A-Za-z_-]+)\.tex\}", re.MULTILINE)
RE_QID_LINE = re.compile(r"^\s*%\s*qid:\s*([^\s%|]+)", re.MULTILINE)
RE_SEC_TEX = re.compile(r"^sec[0-9A-Za-z_-]+\.tex$")

#: 写域白名单前缀（audit §5.2）：.pd/ 下派发/证据/报告 + 自己的 secN.tex
ALLOWED_PREFIXES = (".pd/",)
FORBIDDEN_TOP_LEVEL = {"main.tex", "student.tex", "teacher.tex", "run.json"}


def _input_digest(self_dir):
    """受审输入摘要（与 deliver.py 的 input_digest 绑定口径一致）。"""
    try:
        return evidence.content_digest(evidence.collect_inputs(self_dir))
    except Exception:
        return ""


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sha256_file(path):
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def expected_sections(self_dir):
    r"""main.tex 的 \input 清单；缺失时回退磁盘 sec*.tex。"""
    path = os.path.join(self_dir, "main.tex")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            found = RE_MAIN_INPUT.findall(f.read())
        if found:
            return list(dict.fromkeys(found))
    names = [n for n in os.listdir(self_dir) if RE_SEC_TEX.match(n)] \
        if os.path.isdir(self_dir) else []
    return sorted(os.path.splitext(n)[0] for n in names)


def _load_requests(self_dir):
    d = os.path.join(self_dir, REQUESTS_DIR_REL)
    if not os.path.isdir(d):
        return []
    out = []
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        req = _load_json(path)
        if isinstance(req, dict) and req.get("dispatch_id"):
            out.append(req)
    return out


def _load_ledger(self_dir, dispatch_id):
    return _load_json(os.path.join(self_dir, CALLS_DIR_REL,
                                   str(dispatch_id) + ".json"))


def _shuffle_bound(record_digest, current_digest, edges):
    """打乱血缘豁免（复用 check_semantic.py 的确定性口径）。"""
    if not record_digest or not current_digest:
        return False
    try:
        import check_semantic
        return check_semantic.shuffle_bound(record_digest, current_digest, edges)
    except Exception:
        return False


def _writer_certifies(self_dir, sec, requests, current_digest, edges):
    """是否存在一条证据台账认证「当前切片内容」的写作 attempt。

    返回 (ok, detail)：detail 描述最佳候选失败原因（留痕用）。
    """
    best = None
    for req in requests:
        artifacts = [a for a in (req.get("artifacts") or [])
                     if isinstance(a, str)]
        if str(req.get("scope") or "") != sec or (sec + ".tex") not in artifacts:
            continue
        ledger = _load_ledger(self_dir, req.get("dispatch_id"))
        if not isinstance(ledger, dict):
            best = best or "派发单 %s 无证据台账" % req.get("dispatch_id")
            continue
        if str(ledger.get("status") or "") != "ok":
            best = best or ("派发单 %s 的证据状态为 %r"
                            % (req.get("dispatch_id"), ledger.get("status")))
            continue
        digests = ledger.get("output_digests")
        if not isinstance(digests, dict):
            best = best or "派发单 %s 的证据缺 output_digests" % req.get("dispatch_id")
            continue
        recorded = digests.get(sec + ".tex")
        if recorded == current_digest:
            return True, "dispatch_id=%s" % req.get("dispatch_id")
        if _shuffle_bound(recorded, current_digest, edges):
            return True, ("dispatch_id=%s（打乱血缘有效）"
                          % req.get("dispatch_id"))
        best = best or ("派发单 %s 认证的摘要与当前切片不符"
                        % req.get("dispatch_id"))
    return False, best or "该小节没有任何写作派发单"


def _artifact_paths_allowed(self_dir, ledger):
    """证据台账写入路径必须落在写域白名单内。"""
    written = set()
    for rel in (ledger.get("artifacts_written") or []):
        if isinstance(rel, str) and rel:
            written.add(rel)
    digests = ledger.get("output_digests")
    if isinstance(digests, dict):
        written.update(k for k in digests if isinstance(k, str) and k)
    bad = []
    for rel in sorted(written):
        norm = rel.replace("\\", "/")
        if os.path.isabs(norm) or ".." in norm.split("/"):
            bad.append(rel)
            continue
        if os.path.basename(norm) in FORBIDDEN_TOP_LEVEL:
            bad.append(rel)
            continue
        if not any(norm.startswith(p) for p in ALLOWED_PREFIXES) \
                and not RE_SEC_TEX.match(os.path.basename(norm)):
            bad.append(rel)
    return bad


def check_provenance_project(workdir):
    """返回 {errors, warnings, checked, workdir}。"""
    self_dir = os.path.abspath(workdir)
    errors, warnings = [], []

    sections = expected_sections(self_dir)
    requests = _load_requests(self_dir)
    has_pipeline = bool(requests)

    edges = []
    try:
        import check_semantic
        edges = check_semantic.load_shuffle_transitions(self_dir)
    except Exception:
        edges = []

    coverage_text = ""
    cov_path = os.path.join(self_dir, COVERAGE_MAP_REL)
    if os.path.isfile(cov_path):
        with open(cov_path, encoding="utf-8", errors="replace") as f:
            coverage_text = f.read()

    semantic_dir = os.path.join(self_dir, SEMANTIC_DIR_REL)
    has_semantic = os.path.isdir(semantic_dir) and any(
        f.endswith(".json") for f in os.listdir(semantic_dir))

    evaluate = None
    try:
        from workflow.nodes import semantic_nodes
        evaluate = semantic_nodes.evaluate_checker_result
    except Exception:
        evaluate = None

    if not has_pipeline:
        warnings.append({
            "code": "PROV_NO_DISPATCH_CHAIN",
            "detail": "工程没有任何派发单（.pd/agent-requests/ 为空），"
                      "写作 attempt 与写域核对跳过（非多 Agent 交付路径）"})

    checked = {"sections": sections, "requests": len(requests),
               "certified": [], "checker": {}, "has_pipeline": has_pipeline}

    for sec in sections:
        sec_path = os.path.join(self_dir, sec + ".tex")
        if not os.path.isfile(sec_path):
            errors.append({"code": "PROV_SECTION_MISSING",
                           "detail": "main.tex 引用的 %s.tex 不存在" % sec})
            continue
        current = _sha256_file(sec_path)

        # ---- 1. writer attempt 链 ----
        if has_pipeline:
            ok, detail = _writer_certifies(
                self_dir, sec, requests, current, edges)
            if not ok:
                errors.append({
                    "code": "PROV_WRITER_DIGEST_MISSING",
                    "detail": "%s.tex 无法回溯到认证当前内容的写作 attempt"
                              "（%s）——旧结果不得因文件存在而复用" % (sec, detail)})
            else:
                checked["certified"].append(sec)

            # ---- 2. 写域白名单（只看认证当前内容的证据台账）----
            for req in requests:
                if str(req.get("scope") or "") != sec:
                    continue
                if (sec + ".tex") not in [a for a in (req.get("artifacts") or [])
                                          if isinstance(a, str)]:
                    continue
                ledger = _load_ledger(self_dir, req.get("dispatch_id"))
                if not isinstance(ledger, dict):
                    continue
                if str(ledger.get("status") or "") != "ok":
                    continue
                bad = _artifact_paths_allowed(self_dir, ledger)
                if bad:
                    errors.append({
                        "code": "PROV_PATH_OUTSIDE_ALLOWLIST",
                        "detail": "派发单 %s 的证据台账写入了白名单外路径: %s"
                                  % (req.get("dispatch_id"), ", ".join(bad))})

        # ---- 3. checker 链（复用语义审读求值，含打乱血缘豁免）----
        if has_semantic or has_pipeline:
            if evaluate is not None:
                status, detail = evaluate(self_dir, sec, current)
            else:
                # workflow 包不可导入时的本地退化核对
                rec = _load_json(os.path.join(semantic_dir, sec + ".json"))
                if rec is None:
                    status, detail = "missing_result", "审读结果缺失"
                elif str(rec.get("section_digest") or "") != current \
                        and not _shuffle_bound(
                            str(rec.get("section_digest") or ""), current, edges):
                    status, detail = "stale_digest", "审读绑定摘要与当前不符"
                else:
                    status, detail = "ok", ""
            checked["checker"][sec] = status
            code_map = {
                "missing_result": "PROV_CHECKER_MISSING",
                "invalid_result": "PROV_CHECKER_INVALID",
                "stale_digest": "PROV_CHECKER_STALE",
                "revise": "PROV_CHECKER_REVISE",
                "needs_evidence": "PROV_CHECKER_NEEDS_EVIDENCE",
            }
            if status in code_map:
                errors.append({
                    "code": code_map[status],
                    "detail": "%s.tex 的语义审读血缘不完整（%s: %s）"
                              % (sec, status, detail)})

        # ---- 4. qid 规划登记 ----
        with open(sec_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        for qid in dict.fromkeys(RE_QID_LINE.findall(text)):
            if coverage_text and qid not in coverage_text:
                errors.append({
                    "code": "PROV_QID_UNPLANNED",
                    "detail": "%s.tex 的 qid=%s 未登记于 coverage-map.md"
                              "（规划链路断裂）" % (sec, qid)})

    return {"errors": errors, "warnings": warnings,
            "checked": checked, "workdir": self_dir}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="派发血缘门禁 (check_provenance.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    res = check_provenance_project(args.workdir)
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
        print("[check_provenance] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_provenance] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_provenance] 发现 %d 个派发血缘问题（工作区: %s）:"
              % (len(errors), res["workdir"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    checked = res["checked"]
    print("[check_provenance] 通过：血缘链完整（%d 节 / %d 张派发单，"
          "认证 %d 节，checker %s）。"
          % (len(checked["sections"]), checked["requests"],
             len(checked["certified"]),
             ",".join("%s:%s" % kv for kv in sorted(checked["checker"].items()))
             or "无"))
    return 0


check_provenance = check_provenance_project


if __name__ == "__main__":
    sys.exit(main())
