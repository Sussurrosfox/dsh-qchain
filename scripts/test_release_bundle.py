#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_release_bundle.py — 任务卡 01：独立 release 资源完整性回归

覆盖（验收 ID 见 repair-plan/tasks/01.md）：
  REL-01  release 目录复制到另一个 self（原工作目录删除）后，所有
          CSS / JS / font / img 引用仍可解析；
  REL-02  同大小字节变化会触发更新（KaTeX 资源与图片）；两张同名不同内容
          图片得到不同 URL 且各自保留自己的内容；
  REL-03  缺任一必需资源（图片、KaTeX 字体）时发布失败，旧 release 逐字节
          保持原哈希，且不残留 .release-staging；
  REL-04  manifest.json 的 files 清单覆盖 release 内全部实际文件、排除自身、
          size/sha256 与实际一致；越界/绝对/外网引用被拒绝。

本文件的验证器（refs_of / unresolved_refs）刻意独立于 asset_bundle 实现，
避免用被测代码证明被测代码。

运行: python -m pytest -q -p no:cacheprovider scripts/test_release_bundle.py
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tex2cards  # noqa: E402
import render_html  # noqa: E402
import deliver  # noqa: E402
from test_render import make_project  # noqa: E402

try:  # 卡 01 新增模块；缺失时相关用例以断言失败（而非收集失败）暴露
    import asset_bundle  # noqa: E402
except ImportError:  # pragma: no cover - 修复前路径
    asset_bundle = None

SEC_WITH_IMAGES = r"""\section*{I 第一节}
\begin{sectiondialogue}[入口：试引入]
\speaker{$\Psi$}{先观察。}
\speaker{$\alpha$}{有什么规律？}
\speaker{$\Psi$}{请完成下题。}
\end{sectiondialogue}

导语一句。

\begin{probchain}
% qid: q1 | 题型: 进入题 | 难度: ★☆☆
\item \qtype{进入题} 计算 $1+1=$\fillin[$2$]{15mm}[1/2/3/4]。
\ansspace{1cm}
\begin{solution}
$2$。
\end{solution}
\begin{teacherNote}
观察加法。
\end{teacherNote}
\end{probchain}
\begin{microknowledge}[加法定义]
\begin{enumerate}
\item 加法是二元运算。
\end{enumerate}
\end{microknowledge}
\begin{knowledgebox}[本节结论]
结论一 \includegraphics{figA/plot.png} 与 \includegraphics{figB/plot.png}，公式 $x^2+y^2$。
\end{knowledgebox}
"""

# 两张同名同大小、内容不同的“图片”（字节内容足以区分；不需要真实 PNG 解码）
IMG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
IMG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 64
assert len(IMG_A) == len(IMG_B) and IMG_A != IMG_B


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def tree_hashes(root):
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = sha(p)
    return out


class _RefParser(HTMLParser):
    """独立的引用收集器：img/script/source 的 src 与 link 的 href。"""

    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("img", "script", "source", "audio", "video", "iframe"):
            if a.get("src"):
                self.refs.append(a["src"])
        elif tag == "link" and a.get("href"):
            self.refs.append(a["href"])


def refs_of(html_path):
    p = _RefParser()
    with open(html_path, encoding="utf-8") as f:
        p.feed(f.read())
    return p.refs


