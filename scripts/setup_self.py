#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "1.0"
r"""
setup_self.py — 在输入文件所在处生成 self 文件夹并初始化题链脚手架

核心职责:
  1. 定位基准目录:
     - 若输入路径为文件 (如 path/to/ii.4.tex)，则基准目录为该文件所在目录 path/to/；
     - 若输入路径为目录且本身不是 self，则基准目录即为该目录；
     - 若输入路径未提供，则默认为当前工作目录。
  2. 在基准目录下创建并锁定 self 文件夹 (即 <base_dir>/self)；
  3. 拷贝基础 LaTeX 模板、YAML 配置及 HTML 牌组模板到 self/ 内部；
  4. 依据参数更新 self/config.yaml，并驱动 gen_config 与 pdstate 初始化；
  5. 强制隔离红线: 所有制成文件 (TeX切片、HTML卡片、双源PDF、报告、交付清单) 必须严格限定在 self/ 文件夹内。

用法:
  python setup_self.py [输入文件或目录路径] [--title 标题] [--profile {self_study,teaching,classroom}] [--format {both,pdf,html}] [--force]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
TEMPLATES_DIR = os.path.join(ENGINE_ROOT, "templates")

TEMPLATE_FILES = [
    "main.tex", "header.tex", "footer.tex", "student.tex", "teacher.tex",
    "config.yaml", "sec_template.tex", "classroom-plan.md", "error-log.md", "overview-guide.md"
]


def resolve_self_dir(input_path: str = None) -> tuple[str, str]:
    """根据输入文件或目录路径解析 (base_dir, self_dir)。"""
    if not input_path:
        base_dir = os.path.abspath(os.getcwd())
    else:
        abs_p = os.path.abspath(input_path)
        if os.path.isfile(abs_p):
            base_dir = os.path.dirname(abs_p)
        elif os.path.isdir(abs_p):
            if os.path.basename(abs_p) == "self":
                return os.path.dirname(abs_p), abs_p
            base_dir = abs_p
        else:
            # 路径可能尚不存在（如新建文件），若有扩展名则视作文件
            if os.path.splitext(abs_p)[1]:
                base_dir = os.path.dirname(abs_p)
            else:
                base_dir = abs_p

    self_dir = os.path.join(base_dir, "self")
    return base_dir, self_dir


def sanitize_title(title):
    r"""D4：标题消毒——防 config.yaml 注入与下游 TeX 注入。

    - 去除全部控制字符（含换行：换行可向 YAML 注入任意配置键）；
    - 去除单双引号（写入 YAML 时以单引号包裹——单引号标量内反斜杠为
      字面量、无双引号转义语义，可安全承载含 `\` 的标题）；
    - 去除首尾空白，长度上限 80 字符；
    - 消毒后为空则报错（调用方不得静默降级为默认标题掩盖攻击）。
    """
    if title is None:
        return None
    t = re.sub(r"[\x00-\x1f\x7f]", "", str(title))
    t = t.replace('"', "").replace("'", "").strip()
    if len(t) > 80:
        t = t[:80]
    if not t:
        raise ValueError("main_title 消毒后为空（原始输入含非法内容）")
    return t


def update_config_yaml(yaml_path: str, spec: dict) -> None:
    """按 spec 更新 config.yaml 中的指定配置行。"""
    if not os.path.isfile(yaml_path):
        return

    with open(yaml_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    mapping = {
        "main_title": spec.get("main_title"),
        "delivery_profile": spec.get("delivery_profile"),
        "output_format": spec.get("output_format"),
        "lesson_minutes": spec.get("lesson_minutes"),
    }

    def _fmt(key, val):
        # 文本类字段以单引号包裹写入（YAML 单引号标量内反斜杠为字面量，
        # 无双引号转义语义；值已经 sanitize_title 消毒，不含控制字符与
        # 引号），避免 ": "/“ #”/反斜杠等序列破坏扁平 YAML 解析。
        if key == "main_title":
            return "'%s'" % val
        return str(val)

    updated_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        matched_key = None
        for key in mapping:
            if stripped.startswith(f"{key}:"):
                matched_key = key
                break

        if matched_key and mapping[matched_key] is not None:
            val = mapping[matched_key]
            updated_lines.append(f"{matched_key}: {_fmt(matched_key, val)}\n")
            seen.add(matched_key)
        else:
            updated_lines.append(line)

    for key, val in mapping.items():
        if val is not None and key not in seen:
            updated_lines.append(f"{key}: {_fmt(key, val)}\n")

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)


def setup_self(
    input_path: str = None,
    title: str = None,
    profile: str = "self_study",
    output_format: str = "both",
    lesson_minutes: int = None,
    force: bool = False,
    python_bin: str = sys.executable
) -> str:
    """核心入口：在输入文件所在处生成并初始化 self/ 文件夹，返回 self_dir 绝对路径。"""
    base_dir, self_dir = resolve_self_dir(input_path)
    os.makedirs(self_dir, exist_ok=True)

    # 1. 复制 LaTeX 与 Markdown 基础模板
    for tf in TEMPLATE_FILES:
        src = os.path.join(TEMPLATES_DIR, tf)
        dst = os.path.join(self_dir, tf)
        if os.path.isfile(src) and (force or not os.path.exists(dst)):
            shutil.copy2(src, dst)

    # 2. 复制 HTML deck 模板
    html_src = os.path.join(TEMPLATES_DIR, "html", "deck.template.html")
    html_dst_dir = os.path.join(self_dir, "templates", "html")
    html_dst = os.path.join(html_dst_dir, "deck.template.html")
    if os.path.isfile(html_src) and (force or not os.path.exists(html_dst)):
        os.makedirs(html_dst_dir, exist_ok=True)
        shutil.copy2(html_src, html_dst)

    # 3. 注入配置（D4：标题先消毒，防 YAML/TeX 注入链）
    spec = {}
    if title:
        spec["main_title"] = sanitize_title(title)
    if profile:
        spec["delivery_profile"] = profile
    if output_format:
        spec["output_format"] = output_format

    # F1：lesson_minutes 供水，保证写入 config.yaml，不留为 None
    if lesson_minutes is not None:
        try:
            spec["lesson_minutes"] = int(lesson_minutes)
        except (ValueError, TypeError):
            spec["lesson_minutes"] = 90
    else:
        spec["lesson_minutes"] = 90

    config_yaml_path = os.path.join(self_dir, "config.yaml")
    if spec:
        update_config_yaml(config_yaml_path, spec)

    # 4. 生成 LaTeX 派生配置 (gen_config.py)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    gen_config_py = os.path.join(SCRIPTS_DIR, "gen_config.py")
    if os.path.isfile(gen_config_py):
        res = subprocess.run(
            [python_bin, gen_config_py, "--project", self_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env
        )
        if res.returncode != 0:
            print(f"[setup_self] [警告] gen_config 初始化提示: {res.stderr or res.stdout}")

    # 5. 初始化运行状态契约 (pdstate.py)
    pdstate_py = os.path.join(SCRIPTS_DIR, "pdstate.py")
    run_json = os.path.join(self_dir, ".pd", "run.json")
    if os.path.isfile(pdstate_py) and (force or not os.path.isfile(run_json)):
        subprocess.run(
            [python_bin, pdstate_py, self_dir, "init"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env
        )

    # 6. 写入工作区隔离清单 (.pd/workspace.json)
    pd_dir = os.path.join(self_dir, ".pd")
    os.makedirs(pd_dir, exist_ok=True)
    ws_meta = {
        "tool": "setup_self.py",
        "tool_version": TOOL_VERSION,
        "base_dir": base_dir,
        "self_dir": self_dir,
        "input_path": os.path.abspath(input_path) if input_path else "",
        "enforce_self_isolation": True,
        "description": "所有题链制成文件与衍生构建物严格收敛于此 self 文件夹内部，严禁溢出至基准目录。"
    }
    with open(os.path.join(pd_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump(ws_meta, f, ensure_ascii=False, indent=2)

    return self_dir


def main():
    parser = argparse.ArgumentParser(description="在输入文件处生成 self 文件夹并初始化题链脚手架")
    parser.add_argument("input_path", nargs="?", default="", help="原材料输入文件或目标目录")
    parser.add_argument("--title", help="学案主标题")
    parser.add_argument("--profile", choices=["self_study", "teaching", "classroom"], default="self_study",
                        help="交付模式 (默认 self_study)")
    parser.add_argument("--format", choices=["both", "pdf", "html"], default="both",
                        help="输出格式 (默认 both)")
    parser.add_argument("--lesson-minutes", type=int, default=None,
                        help="课时总预算分钟数 (教学模式默认 90)")
    parser.add_argument("--force", action="store_true", help="强制覆盖已有的模板配置")

    args = parser.parse_args()

    self_dir = setup_self(
        input_path=args.input_path,
        title=args.title,
        profile=args.profile,
        output_format=args.format,
        lesson_minutes=args.lesson_minutes,
        force=args.force
    )

    base_dir, _ = resolve_self_dir(args.input_path)
    print("=" * 60)
    print(f"[*] 题链工作区初始化完成！")
    print(f"  基准目录 (输入处): {base_dir}")
    print(f"  制成文件工作区:   {self_dir}")
    print(f"  强制隔离规则:     所有切片(sec*.tex)、卡片(cards.json)、双源PDF及交付物严格位于 self/ 内。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
