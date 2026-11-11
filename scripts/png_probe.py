#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""png_probe.py — 纯 stdlib 的 PNG 体检：尺寸、字节数、墨迹比例（判空白页）。

为什么需要它（修复 N8）：`deliver.smoke_gate_status` 原先只核对
`png_count >= 1`——只要冒烟跑出了至少一张图就算过。可冒烟的目的是
「排版事故肉眼可见」，而「一张全白的 A4 渲染图」正是最典型的排版事故
（选项整段丢失、字体未嵌、页边距错位都可能是空白）。把「有没有图」
当成「图对不对」，等于把这道门槛交给了真人；而当前主 Agent 侧几个模型
都没有图像输入能力，真人也就永远等在那里——门槛形同虚设。

这里只用标准库解 PNG：解析 IHDR/IDAT，按 filter 类型还原扫描线，统计
「非白像素」占比（ink_ratio）。全白页 ink_ratio ≈ 0，正常排版页通常在
0.01 以上。阈值故意取得很保守（默认 0.002），只抓「几乎全白」这种 unmistakable
的事故，不替人判断美观。

用法（CLI）:
  python png_probe.py <a.png> [<b.png> ...] [--json]
退出码: 0 = 所有文件可读（不代表不空白）；1 = 有文件不可读/不是 PNG。
"""
import argparse
import json
import struct
import sys
import zlib

TOOL_NAME = "png_probe.py"

#: 判定「几乎全白」的墨迹比例下限（保守值，只抓明确事故）
BLANK_INK_RATIO = 0.002
#: 判定「严重偏空」的下限（更宽，用于提示而非阻断）
SPARSE_INK_RATIO = 0.01
#: 小于这个像素宽/高基本不是 A4 页面渲染结果
MIN_SIDE_PX = 200
#: 灰度阈值：R/G/B 三元组均值不小于它视为「白」
WHITE_LEVEL = 245
#: 最多采样的像素数（超过则按步长抽样，避免大图拖慢门禁）
MAX_SAMPLES = 200000

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngProbeError(Exception):
    """PNG 无法解析（不是 PNG / 截断 / 不支持的位深与颜色类型）。"""


def _read_chunks(data):
    if not data.startswith(PNG_SIGNATURE):
        raise PngProbeError("不是 PNG 文件（签名不匹配）")
    pos = len(PNG_SIGNATURE)
    ihdr = None
    idat = []
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        body = data[pos:pos + length]
        if len(body) != length:
            raise PngProbeError("PNG 数据被截断（chunk %r 长度不足）" % ctype)
        pos += length + 4  # 跳过 CRC
        if ctype == b"IHDR":
            ihdr = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise PngProbeError("PNG 缺少 IHDR")
    if not idat:
        raise PngProbeError("PNG 缺少 IDAT")
    return ihdr, b"".join(idat)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, bpp, stride):
    """按 PNG 规范还原扫描线，返回 [(row_bytes), ...]；不支持的 filter 抛错。"""
    out = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        if pos + 1 + stride > len(raw):
            raise PngProbeError("PNG 像素数据不足（可能被截断）")
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise PngProbeError("不支持的 PNG filter 类型 %d" % ftype)
        out.append(line)
        prev = line
    return out


def _channels(color_type):
    # 0=灰度, 2=RGB, 3=调色板, 4=灰度+alpha, 6=RGBA
    return {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)


def probe_png(path):
    r"""读取 PNG，返回体检字典。

    返回键：width/height/bit_depth/color_type/bytes/ink_ratio/samples/
    blank/sparse/reason。
    """
    with open(path, "rb") as f:
        data = f.read()
    ihdr, idat = _read_chunks(data)
    if len(ihdr) < 13:
        raise PngProbeError("IHDR 长度非法")
    width, height, bit_depth, color_type, compression, filter_method, interlace = \
        struct.unpack(">IIBBBBB", ihdr[:13])
    if width <= 0 or height <= 0:
        raise PngProbeError("PNG 尺寸非法: %dx%d" % (width, height))
    if interlace != 0:
        raise PngProbeError("不支持隔行扫描 PNG（pdftoppm 不产出此类文件）")
    if compression != 0 or filter_method != 0:
        raise PngProbeError("不支持的 PNG 压缩/过滤方法")
    nch = _channels(color_type)
    if nch is None:
        raise PngProbeError("不支持的 PNG 颜色类型 %d" % color_type)
    if bit_depth != 8:
        raise PngProbeError("仅支持 8 位深 PNG（当前 %d）" % bit_depth)

    raw = zlib.decompress(idat)
    bpp = nch
    stride = width * bpp
    rows = _unfilter(raw, width, height, bpp, stride)

    total = width * height
    step = max(1, total // MAX_SAMPLES)
    sampled = 0
    inked = 0
    pixel_index = 0
    for y in range(height):
        row = rows[y]
        for x in range(width):
            if step > 1 and (pixel_index % step) != 0:
                pixel_index += 1
                continue
            pixel_index += 1
            base = x * bpp
            if color_type in (0, 4):
                r = g = b = row[base]
            else:
                r, g, b = row[base], row[base + 1], row[base + 2]
            sampled += 1
            if (r + g + b) / 3.0 < WHITE_LEVEL:
                inked += 1
    ink_ratio = (inked / sampled) if sampled else 0.0
    reason = ""
    if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
        reason = "尺寸 %dx%d 过小（疑似非页面渲染结果）" % (width, height)
    elif ink_ratio <= BLANK_INK_RATIO:
        reason = "墨迹占比 %.5f%%（几乎全白：疑似空白页/字体或选项丢失）" % (ink_ratio * 100.0)
    return {
        "path": path,
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "bytes": len(data),
        "ink_ratio": ink_ratio,
        "samples": sampled,
        "blank": bool(ink_ratio <= BLANK_INK_RATIO),
        "sparse": bool(ink_ratio <= SPARSE_INK_RATIO),
        "bad": bool(reason),
        "reason": reason,
    }


def probe_or_error(path):
    """永不抛错的版本：失败时返回 {"path","bad":True,"reason":…}。"""
    try:
        return probe_png(path)
    except (PngProbeError, OSError, zlib.error, struct.error) as e:
        return {"path": path, "bad": True, "blank": False, "sparse": False,
                "ink_ratio": None, "bytes": None, "width": None, "height": None,
                "reason": "PNG 无法解析: %s" % e}


def main(argv=None):
    ap = argparse.ArgumentParser(description="PNG 体检（尺寸/字节数/墨迹比例）")
    ap.add_argument("paths", nargs="+", help="PNG 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    results = [probe_or_error(p) for p in args.paths]
    if args.json:
        print(json.dumps({"tool": TOOL_NAME, "tool_version": TOOL_VERSION,
                          "blank_ink_ratio": BLANK_INK_RATIO,
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print("%s: %sx%s ink=%.4f%% bytes=%s %s"
                  % (r.get("path"), r.get("width"), r.get("height"),
                     (r.get("ink_ratio") or 0.0) * 100.0, r.get("bytes"),
                     r.get("reason") or "ok"))
    return 0 if all(not (r.get("bad") and r.get("ink_ratio") is None) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
