# -*- coding: utf-8 -*-
"""scope_selection.py — 工作集选择（audit §17 任务2 / §12.4）。

Phase 0 让用户选择本次运行的工作集：

- ``full``    ：全量生成，所有 expected_sections 都参与派发（默认行为）；
- ``focused`` ：只生成目标小节（``target_sections`` / ``target_qids`` 指定）；
- ``edit``    ：只重生成**已存在**的目标小节（局部微调，未选中节标记 reused）。

本模块是纯函数实现（不读文件系统、不依赖 state），方便单测与复用；
"已存在"由调用方通过 ``existing_sections`` 注入（缺省视为 expected 全集）。
选择结果按 ``expected_sections`` 的稳定顺序返回，保证派发顺序确定。
"""

from typing import List, Optional

RUN_SCOPES = ("full", "focused", "edit")


class ScopeSelectionError(ValueError):
    """工作集选择失败（非法 run_scope / 目标不存在 / 选择为空）。

    非法值一律显式报错，绝不静默回退 full——静默回退会让"只想改一节"
    的用户付出全量重跑的代价，且掩盖配置错误（audit §17 任务1）。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_targets(raw) -> List[str]:
    """把配置层的逗号分隔字符串（或 list）规范化为 list[str]。

    配置层保留字符串（gen_config FIELDS ``text`` 校验），进 state 前
    统一在此规范化：去空白、去空项、去重（保序）。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            if entry is None:
                continue
            items.extend(s.strip() for s in str(entry).split(","))
    else:
        items = [s.strip() for s in str(raw).split(",")]
    out: List[str] = []
    for it in items:
        if it and it not in out:
            out.append(it)
    return out


def _section_of_qid(qid: str) -> str:
    """qid -> 所属 section（插件约定 qid 形如 ``sec1-entry``）。"""
    return qid.split("-", 1)[0].strip()


def select_scopes(run_scope: str,
                  target_sections: List[str],
                  target_qids: List[str],
                  expected_sections: List[str],
                  existing_sections: Optional[List[str]] = None) -> List[str]:
    """按 run_scope 选择本次要派发的 section 列表（稳定顺序 = expected 顺序）。

    参数:
        run_scope: full | focused | edit（其它值报 ScopeSelectionError）
        target_sections: 目标小节（focused/edit 使用）
        target_qids: 目标 qid（focused 使用；按其所属小节反推）
        expected_sections: 规划产出的全部小节（顺序即稳定顺序）
        existing_sections: 已存在的节（edit 模式白名单；缺省 = expected 全集）

    返回:
        选中的 section 列表，顺序与 ``expected_sections`` 一致。

    异常:
        ScopeSelectionError: 非法 run_scope / 目标节不在 expected /
                             focused 未给目标 / 选择结果为空。
    """
    scope = (run_scope or "").strip()
    if scope not in RUN_SCOPES:
        raise ScopeSelectionError(
            "BAD_RUN_SCOPE",
            "run_scope 取值 “%s” 非法，只能是: %s" % (run_scope, " / ".join(RUN_SCOPES)))

    expected = [s for s in (expected_sections or []) if s]
    expected_set = set(expected)

    if scope == "full":
        # focused/edit 目标在 full 模式下忽略（full 语义 = 全部）
        return list(expected)

    targets = list(target_sections or [])
    if not targets and target_qids:
        # 仅给 qid 时按所属小节反推（保持 qid 出现顺序）
        for qid in target_qids:
            sec = _section_of_qid(qid)
            if sec and sec not in targets:
                targets.append(sec)
    elif target_qids:
        # 同时给了节与 qid：并集（节顺序仍以 expected 为准）
        for qid in target_qids:
            sec = _section_of_qid(qid)
            if sec and sec not in targets:
                targets.append(sec)

    if not targets:
        raise ScopeSelectionError(
            "EMPTY_TARGETS",
            "run_scope=%s 需要 target_sections 或 target_qids 指定工作集" % scope)

    unknown = [t for t in targets if t not in expected_set]
    if unknown:
        raise ScopeSelectionError(
            "UNKNOWN_TARGET",
            "目标小节 %s 不在规划清单 %s 中" % (", ".join(unknown), ", ".join(expected)))

    if scope == "edit":
        existing = set(existing_sections) if existing_sections is not None else expected_set
        rejected = [t for t in targets if t not in existing]
        if rejected:
            raise ScopeSelectionError(
                "TARGET_NOT_EXISTING",
                "edit 模式只允许重生成已存在的小节，未找到: %s" % ", ".join(rejected))

    selected = set(targets)
    return [s for s in expected if s in selected]
