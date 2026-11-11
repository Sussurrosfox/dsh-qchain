"""E7 回归：html_smoke 浏览器不可用时的识别 + deliver auto 降级。

历史缺陷（bug.md E7 / 批次 A 的 A12 相邻）：受限沙箱里
``pw.chromium.launch()`` 不报错只是**永久挂起**（named pipe EPERM，实测
>24 分钟零输出），调用方拿不到任何诊断；且旧交付链路只能靠人工放环境变量
``QCHAIN_ALLOW_SKIP_HTML_SMOKE=1`` + 落标记文件绕过，节点还会把该变量写进
进程环境污染后续工作区。

本文件锁三件事：
  1. html_smoke.probe_browser 在子进程里带硬超时探测，挂死→可归因诊断；
  2. 分类器把 named pipe / EPERM / 权限拒绝识别为"环境问题"而非"没装浏览器"；
  3. deliver.py 的 ``--html-smoke-mode=auto`` 只降级 BLOCKED_ENV，
     HTML 内容缺陷仍照旧中止，且 manifest 如实记 blocked_env_degraded。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deliver  # noqa: E402
import html_smoke  # noqa: E402


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TestClassifyLaunchFailure(unittest.TestCase):
    def test_named_pipe_and_eperm_are_environment_not_missing(self):
        for text in (
            "OSError: [EPERM] Failed to create named pipe",
            "pipe is being closed",
            "PermissionError: [WinError 5] Access is denied",
            "拒绝访问。",
        ):
            kind, hint = html_smoke.classify_launch_failure(text)
            self.assertEqual(kind, "pipe", text)
            self.assertIn("受限沙箱", hint)

    def test_missing_executable_is_missing(self):
        kind, _ = html_smoke.classify_launch_failure(
            "Executable doesn't exist at C:\\nope\\chrome.exe")
        self.assertEqual(kind, "missing")

    def test_unknown_is_other(self):
        kind, hint = html_smoke.classify_launch_failure("weird boom")
        self.assertEqual(kind, "other")
        self.assertEqual(hint, "")


class TestProbeBrowser(unittest.TestCase):
    def _patch(self, rc, out, err="", timeout=False):
        def fake_run(cmd, **kwargs):
            if timeout:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"],
                                                output=out)
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return mock.patch.object(html_smoke.subprocess, "run", fake_run)

    def test_hang_is_reported_as_timeout_not_silently_passed(self):
        with self._patch(0, "", timeout=True):
            ok, reason = html_smoke.probe_browser(timeout_s=5)
        self.assertFalse(ok)
        self.assertIn("超时", reason)
        self.assertIn(html_smoke.PROBE_TIMEOUT_ENV, reason)

    def test_selftest_pass_is_ok(self):
        out = "[html_smoke] 可用浏览器: C:\\chrome.exe\n"
        with self._patch(0, out):
            ok, reason = html_smoke.probe_browser(timeout_s=5)
        self.assertTrue(ok)
        self.assertIn("chrome.exe", reason)

    def test_pipe_failure_is_classified(self):
        err = "OSError: [EPERM] named pipe\n"
        with self._patch(1, "", err):
            ok, reason = html_smoke.probe_browser(timeout_s=5)
        self.assertFalse(ok)
        self.assertIn("受限环境", reason)

    def test_timeout_floor_and_env_override(self):
        with mock.patch.dict(os.environ,
                             {html_smoke.PROBE_TIMEOUT_ENV: "7"}):
            with self._patch(0, "[html_smoke] x") as _:
                seen = {}
                orig = html_smoke.subprocess.run

                def spy(cmd, **kwargs):
                    seen["t"] = kwargs["timeout"]
                    return subprocess.CompletedProcess(cmd, 0, "[html_smoke] x")
                with mock.patch.object(html_smoke.subprocess, "run", spy):
                    html_smoke.probe_browser()
            self.assertEqual(seen["t"], 7)
        # 低于下限时兜底抬到 5s，避免误配 0/1 造成假阳性
        with self._patch(0, "[html_smoke] x") as _:
            seen2 = {}

            def spy2(cmd, **kwargs):
                seen2["t"] = kwargs["timeout"]
                return subprocess.CompletedProcess(cmd, 0, "[html_smoke] x")
            with mock.patch.object(html_smoke.subprocess, "run", spy2):
                html_smoke.probe_browser(timeout_s=1)
        self.assertEqual(seen2["t"], 5)

    def test_run_smoke_probes_before_launch(self):
        """探测失败时必须立刻 blocked_env，绝不进入 launch。"""
        calls = {"launch": 0}

        def fake_probe(timeout_s=None, browser_path=None, candidates=None):
            return False, "浏览器启动探测超时"

        def boom(*a, **k):  # pragma: no cover - 到达即失败
            calls["launch"] += 1
            raise AssertionError("探测失败后仍去启动浏览器")

        pkg = tempfile.mkdtemp(prefix="qchain-e7-")
        _write(os.path.join(pkg, "student.html"), "<html></html>")
        with mock.patch.object(html_smoke, "probe_browser", fake_probe), \
                mock.patch.object(html_smoke, "import_playwright",
                                  return_value=boom):
            report, code = html_smoke.run_smoke(
                pkg, report_path=os.path.join(tempfile.mkdtemp(), "r.json"))
        self.assertEqual(code, html_smoke.EXIT_BLOCKED_ENV)
        self.assertEqual(report["status"], "blocked_env")
        self.assertEqual(
            (report.get("evidence") or {}).get("dependency", {}).get("stage"),
            "browser_probe")
        self.assertEqual(calls["launch"], 0)


class TestDeliverAutoDegrade(unittest.TestCase):
    def _manifest(self):
        return {"deliverables": [{"file": "student.html"}],
                "files": []}

    def test_auto_mode_raises_blocked_env_subtype(self):
        """auto 模式下浏览器不可用抛的是子类，内容缺陷抛父类。"""
        with tempfile.TemporaryDirectory(prefix="qchain-e7-") as td:
            _write(os.path.join(td, "student.html"), "<html></html>")
            _write(os.path.join(td, "teacher.html"), "<html></html>")
            with mock.patch.object(deliver, "run_html_smoke_check_ex",
                                   return_value=(False, "BLOCKED_ENV: no browser", True)):
                with self.assertRaises(deliver.HtmlSmokeBlockedEnv):
                    deliver.prepare_staging(td, self._manifest(), fmt="html",
                                            html_smoke_mode="auto")

    def test_strict_mode_raises_plain_error(self):
        with tempfile.TemporaryDirectory(prefix="qchain-e7-") as td:
            _write(os.path.join(td, "student.html"), "<html></html>")
            with mock.patch.object(deliver, "run_html_smoke_check_ex",
                                   return_value=(False, "BLOCKED_ENV: no browser", True)):
                with self.assertRaises(deliver.HtmlSmokeError) as ctx:
                    deliver.prepare_staging(td, self._manifest(), fmt="html",
                                            html_smoke_mode="strict")
        self.assertNotIsInstance(ctx.exception, deliver.HtmlSmokeBlockedEnv)

    def test_content_failure_is_not_degradable_in_auto_mode(self):
        """auto 不等于放水：HTML 内容缺陷在 auto 下照样中止。"""
        with tempfile.TemporaryDirectory(prefix="qchain-e7-") as td:
            _write(os.path.join(td, "student.html"), "<html></html>")
            with mock.patch.object(deliver, "run_html_smoke_check_ex",
                                   return_value=(False, "16 项问题", False)):
                with self.assertRaises(deliver.HtmlSmokeError) as ctx:
                    deliver.prepare_staging(td, self._manifest(), fmt="html",
                                            html_smoke_mode="auto")
        self.assertNotIsInstance(ctx.exception, deliver.HtmlSmokeBlockedEnv)

    def test_atomic_release_auto_publishes_with_degraded_marker(self):
        with tempfile.TemporaryDirectory(prefix="qchain-e7-") as td:
            _write(os.path.join(td, "student.html"), "<html></html>")
            _write(os.path.join(td, "teacher.html"), "<html></html>")
            with mock.patch.object(deliver, "run_html_smoke_check_ex",
                                   return_value=(False, "BLOCKED_ENV: 挂起", True)):
                rel = deliver.atomic_release(td, self._manifest(), fmt="html",
                                             html_smoke_mode="auto")
            man = json.load(open(os.path.join(rel, "manifest.json"),
                                 encoding="utf-8"))
            self.assertEqual(man["html_smoke"]["status"], "blocked_env_degraded")
            self.assertIn("BLOCKED_ENV", man["html_smoke"]["note"])
            self.assertTrue(os.path.isfile(os.path.join(rel, "student.html")),
                            "auto 降级后交付物缺失")

    def test_resolve_skip_rejects_unknown_mode(self):
        skip, reason = deliver.resolve_html_smoke_skip(False, {}, mode="bogus")
        self.assertIsNone(skip)
        self.assertIn("--html-smoke-mode", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
