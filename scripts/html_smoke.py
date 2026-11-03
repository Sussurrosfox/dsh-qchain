#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""html_smoke.py — 发布切换前的 HTML 离线交付包浏览器验收（契约 C8）

职责（与 render_smoke.py 的 PDF 视觉冒烟是两件不同的事）：
  对**将要交付的实际包目录**（发布流程中的 ``<self>/.release-staging``，
  或已经切换完成的 ``<self>/release``）用本机真实 Chrome/Chromium 打开
  ``student.html`` / ``teacher.html``（``file://``，阻断全部 http/https），
  逐卡在 desktop 与 390px 两种视口验收：

    RES  资源：KaTeX JS/CSS/字体、图片、共享数学配置全部从包内加载成功
         （任何 file:// 请求失败都记证据并判失败；外网请求被阻断即失败）
    MATH 数学：复用渲染层的显式状态（data-math-status / window.__qcMath），
         逐节点 ok/error 统计——无公式的合法卡片允许零公式，公式存在但
         零渲染必须失败；坏宏（throwOnError）必然失败
    NAV  交互：目录条目 data-goto 命中唯一卡片，逐条点击后显示正确目标
    OPT  选项：可见卡片的选项组点击后进入已作答状态（判题可用）
    LAY  布局：390px 下整页/整卡不横向溢出，且不得用 overflow:hidden
         掩盖裁切（长公式只允许在自身容器内横向滚动）

退出码：0 = 通过；1 = 包有缺陷（资源/数学/交互/布局）；3 = 环境不可用
（缺 playwright 或无可用浏览器）——**3 绝不等于通过**，发布路径必须把
它当成"无法验收"而非"验收通过"。

用法：
  python scripts/html_smoke.py <package_dir>            # 直接验收任意包目录
  python scripts/html_smoke.py <package_dir> --json r.json
  python scripts/html_smoke.py <package_dir> --shots .pd/review/html-smoke
可选环境变量：
  QC_HTML_SMOKE_BROWSER  指定 Chrome/Chromium 可执行文件（覆盖自动探测）
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TOOL_NAME = "html_smoke.py"
TOOL_VERSION = "1.0"
SCHEMA_VERSION = 2

DEFAULT_REPORT_REL = os.path.join(".pd", "reports", "html_smoke.json")
SHOT_REL = os.path.join(".pd", "review", "html-smoke")

VERSIONS = ("student", "teacher")
VIEWPORTS = (("desktop", 1280, 800), ("narrow390", 390, 844))

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED_ENV = 3

BROWSER_ENV = "QC_HTML_SMOKE_BROWSER"

# 本机可配置浏览器：先看显式路径/环境变量，再按平台常见安装位置探测，
# 最后退回 Playwright 自带 channel（chrome/msedge/chromium）。
BROWSER_CANDIDATES = (
    ("exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("exe", os.path.expanduser(
        r"~\AppData\Local\Google\Chrome\Application\chrome.exe")),
    ("exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("exe", "/usr/bin/google-chrome"),
    ("exe", "/usr/bin/chromium"),
    ("exe", "/usr/bin/chromium-browser"),
    ("channel", "chrome"),
    ("channel", "msedge"),
    ("channel", "chromium"),
)

INSTALL_HINT = (
    "安装依赖：pip install playwright && python -m playwright install chromium；"
    "或把本机 Chrome/Chromium 可执行文件路径写入环境变量 %s" % BROWSER_ENV
)


class SmokeUnavailable(Exception):
    """环境不可用（缺 playwright / 无可用浏览器）——BLOCKED_ENV，不是 PASS。"""

    def __init__(self, reason, diagnostics=None):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics or []


# --------------------------------------------------------------------------
# 浏览器探测
# --------------------------------------------------------------------------
def browser_candidates(browser_path=None):
    """返回候选清单 [(kind, value)]：显式路径 > 环境变量 > 内置探测。"""
    out = []
    explicit = browser_path or os.environ.get(BROWSER_ENV) or ""
    explicit = explicit.strip()
    if explicit:
        out.append(("exe", explicit))
    for item in BROWSER_CANDIDATES:
        if item not in out:
            out.append(item)
    return tuple(out)


