#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tikz_rasterize.py — 源文档侧 TikZ 预栅格化工具（qchain 流水线前置步骤）。

用途
----
真实事故背景：含 tikzcd/tikzpicture 的源文档在 T2 切片阶段被子 Agent 静默丢弃，
导致学案缺图。本工具在流水线 run 之前对源文档做**预栅格化**：

1. 提取 ``\\begin{tikzcd}...\\end{tikzcd}`` 与 ``\\begin{tikzpicture}...\\end{tikzpicture}``
   环境块（re.DOTALL 非贪婪，两种环境精确区分，跳过 ``%`` 注释行内的假环境）；
2. 逐块生成独立 standalone 文档，用 xelatex 编译为 PDF；
3. 用 pdftoppm 栅格化为 PNG，输出到 ``--out-dir``（默认 ``<源目录>/assets``），
   命名 ``<prefix>-NN.png``（NN 按源中出现顺序 01 起）。

栅格化后由人工或脚本把源文档中的 tikz 块替换为 ``\\includegraphics`` 引用，
再正常跑流水线（tex2cards 对 includegraphics 已有成熟支持）。``--rewrite OUT.tex``
模式把该替换步骤自动化：全部块成功后写出重写版源文件，块外内容逐字节保持不变。

源文档导言区中的依赖定义行（``\\newcommand``/``\\def``/``\\usetikzlibrary`` 等）
默认自动提取并注入 standalone 导言区（``extract_preamble_macros``，可用
``--no-auto-preamble`` 关闭），使含自定义宏或 tikz 库依赖的 TikZ 图无需人工准备
``--preamble`` 文件即可编译。

CLI
---
    python scripts/tikz_rasterize.py --source FILE.tex --list [--json]
    python scripts/tikz_rasterize.py --source FILE.tex [--out-dir DIR]
        [--prefix NAME] [--dpi 300] [--preamble EXTRA.tex ...]
        [--no-auto-preamble] [--only 1,3]
        [--rewrite OUT.tex] [--json]

退出码：0 = 全部成功（或 --list）；2 = 任一图编译/栅格化失败（``--rewrite``
模式下不写出 OUT.tex）；1 = 环境或参数错误。

仅依赖 Python 标准库；xelatex / pdftoppm 缺失时明确报错。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

# 两种环境精确区分；re.DOTALL 非贪婪；\\1 反向引用保证 begin/end 环境名一致。
ENV_PATTERN = re.compile(
    r"\\begin\{(tikzcd|tikzpicture)\}.*?\\end\{\1\}", re.DOTALL
)

# standalone 包装模板（str.replace 令牌替换，避开 LaTeX 花括号与 str.format 冲突）。
# 导言区显式加载 tikz-cd 包（复盘 §4.3.1）：tikz-cd 定义 tikzcd 环境并自带 tikz，
# 比 tikz + \usetikzlibrary{cd} 更贴近 tikzcd 生态的标准用法。
STANDALONE_TEMPLATE = (
    "\\documentclass[tikz,border=2pt]{standalone}\n"
    "\\usepackage{ctex}\n"
    "\\usepackage{tikz-cd}\n"
    "\\usepackage{amsmath,amssymb,mathtools,bm}\n"
    "@PREAMBLE@\n"
    "\\begin{document}\n"
    "@BODY@\n"
    "\\end{document}\n"
)

# 源导言区依赖定义行：宏定义命令 + tikz 库声明（行首允许空白即整行机械提取）。
# \usetikzlibrary 一并提取：真实文档的 TikZ 图常依赖 calc 等库（见 GR_4 蛇形引理图），
# 不提取则 standalone 编译缺库失败。
PREAMBLE_MACRO_RE = re.compile(
    r"^\s*\\(?:newcommand|renewcommand|providecommand|DeclareMathOperator"
    r"|newenvironment|renewenvironment|def|usetikzlibrary)\b"
)

XELATEX_ARGS = ["-interaction=nonstopmode", "-halt-on-error"]
COMPILE_TIMEOUT = 120  # 单图 xelatex 超时（秒）
LOG_TAIL_LINES = 30  # 失败时收集的 log 尾部行数


