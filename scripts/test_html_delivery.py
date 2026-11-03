#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_html_delivery.py — 任务卡 04：发布切换前验收实际离线交付包

覆盖（验收 ID 见 repair-plan/tasks/04.md）：
  DEL-01  完整包在全断网 + 移动到新目录后，公式、图片、目录与选项仍可用；
  DEL-02  删除 staging 内一个字体/JS/图片，或注入坏宏 → 发布失败，旧 release
          逐字节不变，且不残留 .release-staging / .release-prev；
  DEL-03  html / pdf / both 三种格式分别执行正确的门禁；浏览器不可用时报
          BLOCKED_ENV 而不是 PASS；--skip-smoke（PDF 侧）不豁免 HTML 侧；
  DEL-04  staging 验收失败不触碰 release；最终改名失败仍能回滚。

关键点：这里验收的是 **staging 里那份将要交付的包**（写完 manifest 之后），
不是工作区原版 HTML；浏览器一律 file:// 打开包内页面，并阻断全部 http/https
请求，绝不启动替代在线服务器掩盖问题。

运行: python -m pytest -q -p no:cacheprovider scripts/test_html_delivery.py
"""
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tex2cards  # noqa: E402
import render_html  # noqa: E402
import deliver  # noqa: E402
from test_render import make_project  # noqa: E402

try:  # 卡 04 新增模块；缺失时用例以断言失败暴露（而不是收集错误）
    import html_smoke  # noqa: E402
except ImportError:  # pragma: no cover - 修复前路径
    html_smoke = None


SEC_DECK = r"""\section*{I 函数与图像}
\begin{sectiondialogue}[入口：从图像读信息]
\speaker{$\Psi$}{先看这张图。}
\speaker{$\alpha$}{顶点在哪里？}
\end{sectiondialogue}

导语一句。

\begin{probchain}
% qid: q1 | 题型: 进入题 | 难度: ★☆☆
\item \qtype{进入题} 计算 $\quotgrp{1}{2}+\quotring{3}{4}=$\fillin[$5/4$]{15mm}[1/2/3/4]。
\ansspace{1cm}
\begin{solution}
通分得 $\quotgrp{5}{4}$。
\end{solution}
\begin{teacherNote}
先通分再相加。
\end{teacherNote}
\end{probchain}

\begin{microknowledge}[通分]
\begin{enumerate}
\item 分母相同才能直接相加。
\end{enumerate}
\end{microknowledge}

\begin{knowledgebox}[本节结论]
如图 \includegraphics{figA/plot.png} 与 \includegraphics{figB/plot.png}，
公式 $\linf$ 与 $x^2+y^2$。
\end{knowledgebox}
"""


def _png_bytes(rgb):
    """构造一张真实可解码的 1x1 PNG（浏览器要能真的画出像素）。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_A = _png_bytes((220, 40, 40))
PNG_B = _png_bytes((40, 40, 220))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tree_hashes(root):
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = sha(p)
    return out


def browser_available():
    """(ok, 描述/原因)：本机是否具备真实浏览器验收条件。"""
    if html_smoke is None:
        return False, "缺少 scripts/html_smoke.py"
    try:
        sync_pw = html_smoke.import_playwright()
    except html_smoke.SmokeUnavailable as exc:
        return False, exc.reason
    pw = sync_pw().start()
    try:
        browser, desc, diag = html_smoke.find_browser(pw)
        if browser is None:
            return False, html_smoke.browser_diagnostic(diag)
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        return True, desc
    finally:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


_BROWSER = None


def require_browser(case):
    """没有真实浏览器时如实报 BLOCKED_ENV（绝不伪装成 PASS）。"""
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = browser_available()
    if not _BROWSER[0]:
        raise unittest.SkipTest("BLOCKED_ENV: %s" % _BROWSER[1])