def import_playwright():
    """导入 playwright；缺失时抛 SmokeUnavailable（附安装指引）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise SmokeUnavailable(
            "未安装 playwright(Python)：%s" % str(exc)[:160], [INSTALL_HINT])
    return sync_playwright


def find_browser(pw, browser_path=None, candidates=None):
    """启动第一个可用浏览器。返回 (browser|None, 描述, 诊断清单)。"""
    errors = []
    for kind, val in (candidates if candidates is not None
                      else browser_candidates(browser_path)):
        if not val:
            continue
        if kind == "exe":
            if not os.path.exists(val):
                errors.append("exe 不存在: %s" % val)
                continue
            try:
                return (pw.chromium.launch(executable_path=val, headless=True),
                        val, errors)
            except Exception as exc:  # noqa: BLE001
                errors.append("exe 启动失败 %s: %s" % (val, str(exc)[:140]))
                continue
        try:
            return (pw.chromium.launch(channel=val, headless=True),
                    "channel:" + val, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append("channel 启动失败 %s: %s" % (val, str(exc)[:140]))
    return None, None, errors


def browser_diagnostic(errors):
    """把探测失败清单拼成人类可读的依赖诊断。"""
    detail = "; ".join(errors) if errors else "未找到任何 Chrome/Chromium/Edge"
    return "无可用浏览器（HTML 离线包验收无法进行）：%s。%s" % (detail, INSTALL_HINT)


# --------------------------------------------------------------------------
# 页面内量取脚本
# --------------------------------------------------------------------------
# 资源 / 数学 / 交互结构的一次性快照
PAGE_STATE_JS = r"""
() => {
  const qc = window.__qcMath || null;
  const nodes = Array.from(document.querySelectorAll('.qc-math'));
  const status = nodes.map(n => n.getAttribute('data-math-status'));
  const imgs = Array.from(document.images).map(i => ({
    src: i.getAttribute('src') || '',
    complete: !!i.complete,
    naturalWidth: i.naturalWidth || 0,
    inCard: !!i.closest('.qc-card')
  }));
  // 字体是"随包资源"里最容易漏的一类：CSS/JS 都在、字体没进包时公式会静默
  // 回退到系统字体（页面上不报错），因此必须直接读 FontFace 状态。
  const fonts = [];
  try {
    document.fonts.forEach(f => fonts.push({family: f.family, status: f.status}));
  } catch (e) { /* 老引擎无 FontFaceSet */ }
  return {
    hasMathReport: document.documentElement.getAttribute('data-math-report') !== null,
    mathReport: document.documentElement.getAttribute('data-math-report'),
    engine: !!(window.katex && typeof window.katex.render === 'function'),
    hasMathConfig: !!document.getElementById('qc-math-config'),
    mathNodes: nodes.length,
    mathOk: status.filter(s => s === 'ok').length,
    mathError: status.filter(s => s === 'error').length,
    mathUnmarked: status.filter(s => !s).length,
    mathIds: nodes.map(n => n.getAttribute('data-math-id')),
    katexRendered: document.querySelectorAll('.katex').length,
    mathErrors: qc ? (qc.errors || []).slice(0, 20) : [],
    qcMathTotals: qc ? {total: qc.total, ok: qc.ok, error: qc.error} : null,
    images: imgs,
    fonts: fonts,
    cards: document.querySelectorAll('.qc-card').length,
    tocItems: document.querySelectorAll('.qc-toc-item').length,
    optionGroups: document.querySelectorAll('.qc-options').length,
    nokatex: document.documentElement.classList.contains('qc-nokatex')
  };
}
"""

# file:// 下 cssRules 会被判为跨域（SecurityError），因此用"计算样式里是否真的
# 用上了 KaTeX 字体"来证明随包 katex.min.css 已生效——这比数规则条数可靠。
CSS_PROBE_JS = r"""
() => {
  const el = document.querySelector('.katex');
  const out = {katex: document.querySelectorAll('.katex').length,
               fontFamily: '', hasLink: false};
  const link = Array.from(document.querySelectorAll('link[rel~="stylesheet"]'))
      .find(l => (l.getAttribute('href') || '').indexOf('katex') >= 0);
  out.hasLink = !!link;
  if (el) out.fontFamily = getComputedStyle(el).fontFamily || '';
  return out;
}
"""

# 卡片用 hidden 隐藏、图片带 loading="lazy"：先强制 eager 再等全部解码完成，
# 否则未翻到的卡片里的图片 naturalWidth 恒为 0（误判成坏图）。
FORCE_EAGER_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('img[loading="lazy"]').forEach(i => {
    i.loading = 'eager'; n += 1;
  });
  return n;
}
"""

