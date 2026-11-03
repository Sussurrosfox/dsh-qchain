#!/usr/bin/env python3
# -*- coding: utf-8 -*-
TOOL_VERSION = "2.3"
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
    # T2 并行写作派发档位（workflow/nodes/section_batch_nodes.py 读取；
    # 这些键只影响派发节奏，不进入 config.tex 版面输出）
    # agent_dispatch 多态兼容：标量档位（旧）或映射块
    # {profile, allow_unattested, model_map, max_concurrency}（新，q_audit.md
    # 多 Agent 证据链改造；映射嵌套键见 _validate_dispatch_map）
    # 档位语义（audit §12.1）：interactive=并发1 交互微调默认；safe=并发1
    # 调试/provider 不稳定；fast=并发4 完整生成默认；budget=并发2 限流。
    "agent_dispatch": ("enum", ("interactive", "safe", "fast", "budget")),
    "agent_max_concurrency": ("int0", None),
    # 任务19（DATA-CONTRACTS C12）：派发契约版本。
    # 缺省（键不存在）= 1 = legacy：保留旧的「扁平键优先」优先级与旧并发语义，
    # 旧工程/旧检查点因此不会被静默放大。写 2 = 执行器计划契约：
    # 工具显式正整数 > 嵌套 agent_dispatch.max_concurrency > 扁平
    # agent_max_concurrency > 档位表；嵌套与扁平同时为正且不一致 → 冲突报错
    # （CONFIG_CONCURRENCY_CONFLICT），0 表示自动，负数/小数/布尔/>16 一律拒绝。
    # 未知版本（如 3）必须拒绝：绝不按最新版本的规则去解释一份旧计划。
    "dispatch_contract_version": ("int", None),
    "agent_batch_window": ("int", None),
    "agent_timeout_seconds": ("int", None),
    "agent_max_retries": ("int", None),
    # 证据链逃生阀（扁平形态；等价于 agent_dispatch.allow_unattested）
    "agent_allow_unattested": ("bool", None),
    # F0 内容契约（audit §18.3；这些键只进入 Phase 0 冻结的
    # source-manifest.json / intent-contract.json，不进入 config.tex 版面输出）
    "content_policy": ("enum", ("faithful", "authoring")),
    "source_files": ("text", None),
    "excluded_sources": ("text", None),
    "allowed_extensions": ("text", None),
    "reorder_allowed": ("bool", None),
    "must_cover": ("text", None),
    "student_level": ("text", None),
    # 任务1（audit §17 任务1 / §12.4 工作集选择）：Phase 0 让用户选
    # full=全量 / focused=只做目标节 / edit=只重生成已存在节。
    # target_sections/target_qids 在配置层保持逗号分隔字符串，进 state 时
    # 规范化为 list[str]（见 workflow/scope_selection.py）；这些键不进入
    # config.tex 版面输出，只影响派发范围。
    "run_scope": ("enum", ("full", "focused", "edit")),
    "target_sections": ("text", None),
    "target_qids": ("text", None),
    # 任务10（DATA-CONTRACTS C1）：执行契约扁平键——把「内容策略」与「执行图」
    # 分开。这些键只进入 Phase 0 冻结的 state / intent-contract.json，**绝不进入
    # config.tex 版面输出**：既不在 text_keys 里，也不以 dialogue_ 开头
    # （generate() 只遍历 text_keys 与 dialogue_* 两组键，见 test_t08）。
    # 取值语义：execution_profile 决定编译哪张图（缺省 legacy）；其余键是
    # faithful_fast 的工程初始值（非已验证最佳值）。
    "execution_profile": ("enum", ("legacy", "faithful_fast")),
    "pedagogy_policy": ("enum", ("advisory", "strict")),
    "budget_policy": ("enum", ("advisory", "strict")),
    "require_classroom_plan": ("bool", None),
    "writer_batch_max_sections": ("int", None),
    "writer_batch_max_source_chars": ("int", None),
    "agent_prompt_max_chars": ("int", None),
    "repair_max_per_scope": ("int", None),
    "repair_max_total": ("int", None),
}

#: agent_dispatch 映射块允许的嵌套键及其校验类型（q_audit.md §1.4）
DISPATCH_MAP_FIELDS = {
    "profile": ("enum", ("interactive", "safe", "fast", "budget")),
    "allow_unattested": ("bool", None),
    "max_concurrency": ("int0", None),
    "model_map": ("map", None),
}
DIM_RE = re.compile(r"^\d+(\.\d+)?(mm|cm|in|pt|em|ex|bp|pc)$")
DLG_ENABLE_RE = re.compile(r"^(true|false|sec\d+(,sec\d+)*)$")

# F2：数值上限防线，防止异常超大值导致排版或规划失控
INT_MAX_BOUNDS = {
    "lesson_minutes": 300,
    "core_task_limit": 30,
    "max_student_pages": 40,
    # 任务19（C12）：客户端并发硬上限，与 workflow/dispatch_policy.py 的
    # MAX_CONCURRENCY_LIMIT、scripts/check_dispatch.py 的 MAX_CONCURRENCY_LIMIT 同值
    "agent_max_concurrency": 16,
}