@dataclass
class Block:
    """一个 TikZ 环境块。"""

    index: int  # 1 起，按源中出现顺序
    env: str  # "tikzcd" | "tikzpicture"
    start_line: int  # 1 起，含 \begin 行
    end_line: int  # 1 起，含 \end 行
    chars: int  # 源码字符数
    source: str  # 原始源码（含内部注释，逐字保留）
    char_start: int  # 在原文字符串中的起始偏移（含）
    char_end: int  # 在原文字符串中的结束偏移（不含）


def _mask_comments(text: str) -> str:
    """把每行未转义 % 之后的注释内容替换为等长空格（保留换行与总长不变）。

    用于检测阶段：注释行内的 \\begin{tikzcd} 等假环境不参与匹配。
    长度保持不变，因此屏蔽文本上的 span 可直接用于切取原文。
    """

    out_lines = []
    for line in text.split("\n"):
        cut = len(line)
        i = 0
        while True:
            j = line.find("%", i)
            if j < 0:
                break
            # 统计 % 前紧邻的反斜杠个数：奇数 = 被转义（\%），不是注释
            backslashes = 0
            k = j - 1
            while k >= 0 and line[k] == "\\":
                backslashes += 1
                k -= 1
            if backslashes % 2 == 0:
                cut = j
                break
            i = j + 1
        out_lines.append(line[:cut] + " " * (len(line) - cut))
    return "\n".join(out_lines)


def find_blocks(text: str) -> list[Block]:
    """扫描源文档，返回全部 tikzcd/tikzpicture 块（按出现顺序，跳过注释内假环境）。"""

    masked = _mask_comments(text)
    blocks: list[Block] = []
    for idx, m in enumerate(ENV_PATTERN.finditer(masked), start=1):
        source = text[m.start() : m.end()]  # 逐字取自原文，保留块内注释
        blocks.append(
            Block(
                index=idx,
                env=m.group(1),
                start_line=text.count("\n", 0, m.start()) + 1,
                end_line=text.count("\n", 0, m.end()) + 1,
                chars=len(source),
                source=source,
                char_start=m.start(),
                char_end=m.end(),
            )
        )
    return blocks


def build_standalone(body: str, preamble_lines: list[str] | None = None) -> str:
    """把块源码包装为独立 standalone 文档；preamble_lines 为注入的额外行（项目宏兜底等）。"""

    preamble = "\n".join(preamble_lines or [])
    return STANDALONE_TEMPLATE.replace("@PREAMBLE@", preamble).replace("@BODY@", body)


