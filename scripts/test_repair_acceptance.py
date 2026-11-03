#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_repair_acceptance.py — 卡 17：全链验收与真实成本采样（E2E-01..05）。

验收矩阵：[tasks/17.md](<D:/teaching/calculus/slides/CA_6/self/.pd/audits/qchain-review/repair-plan/tasks/17.md>)

E2E-01 三类原料（真实微积分样本 / 单节短样本 / 含图多宏长公式）× legacy|faithful_fast
       × pdf|html|both 各有**实际执行结果**（不是计划、不是一个 all_pass 布尔）。
E2E-02 冷缓存调用预算：实测 K（作者批）与 R（终审批），恒等式 1+K+R；不写死 4。
       三节实测 K=2、R=2 → 5，与 C9 文档口径「三节=4」偏差 1 次（终审单批 ≤2 节），
       偏差按 FAIL/DEVIATION 如实记录，不篡改审读批策略凑数。
E2E-03 故障注入：局部返修、坏宏（严格 KaTeX 定位）、资源缺失（verify_bundle 拒收）、
       取消未确认（持槽不释放）、旧 checkpoint（legacy 图与旧 next/path_map 兼容）、
       缓存全命中（0 派发）、合批部分失败（成功单不被牵连、不跨域覆盖）。
E2E-04 真实 agent 试跑与 fake 路由结果**分开记录**；未执行项只能 pending_external，
       绝不写 PASS（本文件用断言强制这条规则，见 TestE2E04）。
E2E-05 总验收含 C12 三层并发（L1/L2/L3）与未完成项；调用数下降与墙钟改善分开记录。

证明边界（不得混写）：
* 本文件执行的都是真实插件代码（真实图节点、真实批工具入口、真实 Node/KaTeX、
  真实文件系统）；只有「子代理」这一层被可控 stub 顶替；
* stub 的耗时不代表生产模型加速，因此这里只断言调用数/重叠/拒绝语义，不断言墙钟比例；
* 供应商推理并发、真实宿主取消、真实 agent 质量与费用 **不在本层证明**（pending_external）。

所有写入都落在系统临时目录；不启动真实流水线、不派发真实 Agent、不读取 Phase 0 令牌、
不触碰真实学案与生产插件。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
CANDIDATE_ROOT = os.path.dirname(HERE)
if CANDIDATE_ROOT not in sys.path:
    sys.path.insert(0, CANDIDATE_ROOT)
REPAIR_WORK = os.path.dirname(CANDIDATE_ROOT)
EVIDENCE_DIR = os.path.join(REPAIR_WORK, "evidence")

#: 真实学案（只读原料来源；绝不写入）
REAL_SELF = r"D:\teaching\calculus\slides\CA_6\self"
#: 真实学案里可安全复制的「原料 + 版式」文件（不含 .pd 检查点、release、构建产物）
REAL_MATERIAL_FILES = ("sec1.tex", "sec2.tex", "sec3.tex",
                       "config.tex", "config-class.tex", "header.tex",
                       "footer.tex", "main.tex")

import test_parallel_pipeline as tpp  # noqa: E402  (卡22 真实批入口治具)
import test_section_batch as tsb  # noqa: E402
import asset_bundle  # noqa: E402
import review_projection as rp  # noqa: E402
from workflow import execution_policy as ep  # noqa: E402
from workflow import graph as wf_graph  # noqa: E402
from workflow.nodes import fast_review_nodes as frn  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

NODE = shutil.which("node")

FAST = "faithful_fast"
LEGACY = "legacy"

#: 全文件共享的「实测行」账本（供跨用例汇总与证据落盘）；每个用例写自己的键。
ROWS = {}


# ---------------------------------------------------------------------------
# 原料治具：三类隔离 self
# ---------------------------------------------------------------------------

SHORT_SECTION = r"""% sec1 — 单节短样本（约 1 屏）
% qid: sec1-01
\section{极限的直觉}

序列 $a_n=\dfrac{1}{n}$ 当 $n\to\infty$ 时趋于 $0$。

\begin{quote}
若对任意 $\varepsilon>0$ 都存在 $N$，使 $n>N$ 时 $|a_n|<\varepsilon$，
则称 $a_n\to 0$。
\end{quote}
"""

LONG_FORMULA = (
    r"\begin{aligned}"
    r"\lim_{n\to\infty}\left(\frac{1}{n^{2}}+\frac{2}{n^{2}}+\cdots"
    r"+\frac{n}{n^{2}}\right)"
    r"&=\lim_{n\to\infty}\frac{1+2+\cdots+n}{n^{2}}"
    r"=\lim_{n\to\infty}\frac{n(n+1)}{2n^{2}}"
    r"=\lim_{n\to\infty}\left(\frac{1}{2}+\frac{1}{2n}\right)"
    r"=\frac{1}{2}\\"
    r"\int_{0}^{1}x^{2}\,\mathrm{d}x&=\frac{1}{3},\qquad "
    r"\sum_{k=1}^{n}k^{2}=\frac{n(n+1)(2n+1)}{6},"
    r"\qquad \lim_{x\to 0}\frac{\sin x}{x}=1"
    r"\end{aligned}"
)

RICH_SECTION_TEMPLATE = r"""% __SEC__ — 含图 / 多宏 / 长公式样本
% qid: __SEC__-01
% qid: __SEC__-02
\section{__SEC__ 含图与宏示例}

本节的图来自 \texttt{assets/fig/a.png}：

\begin{center}
\includegraphics[width=0.5\linewidth]{assets/fig/a.png}
\end{center}

自定义宏（\texttt{linf} / \texttt{bm} / \texttt{quotgrp}）：

\[
\linf_{\bm{x}\in\quotgrp{G}} \left\lVert \bm{x} \right\rVert_2
\]

超长公式：

\[
__FORMULA__
\]

\begin{enumerate}
\item 第一问：用夹逼定理求上面极限。
\item 第二问：说明宏展开后仍可被 KaTeX 严格渲染。
\end{enumerate}
"""


def rich_section(sec):
    """含图 / 多宏 / 长公式的小节正文（不经过 str.format，避免 LaTeX 花括号）。"""
    return (RICH_SECTION_TEMPLATE.replace("__SEC__", sec)
            .replace("__FORMULA__", LONG_FORMULA))

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


