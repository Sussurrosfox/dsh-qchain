#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""render_html.py — 卡片牌组 → HTML 一页一卡交付渲染器

用 templates/html/deck.template.html 填充 cards.json，生成自包含 HTML：
  student.html  学生版：参考解答收入隐藏揭示区（按钮/键盘揭示），
                无教学要点、无题型/难度标签、教师专属对白默认隐藏；
  teacher.html  教师版：参考解答默认可见（可一键隐藏），
                含教学要点、题型/难度/练习模式标签与教师专属对白。

组件 → 页面映射（填写规范见 references/html-interface.md）：
  每张卡片渲染为一个 <section class="qc-card">，即 HTML 的一页；
  JS 一次只显示一张卡，配合翻页按钮 / 目录 / 键盘 / URL hash 导航。

用法:
  python render_html.py <project> --version student|teacher --out <html路径>
  python render_html.py <project> --version both --outdir <目录>
退出码: 0=成功, 2=输入/模版缺失或渲染失败。
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import asset_bundle  # noqa: E402
import math_fragments  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_TEMPLATE = os.path.join(ROOT, "templates", "html",
                                "deck.template.html")

#: 随模版一起发布的 KaTeX 静态资源目录（离线自足，不再依赖 CDN）。
#: 交付物旁的 `katex/` 目录由 copy_katex() 从该处复制而来。
KATEX_SRC = os.path.join(ROOT, "templates", "html", "katex")

RE_IMG = asset_bundle.RE_IMG
IMG_EXTS = asset_bundle.IMG_EXTS

KIND_LABEL = {
    "section": "节标题",
    "dialogue": "课堂对话",
    "question": "题目",
    "knowledge": "微型知识点",
    "conclusion": "本节结论",
    "hint": "解题指引与按需提示",
}
DIALOG_LABEL = {"sectiondialogue": "节引入对话",
                "exampledialogue": "引例对话"}


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def load_config_value(project, key, default=""):
    """从 config.yaml 读取扁平键值（与 gen_config 语义一致的子集）。"""
    cfg = os.path.join(project, "config.yaml")
    if not os.path.exists(cfg):
        return default
    with open(cfg, encoding="utf-8", errors="replace") as f:
        m = re.search(r"(?m)^\s*%s\s*:\s*(.+?)\s*$" % re.escape(key),
                      f.read())
    if not m:
        return default
    v = m.group(1).strip()
    if v[:1] in ('"', "'") and v.rfind(v[0]) > 0:
        v = v[1:v.rfind(v[0])]
    return v


def collect_images(project):
    """项目切片引用的图片资产（兼容旧接口）：{raw: 源文件相对路径|None}。

    映射规则统一由 asset_bundle.image_asset 提供（含无扩展名探测、越界拒绝）。
    """
    out = {}
    for raw in asset_bundle.collect_image_refs(project):
        out[raw] = asset_bundle.image_asset(project, raw)["rel_source"]
    return out


def copy_katex(out_html, template_path=None):
    """把随模版发布的 KaTeX 资源复制到 <html所在目录>/katex/。

    背景（缺陷复盘）：此前卡片 HTML 从 jsDelivr CDN 拉 katex.min.js /
    auto-render.min.js。内网或断网环境下 CDN 不可达 → 数学公式整段以
    `$...$` 源码形式直接显示，读者看到的就是“成片乱码”。改为随交付物
    本地自足发布后，离线/内网双击打开也能正常渲染。

    返回 (stale_copy_count, missing_src)。源目录缺失时返回 missing_src=True，
    由调用方决定是否降级（模板自身有 qc-nokatex 兜底样式）。
    """
    src = KATEX_SRC
    if template_path:
        alt = os.path.join(os.path.dirname(os.path.abspath(template_path)),
                           "katex")
        if os.path.isdir(alt):
            src = alt
    if not os.path.isdir(src):
        return 0, True
    outdir = os.path.dirname(os.path.abspath(out_html))
    dst = os.path.join(outdir, "katex")
    # 按内容 sha256 同步：大小相同但字节不同也必须刷新
    copied = asset_bundle.sync_tree(src, dst)
    return copied, False


def _iter_rel_files(root):
    for base, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(base, fn)
            yield os.path.relpath(full, root)


