"""source_inventory.py — F1：确定性生成 source-inventory.json（audit §18.4）。

在 T1 让 Agent 规划之前，先把 Phase 0 冻结的 source-manifest.json 展开为
逐单元的 source-inventory.json，使后续映射（F2）与忠实性门禁（F3）可以
按 source_id 逐条对账。本脚本零 LLM、零随机：同一 manifest + 同一文件
内容必然产出逐字节一致的清单（generated_at 不参与自摘要）。

每个原材料单元：
  source_id    SRC-001 …（按 manifest 登记顺序全局递增）
  source_file  manifest 中的相对路径
  locator      "heading 路径 L起-止"（heading/line range）
  kind         definition|claim|proof|example|exercise|goal
  text_digest  sha256:<单元文本归一化后的摘要>
  source_order 1..N 全局顺序（F3 order check 的依据）
  disposition  preserve（纳入）| excluded（用户明确排除，不计覆盖缺口）

用法：
  python source_inventory.py --self-dir <self>
  python source_inventory.py --self-dir <self> --check   # 校验现清单未过期
"""

import argparse
import hashlib
import json
import os
import re
import sys

TOOL_VERSION = "1.0"
SCHEMA_VERSION = 1
INVENTORY_REL = os.path.join(".pd", "source-inventory.json")

SOURCE_ROLES = ("source-baseline", "optional-problem")
KINDS = ("definition", "claim", "proof", "example", "exercise", "goal")

_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RE_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$")
_RE_FENCE = re.compile(r"^\s*(?:```|~~~)")
_RE_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

_KIND_RULES = (
    ("goal", ("课时定位", "学习目标", "课堂流程", "达标", "目标", "定位",
              "goal", "objective", "overview")),
    ("definition", ("定义", "definition")),
    ("proof", ("证明", "proof")),
    ("example", ("例题", "示例", "例：", "例:", "example")),
    ("exercise", ("习题", "练习", "题目", "复习题", "exercise", "problem",
                  "question")),
    ("claim", ("定理", "命题", "引理", "推论", "结论", "性质", "法则", "公式",
               "theorem", "lemma", "proposition", "corollary", "claim")),
)


