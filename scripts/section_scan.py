#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""section_scan.py — main.tex 小节扫描的唯一口径（修 N7：零命中不得 fail-open）

问题（审计 N7）：check_semantic.py / check_scope.py / check_provenance.py 各自
复制了一份
    r"^\\s*\\\\input\\{(sec[0-9A-Za-z_-]+)\\.tex\\}"
只认带扩展名的写法。而 `\\input{sec1}` 是完全合法的 LaTeX（TeX 会自己补 .tex，
`\\input{sec1}` 与 `\\input{sec1.tex}` 等价）。于是一份正常工程会扫出 **0 个小节**，
而三个门禁的循环都是「对每个小节做校验」—— 零小节就是零受检项，报告 status=pass，
门禁"全绿"但一个字节都没检查。这就是典型的 fail-open：写作者只要不写 .tex 后缀，
就能让语义审读、范围核对、血缘核对三件套同时失效。

修法（三条一起做才闭合）：
1. **口径唯一**：所有门禁都走本模块，不各自写正则。扩展名可选，顺带收 \\include
   与带前导空白的变体（原正则 `^\\s*` 只吃行首空白，`{}` 里的空格也不允许）。
2. **回落磁盘**：main.tex 扫不出来时，以工作区里实存 sec*.tex 的文件名为准
   （文件名即小节名）——宁可多查，不可少查。
3. **零命中即阻断**：两条路都空时由调用方报 *_NO_SECTIONS（GATE_NO_SLICE）硬失败，
   绝不当"没有小节要检查"放行。
"""

import os
import re

#: main.tex 里引用小节行：`\input{sec1}` / `\input{sec1.tex}` / `\include{ sec1 }`
RE_MAIN_INPUT = re.compile(
    r"^[^\S\n]*\\(?:input|include)\s*\{\s*(sec[0-9A-Za-z_-]+?)\s*(?:\.tex)?\s*\}",
    re.MULTILINE)

#: 磁盘上的小节切片文件名
RE_SEC_TEX = re.compile(r"^sec[0-9A-Za-z_-]+\.tex$")

MAIN_TEX = "main.tex"


def scan_main_text(text):
    """从 main.tex 正文里按出现顺序抽出小节名（去重）。"""
    out = []
    for name in RE_MAIN_INPUT.findall(text or ""):
        if name not in out:
            out.append(name)
    return out


def main_tex_path(workdir):
    return os.path.join(workdir, MAIN_TEX)


def has_main_tex(workdir):
    return os.path.isfile(main_tex_path(workdir))


def scan_main_file(workdir):
    """读 main.tex 抽小节；文件不存在返回 []。"""
    path = main_tex_path(workdir)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return scan_main_text(f.read())


def list_section_files(workdir):
    """工作区里实存的 sec*.tex（返回不带扩展名的小节名，排序）。"""
    if not os.path.isdir(workdir):
        return []
    names = [n for n in os.listdir(workdir) if RE_SEC_TEX.match(n)]
    return sorted(os.path.splitext(n)[0] for n in names)


def resolve_sections(workdir):
    """返回 (sections, source)。

    source:
      'main'  —— 来自 main.tex 的 \\input/\\include（首选）
      'files' —— main.tex 扫不出、回落磁盘 sec*.tex
      ''      —— 两条路都空（调用方必须按 GATE_NO_SLICE 阻断）
    """
    sections = scan_main_file(workdir)
    if sections:
        return sections, "main"
    files = list_section_files(workdir)
    if files:
        return files, "files"
    return [], ""


def no_slice_detail(workdir):
    """零命中时的统一说明（各门禁把它包进自己的错误码）。"""
    if not has_main_tex(workdir):
        return ("工作区没有 main.tex（%s），也没有任何 sec*.tex 切片："
                "无法确定受检小节范围（GATE_NO_SLICE）" % MAIN_TEX)
    return ("main.tex 里没有可识别的 \\input{secN} 引用，工作区也没有 sec*.tex"
            "切片：受检小节为 0 —— 拒绝以「没有小节要检查」放行（GATE_NO_SLICE）")
