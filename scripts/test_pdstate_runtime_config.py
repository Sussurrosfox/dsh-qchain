# -*- coding: utf-8 -*-
"""config.yaml 双语义解耦回归测试（D3 / B1 / B2）。

pdstate 把 config.yaml 计入冻结输入摘要（TASK_INPUT_FILES），而
agent_dispatch 子系统又按官方注释把它当**运行时可调**——同一文件两套语义
导致「改一行派发参数」作废所有在途候选。本测试锁定修复后的语义：

* B1 运行时键（agent_dispatch / 并发 / 预算 …）变化 → 输入摘要**不变**；
* B2 内容键（main_title / delivery_profile …）变化 → 摘要**变**；
* D3 归一化口径是可重入的：再剥一次（键已删）不应再变；
* overrides 只换内容口径、不增删条目，路径缺失时一律记 None。
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdstate  # noqa: E402


def _snapshot(tmp_path, cfg_text, **extra_sources):
    """落盘 config.yaml + 其余基线来源，返回 (pdir, task_id, paths)。"""
    pdir = str(tmp_path / "self")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg_text)
    written = {"config.yaml"}
    for name, text in extra_sources.items():
        with open(os.path.join(pdir, name), "w", encoding="utf-8") as f:
            f.write(text)
        written.add(name)
    paths = [os.path.join(pdir, n) for n in sorted(written)]
    return pdir, paths


def test_runtime_keys_excluded_from_digest(tmp_path):
    """B1：只改运行时键 → 输入摘要不变（不再误标 stale）。"""
    base_cfg = (
        "main_title: 对称群\n"
        "delivery_profile: self_study\n"
        "agent_dispatch:\n"
        "  model_map:\n"
        "    T2: {provider: scnet, model: SCNet-Max}\n"
        "agent_max_concurrency: 0\n"
    )
    other_cfg = (
        "main_title: 对称群\n"
        "delivery_profile: self_study\n"
        "agent_dispatch:\n"
        "  model_map:\n"
        "    T4: {provider: our-free-model, model: combo:recommended-free}\n"
        "agent_max_concurrency: 8\n"
        "repair_per_scope_limit: 12\n"
        "agent_timeout_seconds: 900\n"
    )
    pdir1, paths1 = _snapshot(tmp_path / "a", base_cfg, source_map="# map\n")
    pdir2, paths2 = _snapshot(tmp_path / "b", other_cfg, source_map="# map\n")

    d1 = pdstate.digest_task_inputs(paths1)[0]
    d2 = pdstate.digest_task_inputs(paths2)[0]
    assert d1 == d2, "运行时键不得影响冻结输入摘要"


def test_content_keys_change_digest(tmp_path):
    """B2：内容键变化 → 摘要必须变化（受审输入未被静默冻结）。"""
    cfg_a = "main_title: 对称群\nagent_max_concurrency: 0\n"
    cfg_b = "main_title: 交错群\nagent_max_concurrency: 0\n"
    pdir1, paths1 = _snapshot(tmp_path / "a", cfg_a)
    pdir2, paths2 = _snapshot(tmp_path / "b", cfg_b)

    assert (pdstate.digest_task_inputs(paths1)[0]
            != pdstate.digest_task_inputs(paths2)[0])


def test_runtime_agnostic_bytes_is_idempotent_and_prefixed(tmp_path):
    """D3：剥离后字节再剥离不变；带前缀与正常序列化。"""
    cfg = (
        "agent_dispatch: {model_map: {T2: {provider: scnet}}}\n"
        "repair_per_scope_limit: 4\n"
        "max_student_pages: 6\n"
        "main_title: 对称群\n"
    )
    pdir = tmp_path / "self"
    os.makedirs(pdir, exist_ok=True)
    cfg_path = pdir / "config.yaml"
    cfg_path.write_text(cfg, encoding="utf-8")

    first = pdstate.config_runtime_agnostic_bytes(str(cfg_path))
    assert first.startswith(b"config.yaml#runtime-agnostic\n")
    assert b"agent_dispatch" not in first
    assert b"repair_per_scope_limit" not in first
    assert b"main_title" in first

    # 再剥一次（键已被删）：归一化口径稳定（JSON 是合法 YAML）
    prefix = b"config.yaml#runtime-agnostic\n"
    stripped = pdir / "stripped.yaml"
    stripped.write_text(first[len(prefix):].decode("utf-8"), encoding="utf-8")
    assert pdstate.config_runtime_agnostic_bytes(str(stripped)) == first


def test_malformed_config_falls_back_to_raw_bytes(tmp_path):
    """非映射 / 坏 YAML / 不可读 → 返回 None（退回整文件口径，宁可误 stale）。"""
    pdir = tmp_path / "self"
    os.makedirs(pdir, exist_ok=True)
    bad = pdir / "config.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert pdstate.config_runtime_agnostic_bytes(str(bad)) is None
    assert pdstate.config_runtime_agnostic_bytes(str(pdir / "missing.yaml")) is None


def test_overrides_do_not_change_entry_count_or_paths(tmp_path):
    """overrides 只换单文件内容口径：条目集合与路径不变。"""
    import evidence  # noqa: E402

    pdir, paths = _snapshot(tmp_path, "main_title: x\n", source_map="# map\n")
    base_digest, base_entries = evidence.digest_paths(paths)
    cfg_path = os.path.join(pdir, "config.yaml")
    with open(cfg_path, "rb") as f:
        raw = f.read()
    over_digest, over_entries = evidence.digest_paths(
        paths, {cfg_path: b"replaced content"})
    assert base_digest != over_digest
    assert [e["path"] for e in base_entries] == [e["path"] for e in over_entries]
    cfg_entry = [e for e in over_entries if e["path"].endswith("config.yaml")][0]
    assert cfg_entry["sha256"] == evidence.sha256_bytes(b"replaced content")

    # 缺失路径带 override 也不放行：仍记 None
    missing = os.path.join(pdir, "nope.yaml")
    _, entries = evidence.digest_paths([missing], {missing: b"x"})
    assert entries[0]["sha256"] is None


def test_runtime_agnostic_bytes_survives_missing_yaml(tmp_path, monkeypatch):
    """PyYAML 不可用时不得崩：退回整文件口径（None）。"""
    import builtins  # noqa: E402

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml here")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    pdir = tmp_path / "self"
    os.makedirs(pdir, exist_ok=True)
    cfg = pdir / "config.yaml"
    cfg.write_text("main_title: x\n", encoding="utf-8")
    assert pdstate.config_runtime_agnostic_bytes(str(cfg)) is None
