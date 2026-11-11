r"""check_fidelity.py — 来源忠实性确定性门禁（audit §18.6）。

七项确定性检查（第一版不要求理解全部数学语义，先阻断可确定的来源漂移）：

1. source coverage  每个未排除的 source_id 都有处置状态；
2. output grounding 每个输出 section/qid/knowledgebox/solution 都有
                    source 或批准 bridge 锚点；
3. order check      faithful 模式 source_id 顺序不得逆序；
4. novelty check    未列入 source inventory 或批准 bridge 的新对象
                    必须停在 PENDING_APPROVAL；
5. extension check  content_policy=faithful 时存在未隔离的 EXTEND
                    立即 FAIL；
6. exclusion check  EXCLUDED_BY_USER 不计覆盖缺口，但不得被正文
                    偷偷引用；
7. approval check   所有 bridge/extension 必须有批准记录和理由；
8. asset check      源文档视觉/结构资产守恒对账：源侧非 excluded 单元
                    内的图形类资产（\includegraphics / tikzcd /
                    tikzpicture，按出现次数计）与表格类资产（tabular /
                    tabular*）总数，不得多于产物 sec*.tex 中的同类总数
                    （tikz 栅格化为 PNG 后以 \includegraphics 计入同类，
                    属合法置换）。少于即 FIDELITY_ASSET_LOST。

输入（workdir = self/ 目录）：
- .pd/source-manifest.json   Phase 0 冻结的输入清单（digest 对账）
- .pd/intent-contract.json   Phase 0 冻结的目标合同（content_policy 等）
- .pd/source-inventory.json  F1 确定性生成的逐单元清单
- .pd/approvals.json         人工批准台账（bridge/extension）
- source-map.md              F2 映射契约：source_id -> scope ->
                             action -> status -> reason
- coverage-map.md            F2 覆盖契约（source_ids/action/new_objects）
- sec*.tex                   正文切片（% source: / % bridge: 锚点）

用法: python check_fidelity.py [工作目录] [--json PATH]
退出码: 0 = 通过, 1 = 存在阻断性问题。
"""

import argparse
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

TOOL_NAME = "check_fidelity.py"
TOOL_VERSION = "1.1"

MANIFEST_REL = os.path.join(".pd", "source-manifest.json")
CONTRACT_REL = os.path.join(".pd", "intent-contract.json")
INVENTORY_REL = os.path.join(".pd", "source-inventory.json")
APPROVALS_REL = os.path.join(".pd", "approvals.json")
SOURCE_MAP_REL = "source-map.md"
COVERAGE_MAP_REL = "coverage-map.md"

#: F2 映射行：source_id -> output_scope -> action -> status -> reason
RE_SOURCE_MAP_ROW = re.compile(
    r"^\s*(SRC-\d+)\s*->\s*(\S+)\s*->\s*([A-Z_]+)\s*->\s*([A-Z_]+)\s*->\s*(.+?)\s*$")
RE_SOURCE_ANCHOR = re.compile(r"%\s*source:\s*(SRC-\d+)", re.IGNORECASE)
RE_BRIDGE_ANCHOR = re.compile(r"%\s*bridge:\s*(BR-[\w-]+)", re.IGNORECASE)
RE_QID_LINE = re.compile(r"^\s*%\s*qid:\s*([^\s%|]+)", re.MULTILINE)
RE_SEC_TEX = re.compile(r"^sec[0-9A-Za-z_-]+\.tex$")
RE_BEGIN_ENV = re.compile(r"\\begin\{(microknowledge|knowledgebox|solution)\}")
RE_END_ENV = re.compile(r"\\end\{(microknowledge|knowledgebox|solution)\}")

#: 第 8 项资产守恒对账：图形类（\includegraphics / \begin{tikzcd} /
#: \begin{tikzpicture}，精确环境名，tikzcd 不会误匹配 tikzpicture，
#: 反之亦然——花括号闭界定）；tikz 栅格化为 PNG 后在产物侧表现为
#: \includegraphics，源/产物两侧同口径计入同一图形类。
RE_ASSET_FIGURE = re.compile(
    r"\\includegraphics(?=[\[{])|\\begin\{(?:tikzcd|tikzpicture)\}")
