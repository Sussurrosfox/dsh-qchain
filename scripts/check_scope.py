"""check_scope.py — 范围与预算一致性门禁（audit §7 scope_gate）。

背景：一次运行中「self_study/90/137 分钟 core」这类目标值曾被 Agent 推断
覆盖（q_audit §2）。F0 已在 Phase 0 冻结 intent-contract.json；本门禁在
交付前核对「生效配置」与「冻结合同」零漂移，任何未经人工确认的范围变更
一律阻断：

  SCOPE_CONTRACT_MISSING        缺少 .pd/intent-contract.json（Phase 0 未冻结）
  SCOPE_CONTRACT_BAD_JSON        合同文件损坏（无法解析）
  SCOPE_CONTRACT_TAMPERED        合同内容被改写（规范化摘要与冻结 digest 不符）
  SCOPE_PROFILE_DRIFT            config.yaml 的 delivery_profile 与合同不一致
  SCOPE_MINUTES_DRIFT            生效课时预算与合同不一致（self_study 视作无上限）
  SCOPE_UNAUTHORIZED_DOWNGRADE   confirmed-spec 的交付模式与合同不一致
                                （teaching/classroom 被降级为 self_study 等）
  SCOPE_POLICY_DRIFT             content_policy 与合同不一致
  SCOPE_CHANGE_UNAPPROVED        .pd/scope-change.json 存在未批准的扩展请求
  SCOPE_UNPLANNED_SECTION        main.tex 引入了 coverage-map 未规划的小节
  SCOPE_CORE_BUDGET_OVERFLOW     coverage-map 的核心推导总分钟数超出合同预算

输入（workdir = self/ 目录）：
- .pd/intent-contract.json   Phase 0 冻结的目标合同
- .pd/confirmed-spec.json    Phase 0 人工确认态（缺省时跳过降级核对）
- .pd/scope-change.json      范围变更请求台账（存在时逐条核对批准人）
- config.yaml                生效配置（PyYAML 优先，扁平 key: value 兜底）
- main.tex / coverage-map.md 节数与规划核对

用法: python check_scope.py [工作目录] [--json PATH]
退出码: 0 = 通过, 1 = 存在阻断性问题, 2 = 输入缺失（合同未冻结）。
"""

import argparse
import hashlib
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import evidence  # noqa: E402（scripts/ 已在 sys.path）

TOOL_NAME = "check_scope.py"
TOOL_VERSION = "1.0"

CONTRACT_REL = os.path.join(".pd", "intent-contract.json")
CONFIRMED_SPEC_REL = os.path.join(".pd", "confirmed-spec.json")
SCOPE_CHANGE_REL = os.path.join(".pd", "scope-change.json")
CONFIG_REL = "config.yaml"
COVERAGE_MAP_REL = "coverage-map.md"

RE_MAIN_INPUT = re.compile(r"^\s*\\input\{(sec[0-9A-Za-z_-]+)\.tex\}", re.MULTILINE)
#: coverage-map 统计行：**核心推导总分钟数**: 137 分钟
RE_CORE_MINUTES = re.compile(r"核心推导总分钟数[^\d]{0,20}(\d+)\s*分钟")

