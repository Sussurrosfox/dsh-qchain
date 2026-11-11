#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_path_safety.py — 路径安全原语与三处越界点的回归（修复 T1 / D1 / D2）

覆盖审计发现的三个 P0：
  T1  assets/%2e%2e/%2e%2e/x  → 先过滤后 unquote，覆盖写到工作目录之外
  D1  dispatch_id='../../x'   → claim 落到 self 的父目录
  D2  self/assets 是 junction → 词法在 self 内、实际写 self 外

断言口径统一为「**既不越界、也不留痕**」：越界写入必须发生在任何副作用之前。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import asset_bundle  # noqa: E402
import path_safety  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)


def _make_junction(link_path, target_path):
    """优先 mklink /J（Windows 目录 junction，无需开发者模式），失败则退回 os.symlink。"""
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", link_path, target_path],
                              capture_output=True, text=True,
                              # mklink 的中文提示按 cp936 输出；显式声明后
                              # 父进程不会因本地代码页差异崩在 reader 线程上
                              encoding="utf-8", errors="replace")
        if proc.returncode == 0 and os.path.exists(link_path):
            return True
    try:
        os.symlink(target_path, link_path, target_is_directory=True)
        return os.path.exists(link_path)
    except (OSError, NotImplementedError):
        return False


class TNormalizeRel(unittest.TestCase):
    """normalize_rel：解一次 percent-encoding，拒绝上跳与绝对路径。"""

    def test_percent_encoded_traversal_rejected(self):
        with self.assertRaises(path_safety.PathEscape) as ctx:
            path_safety.normalize_rel("assets/%2e%2e/%2e%2e/sentinel.txt")
        self.assertEqual(ctx.exception.code, "PATH_TRAVERSAL")

    def test_plain_traversal_rejected(self):
        with self.assertRaises(path_safety.PathEscape) as ctx:
            path_safety.normalize_rel("../outside.txt")
        self.assertEqual(ctx.exception.code, "PATH_TRAVERSAL")

    def test_backslash_traversal_rejected(self):
        with self.assertRaises(path_safety.PathEscape):
            path_safety.normalize_rel("assets\\..\\..\\x.txt")

    def test_absolute_and_drive_rejected(self):
        for bad in ("/etc/passwd", "C:/Windows/win.ini", "c:x", "\\\\server\\share"):
            with self.assertRaises(path_safety.PathEscape):
                path_safety.normalize_rel(bad)

    def test_segment_smuggling_rejected(self):
        # %2f 解码后得到分隔符 ⇒ 段落走私
        with self.assertRaises(path_safety.PathEscape) as ctx:
            path_safety.normalize_rel("assets/a%2fb.png")
        self.assertEqual(ctx.exception.code, "PATH_SEGMENT_SMUGGLED")

    def test_no_double_decode(self):
        # %252e 只解一次 → 字面 "%2e"，不是 ".."；允许但不得逃逸
        self.assertEqual(path_safety.normalize_rel("assets/%252e%252e/x.png"),
                         "assets/%2e%2e/x.png")

    def test_empty_and_nul_rejected(self):
        for bad in ("", "   ", "a\x00b"):
            with self.assertRaises(path_safety.PathEscape):
                path_safety.normalize_rel(bad)

    def test_normal_path_preserved(self):
        self.assertEqual(path_safety.normalize_rel("./assets//pic.png"), "assets/pic.png")


class TSanitizeId(unittest.TestCase):
    """sanitize_id：dispatch_id 用于拼文件名，必须是严格白名单。"""

    def test_traversal_id_rejected(self):
        for bad in ("../../../escaped", "..", ".", "a/b", "a\\b", "", "a b", "a\tb"):
            with self.assertRaises(path_safety.PathEscape):
                path_safety.sanitize_id(bad, label="dispatch_id")

    def test_surrounding_whitespace_trimmed(self):
        self.assertEqual(path_safety.sanitize_id("  dsp-1  "), "dsp-1")

    def test_valid_ids_accepted(self):
        for good in ("dsp-t2-sec1-0d86957a20", "bat.t4c_1", "a1"):
            self.assertEqual(path_safety.sanitize_id(good), good)

    def test_overlong_rejected(self):
        with self.assertRaises(path_safety.PathEscape) as ctx:
            path_safety.sanitize_id("a" * 200)
        self.assertEqual(ctx.exception.code, "ID_TOO_LONG")


class TSafeJoinUnder(unittest.TestCase):
    """safe_join_under / assert_within：realpath 归一，junction 不例外。"""

    def test_inside_ok(self):
        with tempfile.TemporaryDirectory() as td:
            target, norm = path_safety.safe_join_under(td, "assets/pic.png")
            self.assertEqual(norm, "assets/pic.png")
            self.assertTrue(target.lower().startswith(os.path.realpath(td).lower()))

    def test_nonexistent_nested_target_ok(self):
        with tempfile.TemporaryDirectory() as td:
            target, _ = path_safety.safe_join_under(td, "a/b/c/d.txt")
            path_safety.assert_within(td, target)  # 不抛即通过

    def test_escape_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(path_safety.PathEscape):
                path_safety.safe_join_under(td, "../escape.txt")

    def test_junction_escape_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "self")
            outside = os.path.join(td, "outside")
            os.makedirs(root)
            os.makedirs(outside)
            link = os.path.join(root, "assets")
            if not _make_junction(link, outside):
                self.skipTest("当前环境无法创建 junction/symlink")
            # 词法上 assets/leak.txt 在 self 内，realpath 后落在 outside
            with self.assertRaises(path_safety.PathEscape) as ctx:
                path_safety.safe_join_under(root, "assets/leak.txt")
            self.assertEqual(ctx.exception.code, "PATH_ESCAPES_ROOT")
            self.assertFalse(path_safety.is_within(root, os.path.join(link, "leak.txt")))