#: 表格类资产：\begin{tabular} / \begin{tabular*}（不含 array/tabularx）。
RE_ASSET_TABLE = re.compile(r"\\begin\{tabular\*?\}")
#: 单元 locator 的行号区间（如 "§ 2.4.1 定义 L12-80"）。
RE_LOCATOR_LINES = re.compile(r"L(\d+)-(\d+)\s*$")
#: novelty 归一时要剥掉的括号补充说明（捕获括号内文字，供"是否只是空话"判定）。
_RE_PAREN_NOTE = re.compile(r"[\(（\[【]([^\)）\]】]*)[\)）\]】]")
#: novelty 归一收尾时要剥掉的标点（真声明不会以这些字符开头/结尾）。
_NOVELTY_PUNCT = ("。.,，、;；:：!！?？~～-_—–=+*`'\"“”‘’()（）[]【】{}<>《》"
                  "/／\\|·・×∅ø　")
#: novelty 归一用的"空词"碎片：删光后无残渣即视为未声明新对象（见
#: normalize_new_objects 的说明——判定的是残渣，不是穷举整串）。
_EMPTY_TOKENS = (
    "无新增对象", "无新增内容", "无新增", "无扩展", "无新对象", "无变化",
    "未新增任何对象", "未新增", "没有任何", "任何", "暂无", "没有", "无", "未",
    "新增", "扩展", "新对象", "对象", "内容", "变化", "改动", "变更", "沿用",
    "原文", "保持", "不变", "仅",
    "none", "null", "nil", "n/a", "na", "nothing", "empty", "blank", "[]", "()", "{}",
)
_EMPTY_TOKENS_SORTED = tuple(sorted(set(_EMPTY_TOKENS), key=len, reverse=True))

ASSET_KIND_LABEL = {"figure": "图形", "table": "表格"}

ACTIONS = ("PRESERVE", "EXPLAIN", "BRIDGE", "EXTEND")
STATUSES = ("PRESERVED", "EXPLAINED", "BRIDGED",
            "EXCLUDED_BY_USER", "PENDING_APPROVAL")
#: 需要批准台账支撑的动作
APPROVAL_KINDS = {"BRIDGE": "bridge", "EXTEND": "extension"}


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except OSError as e:
        return None, "读取失败: %s" % e
    except ValueError as e:
        return None, "JSON 解析失败: %s" % e


def active_approval_entries(approvals):
    """台账里**生效中**的批准条目（缺 status 视为 active，撤销的剔除）。

    撤销（``ledger.py revoke`` / ``qchain_approve action=revoke``）只改 status，
    不改历史记录——所以这里必须过滤，否则"撤销"在门禁侧等于没发生。
    """
    entries = (approvals or {}).get("approvals") if isinstance(approvals, dict) else None
    return [a for a in (entries or [])
            if isinstance(a, dict)
            and str(a.get("status") or "active").lower() == "active"]


def _input_digest(workdir):
    """受审输入摘要（与 deliver.py 的 input_digest 绑定口径一致）。

    deliver.py 逐报告核对 input_digest（防旧报告冒充本轮证据）；缺失时
    返回空串，由 verify_report 判为失配阻断——宁可误报也不放行陈旧报告。
    """
    try:
        return evidence.content_digest(evidence.collect_inputs(workdir))
    except Exception:
        return ""


def _norm_header(cell):
    return re.sub(r"[\s_\-（）()：:.]", "", cell).lower()


def parse_source_map(self_dir):
    """解析 source-map.md，返回 (rows, parse_errors)。

    rows: [{source_id, scope, action, status, reason, line}]
    """
    path = os.path.join(self_dir, SOURCE_MAP_REL)
    if not os.path.isfile(path):
        return None, [{"code": "FIDELITY_MAP_MISSING",
                       "detail": "缺少 source-map.md（%s）" % SOURCE_MAP_REL}]
    rows, errors = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if "SRC-" not in line or "->" not in line:
                continue
            m = RE_SOURCE_MAP_ROW.match(line)
            if not m:
                if re.match(r"^\s*(SRC-\d+)\s*->", line):
                    errors.append({"code": "FIDELITY_MAP_ROW_FORMAT",
                                   "detail": "source-map.md:%d 映射行格式不合法"
                                             % lineno})
                continue
            rows.append({
                "source_id": m.group(1),
                "scope": m.group(2),
                "action": m.group(3).upper(),
                "status": m.group(4).upper(),
                "reason": m.group(5),
                "line": lineno,
            })
    if not rows and not errors:
        errors.append({"code": "FIDELITY_MAP_NO_ROWS",
                       "detail": "source-map.md 没有任何 SRC-xxx 映射行"})
    for row in rows:
        if row["action"] not in ACTIONS:
            errors.append({"code": "FIDELITY_MAP_BAD_ACTION",
                           "detail": "source-map.md:%d action=%s 非法"
                                     % (row["line"], row["action"])})
        if row["status"] not in STATUSES:
            errors.append({"code": "FIDELITY_MAP_BAD_STATUS",
                           "detail": "source-map.md:%d status=%s 非法"
                                     % (row["line"], row["status"])})
    return rows, errors