def copy_assets(project, out_html, template_path=None):
    """把图片资产复制到 <html所在目录>/assets/，返回缺失清单。

    文件名/URL 与 tex2cards 渲染 HTML 时使用同一映射
    （asset_bundle.image_asset：assets/<内容哈希>-<basename>），
    同名不同内容的图互不覆盖，内容变化后按哈希更新。
    同时把 KaTeX 静态资源复制到 <html所在目录>/katex/（见 copy_katex）。
    """
    outdir = os.path.dirname(os.path.abspath(out_html))
    missing = asset_bundle.copy_images(project, os.path.join(outdir, "assets"))
    n_katex, no_katex = copy_katex(out_html, template_path)
    if no_katex:
        print("[render_html] [警告] 未找到本地 KaTeX 资源目录（%s），"
              "卡片将由浏览器尝试外部加载，离线环境下公式可能显示为源码"
              % KATEX_SRC)
    elif n_katex:
        print("[render_html] KaTeX 本地资源已同步：%d 个文件" % n_katex)
    return missing


# ---------------------------------------------------------------------------
# 卡片渲染
# ---------------------------------------------------------------------------
def render_speaker(entry, version, reveal_full):
    """渲染一条台词；teacher_only 台词学生版默认隐藏（reveal_full 时显示）。"""
    if entry.get("teacher_only") and version == "student" and not reveal_full:
        return ""
    flag = "1" if entry.get("teacher_only") else "0"
    return ('<div class="qc-speaker" data-teacher-only="%s">'
            '<span class="qc-role">%s</span>'
            '<span class="qc-line">%s</span></div>'
            % (flag, entry.get("role_html", ""), entry.get("html", "")))


def render_card(card, version, page_no, total, ctx):
    """渲染一张卡片（一页）。学生版剥离教师专属内容。"""
    cid = card["id"]
    kind = card["kind"]
    reveal_full = ctx.get("dialogue_student_reveal") == "full"
    attrs = ['class="qc-card"', 'id="qc-card-%s"' % cid,
             'data-kind="%s"' % kind, 'data-section="%s"' % card.get(
                 "section", ""),
             'data-seq="%d"' % card.get("seq", page_no)]
    if card.get("qid"):
        attrs.append('data-qid="%s"' % html_escape(card["qid"]))
    if card.get("source"):
        attrs.append('data-source="%s"' % html_escape(card["source"]))
    parts = ["<section %s>" % " ".join(attrs)]

    # ---- 卡头 ----
    head = ['<div class="qc-card-head">',
            '<span class="qc-kind-badge">%s</span>' % KIND_LABEL.get(
                kind, kind)]
    if kind == "question" and card.get("qid"):
        head.append('<span class="qc-qid">%s</span>'
                    % html_escape(card["qid"]))
    meta = []
    if version == "teacher":
        if card.get("qtype"):
            meta.append("【%s】" % html_escape(card["qtype"]))
        if card.get("difficulty"):
            meta.append('<span class="qc-tag">%s</span>'
                        % html_escape(card["difficulty"]))
        if card.get("practice_mode") == "unprompted":
            meta.append('<span class="qc-tag">无提示练习</span>')
    if meta:
        head.append('<span class="qc-card-meta">%s</span>' % " ".join(meta))
    head.append('<span class="qc-page-no">第 %d / %d 页</span>'
                % (page_no, total))
    head.append("</div>")
    parts.append("".join(head))

    # ---- 卡体 ----
    body = ['<div class="qc-card-body">']
    if card.get("lead_html"):
        body.append('<p class="qc-lead">%s</p>' % card["lead_html"])
    if kind == "section":
        body.append('<h2 class="qc-card-title">%s</h2>'
                    % card.get("title_html", ""))
    elif kind == "dialogue":
        if card.get("title"):
            body.append('<h2 class="qc-card-title">%s</h2>'
                        % html_escape(card["title"]))
        for entry in card.get("speakers", []):
            body.append(render_speaker(entry, version, reveal_full))
        if not card.get("speakers"):
            body.append('<p class="qc-lead">（本对话卡无台词）</p>')
    elif kind == "question":
        if card.get("stem_html"):
            body.append('<p class="qc-stem">%s</p>' % card["stem_html"])
        if card.get("parts"):
            body.append('<ol class="qc-parts">')
            for p in card["parts"]:
                body.append('<li><span class="qc-part-label">%s</span>'
                            '<span class="qc-part-html">%s</span></li>'
                            % (p.get("label", ""), p.get("html", "")))
            body.append("</ol>")
        if card.get("solution_html"):
            hidden = "" if version == "teacher" else " hidden"
            body.append(
                '<div class="qc-reveal qc-solution"%s>'
                '<div class="qc-reveal-title">参考解答</div>'
                '<div class="qc-reveal-body">%s</div></div>'
                % (hidden, card["solution_html"]))
        if version == "teacher" and card.get("teacher_note_html"):
            body.append(
                '<div class="qc-teacher-note">'
                '<div class="qc-teacher-note-title">教学要点与易错分析</div>'
                '<div>%s</div></div>' % card["teacher_note_html"])
    elif kind in ("knowledge", "conclusion", "hint"):
        if card.get("title"):
            body.append('<h2 class="qc-card-title">%s</h2>'
                        % html_escape(card["title"]))
        items = card.get("items_html") or []
        if items:
            body.append('<ul class="qc-know-items">%s</ul>'
                        % "".join("<li>%s</li>" % it for it in items))
        elif card.get("body_html"):
            body.append('<div class="qc-know-body">%s</div>'
                        % card["body_html"])
        else:
            body.append('<p class="qc-lead">（本卡无条目）</p>')
    body.append("</div>")
    parts.append("".join(body))

    # ---- 卡脚（作答统计 + 重置）----
    parts.append(
        '<div class="qc-card-foot">'
        '<span class="qc-score">本页选择题 <b>0 / 0</b></span>'
        '<button type="button" class="qc-btn qc-reset" '
        'title="清空本页作答，重新判定">重置本页</button>'
        '</div>')
    parts.append("</section>")
    return "".join(parts)


