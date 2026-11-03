"""contracts.py — Phase 0 冻结契约：source-manifest.json 与 intent-contract.json。

审计依据（qchain 改造审查报告 §4.1/§4.2/§18.3）：插件必须把「读了哪些文件、
排除了哪些文件、目标模式和预算是什么」在 Phase 0 冻结为可验证的磁盘契约，
而不是让下游 Agent 自行推断：

    source-manifest.json   每个输入带 path/role/digest/include/reason；
                          排除项也必须登记（include=false），不得从清单消失。
    intent-contract.json   冻结 title/delivery_profile/lesson_minutes/
                          content_policy/must_cover/allowed_extensions/reorder_allowed。

插件必须拒绝（本模块以 ContractError 抛出稳定错误码）：
    SOURCE_IS_GENERATED   source-baseline 指向已生成文件或中间稿（self/ 内、
                          .pd/、.langgraph/ 或构建副产物后缀）；
    SOURCE_DUPLICATE_ROLE 同一文件同时被标为 source 与 excluded；
    SOURCE_MISSING        源文件不存在；
    SOURCE_UNDIGESTED     源文件不可读（digest 为空）；
    INVALID_CONTENT_POLICY content_policy 不是 faithful|authoring。

下游校验（F3 check_fidelity / T1 输入装配复用）：
    verify_source_manifest  重算 digest，返回与冻结清单不一致的条目；
    verify_contract_pair    对账 manifest 与 contract 的 source/excluded 列表。

本模块不依赖 langgraph，可被 workflow 节点与 scripts 门禁双向导入。
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

#: 契约 schema 版本（rule version 的一部分，进入 cache key）
CONTRACT_SCHEMA_VERSION = 1

#: 契约文件相对 self_dir 的位置
MANIFEST_REL = os.path.join(".pd", "source-manifest.json")
CONTRACT_REL = os.path.join(".pd", "intent-contract.json")

#: 内容策略：faithful=忠实转换（默认），authoring=显式扩写授权
CONTENT_POLICIES = ("faithful", "authoring")
DEFAULT_CONTENT_POLICY = "faithful"

#: 任务10（C1）：执行契约冻结元数据字段（与 workflow/execution_policy.py 的
#: FROZEN_FIELDS 同序）。此处独立声明，避免 contracts 反向依赖执行策略模块。
EXECUTION_FROZEN_FIELDS = (
    "execution_schema_version",
    "execution_profile",
    "graph_version",
    "policy_digest",
    "execution_config",
)

#: 任务19（C12）：派发契约冻结字段。与执行契约同批写入 intent-contract.json 的
#: ``dispatch`` 子块；旧契约没有该块时一律按 legacy 派发契约（版本 1）处理，
#: 因此这里绝不能并入 EXECUTION_FROZEN_FIELDS（否则旧契约会被判为不完整）。
DISPATCH_FROZEN_FIELDS = (
    "dispatch_contract_version",
    "profile",
    "configured_concurrency",
    "concurrency_source",
)

#: 已生成文件/中间稿的构建副产物后缀（不得作为 source-baseline）
GENERATED_SUFFIXES = (
    ".pdf", ".html", ".aux", ".log", ".out", ".synctex", ".synctex.gz",
    ".fls", ".fdb_latexmk", ".toc", ".nav", ".snm", ".vrb", ".xdv",
)

#: 路径中出现这些目录成分即视为生成物工作区（self/ 与其状态目录）
GENERATED_DIR_NAMES = ("self", ".pd", ".langgraph")

#: 明确的中间稿/生成物文件名（无论位于何处都不得作为 source-baseline）
GENERATED_BASENAMES = frozenset({
    "source-map.md", "coverage-map.md", "dependency-map.md",
    "run.json", "confirmed-spec.json", "source-manifest.json",
    "intent-contract.json", "source-inventory.json", "dispatch-plan.json",
    "workspace.json",
})


class ContractError(ValueError):
    """契约拒绝：code 为稳定错误码，detail 为人可读原因。"""

    def __init__(self, code: str, detail: str):
        super().__init__("[%s] %s" % (code, detail))
        self.code = code
        self.detail = detail


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_digest(path: str) -> str:
    """文件 sha256 摘要（格式 sha256:<hex>）；不可读返回空串。"""
    if not path or not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return "sha256:" + h.hexdigest()


def canonical_digest(obj: Any) -> str:
    """任意 JSON 对象的规范化 sha256（排序键、无空白）。"""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_list(value: Any) -> List[str]:
    """配置值规范化为去空去重字符串列表（保持顺序）。

    接受 None / list / 逗号或换行分隔字符串。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        items = [x.strip() for x in re.split(r"[,;\n]", str(value))]
    out: List[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def parse_excluded_entries(value: Any) -> List[Tuple[str, str]]:
    """解析排除项清单为 (path, reason)。

    支持 "path :: reason" / "path|reason" / 纯 "path"（原因留默认说明）。
    """
    out: List[Tuple[str, str]] = []
    for entry in _split_list(value):
        reason = ""
        path = entry
        for sep in ("::", "|"):
            if sep in entry:
                path, _, reason = entry.partition(sep)
                path, reason = path.strip(), reason.strip()
                break
        if path:
            out.append((path, reason or "用户明确排除（未注明理由）"))
    return out


def _abspath(project_root: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(project_root, path)


def _display_path(project_root: str, abspath: str) -> str:
    """清单内展示路径：位于 project_root 内则相对化，否则保留绝对路径。"""
    try:
        rel = os.path.relpath(abspath, project_root)
    except ValueError:
        return abspath
    if rel.startswith(".."):
        return abspath
    return rel.replace(os.sep, "/")


def generated_reason(project_root: str, abspath: str) -> Optional[str]:
    """返回「该路径为何是生成物/中间稿」的原因；不是生成物返回 None。"""
    norm = abspath.replace(os.sep, "/")
    parts = [p for p in norm.split("/") if p]
    for name in GENERATED_DIR_NAMES:
        if name in parts:
            return "位于生成物工作区目录 %s/ 内" % name
    base = os.path.basename(norm)
    if base in GENERATED_BASENAMES:
        return "属于插件生成/中间稿文件名 %s" % base
    for suffix in GENERATED_SUFFIXES:
        if norm.lower().endswith(suffix):
            return "属于构建副产物后缀 %s" % suffix
    return None


def build_source_manifest(*, project_root: str, self_dir: str = "",
                          source_files: Any = None,
                          excluded_sources: Any = None,
                          title: str = "") -> Dict[str, Any]:
    """构建冻结的 source-manifest（audit §4.1）。

    source_files 中每个路径成为 role=source-baseline / include=true 条目；
    excluded_sources 中每个路径成为 include=false 条目（reason 必填语义）。
    任何违规以 ContractError 拒绝，绝不静默丢弃。
    """
    project_root = os.path.abspath(project_root or ".")
    src_paths = _split_list(source_files)
    exc_entries = parse_excluded_entries(excluded_sources)

    seen_src: Dict[str, str] = {}
    seen_exc: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []

    for raw in src_paths:
        abspath = _abspath(project_root, raw)
        key = os.path.normcase(os.path.abspath(abspath))
        if key in seen_src:
            raise ContractError(
                "SOURCE_DUPLICATE",
                "原材料 %r 在 source_files 中重复登记" % raw)
        seen_src[key] = raw
        reason = generated_reason(project_root, abspath)
        if reason:
            raise ContractError(
                "SOURCE_IS_GENERATED",
                "source-baseline %r 指向已生成文件或中间稿（%s）；"
                "原材料必须是用户确认的原始输入" % (raw, reason))
        if not os.path.isfile(abspath):
            raise ContractError("SOURCE_MISSING", "原材料 %r 不存在" % raw)
        digest = file_digest(abspath)
        if not digest:
            raise ContractError("SOURCE_UNDIGESTED", "原材料 %r 不可读，无法计算 digest" % raw)
        entries.append({
            "path": _display_path(project_root, abspath),
            "role": "source-baseline",
            "digest": digest,
            "include": True,
            "reason": "用户确认的原材料",
        })

    for raw, reason in exc_entries:
        abspath = _abspath(project_root, raw)
        key = os.path.normcase(os.path.abspath(abspath))
        if key in seen_src:
            raise ContractError(
                "SOURCE_DUPLICATE_ROLE",
                "文件 %r 同时被登记为 source-baseline 与排除项" % raw)
        if key in seen_exc:
            continue  # 同一排除路径重复列出：保留首条理由
        seen_exc[key] = raw
        digest = file_digest(abspath)
        entries.append({
            "path": _display_path(project_root, abspath),
            "role": "optional-problem",
            "digest": digest,
            "include": False,
            "reason": reason,
        })

    manifest = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "frozen_at": _iso_now(),
        "title": str(title or ""),
        "project_root": project_root,
        "sources": entries,
    }
    # 自摘要排除 frozen_at/digest：digest 是内容的纯函数（cache key 与
    # 跨运行对账需要稳定性；冻结时间只留痕，不参与身份）。
    manifest["digest"] = canonical_digest(
        {k: v for k, v in manifest.items() if k not in ("digest", "frozen_at")})
    return manifest


def build_intent_contract(*, user_spec: Optional[Dict[str, Any]],
                          execution: Optional[Dict[str, Any]] = None,
                          dispatch: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
    """构建冻结的 intent-contract（audit §4.2/§18.3）。

    self_study / lesson_minutes / 137 分钟 core 等值在此冻结，下游 Agent
    不得推断或覆盖；扩写必须经 allowed_extensions 显式批准。

    任务10（DATA-CONTRACTS C1）：`execution` 给出时把执行契约（执行图版本 /
    有效执行配置 / 策略摘要）一并冻结进契约；**缺省 None 保持旧契约形状**
    （旧工程的 .pd/intent-contract.json 没有执行字段，摘要不能被凭空改写）。
    一旦给出就必须完整——残缺块显式拒绝，不猜默认值。

    任务19（DATA-CONTRACTS C12）：`dispatch` 给出时把派发契约（契约版本 /
    档位 / 配置原值 / 生效来源）冻结成契约的 ``dispatch`` 子块。缺省 None
    保持旧契约形状——旧检查点没有该块，按 legacy 派发契约（版本 1）继续。
    """
    spec = dict(user_spec or {})
    policy = str(spec.get("content_policy") or DEFAULT_CONTENT_POLICY).strip().lower()
    if policy not in CONTENT_POLICIES:
        raise ContractError(
            "INVALID_CONTENT_POLICY",
            "content_policy=%r 非法，只能是: %s"
            % (spec.get("content_policy"), " / ".join(CONTENT_POLICIES)))

    extensions: List[Dict[str, str]] = []
    for item in spec.get("allowed_extensions") or []:
        if isinstance(item, dict):
            extensions.append({
                "id": str(item.get("id") or "").strip(),
                "scope": str(item.get("scope") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "approver": str(item.get("approver") or "").strip(),
            })
        else:
            text = str(item).strip()
            if text:
                extensions.append({"id": text, "scope": "", "reason": "", "approver": ""})

    profile = str(spec.get("delivery_profile") or "self_study")
    lesson_minutes = spec.get("lesson_minutes")
    if profile in ("teaching", "classroom") and lesson_minutes is None:
        lesson_minutes = 90

    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "frozen_at": _iso_now(),
        "title": str(spec.get("main_title") or "学案"),
        "delivery_profile": profile,
        "lesson_minutes": lesson_minutes,
        "content_policy": policy,
        "must_cover": _split_list(spec.get("must_cover")),
        # faithful 模式一律禁止未经确认的扩展（不可被 spec 关闭）
        "must_not_expand_without_confirmation": policy != "authoring",
        "allowed_extensions": extensions,
        "reorder_allowed": bool(spec.get("reorder_allowed")),
        "student_level": str(spec.get("student_level") or ""),
        "source_files": _split_list(spec.get("source_files")),
        "excluded_sources": [raw for raw, _ in parse_excluded_entries(
            spec.get("excluded_sources"))],
    }
    if execution is not None:
        contract.update(_freeze_execution_block(execution))
    if dispatch is not None:
        contract["dispatch"] = _freeze_dispatch_block(dispatch)
    contract["digest"] = canonical_digest(
        {k: v for k, v in contract.items() if k not in ("digest", "frozen_at")})
    return contract


def _freeze_dispatch_block(dispatch: Dict[str, Any]) -> Dict[str, Any]:
    """校验并挑出 C12 冻结派发契约块（与 workflow/dispatch_policy.py 同字段）。

    ``configured_concurrency`` 允许为 0（= 自动，按档位表取并发），所以这里
    只用「键必须存在」判定完整性，不能用 ``in (None, "")``——那会把合法的
    0 误判为残缺。版本号非法/缺失仍然显式拒绝。
    """
    block = dict(dispatch or {})
    missing = [field for field in DISPATCH_FROZEN_FIELDS
               if field not in block or block[field] is None
               or (field != "configured_concurrency" and block[field] == "")]
    if missing:
        raise ContractError(
            "INCOMPLETE_DISPATCH_METADATA",
            "派发契约字段不完整，缺少: %s（拒绝按猜测的默认值冻结）"
            % ", ".join(missing))
    return {field: block[field] for field in DISPATCH_FROZEN_FIELDS}


def _freeze_execution_block(execution: Dict[str, Any]) -> Dict[str, Any]:
    """校验并挑出 C1 冻结执行元数据块（字段顺序与 C1 一致）。"""
    block = dict(execution or {})
    missing = [field for field in EXECUTION_FROZEN_FIELDS
               if block.get(field) in (None, "")]
    if missing:
        raise ContractError(
            "INCOMPLETE_EXECUTION_METADATA",
            "执行契约字段不完整，缺少: %s（拒绝按猜测的默认值冻结）"
            % ", ".join(missing))
    return {field: block[field] for field in EXECUTION_FROZEN_FIELDS}


def manifest_entries(manifest: Optional[Dict[str, Any]],
                     include: Optional[bool] = None) -> List[Dict[str, Any]]:
    """按 include 过滤清单条目（None=全部）。"""
    entries = ((manifest or {}).get("sources") or [])
    if include is None:
        return [e for e in entries if isinstance(e, dict)]
    return [e for e in entries
            if isinstance(e, dict) and bool(e.get("include")) is include]


def verify_source_manifest(manifest: Optional[Dict[str, Any]],
                           project_root: str) -> List[Dict[str, Any]]:
    """下游 digest 对账（audit §4.1 末条）：重算磁盘 digest，返回不一致条目。"""
    if not manifest:
        return []
    project_root = os.path.abspath(project_root or ".")
    mismatches: List[Dict[str, Any]] = []
    for entry in manifest_entries(manifest):
        path = str(entry.get("path") or "")
        if not path:
            continue
        abspath = _abspath(project_root, path)
        actual = file_digest(abspath)
        expected = str(entry.get("digest") or "")
        if actual != expected:
            mismatches.append({
                "path": path,
                "expected": expected,
                "actual": actual or None,
            })
    return mismatches


def verify_contract_pair(manifest: Optional[Dict[str, Any]],
                         contract: Optional[Dict[str, Any]]) -> List[str]:
    """对账 manifest 与 contract 的 source/excluded 列表一致性（防篡改）。"""
    errors: List[str] = []
    if not manifest or not contract:
        return ["契约缺失：source-manifest 或 intent-contract 为空"]
    m_src = sorted(e.get("path") or "" for e in manifest_entries(manifest, True))
    m_exc = sorted(e.get("path") or "" for e in manifest_entries(manifest, False))
    c_src = sorted(str(p) for p in (contract.get("source_files") or []))
    c_exc = sorted(str(p) for p in (contract.get("excluded_sources") or []))
    if m_src != c_src:
        errors.append("source_files 不一致: manifest=%s contract=%s" % (m_src, c_src))
    if m_exc != c_exc:
        errors.append("excluded_sources 不一致: manifest=%s contract=%s" % (m_exc, c_exc))
    return errors


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%s" % (path, os.urandom(4).hex())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def write_contracts(self_dir: str, manifest: Dict[str, Any],
                    contract: Dict[str, Any]) -> Tuple[str, str]:
    """原子写入两份冻结契约到 <self_dir>/.pd/，返回 (manifest_path, contract_path)。"""
    self_dir = self_dir or "."
    m_path = _atomic_write_json(os.path.join(self_dir, MANIFEST_REL), manifest)
    c_path = _atomic_write_json(os.path.join(self_dir, CONTRACT_REL), contract)
    return m_path, c_path


def load_contracts(self_dir: str) -> Tuple[Optional[Dict[str, Any]],
                                           Optional[Dict[str, Any]]]:
    """读取冻结契约；缺失或损坏返回 None（调用方按未冻结处理）。"""
    def _load(rel: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self_dir or ".", rel)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    return _load(MANIFEST_REL), _load(CONTRACT_REL)


def scope_summary(manifest: Optional[Dict[str, Any]],
                  contract: Optional[Dict[str, Any]]) -> str:
    """Phase 0 展示用的一行范围摘要（输入数/排除数/策略/预算）。"""
    if not manifest or not contract:
        return "契约未冻结"
    included = manifest_entries(manifest, True)
    excluded = manifest_entries(manifest, False)
    minutes = contract.get("lesson_minutes")
    budget = ("%s 分钟" % minutes) if minutes else "无时长上限"
    return ("范围: %d 个原材料(include) / %d 个排除(include=false) | "
            "内容策略: %s | 模式: %s | 预算: %s | must_cover: %d 项"
            % (len(included), len(excluded),
               contract.get("content_policy"),
               contract.get("delivery_profile"),
               budget,
               len(contract.get("must_cover") or [])))
