#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_deliver_contract.py — 交付链路格式契约回归（T2/T3/T4）

覆盖：
  FMT-01  格式敏感的每个子检查都必须显式收到 --format（CLI 与 config.yaml
          不一致时不能靠回落 config 悄悄按另一种格式校验）；
  FMT-02  manifest.deliverables 只声明本次 fmt 真会打包的产物，且装配阶段
          若声明了包内不存在的文件必须当场拒绝（不许发出"说谎的清单"）；
  FMT-03  干跑（--dry-run）必须跑 HTML 侧离线验收，且不留 staging；
  FMT-04  报告里记录的 output_format 与本次 fmt 不一致 → 交付阻断。

运行: python -B -m pytest -q -p no:cacheprovider scripts/test_deliver_contract.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build  # noqa: E402
import deliver  # noqa: E402

# 这些脚本的判定结果依赖输出格式；deliver 必须把 {fmt} 显式传下去
FORMAT_SENSITIVE = {"build.py", "check_render.py", "compare_versions.py"}


def _write(path, text="fixture"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TChecksPassFormat(unittest.TestCase):
    """FMT-01：格式敏感检查必须显式收到 --format。"""

    def test_every_format_sensitive_check_gets_fmt(self):
        missing = []
        for entry in deliver.CHECKS:
            name = entry[0]
            if name not in FORMAT_SENSITIVE:
                continue
            args = entry[2] if len(entry) > 2 else []
            if "--format" not in args or "{fmt}" not in args:
                missing.append((name, args))
        self.assertEqual(missing, [], "格式敏感检查未显式接收 --format: %r" % (missing,))

    def test_report_format_mismatch_blocks(self):
        """FMT-04：报告记录的 output_format 与本次 fmt 不一致 → 阻断。"""
        source = open(deliver.__file__, encoding="utf-8").read()
        self.assertIn("记录的 output_format=", source)
        self.assertIn("与本次交付格式", source)


class TManifestSelfConsistent(unittest.TestCase):
    """FMT-02：清单只声明真存在的产物。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="qchain-fmt-")
        self.d = self._td.name
        for name in ("student.pdf", "teacher.pdf", "student.html", "teacher.html"):
            _write(os.path.join(self.d, name))

    def tearDown(self):
        self._td.cleanup()

    def test_html_package_declares_only_html(self):
        declared = deliver.build_deliverables(self.d, "html")
        self.assertEqual(sorted(d["file"] for d in declared),
                         ["student.html", "teacher.html"])
        rel = deliver.atomic_release(self.d, {"deliverables": declared},
                                     fmt="html", html_smoke=False)
        saved = json.load(open(os.path.join(rel, "manifest.json"), encoding="utf-8"))
        for entry in saved["deliverables"]:
            self.assertTrue(os.path.exists(os.path.join(rel, entry["file"])),
                            "清单声明了包内不存在的 %s" % entry["file"])

    def test_pdf_package_declares_only_pdf(self):
        declared = deliver.build_deliverables(self.d, "pdf")
        self.assertEqual(sorted(d["file"] for d in declared),
                         ["student.pdf", "teacher.pdf"])

    def test_both_package_declares_everything(self):
        declared = deliver.build_deliverables(self.d, "both")
        self.assertEqual(sorted(d["file"] for d in declared),
                         ["student.html", "student.pdf", "teacher.html", "teacher.pdf"])

    def test_declaration_of_absent_file_is_rejected(self):
        """负向对照：手工把 PDF 塞进 html 包的声明 → 装配阶段拒绝。"""
        with self.assertRaises(deliver.ReleaseManifestError) as ctx:
            deliver.atomic_release(self.d, {"deliverables": [{"file": "student.pdf"}]},
                                   fmt="html", html_smoke=False)
        self.assertIn("不存在的交付物", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, ".release-staging")),
                         "拒绝后不得残留 staging")


class TDryRunValidatesHtml(unittest.TestCase):
    """FMT-03：干跑也走 HTML 离线验收，且清干净 staging。"""

    def test_dry_run_runs_html_smoke_and_cleans_staging(self):
        with tempfile.TemporaryDirectory(prefix="qchain-dry-") as td:
            _write(os.path.join(td, "config.yaml"), "output_format: html\n")
            for name in ("student.pdf", "teacher.pdf", "student.html", "teacher.html"):
                _write(os.path.join(td, name))
            _write(os.path.join(td, ".pd", "run.json"),
                   json.dumps({"slices": ["sec1"],
                               "tasks": {"sec1": {"status": "checked"}}}))

            calls = []

            def fake_run(script, pdir, report_path, extra_args=None):
                Path = __import__("pathlib").Path
                Path(report_path).write_text(json.dumps({
                    "schema_version": 2, "tool": script, "tool_version": "t",
                    "input_digest": "fixture", "status": "pass", "issues": []}),
                    encoding="utf-8")
                calls.append(script)
                return mock.Mock(returncode=0, stdout="", stderr="")

            smoke = []
            buf = io.StringIO()
            argv = ["deliver.py", td, "--format", "html", "--dry-run"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(deliver.gen_config, "generate", return_value=0), \
                    mock.patch.object(deliver.gen_config, "verify_confirmed_spec",
                                      return_value=[]), \
                    mock.patch.object(deliver.evidence, "collect_inputs",
                                      return_value={"files": [], "missing": [],
                                                    "unresolved": []}), \
                    mock.patch.object(deliver.evidence, "content_digest",
                                      return_value="fixture"), \
                    mock.patch.object(deliver.evidence, "card_usages", return_value={}), \
                    mock.patch.object(deliver, "run_check", side_effect=fake_run), \
                    mock.patch.object(deliver, "run_html_smoke_check",
                                      side_effect=lambda *a, **k: (smoke.append(1) or
                                                                   (True, "OK"))), \
                    mock.patch.object(deliver, "atomic_release") as rel, \
                    contextlib.redirect_stdout(buf):
                code = deliver.main()

            self.assertEqual(code, 0, buf.getvalue())
            self.assertEqual(smoke, [1], "干跑未执行 HTML 侧验收")
            rel.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(td, ".release-staging")),
                             "干跑后残留 staging")

    def test_dry_run_html_smoke_failure_exits_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="qchain-dry-fail-") as td:
            _write(os.path.join(td, "config.yaml"), "output_format: html\n")
            for name in ("student.pdf", "teacher.pdf", "student.html", "teacher.html"):
                _write(os.path.join(td, name))
            _write(os.path.join(td, ".pd", "run.json"),
                   json.dumps({"slices": ["sec1"],
                               "tasks": {"sec1": {"status": "checked"}}}))

            buf = io.StringIO()
            argv = ["deliver.py", td, "--format", "html", "--dry-run"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(deliver.gen_config, "generate", return_value=0), \
                    mock.patch.object(deliver.gen_config, "verify_confirmed_spec",
                                      return_value=[]), \
                    mock.patch.object(deliver.evidence, "collect_inputs",
                                      return_value={"files": [], "missing": [],
                                                    "unresolved": []}), \
                    mock.patch.object(deliver.evidence, "content_digest",
                                      return_value="fixture"), \
                    mock.patch.object(deliver.evidence, "card_usages", return_value={}), \
                    mock.patch.object(deliver, "run_check",
                                      side_effect=lambda script, pdir, rp, extra_args=None: (
                                          _write(rp, json.dumps({
                                              "schema_version": 2, "tool": script,
                                              "tool_version": "t", "input_digest": "fixture",
                                              "status": "pass", "issues": []})),
                                          mock.Mock(returncode=0, stdout="", stderr=""))[1]), \
                    mock.patch.object(deliver, "run_html_smoke_check",
                                      side_effect=deliver.HtmlSmokeError("BLOCKED_ENV")), \
                    mock.patch.object(deliver, "atomic_release") as rel, \
                    contextlib.redirect_stdout(buf):
                code = deliver.main()

            self.assertEqual(code, 2, buf.getvalue())
            rel.assert_not_called()
            self.assertIn("HTML 离线包浏览器验收未通过", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