#: 任务19（C12）：受支持的派发契约版本（1=legacy，2=执行器计划契约）
SUPPORTED_DISPATCH_CONTRACT_VERSIONS = (1, 2)


def parse_flat_yaml(text):
    """解析本配置使用的扁平 YAML 子集，两条解析路径共用同一语义。

    与 PyYAML 在该子集上保持一致：单/双引号包裹的值原样取引号内内容
    （引号内 # 不是注释）；未包裹的值在 " #" 处截断注释并去首尾空白。
    """
    data = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if line[:1] in (" ", "\t"):
            continue  # 缩进行=嵌套块内容（如 agent_dispatch 映射块），扁平解析器不识别
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
    # PyYAML 会把 true/false 解析为 Python 布尔值，统一回小写字符串；
    # 映射/列表（如 agent_dispatch 嵌套块）原样保留，交由 validate 分支校验。
    out = {}
    for k, v in (data or {}).items():
        if isinstance(v, bool):
            out[k] = str(v).lower()
        elif isinstance(v, (dict, list)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _validate_dispatch_map(dispatch):
    """校验 agent_dispatch 映射块的嵌套键（q_audit.md §1.4 形态）。"""
    errors = []
    for key, value in dispatch.items():
        if value is None:
            continue  # 键存在但未配置值（YAML `key:` 空值）= 未设置，跳过
        spec = DISPATCH_MAP_FIELDS.get(str(key))
        if spec is None:
            errors.append("agent_dispatch 含未知嵌套键 “%s”（已知键: %s）"
                          % (key, ", ".join(sorted(DISPATCH_MAP_FIELDS))))
            continue
        kind, enum = spec
        v_str = str(value).strip()
        if kind == "enum" and v_str not in enum:
            errors.append("agent_dispatch.%s 取值 “%s” 非法，只能是: %s"
                          % (key, value, " / ".join(enum)))
        elif kind == "bool" and v_str.lower() not in ("true", "false"):
            errors.append("agent_dispatch.%s 取值 “%s” 非法，只能是: true / false"
                          % (key, value))
        elif kind == "int0":
            # 任务19（C12）：bool 是 int 子类（int(True)=1），必须显式拒绝；
            # 0 = 自动（按档位表），正数不得超过客户端并发硬上限 16。
            if isinstance(value, bool):
                errors.append("agent_dispatch.%s 不能是布尔值（收到 “%s”）"
                              % (key, value))
                continue
            try:
                num = int(v_str)
                if num < 0:
                    errors.append("agent_dispatch.%s 必须是非负整数（收到 “%s”）"
                                  % (key, value))
                elif num > INT_MAX_BOUNDS["agent_max_concurrency"]:
                    errors.append(
                        "agent_dispatch.%s (%d) 超过客户端并发上限 %d"
                        % (key, num, INT_MAX_BOUNDS["agent_max_concurrency"]))
            except (TypeError, ValueError):
                errors.append("agent_dispatch.%s 必须是非负整数（收到 “%s”）"
                              % (key, value))
        elif kind == "map":
            if not isinstance(value, dict) or not value:
                errors.append("agent_dispatch.%s 必须是非空映射（如 {T2: {provider: ..., model: ...}}）"
                              % key)
                continue
            for stage, route in value.items():
                if not isinstance(route, dict) or not route.get("provider") or not route.get("model"):
                    errors.append("agent_dispatch.%s[%s] 必须同时提供 provider 与 model"
                                  % (key, stage))
    return errors


def validate(cfg):
    """字段类型、枚举与单位校验；返回错误消息列表。"""
    errors = []
    for k, v in cfg.items():
        if k not in FIELDS:
            errors.append("未知配置键 “%s”（已知键: %s）"
                          % (k, ", ".join(sorted(FIELDS))))
            continue
        # agent_dispatch 映射块（q_audit.md 证据链改造新增形态）：分支校验嵌套键；
        # 空值（扁平解析器遇嵌套块时的退化形态）= 未设置档位，回落默认 safe
        if k == "agent_dispatch" and isinstance(v, dict):
            errors.extend(_validate_dispatch_map(v))
            continue
        if k == "agent_dispatch" and v == "":
            continue
        kind, enum = FIELDS[k]
        v_str = str(v).strip()
        if k == "dispatch_contract_version":
            # 任务19（C12）：版本必须显式受支持，未知版本一律拒绝
            # （绝不按最新版本的规则解释旧计划）
            if isinstance(v, bool):
                errors.append("%s 不能是布尔值（收到 “%s”）；布尔只能用于 bool 字段"
                              % (k, v))
                continue
            try:
                version = int(v_str)
            except (ValueError, TypeError):
                errors.append("%s 必须是整数（收到 “%s”）" % (k, v))
                continue
            if version not in SUPPORTED_DISPATCH_CONTRACT_VERSIONS:
                errors.append(
                    "%s=%d 不受支持，只能是: %s（1=legacy 旧契约，"
                    "2=执行器计划契约）"
                    % (k, version,
                       " / ".join(str(x) for x in SUPPORTED_DISPATCH_CONTRACT_VERSIONS)))
            continue
        if kind == "enum" and v_str not in enum:
            errors.append("%s 取值 “%s” 非法，只能是: %s"
                          % (k, v, " / ".join(enum)))
        elif kind == "bool" and v_str.lower() not in ("true", "false"):
            errors.append("%s 取值 “%s” 非法，只能是: true / false" % (k, v))
        elif kind == "dlg_enable" and not DLG_ENABLE_RE.match(v_str):
            errors.append("%s 取值 “%s” 非法，只能是 true / false / "
                          "逗号分隔的小节列表（如 sec0,sec1）" % (k, v))
        elif kind == "int0":
            # 任务10：bool 是 int 的子类，int(True)=1 会静默通过——必须显式拒绝
            if isinstance(v, bool):
                errors.append("%s 不能是布尔值（收到 “%s”）；布尔只能用于 bool 字段"
                              % (k, v))
                continue
            try:
                val = int(v_str)
                if val < 0:
                    errors.append("%s 必须是非负整数（收到 “%s”）" % (k, v))
                elif k in INT_MAX_BOUNDS and val > INT_MAX_BOUNDS[k]:
                    errors.append("%s (%d) 超过最大允许上限 %d" % (k, val, INT_MAX_BOUNDS[k]))
            except (ValueError, TypeError):
                errors.append("%s 必须是非负整数（收到 “%s”）" % (k, v))
        elif kind == "int":
            # 任务10：bool 是 int 的子类，int(True)=1 会静默通过——必须显式拒绝
            if isinstance(v, bool):
                errors.append("%s 不能是布尔值（收到 “%s”）；布尔只能用于 bool 字段"
                              % (k, v))
                continue
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
    errors.extend(_validate_dispatch_concurrency_conflict(cfg))
    return errors


def _validate_dispatch_concurrency_conflict(cfg):
    """任务19（C12）：v2 派发契约下嵌套/扁平并发值互相矛盾 → 写配置时就点名。

    v2 规则：``agent_dispatch.max_concurrency`` 与 ``agent_max_concurrency``
    同时为正且不一致时，派发阶段会以 CONFIG_CONCURRENCY_CONFLICT 硬失败
    （绝不静默挑一个）。这里提前在生成配置阶段给出同样的诊断，避免跑到
    派发才发现。legacy（无版本或版本 1）保持旧的「扁平优先」语义，不报冲突。
    """
    version = cfg.get("dispatch_contract_version")
    nested = cfg.get("agent_dispatch")
    if not isinstance(nested, dict):
        return []
    try:
        version_num = int(str(version).strip()) if version not in (None, "") else 1
    except (TypeError, ValueError):
        return []  # 版本本身非法由主校验器报错，这里不重复
    if version_num < 2:
        return []

    def _as_int(value):
        if isinstance(value, bool) or value is None:
            return 0
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    flat = _as_int(cfg.get("agent_max_concurrency"))
    inner = _as_int(nested.get("max_concurrency"))
    if flat > 0 and inner > 0 and flat != inner:
        return ["派发并发配置冲突 [CONFIG_CONCURRENCY_CONFLICT]: "
                "agent_dispatch.max_concurrency=%d 与 agent_max_concurrency=%d "
                "不一致；dispatch_contract_version=2 下请只保留一个（或改成 0=自动）"
                % (inner, flat)]
    return []


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
      - 任务10：若 .pd/intent-contract.json 已冻结 execution_profile，
        则 config.yaml 声明不同值即报错（冻结策略不得被当前 YAML 改写）。
    返回具体错误信息列表。
    """
    errors = []
    project_dir = os.path.abspath(project_dir)
    spec_path = os.path.join(project_dir, ".pd", "confirmed-spec.json")
    confirmed = {}
    if os.path.isfile(spec_path):
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

    # 任务10（C1 规则3/6）：执行策略一旦冻结，config.yaml 不得改写它。
    # 冻结记录写在 .pd/intent-contract.json（仅新 run 有执行字段；旧契约没有 -> 跳过）。
    contract_path = os.path.join(project_dir, ".pd", "intent-contract.json")
    if os.path.isfile(contract_path):
        contract = None
        try:
            with open(contract_path, "r", encoding="utf-8") as f:
                contract = json.load(f)
        except Exception as e:
            errors.append("读取 intent-contract.json 失败: %s" % e)
        if isinstance(contract, dict):
            frozen_prof = str(contract.get("execution_profile") or "").strip()
            cfg_exec = str(cfg.get("execution_profile") or "").strip()
            if frozen_prof and cfg_exec and cfg_exec != frozen_prof:
                errors.append(
                    "执行策略被篡改: intent-contract 已冻结 “%s”，但 config.yaml 为 “%s”"
                    "（换图必须新开 thread 或显式重新确认）" % (frozen_prof, cfg_exec)
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