IMAGES_READY_JS = r"""
() => Array.from(document.images).every(i => i.complete)
"""

# 字体在 Chromium 里是"用到才加载"的：只读 FontFace 状态会全是 unloaded，
# 既证明不了随包字体可用、也发现不了缺字体。必须显式 load() 一遍 KaTeX 家族，
# 让每个 @font-face 真正去取文件——缺文件的那一族状态会变成 error。
FONT_PROBE_JS = r"""
async () => {
  const names = ['KaTeX_Main', 'KaTeX_Math', 'KaTeX_AMS', 'KaTeX_Caligraphic',
                 'KaTeX_Fraktur', 'KaTeX_SansSerif', 'KaTeX_Script',
                 'KaTeX_Size1', 'KaTeX_Size2', 'KaTeX_Size3', 'KaTeX_Size4',
                 'KaTeX_Typewriter'];
  for (const n of names) {
    try { await document.fonts.load('16px ' + n); } catch (e) { /* 忽略单族异常 */ }
  }
  const faces = [];
  document.fonts.forEach(f => faces.push({family: f.family, status: f.status}));
  return faces;
}
"""

# 当前可见卡片的横向溢出 / 裁切量取
LAYOUT_JS = r"""
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const res = {innerWidth: vw, scrollWidth: doc.scrollWidth,
               bodyScrollWidth: document.body.scrollWidth,
               cardId: null, offenders: [], hiddenOverflow: [], clipped: []};
  const card = Array.from(document.querySelectorAll('.qc-card')).find(c => !c.hidden);
  if (!card) { res.noVisibleCard = true; return res; }
  res.cardId = card.id;
  const cr = card.getBoundingClientRect();
  for (const el of [doc, document.body, document.getElementById('qc-stage'),
                    card, card.querySelector('.qc-card-body')]) {
    if (!el) continue;
    const ox = getComputedStyle(el).overflowX;
    if (ox === 'hidden' || ox === 'clip') {
      const name = (el.id || el.className || el.tagName) + ':' + ox;
      res.hiddenOverflow.push(name);
      if (el.scrollWidth > el.clientWidth + 1) res.clipped.push(name);
    }
  }
  function clipAncestor(el) {
    for (let n = el; n && n !== card; n = n.parentElement) {
      if (getComputedStyle(n).overflowX !== 'visible') return n;
    }
    return null;
  }
  for (const el of card.querySelectorAll('*')) {
    if (el.closest('.katex-mathml')) continue;   // 屏幕阅读器用，视觉隐藏
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (clipAncestor(el)) continue;              // 由内层滚动容器负责
    if (r.right > cr.right + 1 || r.right > vw + 1) {
      res.offenders.push({tag: el.tagName,
                          cls: String(el.className).slice(0, 40),
                          right: Math.round(r.right),
                          text: (el.textContent || '').slice(0, 24)});
    }
  }
  return res;
}
"""

VISIBLE_CARDS_JS = ("Array.from(document.querySelectorAll('.qc-card'))"
                    ".filter(c => !c.hidden).map(c => c.id)")

TOC_GOTOS_JS = ("Array.from(document.querySelectorAll('.qc-toc-item'))"
                ".map(a => a.getAttribute('data-goto'))")

CARD_IDS_JS = "Array.from(document.querySelectorAll('.qc-card')).map(c => c.id)"

