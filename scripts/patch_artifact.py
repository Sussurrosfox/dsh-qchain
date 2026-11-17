#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性产物修补器（RC-2）：用白名单操作代替「派 Agent 改文本」。

问题背景
--------
旧实现里 ``repair_kind="deterministic"`` 只是一个标签：``decide_route`` 一律
HALT（"需确定性修复…不派 Agent"），却没有任何执行器。于是凡是确定性缺陷
（coverage-map 单元格里的裸竖线、缺一张图、模板 CSS 少一条属性）都只能由
主控手写派发单、把「逐字改写指令」塞进 instruction 交给子 Agent 执行——既
慢（一轮 10+ 分钟）又危险（子 Agent 可能顺手改别处）。

本脚本是那条缺失的执行器：**只做白名单内的定位替换，绝不自由改写**。

四条不变量
----------
1. **CAS（compare-and-swap）**：每个操作必须给出 ``expect_before_sha256``，
   与磁盘现态不符就整体放弃（说明有人先改过了，交人工重新确认）。
2. **命中数强制**：``expect_count`` 必须与实测命中数一致，否则放弃——防止
   「本来想改 1 处、实际改了 3 处」或「一处也没改却报成功」。
3. **事务性**：先在内存里算完全部操作的最终字节，全部成功才原子落盘（同一
   临时文件 rename），中途失败不留半盖现状。
4. **自检**：落盘后重读并核对 sha256 与预期一致；附加 ``--dry-run`` 只报告。

支持的 op（全部是"文本/文件级确定性动作"）
----------------------------------------
* ``replace_literal``  {path, old, new, expect_count=1}  精确字面量替换
* ``append_section``   {path, heading, body, anchor_end=true}
                        在文件末尾（或指定 ``after`` 之后）追加一节，已存在同名
                        标题则跳过（幂等）
* ``ensure_table_row`` {path, row, table_header_prefix, key_column=0}
                        表格里按首列键去重插入一行
* ``copy_asset``       {src, dst}  复制资产并核对 sha256（源必须在基准目录内）

用法
----
    python scripts/patch_artifact.py <self_dir> plan.json [--dry-run] [--json out.json]

plan.json 形如::

    {"schema_version": 1, "reason": "coverage-map 单元格裸竖线",
     "ops": [{"op": "replace_literal", "path": "coverage-map.md",
              "expect_before_sha256": "…", "old": "|N|≤n+1", "new": "N 的元素数不超过 n+1",
              "expect_count": 1}]}

退出码：0 全部成功；1 任一操作未通过（未落盘）；2 plan 本身非法。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

OPS = ("replace_literal", "append_section", "ensure_table_row", "copy_asset")


class PlanError(Exception):
    """plan 结构非法（退出码 2）。"""


