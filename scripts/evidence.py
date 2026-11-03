#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""evidence.py — 受审输入清单与内容摘要（F05 共享模块）

六个脚本（build / check_numbering / check_dialogue / compare_versions /
deliver / make_review_pack）统一通过本模块计算输入证据，避免旧证据冒充
当前结果。本模块只做字面量解析（\input / \includegraphics 的静态参数），
不是完整 TeX 解释器：解析不了的动态路径显式记入 manifest 的
missing / unresolved 字段，绝不静默遗漏。

摘要语义（保守失效方案）：
  - 文件索引按项目相对路径排序，内容为文件 SHA256，修改时间不参与；
  - assembly_order 保留 main.tex 中 \input 的实际顺序，不排序；
    仅交换两个 \input 的顺序也会改变 content_digest；
  - 总摘要 = 对 {"files", "assembly_order", "missing", "unresolved"}
    做确定格式 JSON 序列化（排序键、无多余空白）后的 SHA256。

函数均为纯函数，不读写全局状态，便于用 unittest 固定行为：
  sha256_file(path)            -> str | None
  collect_inputs(project)      -> manifest dict
  content_digest(manifest)     -> str (64 hex)
  digest_paths(paths)          -> (str, entries list)
  write_report(path, report)   -> None

F07 追加（材料/验收/疑似项证据，均为纯函数）：
  scan_tex_markers(text)       注释标记解析（兼容独立 % card: 与旧版
                               % qid: ... | card: 合并行）
  card_usages(project, secs)   切片卡片引用 → {card_id: {files, qids}}
  build_materials_index(project)  由材料包整理 materials.json 机器索引
  validate_material_chain(...) 材料链五核对（机械一致性，不证明真实性）
  validate_acceptance_json(...)  验收 JSON 记录一致性核对
  validate_review_notes(...)   疑似项复核记录与当前报告/PDF 绑定核对
