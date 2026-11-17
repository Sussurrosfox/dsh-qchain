#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产收割：把原始材料引用到的图片/附件确定性复制进 self/。

背景（N1，2026-11-17 实测教训）：流水线把原材料拆成 sec*.tex 写进 self/ 并
在 self/ 内编译，但 setup_self 只复制 LaTeX 模板，**从不搬运原材料引用的图
片**。于是 ``\\includegraphics{fig_1.jpg}`` 在 self/ 内找不到文件，LaTeX 编译
以 ``! Unable to load picture or PDF file`` 失败，而这个错误既不是内容问题也
不是环境问题——它是**资产缺口**。旧实现把它当成内容失败派给切片写作者返修，
子 Agent 改 sec*.tex 永远修不好（图不在那里），白烧一轮返修预算。

本脚本做一件事：扫描「源头 TeX（基准目录下的原材料）+ self/ 内的 sec*.tex /
main.tex」里所有 ``\\includegraphics`` 引用与 ``\\graphicspath`` 声明，把能在
基准目录（递归）里找到的文件复制到 self/ 根，命名按引用原文的 basename，并落
一份 ``.pd/reports/assets-harvest.json`` 台账（来源路径、目标路径、sha256）。

设计约束：
  * 幂等：已存在且 sha256 相同则跳过；内容不同且未 ``--force`` 则只告警不改写。
  * 只读基准目录：绝不修改、绝不移动原材料。
  * 找不到的引用不报错（可能由后续环节补），但会记进台账 ``missing`` 列表，
    供 check_fidelity 的资产定位门禁与人工核对。
  * 不确定性行为可审计：所有动作（copied / skipped / missing / conflict）逐条
    落台账，不静默。

用法：
    python scripts/harvest_assets.py <self_dir> [--force] [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

# \includegraphics[可选参数]{文件名}；文件名可能含空格（罕见）但通常不含花括号
_RE_INCLUDEGRAPHICS = re.compile(
    r"\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
_RE_GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})+)\s*\}")
_RE_GRAPHICSPATH_ITEM = re.compile(r"\{([^{}]*)\}")

ASSET_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".gif", ".bmp",
              ".tif", ".tiff", ".webp")

LEDGER_REL = os.path.join(".pd", "reports", "assets-harvest.json")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def collect_refs(self_dir: str, base_dir: str) -> dict:
    """收集引用：返回 {"names": [引用原文...], "graphicspaths": [目录...]}。"""
    names: list[str] = []
    gpaths: list[str] = []

    tex_files: list[str] = []
    # (a) 基准目录下的源头 TeX（原材料本身；可能就在 base_dir 根，也可能在子目录）
    if base_dir and os.path.isdir(base_dir):
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "node_modules", "__pycache__", "self")]
            for fn in files:
                if fn.lower().endswith((".tex", ".md")):
                    tex_files.append(os.path.join(root, fn))
            # 只扫一层子目录，避免把无关的大树整个走一遍
            if os.path.abspath(root) != os.path.abspath(base_dir):
                dirs[:] = []
    # (b) self/ 内已生成的切片与主装配文件
    if os.path.isdir(self_dir):
        for fn in sorted(os.listdir(self_dir)):
            if fn.lower().endswith(".tex"):
                tex_files.append(os.path.join(self_dir, fn))

    seen: set[str] = set()
    for path in tex_files:
        if os.path.abspath(path).startswith(os.path.abspath(self_dir) + os.sep):
            # self/ 内的文件不贡献新引用（但它的引用照样要被满足，故也扫）
            pass
        text = _read_text(path)
        if not text:
            continue
        for m in _RE_INCLUDEGRAPHICS.finditer(text):
            ref = m.group(1).strip()
            if ref and ref not in seen:
                seen.add(ref)
                names.append(ref)
        for m in _RE_GRAPHICSPATH.finditer(text):
            for item in _RE_GRAPHICSPATH_ITEM.findall(m.group(1)):
                item = item.strip()
                if item and item not in gpaths:
                    gpaths.append(item)
    return {"names": names, "graphicspaths": gpaths, "scanned": tex_files}