class TAssetBundleTraversal(unittest.TestCase):
    """T1：编码穿越必须在复制任何文件之前被拒绝，且工作区不得被改动。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = self._td.name
        self.pdir = os.path.join(root, "self")
        self.staging = os.path.join(self.pdir, ".release-staging")
        os.makedirs(os.path.join(self.pdir, "assets"))
        os.makedirs(self.staging)
        # self 之外的哨兵文件：旧实现会被越界引用复制覆盖 self/sentinel.txt
        self.outside = os.path.join(root, "sentinel.txt")
        with open(self.outside, "w", encoding="utf-8") as fh:
            fh.write("OUTSIDE_SOURCE")
        self.inside = os.path.join(self.pdir, "sentinel.txt")
        with open(self.inside, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL_PROJECT_FILE")

    def tearDown(self):
        self._td.cleanup()

    def _write_page(self, src):
        with open(os.path.join(self.staging, "student.html"), "w", encoding="utf-8") as fh:
            fh.write('<html><body><img src="%s"></body></html>' % src)

    def test_encoded_traversal_rejected_without_side_effect(self):
        self._write_page("assets/%2e%2e/%2e%2e/sentinel.txt")
        with self.assertRaises(asset_bundle.BundleError) as ctx:
            asset_bundle.stage_html_resources(self.pdir, self.staging)
        self.assertTrue(any("非法资源路径" in p for p in ctx.exception.problems))
        with open(self.inside, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ORIGINAL_PROJECT_FILE")
        staged = []
        for dirpath, _dirs, files in os.walk(self.staging):
            for name in files:
                staged.append(os.path.relpath(os.path.join(dirpath, name), self.staging))
        self.assertEqual(staged, ["student.html"])

    def test_plain_dotdot_reference_rejected(self):
        self._write_page("assets/../../sentinel.txt")
        with self.assertRaises(asset_bundle.BundleError):
            asset_bundle.stage_html_resources(self.pdir, self.staging)
        with open(self.inside, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ORIGINAL_PROJECT_FILE")

    def test_legit_asset_still_copied(self):
        with open(os.path.join(self.pdir, "assets", "pic.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        self._write_page("assets/pic.png")
        asset_bundle.stage_html_resources(self.pdir, self.staging)
        self.assertTrue(os.path.isfile(os.path.join(self.staging, "assets", "pic.png")))

    def test_junction_asset_source_rejected(self):
        # assets/ 本身是 junction（指向 self 之外）：词法合法，realpath 越界 ⇒ 拒绝复制
        import shutil as _shutil
        _shutil.rmtree(os.path.join(self.pdir, "assets"))
        outside_dir = os.path.join(os.path.dirname(self.pdir), "outside-assets")
        os.makedirs(outside_dir, exist_ok=True)
        with open(os.path.join(outside_dir, "leak.png"), "wb") as fh:
            fh.write(b"LEAK")
        if not _make_junction(os.path.join(self.pdir, "assets"), outside_dir):
            self.skipTest("当前环境无法创建 junction/symlink")
        self._write_page("assets/leak.png")
        with self.assertRaises(asset_bundle.BundleError) as ctx:
            asset_bundle.stage_html_resources(self.pdir, self.staging)
        self.assertTrue(any("越出" in p for p in ctx.exception.problems), ctx.exception.problems)
        self.assertFalse(os.path.exists(os.path.join(self.staging, "assets", "leak.png")))


class TNodeParity(unittest.TestCase):
    """JS 侧原语与 Python 侧同语义（两边必须同步修改）。"""

    def test_node_path_safety_rejects_same_inputs(self):
        node = shutil_which("node")
        if not node:
            self.skipTest("node 不在 PATH")
        script = r"""
import { normalizeRel, sanitizeId } from '%s';
const bad = ['assets/%%2e%%2e/%%2e%%2e/x', '../x', '/etc/passwd', 'C:/x', 'assets/a%%2fb'];
const out = { normalize: [], ids: [] };
for (const b of bad) {
  try { normalizeRel(b); out.normalize.push('ACCEPTED:' + b); }
  catch (e) { out.normalize.push(e.code); }
}
for (const b of ['../../../x', '..', '.', 'a/b', '']) {
  try { sanitizeId(b); out.ids.push('ACCEPTED:' + b); }
  catch (e) { out.ids.push(e.code); }
}
out.ok = normalizeRel('./assets//pic.png');
process.stdout.write(JSON.stringify(out));
""" % ("file:///" + os.path.join(PLUGIN_ROOT, "scripts", "path_safety.mjs")
       .replace("\\", "/").lstrip("/"))
        proc = subprocess.run([node, "--input-type=module", "-e", script],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["normalize"],
                         ["PATH_TRAVERSAL", "PATH_TRAVERSAL", "PATH_ABSOLUTE",
                          "PATH_ABSOLUTE", "PATH_SEGMENT_SMUGGLED"])
        self.assertEqual(data["ids"],
                         ["ID_CHARSET_INVALID", "ID_RESERVED", "ID_RESERVED",
                          "ID_CHARSET_INVALID", "ID_EMPTY"])
        self.assertEqual(data["ok"], "assets/pic.png")


def shutil_which(name):
    from shutil import which
    return which(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