"""
import hashlib
import json
import os
import re
import sys
import time
from html.parser import HTMLParser

SCHEMA_VERSION = 2

INPUT_RE = re.compile(r"\\input\{([^{}]+)\}")
GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}")
# \includegraphics 缺省扩展名时的探测顺序（与 pdftex/xetex 常用顺序一致）
GRAPHICS_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps")
# 配置与版本入口：始终作为受审输入（存在与否都会反映在 manifest 中）
ENTRY_FILES = ("student.tex", "teacher.tex", "main.tex",
               "config.yaml", "config.tex", "config-class.tex")


def sha256_file(path):
    """文件内容 SHA256（十六进制）；文件不存在返回 None。纯函数。"""
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_comments(text):
    r"""TeX 感知的注释剥离：\% 是转义字符，不作为注释起点。"""
    out = []
    for line in text.splitlines():
        i = 0
        while True:
            j = line.find("%", i)
            if j < 0:
                out.append(line)
                break
            k = j - 1
            while k >= 0 and line[k] == "\\":
                k -= 1
            if (j - 1 - k) % 2 == 1:  # 奇数个反斜杠 → 转义
                i = j + 1
            else:
                out.append(line[:j])
                break
    return "\n".join(out)


def _norm_rel(project, base_rel, target):
    """把 TeX 引用路径规整为项目相对路径（posix 分隔符）。

    返回 (rel 或 None, 是否越界/非法)。target 含反斜杠（动态宏路径）
    时返回 (None, True) 由调用方记入 unresolved。
    """
    if "\\" in target or "{" in target or "}" in target:
        return None, True
    base_dir = os.path.dirname(os.path.join(project, base_rel))
    ap = os.path.abspath(os.path.join(base_dir, target))
    root = os.path.abspath(project)
    if ap != root and not ap.startswith(root + os.sep):
        return None, True  # 越出项目根，无法纳入摘要
    return ap[len(root) + 1:].replace(os.sep, "/"), False


def collect_inputs(project):
    r"""收集受审输入清单。返回 manifest dict（见模块 docstring）。

    manifest 字段：
      project        项目绝对路径（不参与摘要）
      files          [{"path": 相对路径, "sha256": ...}]，按路径排序
      assembly_order main.tex 中全部字面量 \input 目标的实际顺序（不排序）；
                     无 main.tex 时退化为磁盘上 sec*.tex 的排序清单
      missing        被引用但不存在的相对路径（排序）
      unresolved     [{"source","target","kind"}]，动态/越界路径（排序）
    """
    project = os.path.abspath(project)
    files = {}
    missing = set()
    unresolved = []
    assembly_order = []
    visited = set()

    def add_existing(rel):
        if rel not in files:
            files[rel] = sha256_file(os.path.join(project, rel))

    def parse_tex(rel):
        if rel in visited:
            return
        visited.add(rel)
        path = os.path.join(project, rel)
        if not os.path.exists(path):
            missing.add(rel)
            return
        add_existing(rel)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = _strip_comments(f.read())
        for m in INPUT_RE.finditer(text):
            target = m.group(1).strip()
            if not target:
                continue
            if not os.path.splitext(target)[1]:
                target += ".tex"
            trel, bad = _norm_rel(project, rel, target)
            if bad:
                unresolved.append({"source": rel, "target": m.group(1),
                                   "kind": "input"})
                continue
            if rel == "main.tex":
                assembly_order.append(trel)
            if trel.endswith(".tex"):
                parse_tex(trel)
            else:
                if os.path.exists(os.path.join(project, trel)):
                    add_existing(trel)
                else:
                    missing.add(trel)
        for m in GRAPHICS_RE.finditer(text):
            raw = m.group(1).strip()
            if not raw:
                continue
            grel, bad = _norm_rel(project, rel, raw)
            if bad:
                unresolved.append({"source": rel, "target": raw,
                                   "kind": "graphics"})
                continue
            if os.path.splitext(raw)[1]:
                if os.path.exists(os.path.join(project, grel)):
                    add_existing(grel)
                else:
                    missing.add(grel)
                continue
            found = False
            for ext in GRAPHICS_EXTS:
                cand = grel + ext
                if os.path.exists(os.path.join(project, cand)):
                    add_existing(cand)
                    found = True
                    break
            if not found:
                missing.add(grel)

    for name in ENTRY_FILES:
        rel = name.replace(os.sep, "/")
        if name.endswith(".tex"):
            parse_tex(rel)
        elif os.path.exists(os.path.join(project, rel)):
            add_existing(rel)
        else:
            missing.add(rel)

    if not os.path.exists(os.path.join(project, "main.tex")):
        # 无 main.tex：退化为磁盘切片扫描（保持与旧行为兼容的输入范围）
        for f in sorted(os.listdir(project)) if os.path.isdir(project) else []:
            if re.fullmatch(r"sec.*\.tex", f):
                assembly_order.append(f)
                parse_tex(f)

    return {
        "project": project,
        "files": [{"path": p, "sha256": files[p]} for p in sorted(files)],
        "assembly_order": assembly_order,
        "missing": sorted(missing),
        "unresolved": sorted(unresolved,
                             key=lambda u: (u["source"], u["kind"],
                                            u["target"])),
    }


def content_digest(manifest):
    """受审内容总摘要：确定格式 JSON（排序键、无空白）的 SHA256。

    只覆盖 files / assembly_order / missing / unresolved；项目绝对路径
    与文件修改时间不参与，保证同内容同摘要。
    """
    payload = {k: manifest.get(k) for k in
               ("files", "assembly_order", "missing", "unresolved")}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def digest_paths(paths):
    """对若干文件/目录的内容求确定性摘要（纯函数，不看 mtime）。

    paths 中文件以其路径尾部为键，目录递归展开为 "<dirname>/<相对路径>"
    键；键排序后连同内容 SHA256 做确定格式 JSON 序列化再求总摘要。
    不存在的路径记 sha256=None（反映在摘要中，不静默遗漏）。

    D3：键冲突修复——旧实现以 basename 为键，同名输入静默互相覆盖
    （被覆盖者永远不参与摘要与 stale 判定）。现按键冲突逐级上溯父目录名
    直至唯一；无冲突时键与旧版完全一致（行为兼容）。
    返回 (hexdigest, entries)，entries 为 [{"path", "sha256"}] 排序清单。
    """
    # 第一遍：收集 (完整路径, sha256, 首选键)
    collected = []
    for p in paths:
        if os.path.isfile(p):
            collected.append((os.path.normpath(p), sha256_file(p), None))
        elif os.path.isdir(p):
            base = os.path.basename(os.path.normpath(p))
            for root, _dirs, names in os.walk(p):
                for n in names:
                    ap = os.path.join(root, n)
                    rel = os.path.relpath(ap, p).replace(os.sep, "/")
                    # 目录展开键天然含 dirname 前缀，冲突概率低，保留旧口径
                    collected.append((os.path.normpath(ap), sha256_file(ap),
                                      "%s/%s" % (base, rel)))
        else:
            collected.append((os.path.normpath(p), None, None))

    # 第二遍：分配唯一键（首选键冲突时逐级上溯路径尾部）
    entries = {}
    used = {}
    for full, sha, preferred in collected:
        if preferred is not None and preferred not in used:
            key = preferred
        else:
            parts = full.replace("\\", "/").split("/")
            key = parts[-1]
            depth = 2
            while key in used and used[key] != full and depth <= len(parts):
                key = "/".join(parts[-depth:])
                depth += 1
            if key in used and used[key] != full:
                key = full  # 兜底：完整路径必唯一
        used[key] = full
        entries[key] = sha
    ordered = [{"path": k, "sha256": entries[k]} for k in sorted(entries)]
    blob = json.dumps(ordered, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), ordered


def write_report(path, report):
    """把结构化报告写入 PATH（统一交付证据格式）；path 为空时不写。"""
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


# ======================= HTML 可见文本与卡片解析 =======================
class _VisibleTextParser(HTMLParser):
    """可见文本抽取：完整标签栈判定隐藏容器（嵌套安全）。"""

    SKIP = {"script", "style", "template", "noscript"}
    VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base",
            "col", "embed", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.stack = []            # [[tag, hidden_flag], ...] 开标签栈
        self.cards = []            # [(card_id, text_parts)]
        self.cur_card = None

    def _hidden(self, tag, attrs):
        d = dict(attrs)
        if "hidden" in d:
            return True
        style = (d.get("style") or "").replace(" ", "").lower()
        return "display:none" in style

    def _in_hidden(self):
        return any(h for _t, h in self.stack)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        hidden = self._hidden(tag, attrs)
        if tag not in self.VOID:
            self.stack.append([tag, hidden])
        d = dict(attrs)
        if tag == "section" and "qc-card" in (d.get("class") or ""):
            self.cur_card = d.get("id") or ""
            self.cards.append((self.cur_card, []))

    def handle_startendtag(self, tag, attrs):
        pass  # 自闭合标签无文本、不入栈

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if tag in self.VOID:
            return
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                del self.stack[k:]
                break
        if tag == "section" and self.cur_card is not None:
            self.cur_card = None

    def handle_data(self, data):
        if self.skip_depth or self._in_hidden():
            return
        self.parts.append(data)
        if self.cur_card is not None:
            self.cards[-1][1].append(data)


def html_visible_text(path):
    """HTML 文件的可见文本（规范化空白）；文件不可读返回 None。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError:
        return None
    p = _VisibleTextParser()
    p.feed(html)
    p.close()
    return "\n".join("".join(p.parts).split())