def _candidate_paths(ref: str, base_dir: str, gpaths: list[str]) -> list[str]:
    """引用 → 可能的磁盘位置（按优先级），只返回真实存在的文件。"""
    rel = ref.replace("\\", "/").lstrip("./")
    cands: list[str] = []
    # 1) graphicspath + 引用
    for gp in gpaths:
        gp_clean = gp.replace("\\", "/").strip()
        if gp_clean.startswith("{") and gp_clean.endswith("}"):
            gp_clean = gp_clean[1:-1]
        cands.append(os.path.join(base_dir, gp_clean, rel))
    # 2) 基准目录根直接命中
    cands.append(os.path.join(base_dir, rel))
    # 3) 基准目录递归按 basename 命中（取最短路径，最稳）
    base_name = os.path.basename(rel)
    if base_dir and os.path.isdir(base_dir):
        hits = []
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "node_modules", "__pycache__", "self")]
            if base_name in files:
                hits.append(os.path.join(root, base_name))
        hits.sort(key=len)
        cands.extend(hits)
    out: list[str] = []
    for c in cands:
        if os.path.isfile(c) and os.path.abspath(c) not in \
                [os.path.abspath(x) for x in out]:
            out.append(c)
    return out


def harvest(self_dir: str, force: bool = False, quiet: bool = False) -> dict:
    """执行收割；返回台账 dict（同时写入 .pd/reports/assets-harvest.json）。"""
    self_dir = os.path.abspath(self_dir)
    base_dir = os.path.dirname(self_dir)
    refs = collect_refs(self_dir, base_dir)

    ledger: dict = {
        "schema_version": 1,
        "self_dir": self_dir,
        "base_dir": base_dir,
        "scanned_files": len(refs["scanned"]),
        "references": len(refs["names"]),
        "copied": [],
        "skipped": [],
        "conflict": [],
        "missing": [],
    }

    for ref in refs["names"]:
        rel = ref.replace("\\", "/").lstrip("./")
        dst = os.path.join(self_dir, os.path.basename(rel))
        src_cands = _candidate_paths(ref, base_dir, refs["graphicspaths"])
        if not src_cands:
            ledger["missing"].append(ref)
            continue
        src = src_cands[0]
        src_sha = sha256_file(src)
        if os.path.isfile(dst):
            dst_sha = sha256_file(dst)
            if dst_sha == src_sha:
                ledger["skipped"].append(
                    {"ref": ref, "path": os.path.relpath(dst, self_dir),
                     "sha256": src_sha, "reason": "identical"})
                continue
            if not force:
                ledger["conflict"].append(
                    {"ref": ref, "path": os.path.relpath(dst, self_dir),
                     "src": src, "src_sha256": src_sha, "dst_sha256": dst_sha,
                     "reason": "target exists with different content"})
                continue
        shutil.copy2(src, dst)
        ledger["copied"].append(
            {"ref": ref, "path": os.path.relpath(dst, self_dir), "src": src,
             "sha256": src_sha})

    ledger["ok"] = not ledger["missing"] and not ledger["conflict"]
    path = os.path.join(self_dir, LEDGER_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)

    if not quiet:
        print(f"[harvest_assets] 引用 {ledger['references']} 个 · 复制 "
              f"{len(ledger['copied'])} · 跳过 {len(ledger['skipped'])} · "
              f"冲突 {len(ledger['conflict'])} · 缺失 {len(ledger['missing'])}")
        for item in ledger["missing"]:
            print(f"[harvest_assets] [缺失] {item}（未在基准目录找到）")
        for item in ledger["conflict"]:
            print(f"[harvest_assets] [冲突] {item['path']} 内容不同（用 --force 覆盖）")
    return ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把原材料引用的图片收割进 self/")
    ap.add_argument("self_dir", help="self 工作区目录（其父目录为原材料基准目录）")
    ap.add_argument("--force", action="store_true", help="目标内容不同时覆盖")
    ap.add_argument("--json", default=None, help="额外把台账写到此路径")
    args = ap.parse_args(argv)

    ledger = harvest(args.self_dir, force=args.force)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
    # 退出码：冲突算失败（需要人决定），缺失只告警（可能由后续环节补）
    return 1 if ledger["conflict"] else 0


if __name__ == "__main__":
    sys.exit(main())
