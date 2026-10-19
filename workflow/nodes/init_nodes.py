"""init_nodes.py — T0 Project initialization and scaffolding node.
Sets up the self/ working directory, copies TeX/YAML templates, generates config.tex,
and initializes the .pd/run.json execution contract.
"""

import hashlib
import json
import os
import shutil
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Any, List
from ..state import ShrinkCoursewareState
from ..adapters.script_runner import ScriptRunner

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import setup_self


ALLOWED_CONFIG_KEYS = {
    "main_title", "output_format", "delivery_profile", "dialogue_enabled", "dialogue_sections",
    "dialogue_section_opening", "dialogue_example_opening", "dialogue_max_section_turns",
    "dialogue_max_example_turns", "dialogue_student_reveal", "dialogue_require_coverage_hook",
    "font_size", "paperwidth", "paperheight", "orientation", "columns",
    "margin_top", "margin_bottom", "margin_left", "margin_right", "headheight",
    "teacher_head_left",
    "lesson_minutes", "core_task_limit", "max_student_pages",
    # 任务1：工作集选择（配置层字符串，gen_config FIELDS 校验枚举/文本）
    "run_scope", "target_sections", "target_qids",
}


def _update_config_yaml(yaml_path: str, spec: Dict[str, Any]) -> None:
    """Update scalar fields in config.yaml according to user_spec."""
    if not os.path.exists(yaml_path) or not spec:
        return

    filtered_spec = {k: v for k, v in spec.items() if k in ALLOWED_CONFIG_KEYS}
    # 保证写入 config.yaml 的数值合规，不留为 None 破坏 yaml 校验
    for key, val in list(filtered_spec.items()):
        if val is None:
            if key == "lesson_minutes":
                filtered_spec[key] = 90
            else:
                filtered_spec.pop(key)

    if not filtered_spec:
        return

    with open(yaml_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated_lines = []
    keys_updated = set()

    for line in lines:
        stripped = line.strip()
        matched_key = None
        for key in filtered_spec:
            if stripped.startswith(f"{key}:") or stripped.startswith(f"'{key}':") or stripped.startswith(f'"{key}":'):
                matched_key = key
                break

        if matched_key and matched_key in filtered_spec:
            val = filtered_spec[matched_key]
            if isinstance(val, bool):
                val_str = "true" if val else "false"
            elif isinstance(val, str):
                val_str = f'"{val}"' if any(c in val for c in ": #") else val
            else:
                val_str = str(val)
            updated_lines.append(f"{matched_key}: {val_str}\n")
            keys_updated.add(matched_key)
        else:
            updated_lines.append(line)

    for key, val in filtered_spec.items():
        if key not in keys_updated:
            if isinstance(val, bool):
                val_str = "true" if val else "false"
            elif isinstance(val, str):
                val_str = f'"{val}"' if any(c in val for c in ": #") else val
            else:
                val_str = str(val)
            updated_lines.append(f"{key}: {val_str}\n")

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)


def node_t0_init_scaffold(state: ShrinkCoursewareState) -> Dict[str, Any]:
    """T0: Initialize project scaffold, templates, LaTeX macros and pdstate run contract."""
    # 强制定位在输入文件处生成 self 文件夹，隔离所有后续制成文件
    input_path = state.get("input_path")
    project_root = state.get("project_root", ".")
    target_input = input_path or project_root

    base_dir, resolved_self_dir = setup_self.resolve_self_dir(target_input)
    self_dir = state.get("self_dir")
    if not self_dir or not os.path.basename(os.path.abspath(self_dir)) == "self":
        self_dir = resolved_self_dir
    self_dir = os.path.abspath(self_dir)
    project_root = os.path.abspath(base_dir)

    history = list(state.get("history", []))
    spec = state.get("user_spec", {})

    # 通过 setup_self 强制初始化 self/ 内部脚手架与隔离清单
    self_dir = setup_self.setup_self(
        input_path=target_input,
        title=spec.get("main_title") if spec else None,
        profile=spec.get("delivery_profile", "self_study") if spec else "self_study",
        output_format=spec.get("output_format", "both") if spec else "both",
        lesson_minutes=spec.get("lesson_minutes") if spec else None
    )

    # 同步更新 config.yaml
    config_yaml_path = os.path.join(self_dir, "config.yaml")
    if spec:
        _update_config_yaml(config_yaml_path, spec)

    # 原子生成并写入 <self>/.pd/confirmed-spec.json
    pd_dir = os.path.join(self_dir, ".pd")
    os.makedirs(pd_dir, exist_ok=True)

    delivery_profile = spec.get("delivery_profile", "self_study")
    raw_lm = spec.get("lesson_minutes")
    if delivery_profile in ("teaching", "classroom"):
        lesson_minutes = int(raw_lm) if raw_lm is not None else 90
    else:
        lesson_minutes = int(raw_lm) if raw_lm is not None else None

    core_task_limit = int(spec.get("core_task_limit", 12))
    max_student_pages = int(spec.get("max_student_pages", 6))
    confirmed_at = datetime.now(timezone.utc).isoformat()

    phase0_token_digest = (
        state.get("phase0_token_digest")
        or state.get("token_digest")
        or ("sha256:" + hashlib.sha256(b"phase0_unspecified_token").hexdigest())
    )

    spec_raw = json.dumps({
        "core_task_limit": core_task_limit,
        "delivery_profile": delivery_profile,
        "lesson_minutes": lesson_minutes,
        "max_student_pages": max_student_pages,
    }, sort_keys=True)
    spec_digest = "sha256:" + hashlib.sha256(spec_raw.encode("utf-8")).hexdigest()

    confirmed_spec = {
        "schema_version": 1,
        "delivery_profile": delivery_profile,
        "lesson_minutes": lesson_minutes,
        "core_task_limit": core_task_limit,
        "max_student_pages": max_student_pages,
        "confirmed_at": confirmed_at,
        "phase0_token_digest": phase0_token_digest,
        "spec_digest": spec_digest,
    }

    confirmed_spec_path = os.path.join(pd_dir, "confirmed-spec.json")
    tmp_spec_path = "%s.tmp-%s" % (confirmed_spec_path, os.getpid())
    with open(tmp_spec_path, "w", encoding="utf-8") as f:
        json.dump(confirmed_spec, f, ensure_ascii=False, indent=2)
    os.replace(tmp_spec_path, confirmed_spec_path)

    runner = ScriptRunner(self_dir)

    # 验证并补齐 LaTeX 派生配置 (gen_config.py)
    code_cfg, out_cfg, err_cfg = runner.run_script("gen_config.py", ["--project", self_dir])
    if code_cfg != 0:
        err = (err_cfg or out_cfg or "Unknown gen_config failure").strip()
        history.append(f"T0: gen_config.py 失败 (exit {code_cfg}): {err}")
        return {
            "stage": "HALTED",
            "routing_action": "HALT",
            "error_message": f"LaTeX 配置生成失败: {err}",
            "history": history
        }

    run_json = os.path.join(self_dir, ".pd", "run.json")
    run_id = state.get("run_id", "")
    if os.path.isfile(run_json):
        try:
            with open(run_json, "r", encoding="utf-8") as f:
                rdata = json.load(f)
                run_id = rdata.get("run_id", run_id)
        except Exception:
            pass

    history.append(f"T0: 脚手架初始化完成 (所有制成文件严格限定在: {self_dir}, run_id: {run_id})")

    return {
        "project_root": project_root,
        "self_dir": self_dir,
        "run_id": run_id,
        "scaffold_initialized": True,
        "stage": "T1_MAPPING_PENDING",
        "routing_action": "PROCEED",
        "history": history
    }