def render_toc(cards, version="student"):
    """目录抽屉：按节分组，每卡一条。学生版不显示题型标签。"""
    out = []
    cur_sec = None
    for c in cards:
        sec = c.get("section", "")
        if sec != cur_sec:
            cur_sec = sec
            title = ""
            for s in cards:
                if s["kind"] == "section" and s.get("section") == sec:
                    title = re.sub(r"<[^>]+>", "", s.get("title_html", ""))
                    break
            out.append('<div class="qc-toc-sec">%s%s</div>'
                       % (html_escape(sec), (" · " + title) if title else ""))
        if c["kind"] == "question":
            # 题型标签仅教师版可见（学生版隐藏题型，与 TeX 双版纪律一致）
            show_qtype = version == "teacher" and c.get("qtype")
            label = "%s%s" % (c.get("qid", ""),
                              (" · " + c["qtype"]) if show_qtype else "")
        elif c["kind"] in ("dialogue", "knowledge", "conclusion", "hint"):
            kind_label = KIND_LABEL.get(c["kind"], "")
            title = c.get("title") or ""
            if title and title != kind_label:
                label = "%s · %s" % (kind_label, title)
            else:
                label = title or kind_label
        else:
            label = "节标题"
        # 目录入口与卡片 DOM id 必须走同一约定：data-goto 携带完整 DOM id
        # （qc-card-<cid>，见 render_card），href 仍保留既有外链语义 #/<cid>，
        # 避免"两端各加一次前缀"导致的双前缀（NAV-01）。
        out.append('<a class="qc-toc-item" href="#/%s" data-goto="qc-card-%s">'
                   '<span class="qc-toc-no">%02d</span>'
                   '<span class="qc-toc-label">%s</span></a>'
                   % (c["id"], c["id"], c.get("seq", 0),
                      html_escape(label)))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 整册渲染
