#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""asset_bundle.py — 发布资源（图片 / KaTeX）的统一映射、同步与校验（任务卡 01，契约 C8）

背景（缺陷复盘）：
  * 旧实现按 basename 把图片复制到 assets/<basename>：不同目录的同名图互相覆盖，
    且“目标存在即跳过”→ 内容变化后不会更新；
  * 旧 copy_katex 以“大小相同即跳过”→ 同大小字节变化不会刷新；
  * release/ 只拷 HTML/PDF，不含 katex/ 与 assets/ → 发布目录复制到别处即残缺。

本模块是上述三件事的唯一实现，HTML 渲染端（tex2cards）与复制端（render_html /
deliver）共用同一映射函数 image_asset()，不会出现“写 URL 一套、拷文件另一套”。

对外 API：
  image_asset(project, raw)          → 单个 \includegraphics 引用的映射记录
  collect_image_refs(project)        → 切片里引用的全部原始图片参数
  copy_images(project, assets_dir)   → 按内容哈希复制图片，返回缺失清单
  sync_tree(src, dst)                → 按内容 sha256 同步整棵目录树，返回更新数
  build_file_manifest(root)          → [{relative_path,size,sha256}]（排除 manifest.json）
  verify_bundle(root, expected_trees=None) → 问题清单（空=自足）
  stage_html_resources(pdir, staging, katex_expected=None) → 资源入 staging 并校验
  BundleError                        → 发布资源不完整时抛出（携带 .problems）