#: teaching/classroom 才设时长上限；self_study 一律视作无预算（audit §4.2）
BUDGETED_PROFILES = ("teaching", "classroom")


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _canonical_digest(obj):
    """与 workflow.contracts.canonical_digest 同口径的规范化摘要。"""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_flat_config(self_dir):
    """扁平 config.yaml 解析（PyYAML 不可用时的兜底，跳过缩进行）。"""
    path = os.path.join(self_dir, CONFIG_REL)
    cfg = {}
    if not os.path.isfile(path):
        return cfg
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip() or line[:1] in (" ", "\t", "#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            cfg[key.strip()] = value.split("#", 1)[0].strip().strip("\"'")
    return cfg


def load_effective_config(self_dir):
    """生效配置：PyYAML 优先（支持 agent_dispatch 嵌套块），扁平兜底。"""
    try:
        import gen_config  # noqa: E402（scripts/ 已在 sys.path）
        cfg = gen_config.load_config(self_dir)
        if isinstance(cfg, dict) and cfg:
            return cfg
    except Exception:
        pass
    return _load_flat_config(self_dir)


def _input_digest(self_dir):
    """受审输入摘要（与 deliver.py 的 input_digest 绑定口径一致）。"""
    try:
        return evidence.content_digest(evidence.collect_inputs(self_dir))
    except Exception:
        return ""


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _profile(cfg):
    return str((cfg or {}).get("delivery_profile") or "").strip().lower()


def _effective_minutes(cfg):
    """生效课时预算：非预算模式一律 None（配置里的数字不生效）。"""
    profile = _profile(cfg)
    if profile not in BUDGETED_PROFILES:
        return None
    return _as_int((cfg or {}).get("lesson_minutes"))


def _main_sections(self_dir):
    path = os.path.join(self_dir, "main.tex")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return RE_MAIN_INPUT.findall(f.read())


def check_scope_project(workdir):
    """返回 {errors, warnings, checked, workdir}。"""
    self_dir = os.path.abspath(workdir)
    errors, warnings = [], []

    contract = _load_json(os.path.join(self_dir, CONTRACT_REL))
    if contract is None:
        return {
            "errors": [{"code": "SCOPE_CONTRACT_MISSING",
                        "detail": "缺少或无法解析 %s（Phase 0 未冻结目标合同，"
                                  "不得交付）" % CONTRACT_REL}],
            "warnings": [],
            "checked": {"contract": False},
            "workdir": self_dir,
            "fatal": True,
        }

    # ---- 1. 合同防篡改（规范化摘要对账）----
    stored = str(contract.get("digest") or "")
    recomputed = _canonical_digest(
        {k: v for k, v in contract.items() if k not in ("digest", "frozen_at")})
    if not stored or stored != recomputed:
        errors.append({
            "code": "SCOPE_CONTRACT_TAMPERED",
            "detail": "intent-contract.json 的规范化摘要与冻结 digest 不一致"
                      "（期望 %s，实际 %s）——合同内容已被改写" % (stored, recomputed)})

    cfg = load_effective_config(self_dir)

    # ---- 2. 交付模式漂移 ----
    cfg_profile = _profile(cfg)
    contract_profile = str(contract.get("delivery_profile") or "").strip().lower()
    if cfg_profile and cfg_profile != contract_profile:
        errors.append({
            "code": "SCOPE_PROFILE_DRIFT",
            "detail": "config.yaml delivery_profile=%s 与冻结合同 %s 不一致"
                      % (cfg_profile, contract_profile)})

    # ---- 3. 课时预算漂移（生效口径）----
    cfg_minutes = _effective_minutes(cfg)
    contract_minutes = _as_int(contract.get("lesson_minutes"))
    if cfg_minutes != contract_minutes:
        errors.append({
            "code": "SCOPE_MINUTES_DRIFT",
            "detail": "生效课时预算 %s 与冻结合同 %s 不一致"
                      % (cfg_minutes, contract_minutes)})

    # ---- 4. 人工确认态降级核对（teaching -> self_study 等）----
    confirmed = _load_json(os.path.join(self_dir, CONFIRMED_SPEC_REL))
    if confirmed is not None:
        spec_profile = str(confirmed.get("delivery_profile") or "").strip().lower()
        if spec_profile and spec_profile != contract_profile:
            errors.append({
                "code": "SCOPE_UNAUTHORIZED_DOWNGRADE",
                "detail": ".pd/confirmed-spec.json 的 delivery_profile=%s 与"
                          "冻结合同 %s 不一致（未经确认的交付模式变更）"
                          % (spec_profile, contract_profile)})
        spec_minutes = _as_int(confirmed.get("lesson_minutes"))
        if spec_profile in BUDGETED_PROFILES and spec_minutes != contract_minutes:
            errors.append({
                "code": "SCOPE_UNAUTHORIZED_DOWNGRADE",
                "detail": ".pd/confirmed-spec.json 的 lesson_minutes=%s 与"
                          "冻结合同 %s 不一致" % (spec_minutes, contract_minutes)})

    # ---- 5. 内容策略漂移 ----
    cfg_policy = str((cfg or {}).get("content_policy") or "").strip().lower()
    contract_policy = str(contract.get("content_policy") or "").strip().lower()
    if cfg_policy and cfg_policy != contract_policy:
        errors.append({
            "code": "SCOPE_POLICY_DRIFT",
            "detail": "config.yaml content_policy=%s 与冻结合同 %s 不一致"
                      % (cfg_policy, contract_policy)})

    # ---- 6. 范围变更请求必须停在人工确认点 ----
    scope_change = _load_json(os.path.join(self_dir, SCOPE_CHANGE_REL))
    pending_changes = []
    if scope_change is not None:
        entries = scope_change.get("changes") or scope_change.get("entries") or []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not str(entry.get("approved_by") or entry.get("approver") or "").strip():
                pending_changes.append(entry)
    if pending_changes:
        errors.append({
            "code": "SCOPE_CHANGE_UNAPPROVED",
            "detail": ".pd/scope-change.json 有 %d 条未批准的范围变更请求"
                      "（scope=%s）——必须停在人工确认点，不得直接交付"
                      % (len(pending_changes),
                         ", ".join(str(e.get("scope") or "?")
                                   for e in pending_changes[:5]))})

    # ---- 7. 节数规划核对（main.tex 不得引入未规划小节）----
    sections = _main_sections(self_dir)
    coverage_path = os.path.join(self_dir, COVERAGE_MAP_REL)
    coverage_text = ""
    if os.path.isfile(coverage_path):
        with open(coverage_path, encoding="utf-8", errors="replace") as f:
            coverage_text = f.read()
    unplanned = [s for s in sections if s not in coverage_text]
    if unplanned:
        errors.append({
            "code": "SCOPE_UNPLANNED_SECTION",
            "detail": "main.tex 引入了 coverage-map.md 未规划的小节: %s"
                      % ", ".join(sorted(set(unplanned)))})

    # ---- 8. 核心预算核对（合同设上限时）----
    core_minutes = None
    m = RE_CORE_MINUTES.search(coverage_text)
    if m:
        core_minutes = _as_int(m.group(1))
    if contract_minutes is not None and core_minutes is not None \
            and core_minutes > contract_minutes:
        errors.append({
            "code": "SCOPE_CORE_BUDGET_OVERFLOW",
            "detail": "coverage-map 核心推导总分钟数 %d 超出合同预算 %d 分钟"
                      % (core_minutes, contract_minutes)})

    if not contract.get("must_cover"):
        warnings.append({"code": "SCOPE_MUST_COVER_EMPTY",
                         "detail": "intent contract 的 must_cover 为空，"
                                   "覆盖核对将缺少锚点"})

    return {
        "errors": errors,
        "warnings": warnings,
        "checked": {
            "contract": True,
            "delivery_profile": contract_profile,
            "lesson_minutes": contract_minutes,
            "content_policy": contract_policy,
            "sections": sections,
            "core_minutes": core_minutes,
            "scope_change_pending": len(pending_changes),
        },
        "workdir": self_dir,
        "fatal": False,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="范围与预算一致性门禁 (check_scope.py)")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="self 工作区目录（默认当前目录）")
    ap.add_argument("--json", metavar="PATH", help="写入结构化检查报告")
    args = ap.parse_args(argv)

    res = check_scope_project(args.workdir)
    errors, warnings = res["errors"], res["warnings"]

    report = {
        # 与 evidence.SCHEMA_VERSION 对齐（deliver.py 只接受 schema 2）
        "schema_version": 2,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        # deliver.py 逐报告核对 input_digest 绑定（防旧报告复用）
        "input_digest": _input_digest(res["workdir"]),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "checked": res["checked"],
    }

    if args.json:
        json_dir = os.path.dirname(os.path.abspath(args.json))
        if json_dir:
            os.makedirs(json_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[check_scope] 结构化报告已写入 %s" % args.json)

    for w in warnings:
        print("[check_scope] [警告] %s: %s" % (w["code"], w["detail"]))

    if errors:
        print("[check_scope] 发现 %d 个范围/预算一致性问题（工作区: %s）:"
              % (len(errors), res["workdir"]))
        for e in errors:
            print("  - [FAIL] %s: %s" % (e["code"], e["detail"]))
        return 1

    checked = res["checked"]
    print("[check_scope] 通过：范围与预算零漂移（模式 %s / 预算 %s / %d 节）。"
          % (checked["delivery_profile"], checked["lesson_minutes"],
             len(checked["sections"])))
    return 0


check_scope = check_scope_project


if __name__ == "__main__":
    sys.exit(main())
