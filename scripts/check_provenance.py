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

  A21 手工编辑通道（替代"为过血缘硬造一次派发"的规避）：
  PROV_MANUAL_EDIT_UNKNOWN_SECTION / _BAD_DIGEST / _NO_REASON /
  _DUPLICATE / _STALE     .pd/manual-edits.json 登记项本身的问题
  一次登记 = 声明"这一节由人手工改过，当前字节级内容即此"，门禁据此
  为 writer 链放行；checker 链（语义审读）仍按 digest 绑定，不受豁免。

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
- .pd/manual-edits.json        手工编辑登记（A21：writer 链豁免）
- coverage-map.md              规划登记（qid 覆盖核对）

用法: python check_provenance.py [工作目录] [--json PATH] [--mode full|writer-only]
      python check_provenance.py <工作目录> --register-manual-edit sec1 \
          --reason "修正 sec1 题干笔误" [--attested-by 姓名]
      python check_provenance.py <工作目录> --list-manual-edits
      （writer-only 供 faithful_fast 图的「构建前」使用：跳过 checker 链；
        交付前的正式报告一律 full，且仍是 deliver 的 MUST_PASS 项。）
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
import request_scopes  # noqa: E402  C5：多 scope 授权唯一口径
import section_scan  # noqa: E402

TOOL_NAME = "check_provenance.py"
TOOL_VERSION = "1.1"

#: 卡15（C6）门禁分层：full=终审/交付；writer-only=构建前（审读证据尚未产生）
MODE_FULL = "full"
MODE_WRITER_ONLY = "writer-only"
MODES = (MODE_FULL, MODE_WRITER_ONLY)

REQUESTS_DIR_REL = os.path.join(".pd", "agent-requests")
CALLS_DIR_REL = os.path.join(".pd", "agent-calls")
SEMANTIC_DIR_REL = os.path.join(".pd", "semantic")
COVERAGE_MAP_REL = "coverage-map.md"
MANUAL_EDITS_REL = os.path.join(".pd", "manual-edits.json")

#: A21 手工编辑登记：一次登记 = 为 writer 链放行一节；登记必须绑定当前 digest。
MANUAL_EDITS_SCHEMA_VERSION = 1
MANUAL_REASON_MIN_CHARS = 4
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")

RE_MAIN_INPUT = section_scan.RE_MAIN_INPUT
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
    r"""main.tex 的 \input 清单；扫不出时回退磁盘 sec*.tex（N7 统一口径）。"""
    sections, _source = section_scan.resolve_sections(self_dir)
    return sections


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


def load_manual_edits(self_dir):
    """读取 .pd/manual-edits.json，返回 {section: {"digest","reason","edited_by"}}。

    A21：手工编辑（含 repurpose 派发单改写了别节的副作用）过去只能靠"硬造
    一次派发"来恢复血缘，一次手工改动 = 一整轮返修。本通道把这件事变成
    显式登记：登记哪一节、为什么改、改后字节级摘要是什么。

    读取失败/结构不对一律视为"无登记"（安全方向：没登记就没有豁免），
    条目本身的问题由 validate_manual_edits 报错，不在这里静默吞掉。
    """
    path = os.path.join(self_dir, MANUAL_EDITS_REL)
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("edits")
    if not isinstance(raw, list):
        return {}
    out = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sec = entry.get("section")
        if isinstance(sec, str) and sec:
            out[sec] = {
                "digest": str(entry.get("digest") or ""),
                "reason": str(entry.get("reason") or ""),
                "edited_by": str(entry.get("edited_by") or ""),
            }
    return out


def validate_manual_edits(self_dir, sections, edits):
    """校验登记项本身；返回 (errors, warnings)。

    防伪三条：登记的节必须真实存在；摘要必须是 64 位十六进制；必须给理由。
    重复登记同一节只保留最后一条（写入口保证），这里只报 warning 提醒。
    """
    errors, warnings = [], []
    known = set(sections)
    seen = set()
    for sec in sorted(edits):
        entry = edits[sec]
        if sec in seen:
            warnings.append({
                "code": "PROV_MANUAL_EDIT_DUPLICATE",
                "detail": "手工编辑登记中 %s 出现多次，仅最后一条生效" % sec})
        seen.add(sec)
        if sec not in known:
            errors.append({
                "code": "PROV_MANUAL_EDIT_UNKNOWN_SECTION",
                "detail": "手工编辑登记了不存在的切片 %r（main.tex 未引用），"
                          "登记无法核对" % sec})
            continue
        if not RE_SHA256.match(entry.get("digest") or ""):
            errors.append({
                "code": "PROV_MANUAL_EDIT_BAD_DIGEST",
                "detail": "%s 的手工编辑登记缺合法 sha256 摘要（digest=%r），"
                          "血缘无从核对" % (sec, entry.get("digest"))})
            continue
        if len((entry.get("reason") or "").strip()) < MANUAL_REASON_MIN_CHARS:
            errors.append({
                "code": "PROV_MANUAL_EDIT_NO_REASON",
                "detail": "%s 的手工编辑登记缺少理由（至少 %d 字符）："
                          "手工改动必须有可追溯的说明"
                          % (sec, MANUAL_REASON_MIN_CHARS)})
    return errors, warnings