OPTION_CLICK_JS = r"""
() => {
  const card = Array.from(document.querySelectorAll('.qc-card')).find(c => !c.hidden);
  if (!card) return {ok: false, reason: 'no-visible-card'};
  const group = card.querySelector('.qc-options');
  if (!group) return {ok: true, skipped: true};
  const opt = group.querySelector('.qc-option');
  if (!opt) return {ok: false, reason: 'empty-option-group'};
  opt.click();
  const answered = group.getAttribute('data-answered') === '1'
      || group.classList.contains('qc-answered')
      || !!group.querySelector('.qc-correct, .qc-wrong');
  return {ok: answered, answered: answered,
          groups: card.querySelectorAll('.qc-options').length};
}
"""


def _file_url(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p


def _issue(message, severity="error"):
    return {"severity": severity, "message": message}


def package_digest(package_dir):
    """包内文件数与 manifest 摘要（证据用；不参与判定）。"""
    import hashlib
    n = 0
    man_sha = None
    for base, _dirs, names in os.walk(package_dir):
        for name in names:
            p = os.path.join(base, name)
            n += 1
            if name == "manifest.json" and base == package_dir:
                try:
                    with open(p, "rb") as f:
                        man_sha = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    man_sha = None
    return n, man_sha


def _new_report(package_dir):
    file_count, man_sha = (0, None)
    if os.path.isdir(package_dir):
        file_count, man_sha = package_digest(package_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": {"package": package_dir,
                  "manifest_sha256": man_sha,
                  "file_count": file_count},
        "status": "fail",
        "issues": [],
        "evidence": {
            "package": package_dir,
            "manifest_sha256": man_sha,
            "file_count": file_count,
            "network": "已阻断全部 http/https/ws 请求（只允许 file://）",
            "browser": None,
            "viewports": ["%s:%dx%d" % vp for vp in VIEWPORTS],
            "pages": {},
            "counts": {},
            "screenshots": None,
        },
    }


