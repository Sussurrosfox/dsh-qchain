#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_math_validation.py — 任务卡02：全数学段提取 / 同源宏配置 / 严格 KaTeX 校验

验收断言（repair-plan/tasks/02.md）：
  MATH-01 inline 和 display 中的 badmacro 均失败，class 写法变化不改变覆盖数量
  MATH-02 四种定界符中合法积分、分式、中文、quotgrp 均通过
  MATH-03 严格校验段数 == 全部 qc-math 段数；混入坏段退出非零并定位 card
  MATH-04 html/both 缺文件失败；pdf-only 不误拦；引擎不可用不能显示 PASS
  MATH-05 浏览器与服务器配置一致且 file:// 无 fetch 依赖

第三层测试：使用随包真实 Node/KaTeX（templates/html/katex/katex.min.js），不 mock
校验引擎。浏览器内真实渲染行为本机无浏览器时不在此证明（见证据文件 PENDING）。

运行: python -m pytest -q -p no:cacheprovider scripts/test_math_validation.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_render  # noqa: E402
import math_fragments  # noqa: E402
import render_html  # noqa: E402
import tex2cards  # noqa: E402
import test_check_render as tcr  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, ".."))
CONFIG_PATH = os.path.join(ROOT, "templates", "html", "math-config.json")
TEMPLATE_PATH = os.path.join(ROOT, "templates", "html", "deck.template.html")
VALIDATOR = os.path.join(HERE, "validate_math.cjs")
HAVE_NODE = math_fragments.find_node() is not None


def wrap_card(inner, card="c1"):
    return ('<section class="qc-card" id="qc-card-%s" data-qid="%s">'
            '<div class="qc-card-body">%s</div></section>' % (card, card, inner))