def css_urls(css_path):
    with open(css_path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    return [u.strip("'\" ") for u in re.findall(r"url\(([^)]+)\)", txt)]


def unresolved_refs(root):
    """root 下所有 html（及其引用的 css）里无法在 root 内解析的引用清单。"""
    bad = []
    n_checked = 0
    for name in sorted(os.listdir(root)):
        if not name.endswith(".html"):
            continue
        hp = os.path.join(root, name)
        for ref in refs_of(hp):
            n_checked += 1
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^//", ref):
                bad.append((name, ref, "external"))
                continue
            target = os.path.normpath(os.path.join(root, ref.split("?")[0]
                                                   .split("#")[0]))
            if not os.path.isfile(target):
                bad.append((name, ref, "missing"))
                continue
            if target.endswith(".css"):
                for u in css_urls(target):
                    if u.startswith("data:"):
                        continue
                    n_checked += 1
                    t2 = os.path.normpath(os.path.join(
                        os.path.dirname(target), u.split("?")[0].split("#")[0]))
                    if not os.path.isfile(t2):
                        bad.append((ref, u, "missing-css-url"))
    return bad, n_checked


class BundleFixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="qchain-bundle-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.d = os.path.join(self.base, "self")
        os.makedirs(self.d)
        make_project(self.d, sec=SEC_WITH_IMAGES)
        write_bytes(os.path.join(self.d, "figA", "plot.png"), IMG_A)
        write_bytes(os.path.join(self.d, "figB", "plot.png"), IMG_B)

    def render(self):
        deck, _issues = tex2cards.parse_project(self.d)
        for v in ("student", "teacher"):
            ok, msg = render_html.render_version(
                deck, self.d, v, os.path.join(self.d, v + ".html"))
            self.assertTrue(ok, msg)
        with open(os.path.join(self.d, "cards.json"), "w",
                  encoding="utf-8") as f:
            json.dump(deck, f, ensure_ascii=False)
        return deck

    def release(self, fmt="html"):
        manifest = {"schema_version": 2, "tool": "deliver.py"}
        # 本文件聚焦"包内容是否自足 / 回滚是否干净"（图片用不可解码的占位字节，
        # 只比较字节内容与 URL 映射）。卡 04 起 atomic_release 默认还要对 staging
        # 做真实浏览器验收，那部分由 scripts/test_html_delivery.py 覆盖，这里显式
        # 关闭以免用占位图片去跑浏览器（断言强度不变）。
        return deliver.atomic_release(self.d, manifest, fmt=fmt, html_smoke=False)

    def read_manifest(self):
        with open(os.path.join(self.d, "release", "manifest.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def img_srcs(self, html_name="student.html"):
        return [r for r in refs_of(os.path.join(self.d, html_name))
                if r.startswith("assets/")]


class TestSelfContainedRelease(BundleFixture):
    def test_rel01_release_resolves_after_move_to_another_self(self):
        self.render()
        rel = self.release()
        moved_self = os.path.join(self.base, "other-self")
        os.makedirs(moved_self)
        moved = os.path.join(moved_self, "release")
        shutil.copytree(rel, moved)
        # 原工作目录整体删除：不得再参与资源查找
        shutil.rmtree(self.d)
        bad, n_checked = unresolved_refs(moved)
        self.assertEqual(bad, [], "release 内存在无法解析的引用: %r" % bad)
        # 至少覆盖 css/js/font/img 四类（防止“空检查”）
        self.assertGreater(n_checked, 10)
        files = tree_hashes(moved)
        self.assertIn("katex/katex.min.js", files)
        self.assertIn("katex/auto-render.min.js", files)
        self.assertIn("katex/katex.min.css", files)
        self.assertTrue(any(f.startswith("katex/fonts/") and
                            f.endswith(".woff2") for f in files))
        self.assertEqual(
            sum(1 for f in files if f.startswith("assets/")), 2)

    def test_rel01_pdf_only_release_needs_no_html_resources(self):
        for n in ("student.pdf", "teacher.pdf"):
            write_bytes(os.path.join(self.d, n), b"%PDF-1.4 fake\n")
        rel = self.release(fmt="pdf")
        files = tree_hashes(rel)
        self.assertIn("student.pdf", files)
        self.assertFalse(any(f.startswith("katex/") for f in files))


class TestContentHashUpdate(BundleFixture):
    def test_rel02_same_name_images_get_distinct_urls_and_contents(self):
        deck = self.render()
        srcs = self.img_srcs()
        self.assertEqual(len(srcs), 2)
        self.assertEqual(len(set(srcs)), 2, "同名图必须有不同 URL: %r" % srcs)
        contents = {open(os.path.join(self.d, s), "rb").read() for s in srcs}
        self.assertEqual(contents, {IMG_A, IMG_B})
        # cards.json 与 HTML 使用同一映射（不是只改复制端）
        blob = json.dumps(deck, ensure_ascii=False)
        for s in srcs:
            self.assertIn(s, blob)

    def test_rel02_same_size_image_change_triggers_update(self):
        self.render()
        before = set(self.img_srcs())
        changed = b"\x89PNG\r\n\x1a\n" + b"C" * 64
        self.assertEqual(len(changed), len(IMG_A))
        write_bytes(os.path.join(self.d, "figA", "plot.png"), changed)
        self.render()
        after = set(self.img_srcs())
        self.assertNotEqual(before, after, "图片内容变了 URL 必须变")
        contents = {open(os.path.join(self.d, s), "rb").read()
                    for s in after}
        self.assertIn(changed, contents)
        self.assertNotIn(IMG_A, contents)

    def test_rel02_same_size_katex_change_is_refreshed(self):
        self.render()
        css = os.path.join(self.d, "katex", "katex.min.css")
        good = sha(css)
        src_css = os.path.join(render_html.KATEX_SRC, "katex.min.css")
        self.assertEqual(good, sha(src_css))
        with open(css, "rb") as f:
            data = bytearray(f.read())
        data[10] = (data[10] + 1) % 256  # 同大小、不同字节
        with open(css, "wb") as f:
            f.write(bytes(data))
        self.assertEqual(os.path.getsize(css), os.path.getsize(src_css))
        self.assertNotEqual(sha(css), good)
        self.render()
        self.assertEqual(sha(css), good, "同大小改动必须按内容哈希刷新")

    def test_rel02_shared_mapping_api(self):
        self.assertIsNotNone(asset_bundle, "缺少 scripts/asset_bundle.py")
        a = asset_bundle.image_asset(self.d, "figA/plot.png")
        b = asset_bundle.image_asset(self.d, "figB/plot.png")
        self.assertNotEqual(a["url"], b["url"])
        for rec, data in ((a, IMG_A), (b, IMG_B)):
            self.assertTrue(re.fullmatch(r"assets/[0-9a-f]{16}-plot\.png",
                                         rec["url"]), rec["url"])
            self.assertEqual(rec["sha256"],
                             hashlib.sha256(data).hexdigest())
            self.assertTrue(os.path.isfile(rec["source"]))
        # 渲染使用的 URL 必须就是映射返回的 URL
        srcs = set(self.render() and self.img_srcs())
        self.assertEqual(srcs, {a["url"], b["url"]})


class TestAtomicRollback(BundleFixture):
    def _seed_old_release(self):
        self.render()
        self.release()
        old = tree_hashes(os.path.join(self.d, "release"))
        self.assertTrue(old)
        return old

    def _assert_old_intact(self, old):
        self.assertEqual(tree_hashes(os.path.join(self.d, "release")), old,
                         "旧 release 必须逐字节保持")
        self.assertFalse(os.path.exists(
            os.path.join(self.d, ".release-staging")))
        self.assertFalse(os.path.exists(os.path.join(self.d, ".release-prev")))

    def test_rel03_missing_image_asset_fails_and_keeps_old_release(self):
        old = self._seed_old_release()
        src = self.img_srcs()[0]
        os.remove(os.path.join(self.d, src))
        with self.assertRaises(Exception):
            self.release()
        self._assert_old_intact(old)

    def test_rel03_missing_katex_font_fails_and_keeps_old_release(self):
        old = self._seed_old_release()
        fonts = os.path.join(self.d, "katex", "fonts")
        os.remove(os.path.join(fonts, "KaTeX_Main-Regular.woff2"))
        with self.assertRaises(Exception):
            self.release()
        self._assert_old_intact(old)

    def test_rel03_unresolvable_source_image_fails(self):
        old = self._seed_old_release()
        os.remove(os.path.join(self.d, "figB", "plot.png"))
        deck, _ = tex2cards.parse_project(self.d)
        render_html.render_version(deck, self.d, "student",
                                   os.path.join(self.d, "student.html"))
        render_html.render_version(deck, self.d, "teacher",
                                   os.path.join(self.d, "teacher.html"))
        with self.assertRaises(Exception):
            self.release()
        self._assert_old_intact(old)

    def test_rel03_tampered_katex_tree_fails(self):
        old = self._seed_old_release()
        css = os.path.join(self.d, "katex", "katex.min.css")
        with open(css, "ab") as f:
            f.write(b"/* tampered */")
        with self.assertRaises(Exception):
            self.release()
        self._assert_old_intact(old)


class TestManifestAndEscape(BundleFixture):
    def test_rel04_manifest_covers_actual_files_excluding_itself(self):
        self.render()
        rel = self.release()
        man = self.read_manifest()
        listed = man.get("files")
        self.assertIsInstance(listed, list, "manifest 缺少 files 清单")
        actual = tree_hashes(rel)
        self.assertIn("manifest.json", actual)
        paths = [e["relative_path"] for e in listed]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn("manifest.json", paths, "manifest 不得哈希自身")
        self.assertEqual(set(paths), set(actual) - {"manifest.json"})
        for e in listed:
            p = os.path.join(rel, *e["relative_path"].split("/"))
            self.assertEqual(e["size"], os.path.getsize(p))
            self.assertEqual(e["sha256"], sha(p), e["relative_path"])
            self.assertFalse(os.path.isabs(e["relative_path"]))
            self.assertNotIn("..", e["relative_path"].split("/"))

    def test_rel04_asset_sources_traceable(self):
        self.render()
        self.release()
        man = self.read_manifest()
        srcs = man.get("asset_sources") or {}
        self.assertEqual(set(srcs), set(self.img_srcs()))
        self.assertEqual({v["source"] for v in srcs.values()},
                         {"figA/plot.png", "figB/plot.png"})

    def _inject(self, snippet):
        for v in ("student", "teacher"):
            p = os.path.join(self.d, v + ".html")
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            txt = txt.replace("</body>", snippet + "</body>", 1)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)

    def _assert_rejected(self, snippet):
        self.render()
        write_bytes(os.path.join(self.base, "secret.png"), IMG_A)
        self._inject(snippet)
        with self.assertRaises(Exception):
            self.release()
        self.assertFalse(os.path.exists(
            os.path.join(self.d, ".release-staging")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "release")))

    def test_rel04_parent_escape_reference_rejected(self):
        self._assert_rejected('<img src="../secret.png" alt="x">')

    def test_rel04_absolute_reference_rejected(self):
        p = os.path.join(self.base, "secret.png").replace(os.sep, "/")
        self._assert_rejected('<img src="/%s" alt="x">' % p.lstrip("/"))

    def test_rel04_external_reference_rejected(self):
        self._assert_rejected(
            '<script src="https://cdn.example.org/x.js"></script>')

    def test_rel04_image_source_escaping_project_is_rejected(self):
        self.assertIsNotNone(asset_bundle, "缺少 scripts/asset_bundle.py")
        write_bytes(os.path.join(self.base, "outside.png"), IMG_A)
        rec = asset_bundle.image_asset(self.d, "../outside.png")
        self.assertIsNone(rec["source"])
        self.assertTrue(rec["reason"])
        rec = asset_bundle.image_asset(
            self.d, os.path.join(self.base, "outside.png"))
        self.assertIsNone(rec["source"])

    def test_rel04_staging_validator_flags_problems(self):
        self.assertIsNotNone(asset_bundle, "缺少 scripts/asset_bundle.py")
        root = os.path.join(self.base, "stg")
        os.makedirs(root)
        with open(os.path.join(root, "a.html"), "w", encoding="utf-8") as f:
            f.write('<img src="assets/nope.png"><img src="../x.png">'
                    '<link rel="stylesheet" href="http://e.org/a.css">')
        problems = asset_bundle.verify_bundle(root)
        joined = "\n".join(problems)
        self.assertIn("nope.png", joined)
        self.assertIn("../x.png", joined)
        self.assertIn("http://e.org/a.css", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
