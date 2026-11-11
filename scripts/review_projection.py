#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""review_projection.py — 合并终审主产物校验 + 确定性证据投影（C6/C7，修复卡 14）。

背景（复盘报告「重复审读」根因）：旧流程每节派一个语义审读员、再每节派一个
验收员，同一份候选被读两遍、写两套证据；而两套证据之间没有任何可核对的
父子关系，主 Agent 只要自己写一份 `conclusion=PASS` 的 JSON 就能过门。修复
卡 14 把这两轮合成**一次独立审读覆盖多节**：

    .pd/combined-review/<batch_id>-r<N>.json      ← 独立审读员的真实主产物
        ↓（宿主确定性投影，可重算、带 derived_from 溯源）
    .pd/semantic/<sec>.json                       语义硬闸门证据（v1 基础字段 + derived_from）
    .pd/acceptance/<sec>.json                     验收硬闸门证据（v2 + review_kind + 双证据引用）

本模块是这条链路的唯一实现，职责分三层：

1. `validate_combined_review()` —— 零信任校验主产物：schema_version=2 /
   review_kind / scopes 全覆盖 / 逐节 qid 全覆盖 / section_digest 与候选一致
   （打乱血缘仅豁免「摘要不一致」这一项）/ 两个结论均 PASS / 无 open issue /
   交付产物真实指纹（html-only 显式不含 PDF，禁零 hash 伪证）。
2. `build_projections()` / `write_projections()` —— 确定性投影：同一主产物 +
   同一磁盘状态必须得到逐字节相同的两份证据；投影不写任何"当前时间"、
   不臆造历史，全部结论都从主产物搬运。
3. `verify_projection()` —— 认证：先复算（投影必须能由父产物重算出来），
   再回查 `.pd/agent-calls/<dispatch_id>.json` 真实证据台账（status=ok、
   output_digests 登记的父产物摘要 = 磁盘实际摘要、worker 与写作者集合不相交）。

硬规则（C7）：`content_review_refs` 记录审读员**当时**看到的产物指纹，
`delivery_check_ref` 记录**本轮**磁盘实测产物指纹，两者必须分离；绝不修改旧
审读的 artifact_digests 去假装它见过新产物。旧 v1 证据（无 derived_from）
一律走原路径，本模块不碰；不兼容的版本号显式失败，不静默改写历史报告。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PLUGIN_ROOT = os.path.dirname(_HERE)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import ledger  # noqa: E402  （N2：advisory 延期台账，投影时读生效条目）

TOOL_NAME = "review_projection.py"
TOOL_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 版本与常量
# ---------------------------------------------------------------------------

#: 合并终审主产物 schema 版本（与派发单 schema 2 是不同层，各自独立版本号）
REVIEW_SCHEMA_VERSION = 2
#: 合并终审种类：一次审读同时承担「来源忠实性 + 数学正确性 + 交付证据」
REVIEW_KIND = "combined_source_fidelity"
#: 合法结论（与 semantic/acceptance 同一集合）
CONCLUSIONS = ("PASS", "REVISE", "NEEDS_EVIDENCE")
#: issue 生命周期状态（沿用 T4 验收语义）
ISSUE_STATUSES = ("open", "fixed", "withdrawn")
#: 投影 schema 版本：未知版本一律显式拒绝，绝不静默改写
PROJECTION_SCHEMA_VERSION = 1
PROJECTOR = "review_projection.py"
PROJECTOR_VERSION = "1.0"

#: 合并终审主产物目录（相对于 self_dir）
REVIEW_DIR_REL = ".pd/combined-review"
#: 投影落盘目录（与旧轮次同路径：下游闸门无需改读取口径）
SEMANTIC_DIR_REL = ".pd/semantic"
ACCEPTANCE_DIR_REL = ".pd/acceptance"
#: 审读员提示词模板
REVIEW_PROMPT_FILE = "combined_review_prompt.md"
#: 审读角色名前缀（C6：身份必须显式角色化，不能只靠批次名字符串区分）
REVIEWER_ROLE_PREFIX = "合并终审专员"
#: 认可为「审读阶段」的派发 stage（角色化的判据真源）
REVIEWER_STAGES = ("T4_COMBINED_REVIEW",)
#: 审读阶段的族前缀：旧 T4 逐节验收（T4_REVIEW_<SEC>）本身也是审读角色，
#: 不能因为它是旧轮次就被算成「写作者」（否则同一 worker 会被误判同体）。
REVIEWER_STAGE_PREFIXES = ("T4_REVIEW_",)
#: 审读批次 ID 前缀（`bat-t4` 覆盖旧 T4 逐节验收批与 `bat-t4c` 合并终审批）
REVIEW_BATCH_PREFIXES = ("bat-t4",)
#: 合并终审 fan-in 汇总角色
AGGREGATE_REVIEWER_ROLE = "合并终审 fan-in 汇总（combined-reviewer × N）"

#: 各输出格式的必备交付产物：html-only 不要求 PDF，也禁止用零 hash 伪证
FORMAT_ARTIFACTS = {
    "html": ("student.html",),
    "pdf": ("student.pdf",),
    "both": ("student.pdf", "student.html"),
}
OUTPUT_FORMATS = tuple(FORMAT_ARTIFACTS)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
#: 零摘要：任何场合都不得当作「已核验」的指纹（html-only 不得伪造 PDF 证明）
_ZERO_DIGEST = "0" * 64

#: v2 验收投影必须携带的键（缺一不可；见 evidence.validate_acceptance_json）
ACCEPTANCE_V2_REQUIRED = (
    "schema_version", "review_kind", "scope", "reviewed_qids", "content_digest",
    "output_format", "conclusion", "reviewer_role", "issues",
    "source_comparison", "content_review_refs", "delivery_check_ref",
    "derived_from",
)
#: issue 追溯字段（与 scripts/evidence.py 的 _ISSUE_REQUIRED 同口径）
ISSUE_REQUIRED = ("id", "qid", "part", "code", "evidence", "status",
                  "resolution", "verified_digest")