class InventoryError(Exception):
    def __init__(self, code, detail):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def _norm_text(text):
    """行尾空白归一化 + 去掉首尾空行（digest 与展示都基于它）。"""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _raw_digest(path):
    """文件原始字节摘要（与 workflow.contracts.file_digest 同口径）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _digest(text):
    return "sha256:" + hashlib.sha256(
        _norm_text(text).encode("utf-8")).hexdigest()


def _classify(text, heading):
    low = (heading + "\n" + text).lower()
    for kind, needles in _KIND_RULES:
        for needle in needles:
            if needle.lower() in low:
                return kind
    return "claim"


def _heading_path(stack):
    return " > ".join(h for h in stack if h) or "(root)"


def _split_units(text):
    """把 markdown 源文件切成确定性单元 [(text, heading, line_start, line_end)]。

    规则（无随机、无 LLM）：
      - ATX 标题行自成单元，并进入 heading 栈；
      - 列表项（- * + 或 1. ）各成单元，续行并入该项；
      - 空行结束段落单元；围栏代码块整体成一个单元；
      - 水平分割线丢弃。
    """
    lines = text.splitlines()
    units = []
    stack = []
    i = 0
    n = len(lines)
    para_start = None

    def flush_para(end_line):
        nonlocal para_start
        if para_start is None:
            return
        body = "\n".join(lines[para_start:end_line])
        norm = _norm_text(body)
        if norm:
            units.append((norm, _heading_path(stack), para_start + 1,
                          end_line))
        para_start = None

    while i < n:
        line = lines[i]
        m_head = _RE_HEADING.match(line)
        if m_head:
            flush_para(i)
            level = len(m_head.group(1))
            title = m_head.group(2).strip()
            del stack[level - 1:]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            units.append((title, _heading_path(stack[:-1]) if len(stack) > 1
                          else "(root)", i + 1, i + 1))
            i += 1
            continue
        if _RE_FENCE.match(line):
            flush_para(i)
            j = i + 1
            while j < n and not _RE_FENCE.match(lines[j]):
                j += 1
            j = min(j + 1, n)
            body = _norm_text("\n".join(lines[i:j]))
            if body:
                units.append((body, _heading_path(stack), i + 1, j))
            i = j
            continue
        if _RE_HR.match(line):
            flush_para(i)
            i += 1
            continue
        m_item = _RE_LIST_ITEM.match(line)
        if m_item:
            flush_para(i)
            item_start = i
            j = i + 1
            while j < n:
                nxt = lines[j]
                if (not nxt.strip() or _RE_HEADING.match(nxt)
                        or _RE_LIST_ITEM.match(nxt) or _RE_FENCE.match(nxt)
                        or _RE_HR.match(nxt)):
                    if nxt.strip() and not _RE_LIST_ITEM.match(nxt) and \
                            not _RE_HEADING.match(nxt) and \
                            not _RE_FENCE.match(nxt):
                        # 缩进续行并入当前列表项
                        if nxt[:1] in (" ", "\t"):
                            j += 1
                            continue
                    break
                j += 1
            body = _norm_text("\n".join(lines[item_start:j]))
            if body:
                units.append((body, _heading_path(stack), item_start + 1, j))
            i = j
            continue
        if not line.strip():
            flush_para(i)
            i += 1
            continue
        if para_start is None:
            para_start = i
        i += 1
    flush_para(n)
    return units


def _load_manifest(manifest_path):
    if not os.path.isfile(manifest_path):
        raise InventoryError(
            "MANIFEST_MISSING",
            "未找到冻结的 source-manifest.json：%s（Phase 0 必须先冻结契约）"
            % manifest_path)
    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def build_inventory(manifest, project_root):
    """纯函数：manifest dict → inventory dict（不读时钟以外的外部状态）。"""
    units = []
    order = 0
    seen_ids = set()
    for entry in manifest.get("sources", []):
        rel = entry.get("path")
        role = entry.get("role")
        included = bool(entry.get("include"))
        disposition = "preserve" if included else "excluded"
        if role not in SOURCE_ROLES:
            raise InventoryError(
                "SOURCE_ROLE_INVALID",
                "manifest 条目 role=%r 非法（%s）" % (role, rel))
        abspath = rel if os.path.isabs(rel) else os.path.join(
            project_root, rel)
        if not os.path.isfile(abspath):
            raise InventoryError(
                "SOURCE_MISSING", "清单文件不存在：%s" % rel)
        with open(abspath, "r", encoding="utf-8-sig") as f:
            text = f.read()
        # manifest 的 digest 是文件原始字节摘要（contracts.file_digest），
        # 与单元级归一化摘要不同域，必须同口径对账。
        actual = _raw_digest(abspath)
        expected = entry.get("digest")
        if expected and expected != actual:
            raise InventoryError(
                "SOURCE_DIGEST_MISMATCH",
                "文件 %s 内容与冻结 digest 不一致（下游篡改或未走契约更新）"
                % rel)
        for body, heading, start, end in _split_units(text):
            order += 1
            source_id = "SRC-%03d" % order
            if source_id in seen_ids:
                raise InventoryError(
                    "SOURCE_ID_COLLISION", "source_id 冲突：%s" % source_id)
            seen_ids.add(source_id)
            units.append({
                "source_id": source_id,
                "source_file": rel,
                "locator": "%s L%d-%d" % (heading, start, end),
                "kind": _classify(body, heading),
                "text_digest": _digest(body),
                "source_order": order,
                "disposition": disposition,
            })
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "project_root": project_root,
        "manifest_digest": manifest.get("digest", ""),
        "unit_count": len(units),
        "units": units,
    }
    inventory["digest"] = "sha256:" + hashlib.sha256(json.dumps(
        {k: v for k, v in inventory.items()
         if k not in ("digest", "generated_at")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return inventory


def write_inventory(self_dir, inventory):
    pd_dir = os.path.join(self_dir, ".pd")
    os.makedirs(pd_dir, exist_ok=True)
    out = os.path.join(self_dir, INVENTORY_REL)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, out)
    return out


def load_inventory(self_dir):
    path = os.path.join(self_dir, INVENTORY_REL)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def inventory_is_stale(self_dir, manifest):
    """现清单是否已过期（manifest 摘要变化或清单缺失）。"""
    inv = load_inventory(self_dir)
    if inv is None:
        return True
    return inv.get("manifest_digest") != manifest.get("digest")


def resolve_self_dir(explicit):
    if explicit:
        return explicit
    return os.path.join(os.getcwd(), "self")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="F1: 由冻结 source-manifest.json 确定性生成 "
                    "source-inventory.json")
    parser.add_argument("--self-dir", default=None,
                        help="self 工作区目录（缺省 ./self）")
    parser.add_argument("--manifest", default=None,
                        help="manifest 路径（缺省 <self-dir>/.pd/"
                             "source-manifest.json）")
    parser.add_argument("--out", default=None, help="输出路径（缺省 .pd/"
                        "source-inventory.json）")
    parser.add_argument("--check", action="store_true",
                        help="只校验现清单是否最新，不重写")
    args = parser.parse_args(argv)

    self_dir = resolve_self_dir(args.self_dir)
    manifest_path = args.manifest or os.path.join(
        self_dir, ".pd", "source-manifest.json")
    try:
        manifest = _load_manifest(manifest_path)
        project_root = manifest.get("project_root") or os.path.dirname(
            os.path.dirname(manifest_path))
        inventory = build_inventory(manifest, project_root)
        if args.check:
            existing = load_inventory(self_dir)
            if existing is None:
                print("[inventory] 缺失 source-inventory.json")
                return 1
            if existing.get("digest") != inventory.get("digest"):
                print("[inventory] source-inventory.json 已过期（manifest 或"
                      "源文件内容变化）")
                return 1
            print("[inventory] source-inventory.json 最新（%d 单元）"
                  % inventory.get("unit_count", 0))
            return 0
        out = write_inventory(self_dir, inventory) if not args.out else (
            _write_out(args.out, inventory))
        print("[inventory] source-inventory.json 已生成（%d 单元）→ %s"
              % (inventory.get("unit_count", 0), out))
        return 0
    except InventoryError as exc:
        print("[inventory] [错误] %s: %s" % (exc.code, exc.detail),
              file=sys.stderr)
        return 2


def _write_out(out_path, inventory):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, out_path)
    return out_path


if __name__ == "__main__":
    sys.exit(main())