class DeliveryFixture(unittest.TestCase):
    """渲染出真实 HTML 卡组，再走 deliver.atomic_release 的真实发布路径。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="qchain-html-deliv-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.d = os.path.join(self.base, "self")
        os.makedirs(self.d)
        make_project(self.d, sec=SEC_DECK)
        write_bytes(os.path.join(self.d, "figA", "plot.png"), PNG_A)
        write_bytes(os.path.join(self.d, "figB", "plot.png"), PNG_B)
        self.render()

    # -- 构造 ---------------------------------------------------------------
    def render(self):
        deck, issues = tex2cards.parse_project(self.d)
        for v in ("student", "teacher"):
            ok, msg = render_html.render_version(
                deck, self.d, v, os.path.join(self.d, v + ".html"))
            self.assertTrue(ok, msg)
        with open(os.path.join(self.d, "cards.json"), "w",
                  encoding="utf-8") as f:
            json.dump(deck, f, ensure_ascii=False)
        return deck

    def make_pdfs(self):
        for n in ("student", "teacher"):
            write_bytes(os.path.join(self.d, n + ".pdf"),
                        b"%PDF-1.4\n% fake pdf for delivery gate\n")

    def release(self, fmt="html"):
        """走真实发布路径（默认对 staging 做真实浏览器验收）。"""
        return deliver.atomic_release(
            self.d, {"schema_version": 2, "tool": "deliver.py"}, fmt=fmt)

    def report_path(self):
        return os.path.join(self.d, deliver.HTML_SMOKE_REPORT_REL)

    def report(self):
        return read_json(self.report_path())

    # -- 旧 release --------------------------------------------------------
    def seed_old_release(self):
        rel = os.path.join(self.d, "release")
        os.makedirs(rel, exist_ok=True)
        write_bytes(os.path.join(rel, "student.html"),
                    b"<html><body>old release</body></html>")
        write_bytes(os.path.join(rel, "manifest.json"), b'{"old": true}')
        return tree_hashes(rel)

    def assert_old_release_intact(self, before):
        rel = os.path.join(self.d, "release")
        self.assertEqual(tree_hashes(rel), before, "旧 release 被改动（哈希不一致）")
        self.assertFalse(os.path.isdir(os.path.join(self.d, ".release-staging")),
                         "失败后残留 .release-staging")
        self.assertFalse(os.path.isdir(os.path.join(self.d, ".release-prev")),
                         "失败后残留 .release-prev")

    def assert_release_restored(self, before):
        """最终改名失败场景：旧 release 已回滚，且没有残留 backup。"""
        rel = os.path.join(self.d, "release")
        self.assertEqual(tree_hashes(rel), before, "改名失败后旧 release 未回滚")
        self.assertFalse(os.path.isdir(os.path.join(self.d, ".release-prev")),
                         "回滚后残留 .release-prev")


class TestDel01OfflinePackage(DeliveryFixture):
    """DEL-01：完整包断网 + 移动目录后公式/图片/目录/选项仍可用。"""

    def test_del01_release_verified_as_staging_package(self):
        require_browser(self)
        rel = self.release("html")

        # 1) 发布门禁确实验收了 staging 里那份包（不是工作区原版）
        report = self.report()
        self.assertIsNotNone(
            report, "发布未产生 .pd/reports/html_smoke.json：HTML 包未经浏览器验收")
        self.assertEqual(report["status"], "pass",
                         "staging 浏览器验收未通过: %s" % (report.get("issues") or [])[:5])
        self.assertEqual(
            os.path.basename(report["evidence"]["package"]),
            ".release-staging",
            "验收对象必须是 staging 实际包，实际为 %s"
            % report["evidence"]["package"])
        self.assertTrue(re.match(r"^[0-9a-f]{64}$",
                                 report["evidence"].get("manifest_sha256") or ""),
                        "证据里缺少验收时的 manifest 摘要")

        # 2) 交付包自足且验收统计为真（公式/图片/目录/选项都动过）
        counts = report["evidence"]["counts"]
        self.assertGreater(counts["cards"], 0, "包内无卡片")
        self.assertGreater(counts["math_nodes"], 0, "包内无公式节点")
        self.assertEqual(counts["math_error"], 0, "存在渲染失败的公式")
        self.assertEqual(counts["math_ok"], counts["math_nodes"],
                         "并非全部公式都渲染成功")
        self.assertGreater(counts["toc_clicked"], 0, "目录点击未验证")
        self.assertGreater(counts["option_groups_clicked"], 0, "选项组点击未验证")
        self.assertGreater(counts["images"], 0, "包内无图片")
        self.assertEqual(counts["images_broken"], 0, "存在未渲染成功的图片")
        for key, ev in report["evidence"]["pages"].items():
            self.assertEqual(ev.get("blocked_requests"), [],
                             "%s 请求了外网资源（离线包必须自足）" % key)
            self.assertEqual(ev.get("failed_requests"), [],
                             "%s 有资源加载失败" % key)

        # 3) 交付目录里确实是完整包，manifest 记录了浏览器验收结论
        for name in ("student.html", "teacher.html", "cards.json",
                     "manifest.json", "katex/katex.min.js", "katex/katex.min.css"):
            self.assertTrue(os.path.isfile(os.path.join(rel, name.replace("/", os.sep))),
                            "交付包缺少 %s" % name)
        manifest = read_json(os.path.join(rel, "manifest.json"))
        self.assertEqual((manifest.get("html_smoke") or {}).get("status"), "pass",
                         "交付 manifest 未记录 HTML 浏览器验收通过")
        self.assertEqual(
            manifest["evidence"]["evidence_hashes"]["reports"]["html_smoke.json"],
            sha(self.report_path()),
            "manifest 里的 html_smoke 报告哈希与实际证据不一致")

    def test_del01_moved_package_still_passes_offline(self):
        require_browser(self)
        rel = self.release("html")
        moved = os.path.join(self.base, "elsewhere", "release")
        os.makedirs(os.path.dirname(moved))
        shutil.copytree(rel, moved)
        shutil.rmtree(self.d)  # 原工作目录彻底消失，只剩交付包

        report, code = html_smoke.run_smoke(
            moved, os.path.join(self.base, "moved-smoke.json"))
        self.assertEqual(code, html_smoke.EXIT_PASS,
                         "移动后的包不可用: %s" % (report.get("issues") or [])[:5])
        self.assertEqual(report["status"], "pass")
        counts = report["evidence"]["counts"]
        self.assertGreater(counts["math_nodes"], 0)
        self.assertEqual(counts["math_ok"], counts["math_nodes"])
        self.assertEqual(counts["math_error"], 0)
        self.assertGreater(counts["toc_clicked"], 0)
        self.assertGreater(counts["option_groups_clicked"], 0)
        self.assertGreater(counts["images"], 0)
        self.assertEqual(counts["images_broken"], 0)
        for key, ev in report["evidence"]["pages"].items():
            self.assertEqual(ev.get("blocked_requests"), [], key)
            self.assertEqual(ev.get("failed_requests"), [], key)


class TestDel02BrokenPackageBlocksRelease(DeliveryFixture):
    """DEL-02：staging 内资源缺失/坏宏 → 发布失败，旧 release 不动。"""

    def _release_with_staging_damage(self, mutate):
        before = self.seed_old_release()
        real = deliver.asset_bundle.build_file_manifest

        def wrapper(root, *args, **kwargs):
            # 在 staging 写完资源、写 manifest 之前动手：模拟"包里少了东西"
            mutate(root)
            return real(root, *args, **kwargs)

        with mock.patch.object(deliver.asset_bundle, "build_file_manifest",
                               wrapper):
            with self.assertRaises(Exception) as ctx:
                self.release("html")
        self.assert_old_release_intact(before)
        report = self.report()
        self.assertIsNotNone(report, "失败时也必须留下 staging 验收证据")
        self.assertEqual(report["status"], "fail", "包损坏却仍判通过")
        return ctx.exception, report

    def test_del02_missing_font_in_staging_blocks_release(self):
        require_browser(self)

        def drop_font(root):
            fonts = os.path.join(root, "katex", "fonts")
            self.assertTrue(os.path.isdir(fonts), "staging 内没有 katex/fonts")
            hits = [n for n in sorted(os.listdir(fonts))
                    if n.startswith("KaTeX_Main-Regular")]
            self.assertTrue(hits, "staging 内找不到 KaTeX_Main-Regular 字体")
            for n in hits:
                os.remove(os.path.join(fonts, n))

        exc, report = self._release_with_staging_damage(drop_font)
        self.assertIn("HTML", str(exc))
        blob = json.dumps(report, ensure_ascii=False)
        self.assertTrue("字体" in blob or "font" in blob.lower(),
                        "验收证据未指出字体问题: %s" % (report.get("issues") or [])[:5])

    def test_del02_missing_katex_js_in_staging_blocks_release(self):
        require_browser(self)

        def drop_js(root):
            js = os.path.join(root, "katex", "katex.min.js")
            self.assertTrue(os.path.isfile(js), "staging 内没有 katex.min.js")
            os.remove(js)

        exc, report = self._release_with_staging_damage(drop_js)
        self.assertIn("HTML", str(exc))
        blob = json.dumps(report, ensure_ascii=False)
        self.assertTrue("引擎" in blob or "katex" in blob.lower(),
                        "验收证据未指出 KaTeX 引擎缺失: %s"
                        % (report.get("issues") or [])[:5])

    def test_del02_missing_image_in_staging_blocks_release(self):
        require_browser(self)

        def drop_image(root):
            assets = os.path.join(root, "assets")
            self.assertTrue(os.path.isdir(assets), "staging 内没有 assets/")
            names = sorted(os.listdir(assets))
            self.assertTrue(names, "staging 内没有图片资源")
            os.remove(os.path.join(assets, names[0]))

        exc, report = self._release_with_staging_damage(drop_image)
        self.assertIn("HTML", str(exc))
        blob = json.dumps(report, ensure_ascii=False)
        self.assertTrue("图片" in blob or "assets/" in blob,
                        "验收证据未指出图片缺失: %s"
                        % (report.get("issues") or [])[:5])

    def test_del02_bad_macro_blocks_release(self):
        """坏宏在 KaTeX 严格模式下必须判失败（不能只看红字就当成功）。"""
        require_browser(self)
        for v in ("student", "teacher"):
            p = os.path.join(self.d, v + ".html")
            with open(p, encoding="utf-8") as f:
                html = f.read()
            new, n = re.subn(
                r'(<span class="qc-math(?: qc-math-display)?">)(.*?)(</span>)',
                r'\1$\\notarealmacro{1}$\3', html, count=1, flags=re.S)
            self.assertEqual(n, 1, "未能在 %s 中定位公式节点" % v)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)

        before = self.seed_old_release()
        with self.assertRaises(Exception) as ctx:
            self.release("html")
        self.assert_old_release_intact(before)
        report = self.report()
        self.assertIsNotNone(report, "失败时也必须留下 staging 验收证据")
        self.assertEqual(report["status"], "fail", "坏宏显示红字仍被判成功")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertTrue("公式" in blob or "math" in blob.lower(),
                        "验收证据未指出公式渲染失败: %s"
                        % (report.get("issues") or [])[:5])
        self.assertIn("HTML", str(ctx.exception))


class TestDel03FormatGates(DeliveryFixture):
    """DEL-03：html/pdf/both 各跑对应门禁；BLOCKED_ENV 不误报 PASS。"""

    def test_del03_html_format_runs_html_smoke(self):
        require_browser(self)
        self.release("html")
        report = self.report()
        self.assertIsNotNone(report, "html 格式未运行 HTML 浏览器验收")
        self.assertEqual(report["status"], "pass")

    def test_del03_both_format_runs_html_smoke(self):
        require_browser(self)
        self.make_pdfs()
        rel = self.release("both")
        report = self.report()
        self.assertIsNotNone(report, "both 格式未运行 HTML 浏览器验收")
        self.assertEqual(report["status"], "pass")
        for name in ("student.pdf", "teacher.pdf", "student.html", "teacher.html"):
            self.assertTrue(os.path.isfile(os.path.join(rel, name)),
                            "both 格式交付包缺少 %s" % name)

    def test_del03_pdf_format_does_not_run_html_smoke(self):
        self.make_pdfs()
        if os.path.isfile(self.report_path()):
            os.remove(self.report_path())
        rel = self.release("pdf")
        self.assertIsNone(self.report(),
                          "pdf 格式不应运行 HTML 浏览器验收")
        self.assertTrue(os.path.isfile(os.path.join(rel, "student.pdf")))
        self.assertFalse(os.path.isfile(os.path.join(rel, "student.html")),
                         "pdf 格式不应把 HTML 塞进交付包")

    def test_del03_blocked_env_is_not_pass(self):
        """浏览器不可用 → BLOCKED_ENV（退出码 3），绝不能当成通过。"""
        if html_smoke is None:
            self.fail("缺少 scripts/html_smoke.py")
        rel = self.release("html")
        report, code = html_smoke.run_smoke(rel, None, candidates=())
        self.assertEqual(code, html_smoke.EXIT_BLOCKED_ENV,
                         "无浏览器时必须返回 BLOCKED_ENV，实际 %s" % code)
        self.assertEqual(report["status"], "blocked_env")
        self.assertEqual(report["evidence"]["dependency"]["status"],
                         "blocked_env")
        ok, reason = deliver.run_html_smoke_check(rel, None,
                                                 runner=lambda *a, **k: (report, code))
        self.assertFalse(ok, "BLOCKED_ENV 被当成通过")
        self.assertIn("BLOCKED_ENV", reason)

    def test_del03_release_aborts_when_browser_missing(self):
        """发布路径上浏览器不可用必须中止（清 staging、旧 release 不动）。"""
        if html_smoke is None:
            self.fail("缺少 scripts/html_smoke.py")
        before = self.seed_old_release()
        with mock.patch.object(html_smoke, "browser_candidates",
                               lambda *a, **k: ()):
            with self.assertRaises(deliver.HtmlSmokeError) as ctx:
                self.release("html")
        self.assertIn("BLOCKED_ENV", str(ctx.exception))
        self.assert_old_release_intact(before)

    def test_del03_skip_smoke_does_not_exempt_html(self):
        """--skip-smoke 只豁免 PDF 侧；HTML 侧需自己的显式开关。"""
        skip, reason = deliver.resolve_html_smoke_skip(False, {})
        self.assertFalse(skip)
        self.assertIn("html", reason.lower())

        skip, reason = deliver.resolve_html_smoke_skip(True, {})
        self.assertIsNone(skip, "缺环境变量确认时不得静默跳过 HTML 验收")
        self.assertIn(deliver.HTML_SMOKE_SKIP_ENV, reason)

        skip, reason = deliver.resolve_html_smoke_skip(
            True, {deliver.HTML_SMOKE_SKIP_ENV: "1"})
        self.assertTrue(skip)
        self.assertIn("--skip-html-smoke", reason)


class TestDel04Rollback(DeliveryFixture):
    """DEL-04：staging 验收失败不触碰 release；改名失败仍回滚。"""

    def test_del04_smoke_failure_never_touches_release(self):
        require_browser(self)

        def drop_css(root):
            css = os.path.join(root, "katex", "katex.min.css")
            self.assertTrue(os.path.isfile(css), "staging 内没有 katex.min.css")
            os.remove(css)

        before = self.seed_old_release()
        real = deliver.asset_bundle.build_file_manifest

        def wrapper(root, *args, **kwargs):
            drop_css(root)
            return real(root, *args, **kwargs)

        with mock.patch.object(deliver.asset_bundle, "build_file_manifest",
                               wrapper):
            with self.assertRaises(Exception):
                self.release("html")
        self.assert_old_release_intact(before)

    def test_del04_rename_failure_restores_backup(self):
        require_browser(self)
        before = self.seed_old_release()
        real_rename = os.rename

        def fake_rename(src, dst):
            if os.path.basename(src) == ".release-staging":
                raise OSError("模拟最终改名失败")
            return real_rename(src, dst)

        with mock.patch.object(deliver.os, "rename", fake_rename):
            with self.assertRaises(OSError):
                self.release("html")
        # 改名失败前必须已经完成 staging 浏览器验收（否则等于先斩后奏）
        report = self.report()
        self.assertIsNotNone(report, "最终改名之前必须先完成 staging 浏览器验收")
        self.assertEqual(report["status"], "pass")
        self.assert_release_restored(before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
