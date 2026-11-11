#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""frozen_policy.py — 门禁侧读取**已冻结**执行策略的唯一入口（F3 修复）。

问题（F3）：执行契约在 Phase 0 与内容契约同批冻结进
``.pd/intent-contract.json``，但门禁（check_pedagogy / check_scope 等）读的是
**当前** ``config.yaml``。于是把 config.yaml 从 strict 改成 advisory 之后：

  * 已经开始的会话策略被静默放宽（strict → advisory）；
  * ``require_classroom_plan: true → false`` 让课堂计划不再必需；
  * ``repair_max_per_scope`` 被改小/改大，返修预算跟着漂。

修复原则（P-4 冻结优先）：**冻结值 > 当前 config.yaml > 工程默认**。
本模块只负责前半句——把冻结块读出来；没有冻结契约（旧工程 / Phase 0 之前）
时返回 None，调用方照旧回落 config.yaml。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

CONTRACT_REL = os.path.join(".pd", "intent-contract.json")

#: 冻结后不得被当前 YAML 悄悄改写的策略键（内容/预算语义）
FROZEN_POLICY_KEYS = (
    "pedagogy_policy",
    "budget_policy",
    "require_classroom_plan",
    "repair_max_per_scope",
    "repair_max_total",
)


def load_contract(self_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    """读 .pd/intent-contract.json；缺失/损坏返回 None。"""
    if not self_dir:
        return None
    path = os.path.join(self_dir, CONTRACT_REL)
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def frozen_execution(self_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    """冻结执行块；旧契约（无执行字段）返回 None。"""
    contract = load_contract(self_dir)
    if not contract:
        return None
    block = {key: contract.get(key) for key in
             ("execution_schema_version", "execution_profile", "graph_version",
              "policy_digest")}
    config = contract.get("execution_config")
    if not any(value not in (None, "") for value in block.values()) and \
            not isinstance(config, dict):
        return None
    block["execution_config"] = config if isinstance(config, dict) else {}
    return block


def frozen_execution_config(self_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    """冻结的有效执行配置（dict）；无冻结契约返回 None。"""
    block = frozen_execution(self_dir)
    if block is None:
        return None
    config = block.get("execution_config")
    return dict(config) if isinstance(config, dict) else {}


def frozen_execution_profile(self_dir: Optional[str]) -> Optional[str]:
    block = frozen_execution(self_dir)
    if block is None:
        return None
    profile = block.get("execution_profile")
    return str(profile) if profile not in (None, "") else None


def merged_policy(self_dir: Optional[str],
                  declared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并策略：冻结值覆盖 declared（当前 config.yaml）；返回 {key: value}。

    ``source`` 子字典记录每个键的生效来源（frozen / config），供门禁报告与
    排障用——「这个 strict 是谁给的」必须能一眼看出来。
    """
    declared = dict(declared or {})
    frozen = frozen_execution_config(self_dir) or {}
    out: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for key in FROZEN_POLICY_KEYS:
        if key in frozen:
            out[key] = frozen[key]
            sources[key] = "frozen"
        elif key in declared:
            out[key] = declared[key]
            sources[key] = "config"
    out["sources"] = sources
    return out


_TRUE_STRS = ("true", "1", "yes", "on")
_FALSE_STRS = ("false", "0", "no", "off")


def _as_bool(value: Any) -> Any:
    """把 YAML 里的 true/false 字面量归一；认不出就原样返回（交给别处报错）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRS:
            return True
        if text in _FALSE_STRS:
            return False
    return value


def policy_drift(self_dir: Optional[str],
                 declared: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """当前 config.yaml 与冻结合同的策略漂移清单（空列表 = 一致）。

    冻结后改 YAML 不是"立即生效"，而是**必须显式重新确认**（新 thread）；门禁
    看到漂移要报出来，而不是照着新 YAML 放宽判定。
    """
    frozen = frozen_execution_config(self_dir)
    if frozen is None:
        return []
    declared = dict(declared or {})
    drift: List[Dict[str, Any]] = []
    for key in FROZEN_POLICY_KEYS:
        if key not in declared or key not in frozen:
            continue
        want, got = frozen[key], declared[key]
        if isinstance(want, bool) or isinstance(got, bool):
            # 布尔键：config.yaml 里可能写成字符串 "false"，必须先归一，
            # 否则 bool("false") 为 True，漂移被静默吞掉。
            if _as_bool(want) != _as_bool(got):
                drift.append({"key": key, "frozen": want, "config": got})
        elif str(want).strip().lower() != str(got).strip().lower():
            drift.append({"key": key, "frozen": want, "config": got})
    return drift
