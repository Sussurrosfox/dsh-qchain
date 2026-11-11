#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_output_truncation.py — 子 Agent 输出截断的判据回归（N5）

审计发现：子 Agent 输出被 max_tokens 截断时，只要全文没有 <<<END>>> 收尾哨兵，
parseSubagentFiles 的宽松分支就会把截断正文当成完整产物接收——LaTeX 环境都不配平
也照样落盘，于是污染切片或等下游渲染才炸。

断言口径：**不完整的内容一个字节都不许当作产物**（truncated 标记 + 正文剔除），
同时**不能过度收紧**：结构完整的产物（哪怕没写 END）仍要正常接收。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, "dispatch_runtime.mjs")
NODE = shutil.which("node")


def _url(p):
    return "file:///" + p.replace("\\", "/").lstrip("/")


# 在 node 侧跑一批用例，一次性返回结果（避免每个用例起一个进程）
_DRIVER = r"""
import { parseSubagentFiles } from '%s';
const cases = JSON.parse(process.argv[2]);
const out = cases.map(([name, raw, artifacts]) => {
  const r = parseSubagentFiles(raw, artifacts);
  return { name, files: Object.keys(r.files || {}), bodies: r.files || {},
           truncated: !!r.truncated, truncatedFiles: r.truncatedFiles || [], error: r.error || '' };
});
process.stdout.write(JSON.stringify(out));
"""


def _run_cases(cases):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        driver = os.path.join(td, "driver.mjs")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER % _url(RUNTIME))
        proc = subprocess.run([NODE, driver, json.dumps(cases, ensure_ascii=False)],
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return {r["name"]: r for r in json.loads(proc.stdout)}


def _block(name, body, end=True):
    return "<<<FILE:%s>>>\n%s\n%s" % (name, body, ("<<<END:%s>>>" % name) if end else "")


FULL_TEX = "\\section{矩阵}\n设有矩阵 $A$。\n\\begin{proposition}\n若 $A$ 可逆则 $A^{-1}$ 存在。\n\\end{proposition}\n"
TRUNC_TEX = "\\section{矩阵}\n\\begin{proposition}\n若 $A$ 可逆"


@unittest.skipIf(NODE is None, "node 不在 PATH")
class TTruncation(unittest.TestCase):
    def test_single_truncated_block_rejected(self):
        raw = _block("sec2.tex", TRUNC_TEX, end=False)
        r = _run_cases([["single", raw, ["sec2.tex"]]])["single"]
        self.assertEqual(r["files"], [])
        self.assertTrue(r["truncated"])
        self.assertTrue(r["truncatedFiles"], r)

    def test_two_truncated_blocks_both_rejected(self):
        raw = _block("sec1.tex", "A 半句", end=False) + "\n" + _block("sec2.tex", "B 半句", end=False)
        r = _run_cases([["two", raw, ["sec1.tex", "sec2.tex"]]])["two"]
        self.assertEqual(r["files"], [])
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["truncatedFiles"]), 2, r)

    def test_complete_block_without_end_still_accepted(self):
        raw = _block("sec2.tex", FULL_TEX, end=False)
        r = _run_cases([["loose_ok", raw, ["sec2.tex"]]])["loose_ok"]
        self.assertEqual(r["files"], ["sec2.tex"])
        self.assertFalse(r["truncated"])

    def test_end_marker_still_authoritative(self):
        raw = _block("sec2.tex", FULL_TEX, end=True)
        r = _run_cases([["strict", raw, ["sec2.tex"]]])["strict"]
        self.assertEqual(r["files"], ["sec2.tex"])
        self.assertFalse(r["truncated"])

    def test_mixed_complete_and_truncated(self):
        raw = _block("sec1.tex", FULL_TEX, end=False) + "\n" + _block("sec2.tex", TRUNC_TEX, end=False)
        r = _run_cases([["mixed", raw, ["sec1.tex", "sec2.tex"]]])["mixed"]
        self.assertEqual(r["files"], ["sec1.tex"])
        self.assertTrue(r["truncated"])
        self.assertTrue(any("sec2.tex" in t for t in r["truncatedFiles"]), r)

    def test_json_body_not_closed_rejected(self):
        raw = _block("approvals.json", '{"approvals": [{"kind": "bridge"', end=False) + "\n"
        r = _run_cases([["json", raw, ["approvals.json"]]])["json"]
        self.assertEqual(r["files"], [])
        self.assertTrue(r["truncated"])

    def test_host_hint_marks_truncated(self):
        raw = {"output": [{"type": "text", "text": _block("sec2.tex", FULL_TEX, end=True)}],
               "stop_reason": "max_tokens"}
        r = _run_cases([["hint", raw, ["sec2.tex"]]])["hint"]
        self.assertTrue(r["truncated"], r)

    def test_comments_do_not_break_balance(self):
        body = "\\section{x}\n% 这里的 { 未闭合注释不应影响判定\n正文 $a$\n"
        raw = _block("sec1.tex", body, end=False)
        r = _run_cases([["comment", raw, ["sec1.tex"]]])["comment"]
        self.assertEqual(r["files"], ["sec1.tex"], r)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
