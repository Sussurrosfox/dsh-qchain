#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""path_safety.py — 路径安全的唯一原语（修复 T1/D1/D2 的 Python 侧）

与 scripts/path_safety.mjs 同语义（两边必须同步修改）：

  normalize_rel(rel)            → 相对路径规范化（解一次 percent-encoding，拒绝 .. / 绝对路径）
  sanitize_id(value)            → 标识符白名单
  safe_join_under(root, rel)    → 规范化 + 拼接 + realpath 越界断言
  assert_within(root, target)   → 写盘前最后一道闸
  is_within(root, target)       → 布尔版本
  PathEscape                    → 越界/非法即抛（不允许静默跳过）

背景（审计发现 T1）：asset_bundle.stage_html_resources 先按 ``".." not in
path.split("/")`` 过滤，**之后**才 ``urllib.parse.unquote(path)``，于是
``assets/%2e%2e/%2e%2e/x`` 通过过滤 → 解成 ``assets/../../x`` → 复制覆盖到
工作目录之外，越界判定要等 verify_bundle 才做（先破坏、后报错）。
"""
import os
import re
import urllib.parse

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PathEscape(ValueError):
    """路径非法或越界。code 用于门禁/证据台账的显式归因。"""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _decode_segment(seg, label="路径"):
    out = seg
    if "%" in out:
        try:
            out = urllib.parse.unquote(seg, errors="strict")
        except UnicodeDecodeError:
            raise PathEscape("PATH_ENCODING_INVALID",
                             "%s 含非法 percent 编码：%s" % (label, seg),
                             {"segment": seg})
    if "/" in out or "\\" in out:
        raise PathEscape("PATH_SEGMENT_SMUGGLED",
                         "%s 的路径段解码后含分隔符（编码穿越）：%s" % (label, seg),
                         {"segment": seg})
    return out


def normalize_rel(rel, label="路径"):
    """规范成以 '/' 分隔的相对路径；拒绝空、NUL、绝对路径、盘符、任何 '..' 段。

    percent 解码只做一次（%252e 不会被二次还原成 .）。
    """
    if not isinstance(rel, str):
        raise PathEscape("PATH_NOT_STRING", "%s 必须是字符串" % label)
    if not rel.strip():
        raise PathEscape("PATH_EMPTY", "%s 不能为空" % label)
    if "\x00" in rel:
        raise PathEscape("PATH_NUL", "%s 含 NUL 字符" % label)
    raw = rel.replace("\\", "/")
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise PathEscape("PATH_ABSOLUTE", "%s 不能是绝对路径：%s" % (label, rel),
                         {"path": rel})
    parts = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        dec = _decode_segment(seg, label)
        if dec in ("", "."):
            continue
        if dec == "..":
            raise PathEscape("PATH_TRAVERSAL",
                             "%s 不能含上跳段（..）：%s" % (label, rel), {"path": rel})
        if dec.startswith("/") or _DRIVE_RE.match(dec):
            raise PathEscape("PATH_ABSOLUTE",
                             "%s 解码后成为绝对路径：%s" % (label, rel), {"path": rel})
        parts.append(dec)
    if not parts:
        raise PathEscape("PATH_EMPTY", "%s 规范化后为空：%s" % (label, rel), {"path": rel})
    return "/".join(parts)


def sanitize_id(value, label="标识符", max_len=128):
    """拼文件名的标识符白名单：字母/数字开头，仅字母数字与 . _ -"""
    s = value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())
    if not s:
        raise PathEscape("ID_EMPTY", "%s 不能为空" % label)
    if len(s) > max_len:
        raise PathEscape("ID_TOO_LONG", "%s 超长（>%d）：%s…" % (label, max_len, s[:24]))
    if s in (".", ".."):
        raise PathEscape("ID_RESERVED", "%s 不能是 . 或 ..：%s" % (label, s))
    if not _ID_RE.match(s):
        raise PathEscape("ID_CHARSET_INVALID",
                         "%s 只允许字母/数字开头，且只含字母数字与 . _ -：%s" % (label, s),
                         {"value": s})
    return s


def _real_key(target):
    """最近已存在祖先的 realpath + 剩余段回填（目标不存在也能得到真实位置）。"""
    cur = os.path.abspath(target)
    rest = []
    while True:
        if os.path.exists(cur) or os.path.islink(cur):
            real = os.path.realpath(cur)
            return os.path.join(real, *reversed(rest)) if rest else real
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.join(cur, *reversed(rest)) if rest else cur
        rest.append(os.path.basename(cur))
        cur = parent


def is_within(root, target):
    """target 的 realpath 是否落在 root 的 realpath 之内（含 target == root）。"""
    if not root or not target:
        return False
    rk = os.path.normcase(_real_key(root))
    tk = os.path.normcase(_real_key(target))
    if rk == tk:
        return True
    return tk.startswith(rk + os.sep)


def assert_within(root, target, label="写入目标"):
    """写盘前最后一道闸：越界即抛 PathEscape（junction/symlink 不例外）。"""
    if is_within(root, target):
        return _real_key(target)
    raise PathEscape("PATH_ESCAPES_ROOT",
                     "%s 越出 %s（realpath 归一后判定，junction/symlink 不例外）：%s"
                     % (label, _real_key(root), _real_key(target)),
                     {"root": _real_key(root), "target": _real_key(target)})


def safe_join_under(root, rel, label="写入目标"):
    """规范化 + 拼接 + 越界断言，一步到位（写盘点首选）。"""
    norm = normalize_rel(rel, label)
    abs_path = os.path.join(os.path.abspath(root), *norm.split("/"))
    assert_within(root, abs_path, label)
    return abs_path, norm
