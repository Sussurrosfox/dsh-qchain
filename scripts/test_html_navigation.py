#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_html_navigation.py — 任务卡03：HTML 目录跳转与窄屏可读性（真实浏览器验收）

验收映射：
  NAV-01  每个 data-goto 命中唯一真实卡片；点击全部目录条目均显示正确目标
  NAV-02  390px 下 document.scrollWidth <= innerWidth+1；长公式只在自身容器内
          横向滚动且内容可达；不得用 body/卡片 overflow:hidden 掩盖
  NAV-03  翻页/键盘/URL hash 定位/选项判题/答案展开/教师默认展开保持正确

静态字符串断言不能代替实际交互：本文件用 Playwright(Python) 打开真实生成的
student.html / teacher.html（file://，阻断全部 http/https），在 desktop(1280)
与 390px 两种视口逐卡检查。浏览器不可用时 skip（并给出明确原因），不会算 PASS
的证据——BLOCKED_ENV 需在证据里如实记录。

运行: python -m pytest -q -p no:cacheprovider scripts/test_html_navigation.py
可选环境变量：
  QC_NAV_SHOT_DIR   设置后在该目录保存 desktop/390px 关键卡截图
  QC_NAV_REAL_CARDS 真实项目 cards.json（只读，缺省 self/cards.json，不存在则跳过）