def parse_coverage_map(self_dir):
    """解析 coverage-map.md 表格；返回 (rows, warnings)。

    rows: [{source_ids, action, new_objects, scope}]
    表头无法识别时返回 (None, [warning])，跳过 novelty 检查。
    """
    path = os.path.join(self_dir, COVERAGE_MAP_REL)
    if not os.path.isfile(path):
        return None, [{"code": "FIDELITY_COVERAGE_MISSING",
                       "detail": "缺少 coverage-map.md，跳过 novelty 检查"}]
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    header_idx, header = None, None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            norm = [_norm_header(c) for c in cells]
            if any("source" in n and "id" in n for n in norm) and \
                    any(n in ("action", "动作") for n in norm):
                header_idx, header = i, norm
                break
    if header is None:
        return None, [{"code": "FIDELITY_COVERAGE_FORMAT",
                       "detail": "coverage-map.md 未识别出 source_ids/action "
                                 "表头列，跳过 novelty 检查"}]
    col = {name: idx for idx, name in enumerate(header)}
    src_col = next(i for i, n in enumerate(header)
                   if "source" in n and "id" in n)
    act_col = next(i for i, n in enumerate(header)
                   if n in ("action", "动作"))
    new_col = None
    for i, n in enumerate(header):
        if "new" in n or "新增" in n:
            new_col = i
            break
    rows = []
    for line in lines[header_idx + 2:]:
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) <= max(src_col, act_col):
            continue
        rows.append({
            "source_ids": cells[src_col] if src_col < len(cells) else "",
            "action": cells[act_col].upper() if act_col < len(cells) else "",
            "new_objects": cells[new_col] if (new_col is not None
                                              and new_col < len(cells)) else "",
            "scope": cells[col["scope"]] if "scope" in col else "",
        })
    return rows, []


def _novelty_residue(text):
    """把"空词"全删掉后剩下的残渣（去掉标点与空白后的小写形态）。"""
    dense = re.sub(r"\s+", "", text or "").lower()
    residue = dense
    for token in _EMPTY_TOKENS_SORTED:
        residue = residue.replace(token, "")
    return residue.strip(_NOVELTY_PUNCT)


def normalize_new_objects(value):
    """coverage-map「new_objects」列的空值归一（N9 fail-close）。

    审计发现：原实现用一张穷举元组 `("无","无新增","无。","-","—","--",
    "none","NONE","N/A","n/a","无新增对象","∅")` 判空。coverage-planner 写空值
    的写法远不止这些——「无新增。」「无（仅沿用原文）」「未新增任何对象」
    「None」「null」「[]」「暂无」……每漏一个变体，一行本该无事的覆盖记录就被
    FIDELITY_NOVELTY_UNAPPROVED 判成"声明了新对象却没登记扩展"，返修子 Agent
    只能去改一个本来就没问题的格子。

    改为**剥壳 + 残渣判定**（而非穷举）：
      1. 括号里若只是空话（"（仅沿用原文）"）就整段丢掉；括号里若真有内容
         （"（新增 Newton 迭代）"）则保留括号内的文字；
      2. 去掉首尾标点；
      3. 把"空词"逐个删掉，**删完什么都不剩**才判空。
    这样既接住无穷变体，又不会误吞真声明——「无新增对象：正弦定理」删完
    还剩"正弦定理"，照旧阻断。
    """

    def _open_note(match):
        inner = match.group(1) or ""
        return "" if not _novelty_residue(inner) else inner

    raw = (value or "").strip()
    if not raw:
        return ""
    text = _RE_PAREN_NOTE.sub(_open_note, raw)
    text = text.strip(_NOVELTY_PUNCT)
    if not _novelty_residue(text):
        return ""
    return raw