"""
import hashlib
import os
import re
import shutil
import sys
import urllib.parse
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import path_safety  # noqa: E402（scripts/ 已在 sys.path；T1 修复的唯一路径原语）

RE_IMG = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")

#: URL 中哈希前缀长度（16 个十六进制字符 = 64 bit，足以区分同一学案内的图片）
HASH_LEN = 16
MANIFEST_NAME = "manifest.json"

_RE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_RE_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
_RE_CSS_URL = re.compile(r"url\(\s*([^)]*?)\s*\)")
_RE_CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?[\"']([^\"']+)[\"']")
_SKIP_SCHEMES = ("data:", "javascript:", "about:", "mailto:", "blob:")


class BundleError(Exception):
    """发布资源不完整 / 越界 / 被篡改。"""

    def __init__(self, problems):
        self.problems = list(problems)
        shown = "; ".join(self.problems[:8])
        more = "" if len(self.problems) <= 8 else \
            "（另有 %d 项）" % (len(self.problems) - 8)
        super().__init__("发布资源校验失败：%s%s" % (shown, more))


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_content(a, b):
    """内容是否相同（先比大小作快速否决，再比 sha256；相同大小不得视为相同）。"""
    if not (os.path.isfile(a) and os.path.isfile(b)):
        return False
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    return sha256_file(a) == sha256_file(b)


def _copy_atomic(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp-%d" % os.getpid()
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _is_inside(root, path):
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # 不同盘符
        return False


def safe_basename(raw):
    base = os.path.basename(raw.replace("\\", "/").rstrip("/")) or "image"
    return re.sub(r"[^\w.\-]", "_", base)


# ---------------------------------------------------------------------------
# 图片映射（HTML 渲染端与复制端共用）
# ---------------------------------------------------------------------------
def image_asset(project, raw):
    """\\includegraphics{raw} → 资产记录。

    字段：raw、url（HTML 中 img.src，形如 assets/<hash16>-<basename>）、
    source（源文件绝对路径；无法解析时 None）、rel_source（相对项目的 posix 路径，
    供溯源）、sha256、reason（无法解析时的原因）。

    解析失败（缺失 / 绝对路径 / 越出项目目录）→ source=None；url 退化为
    assets/<basename>，该文件不会被复制，随后的 verify_bundle 将报告缺失。
    """
    raw = (raw or "").strip()
    rec = {"raw": raw, "url": "assets/" + safe_basename(raw), "source": None,
           "rel_source": None, "sha256": None, "reason": None}
    if not raw:
        rec["reason"] = "空的图片路径"
        return rec
    norm = raw.replace("\\", "/")
    if os.path.isabs(raw) or norm.startswith("/") or _RE_DRIVE.match(norm) \
            or _RE_SCHEME.match(norm):
        rec["reason"] = "图片路径必须是相对项目目录的路径：%s" % raw
        return rec
    cands = [norm]
    if not os.path.splitext(norm)[1]:
        cands = [norm + ext for ext in IMG_EXTS]
    found = None
    for c in cands:
        p = os.path.normpath(os.path.join(project, *c.split("/")))
        if os.path.isfile(p):
            found = (c, p)
            break
    if not found:
        rec["reason"] = "图片文件不存在：%s" % raw
        return rec
    rel, path = found
    if not _is_inside(project, path):
        rec["reason"] = "图片路径越出项目目录：%s" % raw
        return rec
    digest = sha256_file(path)
    rec.update(
        source=path,
        rel_source=os.path.relpath(path, project).replace(os.sep, "/"),
        sha256=digest,
        url="assets/%s-%s" % (digest[:HASH_LEN], safe_basename(path)))
    return rec


def collect_image_refs(project):
    """项目切片（sec*.tex，不含 sec_template.tex）引用的原始图片参数（去重有序）。"""
    refs = []
    for name in sorted(os.listdir(project)):
        if not re.fullmatch(r"sec.*\.tex", name) or name == "sec_template.tex":
            continue
        with open(os.path.join(project, name), encoding="utf-8",
                  errors="replace") as f:
            for raw in RE_IMG.findall(f.read()):
                raw = raw.strip()
                if raw and raw not in refs:
                    refs.append(raw)
    return refs


def copy_images(project, assets_dir):
    """按内容哈希把全部引用图片复制到 assets_dir，返回缺失/越界的 raw 清单。"""
    missing = []
    os.makedirs(assets_dir, exist_ok=True)
    for raw in collect_image_refs(project):
        rec = image_asset(project, raw)
        if rec["source"] is None:
            missing.append(raw)
            continue
        dst = os.path.join(assets_dir, rec["url"].split("/", 1)[1])
        if not _same_content(rec["source"], dst):
            _copy_atomic(rec["source"], dst)
    return missing


def image_sources(project):
    """{url: {"source": rel_source, "sha256": digest}}（仅可解析的图片）。"""
    out = {}
    for raw in collect_image_refs(project):
        rec = image_asset(project, raw)
        if rec["source"] is not None:
            out[rec["url"]] = {"source": rec["rel_source"],
                               "sha256": rec["sha256"]}
    return out


# ---------------------------------------------------------------------------
# 目录树同步 / 清单
# ---------------------------------------------------------------------------
def iter_files(root):
    for base, _dirs, names in os.walk(root):
        for n in sorted(names):
            full = os.path.join(base, n)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def sync_tree(src, dst):
    """按内容 sha256 把 src 同步到 dst（大小相同但内容不同也更新），返回更新数。"""
    n = 0
    for rel, s in sorted(iter_files(src)):
        d = os.path.join(dst, *rel.split("/"))
        if not _same_content(s, d):
            _copy_atomic(s, d)
            n += 1
    return n


def build_file_manifest(root, exclude=(MANIFEST_NAME,)):
    """root 下全部文件的 [{relative_path,size,sha256}]（顶层 exclude 文件除外）。"""
    out = []
    for rel, full in iter_files(root):
        if rel in exclude:
            continue
        if os.path.islink(full) or not _is_inside(root, full):
            raise BundleError(["清单拒绝符号链接/越界文件：%s" % rel])
        out.append({"relative_path": rel, "size": os.path.getsize(full),
                    "sha256": sha256_file(full)})
    return out


# ---------------------------------------------------------------------------
# 引用提取与校验
# ---------------------------------------------------------------------------
class _RefParser(HTMLParser):
    _SRC_TAGS = ("img", "script", "source", "audio", "video", "iframe",
                 "embed", "track", "input")

    def __init__(self):
        super().__init__()
        self.refs = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self._SRC_TAGS and a.get("src"):
            self.refs.append(a["src"])
        elif tag == "link" and a.get("href"):
            self.refs.append(a["href"])
        elif tag == "object" and a.get("data"):
            self.refs.append(a["data"])
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.refs.extend(css_refs(data))


def css_refs(text):
    refs = [u.strip().strip("'\"").strip() for u in _RE_CSS_URL.findall(text)]
    refs.extend(_RE_CSS_IMPORT.findall(text))
    return [r for r in refs if r]


def html_refs(path):
    p = _RefParser()
    with open(path, encoding="utf-8", errors="replace") as f:
        p.feed(f.read())
    return p.refs


def _classify(root, base_dir, ref):
    """返回 (问题描述|None, 目标绝对路径|None)；可忽略的引用返回 (None, None)。"""
    r = ref.strip()
    if not r or r.startswith("#") or r.lower().startswith(_SKIP_SCHEMES):
        return None, None
    if _RE_SCHEME.match(r) or r.startswith("//"):
        return "外部引用（发布物必须自足）", None
    path = urllib.parse.unquote(r.split("#", 1)[0].split("?", 1)[0])
    if not path:
        return None, None
    if path.startswith("/") or path.startswith("\\") \
            or _RE_DRIVE.match(path) or os.path.isabs(path):
        return "绝对路径引用", None
    target = os.path.normpath(os.path.join(base_dir, *path.replace(
        "\\", "/").split("/")))
    if not _is_inside(root, target) or os.path.relpath(
            os.path.abspath(target), os.path.abspath(root)).startswith(".."):
        return "引用越出发布目录", None
    if not os.path.isfile(target):
        return "引用的资源不存在", None
    return None, target


def verify_bundle(root, expected_trees=None):
    """校验 root（release / staging）自足：

    * 每个 *.html 的 src/href 引用与 *.css 的 url()/@import 全部落在 root 内且文件存在；
    * 外部（http/https/协议相对）、绝对路径、.. 越界引用一律视为问题；
    * expected_trees={"katex": <源目录>}：源目录下每个文件必须在 root/<key>/ 下
      存在且内容 sha256 一致（缺失字体 / 被篡改均报告）。
    返回问题字符串列表；空列表表示通过。
    """
    problems = []
    seen_css = set()

    def check_css(css_path, owner):
        key = os.path.realpath(css_path)
        if key in seen_css:
            return
        seen_css.add(key)
        with open(css_path, encoding="utf-8", errors="replace") as f:
            refs = css_refs(f.read())
        for ref in refs:
            why, target = _classify(root, os.path.dirname(css_path), ref)
            if why:
                problems.append("%s -> %s：%s" % (
                    os.path.relpath(css_path, root).replace(os.sep, "/"),
                    ref, why))
            elif target and target.endswith(".css"):
                check_css(target, owner)

    for rel, full in sorted(iter_files(root)):
        if rel.endswith((".html", ".htm")):
            for ref in html_refs(full):
                why, target = _classify(root, os.path.dirname(full), ref)
                if why:
                    problems.append("%s -> %s：%s" % (rel, ref, why))
                elif target and target.endswith(".css"):
                    check_css(target, rel)
    # 未被 HTML 引用的 css 也检查其 url()
    for rel, full in sorted(iter_files(root)):
        if rel.endswith(".css"):
            check_css(full, rel)

    for key, src in (expected_trees or {}).items():
        if not src or not os.path.isdir(src):
            continue
        for rel, s in sorted(iter_files(src)):
            d = os.path.join(root, key, *rel.split("/"))
            if not os.path.isfile(d):
                problems.append("%s/%s：缺少打包资源" % (key, rel))
            elif not _same_content(s, d):
                problems.append("%s/%s：内容与打包源不一致（sha256）" % (key, rel))
    return problems


def stage_html_resources(pdir, staging, katex_expected=None):
    """把 HTML 所需资源（katex/ 整树 + 被引用的 assets/）放入 staging 并校验。

    staging 中必须已含 student.html / teacher.html 等页面。返回
    asset_sources（url → {source, sha256}）；任何缺失/越界/篡改 → BundleError。
    """
    has_html = any(n.endswith(".html") for n in os.listdir(staging))
    if not has_html:
        return {}
    problems = []
    refs = set()
    uses_katex = False
    for name in sorted(os.listdir(staging)):
        if name.endswith(".html"):
            for ref in html_refs(os.path.join(staging, name)):
                path = ref.split("#", 1)[0].split("?", 1)[0]
                if not path:
                    continue
                # 修复 T1：**先**做 percent 解码与安全规范化，**再**判定是否 assets/。
                # 旧实现先按 ".." 过滤、之后才 unquote，于是 assets/%2e%2e/%2e%2e/x
                # 通过过滤 → 解成 assets/../../x → 覆盖写到工作目录之外。
                try:
                    norm = path_safety.normalize_rel(path, "HTML 资源引用")
                except path_safety.PathEscape as exc:
                    problems.append("HTML 引用了非法资源路径（已拒绝，未复制任何文件）："
                                    "%s —— %s" % (ref, exc))
                    continue
                if norm.startswith("katex/"):
                    uses_katex = True
                elif norm.startswith("assets/"):
                    refs.add(norm)
    # 页面引用 katex/ 时，整棵 KaTeX 树（JS/CSS/fonts）都必须随发布物
    if uses_katex:
        ksrc = os.path.join(pdir, "katex")
        if os.path.isdir(ksrc):
            sync_tree(ksrc, os.path.join(staging, "katex"))
        else:
            problems.append("工作目录缺少 katex/ 资源目录：%s" % ksrc)
    # 修复 T1（第二阶段）：所有复制动作前先全量校验源/目标位置，任何一个越界都
    # 只记问题、不做复制；旧实现边复制边收集问题，越界写先发生、BundleError 后到。
    plan = []
    for url in sorted(refs):
        src = os.path.join(pdir, *url.split("/"))
        dest = os.path.join(staging, *url.split("/"))
        try:
            path_safety.assert_within(pdir, src, "资源源文件")
            path_safety.assert_within(staging, dest, "发布资源目标")
        except path_safety.PathEscape as exc:
            problems.append("%s：%s" % (url, exc))
            continue
        if os.path.isfile(src):
            plan.append((src, dest))
    if problems:
        raise BundleError(problems)
    for src, dest in plan:
        _copy_atomic(src, dest)
    problems.extend(verify_bundle(
        staging, {"katex": katex_expected}
        if (katex_expected and uses_katex) else None))
    sources = {u: v for u, v in image_sources(pdir).items() if u in refs}
    for url, meta in sources.items():
        staged = os.path.join(staging, *url.split("/"))
        if os.path.isfile(staged) and sha256_file(staged) != meta["sha256"]:
            problems.append("%s：内容与源图片 %s 的 sha256 不一致"
                            % (url, meta["source"]))
    if problems:
        raise BundleError(problems)
    return sources
