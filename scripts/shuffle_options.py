#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.1"
r"""shuffle_options.py — 选择题选项随机打乱（TeX 源层的确定性机制）

背景（缺陷修复）：题链自学案的选择题化填空写作
   \fillin[答案]{宽度}[选项1/选项2/选项3]
作者（LLM 切片专员）习惯把参考答案写在选项首位，而 LaTeX 版 \fillin 宏
（templates/header.tex）把选项按源码顺序紧随下划线之后以
（选项1/选项2/选项3）附加——学生直接圈选完成填空。若不机制性打乱，
学生版 PDF 中每个选择题的正确项永远排在第一位（“所有答案都是 A”），
失去选择题的训练意义。

机制（在所有题目制成之后、XeLaTeX 双版编译之前执行）：
   对项目每个切片（main.tex 的 \input 装配顺序，与 check_numbering.sec_inputs
   同口径）中的每个 \fillin 选项组，按“密钥排序”做确定性随机重排：

       新顺序 = options.sort(key = sha256(SEED_SALT + 0x00 + 身份 + 0x00 + 选项原文))

   - 身份 = "<切片名>#<该文件中 \fillin 的出现序号>"（1 起，含无选项的
     \fillin，保证同一文件内不同空位得到不同排列；相同选项集的空位
     不会得到相同排列，避免“成立/不成立/无法判定”类选项的答案位置泄漏规律）；
   - 只重排第三个方括号（选项）的原文片段，以 "/" 原样分隔 rejoined，
     答案参数、宽度参数与文件其余内容字节级不变（注释内的 \fillin 不
     触碰，与 check_numbering.strip_comment 同口径）；
   - 密钥排序是幂等的：对同一输入文件重复执行结果完全相同
     （canonical(canonical(x)) == canonical(x)），因此重建、返修后
     重跑、门禁复跑都不会二次扰动，报告的 input_digest 稳定可绑定；
   - 选项 < 2 个时无需重排；无选项的 \fillin 原样保留（缺选项由
     check_numbering 硬门禁负责，本脚本只记提示不判失败）。

产物与门禁口径：
   - 落盘报告（默认 <workdir>/.pd/reports/shuffle_options.json，统一
     evidence schema_version=2）：逐槽位记录打乱前后的选项顺序与
     答案位置、答案位置分布统计、input_digest（打乱后的受审输入摘要，
     供 deliver.py 绑定核对）；
   - status=pass 当且仅当：所有带选项的 \fillin 均处于密钥排序
     结果（apply 后必然成立；--verify 不复写只核对），且无括号解析
     失败/读写失败；
   - --verify 模式：只读核对，不修改任何文件，用于独立门禁体检。

用法:
   python shuffle_options.py [工作目录] [--json PATH] [--verify] [--salt STR]
退出码: 0=通过（已打乱或复核一致）, 1=未通过, 2=缺少依赖或输入。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import check_numbering  # noqa: E402

# 密钥盐：与工具版本绑定；调整盐值会改变全部排列（谨慎）。
SEED_SALT = "qchain/shuffle-options/v1"
OPTION_SEP = "/"
split_options = check_numbering.split_options


def mask_comments_text(text):
    """整段文本的注释掩码（与 check_numbering.strip_comment 同口径、等长保偏移）。

    只用于定位 \fillin（注释内的写法不参与打乱）；返回与 text 等长的
    掩码串，注释部分替换为等长空格，偏移一一对应。
    """
    lines = text.split("\n")
    out = []
    for line in lines:
        s = check_numbering.strip_comment(line)
        if len(s) < len(line):
            s = s + " " * (len(line) - len(s))
        out.append(s)
    return "\n".join(out)


def project_files(workdir):
    """待打乱切片：与 check_numbering.sec_inputs / 装配顺序同口径。"""
    inputs = check_numbering.sec_inputs(workdir)
    if inputs is None:
        inputs = sorted(
            (f for f in os.listdir(workdir)
             if re.fullmatch(r"sec.*\.tex", f)),
            key=check_numbering.sec_key)
    return [n for n in inputs if os.path.exists(os.path.join(workdir, n))]


# D5：逐字环境（示例代码中的 \fillin 字样不得参与打乱）
_VERBATIM_ENV_RE = re.compile(
    r"\\begin\{(verbatim|lstlisting|verbatim\*)\}.*?\\end\{\1\}",
    re.DOTALL)


def _verbatim_spans(text):
    """返回 verbatim/lstlisting 环境的字符区间列表（跳过示例文本）。"""
    return [(m.start(), m.end()) for m in _VERBATIM_ENV_RE.finditer(text)]


def _in_spans(pos, spans):
    for lo, hi in spans:
        if lo <= pos < hi:
            return True
    return False


def iter_fillins(text):
    r"""扫描 \fillin[答案]{宽度}[选项...]（注释感知，逐字环境跳过），产出字典：

        {"start", "answer", "width", "options", "span", "parse_error"}
    span = (content_start, content_end) 为选项方括号内容在原 text 中的
    半开区间（保字节替换用）；answer/options/width 可为 None（缺失参数）。
    parse_error 非空表示括号未闭合/参数缺失等无法安全重排的机制性问题
    （与 check_numbering“不静默丢弃”纪律一致；该检查对这类写法选择跳过，
    但 tex2cards 之外的 LaTeX 宏同样只会得到残缺选项，故这里显式记账）。

    D5：\fillin 匹配带词边界（不误伤 \myfillin 等派生宏），且
    verbatim/lstlisting 环境内的示例文本不参与扫描。
    """
    masked = mask_comments_text(text)
    n = len(masked)
    verb_spans = _verbatim_spans(masked)
    i = 0
    while True:
        j = masked.find("\\fillin", i)
        if j < 0:
            return
        k = j + len("\\fillin")
        # D5：词边界——\fillinX / \myfillin 等派生宏不视为 \fillin
        if k < n and masked[k].isalpha():
            i = k
            continue
        # D5：逐字环境内的示例文本不参与打乱
        if _in_spans(j, verb_spans):
            i = k
            continue
        while k < n and masked[k] in " \t\n":
            k += 1
        answer = None
        if k < n and masked[k] == "[":
            k, answer, aok = check_numbering._scan_bracket(masked, k)
            while k < n and masked[k] in " \t\n":
                k += 1
            if not aok:
                i = k
                yield {"start": j, "answer": answer, "width": None,
                       "options": None, "span": None,
                       "parse_error": "\\fillin 答案方括号未闭合"}
                continue
        width = None
        if k < n and masked[k] == "{":
            k, width, wok = check_numbering._scan_brace(masked, k)
            if not wok:
                i = k
                yield {"start": j, "answer": answer, "width": width,
                       "options": None, "span": None,
                       "parse_error": "\\fillin 宽度花括号未闭合"}
                continue
            while k < n and masked[k] in " \t\n":
                k += 1
        else:
            # 缺宽度花括号：check_numbering 同样跳过，但 \fillin 宏会发
            # 编译警告（build.py 判严重），这里显式记账（不静默丢弃）
            i = k
            yield {"start": j, "answer": answer, "width": None,
                   "options": None, "span": None,
                   "parse_error": "\\fillin 缺少宽度花括号参数"}
            continue
        options = None
        oclosed = None
        span = None
        if k < n and masked[k] == "[":
            obracket = k
            k, options, oclosed = check_numbering._scan_bracket(masked, k)
            if oclosed:
                span = (obracket + 1, k - 1)  # 内容区间（不含 [ ]）
        i = k
        yield {"start": j, "answer": answer, "width": width,
               "options": options, "span": span,
               "parse_error": ("\\fillin 选项方括号未闭合"
                               if oclosed is False else None)}


def option_key(option, identity, salt):
    """选项的确定性随机密钥（sha256(盐 + 身份 + 选项原文)）。"""
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(b"\x00")
    h.update(identity.encode("utf-8"))
    h.update(b"\x00")
    h.update(option.encode("utf-8"))
    return h.hexdigest()


def canonical_options(options, identity, salt):
    """密钥排序（确定性“随机”排列）。相同 (身份, 选项集) 永远同序。"""
    return sorted(options, key=lambda o: (option_key(o, identity, salt), o))


def _answer_index(options, answer):
    """答案在选项中的下标（归一化口径，与 check_numbering 一致）。"""
    ans = check_numbering.norm_opt(answer)
    if not ans:
        return None
    for idx, o in enumerate(options):
        if check_numbering.norm_opt(o) == ans:
            return idx
    return None


def shuffle_text(text, sec_stem, salt=SEED_SALT):
    """对单个切片文本执行/模拟打乱。

    返回 (new_text, records, errors)：
      new_text  打乱后的文本（调用方决定是否落盘）；
      records   每个带选项 \fillin 的打乱记录（含位置统计证据）；
      errors    括号未闭合/选项区间缺失等机制性错误（无法安全重写）。
    只改写选项方括号的内容片段，其余字节不动。
    """
    records = []
    errors = []
    out = []
    cursor = 0
    fillin_no = 0
    for info in iter_fillins(text):
        fillin_no += 1
        where_line = text.count("\n", 0, info["start"]) + 1
        identity = "%s#%d" % (sec_stem, fillin_no)
        if info.get("parse_error"):
            errors.append("%s:%d: %s，无法安全重排"
                          % (sec_stem, where_line, info["parse_error"]))
            continue
        if info["options"] is None:
            continue  # 无选项：本脚本无权补齐（check_numbering 硬门禁）
        if not info.get("span"):
            errors.append("%s:%d: \\fillin 选项区间缺失，无法安全重排"
                          % (sec_stem, where_line))
            continue
        lo, hi = info["span"]
        raw = text[lo:hi]
        opts = split_options(raw, OPTION_SEP)
        if len(opts) < 2:
            continue  # 单个选项无需重排
        new_opts = canonical_options(opts, identity, salt)
        before_idx = _answer_index(opts, info["answer"])
        after_idx = _answer_index(new_opts, info["answer"])
        records.append({
            "index": fillin_no,
            "line": where_line,
            "identity": identity,
            "answer": (info["answer"] or ""),
            "width": (info["width"] or ""),
            "options_before": opts,
            "options_after": new_opts,
            "answer_position_before": before_idx,
            "answer_position_after": after_idx,
            "changed": new_opts != opts,
        })
        if new_opts != opts:
            out.append(text[cursor:lo])
            out.append(OPTION_SEP.join(new_opts))
            cursor = hi
    out.append(text[cursor:])
    return "".join(out), records, errors


def scan_project(project, salt=SEED_SALT, apply_changes=False):
    """扫描（并按需打乱）整个项目；返回 (results, errors)。

    results: [{path, text_before, text_after, records, changed}]
    errors:  机制性错误清单（字符串）。
    """
    project = os.path.abspath(project)
    results = []
    errors = []
    for name in project_files(project):
        path = os.path.join(project, name)
        stem = os.path.splitext(name)[0]
        try:
            # newline=""：不做换行符翻译，CRLF 原样保留（只重写选项区间）
            with open(path, encoding="utf-8", errors="replace",
                      newline="") as f:
                text = f.read()
        except OSError as exc:
            errors.append("%s: 无法读取切片: %s" % (name, exc))
            continue
        new_text, records, terr = shuffle_text(text, stem, salt)
        errors.extend(terr)
        changed = new_text != text
        sha_before = sha_after = None
        if apply_changes and changed:
            # D5：解码替换字符防护——errors="replace" 读入产生的 U+FFFD
            # 不得被写回磁盘（会把编码受损永久固化进切片）
            if new_text.count("") > text.count(""):
                errors.append(
                    "%s: 打乱结果引入了解码替换字符(U+FFFD)，拒绝写回"
                    "（源文件编码可能受损，请人工核查）" % name)
                changed = False
                new_text = text
            else:
                # D5：原子写回（tmp + os.replace），杜绝中断留下半截文件
                try:
                    with open(path, "rb") as fb:
                        sha_before = hashlib.sha256(fb.read()).hexdigest()
                    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), time.time_ns())
                    with open(tmp, "w", encoding="utf-8", newline="") as f:
                        f.write(new_text)
                    os.replace(tmp, path)
                    with open(path, "rb") as fb:
                        sha_after = hashlib.sha256(fb.read()).hexdigest()
                except OSError as exc:
                    errors.append("%s: 无法写回打乱后的切片: %s" % (name, exc))
                    changed = False
                    new_text = text
                    sha_before = sha_after = None
        results.append({"path": name, "text_before": text,
                        "text_after": new_text, "records": records,
                        "changed": changed,
                        "sha_before": sha_before, "sha_after": sha_after})
    return results, errors


def write_shuffle_map(project, results):
    """落盘打乱血缘台账 .pd/shuffle-map.json（apply 模式专用）。

    语义审读记录（.pd/semantic/<sec>.json）绑定的是打乱前的候选摘要；
    本台账记录 <sec> 的打乱前后摘要迁移，使 check_semantic.py 能验证
    "当前切片 == 被审读候选经唯一确定性打乱得到"（血缘有效），
    而不是把语义中性的选项重排误判为候选被篡改（SEMANTIC_STALE）。
    """
    transitions = {}
    for res in results:
        if not res.get("changed"):
            continue
        stem = os.path.splitext(res["path"])[0]
        if not (res.get("sha_before") and res.get("sha_after")):
            continue
        transitions.setdefault(stem, []).append(
            [res["sha_before"], res["sha_after"]])
    if not transitions:
        return None
    out = os.path.join(project, ".pd", "shuffle-map.json")
    payload = {"schema_version": 1, "transitions": transitions}
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = "%s.tmp.%d" % (out, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, out)
    except OSError:
        return None
    return out


def verify_project(project, salt=SEED_SALT, issues=None):
    """只读复核：每个带选项 \\fillin 的当前顺序必须等于密钥排序结果。

    返回 mismatches：未按机制排序的槽位描述（空 = 全部符合机制）。
    若传入 issues (list)，同步核验答案位置偏斜并追加 POSITION_SKEW 告警。
    """
    mismatches = []
    results, _errors = scan_project(project, salt=salt, apply_changes=False)
    for res in results:
        for r in res["records"]:
            opts = r["options_before"]  # apply_changes=False 时即当前顺序
            if len(opts) < 2:
                continue
            expect = canonical_options(opts, r["identity"], salt)
            if expect != opts:
                mismatches.append(
                    "%s:%d: 选项顺序未按打乱机制排序（槽位 %s，答案位置 %s）"
                    % (res["path"], r["line"], r["identity"],
                       r.get("answer_position_before")))
    if issues is not None:
        all_records = [r for res in results for r in res["records"]]
        all_before = [r["answer_position_before"] for r in all_records
                      if r.get("answer_position_before") is not None]
        skew_msg = check_skew_from_positions(all_before)
        if skew_msg:
            issues.append({"category": "POSITION_SKEW", "severity": "warn",
                           "message": skew_msg})
    return mismatches


def position_stats(records, key):
    dist = {}
    for r in records:
        p = r.get(key)
        if p is None:
            continue
        dist[str(p)] = dist.get(str(p), 0) + 1
    return dist


def check_skew_from_positions(positions, threshold=0.5, min_slots=8):
    """检查位置序列分布均衡性（当槽位数 >= min_slots 时生效）。

    若某位置频次偏离均值期望 > threshold，返回格式化告警信息；否则返回 None。
    """
    if len(positions) < min_slots:
        return None
    from collections import Counter
    dist = Counter(positions)
    n = len(positions)
    k = len(dist)
    if k < 2:
        max_dev = 1.0
        return ("答案位置分布显著不均（%s），最大偏差 %.0f%%"
                % (dict(dist), max_dev * 100))
    expected = n / k
    max_dev = max(abs(v - expected) / expected for v in dist.values())
    if max_dev > threshold:
        return ("答案位置分布显著不均（%s），最大偏差 %.0f%%"
                % (dict(dist), max_dev * 100))
    return None


def check_env():
    """环境预检：Python 版本与解析依赖。"""
    ok = True
    print("[env] Python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 8):
        print("[env] 需要 Python 3.8+")
        ok = False
    print("[env] 预检%s" % ("通过" if ok else "未通过"))
    return 0 if ok else 2


def write_json(path, report):
    """写结构化检查结果（统一交付证据格式）。"""
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    evidence.write_report(path, report)
    print("[shuffle] 结构化报告已写入 %s" % path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="选择题选项随机打乱（TeX 源层确定性机制）")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--json", metavar="PATH",
                    help="报告路径（缺省 <workdir>/.pd/reports/shuffle_options.json）")
    ap.add_argument("--verify", action="store_true",
                    help="只读复核模式：不修改任何文件，仅核对排列是否符合机制")
    ap.add_argument("--salt", default=SEED_SALT,
                    help="密钥盐（调试用；调整会改变全部排列）")
    ap.add_argument("--check-env", action="store_true",
                    help="只做环境预检，不打乱")
    args = ap.parse_args(argv)
    if args.check_env:
        return check_env()
    project = os.path.abspath(args.workdir)
    if not os.path.isdir(project):
        print("[shuffle] 项目目录不存在: %s" % project)
        return 2

    mode = "verify" if args.verify else "apply"
    results, errors = scan_project(project, salt=args.salt,
                                   apply_changes=not args.verify)
    shuffle_map_path = None
    if not args.verify:
        # 打乱血缘台账：语义审读摘要绑定打乱前候选，本台账证明当前切片
        # 是"被审读候选经确定性选项重排"得到（check_semantic 血缘校验）。
        shuffle_map_path = write_shuffle_map(project, results)

    issues = []          # evidence issues：[{category, severity, message}]
    for e in errors:
        print("[shuffle] [错误] %s" % e)
        issues.append({"category": "SHUFFLE_ERROR", "severity": "error",
                       "message": e})
    mismatches = []
    if args.verify:
        mismatches = verify_project(project, salt=args.salt)
    for m in mismatches:
        print("[shuffle] [错误] %s" % m)
        issues.append({"category": "NOT_SHUFFLED", "severity": "error",
                       "message": m})

    # ---- 内容结构提示（缺答案/选项不足/答案不在选项内）→ 仅提示，
    #      硬门禁由 check_numbering 负责（同口径重复记账不双判） ----
    n_struct = 0
    for res in results:
        for r in res["records"]:
            opts = r["options_before"]
            ans = r["answer"]
            if not ans.strip():
                n_struct += 1
                issues.append({"category": "NO_ANSWER", "severity": "warn",
                               "message": "%s:%d: \\fillin 缺少参考答案"
                               % (res["path"], r["line"])})
                continue
            if len(opts) < 3:
                n_struct += 1
                issues.append({"category": "FEW_OPTIONS", "severity": "warn",
                               "message": "%s:%d: 槽位 %s 只有 %d 个选项"
                               "（check_numbering 要求至少 3 个）"
                               % (res["path"], r["line"], r["identity"],
                                  len(opts))})
                continue
            norms = [check_numbering.norm_opt(o) for o in opts]
            if check_numbering.norm_opt(ans) not in norms:
                n_struct += 1
                issues.append({"category": "ANSWER_NOT_IN_OPTIONS",
                               "severity": "warn",
                               "message": "%s:%d: 槽位 %s 的参考答案不在选项中"
                               % (res["path"], r["line"], r["identity"])})
            dup = sorted({x for x in norms if norms.count(x) > 1})
            if dup:
                n_struct += 1
                issues.append({"category": "DUP_OPTIONS", "severity": "warn",
                               "message": "%s:%d: 槽位 %s 的选项重复（%s）"
                               % (res["path"], r["line"], r["identity"],
                                  ", ".join(dup))})

    # ---- 打乱后的受审输入摘要（供 deliver.py 绑定核对） ----
    try:
        digest = evidence.content_digest(evidence.collect_inputs(project))
    except Exception as exc:  # 证据模块异常不得静默
        digest = None
        issues.append({"category": "EVIDENCE_ERROR", "severity": "error",
                       "message": "输入摘要计算失败: %s" % exc})

    all_records = [r for res in results for r in res["records"]]
    n_slots = len(all_records)
    n_changed = sum(1 for r in all_records if r["changed"])
    n_files_changed = sum(1 for res in results if res["changed"])

    # ---- 章节级答案位置分布均衡检查 (F3) ----
    all_after = [r["answer_position_after"] for r in all_records
                 if r.get("answer_position_after") is not None]
    all_before = [r["answer_position_before"] for r in all_records
                  if r.get("answer_position_before") is not None]

    target_pos = all_before if args.verify else all_after
    skew_msg = check_skew_from_positions(target_pos)
    if skew_msg:
        print("[shuffle] [警告] %s" % skew_msg)
        issues.append({"category": "POSITION_SKEW",
                       "severity": "warn", "message": skew_msg})
    elif args.verify:
        skew_msg_after = check_skew_from_positions(all_after)
        if skew_msg_after:
            print("[shuffle] [警告] %s" % skew_msg_after)
            issues.append({"category": "POSITION_SKEW",
                           "severity": "warn", "message": skew_msg_after})

    files_ev = []
    for res in results:
        files_ev.append({
            "path": res["path"],
            "slots": len(res["records"]),
            "rewritten": res["changed"],
            "records": res["records"],
        })

    status = "fail" if (errors or mismatches or digest is None) else "pass"
    report = {
        "schema_version": evidence.SCHEMA_VERSION,
        "tool": "shuffle_options.py",
        "tool_version": TOOL_VERSION,
        "scope": {"project": project, "mode": mode,
                  "files": [res["path"] for res in results],
                  "seed_salt": args.salt},
        "input_digest": digest,
        "pdf_sha256": None,
        "status": status,
        "issues": issues,
        "evidence": {
            "files": files_ev,
            "totals": {
                "slots": n_slots,
                "changed": n_changed,
                "files_rewritten": n_files_changed,
                "answer_positions_before": position_stats(
                    all_records, "answer_position_before"),
                "answer_positions_after": position_stats(
                    all_records, "answer_position_after"),
            },
            "report_generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    }

    for res in results:
        print("[shuffle] %s：%d 个槽位，%s"
              % (res["path"], len(res["records"]),
                 "已重写" if res["changed"] else "无需改动"))
    print("[shuffle] 总计：%d 个选择题槽位，%d 个发生重排；答案位置分布 %s → %s"
          % (n_slots, n_changed,
             report["evidence"]["totals"]["answer_positions_before"],
             report["evidence"]["totals"]["answer_positions_after"]))
    if n_struct:
        print("[shuffle] [提示] %d 处内容结构问题已记录（硬门禁归 "
              "check_numbering）" % n_struct)

    out_json = args.json or os.path.join(project, ".pd", "reports",
                                         "shuffle_options.json")
    write_json(out_json, report)
    print("[shuffle] 总体结果（%s 模式）：%s"
          % (mode, "通过" if status == "pass" else "未通过"))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
