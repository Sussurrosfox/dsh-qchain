#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "2.2"
r"""
gen_config.py — 由项目目录的 config.yaml 生成 LaTeX 参数文件
  输出: <project>/config-class.tex  (\PassOptionsToClass，须在 \documentclass 前 \input)
        <project>/config.tex        (geometry 版面与全部文本参数宏)
用法: python gen_config.py --project <项目目录>
依赖: 无第三方依赖（优先使用 PyYAML，缺失时退回内置解析器；
      两条路径在本配置使用的扁平子集上结果一致，见 parse_flat_yaml）。

配置语义（扁平 key: value，单行值）：
  - 值可用双引号或单引号包裹；引号内的 # 不视为注释；
  - 未加引号的值在第一个 " #" 处截断注释；加引号的值不支持行内注释；
  - 未知键、非法枚举、缺单位尺寸均报错（绝不静默忽略）。
"""
import argparse
import json
import os
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# 已知字段：kind 决定校验与转义方式
FIELDS = {
    "font_size": ("enum", ("10pt", "11pt", "12pt")),
    "paperwidth": ("dim", None),
    "paperheight": ("dim", None),
    "orientation": ("enum", ("landscape", "portrait")),
    "margin_top": ("dim", None),
    "margin_bottom": ("dim", None),
    "margin_left": ("dim", None),
    "margin_right": ("dim", None),
    "headheight": ("dim", None),
    "columns": ("int", None),
    "main_title": ("text", None),
    "teacher_head_left": ("text", None),
    "output_format": ("enum", ("both", "pdf", "html")),
    "delivery_profile": ("enum", ("classroom", "self_study", "teaching")),
    "dialogue_enabled": ("dlg_enable", None),
    "dialogue_section_opening": ("bool", None),
    "dialogue_example_opening": ("bool", None),
    "dialogue_max_section_turns": ("int", None),
    "dialogue_max_example_turns": ("int", None),
    "dialogue_student_reveal": ("enum", ("prompt_only", "full")),
    "dialogue_require_coverage_hook": ("bool", None),
    "dialogue_sections": ("text", None),
    "lesson_minutes": ("int", None),
    "core_task_limit": ("int", None),
    "max_student_pages": ("int", None),
}
DIM_RE = re.compile(r"^\d+(\.\d+)?(mm|cm|in|pt|em|ex|bp|pc)$")
DLG_ENABLE_RE = re.compile(r"^(true|false|sec\d+(,sec\d+)*)$")

# F2：数值上限防线，防止异常超大值导致排版或规划失控
INT_MAX_BOUNDS = {
    "lesson_minutes": 300,
    "core_task_limit": 30,
    "max_student_pages": 40,
}


def parse_flat_yaml(text):
    """解析本配置使用的扁平 YAML 子集，两条解析路径共用同一语义。

    与 PyYAML 在该子集上保持一致：单/双引号包裹的值原样取引号内内容
    （引号内 # 不是注释）；未包裹的值在 " #" 处截断注释并去首尾空白。
    """
    data = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        key, _, val = s.partition(":")
        key, val = key.strip(), val.strip()
        if val[:1] in ('"', "'"):
            q = val[0]
            end = val.find(q, 1)
            if end < 0:
                raise ValueError("config.yaml:%d: 引号未闭合: %s"
                                 % (lineno, line.strip()))
            val = val[1:end]
        else:
            val = val.split(" #", 1)[0].strip()
        data[key] = val
    return data