#: withdrawn 时不许拿「打太极」措辞冒充已处理（与 v1 校验同口径）
_VAGUE_RESOLUTIONS = ("综合考虑", "已处理")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> Optional[str]:
    """文件 sha256；不存在/不可读返回 None（绝不返回零摘要冒充）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _is_digest(value: Any) -> bool:
    """真实指纹判定：64 位小写十六进制且不是全零占位。"""
    return (isinstance(value, str) and bool(_DIGEST_RE.match(value))
            and value != _ZERO_DIGEST)


def _norm_rel(rel: str) -> str:
    return str(rel or "").replace("\\", "/")


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: str, payload: Any) -> bool:
    """原子写 JSON（tmp + os.replace）；失败返回 False，不留下半个文件。"""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _agent_evidence():
    """惰性取 workflow.nodes.agent_evidence（避免与本包 __init__ 形成导入环）。"""
    from workflow.nodes import agent_evidence
    return agent_evidence


def _check_semantic():
    """惰性取 check_semantic（打乱血缘台账口径的唯一实现）。"""
    import check_semantic
    return check_semantic


def review_artifact_rel(batch_id: str, index: int = 1) -> str:
    """合并终审主产物相对路径（批次内第 index 个审读单元的独占产物）。"""
    return "%s/%s-r%d.json" % (_norm_rel(REVIEW_DIR_REL), str(batch_id or "batch"),
                               max(1, int(index)))


def combined_reviewer_role(scopes: List[str]) -> str:
    """合并终审角色名：显式角色化（C6），不只是批次名字符串。"""
    clean = [str(s) for s in (scopes or []) if isinstance(s, str) and s]
    label = "+".join(clean) if clean else "<batch>"
    return "%s（%s｜数学正确性 × 来源忠实性 × 交付证据 一次审读）" % (
        REVIEWER_ROLE_PREFIX, label)


# ---------------------------------------------------------------------------
# 角色化身份判定（创审分离的唯一口径）
# ---------------------------------------------------------------------------

def is_reviewer_stage(stage: Any) -> bool:
    text = str(stage or "").strip().upper()
    if not text:
        return False
    if text in REVIEWER_STAGES:
        return True
    return text.startswith(tuple(p.upper() for p in REVIEWER_STAGE_PREFIXES))


def is_reviewer_batch(batch_id: Any) -> bool:
    text = str(batch_id or "")
    return bool(text) and text.startswith(tuple(REVIEW_BATCH_PREFIXES))


def request_stage(self_dir: str, dispatch_id: str) -> str:
    """读派发单声明的 stage（角色化判据；缺失返回空串）。"""
    did = str(dispatch_id or "").strip()
    if not did:
        return ""
    data = _load_json(os.path.join(self_dir or ".", ".pd", "agent-requests",
                                   did + ".json"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("stage") or "")


def is_reviewer_evidence(self_dir: str, evidence: Dict[str, Any]) -> bool:
    """该条证据是否属于审读角色（stage 优先，批次前缀兜底）。

    卡14：不能只靠 batch_id 字符串区分——派发单声明的 stage 才是角色真源；
    批次前缀只是兼容旧台账（请求文件可能已被清理）。
    """
    if not isinstance(evidence, dict):
        return False
    if is_reviewer_stage(request_stage(self_dir, evidence.get("dispatch_id"))):
        return True
    return is_reviewer_batch(evidence.get("batch_id"))


def writer_workers(self_dir: str) -> set:
    """证据台账中的写作者 worker 集合（排除审读角色）。"""
    out = set()
    try:
        entries = _agent_evidence().scan_evidence(self_dir)
    except Exception:                     # pragma: no cover - 防御性
        return out
    for ev in entries:
        worker = str(ev.get("worker") or "").strip()
        if worker and not is_reviewer_evidence(self_dir, ev):
            out.add(worker)
    return out


# ---------------------------------------------------------------------------
# 磁盘指纹
# ---------------------------------------------------------------------------

def disk_artifact_digests(self_dir: str,
                          output_format: Optional[str] = None) -> Dict[str, Optional[str]]:
    """本轮交付产物在磁盘上的真实指纹（缺失 → None，不伪造 0）。"""
    fmt = str(output_format or "").strip()
    rels = FORMAT_ARTIFACTS.get(fmt) or tuple(
        sorted({rel for group in FORMAT_ARTIFACTS.values() for rel in group}))
    return {rel: sha256_file(os.path.join(self_dir, rel)) for rel in rels}


def disk_section_digests(self_dir: str, scopes: List[str]
                         ) -> Dict[str, Optional[str]]:
    """逐节候选切片指纹（`<sec>.tex`）。"""
    out: Dict[str, Optional[str]] = {}
    for sec in scopes or []:
        if isinstance(sec, str) and sec:
            out[sec] = sha256_file(os.path.join(self_dir, sec + ".tex"))
    return out


# ---------------------------------------------------------------------------
# 主产物加载与零信任校验
# ---------------------------------------------------------------------------

def load_review(self_dir: str, review_rel: str) -> Tuple[Optional[Dict[str, Any]],
                                                         List[str]]:
    """读取合并终审主产物；返回 (record|None, problems)。"""
    rel = _norm_rel(review_rel)
    prefix = _norm_rel(REVIEW_DIR_REL) + "/"
    if not rel.startswith(prefix):
        return None, ["REVIEW_PATH: 合并终审主产物必须位于 %s/ 下（当前 %r）"
                      % (_norm_rel(REVIEW_DIR_REL), review_rel)]
    path = os.path.join(self_dir or ".", rel.replace("/", os.sep))
    if not os.path.isfile(path):
        return None, ["REVIEW_MISSING: 合并终审主产物缺失 %s（不得用旧审读结果替代）"
                      % rel]
    data = _load_json(path)
    if not isinstance(data, dict):
        return None, ["REVIEW_BAD_JSON: 合并终审主产物不可解析 %s" % rel]
    return data, []


def _issue_problems(issues: Any, scope: str) -> List[str]:
    """issue 追溯字段 + 生命周期校验（PASS 记录不得残留 open）。"""
    if issues is None:
        return []
    if not isinstance(issues, list):
        return ["REVIEW_ISSUES: 小节 %s 的 issues 必须是数组" % scope]
    out: List[str] = []
    for idx, item in enumerate(issues):
        label = "小节 %s issues[%d]" % (scope, idx)
        if not isinstance(item, dict):
            out.append("REVIEW_ISSUES: %s 必须是对象" % label)
            continue
        for key in ISSUE_REQUIRED:
            if key not in item:
                out.append("REVIEW_ISSUES: %s 缺少追溯字段 %s（必须可追溯到"
                           " qid/部件/证据/处置）" % (label, key))
        status = str(item.get("status") or "open")
        if status not in ISSUE_STATUSES:
            out.append("REVIEW_ISSUES: %s 的 status=%r 非法，只能是 %s"
                       % (label, item.get("status"), "/".join(ISSUE_STATUSES)))
            continue
        if status == "open":
            out.append("REVIEW_ISSUE_OPEN: %s 存在未关闭问题 %s（%s），不得判 PASS"
                       % (scope, item.get("id") or "issues[%d]" % idx,
                          item.get("code") or ""))
        elif status == "fixed":
            if not str(item.get("resolution") or "").strip():
                out.append("REVIEW_ISSUE_FIXED: %s 标记 fixed 但没写 resolution" % label)
            if not _is_digest(item.get("verified_digest")):
                out.append("REVIEW_ISSUE_FIXED: %s 标记 fixed 但 verified_digest 不是"
                           "真实修复后候选摘要" % label)
        elif status == "withdrawn":
            text = str(item.get("resolution") or "").strip()
            if not text or text in _VAGUE_RESOLUTIONS:
                out.append("REVIEW_ISSUE_WITHDRAWN: %s 撤回结论必须写明实质理由"
                           "（不得用 %s 之类措辞）"
                           % (label, "/".join(_VAGUE_RESOLUTIONS)))
    return out


def _shuffle_edges(self_dir: str) -> list:
    try:
        return _check_semantic().load_shuffle_transitions(self_dir)
    except Exception:                     # pragma: no cover - 防御性
        return []


def _lineage_bound(self_dir: str, reviewed: str, current: str, edges: list) -> bool:
    if not reviewed or not current or reviewed == current:
        return False
    try:
        return bool(_check_semantic().shuffle_bound(reviewed, current, edges))
    except Exception:                     # pragma: no cover - 防御性
        return False


def validate_combined_review(
    self_dir: str,
    record: Any,
    *,
    expected_scopes: Optional[List[str]] = None,
    expected_qids: Optional[Dict[str, List[str]]] = None,
    request_digest: Optional[str] = None,
    output_format: Optional[str] = None,
    section_digests: Optional[Dict[str, Optional[str]]] = None,
    disk_digests: Optional[Dict[str, Optional[str]]] = None,
) -> List[str]:
    """零信任校验合并终审主产物，返回问题清单（空 = 通过）。

    任何一项不成立即整批失败：缺 scope / 缺 qid / 结论非 PASS / open issue /
    摘要与磁盘不符 / html-only 伪造 PDF 证明。
    """
    problems: List[str] = []
    if not isinstance(record, dict):
        return ["REVIEW_BAD_SHAPE: 合并终审主产物根节点必须是 JSON 对象"]

    version = record.get("schema_version")
    if not (isinstance(version, int) and not isinstance(version, bool)
            and version == REVIEW_SCHEMA_VERSION):
        return ["REVIEW_SCHEMA: schema_version=%r 不受支持（合并终审主产物必须 "
                "schema_version=%d；旧档不得冒充新档，也不得静默降级）"
                % (version, REVIEW_SCHEMA_VERSION)]
    if str(record.get("review_kind") or "") != REVIEW_KIND:
        problems.append("REVIEW_KIND: review_kind=%r 不是 %r（旧 v1 stage1/stage2"
                        " 盲审格式不得冒充合并终审）"
                        % (record.get("review_kind"), REVIEW_KIND))

    # ---- scopes 全覆盖 ----
    raw_scopes = record.get("scopes")
    scopes: List[str] = []
    if not isinstance(raw_scopes, list) or not raw_scopes:
        problems.append("REVIEW_SCOPES: scopes 必须是非空数组（一次合并审读必须"
                        "声明它覆盖的完整小节集合）")
    else:
        for item in raw_scopes:
            if not isinstance(item, str) or not item.strip():
                problems.append("REVIEW_SCOPES: scopes 含非法条目 %r" % (item,))
                continue
            if item in scopes:
                problems.append("REVIEW_SCOPES: scopes 重复声明 %s" % item)
                continue
            scopes.append(item)
    expected = [s for s in (expected_scopes or []) if isinstance(s, str) and s]
    if expected:
        missing = [s for s in expected if s not in scopes]
        if missing:
            problems.append("REVIEW_SCOPE_MISSING: 主产物未覆盖小节 %s —— 缺节即"
                            "整批失败，不得用其他小节的通过结论代替"
                            % ",".join(missing))
        extra = [s for s in scopes if s not in expected]
        if extra:
            problems.append("REVIEW_SCOPE_EXTRA: 主产物声明了本批计划外小节 %s"
                            % ",".join(extra))

    # ---- 输入指纹 ----
    digest = record.get("input_digest")
    if not _is_digest(digest):
        problems.append("REVIEW_INPUT_DIGEST: input_digest 必须是真实 64 位摘要"
                        "（须原样回填派发单指纹，不得留空/占位）")
    elif request_digest and digest != request_digest:
        problems.append("REVIEW_INPUT_DIGEST_MISMATCH: input_digest=%s 与派发单"
                        " 指纹 %s 不一致" % (str(digest)[:12],
                                            str(request_digest)[:12]))

    # ---- 输出格式与交付产物指纹 ----
    fmt = str(record.get("output_format") or "").strip()
    if fmt not in OUTPUT_FORMATS:
        problems.append("REVIEW_FORMAT: output_format=%r 非法，只能是 %s"
                        % (record.get("output_format"), "/".join(OUTPUT_FORMATS)))
    elif output_format and fmt != str(output_format):
        problems.append("REVIEW_FORMAT_MISMATCH: 主产物 output_format=%s 与本次"
                        "运行 %s 不一致" % (fmt, output_format))

    artifacts = record.get("artifact_digests")
    if not isinstance(artifacts, dict) or not artifacts:
        problems.append("REVIEW_ARTIFACT_DIGESTS: artifact_digests 必须是非空对象"
                        "（审读员必须绑定它实际看到的交付产物指纹）")
        artifacts = {}
    else:
        for rel, value in sorted(artifacts.items()):
            if not _is_digest(value):
                problems.append("REVIEW_BAD_DIGEST: artifact_digests[%s]=%r 不是真实"
                                " 64 位摘要（零 hash/占位摘要一律按伪造证据失败）"
                                % (rel, value))
        if fmt in FORMAT_ARTIFACTS:
            for rel in FORMAT_ARTIFACTS[fmt]:
                if not _is_digest(artifacts.get(rel)):
                    problems.append("REVIEW_ARTIFACT_MISSING: %s 格式缺少交付产物 %s"
                                    " 的真实摘要（缺交付证明即失败）" % (fmt, rel))
        if fmt == "html" and "student.pdf" in artifacts:
            problems.append("REVIEW_FAKE_PDF_PROOF: html-only 不得声明 student.pdf"
                            " 交付证明（PDF 字段按格式显式不适用）")
        if disk_digests is not None and fmt in FORMAT_ARTIFACTS:
            for rel in FORMAT_ARTIFACTS[fmt]:
                if artifacts.get(rel) != disk_digests.get(rel):
                    problems.append("REVIEW_ARTIFACT_HASH_MISMATCH: 交付产物 %s 的"
                                    " 摘要与磁盘实际文件不一致（审读看到的不是本轮"
                                    "产物）" % rel)

    # ---- 逐节结论 ----
    sections = record.get("sections")
    if not isinstance(sections, list) or not sections:
        problems.append("REVIEW_SECTIONS: sections 必须是非空数组（逐节审读结论）")
        sections = []
    edges = _shuffle_edges(self_dir)
    seen: Dict[str, Dict[str, Any]] = {}
    for idx, section in enumerate(sections):
        label = "sections[%d]" % idx
        if not isinstance(section, dict):
            problems.append("REVIEW_BAD_SECTION: %s 必须是对象" % label)
            continue
        scope = str(section.get("scope") or "").strip()
        if not scope:
            problems.append("REVIEW_BAD_SECTION: %s 缺少 scope" % label)
            continue
        if scope in seen:
            problems.append("REVIEW_SECTION_DUPLICATE: 小节 %s 出现多条审读结论"
                            % scope)
            continue
        seen[scope] = section
        if scopes and scope not in scopes:
            problems.append("REVIEW_SECTION_UNKNOWN: %s 的 scope=%s 未在顶层 scopes"
                            " 中声明" % (label, scope))

        section_digest = section.get("section_digest")
        if not _is_digest(section_digest):
            problems.append("REVIEW_SECTION_DIGEST: 小节 %s 的 section_digest 必须"
                            "是真实 64 位摘要" % scope)
        elif section_digests is not None:
            current = section_digests.get(scope)
            if current and section_digest != current:
                if _lineage_bound(self_dir, section_digest, current, edges):
                    # 打乱血缘只证明「当前切片 = 被审读候选 + 确定性重排」，
                    # 绝不豁免结论判定（下面照旧逐项判定 PASS/REVISE）。
                    pass
                else:
                    problems.append(
                        "REVIEW_SECTION_DIGEST_MISMATCH: 小节 %s 审读绑定的候选"
                        "摘要与当前切片不一致（候选已变更，旧审读不得复用）"
                        % scope)

        reviewed_qids = section.get("reviewed_qids")
        if not isinstance(reviewed_qids, list) or any(
                not isinstance(q, str) or not q.strip() for q in reviewed_qids):
            problems.append("REVIEW_QIDS: 小节 %s 的 reviewed_qids 必须是非空字符串"
                            "数组" % scope)
        elif expected_qids is not None:
            want = [q for q in (expected_qids or {}).get(scope) or []
                    if isinstance(q, str) and q]
            miss = [q for q in want if q not in reviewed_qids]
            if miss:
                problems.append("REVIEW_QID_MISSING: 小节 %s 缺审读 qid %s（未逐题"
                                "审读即失败）" % (scope, ",".join(miss)))
            unknown = [q for q in reviewed_qids if want and q not in want]
            if unknown:
                problems.append("REVIEW_QID_UNKNOWN: 小节 %s 声明了计划外 qid %s"
                                % (scope, ",".join(unknown)))

        for key in ("semantic_conclusion", "acceptance_conclusion"):
            value = str(section.get(key) or "").strip()
            if value not in CONCLUSIONS:
                problems.append("REVIEW_CONCLUSION_INVALID: 小节 %s 的 %s=%r 不在"
                                "合法集合 %s 内"
                                % (scope, key, section.get(key),
                                   "/".join(CONCLUSIONS)))
            elif value != "PASS":
                problems.append("REVIEW_NOT_PASS: 小节 %s 的 %s=%s（阻塞交付；其他"
                                "小节通过或打乱血缘都不得覆盖负面结论）"
                                % (scope, key, value))

        text = section.get("source_comparison")
        if not isinstance(text, str) or len(text.strip()) < 8:
            problems.append("REVIEW_SOURCE_COMPARISON: 小节 %s 缺少真实来源比对文本"
                            "（source_comparison 不得为空/占位，忠实性必须逐节留证）"
                            % scope)

        problems.extend(_issue_problems(section.get("issues"), scope))

    for scope in scopes:
        if scope not in seen:
            problems.append("REVIEW_SECTION_MISSING: 顶层 scopes 声明了 %s 却没有"
                            "对应审读结论（缺节即失败）" % scope)

    # 去重保序
    out: List[str] = []
    for item in problems:
        if item not in out:
            out.append(item)
    return out


def validate_review_file(self_dir: str, review_rel: str, **kwargs) -> List[str]:
    """按文件路径校验（先加载再校验），返回问题清单。"""
    record, problems = load_review(self_dir, review_rel)
    if record is None:
        return problems
    return validate_combined_review(self_dir, record, **kwargs)


# ---------------------------------------------------------------------------
# 确定性投影（一条审读 → 两套证据）
# ---------------------------------------------------------------------------

def _derived_from(record: Dict[str, Any], review_rel: str, review_sha: str,
                  scope: str) -> Dict[str, Any]:
    """投影溯源块：父产物路径/摘要 + 派发身份 + 投影器版本。"""
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "review_kind": REVIEW_KIND,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "review_artifact": _norm_rel(review_rel),
        "review_artifact_sha256": str(review_sha or ""),
        "dispatch_id": str(record.get("dispatch_id") or ""),
        "batch_id": str(record.get("batch_id") or ""),
        "scope": scope,
        "scopes": [str(s) for s in (record.get("scopes") or [])
                   if isinstance(s, str) and s],
        "input_digest": str(record.get("input_digest") or ""),
        "reviewed_at": str(record.get("reviewed_at") or ""),
    }


def _project_issue(item: Any, index: int,
                   deferral: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """审读 issue → 投影 issue（保留追溯字段 + 语义/验收两侧读取口径）。

    N2：登记了人工延期的 advisory 不再按 status=open 升级为 blocking——否则
    "无解但有理由"的问题会逐轮重提，把返修预算耗空。延期是人的决定，必须带
    reason，并作为独立字段留痕（deferred / deferral 块），不覆写原 status。
    """
    data = item if isinstance(item, dict) else {}
    status = str(data.get("status") or "open")
    detail = str(data.get("detail") or data.get("claim") or "")
    deferred = bool(deferral) and status == "open"
    projected = {
        # 追溯字段（验收证据硬要求，缺一即失败）
        "id": str(data.get("id") or "issue-%d" % (index + 1)),
        "qid": str(data.get("qid") or ""),
        "part": str(data.get("part") or "unspecified"),
        "code": str(data.get("code") or "REVIEW_ISSUE"),
        "evidence": str(data.get("evidence") or detail),
        "status": status,
        "resolution": str(data.get("resolution") or ""),
        "verified_digest": str(data.get("verified_digest") or ""),
        # 语义审读 v1 基础字段（evaluate_checker_result 读 severity/claim）
        "claim": str(data.get("claim") or detail),
        "line": data.get("line") if isinstance(data.get("line"), int) else 0,
        "severity": "blocking" if (status == "open" and not deferred) else "advisory",
    }
    if deferred:
        # 只有真的命中延期才多出这两个键：没有延期时投影与旧版逐字节一致，
        # 存量证据仍可复算认证（verify_projection 是逐字节比对的）。
        projected["deferred"] = True
        projected["deferral"] = {
            "id": str(deferral.get("id") or ""),
            "reason": str(deferral.get("reason") or ""),
            "deferred_by": str(deferral.get("deferred_by") or ""),
            "expires_at": str(deferral.get("expires_at") or ""),
        }
    return projected


def _deferral_for(self_dir: Optional[str], scope: str,
                   item: Any) -> Optional[Dict[str, Any]]:
    """查该 issue 是否命中生效中的延期（self_dir 缺失 → 不延期）。"""
    if not self_dir:
        return None
    data = item if isinstance(item, dict) else {}
    try:
        return ledger.find_deferral(
            self_dir, scope=scope, code=data.get("code"),
            qid=data.get("qid"))
    except Exception:  # 台账损坏不该让投影失败（门禁另有阻断）
        return None


def _project_issues(items: Any, scope: str,
                    self_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    return [_project_issue(item, idx, _deferral_for(self_dir, scope, item))
            for idx, item in enumerate(items or [])]


def project_semantic(record: Dict[str, Any], review_rel: str, review_sha: str,
                     section: Dict[str, Any],
                     self_dir: Optional[str] = None) -> Dict[str, Any]:
    """生成单节语义证据：v1 基础字段（可被旧闸门直接读）+ derived_from。"""
    scope = str(section.get("scope") or "")
    issues = _project_issues(section.get("issues"), scope, self_dir)
    return {
        "schema_version": 1,
        "scope": scope,
        "section_digest": str(section.get("section_digest") or ""),
        "conclusion": str(section.get("semantic_conclusion") or ""),
        "issues": issues,
        "input_digest": str(record.get("input_digest") or ""),
        # 时间戳只搬运父产物（绝不取 now()），保证投影可逐字节重算
        "checked_at": str(record.get("reviewed_at") or ""),
        "derived_from": _derived_from(record, review_rel, review_sha, scope),
    }


def project_acceptance(record: Dict[str, Any], review_rel: str, review_sha: str,
                       section: Dict[str, Any],
                       disk_digests: Optional[Dict[str, Optional[str]]] = None,
                       self_dir: Optional[str] = None,
                       ) -> Dict[str, Any]:
    """生成单节验收证据：v2 分支（review_kind + 双证据引用 + derived_from）。"""
    scope = str(section.get("scope") or "")
    fmt = str(record.get("output_format") or "")
    artifacts = {k: v for k, v in (record.get("artifact_digests") or {}).items()
                 if isinstance(k, str)}
    wanted = FORMAT_ARTIFACTS.get(fmt, ())
    delivery = {rel: (disk_digests or {}).get(rel) for rel in wanted}
    # C7：内容审读指纹（审读员当时看到的产物）与本轮交付实测指纹必须分离，
    # 且绝不修改前者去假装它见过新产物。
    reused = any(_is_digest(artifacts.get(rel)) and _is_digest(delivery.get(rel))
                 and artifacts.get(rel) != delivery.get(rel) for rel in wanted)
    issues = _project_issues(section.get("issues"), scope, self_dir)
    return {
        "schema_version": 2,
        "review_kind": REVIEW_KIND,
        "scope": scope,
        "reviewed_qids": [str(q) for q in (section.get("reviewed_qids") or [])
                          if isinstance(q, str) and q],
        "content_digest": str(record.get("input_digest") or ""),
        "output_format": fmt,
        # html-only：PDF 字段显式不适用（None），绝不用零 hash 伪造 PDF 证明
        "student_pdf_sha256": artifacts.get("student.pdf")
        if fmt in ("pdf", "both") else None,
        "student_html_sha256": artifacts.get("student.html")
        if fmt in ("html", "both") else None,
        "conclusion": str(section.get("acceptance_conclusion") or ""),
        "reviewer_role": str(record.get("reviewer_role") or ""),
        "issues": issues,
        # 忠实性/数学/位置证据（C6：v2 不得丢掉这些，也不得伪造旧盲审体验）
        "source_comparison": str(section.get("source_comparison") or ""),
        "scope_content_digest": str(section.get("section_digest") or ""),
        "content_review_refs": {
            "review_artifact": _norm_rel(review_rel),
            "review_artifact_sha256": str(review_sha or ""),
            "dispatch_id": str(record.get("dispatch_id") or ""),
            "batch_id": str(record.get("batch_id") or ""),
            "scope": scope,
            "section_digest": str(section.get("section_digest") or ""),
            # 审读当时的交付产物指纹（真实历史，不得被改写）
            "artifact_digests": dict(artifacts),
            "reviewed_at": str(record.get("reviewed_at") or ""),
        },
        "delivery_check_ref": {
            # 本轮机械验收：磁盘实测指纹（运行时重算，必须逐文件吻合）
            "artifact_digests": delivery,
            "output_format": fmt,
            "source": "%s/%s 投影时磁盘实测" % (PROJECTOR, PROJECTOR_VERSION),
        },
        "content_review_reused": reused,
        "derived_from": _derived_from(record, review_rel, review_sha, scope),
    }


def build_projections(self_dir: str, record: Dict[str, Any], review_rel: str,
                      review_sha: Optional[str] = None, *,
                      expected_scopes: Optional[List[str]] = None,
                      expected_qids: Optional[Dict[str, List[str]]] = None,
                      output_format: Optional[str] = None,
                      section_digests: Optional[Dict[str, Optional[str]]] = None,
                      disk_digests: Optional[Dict[str, Optional[str]]] = None,
                      ) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], List[str]]:
    """把一条合并终审主产物确定性地投影成逐节语义/验收证据。

    返回 ``({"semantic": {sec: rec}, "acceptance": {sec: rec}}, problems)``；
    主产物校验不过时返回 ``({}, problems)``——绝不从非法审读投影出 PASS。
    """
    sha = str(review_sha or sha256_file(
        os.path.join(self_dir or ".", _norm_rel(review_rel).replace("/", os.sep)))
        or "")
    problems = validate_combined_review(
        self_dir, record,
        expected_scopes=expected_scopes,
        expected_qids=expected_qids,
        output_format=output_format,
        section_digests=section_digests,
        disk_digests=disk_digests)
    if problems:
        return {}, problems
    fmt = str(record.get("output_format") or "")
    if disk_digests is None:
        disk_digests = disk_artifact_digests(self_dir, fmt)
    semantic: Dict[str, Dict[str, Any]] = {}
    acceptance: Dict[str, Dict[str, Any]] = {}
    for section in record.get("sections") or []:
        if not isinstance(section, dict):
            continue
        scope = str(section.get("scope") or "")
        if not scope:
            continue
        semantic[scope] = project_semantic(record, review_rel, sha, section,
                                           self_dir=self_dir)
        acceptance[scope] = project_acceptance(record, review_rel, sha, section,
                                               disk_digests=disk_digests,
                                               self_dir=self_dir)
    return {"semantic": semantic, "acceptance": acceptance}, []


def project_paths(self_dir: str, scope: str) -> Dict[str, str]:
    """单节投影落盘路径（相对 self_dir，POSIX 分隔）。"""
    return {
        "semantic": "%s/%s.json" % (_norm_rel(SEMANTIC_DIR_REL), scope),
        "acceptance": "%s/%s.json" % (_norm_rel(ACCEPTANCE_DIR_REL), scope),
    }


def write_projections(self_dir: str, review_rel: str,
                      **kwargs) -> Tuple[List[str], List[str]]:
    """投影并原子落盘，返回 (已写路径列表, problems)。"""
    record, problems = load_review(self_dir, review_rel)
    if record is None:
        return [], problems
    review_sha = sha256_file(
        os.path.join(self_dir or ".", _norm_rel(review_rel).replace("/", os.sep)))
    projections, problems = build_projections(
        self_dir, record, review_rel, review_sha, **kwargs)
    if problems:
        return [], problems
    written: List[str] = []
    for kind in ("semantic", "acceptance"):
        for scope, payload in sorted(projections[kind].items()):
            rel = project_paths(self_dir, scope)[kind]
            path = os.path.join(self_dir or ".", rel.replace("/", os.sep))
            if not _write_json_atomic(path, payload):
                return written, ["PROJECTION_WRITE_FAILED: 投影落盘失败 %s" % rel]
            written.append(rel)
    return written, []


# ---------------------------------------------------------------------------
# 认证：投影必须可重算 + 必须有真实 receipt
# ---------------------------------------------------------------------------

def verify_projection(self_dir: str, record: Any, kind: str) -> List[str]:
    """认证一条投影：溯源块 → 父产物重查 → 复算比对 → 证据台账回查。

    kind ∈ {"semantic", "acceptance"}。返回问题清单（空 = 可认证）。
    """
    if kind not in ("semantic", "acceptance"):
        return ["PROJECTION_KIND: 未知投影种类 %r" % (kind,)]
    if not isinstance(record, dict):
        return ["PROJECTION_BAD_SHAPE: 投影必须是 JSON 对象"]
    derived = record.get("derived_from")
    if not isinstance(derived, dict) or not derived:
        return ["PROJECTION_UNTRACED: 投影没有 derived_from 溯源块——不得只凭主"
                " Agent 自写的 PASS 认证（无父产物、无独立 receipt）"]
    version = derived.get("projection_schema_version")
    if not (isinstance(version, int) and not isinstance(version, bool)
            and version == PROJECTION_SCHEMA_VERSION):
        return ["PROJECTION_INCOMPATIBLE: projection_schema_version=%r 不受支持"
                "（显式拒绝不兼容旧档，不得静默改写历史报告）"
                % (derived.get("projection_schema_version"),)]

    problems: List[str] = []
    if str(derived.get("projector") or "") != PROJECTOR:
        problems.append("PROJECTION_PROJECTOR: projector=%r 不是 %r"
                        % (derived.get("projector"), PROJECTOR))
    if str(derived.get("projector_version") or "") != PROJECTOR_VERSION:
        problems.append("PROJECTION_PROJECTOR: projector_version=%r 不是 %r"
                        % (derived.get("projector_version"), PROJECTOR_VERSION))
    if str(derived.get("review_kind") or "") != REVIEW_KIND:
        problems.append("PROJECTION_KIND: derived_from.review_kind=%r 不是 %r"
                        % (derived.get("review_kind"), REVIEW_KIND))

    review_rel = _norm_rel(derived.get("review_artifact"))
    parent, parent_problems = load_review(self_dir, review_rel)
    if parent is None:
        return problems + parent_problems
    actual_sha = sha256_file(
        os.path.join(self_dir or ".", review_rel.replace("/", os.sep)))
    declared_sha = str(derived.get("review_artifact_sha256") or "")
    if not _is_digest(actual_sha):
        problems.append("PROJECTION_PARENT_MISSING: 父合并终审产物不可读 %s"
                        % review_rel)
    elif actual_sha != declared_sha:
        problems.append("PROJECTION_PARENT_TAMPERED: 父产物实际 sha256=%s 与投影"
                        "声明 %s 不一致（父档被改写，投影随之失效）"
                        % (actual_sha[:12], (declared_sha or "<empty>")[:12]))
    if not (isinstance(parent.get("schema_version"), int)
            and parent.get("schema_version") == REVIEW_SCHEMA_VERSION):
        problems.append("PROJECTION_PARENT_SCHEMA: 父产物 schema_version=%r 不是 %d"
                        " 合并终审（旧档不得冒充）"
                        % (parent.get("schema_version"), REVIEW_SCHEMA_VERSION))
    if str(parent.get("review_kind") or "") != REVIEW_KIND:
        problems.append("PROJECTION_PARENT_SCHEMA: 父产物 review_kind=%r 不是 %r"
                        % (parent.get("review_kind"), REVIEW_KIND))

    scope = str(record.get("scope") or "")
    if scope and scope != str(derived.get("scope") or ""):
        problems.append("PROJECTION_SCOPE: 投影 scope=%r 与 derived_from.scope=%r"
                        " 不一致" % (scope, derived.get("scope")))

    # ---- 复算比对：投影必须能由父产物重算出来 ----
    projections, rebuild_problems = build_projections(
        self_dir, parent, review_rel, actual_sha,
        expected_scopes=parent.get("scopes"))
    if rebuild_problems:
        return problems + rebuild_problems
    expected = (projections.get(kind) or {}).get(scope)
    if expected is None:
        problems.append("PROJECTION_SCOPE_UNKNOWN: 父产物没有小节 %r 的审读结论，"
                        "投影无所依托" % scope)
    else:
        diff = [key for key in sorted(set(expected) | set(record))
                if expected.get(key) != record.get(key)]
        if diff:
            problems.append("PROJECTION_TAMPERED: 投影与父产物重算结果不一致"
                            "（字段 %s）—— 投影必须可重算，手改即失败"
                            % ",".join(diff))

    # ---- 证据台账回查：必须真实派发过独立审读员 ----
    dispatch_id = str(derived.get("dispatch_id") or "")
    if not dispatch_id:
        problems.append("PROJECTION_NO_DISPATCH: derived_from 缺少 dispatch_id，"
                        "无法回查子 Agent 证据台账")
        return problems
    evidence = _agent_evidence().load_evidence(self_dir, dispatch_id)
    if evidence is None:
        problems.append("PROJECTION_NO_RECEIPT: 找不到 .pd/agent-calls/%s.json"
                        "（无 receipt 的投影不得认证：主 Agent 自写 PASS 无效）"
                        % dispatch_id)
        return problems
    if str(evidence.get("status") or "") != _agent_evidence().EVIDENCE_STATUS_OK:
        problems.append("PROJECTION_RECEIPT_NOT_OK: 证据台账 status=%r，未成功完成"
                        % evidence.get("status"))
    digests = evidence.get("output_digests") or {}
    if str(digests.get(review_rel) or "") != str(actual_sha):
        problems.append("PROJECTION_RECEIPT_MISMATCH: 证据台账登记的 %s 摘要与父"
                        "产物实际摘要不一致（父产物不是该次派发的产出）" % review_rel)
    worker = str(evidence.get("worker") or "").strip()
    if not worker:
        problems.append("PROJECTION_RECEIPT_NO_WORKER: 证据台账缺 worker 身份，无法"
                        "证明是独立子 Agent")
    elif worker in writer_workers(self_dir):
        problems.append("PROJECTION_REVIEWER_IDENTITY: 审读 worker %r 同时出现在"
                        "写作者台账中（创审必须分离，同一身份不得自审）" % worker)
    return problems


def verify_projection_file(self_dir: str, rel: str, kind: str) -> List[str]:
    """按路径认证已落盘的投影文件。"""
    path = os.path.join(self_dir or ".", _norm_rel(rel).replace("/", os.sep))
    record = _load_json(path)
    if not isinstance(record, dict):
        return ["PROJECTION_MISSING: 投影文件缺失或不可解析 %s" % rel]
    return verify_projection(self_dir, record, kind)


# ---------------------------------------------------------------------------
# 计划覆盖（R 批必须覆盖全部待审 scope）
# ---------------------------------------------------------------------------

def plan_coverage_problems(plan: Any) -> List[str]:
    """合并终审派发计划的覆盖完整性校验。

    C5 的「请求数可以少于节数，但 scopes 并集必须等于待处理节」在合并终审
    里的等价形态：每个请求用 ``review_scopes`` 声明它这一批要审的节（因为是
    一次审读覆盖多节、写域是**一个**批级主产物，不能用 C5 的「每节一个产物」
    归属规则），并集必须完整覆盖、且无重叠。
    """
    problems: List[str] = []
    if not isinstance(plan, dict):
        return ["PLAN_BAD_SHAPE: 派发计划根节点必须是 JSON 对象"]
    sections = [s for s in (plan.get("sections") or [])
                if isinstance(s, str) and s]
    if not sections:
        return ["PLAN_NO_SECTIONS: 计划没有声明待审小节"]
    requests = [r for r in (plan.get("requests") or []) if isinstance(r, dict)]
    if not requests:
        return ["PLAN_NO_REQUESTS: 合并终审必须至少派发一个审读请求"
                "（零派发不能产出任何审读证据）"]
    owners: Dict[str, List[str]] = {}
    for idx, req in enumerate(requests):
        label = "requests[%d]" % idx
        scopes = req.get("review_scopes")
        if not isinstance(scopes, list) or not scopes:
            problems.append("PLAN_BAD_REVIEW_SCOPES: %s 缺少 review_scopes（合并"
                            "审读的授权真源，不得只靠 scope 兼容标签）" % label)
            continue
        clean: List[str] = []
        for item in scopes:
            if not isinstance(item, str) or not item.strip():
                problems.append("PLAN_BAD_REVIEW_SCOPES: %s 的 review_scopes 含非法"
                                "条目 %r" % (label, item))
                continue
            if item in clean:
                problems.append("PLAN_DUPLICATE_SCOPE: %s 重复声明小节 %s"
                                % (label, item))
                continue
            clean.append(item)
        for item in clean:
            if item not in sections:
                problems.append("PLAN_UNKNOWN_SCOPE: %s 声明了计划外小节 %s"
                                % (label, item))
            owners.setdefault(item, []).append(label)
        artifacts = [a for a in (req.get("artifacts") or [])
                     if isinstance(a, str) and a]
        if len(artifacts) != 1:
            problems.append("PLAN_REVIEW_ARTIFACTS: %s 必须只声明一个合并终审主产物"
                            "（当前 %r）" % (label, artifacts))
        prefix = _norm_rel(REVIEW_DIR_REL) + "/"
        for rel in artifacts:
            if not _norm_rel(rel).startswith(prefix):
                problems.append("PLAN_ARTIFACT_PATH: %s 的产物 %s 不在 %s/ 白名单内"
                                % (label, rel, _norm_rel(REVIEW_DIR_REL)))
    covered = [s for s in sections if s in owners]
    missing = [s for s in sections if s not in owners]
    if missing:
        problems.append("PLAN_UNDER_DISPATCH: 计划声明 %d 个小节，合并审读请求只"
                        "覆盖 %d 个，缺 %s（每节都必须被某一批审读认领）"
                        % (len(sections), len(covered), ",".join(missing)))
    dups = sorted(s for s, labels in owners.items() if len(labels) > 1)
    if dups:
        problems.append("PLAN_DUPLICATE_SCOPE: 小节 %s 被多个审读请求同时认领"
                        % ",".join(dups))
    return problems


def validate_review_plan(plan: Any, *, expected_stage: Optional[str] = None,
                         batch_id: Optional[str] = None) -> List[str]:
    """合并终审派发计划的完整零信任校验（覆盖 + 角色化 + 指纹 + 写域）。

    这是本模块自带的派发前门禁：``check_dispatch.py`` 是**写作者轮次**的门禁，
    它的 ``UNDER_DISPATCH`` 规则建立在「每个 scope 拥有自己的产物」之上；而合并
    终审的写域是**一个批级主产物**，把 C5 的逐节产物归属规则套上去只会逼出
    「为了凑归属而重复审读」。因此审读轮次用本函数作为确定性门禁（结论写入
    ``.pd/reports/check_combined_review_plan.json``），规则不减反增：还额外要求
    角色显式化、提示词唯一、逐节输入指纹齐全、产物互不重复。
    """
    problems = list(plan_coverage_problems(plan))
    if not isinstance(plan, dict):
        return problems
    stage = str(plan.get("stage") or "")
    if expected_stage and stage != expected_stage:
        problems.append("PLAN_STAGE: 计划 stage=%r 不是 %r" % (stage, expected_stage))
    elif not is_reviewer_stage(stage):
        problems.append("PLAN_STAGE: 计划 stage=%r 不是合并终审阶段 %s"
                        % (stage, "/".join(REVIEWER_STAGES)))
    plan_batch = str(plan.get("batch_id") or "")
    if not plan_batch:
        problems.append("PLAN_BATCH: 计划缺少 batch_id")
    elif batch_id and plan_batch != str(batch_id):
        problems.append("PLAN_BATCH: 计划 batch_id=%r 与当前批次 %r 不符"
                        % (plan_batch, batch_id))
    mc = plan.get("max_concurrency")
    if not (isinstance(mc, int) and not isinstance(mc, bool) and 1 <= mc <= 16):
        problems.append("PLAN_CONCURRENCY: max_concurrency=%r 非法（1..16）" % (mc,))

    known = [s for s in (plan.get("sections") or []) if isinstance(s, str) and s]
    seen_ids: Dict[str, int] = {}
    seen_artifacts: Dict[str, int] = {}
    for idx, req in enumerate(plan.get("requests") or []):
        if not isinstance(req, dict):
            continue
        label = "requests[%d]" % idx
        dispatch_id = str(req.get("dispatch_id") or "").strip()
        if not dispatch_id:
            problems.append("PLAN_DISPATCH_ID: %s 缺少 dispatch_id" % label)
        else:
            seen_ids[dispatch_id] = seen_ids.get(dispatch_id, 0) + 1
        req_stage = str(req.get("stage") or "")
        if not is_reviewer_stage(req_stage):
            problems.append("PLAN_REQUEST_STAGE: %s 的 stage=%r 不是审读阶段（角色必须"
                            "显式化，不能只靠批次名区分）" % (label, req_stage))
        if str(req.get("batch_id") or "") != plan_batch:
            problems.append("PLAN_REQUEST_BATCH: %s 的 batch_id=%r 与本批 %r 不符"
                            % (label, req.get("batch_id"), plan_batch))
        role = str(req.get("role") or "")
        if not role.startswith(REVIEWER_ROLE_PREFIX):
            problems.append("PLAN_ROLE: %s 的 role=%r 未显式角色化（须以 %r 开头）"
                            % (label, role, REVIEWER_ROLE_PREFIX))
        if str(req.get("prompt_file") or "") != REVIEW_PROMPT_FILE:
            problems.append("PLAN_PROMPT: %s 的 prompt_file=%r 不是合并终审提示词 %r"
                            % (label, req.get("prompt_file"), REVIEW_PROMPT_FILE))
        if not _is_digest(req.get("input_digest")):
            problems.append("PLAN_INPUT_DIGEST: %s 的 input_digest 不是真实 64 位摘要"
                            % label)
        scopes = [s for s in (req.get("review_scopes") or [])
                  if isinstance(s, str) and s]
        if scopes and str(req.get("scope") or "") != scopes[0]:
            problems.append("PLAN_SCOPE_LABEL: %s 的兼容标签 scope=%r 必须等于"
                            " review_scopes[0]=%r" % (label, req.get("scope"),
                                                      scopes[0]))
        for scope in scopes:
            if scope not in known:
                problems.append("PLAN_UNKNOWN_SCOPE: %s 声明了计划外小节 %s"
                                % (label, scope))
        digests = req.get("scope_input_digests")
        if not isinstance(digests, dict) or not digests:
            problems.append("PLAN_SCOPE_INPUTS: %s 缺少 scope_input_digests（不得只"
                            "凭兼容标签授权）" % label)
        else:
            for scope in scopes:
                if not _is_digest(digests.get(scope)):
                    problems.append("PLAN_SCOPE_INPUTS: %s 的 scope_input_digests[%s]"
                                    " 不是真实摘要" % (label, scope))
        for rel in req.get("artifacts") or []:
            key = _norm_rel(rel)
            seen_artifacts[key] = seen_artifacts.get(key, 0) + 1
    for dispatch_id, count in sorted(seen_ids.items()):
        if count > 1:
            problems.append("PLAN_DUPLICATE_DISPATCH: dispatch_id %s 出现 %d 次"
                            % (dispatch_id, count))
    for rel, count in sorted(seen_artifacts.items()):
        if count > 1:
            problems.append("PLAN_DUPLICATE_ARTIFACT: 主产物 %s 被 %d 个请求同时占用"
                            "（写域必须互斥）" % (rel, count))
    out: List[str] = []
    for item in problems:
        if item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="合并终审主产物校验与确定性证据投影 (review_projection.py)")
    parser.add_argument("self_dir", help="学案工作目录（含 .pd/）")
    parser.add_argument("--review", help="合并终审主产物相对路径"
                                        "（默认 .pd/combined-review/ 下最新一份）")
    parser.add_argument("--write", action="store_true",
                        help="投影并落盘到 .pd/semantic 与 .pd/acceptance")
    parser.add_argument("--verify", action="store_true",
                        help="认证已落盘投影（复算 + 证据台账回查）")
    parser.add_argument("--json", help="结构化报告路径")
    args = parser.parse_args(argv)

    self_dir = os.path.abspath(args.self_dir)
    review_rel = _norm_rel(args.review or "")
    if not review_rel:
        directory = os.path.join(self_dir, _norm_rel(REVIEW_DIR_REL).replace("/", os.sep))
        candidates = sorted(
            (os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".json")), key=os.path.getmtime) \
            if os.path.isdir(directory) else []
        if not candidates:
            print("[review_projection] 未找到合并终审主产物（%s）"
                  % _norm_rel(REVIEW_DIR_REL))
            return 1
        review_rel = _norm_rel(os.path.relpath(candidates[-1], self_dir))

    record, problems = load_review(self_dir, review_rel)
    if record is not None:
        problems = validate_combined_review(self_dir, record)
        if not problems and args.write:
            _, problems = write_projections(self_dir, review_rel)
    if not problems and args.verify:
        for scope in record.get("scopes") or []:
            for kind in ("semantic", "acceptance"):
                rel = project_paths(self_dir, str(scope))[kind]
                problems.extend(verify_projection_file(self_dir, rel, kind))

    report = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "status": "pass" if not problems else "fail",
        "review": review_rel,
        "problems": problems,
    }
    if args.json:
        _write_json_atomic(os.path.abspath(args.json), report)
    for item in problems:
        print("[review_projection] [FAIL] %s" % item)
    if problems:
        print("[review_projection] 发现 %d 个问题（主产物: %s）"
              % (len(problems), review_rel))
        return 1
    print("[review_projection] 通过：%s" % review_rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