def extract_preamble_macros(text: str) -> list[str]:
    """从源文档导言区（``\\begin{document}`` 之前）机械提取依赖定义行。

    - 匹配行首（允许空白）为 ``\\newcommand`` / ``\\renewcommand`` /
      ``\\providecommand`` / ``\\DeclareMathOperator`` / ``\\newenvironment`` /
      ``\\renewenvironment`` / ``\\def`` / ``\\usetikzlibrary`` 的行（整行原样取出）；
    - 保持源中的原顺序；按行文本去重（保留首次出现）；
    - 跳过 ``%`` 注释行；
    - 源文档无 ``\\begin{document}`` 时，整篇按导言区处理。

    返回的行默认注入 standalone 模板 ``@PREAMBLE@``（位于 ``--preamble FILE``
    追加行之前），使源文档自定义宏（如 ``\\quotgrp``）与 tikz 库依赖（如
    ``\\usetikzlibrary{cd,calc}``）的 TikZ 图无需人工准备 ``--preamble`` 文件即可
    编译。纯函数，不做任何编译/IO。
    """

    m = re.search(r"\\begin\{document\}", text)
    preamble_text = text[: m.start()] if m else text
    seen: set[str] = set()
    macros: list[str] = []
    for raw in preamble_text.split("\n"):
        # 源文件可能为 CRLF（本工具按字节读入）：去掉行尾 \r 再比对/去重/返回
        line = raw[:-1] if raw.endswith("\r") else raw
        stripped = line.lstrip()
        if not stripped or stripped.startswith("%"):
            continue
        if not PREAMBLE_MACRO_RE.match(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        macros.append(line)
    return macros


def rewrite_source(
    text: str,
    blocks: list[Block],
    png_map: dict[int, str],
    out_dir: str,
) -> str:
    """把已栅格化的 tikz 块原位替换为 ``\\includegraphics`` 引用，返回重写后全文。

    - ``png_map`` 中有 PNG（键为块序号）的块，其原文字符区间替换为两行：
      标记注释 ``% [tikz-rasterize] 原 L<start>-<end> <env> → <png 名>`` 与
      ``\\includegraphics{<relpath>}``；``relpath`` 按 ``out_dir``（OUT.tex 所在
      目录）相对计算并统一为正斜杠；
    - ``png_map`` 缺失（或值为空）的块原样保留（``--only`` 场景）；
    - 替换区间之外的内容由原文字符串切片逐字节返回，无任何全局重写/格式化；
      块若不在行首（同一行上块之前有非空白内容）则补一个换行，避免标记注释被
      同行前文吞掉；块前的行首缩进原样保留在标记注释之前。
    """

    out_base = os.path.abspath(out_dir)
    pieces: list[str] = []
    cursor = 0
    for block in sorted(blocks, key=lambda b: b.char_start):
        png = png_map.get(block.index)
        if not png or block.char_start < cursor:
            continue
        rel = os.path.relpath(os.path.abspath(png), out_base).replace(os.sep, "/")
        line_start = text.rfind("\n", 0, block.char_start) + 1
        at_line_start = not text[line_start : block.char_start].strip()
        lead = "" if at_line_start else "\n"
        pieces.append(text[cursor : block.char_start])
        pieces.append(
            "%s%% [tikz-rasterize] 原 L%d-%d %s → %s\n"
            "\\includegraphics{%s}"
            % (
                lead,
                block.start_line,
                block.end_line,
                block.env,
                os.path.basename(png),
                rel,
            )
        )
        cursor = block.char_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _read_log_tail(workdir: str, log_name: str, lines: int = LOG_TAIL_LINES) -> list[str]:
    log_path = os.path.join(workdir, log_name)
    if not os.path.isfile(log_path):
        return []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()[-lines:]


def compile_block(
    block: Block,
    *,
    xelatex: str,
    pdftoppm: str,
    dpi: int,
    out_dir: str,
    prefix: str,
    preamble_lines: list[str] | None = None,
    timeout: int = COMPILE_TIMEOUT,
) -> dict:
    """编译并栅格化单个块，返回结果字典（status: ok | failed）。"""

    result = {
        "index": block.index,
        "env": block.env,
        "start_line": block.start_line,
        "end_line": block.end_line,
        "chars": block.chars,
        "status": "failed",
        "png": None,
        "error": None,
        "log_tail": [],
    }
    png_name = f"{prefix}-{block.index:02d}.png"
    out_base = os.path.join(out_dir, f"{prefix}-{block.index:02d}")
    workdir = tempfile.mkdtemp(prefix="tikz_rasterize_")
    try:
        tex_path = os.path.join(workdir, "fig.tex")
        # newline=""：块源码可能含 \r\n（源文件原样读入），逐字写出，
        # 避免 Windows 文本模式把 \r\n 再转成 \r\r\n（空行会炸掉 tikzcd 矩阵）
        with open(tex_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(build_standalone(block.source, preamble_lines))

        try:
            proc = subprocess.run(
                [xelatex, *XELATEX_ARGS, "fig.tex"],
                cwd=workdir,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            result["error"] = f"xelatex 超时（>{timeout}s）"
            return result

        pdf_path = os.path.join(workdir, "fig.pdf")
        if proc.returncode != 0 or not os.path.isfile(pdf_path):
            result["error"] = f"xelatex 编译失败（exit={proc.returncode}）"
            result["log_tail"] = _read_log_tail(workdir, "fig.log")
            return result

        out_png = out_base + ".png"
        try:
            proc = subprocess.run(
                [pdftoppm, "-r", str(dpi), "-png", "-singlefile", pdf_path, out_base],
                cwd=workdir,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            result["error"] = f"pdftoppm 超时（>{timeout}s）"
            return result
        if proc.returncode != 0 or not (
            os.path.isfile(out_png) and os.path.getsize(out_png) > 0
        ):
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            result["error"] = f"pdftoppm 栅格化失败（exit={proc.returncode}）{stderr}"
            return result

        result["status"] = "ok"
        result["png"] = out_png
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _load_preamble(paths: list[str] | None) -> list[str]:
    lines: list[str] = []
    for path in paths or []:
        with open(path, encoding="utf-8") as fh:
            lines.extend(fh.read().splitlines())
    return lines


def _block_public(block: Block) -> dict:
    return {
        "index": block.index,
        "env": block.env,
        "start_line": block.start_line,
        "end_line": block.end_line,
        "chars": block.chars,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="源文档侧 TikZ 预栅格化：提取 tikzcd/tikzpicture 并编译为 PNG。"
    )
    ap.add_argument("--source", required=True, help="源 .tex 文件路径")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="PNG 输出目录（默认 <source 所在目录>/assets）",
    )
    ap.add_argument(
        "--prefix", default=None, help="PNG 文件名前缀（默认 source 主名）"
    )
    ap.add_argument("--dpi", type=int, default=300, help="栅格化 DPI（默认 300）")
    ap.add_argument(
        "--preamble",
        action="append",
        default=None,
        metavar="FILE",
        help="注入 standalone 导言区的额外行文件（可重复；位于自动提取的源宏之后，"
        "用于项目特有宏兜底定义）",
    )
    ap.add_argument(
        "--no-auto-preamble",
        action="store_true",
        help="关闭源导言区宏自动提取（默认开启：自动提取 \\newcommand/\\def 等定义行"
        "并注入 standalone 导言区）",
    )
    ap.add_argument(
        "--only",
        default=None,
        metavar="LIST",
        help="只处理指定块（逗号分隔的 1 起序号，如 1,3；缺省处理全部）。"
        "与 --rewrite 组合时只替换已处理的块，其余块原样保留",
    )
    ap.add_argument(
        "--rewrite",
        default=None,
        metavar="OUT.tex",
        help="隐含执行栅格化；全部块成功后写出重写版源文件：每个 tikz 块原位替换为 "
        "\\includegraphics（前置 %% [tikz-rasterize] 标记注释），块外内容逐字节不变。"
        "与 --only 组合时只替换已处理的块，其余块原样保留；"
        "任一块失败则不写出并以退出码 2 结束",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="只列出发现的块（env/行号/字符数），不编译",
    )
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows GBK 控制台/管道下统一以 UTF-8 输出（含中文错误信息与 JSON）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    if args.list and args.rewrite:
        print(
            "错误：--list 与 --rewrite 互斥（--list 只列出块，不编译也不重写）",
            file=sys.stderr,
        )
        return 1

    if not os.path.isfile(args.source):
        print(f"错误：源文件不存在：{args.source}", file=sys.stderr)
        return 1
    # newline=""：不做换行归一化，保证 --rewrite 的块外内容与源文件逐字节一致
    with open(args.source, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()

    blocks = find_blocks(text)

    if args.only:
        try:
            wanted = {int(x) for x in args.only.split(",") if x.strip()}
        except ValueError:
            print(f"错误：--only 序号非法：{args.only}", file=sys.stderr)
            return 1
        unknown = wanted - {b.index for b in blocks}
        if unknown:
            print(f"错误：--only 引用了不存在的块序号：{sorted(unknown)}", file=sys.stderr)
            return 1
        blocks = [b for b in blocks if b.index in wanted]

    if args.list:
        report = {
            "source": os.path.abspath(args.source),
            "mode": "list",
            "found": len(blocks),
            "by_env": {
                env: sum(1 for b in blocks if b.env == env)
                for env in ("tikzcd", "tikzpicture")
            },
            "blocks": [_block_public(b) for b in blocks],
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"源文件：{report['source']}")
            print(
                f"共发现 {report['found']} 个块"
                f"（tikzcd={report['by_env']['tikzcd']}，"
                f"tikzpicture={report['by_env']['tikzpicture']}）："
            )
            for b in report["blocks"]:
                print(
                    f"  #{b['index']:02d} {b['env']:<12} "
                    f"行 {b['start_line']}-{b['end_line']}（{b['chars']} 字符）"
                )
        return 0

    # 编译模式：先确认外部工具存在，缺失时明确报错
    xelatex = shutil.which("xelatex")
    pdftoppm = shutil.which("pdftoppm")
    missing = [name for name, path in (("xelatex", xelatex), ("pdftoppm", pdftoppm)) if not path]
    if missing:
        print(
            f"错误：缺少必需外部工具：{', '.join(missing)}。"
            "请安装 MiKTeX/TeX Live 并确认其在 PATH 中。",
            file=sys.stderr,
        )
        return 1

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.source)), "assets"
    )
    prefix = args.prefix or os.path.splitext(os.path.basename(args.source))[0]
    os.makedirs(out_dir, exist_ok=True)
    auto_preamble = [] if args.no_auto_preamble else extract_preamble_macros(text)
    preamble_lines = auto_preamble + _load_preamble(args.preamble)

    results = [
        compile_block(
            b,
            xelatex=xelatex,
            pdftoppm=pdftoppm,
            dpi=args.dpi,
            out_dir=out_dir,
            prefix=prefix,
            preamble_lines=preamble_lines,
        )
        for b in blocks
    ]
    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = len(results) - succeeded

    report = {
        "source": os.path.abspath(args.source),
        "mode": "rasterize",
        "out_dir": os.path.abspath(out_dir),
        "dpi": args.dpi,
        "found": len(blocks),
        "succeeded": succeeded,
        "failed": failed,
        "auto_preamble_lines": auto_preamble,
        "results": results,
    }

    # --rewrite：失败即闭（不写出）；全部成功才写重写版源文件
    rewrite_info = None
    if args.rewrite and failed == 0:
        out_abs = os.path.abspath(args.rewrite)
        out_parent = os.path.dirname(out_abs)
        if out_parent:
            os.makedirs(out_parent, exist_ok=True)
        png_map = {
            r["index"]: r["png"] for r in results if r["status"] == "ok" and r["png"]
        }
        rewritten = rewrite_source(text, blocks, png_map, out_parent or ".")
        with open(out_abs, "w", encoding="utf-8", newline="") as fh:
            fh.write(rewritten)
        rewrite_info = {
            "out": out_abs,
            "assets_dir": os.path.abspath(out_dir),
            "mapping": [
                {
                    "index": b.index,
                    "env": b.env,
                    "png": png_map[b.index],
                    "start_line": b.start_line,
                    "end_line": b.end_line,
                }
                for b in blocks
                if b.index in png_map
            ],
        }
        report["rewrite"] = rewrite_info

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"源文件：{report['source']}")
        print(f"输出目录：{report['out_dir']}（dpi={args.dpi}）")
        if args.no_auto_preamble:
            print("源导言区宏自动提取：已关闭（--no-auto-preamble）")
        else:
            print(
                f"源导言区宏自动提取：{len(auto_preamble)} 行"
                "（--no-auto-preamble 可关闭）"
            )
        for r in results:
            if r["status"] == "ok":
                print(f"  ✓ #{r['index']:02d} {r['env']:<12} -> {r['png']}")
            else:
                print(f"  ✗ #{r['index']:02d} {r['env']:<12} {r['error']}")
                for line in r["log_tail"]:
                    print(f"      | {line}")
        print(f"完成：成功 {succeeded}，失败 {failed}")
        if rewrite_info:
            print(
                f"重写文件：{rewrite_info['out']}"
                f"（{len(rewrite_info['mapping'])} 处 \\includegraphics，"
                f"资产目录 {rewrite_info['assets_dir']}）"
            )
        elif args.rewrite:
            print("重写未写出：存在失败块（修复后重跑即可）")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