# ---------------------------------------------------------------------------
def render_version(deck, project, version, out_path,
                   template_path=DEFAULT_TEMPLATE):
    """渲染一个版本的 HTML；返回 (ok, message)。"""
    if version not in ("student", "teacher"):
        return False, "未知版本: %s" % version
    if not os.path.exists(template_path):
        return False, "缺少模版: %s" % template_path
    cards = deck.get("cards", [])
    total = len(cards)
    if total == 0:
        return False, "牌组为空，无可渲染卡片"

    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()

    ctx = {
        "dialogue_student_reveal": load_config_value(
            project, "dialogue_student_reveal", "prompt_only"),
    }
    cards_html = "\n".join(
        render_card(c, version, i + 1, total, ctx)
        for i, c in enumerate(cards))
    toc_html = render_toc(cards, version)

    version_label = "学生版" if version == "student" else "教师版"
    title = deck.get("title", "自学案")
    meta_title = "%s（%s · qchain-html 题链 HTML）" % (title, version_label)

    # 浏览器端严格渲染与 Python 端严格校验共用同一份 math-config.json，
    # 内嵌进页面（file:// 下不能 fetch）。模版必须声明占位符，否则拒绝渲染。
    if "{{MATH_CONFIG_JSON}}" not in tpl:
        return False, ("模版缺少 {{MATH_CONFIG_JSON}} 占位符：浏览器端无法获得与"
                       "服务端一致的数学配置（%s）" % template_path)
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(template_path)),
                            "math-config.json")
    if not os.path.exists(cfg_path):
        cfg_path = math_fragments.DEFAULT_CONFIG_PATH
    try:
        math_config_json = math_fragments.math_config_script_json(
            math_fragments.load_math_config(cfg_path))
    except (OSError, ValueError) as exc:
        return False, "数学配置无效 %s: %s" % (cfg_path, exc)

    text = tpl
    replacements = {
        "LANG": "zh-CN",
        "META_TITLE": meta_title,
        "MAIN_TITLE": html_escape(title),
        "VERSION": version,
        "VERSION_LABEL": version_label,
        "GENERATED_AT": time.strftime("%Y-%m-%d %H:%M:%S"),
        "TOC_HTML": toc_html,
        "CARDS_HTML": cards_html,
        "MATH_CONFIG_JSON": math_config_json,
    }
    for key, val in replacements.items():
        text = text.replace("{{%s}}" % key, val)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        return False, "模版存在未填充占位符: %s" % ", ".join(
            sorted(set(leftover)))
    # 正文区（</head> 之后）不得残留任何形式（单/双括号）的已知占位符
    body_part = text.split("</head>", 1)[-1]
    body_left = re.findall(r"\{+\s*(%s)\s*\}+"
                           % "|".join(replacements.keys()), body_part)
    if body_left:
        return False, "模版正文区存在未填充占位符: %s" % ", ".join(
            sorted(set(body_left)))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    missing = copy_assets(project, out_path, template_path)
    return True, ("%s 已生成（%d 张卡片）%s"
                  % (os.path.basename(out_path), total,
                     ("；缺失图片: " + ", ".join(missing)) if missing else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="卡片牌组 → HTML 渲染")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--version", choices=["student", "teacher", "both"],
                    default="both")
    ap.add_argument("--out", help="单个版本输出路径")
    ap.add_argument("--outdir", help="both 模式输出目录（默认项目目录）")
    ap.add_argument("--cards", help="已有 cards.json（缺省现场解析 TeX）")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    args = ap.parse_args(argv)
    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        print("[render] 项目目录不存在: %s" % project)
        return 2

    sys.path.insert(0, HERE)
    import tex2cards  # noqa: E402
    if args.cards and os.path.exists(args.cards):
        with open(args.cards, encoding="utf-8") as f:
            deck = json.load(f)
    else:
        deck, issues = tex2cards.parse_project(project)
        for msg in issues:
            print("[render] [解析提示] %s" % msg)

    rc = 0
    if args.version == "both":
        outdir = os.path.abspath(args.outdir or project)
        for v in ("student", "teacher"):
            ok, msg = render_version(deck, project, v,
                                     os.path.join(outdir, v + ".html"),
                                     args.template)
            print("[render] %s %s" % ("✔" if ok else "✖", msg))
            rc |= 0 if ok else 2
    else:
        out = args.out or os.path.join(project, args.version + ".html")
        ok, msg = render_version(deck, project, args.version, out,
                                 args.template)
        print("[render] %s %s" % ("✔" if ok else "✖", msg))
        rc = 0 if ok else 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