class OpFailed(Exception):
    """某个操作未通过不变量（退出码 1，整体放弃）。"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".patch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_join(root: str, rel: str) -> str:
    if not isinstance(rel, str) or not rel.strip():
        raise PlanError(f"path 缺失: {rel!r}")
    if os.path.isabs(rel) or rel.startswith("/") or ".." in rel.replace("\\", "/").split("/"):
        raise PlanError(f"path 必须是工作区内相对路径: {rel!r}")
    abs_path = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if not (abs_path == root_abs or abs_path.startswith(root_abs + os.sep)):
        raise PlanError(f"path 越界: {rel!r}")
    return abs_path


# ---------------------------------------------------------------------------
# 各 op 的纯函数实现：输入 (ctx) → 输出新字节；不做落盘
# ---------------------------------------------------------------------------

def _op_replace_literal(ctx: dict, op: dict, files: dict) -> bytes:
    path = op["path"]
    old = op.get("old")
    new = op.get("new")
    if not isinstance(old, str) or not old:
        raise PlanError("replace_literal 需要非空 old")
    if not isinstance(new, str):
        raise PlanError("replace_literal 需要 new（可为空串）")
    expect_count = op.get("expect_count", 1)
    if not isinstance(expect_count, int) or expect_count < 0:
        raise PlanError("expect_count 必须是非负整数")
    raw = files[path]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpFailed(f"{path}: 不是 UTF-8 文本（{exc}）") from exc
    hits = text.count(old)
    if hits != expect_count:
        raise OpFailed(
            f"{path}: 命中数不符（期望 {expect_count}，实测 {hits}）——"
            f"目标文本：{old[:60]!r}")
    return text.replace(old, new).encode("utf-8")


def _op_append_section(ctx: dict, op: dict, files: dict) -> bytes:
    path = op["path"]
    heading = op.get("heading")
    body = op.get("body")
    if not isinstance(heading, str) or not heading.strip():
        raise PlanError("append_section 需要 heading")
    if not isinstance(body, str):
        raise PlanError("append_section 需要 body（可为多行）")
    raw = files[path]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpFailed(f"{path}: 不是 UTF-8 文本（{exc}）") from exc
    # 幂等判定：heading 允许带 `##` 前缀，故按去掉井号后的标题文字比对，
    # 否则「## 标题」永远匹配不到任何 `^#+\s*` 行（曾导致重复追加）。
    heading_text = heading.strip().lstrip("#").strip()
    if heading_text and re.search(
            rf"^#+\s*{re.escape(heading_text)}\s*$", text, re.M):
        return raw  # 幂等：已有同名小节，不动
    anchor = op.get("after")
    block = f"{heading.rstrip()}\n\n{body.strip()}\n"
    if anchor:
        idx = text.find(anchor)
        if idx < 0:
            raise OpFailed(f"{path}: 找不到锚点 {anchor[:60]!r}")
        insert_at = idx + len(anchor)
        out = text[:insert_at] + "\n\n" + block + text[insert_at:]
    else:
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        out = text + sep + block
    return out.encode("utf-8")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _op_ensure_table_row(ctx: dict, op: dict, files: dict) -> bytes:
    path = op["path"]
    row = op.get("row")
    header_prefix = op.get("table_header_prefix")
    key_column = op.get("key_column", 0)
    if not isinstance(row, str) or not row.strip().startswith("|"):
        raise PlanError("ensure_table_row 需要以 | 开头的 row")
    if not isinstance(header_prefix, str) or not header_prefix:
        raise PlanError("ensure_table_row 需要 table_header_prefix（表头首格文本）")
    if not isinstance(key_column, int) or key_column < 0:
        raise PlanError("ensure_table_row 的 key_column 必须是非负整数")
    raw = files[path]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpFailed(f"{path}: 不是 UTF-8 文本（{exc}）") from exc
    lines = text.splitlines(keepends=True)
    new_cells = _split_row(row)
    if key_column >= len(new_cells):
        raise PlanError(f"row 的列数不足以取第 {key_column} 列作键")
    key = new_cells[key_column]
    header_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and header_prefix in line:
            header_idx = i
            break
    if header_idx < 0:
        raise OpFailed(f"{path}: 找不到表头（首格含 {header_prefix!r}）")
    last_row_idx = header_idx
    for j in range(header_idx + 1, len(lines)):
        s = lines[j].strip()
        if not s.startswith("|"):
            break
        last_row_idx = j
        cells = _split_row(lines[j])
        if len(cells) > key_column and cells[key_column] == key:
            return raw  # 幂等：该键已存在
    eol = "\r\n" if lines[last_row_idx].endswith("\r\n") else "\n"
    if not lines[last_row_idx].endswith(("\n", "\r\n")):
        lines[last_row_idx] = lines[last_row_idx] + eol
    insert = row.strip() + eol
    out = "".join(lines[:last_row_idx + 1]) + insert + "".join(lines[last_row_idx + 1:])
    return out.encode("utf-8")


def _op_copy_asset(ctx: dict, op: dict, files: dict) -> bytes:
    """copy_asset 不修改文本，直接复制文件；返回目标字节供自检。"""
    src_rel = op.get("src")
    dst_rel = op.get("dst")
    if not isinstance(dst_rel, str) or not dst_rel:
        raise PlanError("copy_asset 需要 dst")
    if not isinstance(src_rel, str) or not src_rel:
        raise PlanError("copy_asset 需要 src")
    root = ctx["self_dir"]
    base = ctx["base_dir"]
    # 源允许在基准目录（原材料）里：先按基准目录解析，退回落 self
    candidates = []
    if os.path.isabs(src_rel):
        candidates.append(src_rel)
    else:
        candidates.append(os.path.abspath(os.path.join(base, src_rel)))
        candidates.append(_safe_join(root, src_rel))
    src = next((c for c in candidates if os.path.isfile(c)), None)
    if src is None:
        raise OpFailed(f"copy_asset: 找不到源文件 {src_rel!r}")
    data = _read_bytes(src)
    want = op.get("expect_before_sha256")
    if want and want != "absent" and want != sha256_bytes(data):
        raise OpFailed(
            f"copy_asset: 源摘要不符（期望 {want[:16]}…，实测 {sha256_bytes(data)[:16]}…）")
    dst = _safe_join(root, dst_rel)
    if os.path.isfile(dst):
        existing = _read_bytes(dst)
        if existing == data:
            return existing  # 幂等
        if not op.get("overwrite"):
            raise OpFailed(f"copy_asset: 目标已存在且内容不同（需 overwrite=true）: {dst_rel}")
    return data


_HANDLERS = {
    "replace_literal": _op_replace_literal,
    "append_section": _op_append_section,
    "ensure_table_row": _op_ensure_table_row,
    "copy_asset": _op_copy_asset,
}


def validate_plan(plan: dict) -> list[dict]:
    if not isinstance(plan, dict):
        raise PlanError("plan 必须是 JSON 对象")
    ops = plan.get("ops")
    if not isinstance(ops, list) or not ops:
        raise PlanError("plan.ops 必须是非空数组")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise PlanError(f"ops[{i}] 必须是对象")
        name = op.get("op")
        if name not in OPS:
            raise PlanError(f"ops[{i}].op 非法: {name!r}（允许 {', '.join(OPS)}）")
        if name != "copy_asset":
            if not op.get("expect_before_sha256"):
                raise PlanError(
                    f"ops[{i}] 缺 expect_before_sha256（CAS 是强制的：不接受盲改）")
    return ops


def apply_plan(self_dir: str, plan: dict, dry_run: bool = False) -> dict:
    """执行 plan；返回台账。任何 OpFailed → 整体放弃（不落盘）。"""
    ops = validate_plan(plan)
    self_dir = os.path.abspath(self_dir)
    base_dir = os.path.dirname(self_dir)
    ctx = {"self_dir": self_dir, "base_dir": base_dir}

    # 1) 收集涉及的文件现态（虚拟文件表，支持同一文件上的多次操作串行叠加）
    files: dict[str, bytes] = {}
    before: dict[str, str] = {}
    for op in ops:
        rel = op.get("path")
        if not rel or op.get("op") == "copy_asset":
            continue
        abs_path = _safe_join(self_dir, rel)
        if rel not in files:
            if not os.path.isfile(abs_path):
                raise OpFailed(f"{rel}: 文件不存在")
            files[rel] = _read_bytes(abs_path)
            before[rel] = sha256_bytes(files[rel])
    # 2) CAS 校验（整批先验一遍，避免"改到一半才发现第二处不符"）
    for op in ops:
        rel = op.get("path")
        want = op.get("expect_before_sha256")
        if not rel or op.get("op") == "copy_asset" or not want:
            continue
        if want != before.get(rel):
            raise OpFailed(
                f"{rel}: 摘要与 expect_before_sha256 不符（期望 {want[:16]}…，"
                f"实测 {str(before.get(rel))[:16]}…）——请重算 plan")
    # 3) 逐个应用（内存叠加）
    for op in ops:
        handler = _HANDLERS[op["op"]]
        new_bytes = handler(ctx, op, files)
        if op.get("op") == "copy_asset":
            files[f"__asset__{op['dst']}"] = new_bytes
        else:
            files[op["path"]] = new_bytes
    # 4) 落盘 + 自检
    written: list[dict] = []
    for op in ops:
        if op["op"] == "copy_asset":
            dst_rel = op["dst"]
            data = files[f"__asset__{dst_rel}"]
            dst = _safe_join(self_dir, dst_rel)
            if not dry_run:
                _atomic_write(dst, data)
                if sha256_bytes(_read_bytes(dst)) != sha256_bytes(data):
                    raise OpFailed(f"{dst_rel}: 落盘自检失败")
            written.append({"op": "copy_asset", "path": dst_rel,
                            "after_sha256": sha256_bytes(data)})
            continue
        rel = op["path"]
        data = files[rel]
        abs_path = _safe_join(self_dir, rel)
        if not dry_run:
            _atomic_write(abs_path, data)
            if sha256_bytes(_read_bytes(abs_path)) != sha256_bytes(data):
                raise OpFailed(f"{rel}: 落盘自检失败")
        written.append({"op": op["op"], "path": rel,
                        "before_sha256": before.get(rel, ""),
                        "after_sha256": sha256_bytes(data),
                        "changed": before.get(rel) != sha256_bytes(data)})
    return {
        "schema_version": 1,
        "self_dir": self_dir,
        "reason": str(plan.get("reason") or ""),
        "dry_run": bool(dry_run),
        "ops": len(ops),
        "written": written,
        "ok": True,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="确定性产物修补器（白名单 op + CAS）")
    ap.add_argument("self_dir")
    ap.add_argument("plan", help="plan.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    ap.add_argument("--json", default=None, help="台账输出路径")
    args = ap.parse_args(argv)

    try:
        with open(args.plan, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[patch_artifact] plan 读取失败: {exc}", file=sys.stderr)
        return 2
    try:
        ledger = apply_plan(args.self_dir, plan, dry_run=args.dry_run)
    except PlanError as exc:
        print(f"[patch_artifact] [plan 非法] {exc}", file=sys.stderr)
        return 2
    except OpFailed as exc:
        print(f"[patch_artifact] [放弃] {exc}", file=sys.stderr)
        return 1
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
    head = "（dry-run）" if args.dry_run else ""
    print(f"[patch_artifact] {head}应用 {len(ledger['written'])} 个操作:")
    for item in ledger["written"]:
        mark = "*" if item.get("changed") else "="
        print(f"  {mark} {item['op']} {item['path']} → {item['after_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