"""
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
import render_html  # noqa: E402
import test_render as tr  # noqa: E402

try:  # pragma: no cover - 环境探测
    from playwright.sync_api import sync_playwright
    _HAVE_PW = True
except Exception:  # noqa: BLE001
    sync_playwright = None
    _HAVE_PW = False

BROWSER_CANDIDATES = (
    ("exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("channel", "chrome"),
    ("channel", "msedge"),
)

VIEWPORTS = (("desktop", 1280, 800), ("narrow390", 390, 844))

LONG_TOKEN = "A" * 140                      # 无断点长串（URL/标识符）
# 长选项：不可断的长英文串（长标识符一类）。中文选项与 KaTeX 选项都会自动
# 换行，只有这种"无断点 token"才会真正把选项按钮撑宽，必须覆盖。
LONG_OPT = "Z" * 60
LONG_CN = ("这是一段特别特别长的中文说明文字用来检验窄屏下选项与题干是否能够自动换行"
           "而不是把整个页面撑出屏幕之外也不应被卡片裁切掉任何内容")
# 长选项公式：必须真的比 390px 下的选项盒更宽（8 项 dfrac 在 316px 里仍放得下，
# 会让"长选项"断言退化成空跑），故取 14 项——见 test_nav02_long_options_wrap_inside_card。
LONG_FRAC = "+".join(r"\dfrac{A_{%d}}{x-a_{%d}}" % (i, i) for i in range(1, 15)) + "+C"
LONG_DISPLAY = "+".join(r"x_{%d}^{2}y_{%d}" % (i, i) for i in range(1, 36))

SEC_LONG = (
    "\\section*{I 第一节" + LONG_CN + "}\n"
    "\\begin{sectiondialogue}[入口：试引入]\n"
    "\\speaker{$\\Psi$}{" + LONG_CN + LONG_TOKEN + "。}\n"
    "\\speaker{$\\alpha$}{有什么规律？}\n"
    "\\speaker{$\\Psi$}{\\dlgteacher{答案先不说。}请完成下题。}\n"
    "\\end{sectiondialogue}\n\n"
    "导读：" + LONG_CN + "。\n\n"
    "\\begin{probchain}\n"
    "% qid: q1 | 题型: 进入题 | 难度: ★☆☆\n"
    "\\item \\qtype{进入题} " + LONG_CN + LONG_TOKEN + "。计算 $1+1=$"
    "\\fillin[" + LONG_CN + "甲]{15mm}["
    + LONG_CN + "甲/" + LONG_CN + "乙/" + LONG_CN + "丙/" + LONG_CN + "丁] 然后化简\n"
    "$$" + LONG_DISPLAY + "$$\n"
    "\\ansspace{1cm}\n"
    "\\begin{solution}\n$2$。" + LONG_CN + "\n$$" + LONG_DISPLAY + "$$\n\\end{solution}\n"
    "\\begin{teacherNote}\n观察加法。\n\\end{teacherNote}\n"
    "% qid: q2 | 题型: 巩固题 | 难度: ★★☆\n"
    "\\item \\qtype{巩固题} 部分分式 $\\displaystyle\\int \\dfrac{1}{x}\\,dx=$"
    "\\fillin[$" + LONG_FRAC + "$]{15mm}[$" + LONG_FRAC + "$/$" + LONG_FRAC.replace("A_{1}", "B_{1}")
    + "$/$" + LONG_FRAC.replace("A_{2}", "B_{2}") + "$/$" + LONG_FRAC.replace("A_{3}", "B_{3}") + "$]。\n"
    "\\begin{solution}\n见上。\n\\end{solution}\n"
    "% qid: q3 | 题型: 巩固题 | 难度: ★☆☆\n"
    "\\item \\qtype{巩固题} 简单 $2+2=$\\fillin[$4$]{15mm}[$3$/$4$/$5$/$6$]。\n"
    # 长选项（无断点长串）单独一组：中文选项与 KaTeX 选项都会自动换行，
    # 只有这种 token 才能真正把按钮撑宽，是 NAV-02 的必测路径。
    "\\fillin[" + LONG_OPT + "]{15mm}["
    + LONG_OPT + "/" + LONG_OPT + "b/" + LONG_OPT + "c/" + LONG_OPT + "d]。\n"
    "\\begin{solution}\n$4$。\n\\end{solution}\n"
    "\\end{probchain}\n"
    "\\begin{microknowledge}[" + LONG_CN + "]\n"
    "\\begin{enumerate}\n\\item " + LONG_CN + LONG_TOKEN + "。\n"
    "\\item 展示 $$" + LONG_DISPLAY + "$$\n\\end{enumerate}\n"
    "\\end{microknowledge}\n"
    "\\begin{knowledgebox}[本节结论]\n结论一。" + LONG_CN + "\n\\end{knowledgebox}\n"
)


def find_browser(p):
    """返回 (browser, 描述)；全部失败返回 (None, 原因)。"""
    errors = []
    for kind, val in BROWSER_CANDIDATES:
        try:
            if kind == "exe":
                if not os.path.exists(val):
                    continue
                return p.chromium.launch(executable_path=val, headless=True), val
            return p.chromium.launch(channel=val, headless=True), "channel:" + val
        except Exception as exc:  # noqa: BLE001
            errors.append("%s=%s: %s" % (kind, val, str(exc)[:120]))
    return None, "; ".join(errors) or "未找到可用的 Chrome/Edge"


class _TocParser(HTMLParser):
    """按标签属性收集 TOC 条目与卡片 id（不依赖正则的属性顺序）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.goto = []
        self.hrefs = []
        self.card_ids = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "a" and "qc-toc-item" in classes:
            self.goto.append(a.get("data-goto"))
            self.hrefs.append(a.get("href"))
        if tag == "section" and "qc-card" in classes:
            self.card_ids.append(a.get("id"))


def parse_nav(html):
    p = _TocParser()
    p.feed(html)
    return p