def register_manual_edit(self_dir, sec, reason, attested_by=""):
    """把 sec 的当前内容登记为手工编辑；返回 (ok, message)。

    登记摘要取**磁盘当前字节**，所以登记必须紧跟在手工改动之后做；
    之后再改文件而不重新登记，登记即过期（PROV_MANUAL_EDIT_STALE），
    不会变成永久的免检金牌。
    """
    sec_path = os.path.join(self_dir, sec + ".tex")
    digest = _sha256_file(sec_path)
    if not digest:
        return False, "%s.tex 不存在或不可读，无法登记" % sec
    if len((reason or "").strip()) < MANUAL_REASON_MIN_CHARS:
        return False, "理由至少 %d 字符（说明改了什么、为什么）" \
            % MANUAL_REASON_MIN_CHARS
    path = os.path.join(self_dir, MANUAL_EDITS_REL)
    data = {"schema_version": MANUAL_EDITS_SCHEMA_VERSION, "edits": []}
    if os.path.isfile(path):
        old = _load_json(path)
        if isinstance(old, dict) and isinstance(old.get("edits"), list):
            data["edits"] = [e for e in old["edits"] if isinstance(e, dict)]
    data["edits"] = [e for e in data["edits"] if e.get("section") != sec]
    entry = {"section": sec, "digest": digest, "reason": reason.strip()}
    if attested_by:
        entry["edited_by"] = attested_by
    data["edits"].append(entry)
    data["edits"].sort(key=lambda e: str(e.get("section") or ""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return True, "已登记 %s 的手工编辑（digest=%s…）→ %s" \
        % (sec, digest[:12], os.path.relpath(path, self_dir))


def _writer_certifies(self_dir, sec, requests, current_digest, edges,
                      manual=None):
    """是否存在一条证据台账认证「当前切片内容」的写作 attempt。

    返回 (ok, detail)：detail 描述最佳候选失败原因（留痕用）。

    卡13（C5）：一个作者可以一次写 1–2 个相邻节，因此「这个派发单是否被授权写
    sec」必须读正规化 scopes（``request_scopes``），不能再拿 ``scope`` 字段做
    独占判定——否则 sec1+sec2 的合批单永远无法为 sec2 认证，来源链必然断裂。

    A21：``manual`` 为手工编辑登记（{section: {digest, reason}}）。登记项
    摘要与当前字节一致时，视为已声明的人工改动，writer 链放行——这替代了
    "为了过血缘硬造一次派发"的规避；摘要不一致（登记后又改过）不放行，
    并在 detail 里指出，避免登记沦为永久免检。
    """
    if manual:
        entry = manual.get(sec)
        if entry:
            recorded = entry.get("digest") or ""
            if recorded == current_digest:
                return True, "manual-edit: %s" % (entry.get("reason") or "")
            best = ("已有手工编辑登记，但其摘要与当前 %s.tex 不符"
                    "（登记后又改动过？请重新 --register-manual-edit）" % sec)
            return False, best
    best = None
    for req in requests:
        artifacts = [a for a in (req.get("artifacts") or [])
                     if isinstance(a, str)]
        # 授权真源：v2 读 scopes，legacy 回落 scope/context.target_section
        if not request_scopes.request_covers(req, sec):
            continue
        if (sec + ".tex") not in artifacts:
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


def check_provenance_project(workdir, mode=MODE_FULL):
    """返回 {errors, warnings, checked, workdir, mode}。

    卡15（C6）：mode 分两档——
      * MODE_FULL：「writer attempt 链 + 写域白名单 + checker 链 + qid 规划」全查，
        终审（合并审读全部 PASS 之后）与交付前使用；
      * MODE_WRITER_ONLY：只查 writer 认证/写域/qid 规划，跳过 checker 链——
        构建前还不可能有审读证据，跳过不算放行：交付侧的正式报告仍按 FULL 生成
        并纳入 MUST_PASS。
    """
    if mode not in (MODE_FULL, MODE_WRITER_ONLY):
        raise ValueError("未知 provenance 模式: %r" % (mode,))
    self_dir = os.path.abspath(workdir)
    errors, warnings = [], []

    sections = expected_sections(self_dir)
    if not sections:
        # N7：零小节 = 血缘核对没生效，不是"没有小节要查"
        errors.append({
            "code": "PROVENANCE_NO_SECTIONS",
            "detail": section_scan.no_slice_detail(self_dir)})
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
               "certified": [], "checker": {}, "has_pipeline": has_pipeline,
               "manual": [], "mode": mode}

    # A21：手工编辑登记先校验本身，再作为 writer 链的豁免依据
    manual_edits = load_manual_edits(self_dir)
    man_errors, man_warnings = validate_manual_edits(
        self_dir, sections, manual_edits)
    errors.extend(man_errors)
    warnings.extend(man_warnings)

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
                self_dir, sec, requests, current, edges, manual_edits)
            if not ok:
                errors.append({
                    "code": "PROV_WRITER_DIGEST_MISSING",
                    "detail": "%s.tex 无法回溯到认证当前内容的写作 attempt"
                              "（%s）——旧结果不得因文件存在而复用" % (sec, detail)})
            else:
                checked["certified"].append(sec)
                if detail.startswith("manual-edit:"):
                    checked["manual"].append(sec)

            # ---- 2. 写域白名单（只看认证当前内容的证据台账）----
            # 卡13（C5）：授权同样按 scopes 判定——合批单写 sec2 的证据台账也要
            # 被 sec2 的写域检查覆盖，否则第二节的越界写入彻底漏检。
            for req in requests:
                if not request_scopes.request_covers(req, sec):
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
        # writer-only（构建前）跳过：审读证据要到合并终审才产生。
        if mode == MODE_FULL and (has_semantic or has_pipeline):
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
            "checked": checked, "workdir": self_dir, "mode": mode}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="派发血缘门禁 (check_provenance.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    ap.add_argument("--mode", choices=MODES, default=MODE_FULL,
                    help="full=含 checker 链（终审/交付）；"
                         "writer-only=只查 writer 认证/写域/qid（构建前）")
    ap.add_argument("--register-manual-edit", metavar="SECTION",
                    help="A21：登记该切片为手工编辑（摘要取磁盘当前内容），"
                         "writer 链据此放行；必须同时给 --reason")
    ap.add_argument("--reason", metavar="TEXT",
                    help="手工编辑理由（与 --register-manual-edit 连用，"
                         "至少 %d 字符）" % MANUAL_REASON_MIN_CHARS)
    ap.add_argument("--attested-by", metavar="NAME", default="",
                    help="登记人署名（留痕用，可空）")
    ap.add_argument("--list-manual-edits", action="store_true",
                    help="列出手工编辑登记后退出")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.workdir)

    if args.list_manual_edits:
        edits = load_manual_edits(workdir)
        if not edits:
            print("[check_provenance] 无手工编辑登记（%s）"
                  % MANUAL_EDITS_REL)
            return 0
        sections = expected_sections(workdir)
        for sec in sorted(edits):
            entry = edits[sec]
            digest = entry.get("digest") or ""
            now = _sha256_file(os.path.join(workdir, sec + ".tex"))
            state = "有效" if digest == now else "已过期（需重新登记）"
            print("[check_provenance] %s.tex %s digest=%s… 理由=%s"
                  % (sec, state, digest[:12], entry.get("reason") or ""))
            if sec not in sections:
                print("    （警告：%s 不在 main.tex 的切片清单内）" % sec)
        return 0

    if args.register_manual_edit:
        if not args.reason:
            ap.error("--register-manual-edit 必须同时提供 --reason")
        ok, msg = register_manual_edit(
            workdir, args.register_manual_edit, args.reason,
            attested_by=args.attested_by)
        print("[check_provenance] %s" % msg)
        return 0 if ok else 1

    res = check_provenance_project(args.workdir, mode=args.mode)
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
        # 卡15：门禁档位入库，交付侧/UI 可据此识别「构建前只做 writer-only」
        "mode": res["mode"],
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
    print("[check_provenance] 通过（模式 %s）：血缘链完整（%d 节 / %d 张派发单，"
          "认证 %d 节%s，checker %s）。"
          % (res["mode"], len(checked["sections"]), checked["requests"],
             len(checked["certified"]),
             ("，其中手工编辑登记 %d 节" % len(checked.get("manual") or []))
             if checked.get("manual") else "",
             ",".join("%s:%s" % kv for kv in sorted(checked["checker"].items()))
             or ("跳过（writer-only）" if res["mode"] == MODE_WRITER_ONLY else "无")))
    return 0


check_provenance = check_provenance_project


if __name__ == "__main__":
    sys.exit(main())