def _write_report(report, report_path):
    if not report_path:
        return
    parent = os.path.dirname(os.path.abspath(report_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _finalize(report, report_path, exit_code, status):
    report["status"] = status
    _write_report(report, report_path)
    return report, exit_code


# --------------------------------------------------------------------------
# 单页验收
# --------------------------------------------------------------------------
def _check_page(browser, html_path, version, vp_name, width, height,
                timeout_ms, shot_dir=None):
    """打开一个包内 HTML，在指定视口做资源/数学/交互/布局验收。

    返回 (evidence dict, issues list)。
    """
    evidence = {"html": os.path.basename(html_path), "viewport": vp_name,
                "size": "%dx%d" % (width, height)}
    issues = []
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    blocked, failed, pageerrors, console_errors = [], [], [], []

    def _route(route):
        blocked.append(route.request.url)
        route.abort()

    page.route(re.compile(r"^(?:https?|wss?)://"), _route)
    page.on("requestfailed",
            lambda req: failed.append(
                {"url": req.url, "error": (req.failure or "")}))
    page.on("pageerror", lambda e: pageerrors.append(str(e)))
    page.on("console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None)

    tag = "%s/%s" % (version, vp_name)
    try:
        try:
            page.goto(_file_url(html_path), wait_until="load")
            page.wait_for_function(
                "document.documentElement.getAttribute('data-math-report') !== null",
                timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            issues.append(_issue(
                "%s: 渲染脚本未完成（等待 data-math-report 超时/%s）：页面 JS 未跑通"
                "（KaTeX 或导航脚本异常），不得判为可用" % (tag, str(exc)[:120])))
        try:
            page.evaluate("() => (document.fonts && document.fonts.ready)"
                          " ? document.fonts.ready.then(() => true) : true")
        except Exception:  # noqa: BLE001
            pass
        # 卡片用 hidden 隐藏 + 图片 loading="lazy"：强制 eager 后等全部解码完成，
        # 否则未翻到的卡片里的图片 naturalWidth 恒为 0，会被误判成坏图。
        try:
            page.evaluate(FORCE_EAGER_JS)
            page.wait_for_function(IMAGES_READY_JS, timeout=min(timeout_ms, 8000))
        except Exception:  # noqa: BLE001
            pass
        # 显式加载 KaTeX 各字体家族：缺字体文件时状态会变 error（否则永远
        # 停在 unloaded，缺字体就查不出来——页面不会报错，只会静默回退）
        try:
            page.evaluate(FONT_PROBE_JS)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)

        state = page.evaluate(PAGE_STATE_JS)
        css = page.evaluate(CSS_PROBE_JS)
        evidence["state"] = {
            "cards": state["cards"], "toc_items": state["tocItems"],
            "math_nodes": state["mathNodes"], "math_ok": state["mathOk"],
            "math_error": state["mathError"], "math_unmarked": state["mathUnmarked"],
            "math_report": state["mathReport"], "katex_rendered": state["katexRendered"],
            "option_groups": state["optionGroups"],
            "engine": state["engine"],
            "math_ids_unique": len(set(state["mathIds"])) == len(state["mathIds"]),
            "math_errors": state["mathErrors"],
            "katex_css": {"has_link": css.get("hasLink"),
                          "font_family": css.get("fontFamily")},
            "images": state["images"],
        }

        # ---- RES：资源 ----
        internal_failed = [f for f in failed
                           if not re.match(r"^(?:https?|wss?)://", f["url"])]
        if blocked:
            issues.append(_issue(
                "%s: 包内页面请求了外网资源 %d 个（离线交付包必须自足）：%s"
                % (tag, len(blocked), blocked[:5])))
        if internal_failed:
            issues.append(_issue(
                "%s: 包内资源加载失败 %d 个（缺失/损坏）：%s"
                % (tag, len(internal_failed),
                   [f["url"] for f in internal_failed][:5])))
        broken_imgs = [i for i in state["images"]
                       if i["src"] and (not i["complete"] or i["naturalWidth"] <= 0)]
        if broken_imgs:
            issues.append(_issue(
                "%s: 图片未成功渲染 %d 张：%s"
                % (tag, len(broken_imgs), [i["src"] for i in broken_imgs][:5])))
        katex_css_ok = bool(css.get("hasLink"))
        if state["mathNodes"] and state["katexRendered"] and \
                "KaTeX" not in (css.get("fontFamily") or ""):
            katex_css_ok = False
        if state["mathNodes"] and not katex_css_ok:
            issues.append(_issue(
                "%s: 包内 KaTeX 样式表未生效（katex.min.css 缺失/未应用，"
                "公式会显示为裸文本）" % tag))
        if not state["hasMathConfig"]:
            issues.append(_issue(
                "%s: 缺少内嵌共享数学配置 #qc-math-config（file:// 下不得 fetch）"
                % tag))

        # ---- FONT：随包字体必须真的加载成功（缺字体不会报错，只会静默回退）----
        fonts = state.get("fonts") or []
        bad_fonts = [f for f in fonts if (f.get("status") == "error")]
        loaded_fonts = [f for f in fonts if (f.get("status") == "loaded")]
        evidence["state"]["fonts"] = {
            "declared": len(fonts), "loaded": len(loaded_fonts),
            "error": len(bad_fonts),
            "error_families": sorted({f.get("family") for f in bad_fonts})[:8],
        }
        if bad_fonts:
            issues.append(_issue(
                "%s: 包内字体加载失败 %d 个（KaTeX 字体未随包落地/文件损坏）：%s"
                % (tag, len(bad_fonts),
                   sorted({f.get("family") for f in bad_fonts})[:5])))
        elif state["mathNodes"] and state["katexRendered"] and not loaded_fonts:
            issues.append(_issue(
                "%s: 公式已渲染但没有任何随包字体进入 loaded 状态"
                "（字体资源可能未入包，浏览器回退到系统字体）" % tag))

        # ---- MATH：显式成功/失败状态（契约 C8）----
        if state["mathNodes"] and not state["engine"]:
            issues.append(_issue(
                "%s: 包内 KaTeX 引擎未加载（katex.min.js 缺失）：%d 个公式无法渲染"
                % (tag, state["mathNodes"])))
        if state["mathNodes"] and state["mathOk"] == 0:
            issues.append(_issue(
                "%s: 公式存在但零渲染（%d 个 qc-math 节点无一成功）"
                % (tag, state["mathNodes"])))
        if state["mathError"]:
            issues.append(_issue(
                "%s: %d 个公式节点渲染失败（data-math-status=error）：%s"
                % (tag, state["mathError"], state["mathErrors"][:5])))
        if state["mathUnmarked"]:
            issues.append(_issue(
                "%s: %d 个公式节点没有显式渲染状态（data-math-status 缺失）"
                % (tag, state["mathUnmarked"])))
        if state["mathReport"] != "ok":
            issues.append(_issue(
                "%s: 页面级数学状态 data-math-report=%r（应为 ok）"
                % (tag, state["mathReport"])))
        if not state["cards"]:
            issues.append(_issue(
                "%s: 包内 HTML 没有任何 qc-card 卡片（不是可交付的牌组）" % tag))
        if state["tocItems"] != state["cards"]:
            issues.append(_issue(
                "%s: 目录条目 %d 个与卡片 %d 张不一致（目录不可用）"
                % (tag, state["tocItems"], state["cards"])))

        # ---- NAV：目录点击 ----
        goto = page.evaluate(TOC_GOTOS_JS)
        card_ids = page.evaluate(CARD_IDS_JS)
        bad_goto = [g for g in goto if card_ids.count(g) != 1]
        if bad_goto:
            issues.append(_issue(
                "%s: %d 个目录条目 data-goto 未命中唯一卡片：%s"
                % (tag, len(bad_goto), bad_goto[:5])))
        toc_checked = 0
        if not bad_goto and goto:
            for k in reversed(range(len(goto))):
                try:
                    page.click("#qc-btn-toc")
                    page.wait_for_selector("#qc-toc.qc-open", timeout=5000)
                    page.click('.qc-toc-item[data-goto="%s"]' % goto[k])
                except Exception as exc:  # noqa: BLE001
                    issues.append(_issue(
                        "%s: 点击目录第 %d 项失败：%s" % (tag, k + 1, str(exc)[:120])))
                    continue
                vis = page.evaluate(VISIBLE_CARDS_JS)
                if vis != [goto[k]]:
                    issues.append(_issue(
                        "%s: 点击目录第 %d 项后显示 %r，期望 %r"
                        % (tag, k + 1, vis, [goto[k]])))
                toc_checked += 1
        evidence["toc_checked"] = toc_checked

        # ---- LAY + OPT：逐卡布局与选项可用 ----
        per_card = []
        offenders = 0
        opt_groups_checked = 0
        for k in range(state["cards"]):
            page.keyboard.press("Home")
            for _ in range(k):
                page.keyboard.press("ArrowRight")
            m = page.evaluate(LAYOUT_JS)
            if m.get("noVisibleCard"):
                issues.append(_issue("%s: 翻到第 %d 张卡片时没有可见卡片" % (tag, k + 1)))
                break
            rec = {"card": m.get("cardId"),
                   "scrollWidth": m["scrollWidth"],
                   "innerWidth": m["innerWidth"],
                   "offenders": m["offenders"][:3]}
            per_card.append(rec)
            if m["scrollWidth"] > m["innerWidth"] + 1:
                issues.append(_issue(
                    "%s 卡 %s: 页面横向溢出 scrollWidth=%s > innerWidth=%s；越界元素=%s"
                    % (tag, m.get("cardId"), m["scrollWidth"], m["innerWidth"],
                       m["offenders"][:3])))
            if m["offenders"]:
                offenders += 1
                issues.append(_issue(
                    "%s 卡 %s: %d 个元素越出卡片/视口且无滚动容器：%s"
                    % (tag, m.get("cardId"), len(m["offenders"]),
                       m["offenders"][:3])))
            if m["hiddenOverflow"]:
                issues.append(_issue(
                    "%s 卡 %s: 用 overflow:hidden 掩盖横向内容（禁止）：%s"
                    % (tag, m.get("cardId"), m["hiddenOverflow"])))
            if m["clipped"]:
                issues.append(_issue(
                    "%s 卡 %s: 存在被 overflow:hidden 裁掉的内容：%s"
                    % (tag, m.get("cardId"), m["clipped"])))
            # 选项可用性在两种视口都验（390px 下选项可能换行/溢出）
            r = page.evaluate(OPTION_CLICK_JS)
            if not r.get("ok"):
                issues.append(_issue(
                    "%s 卡 %s: 选项组点击后未进入已作答状态（%s）"
                    % (tag, m.get("cardId"), r.get("reason"))))
            elif not r.get("skipped"):
                opt_groups_checked += 1
        evidence["cards_checked"] = len(per_card)
        evidence["cards"] = per_card
        evidence["overflow_cards"] = offenders
        evidence["option_groups_checked"] = opt_groups_checked

        if pageerrors:
            issues.append(_issue("%s: 页面 JS 异常 %d 条：%s"
                                 % (tag, len(pageerrors), pageerrors[:5])))
        if console_errors:
            issues.append(_issue("%s: 控制台 error %d 条：%s"
                                 % (tag, len(console_errors), console_errors[:5])))
        evidence["pageerrors"] = pageerrors[:10]
        evidence["console_errors"] = console_errors[:10]
        evidence["blocked_requests"] = blocked[:10]
        evidence["failed_requests"] = [f["url"] for f in internal_failed][:10]

        if shot_dir:
            try:
                os.makedirs(shot_dir, exist_ok=True)
                path = os.path.join(shot_dir, "%s-%s.png" % (version, vp_name))
                page.screenshot(path=path, full_page=False)
                evidence["screenshot"] = path
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    return evidence, issues


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def run_smoke(package_dir, report_path=None, versions=None, viewports=None,
              browser_path=None, candidates=None, shot_dir=None,
              timeout_ms=15000):
    """对实际交付包目录做 HTML 冒烟。返回 (report dict, exit_code)。"""
    pkg = os.path.abspath(package_dir)
    versions = tuple(versions or VERSIONS)
    viewports = tuple(viewports or VIEWPORTS)
    report = _new_report(pkg)
    report["evidence"]["viewports"] = ["%s:%dx%d" % vp for vp in viewports]
    issues = []

    if not os.path.isdir(pkg):
        issues.append(_issue("HTML 冒烟目标目录不存在: %s" % pkg))
        report["issues"] = issues
        return _finalize(report, report_path, EXIT_FAIL, "fail")

    present = [v for v in versions
               if os.path.isfile(os.path.join(pkg, v + ".html"))]
    missing = [v for v in versions if v not in present]
    if missing:
        issues.append(_issue(
            "包内缺少 %s.html：HTML 冒烟必须验收实际交付的每一版页面"
            % "/".join(missing)))
    if not present:
        issues.append(_issue(
            "包内没有任何 HTML 产物：无从验收，发布不得在缺少 HTML 证据时放行"))
        report["issues"] = issues
        return _finalize(report, report_path, EXIT_FAIL, "fail")

    try:
        sync_playwright = import_playwright()
    except SmokeUnavailable as exc:
        report["evidence"]["dependency"] = {
            "status": "blocked_env", "reason": exc.reason,
            "hints": exc.diagnostics}
        issues.append(_issue("BLOCKED_ENV: %s" % exc.reason))
        report["issues"] = issues
        return _finalize(report, report_path, EXIT_BLOCKED_ENV, "blocked_env")

    pw = sync_playwright().start()
    browser = None
    try:
        browser, desc, diag = find_browser(pw, browser_path=browser_path,
                                          candidates=candidates)
        if browser is None:
            reason = browser_diagnostic(diag)
            report["evidence"]["dependency"] = {
                "status": "blocked_env", "reason": reason,
                "tried": [v for _k, v in (candidates if candidates is not None
                                          else browser_candidates(browser_path))],
                "errors": diag, "hints": INSTALL_HINT}
            issues.append(_issue("BLOCKED_ENV: %s" % reason))
            report["issues"] = issues
            return _finalize(report, report_path, EXIT_BLOCKED_ENV,
                             "blocked_env")
        report["evidence"]["browser"] = desc
        if diag:
            report["evidence"]["browser_fallbacks"] = diag
        for version in present:
            for vp_name, width, height in viewports:
                ev, page_issues = _check_page(
                    browser, os.path.join(pkg, version + ".html"), version,
                    vp_name, width, height, timeout_ms, shot_dir=shot_dir)
                report["evidence"]["pages"]["%s/%s" % (version, vp_name)] = ev
                issues.extend(page_issues)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # 汇总统计（证据可复核：资源/数学/交互）
    counts = {"math_nodes": 0, "math_ok": 0, "math_error": 0, "cards": 0,
              "toc_clicked": 0, "option_groups_clicked": 0,
              "images": 0, "images_broken": 0, "pages": 0}
    for key, ev in report["evidence"]["pages"].items():
        st = ev.get("state") or {}
        counts["pages"] += 1
        counts["math_nodes"] = max(counts["math_nodes"], st.get("math_nodes", 0))
        counts["math_ok"] = max(counts["math_ok"], st.get("math_ok", 0))
        counts["math_error"] += st.get("math_error", 0)
        counts["cards"] = max(counts["cards"], st.get("cards", 0))
        counts["toc_clicked"] += ev.get("toc_checked", 0)
        counts["option_groups_clicked"] += ev.get("option_groups_checked", 0)
        counts["images"] = max(counts["images"],
                               len((st.get("images") or [])))
        counts["images_broken"] += len([i for i in (st.get("images") or [])
                                        if i.get("src") and
                                        (not i.get("complete") or
                                         i.get("naturalWidth", 0) <= 0)])
    report["evidence"]["counts"] = counts
    report["evidence"]["screenshots"] = shot_dir
    report["issues"] = issues
    status = "pass" if not issues else "fail"
    return _finalize(report, report_path,
                     EXIT_PASS if not issues else EXIT_FAIL, status)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="HTML 离线交付包浏览器验收（发布切换前，file:// 断网）")
    ap.add_argument("package", help="待验收的包目录（.release-staging 或 release/）")
    ap.add_argument("--report", default=None,
                    help="证据报告路径（缺省 <package>/../%s）"
                    % DEFAULT_REPORT_REL.replace(os.sep, "/"))
    ap.add_argument("--json", default=None, help="同 --report（兼容习惯）")
    ap.add_argument("--shots", default=None, help="截图目录（可选）")
    ap.add_argument("--browser", default=None,
                    help="Chrome/Chromium 可执行文件路径（覆盖自动探测）")
    ap.add_argument("--versions", default=None,
                    help="逗号分隔的版本列表（缺省 student,teacher）")
    ap.add_argument("--timeout-ms", type=int, default=15000)
    ap.add_argument("--list-browsers", action="store_true",
                    help="只打印浏览器探测结果，不验收")
    args = ap.parse_args(argv)

    if args.list_browsers:
        try:
            sync_playwright = import_playwright()
        except SmokeUnavailable as exc:
            print("[html_smoke] BLOCKED_ENV: %s" % exc.reason)
            for h in exc.diagnostics:
                print("  - " + h)
            return EXIT_BLOCKED_ENV
        pw = sync_playwright().start()
        try:
            browser, desc, diag = find_browser(pw, browser_path=args.browser)
            if browser is None:
                print("[html_smoke] BLOCKED_ENV: %s" % browser_diagnostic(diag))
                return EXIT_BLOCKED_ENV
            browser.close()
            print("[html_smoke] 可用浏览器: %s" % desc)
            for d in diag:
                print("  (fallback) " + d)
            return EXIT_PASS
        finally:
            pw.stop()

    pkg = os.path.abspath(args.package)
    report_path = args.report or args.json
    if not report_path:
        parent = os.path.dirname(pkg)
        report_path = os.path.join(parent, DEFAULT_REPORT_REL)
    versions = None
    if args.versions:
        versions = tuple(v.strip() for v in args.versions.split(",") if v.strip())
    report, code = run_smoke(pkg, report_path=report_path, versions=versions,
                             browser_path=args.browser, shot_dir=args.shots,
                             timeout_ms=args.timeout_ms)
    print("[html_smoke] 包目录: %s" % pkg)
    print("[html_smoke] 状态: %s（退出码 %d）" % (report["status"], code))
    counts = report["evidence"].get("counts") or {}
    if counts:
        print("[html_smoke] 统计: 卡片 %(cards)d / 公式节点 %(math_nodes)d"
              "（ok %(math_ok)d, error %(math_error)d）/ 目录点击 %(toc_clicked)d"
              " / 选项组点击 %(option_groups_clicked)d / 图片 %(images)d"
              "（坏 %(images_broken)d）" % counts)
    for it in report["issues"][:20]:
        print("  - " + it["message"])
    print("[html_smoke] 证据: %s" % report_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