# 页面内逐元素量取溢出/裁切信息；返回 JSON 可序列化对象
_METRICS_JS = r"""
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const res = {innerWidth: vw, scrollWidth: doc.scrollWidth,
               bodyScrollWidth: document.body.scrollWidth, offenders: [],
               scrollers: [], clipped: [], hiddenOverflowAncestors: []};
  const card = Array.from(document.querySelectorAll('.qc-card')).find(c => !c.hidden);
  if (!card) { res.noVisibleCard = true; return res; }
  res.cardId = card.id;
  const cr = card.getBoundingClientRect();
  res.cardRight = cr.right;
  // 整页/整卡不得用 overflow 掩盖
  for (const el of [document.documentElement, document.body,
                    document.getElementById('qc-stage'), card,
                    card.querySelector('.qc-card-body')]) {
    if (!el) continue;
    const ox = getComputedStyle(el).overflowX;
    if (ox === 'hidden' || ox === 'clip') res.hiddenOverflowAncestors.push((el.id || el.className || el.tagName) + ':' + ox);
  }
  function clipAncestor(el) {
    // 返回 el 与 card 之间第一个裁切/滚动祖先（含自身）
    for (let n = el; n && n !== card; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.overflowX !== 'visible') return n;
    }
    return null;
  }
  const all = card.querySelectorAll('*');
  const seenScroller = new Set();
  for (const el of all) {
    if (el.closest('.katex-mathml')) continue;     // 屏幕阅读器用，视觉隐藏
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const clip = clipAncestor(el);
    if (clip) {
      const cs = getComputedStyle(clip);
      if (!seenScroller.has(clip)) {
        seenScroller.add(clip);
        const cr2 = clip.getBoundingClientRect();
        const scrollable = clip.scrollWidth > clip.clientWidth + 1;
        res.scrollers.push({tag: clip.tagName, cls: clip.className,
          overflowX: cs.overflowX, scrollable: scrollable,
          clientWidth: clip.clientWidth, scrollWidth: clip.scrollWidth,
          rectRight: cr2.right, insideCard: cr2.right <= cr.right + 1,
          text: (clip.textContent || '').slice(0, 30)});
        if (scrollable) {
          // 可达性：滚到最右后，最右侧子孙的右缘必须落入容器可视区
          const before = clip.scrollLeft;
          clip.scrollLeft = clip.scrollWidth;
          let maxRight = 0;
          clip.querySelectorAll('*').forEach(d => {
            if (d.closest('.katex-mathml')) return;
            // 只统计「被本容器裁切」的内容：若 d 自身另有更近的裁切/滚动祖先
            // （如 KaTeX 拉伸定界符的 <svg overflow:hidden>，其内部 path 的
            // getBoundingClientRect 返回的是用户坐标单位，可达 7000+px 的幻影盒），
            // 该内容由内层容器负责，不计入本容器的可达性。
            if (clipAncestor(d) !== clip) return;
            const dr = d.getBoundingClientRect();
            if (dr.width === 0 && dr.height === 0) return;
            maxRight = Math.max(maxRight, dr.right);
          });
          const clipR = clip.getBoundingClientRect().right;
          res.scrollers[res.scrollers.length - 1].reachable = maxRight <= clipR + 2;
          res.scrollers[res.scrollers.length - 1].maxRight = maxRight;
          res.scrollers[res.scrollers.length - 1].clipRight = clipR;
          clip.scrollLeft = before;
        }
      }
      if (cs.overflowX === 'hidden' || cs.overflowX === 'clip') {
        // overflow:hidden 的容器内部内容被裁掉则视为被遮掩
        if (clip.scrollWidth > clip.clientWidth + 1)
          res.clipped.push(clip.tagName + '.' + clip.className);
      }
      continue;
    }
    if (r.right > cr.right + 1 || r.right > vw + 1)
      res.offenders.push({tag: el.tagName, cls: String(el.className).slice(0, 40),
                          right: Math.round(r.right), text: (el.textContent || '').slice(0, 24)});
  }
  // 固定导航/顶栏自身不得越界
  for (const sel of ['.qc-nav', '.qc-topbar']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 || r.left < -1) res.offenders.push({tag: sel, right: Math.round(r.right), left: Math.round(r.left)});
  }
  return res;
}
"""