def parse_anchors(self_dir):
    """扫描 sec*.tex 的来源锚点；返回 {sec: {anchors, qid_lines, blocks}}。

    blocks 的锚点判定窗口向上扩展到紧邻 ``\\begin{...}`` 的注释头
    （连续空行/``%`` 注释行），与 T2 写作契约（锚点写在环境之前）一致。
    """
    result = {}
    if not os.path.isdir(self_dir):
        return result
    for name in sorted(os.listdir(self_dir)):
        if not RE_SEC_TEX.match(name) or name == "sec_template.tex":
            continue
        path = os.path.join(self_dir, name)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        anchors = []          # (line_idx, kind, id)
        qid_lines = []        # (line_idx, qid)
        blocks = []           # (env, start_idx, end_idx)
        stack = []
        for idx, line in enumerate(lines):
            for m in RE_SOURCE_ANCHOR.finditer(line):
                anchors.append((idx, "source", m.group(1).upper()))
            for m in RE_BRIDGE_ANCHOR.finditer(line):
                anchors.append((idx, "bridge", m.group(1).upper()))
            m = RE_QID_LINE.match(line)
            if m:
                qid_lines.append((idx, m.group(1)))
            mb = RE_BEGIN_ENV.search(line)
            if mb:
                stack.append((mb.group(1), idx))
            me = RE_END_ENV.search(line)
            if me and stack:
                env, start = stack.pop()
                if env == me.group(1):
                    head = start
                    while head > 0:
                        prev = lines[head - 1].strip()
                        if prev == "" or prev.startswith("%"):
                            head -= 1
                        else:
                            break
                    blocks.append((env, head, idx))
        bounds = [ln for ln, _ in qid_lines] + [len(lines)]
        qid_ranges = [(qid_lines[i][0], bounds[i + 1])
                      for i in range(len(qid_lines))]
        result[name] = {"anchors": anchors, "qid_lines": qid_lines,
                        "blocks": blocks, "qid_ranges": qid_ranges}
    return result


def _scan_asset_lines(text):
    """逐行统计两类资产出现位置（TeX 注释感知：% 注释行不计，\\% 转义
    不误判，与 evidence._strip_comments 同口径）。

    返回 {"figure": [lineno, ...], "table": [lineno, ...]}，同一行出现
    多次则重复记录（按 \\begin / \\includegraphics 次数计）。
    """
    stripped = evidence._strip_comments(text)
    found = {"figure": [], "table": []}
    for lineno, line in enumerate(stripped.splitlines(), 1):
        found["figure"].extend([lineno] * len(RE_ASSET_FIGURE.findall(line)))
        found["table"].extend([lineno] * len(RE_ASSET_TABLE.findall(line)))
    return found