def run_gate(d, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check_render.main([d] + list(extra))
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 提取：HTMLParser，按 class token，不依赖精确 class 字符串
# ---------------------------------------------------------------------------
class FragmentExtraction(unittest.TestCase):
    VARIANTS = (
        '<span class="qc-math">$a$</span>'
        '<span class="qc-math qc-math-display">$$b$$</span>'
        '<span class="qc-math-display qc-math">$$c$$</span>'
        "<span class='qc-math'>$d$</span>"
        '<span   data-x="1"   class="  x   qc-math  y ">$e$</span>'
        '<span class="qc-math"><span class="inner">$f</span>$</span>'
    )

    def test_class_variants_all_collected(self):
        frs = math_fragments.extract_math_fragments(wrap_card(self.VARIANTS))
        self.assertEqual(len(frs), 6)
        self.assertEqual([f.raw for f in frs],
                         ["a", "b", "c", "d", "e", "f"])
        self.assertEqual([f.display for f in frs],
                         [False, True, True, False, False, False])
        self.assertEqual([f.math_id for f in frs],
                         ["qcm-%d" % i for i in range(1, 7)])
        self.assertTrue(all(f.error is None for f in frs))
        self.assertTrue(all(f.card == "c1" for f in frs))

    def test_entities_decoded_and_inner_dollar_kept(self):
        html = ('<span class="qc-math">$a &lt; b &amp;&amp; c &gt; d$</span>'
                '<span class="qc-math">$\\$5 + 5\\$$</span>')
        frs = math_fragments.extract_math_fragments(html)
        self.assertEqual(frs[0].raw, "a < b && c > d")
        # 只剥最外层定界符：转义的 \$ 保留，不能 .strip('$')
        self.assertEqual(frs[1].raw, "\\$5 + 5\\$")
        self.assertIsNone(frs[1].error)

    def test_rendered_katex_dom_not_counted_as_new_formula(self):
        html = ('<span class="qc-math"><span class="katex">'
                '<span class="katex-html"><span class="mord">x</span></span>'
                '</span></span>')
        frs = math_fragments.extract_math_fragments(html)
        self.assertEqual(len(frs), 1)

    def test_nested_qc_math_counts_once_outermost(self):
        html = ('<span class="qc-math">$x<span class="qc-math">$y$</span>$'
                '</span><span class="qc-math">$z$</span>')
        frs = math_fragments.extract_math_fragments(html)
        self.assertEqual(len(frs), 2)

    def test_void_tags_inside_math_do_not_break_depth(self):
        html = ('<span class="qc-math">$a<br>b$</span>'
                '<span class="qc-math">$c$</span>')
        frs = math_fragments.extract_math_fragments(html)
        self.assertEqual([f.raw for f in frs], ["a" + "b", "c"])

    def test_card_attribution(self):
        html = (wrap_card('<span class="qc-math">$a$</span>', "cA") +
                '<span class="qc-math">$out$</span>' +
                wrap_card('<p><span class="qc-math">$b$</span></p>', "cB"))
        frs = math_fragments.extract_math_fragments(html, source="s.html")
        self.assertEqual([f.card for f in frs], ["cA", None, "cB"])
        self.assertIn("cB", frs[2].location())
        self.assertIn("s.html", frs[2].location())

    def test_delimiter_problems_flagged(self):
        for text, why in (("x+1", "缺少"), ("$x+1", "缺少"),
                          ("$$", "为空"), ("$ $", "为空"), ("$$  $$", "为空")):
            body, disp, err = math_fragments.strip_math_delimiters(text)
            self.assertIsNotNone(err, text)
            self.assertIn(why, err, text)

    def test_display_flag_from_delimiter_or_class(self):
        self.assertTrue(math_fragments.strip_math_delimiters("$$x$$")[1])
        self.assertFalse(math_fragments.strip_math_delimiters("$x$")[1])
        self.assertTrue(math_fragments.strip_math_delimiters("$x$", True)[1])


# ---------------------------------------------------------------------------
# 共享配置（MATH-05 的服务器侧）
# ---------------------------------------------------------------------------
class SharedConfig(unittest.TestCase):
    def test_config_keeps_existing_macros_and_is_safe(self):
        cfg = math_fragments.load_math_config()
        for name, body in (("\\linf", "\\ell_\\infty"),
                           ("\\bm", "\\boldsymbol"),
                           ("\\quotgrp", "#1/#2"),
                           ("\\quotring", "#1/#2")):
            self.assertEqual(cfg["macros"][name], body)
        self.assertIs(cfg["trust"], False)
        self.assertIsInstance(cfg["maxExpand"], int)
        self.assertGreater(cfg["maxExpand"], 0)
        self.assertLessEqual(cfg["maxExpand"], 10000)

    def test_trust_cannot_be_enabled_by_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"macros": {}, "trust": True, "maxExpand": 10}, f)
            self.assertIs(math_fragments.load_math_config(p)["trust"], False)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"macros": {}, "maxExpand": None}, f)
            with self.assertRaises(ValueError):
                math_fragments.load_math_config(p)

    def test_script_json_escapes_script_closer_and_roundtrips(self):
        cfg = {"macros": {"\\evil": "</script><b>&\u2028x"}, "maxExpand": 5}
        text = math_fragments.math_config_script_json(cfg)
        self.assertNotIn("</script>", text)
        self.assertNotIn("<", text)
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertEqual(json.loads(text), cfg)