def html_card_pages(path):
    """按卡片页返回 [(card_id, text), ...]（可见文本，隐藏揭示区除外）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError:
        return None
    p = _VisibleTextParser()
    p.feed(html)
    p.close()
    return [(cid, "\n".join("".join(parts).split()))
            for cid, parts in p.cards]



# ======================= F07：材料/验收/疑似项证据 =======================
# 注释标记解析：同时兼容独立行 `% card: CARD-10` 与旧版合并行
# `% qid: sec4-entry | 题型: ... | card: CARD-10`；新写作只输出独立行。
_QID_PART = re.compile(r"\bqid:\s*([^\s%|]+)")
_CARD_PART = re.compile(r"\bcard:\s*([A-Za-z]+-\d+)")
_CARD_HEADER = re.compile(
    r"\*\*卡片 ID\*\*:\s*`([A-Za-z]+-\d+)`\s*\|\s*"
    r"\*\*对应需求\*\*:\s*`([A-Za-z]+-\d+)`\s*\|\s*"
    r"\*\*状态\*\*:\s*`([A-Za-z-]+)`")
_ANCHOR = re.compile(r"([\w./\\-]+\.tex)`?\s*(L[\d-]+)")


def scan_tex_markers(text):
    """逐行扫描 TeX 注释标记（qid / card），兼容独立行与旧版合并行。

    返回 [{"line", "qid", "card"}]（qid/card 可为 None），按行号有序。
    供 deliver/make_review_pack 与后续检查脚本复用，不要另写解析器。
    """
    events = []
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s.startswith("%"):
            continue
        mq = _QID_PART.search(s)
        mc = _CARD_PART.search(s)
        if mq or mc:
            events.append({"line": lineno,
                           "qid": mq.group(1) if mq else None,
                           "card": mc.group(1) if mc else None})
    return events


def card_usages(project, sec_files):
    """切片中的卡片引用 → {card_id: {"files": [...], "qids": [...]}}。

    卡片归属于其前面最近的 qid 注释（合并行则取同行的 qid）。
    """
    usage = {}
    for name in sec_files:
        path = os.path.join(project, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            events = scan_tex_markers(f.read())
        cur_qid = None
        for ev in events:
            if ev["qid"]:
                cur_qid = ev["qid"]
            if ev["card"]:
                u = usage.setdefault(ev["card"], {"files": [], "qids": []})
                if name not in u["files"]:
                    u["files"].append(name)
                qid = ev["qid"] or cur_qid
                if qid and qid not in u["qids"]:
                    u["qids"].append(qid)
    return usage


def _parse_pack_cards(text):
    """解析材料包中的卡片块，返回 [{card_id, requirement_id, status,
    source_type, verification_text}]（status 为精确字符串，不子串匹配）。"""
    cards = []
    heads = list(_CARD_HEADER.finditer(text))
    for i, m in enumerate(heads):
        block = text[m.end():heads[i + 1].start() if i + 1 < len(heads)
                       else len(text)]
        mtype = re.search(r"\*\*材料类型\*\*:\s*`?([A-Z-]+)`?", block)
        mver = re.search(r"\*\*核验结论\*\*:\s*(.+)", block)
        cards.append({
            "card_id": m.group(1),
            "requirement_id": m.group(2),
            "status": m.group(3),
            "source_type": mtype.group(1) if mtype else "",
            "verification_text": mver.group(1).strip() if mver else "",
        })
    return cards


def build_materials_index(project, sec_files=None):
    """由 material-pack-*.md 的已有证据整理 .pd/materials.json 机器索引
    （不替代原来源摘录）。返回 index dict；供独立小入口或 F08 迁移调用。"""
    project = os.path.abspath(project)
    if sec_files is None:
        manifest = collect_inputs(project)
        sec_files = [f for f in manifest["assembly_order"]
                     if re.match(r"sec", os.path.basename(f))]
    usage = card_usages(project, sec_files)
    cards = []
    packs = sorted(f for f in os.listdir(project)
                   if re.fullmatch(r"material-pack-.*\.md", f))
    for pk in packs:
        with open(os.path.join(project, pk), encoding="utf-8",
                  errors="replace") as f:
            for c in _parse_pack_cards(f.read()):
                anchor = _ANCHOR.search(c["verification_text"])
                if c["source_type"] == "SELF-CONTAINED":
                    locator = "SELF-CONTAINED: 数据与复算过程见核验结论"
                else:
                    locator = ("%s %s" % (anchor.group(1), anchor.group(2))
                               if anchor else "")
                cards.append({
                    "card_id": c["card_id"],
                    "requirement_id": c["requirement_id"],
                    "status": c["status"],
                    "source_type": c["source_type"],
                    "source_locator": locator,
                    "verification_locator": "%s#%s" % (pk, c["card_id"]),
                    "used_by_qids": usage.get(c["card_id"], {}).get(
                        "qids", []),
                })
    cards.sort(key=lambda c: c["card_id"])
    return {"schema_version": SCHEMA_VERSION,
            "tool": "evidence.py build_materials_index",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cards": cards}


def validate_material_chain(project, index, sec_files):
    """材料链五核对（机械一致性；来源与计算的真实性归审读，不在此证明）。

    返回问题列表（空 = 通过）：
      1. 正文引用的卡片在索引中存在且 status 精确等于 approved；
      2. 卡片的 requirement_id 在 research-brief.md 中存在；
      3. 材料包中有该卡片的 approved 批准记录（精确状态匹配）；
      4. 原文/复算证据定位（source_locator/verification_locator）非空；
      5. 引用卡片的实际 qid 在 coverage-map.md 中存在。
    """
    problems = []
    usage = card_usages(project, sec_files)
    cards = {c.get("card_id"): c for c in (index or {}).get("cards", [])}
    if usage and not cards:
        problems.append("正文引用卡片 %s，但 materials.json 无卡片记录"
                        % ", ".join(sorted(usage)))
        return problems

    def _read(name):
        path = os.path.join(project, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    brief = _read("research-brief.md")
    coverage = _read("coverage-map.md")
    packs = {}
    for f in os.listdir(project):
        if re.fullmatch(r"material-pack-.*\.md", f):
            packs[f] = _read(f)

    for cid, u in sorted(usage.items()):
        where = "/".join(u["files"])
        c = cards.get(cid)
        if c is None:
            problems.append("切片 %s 引用卡片 %s，但 materials.json 无索引"
                            % (where, cid))
            continue
        # 1. 状态精确相等：unapproved 不得误匹配
        if c.get("status") != "approved":
            problems.append("卡片 %s 状态为 %r，只有 approved 可进入正文"
                            % (cid, c.get("status")))
        # 2. requirement 在 research-brief 中存在
        req = c.get("requirement_id") or ""
        if brief is None:
            problems.append("缺少 research-brief.md，无法核对卡片 %s 的需求 %s"
                            % (cid, req))
        elif not re.search(r"\b%s\b" % re.escape(req), brief):
            problems.append("卡片 %s 对应需求 %s 不在 research-brief.md 中"
                            % (cid, req))
        # 3. 材料包有批准记录（精确状态）
        approved_in_pack = any(
            pc["card_id"] == cid and pc["status"] == "approved"
            for text in packs.values() for pc in _parse_pack_cards(text))
        if not approved_in_pack:
            problems.append("卡片 %s 在任何 material-pack-*.md 中都无 "
                            "approved 批准记录" % cid)
        # 4. 原文/复算证据定位存在
        if not c.get("source_locator"):
            problems.append("卡片 %s 缺少原文锚点/复算证据定位 "
                            "（source_locator 为空）" % cid)
        if not c.get("verification_locator"):
            problems.append("卡片 %s 缺少核验记录定位 "
                            "（verification_locator 为空）" % cid)
        # 5. 实际 qid 在 coverage 中存在
        for qid in u["qids"]:
            if coverage is None:
                problems.append("缺少 coverage-map.md，无法登记卡片 %s 的 "
                                "实际 qid %s" % (cid, qid))
            elif qid not in coverage:
                problems.append("卡片 %s 引用于 qid %s，但 coverage-map.md "
                                "中不存在该 qid" % (cid, qid))
    return problems


_ACCEPTANCE_REQUIRED = ("schema_version", "scope", "reviewed_qids",
                        "content_digest", "student_pdf_sha256",
                        "conclusion", "reviewer_role", "stage1_record",
                        "stage2_record", "issues")
_ISSUE_REQUIRED = ("id", "qid", "part", "code", "evidence", "status",
                   "resolution", "verified_digest")

# ---------------------------------------------------------------------------
# 修复卡 14：合并终审投影（v2）验收证据
# ---------------------------------------------------------------------------
# 判别式刻意用 review_kind 而**不是** schema_version：旧 v1 记录的
# schema_version 本来就是 SCHEMA_VERSION(=2)，拿版本号当判别式会把历史记录
# 误判成新格式、进而放松旧校验。因此：
#   - 记录里出现 review_kind  → 走 v2 分支（合并终审确定性投影）；
#   - 否则                    → 走下面原封不动的 v1 分支（历史档仍须满足它
#                                自己的全部字段，绝不被新格式放松）。
_ACCEPTANCE_V2_REQUIRED = ("schema_version", "review_kind", "scope",
                           "reviewed_qids", "content_digest", "output_format",
                           "conclusion", "reviewer_role", "issues",
                           "source_comparison", "content_review_refs",
                           "delivery_check_ref", "derived_from")
#: 已知审读种类（未知种类显式拒绝，避免未来种类被旧校验默默放过）
_ACCEPTANCE_REVIEW_KINDS = ("combined_source_fidelity",)
#: 各输出格式必备交付产物（html-only 不含 PDF：不得用零 hash 伪造 PDF 证明）
_ACCEPTANCE_FORMAT_ARTIFACTS = {
    "html": ("student.html",),
    "pdf": ("student.pdf",),
    "both": ("student.pdf", "student.html"),
}
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_DIGEST = "0" * 64


def _is_real_digest(value):
    """真实指纹：64 位小写十六进制且不是全零占位。"""
    return (isinstance(value, str) and bool(_DIGEST_RE.match(value))
            and value != _ZERO_DIGEST)


def _issue_lifecycle_problems(issues, scope, required=None):
    """issue 追溯字段 + 生命周期（v1/v2 同一口径）。"""
    problems = []
    keys = required or _ISSUE_REQUIRED
    for it in issues or []:
        if not isinstance(it, dict):
            problems.append("验收记录 %s 的 issue 必须是对象: %r" % (scope, it))
            continue
        missing = [k for k in keys if k not in it]
        if missing:
            problems.append("验收记录 %s 的 issue 缺少字段 %s: %r"
                            % (scope, ",".join(missing), it.get("id")))
            continue
        status = it.get("status")
        if status == "open":
            continue
        if status == "fixed":
            if not it.get("resolution") or not it.get("verified_digest"):
                problems.append("issue %s 标记 fixed 但缺少改后位置或复验摘要"
                                % it.get("id"))
        elif status == "withdrawn":
            res = str(it.get("resolution") or "").strip()
            if not res or res in ("综合考虑", "已处理"):
                problems.append("issue %s 标记 withdrawn 但未说明误报依据"
                                % it.get("id"))
        else:
            problems.append("issue %s 状态非法: %r（允许 open/fixed/withdrawn）"
                            % (it.get("id"), status))
    return problems


def _validate_acceptance_v2(rec, current_digest=None, student_pdf_sha=None,
                            student_html_sha=None):
    """校验合并终审投影的验收证据（v2）。

    与 v1 的差别只有「证据从哪来」：v1 是审读员直接写的盲审记录，v2 是宿主从
    合并终审主产物确定性投影出来的记录，因此**不要求** stage1_record /
    stage2_record（不得伪造旧盲审体验），但要求更强的可核性：

      - 显式声明 review_kind（未知种类拒绝）；
      - content_review_refs（审读员当时看到的产物指纹）与 delivery_check_ref
        （本轮磁盘实测指纹）**必须分离**：前者是历史，后者是本轮机械验收；
      - 两份引用都必须携带真实 64 位摘要（零 hash / 占位摘要按伪造证据失败）；
      - derived_from 必须能对回父主产物与派发身份；
      - html-only 显式不含 PDF 证明：不得出现 student.pdf 指纹。
    """
    problems = []
    scope = (rec or {}).get("scope", "?")
    for k in _ACCEPTANCE_V2_REQUIRED:
        if k not in rec:
            problems.append("验收记录 %s.json 缺少字段 %s（v2 合并终审投影）"
                            % (scope, k))
    if problems:
        return problems

    kind = str(rec.get("review_kind") or "")
    if kind not in _ACCEPTANCE_REVIEW_KINDS:
        problems.append("验收记录 %s 的 review_kind=%r 不受支持（已知 %s）"
                        % (scope, kind, "/".join(_ACCEPTANCE_REVIEW_KINDS)))
    fmt = str(rec.get("output_format") or "")
    if fmt not in _ACCEPTANCE_FORMAT_ARTIFACTS:
        problems.append("验收记录 %s 的 output_format=%r 非法（允许 %s）"
                        % (scope, rec.get("output_format"),
                           "/".join(_ACCEPTANCE_FORMAT_ARTIFACTS)))
    if rec.get("conclusion") not in ("PASS", "REVISE", "NEEDS_EVIDENCE"):
        problems.append("验收记录 %s 结论非法: %r" % (scope, rec.get("conclusion")))
    if not isinstance(rec.get("reviewed_qids"), list):
        problems.append("验收记录 %s 的 reviewed_qids 必须是数组" % scope)
    if not str(rec.get("reviewer_role") or "").strip():
        problems.append("验收记录 %s 缺少 reviewer_role（审读身份必须角色化）"
                        % scope)
    text = rec.get("source_comparison")
    if not isinstance(text, str) or len(text.strip()) < 8:
        problems.append("验收记录 %s 缺少 source_comparison（忠实性审读证据不得为空）"
                        % scope)
    problems.extend(_issue_lifecycle_problems(rec.get("issues"), scope))

    # ---- derived_from：投影必须能对回父主产物与派发身份 ----
    derived = rec.get("derived_from")
    if not isinstance(derived, dict):
        problems.append("验收记录 %s 的 derived_from 必须是对象（投影必须可溯源）"
                        % scope)
        derived = {}
    else:
        version = derived.get("projection_schema_version")
        if not (isinstance(version, int) and not isinstance(version, bool)):
            problems.append("验收记录 %s 的 derived_from.projection_schema_version"
                            " 缺失或非法：%r（不兼容旧档必须显式拒绝）"
                            % (scope, version))
        if str(derived.get("scope") or "") != str(scope):
            problems.append("验收记录 %s 的 derived_from.scope=%r 与记录 scope 不一致"
                            % (scope, derived.get("scope")))
        for key in ("review_artifact", "dispatch_id"):
            if not str(derived.get(key) or "").strip():
                problems.append("验收记录 %s 的 derived_from 缺少 %s"
                                % (scope, key))
        if not _is_real_digest(derived.get("review_artifact_sha256")):
            problems.append("验收记录 %s 的 derived_from.review_artifact_sha256 不是"
                            "真实摘要（不得用零 hash 冒充父产物）" % scope)

    # ---- content_review_refs vs delivery_check_ref：双重证据必须分离 ----
    content = rec.get("content_review_refs")
    if not isinstance(content, dict) or not content:
        problems.append("验收记录 %s 缺少 content_review_refs（内容审读引用不可缺）"
                        % scope)
        content = {}
    if not _is_real_digest(content.get("review_artifact_sha256")):
        problems.append("验收记录 %s 的 content_review_refs.review_artifact_sha256 "
                        "不是真实摘要" % scope)
    content_digests = content.get("artifact_digests")
    if not isinstance(content_digests, dict) or not content_digests:
        problems.append("验收记录 %s 的 content_review_refs.artifact_digests 必须是"
                        "非空对象（审读员当时看到的交付产物指纹）" % scope)
        content_digests = {}
    if derived and str(content.get("review_artifact_sha256") or "") != str(
            derived.get("review_artifact_sha256") or ""):
        problems.append("验收记录 %s 的 content_review_refs 与 derived_from 指向的"
                        "父产物摘要不一致" % scope)
    if derived and str(content.get("dispatch_id") or "") != str(
            derived.get("dispatch_id") or ""):
        problems.append("验收记录 %s 的 content_review_refs 与 derived_from 的派发"
                        "身份不一致" % scope)

    delivery = rec.get("delivery_check_ref")
    if not isinstance(delivery, dict) or not delivery:
        problems.append("验收记录 %s 缺少 delivery_check_ref（本轮机械验收引用不可缺）"
                        % scope)
        delivery = {}
    delivery_digests = delivery.get("artifact_digests")
    if not isinstance(delivery_digests, dict) or not delivery_digests:
        problems.append("验收记录 %s 的 delivery_check_ref.artifact_digests 必须是"
                        "非空对象（本轮磁盘实测产物指纹）" % scope)
        delivery_digests = {}
    # C7 的「分离」是**溯源分离**，不是「指纹数值必须不同」：同一轮里审读员
    # 看到的正是本轮产物，两份指纹本就应当相等；若强行要求不等，只会把人逼去
    # 伪造第二套指纹（或把正常同轮审读判死）。真正的分离要求是：内容审读引用
    # 必须能追到父主产物 + 派发身份 + 当时的切片指纹（历史），交付核验引用
    # 必须显式标明「本轮磁盘实测」来源（当下）。二者混淆/缺失即失败。
    for key in ("review_artifact", "dispatch_id", "section_digest"):
        if not str(content.get(key) or "").strip():
            problems.append("验收记录 %s 的 content_review_refs 缺少 %s（内容审读"
                            "引用必须可溯源到父产物/派发/切片）" % (scope, key))
    if not str(delivery.get("source") or "").strip():
        problems.append("验收记录 %s 的 delivery_check_ref 缺少 source（交付核验"
                        "必须标明是本轮机械实测，不得与历史审读混淆）" % scope)
    if rec.get("content_review_reused") is True and content_digests and \
            delivery_digests and content_digests == delivery_digests:
        problems.append("验收记录 %s 声明 content_review_reused=true，但内容审读"
                        "指纹与本轮交付实测完全相同（复用标记与证据自相矛盾）"
                        % scope)

    for label, digests in (("content_review_refs", content_digests),
                           ("delivery_check_ref", delivery_digests)):
        for rel, value in sorted(digests.items()):
            if not _is_real_digest(value):
                problems.append("验收记录 %s 的 %s[%s]=%r 不是真实摘要（零 hash/"
                                "占位摘要按伪造证据失败）" % (scope, label, rel, value))

    wanted = _ACCEPTANCE_FORMAT_ARTIFACTS.get(fmt, ())
    for rel in wanted:
        if not _is_real_digest(delivery_digests.get(rel)):
            problems.append("验收记录 %s 的 delivery_check_ref 缺少 %s 的实测指纹"
                            "（%s 格式必须绑定该交付产物）" % (scope, rel, fmt))
    if fmt == "html":
        if rec.get("student_pdf_sha256"):
            problems.append("验收记录 %s 为 html-only，却带 student_pdf_sha256"
                            "（不得伪造 PDF 交付证明）" % scope)
        if "student.pdf" in delivery_digests or "student.pdf" in content_digests:
            problems.append("验收记录 %s 为 html-only，却声明 student.pdf 指纹"
                            "（PDF 按格式显式不适用）" % scope)

    if rec.get("conclusion") == "PASS":
        open_ids = [str(it.get("id")) for it in (rec.get("issues") or [])
                    if isinstance(it, dict) and it.get("status") == "open"]
        if open_ids:
            problems.append("验收记录 %s 结论为 PASS，但存在未处理的 open issue: %s"
                            % (scope, ", ".join(open_ids)))
        if current_digest is not None and rec.get("content_digest") != current_digest:
            problems.append("验收记录 %s 绑定的内容摘要与当前摘要不一致"
                            "（旧 PASS 不能冒充当前结果）" % scope)
        if student_pdf_sha is not None:
            bound = delivery_digests.get("student.pdf") or rec.get("student_pdf_sha256")
            if bound and bound != student_pdf_sha:
                problems.append("验收记录 %s 绑定的 student.pdf 哈希与当前构建不一致"
                                % scope)
        if student_html_sha is not None:
            bound = (delivery_digests.get("student.html")
                     or rec.get("student_html_sha256"))
            if bound and bound != student_html_sha:
                problems.append("验收记录 %s 绑定的 student.html 哈希与当前构建不一致"
                                % scope)
    return problems


def validate_acceptance_json(rec, current_digest=None, student_pdf_sha=None, student_html_sha=None):
    """校验 .pd/acceptance/<scope>.json 的记录一致性（不代替实际审读）。

    返回问题列表（空 = 通过）。规则：
      - 必需字段齐全；conclusion ∈ PASS/REVISE/NEEDS_EVIDENCE；
      - conclusion=PASS 时不得有 status=open 的 issue，且绑定当前
        content_digest 与产物哈希（调用方提供时）；
      - status=fixed 的 issue 必须有改后位置（resolution）与复验摘要
        （verified_digest）；status=withdrawn 必须给出误报依据
        （resolution 非空且不得只填“综合考虑”）。
    """
    # 卡14：v2 = 合并终审确定性投影（判别式 review_kind，不看 schema_version：
    # 旧 v1 记录的 schema_version 本来就是 2，拿版本号判别会放松历史档校验）。
    if isinstance(rec, dict) and "review_kind" in rec:
        return _validate_acceptance_v2(
            rec,
            current_digest=current_digest,
            student_pdf_sha=student_pdf_sha,
            student_html_sha=student_html_sha,
        )
    problems = []
    scope = (rec or {}).get("scope", "?")
    for k in _ACCEPTANCE_REQUIRED:
        if k not in rec:
            problems.append("验收记录 %s.json 缺少字段 %s" % (scope, k))
    if problems:
        return problems
    if rec["conclusion"] not in ("PASS", "REVISE", "NEEDS_EVIDENCE"):
        problems.append("验收记录 %s 结论非法: %r" % (scope, rec["conclusion"]))
    if not isinstance(rec["reviewed_qids"], list):
        problems.append("验收记录 %s 的 reviewed_qids 必须是数组" % scope)
    open_issues = []
    for it in rec["issues"]:
        for k in _ISSUE_REQUIRED:
            if k not in it:
                problems.append("验收记录 %s 的 issue 缺少字段 %s: %r"
                                % (scope, k, it.get("id")))
                break
        else:
            if it["status"] == "open":
                open_issues.append(it["id"])
            elif it["status"] == "fixed":
                if not it["resolution"] or not it["verified_digest"]:
                    problems.append("issue %s 标记 fixed 但缺少改后位置或复验摘要"
                                    % it["id"])
            elif it["status"] == "withdrawn":
                res = (it["resolution"] or "").strip()
                if not res or res in ("综合考虑", "已处理"):
                    problems.append("issue %s 标记 withdrawn 但未说明误报依据"
                                    % it["id"])
            else:
                problems.append("issue %s 状态非法: %r（允许 open/fixed/"
                                "withdrawn）" % (it["id"], it["status"]))
    if rec["conclusion"] == "PASS":
        if open_issues:
            problems.append("验收记录 %s 结论为 PASS，但存在未处理的 open "
                            "issue: %s" % (scope, ", ".join(open_issues)))
        if current_digest is not None \
                and rec["content_digest"] != current_digest:
            problems.append("验收记录 %s 绑定的内容摘要与当前摘要不一致"
                            "（旧 PASS 不能冒充当前结果）" % scope)
        if student_pdf_sha is not None \
                and rec.get("student_pdf_sha256") and rec["student_pdf_sha256"] != student_pdf_sha:
            problems.append("验收记录 %s 绑定的 student.pdf 哈希与当前构建"
                            "不一致" % scope)
        if student_html_sha is not None \
                and rec.get("student_html_sha256") and rec["student_html_sha256"] != student_html_sha:
            problems.append("验收记录 %s 绑定的 student.html 哈希与当前构建"
                            "不一致" % scope)
    return problems


def validate_review_notes(notes, compare_report, compare_report_sha,
                          input_digest, pdf_sha256=None, html_sha256=None):
    """校验 .pd/review-notes.json 与当前 compare 报告/交付物产物的绑定关系。

    返回问题列表（空 = 通过）：报告文件哈希、输入摘要、产物哈希必须
    与当前一致（上次同名文件不能替代本次审查）；compare 报告中的每条
    疑似项（severity=review 的 issue_id）都必须有带判断与证据的复核记录。
    """
    problems = []
    for k in ("schema_version", "compare_report_sha256", "input_digest", "decisions"):
        if k not in notes:
            problems.append("review-notes.json 缺少字段 %s" % k)
    if problems:
        return problems
    if notes["compare_report_sha256"] != compare_report_sha:
        problems.append("review-notes.json 绑定的 compare 报告哈希与当前报告"
                        "不一致（旧复核记录不覆盖新报告）")
    if notes["input_digest"] != input_digest:
        problems.append("review-notes.json 绑定的输入摘要与当前不一致")
    if pdf_sha256 is not None and "pdf_sha256" in notes:
        for k in ("student", "teacher"):
            if (notes["pdf_sha256"] or {}).get(k) != (pdf_sha256 or {}).get(k):
                problems.append("review-notes.json 绑定的 %s.pdf 哈希与当前构建"
                                "不一致（旧复核不覆盖新 PDF）" % k)
    if html_sha256 is not None and "html_sha256" in notes:
        for k in ("student", "teacher"):
            if (notes["html_sha256"] or {}).get(k) != (html_sha256 or {}).get(k):
                problems.append("review-notes.json 绑定的 %s.html 哈希与当前构建"
                                "不一致（旧复核不覆盖新 HTML）" % k)
    issue_ids = {i["issue_id"] for i in compare_report.get("issues", [])
                 if i.get("severity") == "review" and i.get("issue_id")}
    decided = set()
    for d in notes["decisions"]:
        if not isinstance(d, dict) or not d.get("issue_id"):
            problems.append("review-notes.json 存在缺少 issue_id 的复核记录")
            continue
        if d.get("judgment") and d.get("evidence"):
            decided.add(d["issue_id"])
        else:
            problems.append("疑似项 %s 的复核记录缺少判断或证据"
                            % d["issue_id"])
    for missing in sorted(issue_ids - decided):
        problems.append("compare 疑似项 %s 未有复核判断（漏审）" % missing)
    for extra in sorted(decided - issue_ids):
        problems.append("复核记录包含本次报告中不存在的疑似项 %s（过期记录）"
                        % extra)
    return problems


def _main():
    """独立小入口：python evidence.py materials-index <project> [--out PATH]

    由材料包已有证据整理生成 materials.json（默认写到 --out；不加 --out
    时打印到 stdout，绝不自动写入项目 .pd/）。
    """
    import argparse
    ap = argparse.ArgumentParser(description="evidence 共用证据工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("materials-index",
                       help="由 material-pack-*.md 生成 materials.json")
    p.add_argument("project")
    p.add_argument("--out", help="输出路径（缺省打印到 stdout）")
    args = ap.parse_args()
    if args.cmd == "materials-index":
        if not os.path.isdir(args.project):
            print("[evidence] 项目目录不存在: %s" % args.project)
            return 2
        index = build_materials_index(args.project)
        if args.out:
            write_report(args.out, index)
            print("[evidence] materials.json 已写入 %s（%d 张卡片）"
                  % (args.out, len(index["cards"])))
        else:
            print(json.dumps(index, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_main())