def check_asset_conservation(workdir, manifest, units, rows=None):
    """第 8 项：源文档视觉/结构资产守恒对账。

    源侧：按 manifest 读取各 include 源文件原文，统计两类资产行号；用
    inventory 单元 locator 的 L起-止 行号区间把资产归属到单元，
    disposition == "excluded" 单元内的资产不计入"应保留资产"。locator
    解析失败的文件降级为整体按非 excluded 处理并记 warning（不 FAIL）。
    产物侧：连接 self/sec*.tex 同口径统计。守恒判定：产物侧各类总数
    >= 源侧应保留总数，否则 FIDELITY_ASSET_LOST。

    rows 为 source-map.md 解析行（source_id -> scope），用于在缺失报文里
    指明每个资产“应迁入哪个切片”：scope 命中现有切片用该切片，doc-infra /
    未登记等非切片范围并入末尾切片（学案无独立基础设施切片，致谢/页脚类
    资产按惯例落在最后一节末尾），使 FIDELITY_ASSET_LOST 能路由到
    REPAIR_SECTION 而不是无目标地 REPLAN 回 T1。

    返回 (problems, warnings, stats)；stats = {"source_kept": {...},
    "output": {...}}（各类计数），供报告 checked 区入账。
    """
    problems, warnings = [], []
    project_root = manifest.get("project_root") or os.path.dirname(workdir)

    # source-map 输出范围（source_id -> scope）与现有切片清单
    scope_of = {}
    for r in rows or []:
        sid = str(r.get("source_id") or "").strip().upper()
        scope = str(r.get("scope") or "").strip()
        if sid and scope:
            scope_of[sid] = scope
    sec_ids = []
    if os.path.isdir(workdir):
        sec_ids = [name[:-4] for name in sorted(os.listdir(workdir))
                   if RE_SEC_TEX.match(name) and name != "sec_template.tex"]

    def _target_of(sid):
        """scope → (目标切片 id, 范围说明)；非切片范围并入末尾切片。"""
        s = scope_of.get(str(sid or "").strip().upper(), "")
        m = re.match(r"^(sec[0-9A-Za-z_-]+)", s)
        if m and m.group(1) in sec_ids:
            return m.group(1), s
        tail = sec_ids[-1] if sec_ids else None
        if not s:
            return tail, "未登记输出范围，并入末尾切片 %s" % tail
        return tail, "%s，并入末尾切片 %s" % (s, tail)

    # 单元 locator → 每文件行号区间表
    ranges = {}        # source_file -> [(lo, hi, source_id, disposition)]
    bad_files = set()
    for u in units:
        sf = u.get("source_file") or ""
        m = RE_LOCATOR_LINES.search(str(u.get("locator") or ""))
        if not m:
            bad_files.add(sf)
            continue
        ranges.setdefault(sf, []).append((int(m.group(1)), int(m.group(2)),
                                          u.get("source_id"),
                                          u.get("disposition")))
    for sf in sorted(bad_files):
        warnings.append({
            "code": "FIDELITY_ASSET_LOCATOR_UNKNOWN",
            "detail": "源文件 %s 存在 locator 行号无法解析的单元，该文件"
                      "整体按非 excluded 处理（资产全部计入应保留）" % sf})

    # ---- 源侧计数 ----
    kept = {"figure": [], "table": []}   # [(source_file, lineno, source_id)]
    for s in (manifest.get("sources") or []):
        if not s.get("include", True):
            continue
        src = s.get("path") or ""
        full = src if os.path.isabs(src) else os.path.join(project_root, src)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            warnings.append({
                "code": "FIDELITY_ASSET_SOURCE_UNREADABLE",
                "detail": "源文件 %s 读取失败（%s），资产对账跳过该文件"
                          % (src, e)})
            continue
        found = _scan_asset_lines(text)
        file_ranges = [] if src in bad_files else ranges.get(src, [])
        for kind in ("figure", "table"):
            for ln in found[kind]:
                owner, excluded = None, False
                for lo, hi, sid, disp in file_ranges:
                    if lo <= ln <= hi:
                        owner = sid
                        excluded = disp == "excluded"
                        break
                # 不在任何单元区间的资产（理论上是空白行，防御性兜底）
                # 按应保留处理——宁可多报也不漏报
                if not excluded:
                    tgt, _note = _target_of(owner)
                    kept[kind].append((src, ln, owner, tgt))

    # ---- 产物侧计数 ----
    prod = {"figure": [], "table": []}   # [(sec_file, lineno)]
    if os.path.isdir(workdir):
        for name in sorted(os.listdir(workdir)):
            if not RE_SEC_TEX.match(name) or name == "sec_template.tex":
                continue
            path = os.path.join(workdir, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                found = _scan_asset_lines(f.read())
            for kind in ("figure", "table"):
                prod[kind].extend((name, ln) for ln in found[kind])

    # ---- 守恒判定 ----
    for kind in ("figure", "table"):
        need, got = len(kept[kind]), len(prod[kind])
        if got < need:
            # 按目标切片归因“仍未配齐”的资产：产物侧现有资产先抵扣各目标
            # 切片的应保留数，清单只列抵扣后仍有缺口的切片内资产（源序取
            # 前）。否则缺失清单永远命中第一个目标切片，已补齐的切片会被
            # 反复返修、真正缺资产的切片永远轮不到（REPAIR 死循环）。
            prod_by_sec = {}
            for sec_name, _ln in prod[kind]:
                stem = sec_name[:-4]
                prod_by_sec[stem] = prod_by_sec.get(stem, 0) + 1
            slots = {}
            for e in kept[kind]:
                if e[3]:
                    slots[e[3]] = slots.get(e[3], 0) + 1
            for sec, cnt in prod_by_sec.items():
                if sec in slots:
                    slots[sec] = max(0, slots[sec] - cnt)
            missing, consumed = [], {}
            for e in kept[kind]:
                tgt = e[3]
                if tgt and consumed.get(tgt, 0) < slots.get(tgt, 0):
                    missing.append(e)
                    consumed[tgt] = consumed.get(tgt, 0) + 1
            if not missing:
                # 归因失败（如资产落进了非目标切片）时退回全量清单
                missing = kept[kind]
            listing = "; ".join(
                "%s L%d%s，迁入 %s" % (sf, ln,
                                       "（单元 %s）" % sid if sid else "（未归属单元）",
                                       tgt)
                for sf, ln, sid, tgt in missing[:20])
            more = " …（共 %d 处，仅列前 20）" % len(missing) \
                if len(missing) > 20 else ""
            targets = [e[3] for e in missing if e[3]]
            tsum = "、".join("%s %d 个" % (t, targets.count(t))
                             for t in dict.fromkeys(targets))
            problems.append({
                "code": "FIDELITY_ASSET_LOST",
                "detail": "%s类资产不守恒：源侧应保留 %d 个，产物 sec*.tex "
                          "仅 %d 个，缺失 %d 个。仍未配齐的资产清单（按目标"
                          "切片归因，请人工核对 tikz 栅格化等合法置换）: %s%s。"
                          "按 source-map 输出范围仍未配齐: %s"
                          % (ASSET_KIND_LABEL[kind], need, got, need - got,
                             listing, more, tsum or "无法定位")})

    stats = {
        "source_kept": {k: len(kept[k]) for k in kept},
        "output": {k: len(prod[k]) for k in prod},
    }
    return problems, warnings, stats


def check_fidelity_project(workdir):
    """执行忠实性门禁；返回 {errors, warnings, checked}。"""
    workdir = os.path.abspath(workdir)
    errors, warnings = [], []

    manifest, err = _load_json(os.path.join(workdir, MANIFEST_REL))
    if manifest is None:
        return {"errors": [{"code": "FIDELITY_MANIFEST_MISSING",
                            "detail": "缺少或无法解析 %s（%s）"
                                      % (MANIFEST_REL, err)}],
                "warnings": [],
                "checked": {"sources": 0, "units": 0, "rows": 0},
                "workdir": workdir}
    contract, err = _load_json(os.path.join(workdir, CONTRACT_REL))
    if contract is None:
        return {"errors": [{"code": "FIDELITY_CONTRACT_MISSING",
                            "detail": "缺少或无法解析 %s（%s）"
                                      % (CONTRACT_REL, err)}],
                "warnings": [],
                "checked": {"sources": 0, "units": 0, "rows": 0},
                "workdir": workdir}
    inventory, err = _load_json(os.path.join(workdir, INVENTORY_REL))
    if inventory is None:
        return {"errors": [{"code": "FIDELITY_INVENTORY_MISSING",
                            "detail": "缺少或无法解析 %s（%s）"
                                      % (INVENTORY_REL, err)}],
                "warnings": [],
                "checked": {"sources": 0, "units": 0, "rows": 0},
                "workdir": workdir}

    policy = str(contract.get("content_policy") or "faithful").strip()
    reorder_allowed = bool(contract.get("reorder_allowed"))
    allowed_ext = contract.get("allowed_extensions") or []

    units = inventory.get("units") or []
    unit_by_id = {u.get("source_id"): u for u in units}

    # ---- 批准台账 ----
    # N1：台账现在有写入通路（scripts/ledger.py，工具 qchain_approve）；
    # 这里只认**生效中**的条目——撤销过的批准不得继续放行（原先按全表匹配，
    # revoke 之后 BRIDGE 仍然通过）。
    approvals, _ = _load_json(os.path.join(workdir, APPROVALS_REL))
    approval_entries = active_approval_entries(approvals)
    approval_ids = {str(a.get("id", "")).upper() for a in approval_entries}
    approval_scopes = {(str(a.get("kind", "")).lower(),
                        str(a.get("scope", ""))) for a in approval_entries}

    rows, map_errors = parse_source_map(workdir)
    if rows is None:
        return {"errors": map_errors, "warnings": [],
                "checked": {"sources": len(manifest.get("sources") or []),
                            "units": len(units), "rows": 0},
                "workdir": workdir}
    errors.extend(map_errors)

    rows_by_source = {}
    for r in rows:
        rows_by_source.setdefault(r["source_id"], []).append(r)

    # ---- 1. source coverage ----
    for unit in units:
        sid = unit.get("source_id")
        excluded = unit.get("disposition") == "excluded"
        rs = rows_by_source.get(sid) or []
        if excluded:
            if not any(r["status"] == "EXCLUDED_BY_USER" for r in rs):
                errors.append({
                    "code": "FIDELITY_EXCLUDED_NOT_RECORDED",
                    "detail": "排除单元 %s 未在 source-map.md 登记 "
                              "EXCLUDED_BY_USER" % sid})
        else:
            if not any(r["status"] in ("PRESERVED", "EXPLAINED", "BRIDGED")
                       for r in rs):
                errors.append({
                    "code": "FIDELITY_SOURCE_NOT_COVERED",
                    "detail": "未排除单元 %s（%s）没有 PRESERVED/EXPLAINED/"
                              "BRIDGED 处置记录"
                              % (sid, unit.get("locator") or "")})
    known = set(unit_by_id)
    for r in rows:
        if r["source_id"] not in known:
            errors.append({"code": "FIDELITY_UNKNOWN_SOURCE_ID",
                           "detail": "source-map.md:%d 引用未登记的 %s"
                                     % (r["line"], r["source_id"])})

    # ---- 2/6. output grounding + exclusion ----
    anchors_by_sec = parse_anchors(workdir)
    excluded_ids = {u.get("source_id") for u in units
                    if u.get("disposition") == "excluded"}
    for sec, info in anchors_by_sec.items():
        anchors = info["anchors"]
        anchor_lines = {a[0] for a in anchors}
        # qid 粒度：每个 qid 注释块内必须有 source/bridge 锚点
        qid_lines = info["qid_lines"]
        bounds = [ln for ln, _ in qid_lines] + [10 ** 9]
        for qi, (line_idx, qid) in enumerate(qid_lines):
            block = range(line_idx, bounds[qi + 1])
            if not any(ln in block for ln in anchor_lines):
                errors.append({
                    "code": "FIDELITY_UNGROUNDED_QID",
                    "detail": "%s 的 %s 缺少 %% source: / %% bridge: 锚点"
                              % (sec, qid)})
        # 知识卡/解答环境粒度：锚点可在环境内/紧邻注释头（parse_anchors
        # 已把块起点向上扩展到注释头），或所在 qid 块内（solution 属于其
        # qid，锚点写在 qid 头即覆盖）。
        for env, start, end in info["blocks"]:
            grounded = any(start <= ln <= end for ln in anchor_lines)
            if not grounded:
                for qs, qe in info["qid_ranges"]:
                    if qs <= start and end < qe and any(qs <= ln <= qe
                                                        for ln in anchor_lines):
                        grounded = True
                        break
            if not grounded:
                errors.append({
                    "code": "FIDELITY_UNGROUNDED_BLOCK",
                    "detail": "%s 的 %s 环境（第 %d-%d 行）缺少来源锚点"
                              % (sec, env, start + 1, end + 1)})
        # 排除单元不得被正文引用
        for ln, kind, sid in anchors:
            if kind == "source" and sid in excluded_ids:
                errors.append({
                    "code": "FIDELITY_EXCLUDED_REFERENCED",
                    "detail": "%s 第 %d 行引用了用户排除单元 %s"
                              % (sec, ln + 1, sid)})
            if kind == "source" and sid not in known:
                errors.append({
                    "code": "FIDELITY_UNKNOWN_SOURCE_ID",
                    "detail": "%s 第 %d 行锚点引用未登记 %s"
                              % (sec, ln + 1, sid)})

    # ---- 3. order check ----
    if policy == "faithful" and not reorder_allowed:
        seq = []
        for r in rows:
            if r["source_id"] in known and \
                    r["status"] in ("PRESERVED", "EXPLAINED", "BRIDGED"):
                seq.append((unit_by_id[r["source_id"]].get("source_order"),
                            r["source_id"], r["line"]))
        for (o1, s1, l1), (o2, s2, l2) in zip(seq, seq[1:]):
            if o1 is not None and o2 is not None and o1 > o2:
                errors.append({
                    "code": "FIDELITY_ORDER_VIOLATION",
                    "detail": "source-map.md:%d-%d %s(order=%s) 先于 "
                              "%s(order=%s)，faithful 模式不得逆序"
                              % (l1, l2, s1, o1, s2, o2)})

    # ---- 4/5/6. novelty / extension / approval ----
    cov_rows, cov_warnings = parse_coverage_map(workdir)
    warnings.extend(cov_warnings)
    if cov_rows:
        for cr in cov_rows:
            # 口径归一化（N9）：不再穷举空值写法，改判"剥壳后的残渣"
            nov = normalize_new_objects(cr["new_objects"])
            cr["new_objects"] = nov
            if nov and cr["action"] not in ("BRIDGE", "EXTEND"):
                errors.append({
                    "code": "FIDELITY_NOVELTY_UNAPPROVED",
                    "detail": "coverage-map 行（%s）声明新对象 %r 但 "
                              "action=%s，必须登记为 BRIDGE/EXTEND 请求"
                              % (cr.get("scope") or "?", cr["new_objects"],
                                 cr["action"] or "?")})
    for r in rows:
        act, status = r["action"], r["status"]
        scope = r["scope"]
        if act == "EXTEND":
            if policy == "faithful":
                if status != "PENDING_APPROVAL":
                    errors.append({
                        "code": "FIDELITY_EXTEND_UNAPPROVED",
                        "detail": "source-map.md:%d %s 的 EXTEND 未停在 "
                                  "PENDING_APPROVAL，faithful 模式禁止未批准"
                                  "扩展进入正文" % (r["line"], r["source_id"])})
                else:
                    warnings.append({
                        "code": "FIDELITY_EXTEND_PENDING",
                        "detail": "source-map.md:%d %s 的 EXTEND 等待人工"
                                  "批准" % (r["line"], r["source_id"])})
            else:
                hit = any(
                    (str(e.get("scope", "")) == scope
                     or scope.startswith(str(e.get("scope", ""))))
                    for e in allowed_ext)
                if not hit:
                    errors.append({
                        "code": "FIDELITY_EXTEND_NOT_ALLOWED",
                        "detail": "source-map.md:%d %s 的 EXTEND(scope=%s) "
                                  "不在 intent contract 的 allowed_extensions"
                                  % (r["line"], r["source_id"], scope)})
        if act == "BRIDGE" and status == "BRIDGED":
            ok = any((k == "bridge" and sc == scope)
                     for k, sc in approval_scopes)
            if not ok:
                errors.append({
                    "code": "FIDELITY_BRIDGE_UNAPPROVED",
                    "detail": "source-map.md:%d %s 的 BRIDGE(scope=%s) 缺少"
                              " .pd/approvals.json 批准记录（需 kind=bridge "
                              "且带 reason；由真人用 qchain_approve / "
                              "scripts/ledger.py approve 登记）" % (r["line"], r["source_id"],
                                                scope)})
    # 正文 bridge 锚点必须有批准 id
    for sec, info in anchors_by_sec.items():
        for ln, kind, bid in info["anchors"]:
            if kind == "bridge" and bid not in approval_ids:
                errors.append({
                    "code": "FIDELITY_BRIDGE_UNAPPROVED",
                    "detail": "%s 第 %d 行 %% bridge: %s 未在 "
                              ".pd/approvals.json 批准（可由真人用 "
                              "qchain_approve 以该 id 登记）" % (sec, ln + 1, bid)})

    # ---- 7. exclusion 对账（contract vs manifest）----
    manifest_excluded = {os.path.basename(s.get("path", ""))
                         for s in (manifest.get("sources") or [])
                         if not s.get("include", True)}
    contract_excluded = set()
    for raw in (contract.get("excluded_sources") or []):
        text = str(raw)
        for sep in ("::", "|"):
            if sep in text:
                text = text.split(sep)[0]
                break
        contract_excluded.add(os.path.basename(text.strip()))
    if manifest_excluded != contract_excluded:
        errors.append({
            "code": "FIDELITY_EXCLUSION_MISMATCH",
            "detail": "intent contract 的 excluded_sources 与 source "
                      "manifest 的 include=false 清单不一致"})

    # ---- 0. 输入 digest 对账（内容漂移立即 FAIL）----
    project_root = manifest.get("project_root") or os.path.dirname(workdir)
    for s in (manifest.get("sources") or []):
        if not s.get("include", True):
            continue
        src = s.get("path") or ""
        full = src if os.path.isabs(src) else os.path.join(project_root, src)
        if not os.path.isfile(full):
            errors.append({"code": "FIDELITY_SOURCE_MISSING",
                           "detail": "输入 %s 已不存在" % src})
            continue
        actual = "sha256:%s" % _sha256_file(full)
        if actual != s.get("digest"):
            errors.append({
                "code": "FIDELITY_SOURCE_DIGEST_MISMATCH",
                "detail": "输入 %s 的 sha256 与冻结清单不一致"
                          "（期望 %s，实际 %s）"
                          % (src, s.get("digest"), actual)})

    # ---- 8. 资产守恒对账（源文档视觉/结构资产 vs 产物 sec*.tex）----
    asset_problems, asset_warnings, asset_stats = check_asset_conservation(
        workdir, manifest, units, rows)
    errors.extend(asset_problems)
    warnings.extend(asset_warnings)

    bridges = sorted({
        bid for info in anchors_by_sec.values()
        for _, kind, bid in info["anchors"] if kind == "bridge"})
    return {
        "errors": errors,
        "warnings": warnings,
        "checked": {
            "sources": len(manifest.get("sources") or []),
            "units": len(units),
            "rows": len(rows),
            "sections": len(anchors_by_sec),
            "content_policy": policy,
            "bridges": bridges,
            "assets": asset_stats,
        },
        "workdir": workdir,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="来源忠实性确定性门禁 (check_fidelity.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    res = check_fidelity_project(args.workdir)
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
        print("[check_fidelity] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_fidelity] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_fidelity] 发现 %d 个来源忠实性问题（工作区: %s）:"
              % (len(errors), res["workdir"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    checked = res["checked"]
    print("[check_fidelity] 通过：来源忠实性合规（%d 个输入 / %d 个单元 / "
          "%d 条映射 / %d 个切片，策略 %s，bridge %d 个）。"
          % (checked["sources"], checked["units"], checked["rows"],
             checked["sections"], checked["content_policy"],
             len(checked["bridges"])))
    assets = checked.get("assets")
    if assets:
        print("[check_fidelity] 资产守恒：图形 源 %d → 产物 %d；表格 源 %d "
              "→ 产物 %d。"
              % (assets["source_kept"]["figure"], assets["output"]["figure"],
                 assets["source_kept"]["table"], assets["output"]["table"]))
    return 0


check_fidelity = check_fidelity_project


if __name__ == "__main__":
    sys.exit(main())