class _BrowserCase(unittest.TestCase):
    """类级共享一个浏览器与夹具项目。"""

    @classmethod
    def setUpClass(cls):
        if not _HAVE_PW:
            raise unittest.SkipTest("BLOCKED_ENV: 未安装 playwright(Python)")
        cls._pw = sync_playwright().start()
        cls.browser, cls.browser_desc = find_browser(cls._pw)
        if cls.browser is None:
            cls._pw.stop()
            raise unittest.SkipTest("BLOCKED_ENV: 无可用浏览器: %s" % cls.browser_desc)
        cls.tmp = tempfile.mkdtemp(prefix="qchain-html-nav-")
        cls.proj = os.path.join(cls.tmp, "proj")
        os.makedirs(cls.proj)
        tr.make_project(cls.proj, sec=SEC_LONG, title="导航与窄屏夹具")
        cls.deck, ok = tr.render_both(cls.proj)
        assert ok, "夹具渲染失败"
        cls.shot_dir = os.environ.get("QC_NAV_SHOT_DIR")
        if cls.shot_dir:
            os.makedirs(cls.shot_dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls._pw.stop()
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers ----
    def open_page(self, html_path, width, height, hash_=""):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        self.errors = []
        self.blocked = []

        def _route(route):
            self.blocked.append(route.request.url)
            route.abort()
        page.route(re.compile(r"^https?://"), _route)
        page.on("pageerror", lambda e: self.errors.append("pageerror: %s" % e))
        page.on("console", lambda m: self.errors.append("console.error: %s" % m.text)
                if m.type == "error" else None)
        url = "file:///" + html_path.replace("\\", "/") + hash_
        page.goto(url)
        page.wait_for_function(
            "document.documentElement.getAttribute('data-math-report') !== null",
            timeout=20000)
        return ctx, page

    def visible_card_ids(self, page):
        return page.evaluate(
            "Array.from(document.querySelectorAll('.qc-card')).filter(c=>!c.hidden).map(c=>c.id)")

    def shot(self, page, name):
        if self.shot_dir:
            page.screenshot(path=os.path.join(self.shot_dir, name + ".png"))


class NavigationStatic(unittest.TestCase):
    """不需要浏览器的静态一致性（只做补充；真实交互见 NavigationBrowser）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-html-nav-static-")
        tr.make_project(self.tmp, sec=SEC_LONG, title="静态夹具")
        self.deck, ok = tr.render_both(self.tmp)
        self.assertTrue(ok)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nav01_goto_matches_unique_card_dom_id(self):
        for v in ("student", "teacher"):
            with open(os.path.join(self.tmp, v + ".html"), encoding="utf-8") as f:
                nav = parse_nav(f.read())
            n = len(self.deck["cards"])
            self.assertEqual(len(nav.card_ids), n)
            self.assertEqual(len(set(nav.card_ids)), n, "卡片 DOM id 必须唯一")
            self.assertEqual(len(nav.goto), n)
            for g in nav.goto:
                self.assertEqual(nav.card_ids.count(g), 1,
                                 "%s: data-goto=%r 未命中唯一卡片 DOM id" % (v, g))
            # URL #/<cid> 外链含义保持：href 为 #/<cid>，cid 即牌组卡片 id
            self.assertEqual([h for h in nav.hrefs],
                             ["#/%s" % c["id"] for c in self.deck["cards"]])
            # 且与 qc-card-<cid> 的映射恰为一一对应（不双加前缀）
            self.assertEqual(nav.goto, ["qc-card-%s" % c["id"] for c in self.deck["cards"]])


class NavigationBrowser(_BrowserCase):

    def _html(self, version):
        return os.path.join(self.proj, version + ".html")

    # ---------------- NAV-01 ----------------
    def test_nav01_every_toc_button_goes_to_correct_card(self):
        total_checked = {}
        for version in ("student", "teacher"):
            for vp, w, h in VIEWPORTS:
                ctx, page = self.open_page(self._html(version), w, h)
                try:
                    gotos = page.evaluate(
                        "Array.from(document.querySelectorAll('.qc-toc-item')).map(a=>a.getAttribute('data-goto'))")
                    card_ids = page.evaluate(
                        "Array.from(document.querySelectorAll('.qc-card')).map(c=>c.id)")
                    self.assertEqual(len(gotos), len(card_ids))
                    for g in gotos:
                        self.assertEqual(card_ids.count(g), 1, "data-goto %r 未命中唯一卡片" % g)
                    # 逆序点击，避免“恰好已在目标页”造成假通过
                    for k in reversed(range(len(gotos))):
                        page.click("#qc-btn-toc")
                        page.wait_for_selector("#qc-toc.qc-open")
                        page.click('.qc-toc-item[data-goto="%s"]' % gotos[k])
                        vis = self.visible_card_ids(page)
                        self.assertEqual(vis, [gotos[k]],
                                         "%s/%s 点击目录第%d项后显示 %r，期望 %r"
                                         % (version, vp, k + 1, vis, gotos[k]))
                        self.assertFalse(page.evaluate("document.getElementById('qc-toc').classList.contains('qc-open')"),
                                         "跳转后目录应收起")
                        self.assertEqual(
                            page.evaluate("document.getElementById('qc-indicator').textContent"),
                            "%02d / %02d" % (k + 1, len(gotos)))
                        self.assertEqual(
                            page.evaluate("location.hash"), "#/" + gotos[k].replace("qc-card-", "", 1))
                        cur = page.evaluate(
                            "Array.from(document.querySelectorAll('.qc-toc-item.qc-current')).map(a=>a.getAttribute('data-goto'))")
                        self.assertEqual(cur, [gotos[k]], "目录当前项高亮应与页面一致")
                    total_checked["%s/%s" % (version, vp)] = len(gotos)
                    self.assertEqual(self.errors, [], "页面存在 JS/控制台错误")
                    self.assertEqual(self.blocked, [], "页面不应请求外部网络")
                finally:
                    ctx.close()
        self.assertTrue(all(v == len(self.deck["cards"]) for v in total_checked.values()))
        type(self).nav01_counts = total_checked

    # ---------------- NAV-02 ----------------
    def test_nav02_narrow_390_no_page_overflow_each_card(self):
        for version in ("student", "teacher"):
            ctx, page = self.open_page(self._html(version), 390, 844)
            try:
                n = len(self.deck["cards"])
                seen_scroller_reachable = 0
                for k in range(n):
                    page.keyboard.press("Home")
                    for _ in range(k):
                        page.keyboard.press("ArrowRight")
                    m = page.evaluate(_METRICS_JS)
                    cid = m.get("cardId")
                    self.assertLessEqual(
                        m["scrollWidth"], m["innerWidth"] + 1,
                        "%s 卡 %s: document.scrollWidth=%s > innerWidth=%s；越界元素=%s"
                        % (version, cid, m["scrollWidth"], m["innerWidth"], m["offenders"][:4]))
                    self.assertLessEqual(m["bodyScrollWidth"], m["innerWidth"] + 1)
                    self.assertEqual(m["offenders"], [],
                                     "%s 卡 %s 有元素越出卡片/视口且无滚动容器: %s"
                                     % (version, cid, m["offenders"][:4]))
                    self.assertEqual(m["hiddenOverflowAncestors"], [],
                                     "不得用 overflow:hidden 遮掩: %s" % m["hiddenOverflowAncestors"])
                    self.assertEqual(m["clipped"], [], "存在被 overflow:hidden 裁切的内容: %s" % m["clipped"])
                    for s in m["scrollers"]:
                        if s["scrollable"]:
                            self.assertIn(s["overflowX"], ("auto", "scroll"),
                                          "可滚动容器必须允许横向滚动: %s" % s)
                            self.assertTrue(s["insideCard"], "滚动容器本身越出卡片: %s" % s)
                            self.assertTrue(s.get("reachable"), "滚动到末端后内容仍不可达: %s" % s)
                            seen_scroller_reachable += 1
                    if self.shot_dir and cid and any(x in cid for x in ("-01", "-02")):
                        self.shot(page, "%s-%s-390" % (version, cid))
                # 夹具含长展示公式：至少有若干可滚动且可达的容器，证明断言被真实触发
                self.assertGreater(seen_scroller_reachable, 0,
                                   "夹具的长公式应在自身容器内滚动（否则测试没有覆盖该路径）")
                self.assertEqual(self.errors, [])
            finally:
                ctx.close()

    def test_nav02_desktop_no_overflow_each_card(self):
        ctx, page = self.open_page(self._html("student"), 1280, 800)
        try:
            for k in range(len(self.deck["cards"])):
                page.keyboard.press("Home")
                for _ in range(k):
                    page.keyboard.press("ArrowRight")
                m = page.evaluate(_METRICS_JS)
                self.assertLessEqual(m["scrollWidth"], m["innerWidth"] + 1, m.get("cardId"))
                self.assertEqual(m["offenders"], [], "%s: %s" % (m.get("cardId"), m["offenders"][:4]))
        finally:
            ctx.close()

    def test_nav02_long_options_wrap_inside_card(self):
        """长选项按钮自身不超过卡片宽度（可换行或自身滚动，但不能撑出）。

        同时守住两件事：
        1) 夹具里必须真有长选项（无断点长串），否则这条断言会退化成空跑；
        2) 任何内容宽于自身盒子的选项都必须是自身横向滚动容器且末端可达，
           不允许用 overflow:hidden 裁切遮掩（DATA-CONTRACTS C8）。
        """
        ctx, page = self.open_page(self._html("student"), 390, 844)
        try:
            idxs = page.evaluate(
                "Array.from(document.querySelectorAll('.qc-card')).map((c,i)=>c.querySelector('.qc-option')?i:-1).filter(i=>i>=0)")
            self.assertGreaterEqual(len(idxs), 2)
            checked = 0
            longest = 0
            for i in idxs:
                page.keyboard.press("Home")
                for _ in range(i):
                    page.keyboard.press("ArrowRight")
                info = page.evaluate("""() => {
                  const card = Array.from(document.querySelectorAll('.qc-card')).find(c => !c.hidden);
                  const cr = card.getBoundingClientRect();
                  return {
                    cardId: card.id,
                    options: Array.from(card.querySelectorAll('.qc-option')).map(b => {
                      const r = b.getBoundingClientRect();
                      const cs = getComputedStyle(b);
                      const before = b.scrollLeft;
                      b.scrollLeft = 99999;
                      const reach = b.scrollLeft;
                      b.scrollLeft = before;
                      return {right: r.right, left: r.left, cardRight: cr.right,
                              cardLeft: cr.left, w: Math.round(r.width),
                              scrollW: b.scrollWidth, clientW: b.clientWidth,
                              overflowX: cs.overflowX, reach: Math.round(reach),
                              textLen: (b.textContent || '').length,
                              text: (b.textContent || '').slice(0, 24)};
                    })
                  };
                }""")
                cid = info["cardId"]
                opts = info["options"]
                bad = [o for o in opts
                       if o["right"] > o["cardRight"] + 1 or o["left"] < o["cardLeft"] - 1]
                self.assertEqual(bad, [], "%s: 选项按钮越出卡片: %s" % (cid, bad))
                for o in opts:
                    checked += 1
                    longest = max(longest, o["textLen"])
                    if o["scrollW"] <= o["clientW"] + 1:
                        continue          # 内容放得下（已换行），无需滚动
                    self.assertIn(o["overflowX"], ("auto", "scroll"),
                                  "%s: 选项内容超宽却不是自身滚动容器（会被裁切）: %s"
                                  % (cid, o))
                    self.assertGreater(o["reach"], 0,
                                       "%s: 选项横向滚动到末端仍不可达: %s" % (cid, o))
                self.shot(page, "student-options-card%d-390" % (i + 1))
            self.assertGreater(checked, 0)
            # 夹具覆盖：必须存在足够长的选项文本，否则"长选项"路径没被跑到
            self.assertGreater(longest, 40,
                               "夹具缺少长选项（最长选项仅 %d 字），NAV-02 长选项路径未被覆盖"
                               % longest)
        finally:
            ctx.close()

    # ---------------- NAV-03 ----------------
    def test_nav03_paging_keyboard_hash_judge_answers(self):
        n = len(self.deck["cards"])
        cards = self.deck["cards"]
        qi = [i for i, c in enumerate(cards) if c["kind"] == "question"]
        self.assertGreaterEqual(len(qi), 3)
        for vp, w, h in VIEWPORTS:
            # --- 前后页按钮/键盘 ---
            ctx, page = self.open_page(self._html("student"), w, h)
            try:
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[0]["id"]])
                self.assertTrue(page.is_disabled("#qc-btn-prev"))
                page.click("#qc-btn-next")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[1]["id"]])
                page.click("#qc-btn-next2")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[2]["id"]])
                page.click("#qc-btn-prev")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[1]["id"]])
                page.keyboard.press("End")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[-1]["id"]])
                self.assertTrue(page.is_disabled("#qc-btn-next"))
                self.assertTrue(page.is_disabled("#qc-btn-next2"))
                page.keyboard.press("ArrowLeft")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[-2]["id"]])
                page.keyboard.press("Home")
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[0]["id"]])
                # 键盘 t 打开目录、Escape 关闭
                page.keyboard.press("t")
                self.assertTrue(page.evaluate("document.getElementById('qc-toc').classList.contains('qc-open')"))
                page.keyboard.press("Escape")
                self.assertFalse(page.evaluate("document.getElementById('qc-toc').classList.contains('qc-open')"))

                # --- 选项判题（第一题：错选 → 错红 + 正确项提示；仅判一次；重置可重做） ---
                page.keyboard.press("Home")
                for _ in range(qi[0]):
                    page.keyboard.press("ArrowRight")
                card_sel = "#qc-card-%s" % cards[qi[0]]["id"]
                opts = page.locator(card_sel + " .qc-options").first.locator(".qc-option")
                self.assertGreaterEqual(opts.count(), 3)
                correct_idx = [i for i in range(opts.count())
                               if opts.nth(i).get_attribute("data-correct") == "true"]
                self.assertEqual(len(correct_idx), 1)
                wrong_idx = [i for i in range(opts.count()) if i != correct_idx[0]][0]
                opts.nth(wrong_idx).click()
                self.assertIn("qc-wrong", opts.nth(wrong_idx).get_attribute("class"))
                self.assertIn("qc-reveal-correct", opts.nth(correct_idx[0]).get_attribute("class"))
                # 已判定后再点正确项不再改变
                opts.nth(correct_idx[0]).click()
                self.assertNotIn(" qc-correct", " " + opts.nth(correct_idx[0]).get_attribute("class"))
                score = page.inner_text(card_sel + " .qc-score b")
                self.assertEqual(score, "1 / 1")
                page.click(card_sel + " .qc-reset")
                self.assertEqual(page.inner_text(card_sel + " .qc-score b"), "0 / 1")
                opts.nth(correct_idx[0]).click()
                self.assertIn("qc-correct", opts.nth(correct_idx[0]).get_attribute("class"))

                # --- 学生答案默认隐藏，按钮可展开/收起 ---
                sol = page.locator(card_sel + " .qc-reveal")
                self.assertTrue(sol.first.get_attribute("hidden") is not None)
                self.assertEqual(page.inner_text("#qc-btn-answer"), "显示答案")
                page.click("#qc-btn-answer")
                self.assertTrue(sol.first.is_visible())
                self.assertEqual(page.inner_text("#qc-btn-answer"), "隐藏答案")
                page.click("#qc-btn-answer")
                self.assertFalse(sol.first.is_visible())
                self.assertEqual(self.errors, [])
            finally:
                ctx.close()

            # --- URL hash 定位（#/<cid> 外链含义保持）；非法 hash 回到首页 ---
            target = cards[qi[1]]["id"]
            ctx, page = self.open_page(self._html("student"), w, h, hash_="#/" + target)
            try:
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + target])
                self.assertEqual(page.evaluate("location.hash"), "#/" + target)
            finally:
                ctx.close()
            ctx, page = self.open_page(self._html("student"), w, h, hash_="#/no-such-card")
            try:
                self.assertEqual(self.visible_card_ids(page), ["qc-card-" + cards[0]["id"]])
            finally:
                ctx.close()

            # --- 教师版默认展开解答 ---
            ctx, page = self.open_page(self._html("teacher"), w, h, hash_="#/" + cards[qi[0]]["id"])
            try:
                card_sel = "#qc-card-%s" % cards[qi[0]]["id"]
                self.assertTrue(page.locator(card_sel + " .qc-solution").first.is_visible())
                self.assertEqual(page.inner_text("#qc-btn-answer"), "隐藏答案")
                self.assertEqual(self.errors, [])
            finally:
                ctx.close()


class NavigationRealCardsRegression(_BrowserCase):
    """审查中三张溢出卡（sec2-07/sec3-07/sec3-08）的只读回归：
    从真实项目 cards.json 用候选渲染器渲染到临时目录（不写项目）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        default = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", "..", "cards.json"))
        cls.real = os.environ.get("QC_NAV_REAL_CARDS") or default
        if not os.path.exists(cls.real):
            cls.tearDownClass()
            raise unittest.SkipTest("真实项目 cards.json 不可用（只读回归跳过）: %s" % cls.real)

    def test_real_three_overflow_cards_390(self):
        with open(self.real, encoding="utf-8") as f:
            deck = json.load(f)
        real_proj = os.path.dirname(self.real)
        out = os.path.join(self.tmp, "real")
        os.makedirs(out)
        ok, msg = render_html.render_version(deck, real_proj, "student",
                                             os.path.join(out, "student.html"))
        self.assertTrue(ok, msg)
        ids = [c["id"] for c in deck["cards"]]
        ctx, page = self.open_page(os.path.join(out, "student.html"), 390, 844)
        try:
            # 全部目录点击都命中
            gotos = page.evaluate(
                "Array.from(document.querySelectorAll('.qc-toc-item')).map(a=>a.getAttribute('data-goto'))")
            self.assertEqual(len(gotos), len(ids))
            for k in reversed(range(len(gotos))):
                page.click("#qc-btn-toc")
                page.click('.qc-toc-item[data-goto="%s"]' % gotos[k])
                self.assertEqual(self.visible_card_ids(page), [gotos[k]])
            # 逐卡无整页溢出（含 3 张曾溢出的卡）
            for k, cid in enumerate(ids):
                page.keyboard.press("Home")
                for _ in range(k):
                    page.keyboard.press("ArrowRight")
                m = page.evaluate(_METRICS_JS)
                self.assertLessEqual(m["scrollWidth"], m["innerWidth"] + 1,
                                     "真实卡 %s 溢出: %s" % (cid, m["offenders"][:4]))
                self.assertEqual(m["offenders"], [], "真实卡 %s: %s" % (cid, m["offenders"][:4]))
                self.assertEqual(m["clipped"], [])
                for s in m["scrollers"]:
                    if s["scrollable"]:
                        self.assertTrue(s.get("reachable"), "真实卡 %s 滚动内容不可达 %s" % (cid, s))
                if cid in ("sec2-07", "sec3-07", "sec3-08"):
                    self.shot(page, "real-%s-390" % cid)
            for need in ("sec2-07", "sec3-07", "sec3-08"):
                self.assertIn(need, ids)
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main()