def write_text(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build_material(self_dir, kind):
    """在 self_dir 造一份原料（三类之一）；返回 (sections, 说明)。

    只读复制真实学案正文文件，绝不写回真实学案。
    """
    if not os.path.isdir(self_dir):
        os.makedirs(self_dir, exist_ok=True)
    tsb.write_config(self_dir, agent_dispatch="fast")
    if kind == "real_calculus":
        if not os.path.isdir(REAL_SELF):
            raise unittest.SkipTest("真实学案目录不存在，无法引用高质量微积分样本")
        copied = []
        for name in REAL_MATERIAL_FILES:
            src = os.path.join(REAL_SELF, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(self_dir, name))
                copied.append(name)
        with open(os.path.join(self_dir, "sec1.tex"), encoding="utf-8") as f:
            probe = f.read(4000)
        if "section" not in probe:
            raise unittest.SkipTest("真实学案 sec1.tex 形态不符，拒绝作为原料")
        return ("sec1", "sec2", "sec3"), {"copied": copied}
    if kind == "single_short":
        write_text(os.path.join(self_dir, "sec1.tex"), SHORT_SECTION)
        return ("sec1",), {"chars": len(SHORT_SECTION)}
    if kind == "figures_macros_long":
        for sec in ("sec1", "sec2"):
            write_text(os.path.join(self_dir, sec + ".tex"), rich_section(sec))
        fig = os.path.join(self_dir, "assets", "fig", "a.png")
        os.makedirs(os.path.dirname(fig), exist_ok=True)
        with open(fig, "wb") as f:
            f.write(TINY_PNG)
        write_text(os.path.join(self_dir, "header.tex"),
                   "\\newcommand{\\linf}{\\liminf}\\newcommand{\\bm}[1]"
                   "{\\boldsymbol{#1}}\\newcommand{\\quotgrp}[1]"
                   "{\\overline{#1}}\n")
        return ("sec1", "sec2"), {"chars": len(rich_section("sec1")),
                                  "figure": "assets/fig/a.png"}
    raise AssertionError("未知原料类别: %s" % kind)


MATERIAL_KINDS = ("real_calculus", "single_short", "figures_macros_long")


def plan_authors(self_dir, sections, section_sources=None, **cfg):
    """用真实 node_t2_plan_dispatch 规划作者批（门禁用 PASS 替身）。

    ``section_sources`` 对应真实运行里 state 携带的来源切片文本（字符预算
    与 scope_inputs 指纹都按它算）；缺省不给 = 预算不可触发。
    """
    tsb.write_config(self_dir, agent_dispatch="fast", **cfg)
    extra = {}
    if section_sources is not None:
        extra["section_sources"] = dict(section_sources)
    state = tsb.make_state(self_dir, sections=sections, **extra)
    state["execution_profile"] = FAST
    patch = tsb.run_plan(state, gate=tsb.gate_report("PASS"))
    return state, patch


def author_requests(patch):
    return list((patch.get("dispatch_plan") or {}).get("requests_full") or [])


def request_scopes(req):
    return list(req.get("scopes") or ([req["scope"]] if req.get("scope") else []))


def plan_review(self_dir, sections, **state_extra):
    """用真实 node_t4_plan_combined_review 规划终审批。"""
    state = tsb.make_state(self_dir, sections=sections)
    state["execution_profile"] = FAST
    state["section_statuses"] = {sec: "staged" for sec in sections}
    state["output_format"] = state_extra.pop("output_format", "both")
    state.update(state_extra)
    return frn.node_t4_plan_combined_review(state)


def review_requests(patch):
    return list((patch.get("dispatch_plan") or {}).get("requests_full") or [])


def write_release_fixture(root, katex_src):
    """造一个「已暂存」的离线包骨架：index.html + katex 整树。"""
    os.makedirs(os.path.join(root, "katex"), exist_ok=True)
    for rel, _ in asset_bundle.iter_files(katex_src):
        src = os.path.join(katex_src, *rel.split("/"))
        dst = os.path.join(root, "katex", *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    write_text(os.path.join(root, "index.html"),
               '<!doctype html><html><head>'
               '<link rel="stylesheet" href="katex/katex.min.css">'
               '</head><body><span class="qc-math">$1+1=2$</span>'
               '<script src="katex/katex.min.js"></script></body></html>\n')


KATEX_TEMPLATE_DIR = os.path.join(CANDIDATE_ROOT, "templates", "html", "katex")


# ---------------------------------------------------------------------------
# E2E-01 三类原料 × legacy/fast × pdf|html|both
# ---------------------------------------------------------------------------

class TestE2E01_MaterialsProfilesFormats(unittest.TestCase):
    """E2E-01：三类原料、两条执行图、三种输出格式都有**实际执行结果**。"""

    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qchain-e2e01-")
        self.addCleanup(self._tmp.cleanup)

    def _self(self, name):
        d = os.path.join(self._tmp.name, name, "self")
        os.makedirs(d, exist_ok=True)
        return d

    def test_material_classes_ingest_and_plan(self):
        """三类原料都能进真实作者规划（不发 HALT），并给出实测 K。"""
        results = {}
        for kind in MATERIAL_KINDS:
            d = self._self(kind)
            try:
                sections, meta = build_material(d, kind)
            except unittest.SkipTest as exc:      # pragma: no cover - 环境缺料
                results[kind] = {"status": "BLOCKED_ENV", "reason": str(exc)}
                continue
            _state, patch = plan_authors(d, sections)
            self.assertNotEqual(patch.get("stage"), "HALTED",
                                "%s 规划失败: %s" % (kind, patch.get("error_message")))
            reqs = author_requests(patch)
            covered = [s for r in reqs for s in request_scopes(r)]
            self.assertEqual(sorted(covered), sorted(sections),
                             "%s 作者请求必须覆盖全部节" % kind)
            shapes = set()
            for req in reqs:
                if "scopes" in req:
                    # 合批请求（C5 v2 形状）：逐节产物归属必须齐全
                    shapes.add("batched-v2")
                    self.assertEqual(req["schema_version"], 2)
                    for art in req["artifacts"]:
                        self.assertEqual(req["artifact_scopes"][art],
                                         art[:-len(".tex")])
                else:
                    # 单节串行路径（v1 形状）：仍必须逐节声明产物
                    shapes.add("serial-v1")
                    self.assertEqual(req["artifacts"], [req["scope"] + ".tex"])
            results[kind] = {"status": "PASS", "sections": list(sections),
                             "K": len(reqs), "request_shapes": sorted(shapes),
                             "meta": meta}
        self.assertEqual(len(results), len(MATERIAL_KINDS))
        for kind, row in results.items():
            self.assertIn(row["status"], ("PASS", "BLOCKED_ENV"),
                          "%s: %s" % (kind, row))
        ROWS["E2E-01/A"] = {"check": "E2E-01/A", "results": results,
                            "status": "PASS"}

    def test_profile_matrix_legacy_and_fast(self):
        """legacy 与 faithful_fast 各有真实编译图；节点集差异与图版本一致。

        并且 **3 类原料 × 2 条执行图 = 6 格** 每格都有实际执行结果（真实节点、
        真实请求数），不允许用一个布尔代替整格。
        """
        legacy = wf_graph.create_legacy_graph(checkpointer=MemorySaver())
        fast = wf_graph.create_fast_graph(checkpointer=MemorySaver())

        def nodes(compiled):
            return set(compiled.builder.nodes) - {"__start__", "__end__"}

        legacy_nodes, fast_nodes = nodes(legacy), nodes(fast)
        # 旧 checkpoint 无执行版本 → legacy-v1（content_policy 不换图）
        for values in ({}, {"content_policy": "faithful"}):
            resolved = ep.resolve_resume(values, None)
            self.assertEqual(resolved["graph_version"], ep.GRAPH_VERSION_LEGACY)
            self.assertTrue(resolved["legacy"])
            self.assertEqual(
                ep.graph_version_for_profile(resolved["execution_profile"]),
                ep.GRAPH_VERSION_LEGACY)
        # 冻结为 faithful_fast 的会话 → 快速图
        frozen = ep.build_frozen_metadata(
            profile=FAST,
            execution_config=ep.resolve_execution_config({}))
        resolved_fast = ep.resolve_resume(frozen, frozen)
        self.assertEqual(resolved_fast["graph_version"], ep.GRAPH_VERSION_FAST)
        self.assertFalse(resolved_fast["legacy"])
        self.assertEqual(ep.cache_namespace(FAST), ep.GRAPH_VERSION_FAST)
        self.assertNotEqual(ep.cache_namespace(FAST), ep.cache_namespace(LEGACY),
                            "新旧 profile 的缓存命名空间必须隔离（C7）")
        # 逐节 T2 checker 只在 legacy 图
        for name in ("t2_plan_check", "t2_dispatch_check", "t2_join_check"):
            self.assertIn(name, legacy_nodes)
            self.assertNotIn(name, fast_nodes)
        # legacy 与 fast 都复用 T2 批量三节点；legacy 的逐节把关由 checker 补足
        for name in ("t2_plan_dispatch", "t2_dispatch_sections",
                     "t2_join_sections"):
            self.assertIn(name, fast_nodes)
            self.assertIn(name, legacy_nodes)
        # 快速图的统一 T1 与合并终审节点
        for name in ("fast_t1_request_unified_plan",
                     "t4_plan_combined_review", "t4_join_combined_review"):
            self.assertIn(name, fast_nodes)
        self.assertNotIn("t4_plan_combined_review", legacy_nodes)
        # 未知图版本必须显式停止，不猜回退
        with self.assertRaises(ep.ExecutionPolicyError):
            ep.resolve_resume({"graph_version": "faithful-fast-v9",
                               "execution_schema_version": 2,
                               "execution_profile": FAST,
                               "policy_digest": "0" * 64}, None)

        cells = {}
        for kind in MATERIAL_KINDS:
            for profile in (LEGACY, FAST):
                d = self._self("%s-%s" % (kind, profile))
                try:
                    sections, _meta = build_material(d, kind)
                except unittest.SkipTest as exc:   # pragma: no cover - 缺料
                    cells["%s|%s" % (kind, profile)] = {
                        "status": "BLOCKED_ENV", "reason": str(exc)}
                    continue
                # 作者侧：两条图共用 T2 批量调度节点（fast） / 逐节串行（legacy）
                _state, patch = plan_authors(d, sections)
                self.assertNotEqual(
                    patch.get("stage"), "HALTED",
                    "%s/%s 作者规划失败: %s" % (kind, profile,
                                                patch.get("error_message")))
                k_authors = len(author_requests(patch))
                cell = {"status": "PASS", "kind": kind, "profile": profile,
                        "sections": list(sections), "graph_version":
                        ep.graph_version_for_profile(profile)}
                if profile == FAST:
                    rpatch = plan_review(d, sections)
                    self.assertEqual(rpatch.get("stage"),
                                     "T4_COMBINED_REVIEW_PENDING",
                                     rpatch.get("error_message") or rpatch)
                    r_review = len(review_requests(rpatch))
                    cell.update({
                        "author_requests": k_authors,
                        "review_requests": r_review,
                        "t1_requests": 1,
                        "measured_total_1_plus_K_plus_R":
                            1 + k_authors + r_review,
                    })
                else:
                    # 旧图的逐节语义检查与逐节验收：均由真实节点规划
                    import test_semantic_nodes as tsem
                    import test_t4_review_batch as tt4
                    from workflow.nodes import t4_review_batch_nodes as t4n
                    for sec in sections:
                        tsem.write_sec(d, sec)
                    cstate = tsem.make_check_state(
                        d, sections, {sec: "staged" for sec in sections})
                    cstate["execution_profile"] = LEGACY
                    cpatch, _calls = tsem.run_plan_check(cstate)
                    self.assertEqual(cpatch.get("stage"), "T2_CHECK_PENDING",
                                     cpatch.get("error_message") or cpatch)
                    check_reqs = len(
                        (cpatch.get("dispatch_plan") or {})
                        .get("requests_full") or [])
                    tt4.make_self(d, sections)
                    rstate = tt4.base_state(d, sections)
                    rpatch = t4n.node_t4_plan_review(rstate)
                    self.assertEqual(rpatch.get("stage"),
                                     "T4_REVIEW_PENDING",
                                     rpatch.get("error_message") or rpatch)
                    review_reqs = len(
                        (rpatch.get("dispatch_plan") or {})
                        .get("requests_full") or [])
                    cell.update({
                        "author_requests_serial_per_section": len(sections),
                        "t2_check_requests": check_reqs,
                        "t4_review_requests": review_reqs,
                        "documented_total_3_plus_3N": 3 + 3 * len(sections),
                        "total_measured": "未做整链 legacy 实测（只测逐节检查/"
                                          "验收请求数），不得把 3+3N 冒充实测",
                    })
                cells["%s|%s" % (kind, profile)] = cell
        self.assertEqual(len(cells), len(MATERIAL_KINDS) * 2)
        for key, cell in cells.items():
            self.assertIn(cell["status"], ("PASS", "BLOCKED_ENV"), key)
        ROWS["E2E-01/B"] = {
            "check": "E2E-01/B",
            "question": "3 类原料 × legacy/faithful_fast = 6 格，逐格实际执行",
            "legacy_nodes": len(legacy_nodes), "fast_nodes": len(fast_nodes),
            "cells": cells,
            "executed_cells": len([c for c in cells.values()
                                   if c["status"] == "PASS"]),
            "status": "PASS",
        }

    def test_format_matrix_pdf_html_both(self):
        """pdf|html|both × 3 类原料 = 9 格，逐格实测产物集合与终审派发格式。

        缺产物必须被判「缺失」（不伪 0、不伪造 sha）。
        """
        cells = {}
        for kind in MATERIAL_KINDS:
            for fmt in ("pdf", "html", "both"):
                d = self._self("fmt-%s-%s" % (kind, fmt))
                try:
                    sections, _meta = build_material(d, kind)
                except unittest.SkipTest as exc:   # pragma: no cover - 缺料
                    cells["%s|%s" % (kind, fmt)] = {"status": "BLOCKED_ENV",
                                                    "reason": str(exc)}
                    continue
                # 只有声明产物里的项落盘：缺的那项必须被报 missing
                expected = list(rp.FORMAT_ARTIFACTS[fmt])
                for rel in expected:
                    if rel.endswith(".html"):
                        write_text(os.path.join(d, rel),
                                   "<!doctype html><html><body>ok</body></html>\n")
                    else:
                        with open(os.path.join(d, rel), "wb") as f:
                            f.write(b"%PDF-1.4\n% fixture\n")
                digests = rp.disk_artifact_digests(d, fmt)
                self.assertEqual(sorted(digests), sorted(expected),
                                 "%s/%s 交付产物集合不符" % (kind, fmt))
                for rel, sha in digests.items():
                    self.assertTrue(sha, "%s/%s 的 %s 缺失（不得伪 0）"
                                    % (kind, fmt, rel))
                patch = plan_review(d, sections, output_format=fmt)
                self.assertEqual(patch.get("stage"),
                                 "T4_COMBINED_REVIEW_PENDING",
                                 patch.get("error_message") or patch)
                plan = patch.get("dispatch_plan") or {}
                self.assertEqual(plan.get("output_format"), fmt)
                reqs = review_requests(patch)
                self.assertTrue(reqs, "%s/%s 终审无请求" % (kind, fmt))
                for req in reqs:
                    self.assertEqual((req.get("context") or {})
                                     .get("output_format"), fmt,
                                     "%s/%s 终审请求未携带输出格式"
                                     % (kind, fmt))
                covered = sorted(s for req in reqs
                                 for s in (req.get("review_scopes") or []))
                self.assertEqual(covered, sorted(sections),
                                 "%s/%s 终审必须覆盖全部节" % (kind, fmt))
                cells["%s|%s" % (kind, fmt)] = {
                    "status": "PASS", "kind": kind, "format": fmt,
                    "artifacts": {rel: ("present" if sha else "missing")
                                  for rel, sha in digests.items()},
                    "review_requests": len(reqs),
                }
        # 缺产物纪律：只交 html 时 pdf 必须 missing（不得 0/伪造）
        probe = self._self("fmt-missing")
        sections, _meta = build_material(probe, "single_short")
        write_text(os.path.join(probe, "student.html"), "<html>ok</html>\n")
        html_only = rp.disk_artifact_digests(probe, "html")
        both = rp.disk_artifact_digests(probe, "both")
        self.assertTrue(html_only["student.html"])
        self.assertIsNone(both["student.pdf"], "缺失的 pdf 必须是 None")
        zero_review = {
            "schema_version": 2,
            "review_kind": "combined_source_fidelity",
            "scopes": list(sections),
            "input_digest": "a" * 64,
            "output_format": "both",
            "artifact_digests": {"student.pdf": None, "student.html": None},
            "sections": [],
        }
        problems = rp.validate_combined_review(
            probe, zero_review, expected_scopes=list(sections))
        self.assertTrue(problems, "缺产物/空 sections 必须报问题")
        self.assertEqual(len(cells), len(MATERIAL_KINDS) * 3)
        for key, cell in cells.items():
            self.assertIn(cell["status"], ("PASS", "BLOCKED_ENV"), key)
        ROWS["E2E-01/C"] = {
            "check": "E2E-01/C",
            "question": "3 类原料 × pdf|html|both = 9 格，逐格实际产物与终审格式",
            "cells": cells,
            "executed_cells": len([c for c in cells.values()
                                   if c["status"] == "PASS"]),
            "missing_artifact_discipline": {
                "pdf_missing_reported_as": "None",
                "combined_review_problems": len(problems),
            },
            "status": "PASS",
        }


# ---------------------------------------------------------------------------
# E2E-02 冷缓存调用预算 1+K+R
# ---------------------------------------------------------------------------

class TestE2E02_CallBudget(unittest.TestCase):
    """E2E-02：调用数由实测 K/R 决定，恒等式 1+K+R；绝不写死 4。"""

    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qchain-e2e02-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(tsb.register_capable_worker(4))

    def _self(self, name="self"):
        d = os.path.join(self._tmp.name, name, "self")
        os.makedirs(d, exist_ok=True)
        return d

    def _budget(self, d, sections, **cfg):
        _state, patch = plan_authors(d, sections, **cfg)
        self.assertNotEqual(patch.get("stage"), "HALTED",
                            patch.get("error_message") or "")
        k = len(author_requests(patch))
        review = plan_review(d, sections)
        self.assertEqual(review.get("stage"), "T4_COMBINED_REVIEW_PENDING",
                         review.get("error_message") or review)
        r = len(review_requests(review))
        return k, r

    def test_three_sections_cold_cache_budget(self):
        """三节冷缓存：T1=1 + K + R；实测 K=2、R=2 = 5（与文档口径 4 偏差 1）。"""
        d = self._self("three")
        sections, _meta = build_material(d, "real_calculus")
        self.assertEqual(len(sections), 3)
        k, r = self._budget(d, sections)
        total = 1 + k + r
        self.assertEqual(k, 2, "三节合成两个相邻作者请求（≤2 节/单）")
        self.assertEqual(r, 2, "终审单批 ≤2 节 → 三节必须两批（实测，不改策略）")
        self.assertEqual(total, 1 + k + r, "恒等式必须先成立")
        self.assertEqual(total, 5, "本实现下三节实测总调用数")
        # C9 文档口径是三节 4 次（R=1）；实测 5 次 → 偏差必须显式记录
        documented = 4
        self.assertNotEqual(
            total, documented,
            "若实现已改到 4 次，必须同步更新 ACCEPTANCE/本断言与偏差记录")
        deviation = total - documented
        self.assertEqual(deviation, 1)
        ROWS["E2E-02/A"] = {"check": "E2E-02/A", "sections": 3, "K": k, "R": r,
                     "total": total, "documented": documented,
                     "deviation": deviation,
                     "deviation_reason": (
                         "split_review_batches 把审读单批夹取到 1–2 节"
                         "（C5 口径），三节必须 R=2；C9 的「三节=4」要求 R=1"),
                     "status": "DEVIATION"}

    def test_long_input_grows_k_and_follows_formula(self):
        """长输入抬高 K（按来源字符预算拆批），总数仍严格等于 1+K+R。"""
        d = self._self("long")
        base, _meta = build_material(d, "figures_macros_long")
        write_text(os.path.join(d, "sec3.tex"), rich_section("sec3"))
        sections = ("sec1", "sec2", "sec3")
        self.assertEqual(base, ("sec1", "sec2"))
        # 真实运行里 state 携带来源切片文本；这里给每节约 9000 字符的长来源。
        long_sources = {sec: ("来源正文 %s。" % sec) * 900 for sec in sections}
        # 宽松预算：三节按「≤2 节/单」合批 → K=2
        _state, loose = plan_authors(d, sections, long_sources,
                                     writer_batch_max_source_chars=40000)
        self.assertNotEqual(loose.get("stage"), "HALTED",
                            loose.get("error_message") or "")
        k_loose = len(author_requests(loose))
        self.assertEqual(k_loose, 2, "宽松字符预算下三节应合成两批")
        # 收紧预算到 < 两节来源字符合计 → 必须逐节拆批（K 从 2 变 3）
        _state, tight = plan_authors(d, sections, long_sources,
                                     writer_batch_max_source_chars=10000)
        self.assertNotEqual(tight.get("stage"), "HALTED",
                            tight.get("error_message") or "")
        k_tight = len(author_requests(tight))
        self.assertGreater(k_tight, k_loose,
                           "长输入收紧预算后必须拆成更多作者批（K 变化）")
        self.assertEqual(k_tight, 3, "三节各自超预算 → 三批")
        review = plan_review(d, sections, output_format="both")
        r_tight = len(review_requests(review))
        total = 1 + k_tight + r_tight
        self.assertEqual(total, 1 + k_tight + r_tight)
        self.assertEqual(total, 6, "实测 1+3+2；不得套用固定的 4")
        self.assertNotEqual(total, 4,
                            "K/R 变化时按公式核对，不得固定按 4 次误判")
        ROWS["E2E-02/B"] = {"check": "E2E-02/B", "sections": 3,
                            "K_loose_budget": k_loose,
                            "K_tight_budget": k_tight,
                            "R": r_tight, "total": total, "status": "PASS"}

    def test_single_section_budget(self):
        """单节短样本：K=1、R=1、总数 3（低峰值同样按公式算）。"""
        d = self._self("single")
        sections, _meta = build_material(d, "single_short")
        k, r = self._budget(d, sections)
        self.assertEqual((k, r), (1, 1))
        self.assertEqual(1 + k + r, 3)
        ROWS["E2E-02/C"] = {"check": "E2E-02/C", "sections": 1, "K": k, "R": r,
                     "total": 3, "status": "PASS"}


class TestE2E02_HostCallsThroughRealEntry(tpp._Base):
    """E2E-02/D：真实 qchain_dispatch 入口上的**实测**宿主调用数 = K + R。

    BOUNDARY: 这一层用的是可控 stub 子代理（无模型调用），证明的是批入口的
    派发次数与批不重复派发；不证明真实模型质量/耗时/供应商并发。
    """

    SECTIONS = ("sec1", "sec2", "sec3")

    def test_host_call_count_matches_budget(self):
        tsb.write_config(self.d, agent_dispatch="fast")
        # 相邻两节合批：用统一的三节短正文，确保 K 由节数而不是字符预算决定
        for sec in self.SECTIONS:
            with open(os.path.join(self.d, sec + ".tex"), "w",
                      encoding="utf-8") as f:
                f.write("%%%% %s\n\\section{%s}\n$1+1=2$\n" % (sec, sec))
        _state, patch = plan_authors(self.d, self.SECTIONS)
        requests = author_requests(patch)
        k = len(requests)
        self.assertEqual(k, 2)
        self._mark(requests)
        sbn._reset_pool()
        author_out = self._run_tool(patch["batch_id"])
        self.assertEqual(author_out["result"]["ok"], k,
                         author_out["result"].get("statuses"))
        self.assertEqual(len(author_out["calls"]), k,
                         "作者批的宿主调用数必须等于请求数 K：%s"
                         % author_out["calls"])
        # 重复派发同一批：回执复用，宿主调用数不增长（0 次新调用）
        again = self._run_tool(patch["batch_id"])
        self.assertEqual(again["calls"], [])

        review = plan_review(self.d, self.SECTIONS)
        self.assertEqual(review.get("stage"), "T4_COMBINED_REVIEW_PENDING",
                         review.get("error_message") or review)
        review_reqs = review_requests(review)
        r = len(review_reqs)
        self._mark(review_reqs)
        sbn._reset_pool()
        review_out = self._run_tool(review["batch_id"])
        self.assertEqual(len(review_out["calls"]), r,
                         "终审批的宿主调用数必须等于请求数 R：%s"
                         % review_out["calls"])
        self.assertEqual(1 + k + r, 1 + 2 + 2)
        ROWS["E2E-02/D"] = {"check": "E2E-02/D", "K": k, "R": r,
                     "host_calls_author": len(author_out["calls"]),
                     "host_calls_review": len(review_out["calls"]),
                     "T1_requests": 1,
                     "total": 1 + k + r,
                     "peak_author": author_out["result"]
                     .get("peak_observed_host_in_flight"),
                     "peak_review": review_out["result"]
                     .get("peak_observed_host_in_flight"),
                     "executor_kind": "host_subagent",
                     "status": "PASS"}


# ---------------------------------------------------------------------------
# E2E-03 故障注入
# ---------------------------------------------------------------------------

class TestE2E03_FaultInjection(unittest.TestCase):
    """E2E-03：七类故障注入都必须有实际执行证据。"""

    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qchain-e2e03-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(sbn._reset_pool)

    def _self(self, name="self"):
        d = os.path.join(self._tmp.name, name, "self")
        os.makedirs(d, exist_ok=True)
        tsb.write_config(d, agent_dispatch="fast")
        return d

    # -- 1) 合批部分失败：成功单不被牵连、不跨域覆盖 ------------------------
    def test_partial_batch_failure_isolated(self):
        d = self._self("partial")
        self.addCleanup(tsb.register_capable_worker(4))
        sections = ("sec1", "sec2", "sec3")
        state = tsb.make_state(d, sections=sections)
        patch = tsb.run_plan(state, gate=tsb.gate_report("PASS"))
        state.update(patch)
        requests = author_requests(patch)
        self.assertEqual([request_scopes(r) for r in requests],
                         [["sec1", "sec2"], ["sec3"]])
        # 成功单写下两节真实产物；失败单不写任何东西
        for sec in request_scopes(requests[0]):
            write_text(os.path.join(d, sec + ".tex"), "%% %s ok\n" % sec)
        before = {}
        for sec in request_scopes(requests[0]):
            with open(os.path.join(d, sec + ".tex"), encoding="utf-8") as f:
                before[sec] = f.read()
        results = {
            requests[0]["dispatch_id"]: tsb.DispatchResult(
                dispatch_id=requests[0]["dispatch_id"], scope="sec1",
                status="ok", output={"files": {}}, attempts=1),
            requests[1]["dispatch_id"]: tsb.DispatchResult(
                dispatch_id=requests[1]["dispatch_id"], scope="sec3",
                status="failed", error="stub: boom", attempts=2),
        }
        pool = tsb.FakePool(results=results,
                            stats={"deferred": 0, "ok": 2, "failed": 1,
                                   "max_in_flight": 2})
        sbn._POOL_STATE["pool"] = pool
        sbn._POOL_STATE["batch_id"] = state["batch_id"]
        joined = sbn.node_t2_join_sections(state)
        self.assertEqual(joined["routing_action"], "REPAIR_SECTION")
        self.assertEqual(joined["repair_target"], "sec3")
        self.assertEqual(joined["section_statuses"]["sec1"], "staged")
        self.assertEqual(joined["section_statuses"]["sec2"], "staged")
        self.assertNotEqual(joined["section_statuses"].get("sec3"), "staged")
        # 成功单产物逐字节未被失败单/收尾逻辑改写（不跨域覆盖）
        for sec, text in before.items():
            with open(os.path.join(d, sec + ".tex"), encoding="utf-8") as f:
                self.assertEqual(f.read(), text, "%s 被跨域覆盖" % sec)
        self.assertFalse(os.path.exists(os.path.join(d, "sec3.tex")),
                         "失败 scope 不得留下产物")
        ROWS["E2E-03/1"] = {"status": "PASS", "repair_target": "sec3",
                                 "staged": ["sec1", "sec2"]}

    # -- 2) 坏宏：严格 KaTeX 必须 FAIL 并定位卡片 ---------------------------
    def test_bad_macro_strict_katex_fails(self):
        if not NODE:
            self.skipTest("node 不在 PATH 上（BLOCKED_ENV）")
        d = self._self("badmacro")
        payload = {
            "config": {"macros": {"\\linf": "\\liminf"}},
            "items": [
                {"id": "card-01", "tex": "\\linf_{n} a_n",
                 "display": False, "location": "student.html#card-01"},
                {"id": "card-07", "tex": "\\undefinedmacro{x}",
                 "display": True, "location": "student.html#card-07"},
            ],
        }
        inp = os.path.join(d, "math-input.json")
        with open(inp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        proc = subprocess.run(
            [NODE, os.path.join(HERE, "validate_math.cjs"), "--input", inp],
            capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertNotEqual(proc.returncode, 0,
                            "坏宏在 throwOnError=true 下必须非零退出: %s"
                            % proc.stdout[-800:])
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["checked"], 2)
        self.assertTrue(report["failures"], "必须给出失败明细")
        fail = report["failures"][0]
        self.assertEqual(fail.get("id"), "card-07",
                         "失败必须定位到具体卡片: %s" % fail)
        text = json.dumps(fail, ensure_ascii=False)
        self.assertIn("card-07", text)
        self.assertTrue(fail.get("location") or "card-07" in text)
        ROWS["E2E-03/2"] = {"status": "PASS", "exit_code": proc.returncode,
                                 "failed_id": fail.get("id")}

    # -- 3) 资源缺失：独立离线包必须被 verify_bundle 拒收 -------------------
    def test_missing_resource_rejected_by_verify_bundle(self):
        if not os.path.isdir(KATEX_TEMPLATE_DIR):
            self.skipTest("随包 KaTeX 目录缺失（BLOCKED_ENV）")
        root = os.path.join(self._tmp.name, "staging", "release")
        write_release_fixture(root, KATEX_TEMPLATE_DIR)
        self.assertEqual(asset_bundle.verify_bundle(
            root, expected_trees={"katex": KATEX_TEMPLATE_DIR}), [],
            "完好包必须自足通过")
        # 删掉一个真实字体 → 必须报「缺少打包资源」，不静默通过
        victims = []
        for rel, full in asset_bundle.iter_files(KATEX_TEMPLATE_DIR):
            if rel.lower().endswith((".woff2", ".woff", ".ttf")):
                victims.append(rel)
                break
        self.assertTrue(victims, "随包 KaTeX 应含字体文件")
        gone = os.path.join(root, "katex", *victims[0].split("/"))
        os.remove(gone)
        problems = asset_bundle.verify_bundle(
            root, expected_trees={"katex": KATEX_TEMPLATE_DIR})
        self.assertTrue(problems, "缺字体必须被拒收")
        self.assertIn("katex/" + victims[0], " ".join(problems))
        # 篡改内容（同大小不同内容）同样必须被拒收
        css = os.path.join(root, "katex", "katex.min.css")
        if os.path.isfile(css):
            with open(css, "r+b") as f:
                data = bytearray(f.read(64))
                data[0] = data[0] ^ 0x01
                f.seek(0)
                f.write(bytes(data))
            self.assertTrue(asset_bundle.verify_bundle(
                root, expected_trees={"katex": KATEX_TEMPLATE_DIR}),
                "内容被篡改必须被拒收")
        ROWS["E2E-03/3"] = {"status": "PASS", "missing_font": victims[0],
                                 "problems": len(problems)}

    # -- 4) 取消未确认：持槽不释放，不重复派发 ------------------------------
    def test_unconfirmed_cancellation_holds_slot(self):
        d = self._self("cancel")
        self.addCleanup(tsb.register_capable_worker(4))
        sections = ("sec1", "sec2", "sec3")
        state = tsb.make_state(d, sections=sections)
        patch = tsb.run_plan(state, gate=tsb.gate_report("PASS"))
        state.update(patch)
        requests = author_requests(patch)
        self.assertEqual([request_scopes(r) for r in requests],
                         [["sec1", "sec2"], ["sec3"]])
        batch_id = state["batch_id"]
        # 磁盘批摘要：一个 scope 成功、一个取消未确认（slot 仍被占用）
        run = {
            "schema_version": 2,
            "batch_id": batch_id,
            "request_ids": [r["dispatch_id"] for r in requests],
            "request_count": len(requests),
            "executor_kind": "host_subagent",
            "peak_observed_host_in_flight": 1,
            "observed_scope": "host_call",
            "slots_held_unconfirmed": 1,
            "undispatched": 0,
            "cap_respected": True,
            "serial_reason_code": "host_rate_limited",
            "serial_reason_source": "host:stub",
            "elapsed_ms": 12,
            "vendor_queue_ms": None,
            "inference_ms": None,
        }
        write_text(os.path.join(d, ".pd", "dispatch-runs", batch_id + ".json"),
                   json.dumps(run, ensure_ascii=False, indent=2))
        write_text(os.path.join(d, "sec1.tex"), "%% sec1 ok\n")
        write_text(os.path.join(d, "sec2.tex"), "%% sec2 ok\n")
        tsb.seed_agent_calls(d, requests[:1], worker="subagent:test-agent")
        # 宿主派发（无活池）后的收尾：真实磁盘重推导 + 磁盘批摘要
        sbn._reset_pool()
        join_state = dict(state)
        join_state["manual_interrupted_batch"] = batch_id
        join_state["dispatch_mode"] = "manual"
        joined = sbn.node_t2_join_sections(join_state)
        hist = " ".join(str(h) for h in (joined.get("history") or []))
        self.assertIn("取消未获确认", hist,
                      "取消未确认必须留下明确诊断: %s" % hist)
        metrics = joined.get("dispatch_metrics") or {}
        self.assertEqual(metrics.get("slots_held_unconfirmed"), 1,
                         "持槽数必须如实上报（不得清 0 假装已释放）")
        self.assertEqual(metrics.get("max_in_flight"), 1,
                         "低峰值只记录原因，不改判为返修")
        self.assertEqual(joined["section_statuses"].get("sec1"), "staged")
        self.assertNotEqual(joined["section_statuses"].get("sec3"), "staged",
                            "取消未确认的 scope 不得当作完成")
        self.assertEqual(joined["routing_action"], "REPAIR_SECTION")
        self.assertEqual(joined.get("repair_target"), "sec3")
        self.assertFalse(os.path.exists(os.path.join(d, "sec3.tex")),
                         "取消未确认不得留下产物（晚到结果不落盘）")
        ROWS["E2E-03/4"] = {"status": "PASS",
                            "slots_held_unconfirmed":
                                metrics.get("slots_held_unconfirmed"),
                            "repair_target": joined.get("repair_target")}

    # -- 5) 旧 checkpoint：legacy 图与旧 next/path_map 完全兼容 -------------
    def test_old_checkpoint_stays_legacy(self):
        legacy = wf_graph.create_legacy_graph(checkpointer=MemorySaver())
        nodes = set(legacy.builder.nodes) - {"__start__", "__end__"}
        # 旧会话的 pending batch / next 里出现过的节点名必须仍在图中
        old_next = ["t1_request_coverage_plan", "t2_plan_check",
                    "t2_dispatch_check", "t2_join_check", "t3_checks",
                    "t4_plan_review", "t4_dispatch_review", "t4_join_review",
                    "repair_section", "t4_promote_and_deliver"]
        missing = [n for n in old_next if n not in nodes]
        self.assertEqual(missing, [], "旧 next 节点在 legacy 图中缺失: %s" % missing)
        # path_map 的全部落点必须是真实节点（旧图不被新图替换）
        from workflow import edges as wf_edges
        self.assertTrue(hasattr(wf_edges, "route_after_section_check"))
        resolved = ep.resolve_resume(
            {"next": old_next, "pending_batch": "bat-old-1",
             "content_policy": "faithful"}, None)
        self.assertEqual(resolved["graph_version"], ep.GRAPH_VERSION_LEGACY)
        self.assertTrue(resolved["legacy"])
        # 新会话显式冻结 fast 也不会改写旧 checkpoint 的判定
        frozen_fast = ep.build_frozen_metadata(
            profile=FAST, execution_config=ep.resolve_execution_config({}))
        self.assertEqual(ep.resolve_resume(frozen_fast, frozen_fast)
                         ["graph_version"], ep.GRAPH_VERSION_FAST)
        self.assertEqual(ep.resolve_resume({}, None)["graph_version"],
                         ep.GRAPH_VERSION_LEGACY)
        ROWS["E2E-03/5"] = {"status": "PASS", "legacy_nodes": len(nodes),
                                 "old_next_checked": len(old_next)}

    # -- 6) 缓存全命中：真 0 次模型调用 ------------------------------------
    def test_full_cache_hit_zero_calls(self):
        d = self._self("cache")
        sections = ("sec1", "sec2")
        import test_fast_cache as tfc
        state = tsb.make_state(d, sections=sections)
        for sec in sections:
            write_text(os.path.join(d, sec + ".tex"),
                       "%% %s\n%% qid: %s-entry\n$1+1=2$\n" % (sec, sec))
        assets = sbn._source_asset_counts(d)
        cfg = {"timeout_seconds": 900, "max_retries": 2}
        for sec in sections:
            stored = sbn.store_section_cache(d, state, sec, cfg, assets)
            self.assertTrue(stored.get("stored"),
                            "第一轮真实产物必须写进分节缓存: %s" % stored)
        before = tsb.request_files(d)
        patch = sbn.node_t2_plan_dispatch(state)
        self.assertEqual(tsb.request_files(d), before,
                         "全命中时不得写出任何新派发单（0 次模型调用）")
        self.assertNotIn("dispatch_plan", patch)
        self.assertEqual((patch.get("section_statuses") or {}).get("sec1"),
                         "staged")
        self.assertIsNotNone(tfc.CFG)      # 夹具模块仍可用（未越界改口径）
        ROWS["E2E-03/6"] = {"status": "PASS", "new_requests": 0}

    # -- 7) 局部返修：只返修未通过 scope，次数有界 -------------------------
    def test_local_repair_targets_only_failing_scope(self):
        d = self._self("repair")
        sections, _meta = build_material(d, "single_short")
        write_text(os.path.join(d, "sec1.tex"), SHORT_SECTION)
        write_text(os.path.join(d, "student.html"), "<html>ok</html>\n")
        patch = plan_review(d, ("sec1",), combined_review_scopes=["sec1"])
        self.assertEqual(patch.get("stage"), "T4_COMBINED_REVIEW_PENDING",
                         patch.get("error_message") or patch)
        reqs = review_requests(patch)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(sorted(reqs[0]["review_scopes"]), ["sec1"])
        # 返修预算：per-scope 2、total 6，非法值必须拒绝（不静默钳成 0/负数）
        self.assertEqual(ep.EXECUTION_DEFAULTS["repair_max_per_scope"], 2)
        self.assertEqual(ep.EXECUTION_DEFAULTS["repair_max_total"], 6)
        for bad in ("0", "-1", "abc"):
            with self.assertRaises(ep.ExecutionPolicyError):
                ep.coerce_int(bad, "repair_max_per_scope")
        ROWS["E2E-03/7"] = {"status": "PASS", "review_requests": 1,
                                 "scopes": ["sec1"]}

    def test_real_host_cancellation_is_pending_external(self):
        """真实宿主取消：本机无宿主服务可注入 → PENDING_EXTERNAL，绝不写 PASS。

        已测的只是插件侧契约（E2E-03/4：取消未确认时旧 attempt 仍占槽、
        晚到结果不写盘）；宿主是否真的中止子代理、真取消的时序，本层无法证明。
        """
        stub = ROWS.get("E2E-03/4") or {}
        ROWS["E2E-03/8"] = {
            "check": "E2E-03/8",
            "status": "PENDING_EXTERNAL",
            "executed": False,
            "pending_external_id": "EXT-2",
            "what": "真实宿主取消（宿主侧真正中止子代理、晚到结果不落盘）",
            "plugin_side_contract_check": "E2E-03/4",
            "plugin_side_contract_status": stub.get("status"),
            "why_not_run": "审计环境无真实宿主服务可注入取消信号",
            "evidence_required_when_run": ["宿主 run 执行记录",
                                           "取消确认时间戳",
                                           "晚到结果未写盘的产物快照"],
            "note": "stub 只能证明插件侧契约（旧 attempt 仍占槽、晚到结果不"
                    "写盘），不能证明真实取消已发生",
        }
        self.assertEqual(ROWS["E2E-03/8"]["status"], "PENDING_EXTERNAL")

    def test_zz_rows_collected(self):
        """汇总：每个故障注入都必须留下实际执行结果（缺项即失败）。"""
        expected = ["E2E-03/%d" % i for i in range(1, 8)]
        missing = [k for k in expected if k not in ROWS]
        self.assertEqual(missing, [], "故障注入未执行: %s" % missing)
        for key in expected:
            self.assertEqual(ROWS[key]["status"], "PASS", key)
        # 真实宿主取消只能登记为 PENDING_EXTERNAL（不得混进 PASS 组）
        self.assertEqual(ROWS["E2E-03/8"]["status"], "PENDING_EXTERNAL")
        # 它的插件侧契约证据必须来自真实执行过的 E2E-03/4
        self.assertEqual(ROWS["E2E-03/4"]["status"], "PASS")
        ROWS["E2E-03/8"]["plugin_side_contract_status"] = \
            ROWS["E2E-03/4"]["status"]
        self.assertEqual(ROWS["E2E-03/4"]["slots_held_unconfirmed"], 1,
                         "取消未确认时旧 attempt 必须仍占槽（不得假释放）")


# ---------------------------------------------------------------------------
# 证据装配：一份文档，多个消费者（本文件断言 + 编排脚本落盘）
# ---------------------------------------------------------------------------
#: 本文件承诺必须**实际执行**的检查项全集；缺一项即 NOT_EXECUTED（≈PENDING）。
CHECK_IDS = (
    "E2E-01/A", "E2E-01/B", "E2E-01/C",
    "E2E-02/A", "E2E-02/B", "E2E-02/C", "E2E-02/D",
    "E2E-03/1", "E2E-03/2", "E2E-03/3", "E2E-03/4",
    "E2E-03/5", "E2E-03/6", "E2E-03/7", "E2E-03/8",
    "E2E-04/A", "E2E-04/B", "E2E-04/C",
    "E2E-05/A",
)

#: 真实模型/真实宿主层：**本文件不执行**，只能 pending_external，永不写 PASS。
PENDING_EXTERNAL = (
    {
        "id": "EXT-1",
        "what": "小规模真实 agent 试跑（真实模型质量 + 真实耗时/费用采样）",
        "why_not_run": "需要用户直接授权与真实模型额度；本卡禁止自行执行",
        "proposed_model": "由主代理与真人确认（建议沿用宿主已配置的子代理路由）",
        "call_cap": "≤4 次子代理调用（三节样本：T1 1 + 作者 2 + 终审 1）；"
                    "单次 prompt ≤32000 字符",
        "estimated_cost": "未实测；按 4 次调用 × 约 3–6 万 input tokens 估算区间，"
                          "须由真人确认额度后才能报价",
        "authorization_required": "真人显式授权（含模型、次数上限、费用上限），"
                                  "并走 Phase 0 真人确认流程（Agent 不得自行读令牌）",
        "evidence_required_when_run": "child run ID、逐 attempt 时序、"
                                      "供应商 token/费用、真实产物 digest",
    },
    {
        "id": "EXT-2",
        "what": "真实宿主取消（宿主侧真正中止子代理、晚到结果不落盘）",
        "why_not_run": "审计环境无真实宿主服务可注入取消信号，"
                       "只能用可控 stub 证明插件侧契约",
        "proposed_model": "不适用（非模型调用）",
        "call_cap": "≤2 次真实子代理调用",
        "estimated_cost": "≈0（仅宿主调用，无供应商费用）",
        "authorization_required": "真人授权一次真实宿主运行（含取消演练）",
        "evidence_required_when_run": "宿主 run 执行记录、取消确认时间戳、"
                                      "晚到结果未写盘的产物快照",
    },
)

#: 环境阻塞项：不是「没做」，而是本机无法测量。
BLOCKED_ENV = (
    {
        "id": "ENV-1",
        "what": "L3 供应商推理并发（真实模型排队/首 token/推理并行度）",
        "why_blocked": "本机无法观测供应商侧排队与推理时序；卡22 已同样是 BLOCKED_ENV",
        "consequence": "只声称宿主调用窗口重叠；不得声称真实推理并行或墙钟加速",
    },
)


def delegated_runs():
    """编排脚本实测的「委派套件」运行结果（命令 + 退出码 + 日志路径）。"""
    path = os.environ.get("QCHAIN_ACCEPTANCE_DELEGATED") or ""
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    runs = payload.get("runs") if isinstance(payload, dict) else payload
    return list(runs or [])


#: E2E 断言 ID ↔ 子检查 的聚合关系（矩阵口径；子检查缺一即不可 PASS）
MATRIX_GROUPS = (
    ("E2E-01", ("E2E-01/A", "E2E-01/B", "E2E-01/C")),
    ("E2E-02", ("E2E-02/A", "E2E-02/B", "E2E-02/C", "E2E-02/D")),
    ("E2E-03", tuple("E2E-03/%d" % i for i in range(1, 9))),
    ("E2E-04", ("E2E-04/A", "E2E-04/B", "E2E-04/C")),
    ("E2E-05", ("E2E-05/A",)),
)

#: 这些断言允许的最差结论（偏差要如实保留，不得合并成 PASS）
GROUP_VERDICT = {
    "E2E-01": "PASS",
    "E2E-02": "DEVIATION",          # 三节实测 5 次；文档口径 4 次（终审单批 ≤2 节）
    "E2E-03": "PENDING_EXTERNAL",   # 七类故障实测 PASS；真实宿主取消 = EXT-2
    "E2E-04": "PENDING_EXTERNAL",   # 真实 agent 质量/成本 = EXT-1（未执行）
    "E2E-05": "PASS",
}

#: 子检查状态的严重度排序（越大越「未完成」）；取最差者作为组结论。
STATUS_SEVERITY = (
    "PASS", "DEVIATION", "BLOCKED_ENV", "PENDING",
    "PENDING_EXTERNAL", "NOT_EXECUTED", "FAIL",
)


def build_acceptance_document(rows=None, generated_at=None):
    """把实测行 + 未执行项装配成 acceptance-results 文档（不写盘）。"""
    rows = ROWS if rows is None else rows
    checks = []
    for cid in CHECK_IDS:
        row = rows.get(cid)
        if row is None:
            checks.append({"id": cid, "status": "NOT_EXECUTED",
                           "measured": None,
                           "note": "本项未产生实测行（等同 PENDING，不得计为 PASS）"})
            continue
        status = str(row.get("status") or "PASS")
        item = {"id": cid, "status": status, "measured": row}
        if status != "PASS":
            item["note"] = row.get("deviation_reason") or (
                "子项未完全满足断言：见 measured")
        checks.append(item)
    by_id = {c["id"]: c for c in checks}

    matrix = []
    for group, members in MATRIX_GROUPS:
        members = list(members)
        sub = [by_id.get(m, {"id": m, "status": "NOT_EXECUTED"})
               for m in members]
        statuses = [s["status"] for s in sub]
        worst = max(statuses, key=lambda s: STATUS_SEVERITY.index(s)
                    if s in STATUS_SEVERITY else len(STATUS_SEVERITY))
        matrix.append({
            "id": group,
            "status": worst,
            "allowed_verdict": GROUP_VERDICT[group],
            "sub_checks": [{"id": s["id"], "status": s["status"]} for s in sub],
            "note": {
                "E2E-02": "恒等式 1+K+R 成立；三节实测 1+2+2=5，与 C9 文档口径"
                          "「三节=4」偏差 1 次（终审单批夹取 ≤2 节）",
                "E2E-03": "七类故障注入均有实测 PASS；真实宿主取消见 EXT-2，"
                          "因此本组整体不判 PASS",
                "E2E-04": "真实 agent 质量/成本未执行；与 fake 路由结果分区记录，"
                          "严禁写 PASS（见 pending_external EXT-1 / EXT-2）",
            }.get(group, ""),
        })

    return {
        "task_id": "17",
        "generated_at": generated_at or __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "statuses_legend": ["PASS", "FAIL", "PENDING", "BLOCKED_ENV",
                            "DEVIATION", "PENDING_EXTERNAL", "NOT_EXECUTED"],
        "environment": {
            "PYTHONUTF8": os.environ.get("PYTHONUTF8"),
            "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING"),
            "PYTHONDONTWRITEBYTECODE": os.environ.get("PYTHONDONTWRITEBYTECODE"),
            "python": sys.version.split()[0],
            "node": NODE or None,
            "candidate_root": CANDIDATE_ROOT,
            "real_self_readonly": REAL_SELF,
        },
        "checks": checks,
        "acceptance_matrix": matrix,
        "delegated_runs": delegated_runs(),
        "pending_external": [dict(p) for p in PENDING_EXTERNAL],
        "blocked_env": [dict(b) for b in BLOCKED_ENV],
        "concurrency_layers": {
            "L1_stub": "PASS（插件内调度/批次解析/取消契约，虚拟 fs）",
            "L2_host_call": "PASS（真实 qchain_dispatch 入口 + 阻塞 stub："
                            "宿主调用窗口重叠与峰值）",
            "L3_vendor_inference": "BLOCKED_ENV（供应商排队/推理时序不可观测）",
            "real_host_cancellation": "PENDING_EXTERNAL（EXT-2）",
        },
        # 卡22 并发验收并入总报告：配置 / 宿主调用峰值 / 真实模型证据 三者分列
        "concurrency_observation": {
            "configuration": {
                "tier": "fast（faithful_fast 缺省目标并发 4；"
                        "显式 safe 档位保持串行且带 serial_reason）",
                "priority": "per-call 覆盖 > 嵌套 max_concurrency > "
                            "扁平 agent_max_concurrency > profile（C12.1）",
                "cap_bound": "effective = min(configured, worker 能力)；"
                             "worker 能力未知 → null（不写 0）",
                "conflict_rule": "嵌套与扁平同时为正且不同 → "
                                 "CONFIG_CONCURRENCY_CONFLICT（不静默择一）",
            },
            "host_call_peak": {
                "measured_peak": (ROWS.get("E2E-02/D") or {}).get("peak_author"),
                "cap_respected": True,
                "observed_scope": "host_call",
                "evidence": "E2E-02/D（真实 qchain_dispatch 入口 + 阻塞 stub）"
                            "；卡22 PARE2E / HOSTPAR 套件",
            },
            "real_model_evidence": {
                "status": "PENDING_EXTERNAL",
                "vendor_queue_ms": None,
                "inference_ms": None,
                "note": "宿主调用窗口重叠 ≠ 供应商推理并行；无真实模型证据",
            },
        },
        "no_rework_guarantee": {
            "single_section": "单节只会把峰值压到 1：合法，只记 serial_reason "
                              "与告警，不得改写正文、不得重派 Agent 返修",
            "cache_hit": "缓存全命中的批次本来就没有模型调用（0 次），"
                         "峰值低不是缺陷",
            "rate_limit": "宿主/供应商限流同样只产生告警，"
                          "峰值低一律不得触发正文返修",
            "low_peak_policy": "生产不设「峰值 ≥2」硬门禁；只有"
                               "超 cap / 重复写域 / 收据不符才是正确性阻断",
        },
        "cost_report_partition": {
            "dispatch_counts": "见 performance-sample.json#dispatch_counts"
                               "（本文件 E2E-02/D 为实测宿主调用数）",
            "attempt_counts": "见 performance-sample.json#attempt_counts",
            "wall_clock": "见 performance-sample.json#wall_clock"
                          "（只含本次实测；严禁把旧 8.9226h 日志求和当整体墙钟）",
            "vendor_tokens_cost": "无真实供应商调用 → null",
        },
        "preexisting_failures": [
            {
                "id": "test_source_inventory.py::T02Persistence::test_t07_cli_check",
                "kind": "non-logic / Windows 非 UTF-8 终端下 subprocess gbk 解码",
                "attribution": "与本卡改动无关",
                "measured_this_round": {
                    "with_PYTHONUTF8_1": "8 passed（BASE-utf8）",
                    "without_PYTHONUTF8": "8 passed（BASE-nonutf8：本机控制台已是 "
                                          "UTF-8 代码页，未复现 gbk 失败）",
                    "verdict": "本机两轮均无失败；该失败属环境相关（非 UTF-8 "
                               "终端），保留登记以备他机复核",
                },
                "evidence": "见 delegated_runs 中 BASE-utf8 / BASE-nonutf8 两条"
                            "（命令 + 退出码 + 日志路径）",
            }
        ],
        "side_effects": [
            "只在 candidate 内新增测试文件；未改 workflow/ 与 prompts/ 运行代码",
            "未写生产插件、未写真实学案正文、未启动真实流水线、未读 Phase 0 令牌",
            "所有 fixture 位于临时目录并在用例结束清理",
        ],
        "remaining": [
            "E2E-02 的「三节=4 次」文档口径与实测 5 次偏差 1（终审单批 ≤2 节），"
            "需在设计层决定是改 C9 口径还是改审读批策略",
            "真实 agent 质量/成本（EXT-1）、真实宿主取消（EXT-2）、"
            "L3 供应商推理并发（ENV-1）仍缺外部证据",
        ],
    }


def build_performance_block(timings=None, rows=None):
    """成本采样块（C9 口径）：只有实测值才填，缺失一律 null。"""
    rows = ROWS if rows is None else rows
    budget = rows.get("E2E-02/A") or {}
    host = rows.get("E2E-02/D") or {}
    timings = dict(timings or {})
    return {
        "task_id": "17",
        "scope": "faithful_fast 冷缓存调用预算实测（fake-agent 路由 + 真实批工具入口）；"
                 "不含真实模型质量/费用",
        "dispatch_counts": {
            "formula": "1 + K + R",
            "t1": 1,
            "author_K": budget.get("K"),
            "review_R": budget.get("R"),
            "three_section_measured_total": budget.get("total"),
            "documented_three_section_total": budget.get("documented"),
            "deviation": budget.get("deviation"),
            "host_calls_measured": {
                "author": host.get("host_calls_author"),
                "review": host.get("host_calls_review"),
                "t1": host.get("T1_requests"),
            },
            "legacy_comparison": {
                "value": 12,
                "formula": "3 + 3N（N=3，C9 文档口径）",
                "measured": False,
                "source": "DATA-CONTRACTS C9（本卡未实测 legacy 全链）",
            },
        },
        "attempt_counts": {
            "fixture_attempts": "见各用例 DispatchResult.attempts（stub 账本）",
            "real_attempts": None,
            "detail": None,
        },
        "wall_clock": {
            "delegated_suites_s": timings.get("delegated_suites_s"),
            "acceptance_suite_s": timings.get("acceptance_suite_s"),
            "regression_python_s": timings.get("regression_python_s"),
            "regression_js_s": timings.get("regression_js_s"),
            "whole_run_s": timings.get("whole_run_s"),
            "note": "只记录本次实际测量；历史 8.9226h 日志是旧 run 的求和，"
                    "不是整体墙钟，禁止当性能基线",
        },
        "queue_time": {
            "host_queue_ms": None,
            "vendor_queue_ms": None,
            "note": "宿主/供应商排队时间无真实数据 → null（不得写 0）",
        },
        "model_time": {"inference_ms": None, "first_token_ms": None},
        "char_counts": {
            "prompt_chars": None,
            "output_chars": None,
            "source_material_chars": (rows.get("E2E-01/A") or {}).get("results"),
            "note": "真实 prompt/output 字符数需真实模型调用才有；此处只登记原料字符量",
        },
        "vendor": {
            "tokens_input": None,
            "tokens_output": None,
            "cost": None,
            "currency": None,
            "reason": "本卡未获真实模型授权（EXT-1），无供应商用量返回",
        },
        "call_count_vs_wall_clock": {
            "call_count_reduction": "faithful_fast 三节实测 5 次；legacy 文档口径 12 次",
            "wall_clock_improvement_claimed": False,
            "note": "调用数下降 ≠ 墙钟下降；stub 耗时不能当真实模型加速",
        },
        "boundaries": [
            "所有数字来自本机实测或明确标注的文档口径；缺失一律 null",
            "L3 供应商推理并发 BLOCKED_ENV（ENV-1）",
            "真实 agent 质量与费用 PENDING_EXTERNAL（EXT-1）",
        ],
    }


def _write_json(path, payload):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)
    return path


ACCEPTANCE_PATH = os.path.join(EVIDENCE_DIR, "acceptance-results.json")
PERFORMANCE_PATH = os.path.join(EVIDENCE_DIR, "performance-sample.json")


def teardown_module(module=None):
    """实测行落盘（仅当显式启用）：证据只能来自真正跑过的用例。"""
    if os.environ.get("QCHAIN_ACCEPTANCE_WRITE") != "1":
        return
    _write_json(ACCEPTANCE_PATH, build_acceptance_document())


class TestE2E04_RealAgentBoundary(unittest.TestCase):
    """E2E-04：真实 agent 结果与 fake 路由**分区**记录，未执行项不得写 PASS。"""

    def test_pending_external_never_reported_as_pass(self):
        # 先把「未执行」的两项登记进账本：未执行 = PENDING_EXTERNAL，永不 PASS
        ext = {e["id"]: e for e in PENDING_EXTERNAL}
        ROWS["E2E-04/A"] = {
            "check": "E2E-04/A",
            "status": "PENDING_EXTERNAL",
            "executed": False,
            "pending_external_id": "EXT-1",
            "proposed_model": ext["EXT-1"]["proposed_model"],
            "call_cap": ext["EXT-1"]["call_cap"],
            "estimated_cost": None,
            "authorization_required": True,
            "real_model_verified": False,
            "note": "本卡禁止自行执行真实 agent 试跑；质量/耗时/费用一律未验证",
        }
        ROWS["E2E-04/B"] = {
            "check": "E2E-04/B",
            "status": "PENDING_EXTERNAL",
            "executed": False,
            "pending_external_id": "EXT-2",
            "call_cap": ext["EXT-2"]["call_cap"],
            "estimated_cost": 0,
            "stub_evidence_only": True,
            "note": "无真实宿主可注入取消信号；本轮只有可控 stub 的插件侧契约证据",
        }
        doc = build_acceptance_document()
        statuses = {c["id"]: c["status"] for c in doc["checks"]}
        # 未实测的检查项必须是 NOT_EXECUTED（而不是 PASS）
        for cid in CHECK_IDS:
            if cid not in ROWS:
                self.assertEqual(statuses[cid], "NOT_EXECUTED", cid)
        pe = doc["pending_external"]
        self.assertTrue(pe, "真实 agent/宿主项必须显式登记为 pending_external")
        for item in pe:
            for key in ("id", "what", "why_not_run", "proposed_model",
                        "call_cap", "estimated_cost",
                        "authorization_required"):
                self.assertIn(key, item, "pending_external 缺字段 %s" % key)
            self.assertNotIn("PASS", json.dumps(item, ensure_ascii=False))
        # 矩阵里 E2E-04 绝不能是 PASS（未实测项不得冒充通过）
        e2e04 = [m for m in doc["acceptance_matrix"] if m["id"] == "E2E-04"]
        self.assertEqual(len(e2e04), 1)
        self.assertNotEqual(e2e04[0]["status"], "PASS",
                            "真实 agent/宿主项未执行却写成了 PASS")
        # 真实供应商 token/费用缺失必须是 null（不得写 0）
        perf = build_performance_block()
        self.assertIsNone(perf["vendor"]["tokens_input"])
        self.assertIsNone(perf["vendor"]["cost"])
        self.assertIsNone(perf["model_time"]["inference_ms"])
        self.assertFalse(perf["call_count_vs_wall_clock"]
                         ["wall_clock_improvement_claimed"])

    def test_real_agent_and_fake_routing_are_separated(self):
        doc = build_acceptance_document()
        layers = doc["concurrency_layers"]
        # 三层口径必须分开，且真实层不得被 stub 结果背书
        self.assertIn("L1_stub", layers)
        self.assertIn("L2_host_call", layers)
        self.assertEqual(layers["L3_vendor_inference"], "BLOCKED_ENV（供应商排队/推理"
                         "时序不可观测）")
        for check in doc["checks"]:
            text = json.dumps(check, ensure_ascii=False)
            self.assertNotIn("真实模型", check.get("status", ""))
            self.assertNotIn('"real_model_verified": true', text)
        obs = doc["concurrency_observation"]["real_model_evidence"]
        self.assertEqual(obs["status"], "PENDING_EXTERNAL")
        self.assertIsNone(obs["vendor_queue_ms"])
        self.assertIsNone(obs["inference_ms"])
        ROWS["E2E-04/C"] = {
            "check": "E2E-04/C",
            "status": "PASS",
            "question": "fake 路由结论与真实模型结论必须分区，且缺失值不写 0",
            "pending_external_ids": sorted(p["id"] for p in
                                          doc["pending_external"]),
            "vendor_tokens": None,
            "vendor_cost": None,
            "wall_clock_improvement_claimed": False,
            "real_model_evidence_status": obs["status"],
            "blocked_env_ids": sorted(b["id"] for b in doc["blocked_env"]),
        }
        # /C 登记后矩阵口径定型：E2E-04 必须是 pending_external 组
        final = build_acceptance_document()
        e2e04 = [m for m in final["acceptance_matrix"] if m["id"] == "E2E-04"]
        self.assertEqual(len(e2e04), 1)
        self.assertEqual(e2e04[0]["status"], "PENDING_EXTERNAL",
                         "真实 agent 试跑未执行，E2E-04 只能是 pending_external")


class TestE2E05_OverallReport(unittest.TestCase):
    """E2E-05：总验收含 C12 三层并发与未完成项；调用数与墙钟分开记录。"""

    def test_report_includes_layers_and_unfinished(self):
        doc = build_acceptance_document()
        perf = build_performance_block()
        # 三层 + 真实宿主取消
        self.assertEqual(set(doc["concurrency_layers"]) >=
                         {"L1_stub", "L2_host_call", "L3_vendor_inference",
                          "real_host_cancellation"}, True)
        # 未完成项必须写进 remaining，不得静默
        self.assertTrue(doc["remaining"])
        self.assertIn("EXT-1", json.dumps(doc["pending_external"],
                                          ensure_ascii=False))
        # 成本分区：dispatch/attempt/墙钟/供应商 各自独立
        for key in ("dispatch_counts", "attempt_counts", "wall_clock", "vendor"):
            self.assertIn(key, perf)
        self.assertEqual(perf["dispatch_counts"]["formula"], "1 + K + R")
        self.assertFalse(perf["dispatch_counts"]["legacy_comparison"]["measured"],
                         "legacy 12 次是文档口径，不得冒充实测")
        # 墙钟：没有真实模型数据时必须 null，且禁止把旧 8.9h 日志当基线
        self.assertIn("8.9226", perf["wall_clock"]["note"])
        self.assertIsNone(perf["model_time"]["inference_ms"])
        self.assertIsNone(perf["queue_time"]["vendor_queue_ms"])
        # 单节/缓存/限流不造成正文返修（卡22 结论必须进总报告）
        guard = doc["no_rework_guarantee"]
        self.assertIn("不得改写正文", guard["single_section"])
        self.assertIn("峰值低不是缺陷", guard["cache_hit"])
        self.assertIn("不得触发正文返修", guard["rate_limit"])
        self.assertIn("硬门禁", guard["low_peak_policy"])
        # 卡22 并发观测三分列：配置 / 宿主调用峰值 / 真实模型证据
        obs = doc["concurrency_observation"]
        for key in ("configuration", "host_call_peak", "real_model_evidence"):
            self.assertIn(key, obs)
        self.assertEqual(obs["real_model_evidence"]["status"],
                         "PENDING_EXTERNAL")
        self.assertTrue(obs["host_call_peak"]["cap_respected"])
        ROWS["E2E-05/A"] = {
            "check": "E2E-05/A",
            "status": "PASS",
            "question": "总验收含 C12 三层并发证明 + 未完成项 + 调用数/墙钟分开",
            "layers": sorted(doc["concurrency_layers"]),
            "concurrency_observation": obs,
            "no_rework_guarantee": guard,
            "remaining": doc["remaining"],
            "dispatch_formula": perf["dispatch_counts"]["formula"],
            "legacy_comparison_measured":
                perf["dispatch_counts"]["legacy_comparison"]["measured"],
        }

    def test_existing_delegated_runs_recorded_when_present(self):
        """已跑过的委派套件（命令/退出码/日志）必须进证据文档。"""
        runs = delegated_runs()
        doc = build_acceptance_document()
        self.assertEqual(len(doc["delegated_runs"]), len(runs))
        for run in runs:
            self.assertIn("command", run)
            self.assertIn("exit_code", run)
            self.assertIn("evidence", run)
            self.assertIsInstance(run["exit_code"], int,
                                  "退出码必须是整数实测值（含已知基线失败 1）")


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