def load_config(path):
    """读取扁平的 key: value 配置。优先 PyYAML，缺失时用内置解析器。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml  # type: ignore
    except ImportError:
        return parse_flat_yaml(text)
    data = yaml.safe_load(text)
    # PyYAML 会把 true/false 解析为 Python 布尔值，统一回小写字符串
    return {k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in (data or {}).items()}


def validate(cfg):
    """字段类型、枚举与单位校验；返回错误消息列表。"""
    errors = []
    for k, v in cfg.items():
        if k not in FIELDS:
            errors.append("未知配置键 “%s”（已知键: %s）"
                          % (k, ", ".join(sorted(FIELDS))))
            continue
        kind, enum = FIELDS[k]
        v_str = str(v).strip()
        if kind == "enum" and v_str not in enum:
            errors.append("%s 取值 “%s” 非法，只能是: %s"
                          % (k, v, " / ".join(enum)))
        elif kind == "bool" and v_str.lower() not in ("true", "false"):
            errors.append("%s 取值 “%s” 非法，只能是: true / false" % (k, v))
        elif kind == "dlg_enable" and not DLG_ENABLE_RE.match(v_str):
            errors.append("%s 取值 “%s” 非法，只能是 true / false / "
                          "逗号分隔的小节列表（如 sec0,sec1）" % (k, v))
        elif kind == "int":
            try:
                val = int(v_str)
                if val < 1:
                    errors.append("%s 必须是正整数（收到 “%s”）" % (k, v))
                elif k in INT_MAX_BOUNDS and val > INT_MAX_BOUNDS[k]:
                    errors.append("%s (%d) 超过最大允许上限 %d" % (k, val, INT_MAX_BOUNDS[k]))
            except (ValueError, TypeError):
                errors.append("%s 必须是正整数（收到 “%s”）" % (k, v))
        elif kind == "dim" and not DIM_RE.match(v_str):
            errors.append("%s 必须带单位（mm/cm/in/pt 等），收到 “%s”"
                          % (k, v))
    return errors


def esc(v):
    r"""转义普通文本参数中的 LaTeX 特殊字符（配置为受信纯文本）。

    D4：旧实现不转义 \ { } ~ ^——`}` 可闭合 \providecommand 的宏体后
    注入任意 TeX 代码。反斜杠必须最先转义；控制字符直接剔除。
    """
    out = []
    for c in v:
        if ord(c) < 0x20 or ord(c) == 0x7F:
            continue  # 控制字符（含换行）不参与排版，直接剔除
        if c == "\\":
            out.append(r"\textbackslash{}")
        elif c == "{":
            out.append(r"\{")
        elif c == "}":
            out.append(r"\}")
        elif c == "~":
            out.append(r"\textasciitilde{}")
        elif c == "^":
            out.append(r"\textasciicircum{}")
        elif c in "%&_#":
            out.append("\\" + c)
        elif c == "$":
            out.append(r"\$")
        else:
            out.append(c)
    return "".join(out)


def verify_confirmed_spec(project_dir, cfg=None):
    """读取 <project_dir>/.pd/confirmed-spec.json（若存在），比对 config.yaml 校验：
      - 禁止未授权降级：如果 confirmed-spec 中是 teaching/classroom，而 config.yaml 被篡改为 self_study，返回错误；
      - 如果 lesson_minutes 与 confirmed-spec 不匹配，返回错误。
    返回具体错误信息列表。
    """
    errors = []
    project_dir = os.path.abspath(project_dir)
    spec_path = os.path.join(project_dir, ".pd", "confirmed-spec.json")
    if not os.path.isfile(spec_path):
        return errors

    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            confirmed = json.load(f)
    except Exception as e:
        errors.append("读取 confirmed-spec.json 失败: %s" % e)
        return errors

    if cfg is None:
        cfg_path = os.path.join(project_dir, "config.yaml")
        if not os.path.isfile(cfg_path):
            errors.append("缺少配置文件: %s" % cfg_path)
            return errors
        try:
            cfg = load_config(cfg_path)
        except Exception as e:
            errors.append("解析 config.yaml 失败: %s" % e)
            return errors

    conf_prof = confirmed.get("delivery_profile")
    cfg_prof = cfg.get("delivery_profile")

    # 禁止未授权降级：如果 confirmed-spec 中是 teaching/classroom，而 config.yaml 被篡改为 self_study，返回错误
    if conf_prof in ("teaching", "classroom") and cfg_prof == "self_study":
        errors.append(
            "未授权交付模式降级: confirmed-spec 为 “%s”，但 config.yaml 被篡改为 “self_study”" % conf_prof
        )

    # 如果 lesson_minutes 与 confirmed-spec 不匹配，返回错误
    conf_lm = confirmed.get("lesson_minutes")
    cfg_lm_raw = cfg.get("lesson_minutes")
    cfg_lm = int(cfg_lm_raw) if (cfg_lm_raw is not None and str(cfg_lm_raw).isdigit()) else None

    if conf_lm is not None:
        if cfg_lm != conf_lm:
            errors.append(
                "lesson_minutes 不匹配: confirmed-spec 为 %s，但 config.yaml 为 %s" % (conf_lm, cfg_lm_raw)
            )
    else:
        # confirmed-spec 中为 None (self_study) 时，如果 config.yaml 改为了 teaching/classroom，禁止篡改
        if conf_prof == "self_study" and cfg_prof in ("teaching", "classroom"):
            errors.append(
                "交付模式不匹配: confirmed-spec 为 “%s”，但 config.yaml 为 “%s”" % (conf_prof, cfg_prof)
            )

    return errors


def generate(tdir):
    """由 <tdir>/config.yaml 确定性生成 config-class.tex 与 config.tex。

    供 build.py 等直接调用（不依赖外部进程状态）。内容相同则不重写文件
    （内容比较，不看 mtime）。返回 0=成功, 2=缺少配置/解析或校验失败。
    """
    tdir = os.path.abspath(tdir)
    cfg_path = os.path.join(tdir, "config.yaml")
    if not os.path.exists(cfg_path):
        print("[gen_config] 缺少 %s" % cfg_path)
        return 2
    try:
        cfg = load_config(cfg_path)
    except ValueError as e:
        print("[gen_config] 配置解析失败: %s" % e)
        return 2

    errors = validate(cfg)
    spec_errors = verify_confirmed_spec(tdir, cfg)
    errors.extend(spec_errors)
    if errors:
        print("[gen_config] 配置校验失败:")
        for e in errors:
            print("  - " + e)
        return 2

    banner = ("% Auto-generated by scripts/gen_config.py from config.yaml\n"
              "% 请勿手改本文件；修改 config.yaml 后重新运行 gen_config.py。\n")

    # ---- config-class.tex：\documentclass 之前的选项注入 ----
    class_tex = (banner + "\\PassOptionsToClass{%s}{ctexart}\n"
                 % cfg.get("font_size", "12pt"))

    # ---- config.tex：geometry 与文本参数 ----
    geo_keys = ["paperwidth", "paperheight", "orientation", "margin_top",
                "margin_bottom", "margin_left", "margin_right", "headheight"]
    geo_map = {"margin_top": "top", "margin_bottom": "bottom",
               "margin_left": "left", "margin_right": "right"}

    # Determine whether to emit the orientation keyword.
    # geometry 宏包的 landscape 选项会交换 paperwidth 与 paperheight 的含义。
    # 若用户已明确填写 paperwidth > paperheight（即横向尺寸），则 geometry 会根据
    # 尺寸自动识别横向版面；此时若再加 landscape 选项，geometry 会再次对调两轴，
    # 造成实际输出为竖版。
    # 因此：只有当 paperwidth <= paperheight（纵向或方形）且 orientation=landscape
    # 时才需要显式写出 landscape；当 paperwidth > paperheight 时完全省略 orientation。
    _pw = cfg.get("paperwidth", "")
    _ph = cfg.get("paperheight", "")

    def _dim_to_pt(s):
        """将带单位的尺寸字符串转为 pt，仅支持 mm/cm/in/pt。"""
        m = re.match(r"^(\d+(?:\.\d+)?)(mm|cm|in|pt)$", s)
        if not m:
            return 0.0
        v, u = float(m.group(1)), m.group(2)
        return {"mm": v * 2.8346, "cm": v * 28.346, "in": v * 72.0, "pt": v}[u]

    _pw_pt = _dim_to_pt(_pw)
    _ph_pt = _dim_to_pt(_ph)
    # 当 paperwidth > paperheight，省略 orientation（尺寸本身已是横版）
    _skip_orientation = (_pw_pt > 0 and _ph_pt > 0 and _pw_pt > _ph_pt)

    opts = []
    for k in geo_keys:
        if k not in cfg:
            continue
        if k == "orientation":
            if _skip_orientation:
                continue  # 横向尺寸已确保横版，无需重复声明 landscape
            opts.append(cfg[k])  # landscape / portrait 为无值选项
        else:
            opts.append("%s=%s" % (geo_map.get(k, k), cfg[k]))
    geometry = "\\usepackage[\n  " + ",\n  ".join(opts) + "\n]{geometry}\n"

    text_keys = ["columns", "main_title", "teacher_head_left", "output_format", "delivery_profile"]
    defaults = {"columns": "2", "output_format": "both", "delivery_profile": "self_study"}

    def csname(k):  # 下划线转 CamelCase（@ 在导言区不是字母，不可用）
        return "pd" + "".join(w.capitalize() for w in k.split("_"))

    macros = []
    for k in text_keys:
        val = cfg.get(k, defaults.get(k, ""))
        if FIELDS[k][0] == "text":
            val = esc(val)  # 普通文本字段转义 LaTeX 特殊字符
        macros.append("\\providecommand{\\%s}{%s}" % (csname(k), val))
    # 对话层配置
    dialogue_keys = [k for k in FIELDS if k.startswith("dialogue_")]
    dialogue_defaults = {"dialogue_student_reveal": "prompt_only", "dialogue_enabled": "false"}
    for k in dialogue_keys:
        val = cfg.get(k, dialogue_defaults.get(k, ""))
        macros.append("\\providecommand{\\%s}{%s}" % (csname(k), esc(str(val)) if FIELDS[k][0] == "text" else str(val)))

    config_tex = (banner + "\n% --- 纸张与版面几何 ---\n" + geometry
                  + "\n% --- 文本参数 (\\pd<Key>) ---\n"
                  + "\n".join(macros) + "\n")

    for name, content in [("config-class.tex", class_tex),
                          ("config.tex", config_tex)]:
        out = os.path.join(tdir, name)
        old = open(out, encoding="utf-8").read() if os.path.exists(out) else None
        if old == content:
            print("[gen_config] %s 无变化" % name)
        else:
            with open(out, "w", encoding="utf-8") as f:
                f.write(content)
            print("[gen_config] 已生成 %s" % out)
    return 0


def main():
    ap = argparse.ArgumentParser(description="由 config.yaml 生成 LaTeX 参数文件")
    ap.add_argument("--project", required=True,
                    help="项目目录（含 config.yaml；config*.tex 生成于此）")
    args = ap.parse_args()
    return generate(args.project)


if __name__ == "__main__":
    sys.exit(main())