# ---------------------------------------------------------------------------
# 第三层：真实 Node/KaTeX 严格校验器
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_NODE, "需要 node")
class StrictEngine(unittest.TestCase):
    def frs(self, html):
        return math_fragments.extract_math_fragments(html)

    def test_real_engine_version_and_ok(self):
        res = math_fragments.validate_fragments(
            self.frs('<span class="qc-math">$x^2$</span>'))
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["engine"]["name"], "katex")
        self.assertRegex(res["engine"]["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["checked"], 1)

    def test_math01_bad_macro_inline_and_display_fail(self):
        for cls, tex in (("qc-math", "$\\badmacro{x}$"),
                         ("qc-math qc-math-display", "$$\\badmacro{x}$$"),
                         ("qc-math-display qc-math", "$$\\badmacro{x}$$")):
            html = wrap_card('<span class="%s">%s</span>' % (cls, tex), "cx")
            res = math_fragments.validate_fragments(self.frs(html))
            self.assertEqual(res["status"], "fail", cls)
            self.assertEqual(len(res["failures"]), 1)
            self.assertEqual(res["failures"][0]["card"], "cx")
            self.assertIn("badmacro", res["failures"][0]["message"])

    def test_unclosed_group_and_bad_environment_fail(self):
        html = ('<span class="qc-math">$\\frac{a}{b$</span>'
                '<span class="qc-math">$\\begin{nosuchenv}x\\end{nosuchenv}$'
                '</span><span class="qc-math">$\\frac{a}{b}$</span>')
        res = math_fragments.validate_fragments(self.frs(html))
        self.assertEqual(res["status"], "fail")
        self.assertEqual([f["index"] for f in res["failures"]], [1, 2])
        self.assertIsNotNone(res["failures"][0]["position"])

    def test_macros_from_shared_config_are_honoured(self):
        html = ('<span class="qc-math">$\\linf + \\bm{x} + '
                '\\quotgrp{G}{N} + \\quotring{R}{I}$</span>')
        res = math_fragments.validate_fragments(self.frs(html))
        self.assertEqual(res["status"], "ok", res)
        # 同一输入换成不含宏的配置 → 失败，证明确实用了配置里的宏
        res2 = math_fragments.validate_fragments(
            self.frs(html), config={"macros": {}, "maxExpand": 1000})
        self.assertEqual(res2["status"], "fail")

    def test_trust_false_blocks_href(self):
        # KaTeX 在 trust=false 下不抛错，而是把 \href 渲染成错误色文本；
        # 严格校验必须把这种“静默降级”判为失败，且配置里 trust:true 不得放宽。
        for tex in ("\\href{javascript:alert(1)}{x}", "\\url{http://a.b}",
                    "\\includegraphics{a.png}"):
            html = '<span class="qc-math">$%s$</span>' % tex
            res = math_fragments.validate_fragments(
                self.frs(html), config={"macros": {}, "maxExpand": 1000,
                                        "trust": True})
            self.assertEqual(res["status"], "fail", "trust 必须恒为 false: " + tex)
            self.assertIn("错误色", res["failures"][0]["message"])

    def test_max_expand_limits_runaway_macro(self):
        html = '<span class="qc-math">$\\loop$</span>'
        res = math_fragments.validate_fragments(
            self.frs(html), config={"macros": {"\\loop": "\\loop\\loop"},
                                    "maxExpand": 50})
        self.assertEqual(res["status"], "fail")

    def test_macros_do_not_leak_between_formulas(self):
        html = ('<span class="qc-math">$\\gdef\\leak{1} \\leak$</span>'
                '<span class="qc-math">$\\leak$</span>')
        res = math_fragments.validate_fragments(self.frs(html))
        self.assertEqual([f["index"] for f in res["failures"]], [2])

    def test_delimiter_errors_are_failures_not_skipped(self):
        html = ('<span class="qc-math">x+1</span>'
                '<span class="qc-math">$ok$</span>')
        res = math_fragments.validate_fragments(self.frs(html))
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["checked"], 2)

    def test_cli_exit_codes(self):
        node = math_fragments.find_node()
        good = json.dumps({"config": {}, "items": [
            {"index": 1, "id": "a", "tex": "x", "display": False}]})
        bad = json.dumps({"config": {}, "items": [
            {"index": 1, "id": "a", "tex": "\\nope", "display": False}]})

        def run(inp, *args):
            return subprocess.run([node, VALIDATOR] + list(args),
                                  input=inp.encode("utf-8"),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.assertEqual(run(good).returncode, 0)
        r = run(bad)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["failures"][0]["id"], "a")
        self.assertEqual(run(good, "--katex", "no-such-katex.js").returncode, 2)
        self.assertEqual(run("not json").returncode, 2)

    def test_math02_four_delimiters_legit_formulas_pass(self):
        """$..$ / $$..$$ / \\(..\\) / \\[..\\]：积分、分式、中文、quotgrp。"""
        samples = []
        inner = [r"\int_0^1 x^2\,dx", r"\frac{a+b}{c}",
                 r"H \le G 且 K \neq N", r"\quotgrp{G}{N} \cong \quotring{R}{I}"]
        for body in inner:
            samples += ["$%s$" % body, "$$%s$$" % body,
                        "\\(%s\\)" % body, "\\[%s\\]" % body]
        ctx = tex2cards.InlineCtx("t")
        html = wrap_card("".join(tex2cards.inline_to_html(s, ctx)
                                 for s in samples))
        frs = self.frs(html)
        self.assertEqual(len(frs), 16)
        self.assertEqual(sum(f.display for f in frs), 8)
        res = math_fragments.validate_fragments(frs)
        self.assertEqual(res["status"], "ok", res["failures"])
        self.assertEqual(res["total"], 16)


# ---------------------------------------------------------------------------
# 转换层统一：四种定界符都先过 math_prepare
# ---------------------------------------------------------------------------
class DelimiterUnification(unittest.TestCase):
    def conv(self, s):
        return tex2cards.inline_to_html(s, tex2cards.InlineCtx("t"))

    def test_all_four_delimiters_apply_math_prepare(self):
        for s in ("$ 且 $", "$$ 且 $$", "\\( 且 \\)", "\\[ 且 \\]"):
            out = self.conv(s)
            self.assertIn("\\text{且}", out, s)
            self.assertNotIn(" 且", out.replace("\\text{且}", ""), s)

    def test_quotgrp_expanded_with_slash_in_all_delimiters(self):
        for s in ("$\\quotgrp{G}{N}$", "$$\\quotgrp{G}{N}$$",
                  "\\(\\quotgrp{G}{N}\\)", "\\[\\quotgrp{G}{N}\\]"):
            out = self.conv(s)
            self.assertNotIn("quotgrp", out, s)
            self.assertIn("{G}/{N}", out, s)

    def test_mathish_macro_goes_through_math_prepare(self):
        out = self.conv("\\mathbf{向量}")
        self.assertIn("\\text{向量}", out)

    def test_display_class_matches_delimiter(self):
        self.assertIn('class="qc-math qc-math-display"', self.conv("\\[x\\]"))
        self.assertIn('class="qc-math qc-math-display"', self.conv("$$x$$"))
        self.assertNotIn("display", self.conv("\\(x\\)"))
        self.assertNotIn("display", self.conv("$x$"))
        # 展示公式经 \[ \] 也写成 $$..$$ 定界，可被 strip_math_delimiters 还原
        frs = math_fragments.extract_math_fragments(self.conv("\\[x+1\\]"))
        self.assertEqual((frs[0].raw, frs[0].display), ("x+1", True))


# ---------------------------------------------------------------------------
# check_render 门禁集成
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_NODE, "需要 node")
class CheckRenderStrictMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-math-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def report(self):
        p = os.path.join(self.tmp, "r.json")
        run_gate(self.tmp, "--json", p)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_math01_inline_and_display_badmacro_fail(self):
        for cls, tex in (("qc-math", "$\\badmacro{x}$"),
                         ("qc-math qc-math-display", "$$\\badmacro{x}$$"),
                         ("qc-math-display qc-math", "$$\\badmacro{x}$$")):
            d = tempfile.mkdtemp(prefix="qchain-math-v-")
            try:
                tcr.make_self(d, extra=wrap_card(
                    '<span class="%s">%s</span>' % (cls, tex), "bad1"))
                rc, out = run_gate(d)
                self.assertEqual(rc, 1, (cls, out))
                self.assertIn("badmacro", out)
                self.assertIn("bad1", out)
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def test_math01_class_variant_does_not_change_coverage(self):
        counts = []
        for cls in ("qc-math", "qc-math qc-math-display",
                    "qc-math-display qc-math", "x qc-math"):
            d = tempfile.mkdtemp(prefix="qchain-math-v-")
            try:
                tcr.make_self(d, extra='<span class="%s">$$a$$</span>' % cls)
                p = os.path.join(d, "r.json")
                rc, out = run_gate(d, "--json", p)
                self.assertEqual(rc, 0, (cls, out))
                with open(p, encoding="utf-8") as f:
                    counts.append(json.load(f)["evidence"]["math_strict"]
                                  ["total"])
            finally:
                shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(len(set(counts)), 1, counts)
        self.assertEqual(counts[0], 1)

    def test_math03_checked_equals_all_spans_and_bad_card_located(self):
        good = "".join('<span class="qc-math">$x_{%d}$</span>' % i
                       for i in range(5))
        bad = '<span class="qc-math qc-math-display">$$\\frac{1}{$$</span>'
        tcr.make_self(self.tmp, extra=wrap_card(good, "g1") +
                      wrap_card(bad, "card-bad-7"))
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 1, out)
        self.assertIn("card-bad-7", out)
        self.assertNotIn("g1", "\n".join(
            l for l in out.splitlines() if "KaTeX" in l and "严格" in l))
        ev = self.report()["evidence"]["math_strict"]
        # student.html 含 6 段；teacher.html 不含 extra → 0 段
        self.assertEqual(ev["total"], 6)
        self.assertEqual(ev["checked"], 6)
        self.assertEqual(ev["failed"], 1)
        self.assertEqual(ev["status"], "fail")
        self.assertRegex(ev["engine_version"], r"^\d+\.\d+")

    def test_math03_counts_both_versions(self):
        tcr.make_self(self.tmp, extra='<span class="qc-math">$a$</span>'
                                      '<span class="qc-math">$b$</span>')
        with open(os.path.join(self.tmp, "teacher.html"), "a",
                  encoding="utf-8") as f:
            f.write('<span class="qc-math qc-math-display">$$c$$</span>')
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 0, out)
        ev = self.report()["evidence"]["math_strict"]
        self.assertEqual((ev["total"], ev["checked"], ev["failed"]), (3, 3, 0))

    def test_math04_html_and_both_missing_html_fail_pdf_only_ok(self):
        tcr.make_self(self.tmp)
        for v in ("student", "teacher"):
            os.remove(os.path.join(self.tmp, v + ".html"))
        for fmt in ("html", "both"):
            rc, out = run_gate(self.tmp, "--format", fmt)
            self.assertEqual(rc, 1, (fmt, out))
            self.assertIn("缺少 student.html", out)
            self.assertIn("缺少 teacher.html", out)
        rc, out = run_gate(self.tmp, "--format", "pdf")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("缺少 student.html", out)

    def test_math04_single_missing_html_fails_under_explicit_format(self):
        tcr.make_self(self.tmp)
        os.remove(os.path.join(self.tmp, "teacher.html"))
        rc, out = run_gate(self.tmp, "--format", "html")
        self.assertEqual(rc, 1)
        self.assertIn("缺少 teacher.html", out)

    def test_math04_engine_unavailable_is_not_pass(self):
        tcr.make_self(self.tmp, extra='<span class="qc-math">$x$</span>')
        old = os.environ.get(math_fragments.NODE_ENV)
        os.environ[math_fragments.NODE_ENV] = os.path.join(
            self.tmp, "no-such-node.exe")
        try:
            rc, out = run_gate(self.tmp)
            ev = self.report()
        finally:
            if old is None:
                os.environ.pop(math_fragments.NODE_ENV, None)
            else:
                os.environ[math_fragments.NODE_ENV] = old
        self.assertEqual(rc, 1, out)
        self.assertIn("MATH_ENGINE_UNAVAILABLE", out)
        self.assertNotIn("通过：", out)
        self.assertEqual(ev["status"], "fail")
        self.assertEqual(ev["evidence"]["math_strict"]["status"],
                         "env_error")

    def test_math04_pdf_only_does_not_need_node(self):
        tcr.make_self(self.tmp)
        old = os.environ.get(math_fragments.NODE_ENV)
        os.environ[math_fragments.NODE_ENV] = os.path.join(self.tmp, "nonode")
        try:
            rc, out = run_gate(self.tmp, "--format", "pdf")
        finally:
            if old is None:
                os.environ.pop(math_fragments.NODE_ENV, None)
            else:
                os.environ[math_fragments.NODE_ENV] = old
        self.assertEqual(rc, 0, out)

    def test_clean_formulas_pass_and_old_h3_h6_still_active(self):
        tcr.make_self(self.tmp, extra='<span class="qc-math">$x$</span>'
                                      '<span class="qc-math">$$\\int_0^1 f$$'
                                      '</span>')
        rc, out = run_gate(self.tmp)
        self.assertEqual(rc, 0, out)
        d2 = tempfile.mkdtemp(prefix="qchain-math-v-")
        try:
            tcr.make_self(d2, extra='<span class="qc-math qc-math-display">'
                                    '$$\\notamacro$$</span>')
            rc, out = run_gate(d2)
            self.assertEqual(rc, 1)
            self.assertIn("KaTeX 不支持的宏", out)    # 旧 H6 白名单仍对展示公式生效
            tcr.make_self(d2, extra='<span class="qc-math qc-math-display">'
                                    '$$  $$</span>')
            rc, out = run_gate(d2)
            self.assertEqual(rc, 1)
            self.assertIn("为空", out)               # 旧 H3 空段仍生效
        finally:
            shutil.rmtree(d2, ignore_errors=True)


