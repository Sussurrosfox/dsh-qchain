#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_png_probe.py — 渲染冒烟截图体检（N8）

审计发现：`deliver.smoke_gate_status` 只核对 `png_count >= 1`——只要冒烟
跑出了至少一张图就算过。而「一张全白的 A4 渲染页」恰恰是选项整段丢失、
字体未嵌、页边距错位最常见的外形。把「有没有图」当成「图对不对」，等于
把这道门槛交给真人；而当前主 Agent 侧模型没有图像输入能力，真人永远等不到。

本测试锁三件事：
  1. 真正的空白页被识别（blank=True，bad=True）；
  2. 正常排版页不被误杀（含少量墨迹即可）；
  3. 尺寸过小/非 PNG 的文件明确报错，而不是静默当作通过。
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import deliver  # noqa: E402
import png_probe  # noqa: E402


def _png_bytes(side=300, inked=True, color_type=2):
    """生成一张合成 PNG。color_type=2 为 RGB；=0 为灰度。"""
    nch = 3 if color_type == 2 else 1
    rows = []
    for y in range(side):
        row = bytearray([0])
        for x in range(side):
            dark = inked and (side // 4 <= y < side // 2)
            val = 0 if dark else 255
            row.extend([val] * nch)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", side, side, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class TestPngProbe(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="png-probe-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _write(self, name, data):
        path = os.path.join(self.d, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_blank_page_is_flagged(self):
        path = self._write("blank.png", _png_bytes(inked=False))
        probe = png_probe.probe_png(path)
        self.assertTrue(probe["blank"])
        self.assertTrue(probe["bad"])
        self.assertIn("几乎全白", probe["reason"])
        self.assertLess(probe["ink_ratio"], png_probe.BLANK_INK_RATIO)

    def test_inked_page_passes(self):
        path = self._write("inked.png", _png_bytes(inked=True))
        probe = png_probe.probe_png(path)
        self.assertFalse(probe["blank"])
        self.assertFalse(probe["bad"])
        self.assertGreater(probe["ink_ratio"], 0.01)

    def test_grayscale_blank_is_flagged(self):
        path = self._write("gray.png", _png_bytes(inked=False, color_type=0))
        probe = png_probe.probe_png(path)
        self.assertTrue(probe["blank"])

    def test_tiny_png_is_flagged(self):
        path = self._write("tiny.png", _png_bytes(side=64, inked=True))
        probe = png_probe.probe_png(path)
        self.assertTrue(probe["bad"])
        self.assertIn("尺寸", probe["reason"])

    def test_non_png_never_raises(self):
        path = self._write("not.png", b"this is not a png")
        probe = png_probe.probe_or_error(path)
        self.assertTrue(probe["bad"])
        self.assertIsNone(probe["ink_ratio"])
        self.assertIn("无法解析", probe["reason"])

    def test_missing_file_never_raises(self):
        probe = png_probe.probe_or_error(os.path.join(self.d, "nope.png"))
        self.assertTrue(probe["bad"])


class TestSmokeGateRejectsBlankPage(unittest.TestCase):
    """deliver.smoke_gate_status：真实 PNG 的空白页必须阻断交付。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="smoke-blank-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _manifest(self, blank=True):
        rel = ".pd/review/smoke/student-p1.png"
        path = os.path.join(self.d, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(_png_bytes(inked=not blank))
        rep = {"schema_version": 2, "tool": "render_smoke.py",
               "tool_version": "1.1", "status": "pass", "issues": [],
               "evidence": {"png_count": 1, "items": [{"png": rel}],
                            "manifest": ".pd/review/smoke/smoke-manifest.json",
                            "smoke_dir": ".pd/review/smoke"}}
        for rel_path, data in ((".pd/reports/render_smoke.json", rep),
                               (".pd/review/smoke/smoke-manifest.json",
                                {"schema_version": 2, "png_count": 1,
                                 "items": [{"png": rel}]})):
            abs_path = os.path.join(self.d, *rel_path.split("/"))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            import json
            with open(abs_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    def test_blank_page_fails_gate(self):
        self._manifest(blank=True)
        ok, reason = deliver.smoke_gate_status(self.d, "pdf")
        self.assertFalse(ok)
        self.assertIn("几乎全白", reason)
        self.assertIn("render_smoke.py", reason)

    def test_inked_page_passes_gate(self):
        self._manifest(blank=False)
        ok, reason = deliver.smoke_gate_status(self.d, "pdf")
        self.assertTrue(ok, reason)
        self.assertIn("墨迹占比", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