# ---------------------------------------------------------------------------
# MATH-05：浏览器与服务器同源配置，file:// 无 fetch
# ---------------------------------------------------------------------------
class BrowserConfigParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-math-r-")
        with open(TEMPLATE_PATH, encoding="utf-8") as f:
            self.tpl = f.read()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def render(self, template_path=TEMPLATE_PATH):
        deck = {"title": "t", "cards": [
            {"id": "c1", "kind": "section", "title": "第一节",
             "blocks": []}]}
        out = os.path.join(self.tmp, "student.html")
        ok, msg = render_html.render_version(
            deck, self.tmp, "student", out, template_path)
        self.assertTrue(ok, msg)
        with open(out, encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def embedded(html):
        m = re.search(r'<script type="application/json" id="qc-math-config">'
                      r'(.*?)</script>', html, re.S)
        assert m, "缺少内嵌 math 配置 script"
        return json.loads(m.group(1))

    def test_template_has_placeholder_and_no_fetch(self):
        self.assertIn("{{MATH_CONFIG_JSON}}", self.tpl)
        self.assertNotRegex(self.tpl, r"\bfetch\s*\(")
        self.assertNotIn("XMLHttpRequest", self.tpl)
        self.assertNotIn("math-config.json", self.tpl)

    def test_embedded_config_equals_shared_json(self):
        html = self.render()
        cfg = self.embedded(html)
        self.assertEqual(cfg, math_fragments.load_math_config(CONFIG_PATH))
        self.assertNotRegex(html, r"\bfetch\s*\(")
        self.assertNotIn("{{", html)

    def test_script_closer_in_config_cannot_break_page(self):
        tdir = os.path.join(self.tmp, "tpl")
        os.makedirs(tdir)
        shutil.copy(TEMPLATE_PATH, os.path.join(tdir, "deck.template.html"))
        cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
        cfg["macros"]["\\evil"] = "</script><script>alert(1)</script>"
        with open(os.path.join(tdir, "math-config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cfg, f)
        html = self.render(os.path.join(tdir, "deck.template.html"))
        n_tpl = self.tpl.count("</script>")
        self.assertEqual(html.count("</script>"), n_tpl)
        self.assertEqual(self.embedded(html)["macros"]["\\evil"],
                         "</script><script>alert(1)</script>")

    def test_template_without_placeholder_is_rejected_not_silent(self):
        tdir = os.path.join(self.tmp, "old")
        os.makedirs(tdir)
        p = os.path.join(tdir, "deck.template.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.tpl.replace("{{MATH_CONFIG_JSON}}", ""))
        deck = {"title": "t", "cards": [{"id": "c1", "kind": "section",
                                          "title": "x", "blocks": []}]}
        ok, msg = render_html.render_version(
            deck, self.tmp, "student", os.path.join(self.tmp, "o.html"), p)
        self.assertFalse(ok)
        self.assertIn("MATH_CONFIG_JSON", msg)

    def test_browser_uses_strict_per_node_render_with_status(self):
        t = self.tpl
        self.assertIn("katex.render(", t)
        self.assertIn("throwOnError: true", t)
        self.assertNotIn("renderMathInElement(", t)
        self.assertIn("data-math-id", t)
        self.assertIn("data-math-status", t)
        self.assertIn("qc-math-config", t)
        self.assertIn("trust: false", t)
        # 配置不再在模板里硬编码宏
        self.assertNotIn('"\\\\linf"', t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
