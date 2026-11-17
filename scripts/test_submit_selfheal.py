#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_submit_selfheal.py — T2 提交环 TOCTOU 自愈 + 契约/内容失败分流回归。

背景（本工作区复现的插件缺陷）：
``node_t2_submit_and_check_sections`` 进节点读一次 status 快照，第一次
``pdstate.py submit`` 失败后仍用该快照判 stale；而 pdstate 检查 3（输入摘要
漂移）正是在 submit 调用**内部**把任务标 stale——于是「submit 过程中被标
stale」这一最常见情形自愈分支从未触发，契约失败被包成 repair_reason 派给
内容返修专员：写作者不改 → no_change HALTED；改 → 重审已 PASS 内容并白烧
返修预算。两类 Agent 都修不了。

本测试锁定修复后的行为：

A1 submit 失败后重读状态、走 mark ready → start 重冻结 → 纳管 → 重试 submit 成功；
A2 候选内容结构失败 → 回写作者（REPAIR_SECTION）；
A3 契约/新鲜度失败 → 定向 HALTED 并附人工处置命令，不再误派内容返修；
A4 失败分类器覆盖 pdstate 全部五类检查的失败串。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.nodes import section_nodes as sn  # noqa: E402

#: pdstate 真实失败串（scripts/pdstate.py cmd_submit 五类检查）
MSG_DIGEST_DRIFT = ("[pdstate] [冲突] sec1 的输入摘要已变化，候选结果作废（stale）；"
                    "请按新输入重新派发（mark ready → start 重冻结基线），"
                    "不得以迟到结果覆盖当前版本。")
MSG_STALE_STATUS = ("[pdstate] [冲突] sec1 当前状态为 stale，不能 submit"
                    "（stale 须先 mark ready 再 start 新 attempt）")
MSG_STRUCTURE = "[pdstate] [冲突] 候选切片不得包含 \\documentclass（须为纯片段）"
MSG_NO_ATTEMPT = "[pdstate] [冲突] sec1 没有当前 attempt"


class _SelfhealRunner:
    """ScriptRunner 替身：可编程的 submit 结果 + 可编程的盘副作用。

    ``submit_results`` 按顺序弹出 (code, out, err)；``mark_stale_on_first_submit``
    模拟 pdstate 检查 3 的副作用——在 submit 调用内部把任务标 stale。
    """

    def __init__(self, project_root, scripts_dir=None):
        self.project_root = project_root
        self.calls = []
        self.submit_results = []
        self.mark_stale_on_first_submit = False

    def run_script(self, script_name, args, timeout=300, cwd=None):
        self.calls.append((script_name, list(args)))
        if script_name == "pdstate.py" and args and args[1] == "submit":
            if self.mark_stale_on_first_submit:
                self.mark_stale_on_first_submit = False
                _write_task_status(self.project_root, args[2], "stale")
            if self.submit_results:
                return self.submit_results.pop(0)
            return (0, "ok", "")
        return (0, "ok", "")

    def run_gate_script(self, *a, **k):
        return None


class _FixedRunner:
    """永远失败的 submit 替身（不产生任何盘副作用）。"""

    def __init__(self, project_root, scripts_dir=None):
        self.project_root = project_root
        self.calls = []

    def run_script(self, script_name, args, timeout=300, cwd=None):
        self.calls.append((script_name, list(args)))
        return (1, "", MSG_STRUCTURE)

    def run_gate_script(self, *a, **k):
        return None


def _write_task_status(d, sec, status):
    path = os.path.join(d, ".pd", "run.json")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        run = json.load(f)
    run.setdefault("tasks", {}).setdefault(sec, {})["status"] = status
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _touch_section(d, sec, text="% qid: sec1-q1\n"):
    with open(os.path.join(d, sec + ".tex"), "w", encoding="utf-8") as f:
        f.write(text)


def _write_run_json(d, sec, status="running", current_attempt=1):
    pd = os.path.join(d, ".pd")
    os.makedirs(pd, exist_ok=True)
    with open(os.path.join(pd, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "run_id": "run-15",
                   "slices": [sec],
                   "tasks": {sec: {"task_id": sec, "status": status,
                                   "attempts": [], "current_attempt": current_attempt,
                                   "input_sources": None, "input_digest": None,
                                   "usage": {}}}}, f, ensure_ascii=False, indent=2)


def _legacy_state(d, sec="sec1"):
    return {
        "self_dir": d,
        "project_root": d,
        "run_id": "run-15",
        "expected_sections": [sec],
        "section_statuses": {},
        "history": [],
        "dispatch_results": {},
        "dispatch_metrics": {},
        "dispatch_history": [],
        "active_section": sec,
    }


def _script_names(runner):
    return [" ".join(a[1:]) for _s, a in runner.calls]


def _write_attempt(d, sec, aid=1, digest="frozen"):
    """落一个 frozen attempt 目录（_import_direct_candidate 的前置条件）。"""
    adir = os.path.join(d, ".pd", "candidates", sec, "attempt-%04d" % aid)
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "task.json"), "w", encoding="utf-8") as f:
        json.dump({"run_id": "run-15", "task_id": sec, "attempt_id": aid,
                   "input_digest": digest}, f)
    return adir


class A1_StaleSelfHeal(unittest.TestCase):
    """A1：submit 过程中被标 stale → 重读状态后重冻结自愈。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, "sec1", status="running")
        _write_attempt(self.d, "sec1")

    def _run_node(self, runner):
        state = _legacy_state(self.d)
        with mock.patch.object(sn, "ScriptRunner", lambda *a, **k: runner), \
             mock.patch("workflow.nodes.semantic_nodes.semantic_gate_verdict",
                        return_value=("PASS", "")), \
             mock.patch.object(sn, "_sync_required_acceptance_scopes",
                               lambda *a, **k: None):
            return sn.node_t2_submit_and_check_sections(state)

    def test_stale_marked_during_submit_triggers_refreeze(self):
        runner = _SelfhealRunner(self.d)
        runner.submit_results = [(1, "", MSG_DIGEST_DRIFT), (0, "ok", "")]
        runner.mark_stale_on_first_submit = True
        patch = self._run_node(runner)

        self.assertEqual(patch["routing_action"], "PROCEED",
                         "自愈后必须走通，不得转返修/HALT")
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        cmds = _script_names(runner)
        self.assertIn("mark sec1 ready", cmds, "缺 mark ready: %s" % cmds)
        self.assertIn("start sec1", cmds, "缺 start 重冻结: %s" % cmds)
        submits = [c for c in cmds if c.endswith("submit sec1")]
        self.assertEqual(len(submits), 2, "应重试一次 submit: %s" % cmds)
        self.assertTrue(any("重冻结自愈" in h for h in patch["history"]),
                        "历史应记录自愈路径: %s" % patch["history"])

    def test_stale_snapshot_before_node_still_selfheals(self):
        """回归：进节点前已是 stale（旧代码已覆盖的情形）不得退步。"""
        _write_task_status(self.d, "sec1", "stale")
        runner = _SelfhealRunner(self.d)
        runner.submit_results = [(1, "", MSG_DIGEST_DRIFT), (0, "ok", "")]
        patch = self._run_node(runner)
        self.assertEqual(patch["routing_action"], "PROCEED")
        cmds = _script_names(runner)
        self.assertIn("mark sec1 ready", cmds)
        self.assertIn("start sec1", cmds)

    def test_fast_path_selfheals_too(self):
        """faithful_fast 路径同享自愈逻辑（prebuild 纳管）。"""
        state = _legacy_state(self.d)
        state.update({
            "execution_profile": "faithful_fast",
            "execution_schema_version": 2,
            "graph_version": "faithful-fast-v1",
        })
        runner = _SelfhealRunner(self.d)
        runner.submit_results = [(1, "", MSG_DIGEST_DRIFT), (0, "ok", "")]
        runner.mark_stale_on_first_submit = True
        with mock.patch.object(sn, "ScriptRunner", lambda *a, **k: runner), \
             mock.patch.object(sn, "_sync_required_acceptance_scopes",
                               lambda *a, **k: None):
            patch = sn.node_t2_submit_and_check_sections(state)
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["gate_phase"], "prebuild")
        self.assertEqual(patch["section_statuses"]["sec1"], "prebuild_ready")
        cmds = _script_names(runner)
        self.assertIn("start sec1", cmds, "fast 路径也须重冻结: %s" % cmds)
        self.assertNotIn("mark sec1 checked", cmds,
                         "prebuild 阶段不得 mark checked")


class A2_ContentFailureRoutesToWriter(unittest.TestCase):
    """A2：候选内容结构失败仍回写作者（不因修 TOCTOU 而丢旧语义）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _touch_section(self.d, "sec1", "\\documentclass{article}\n")
        _write_run_json(self.d, "sec1", status="running")
        _write_attempt(self.d, "sec1")

    def test_content_failure_goes_to_repair(self):
        runner = _FixedRunner(self.d)
        state = _legacy_state(self.d)
        with mock.patch.object(sn, "ScriptRunner", lambda *a, **k: runner), \
             mock.patch("workflow.nodes.semantic_nodes.semantic_gate_verdict",
                        return_value=("PASS", "")), \
             mock.patch.object(sn, "_sync_required_acceptance_scopes",
                               lambda *a, **k: None):
            patch = sn.node_t2_submit_and_check_sections(state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec1")
        self.assertIn("切片提交失败", patch["repair_reason"])
        cmds = _script_names(runner)
        self.assertNotIn("mark sec1 ready", cmds,
                         "内容失败与契约无关，不得顺手改契约状态")
        self.assertNotIn("start sec1", cmds)


class A3_ContractFailureHalts(unittest.TestCase):
    """A3：契约/新鲜度失败定向 HALT，不再误派内容返修。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, "sec1", status="running", current_attempt=None)
        _write_attempt(self.d, "sec1")

    def _run(self, runner):
        state = _legacy_state(self.d)
        with mock.patch.object(sn, "ScriptRunner", lambda *a, **k: runner), \
             mock.patch("workflow.nodes.semantic_nodes.semantic_gate_verdict",
                        return_value=("PASS", "")), \
             mock.patch.object(sn, "_sync_required_acceptance_scopes",
                               lambda *a, **k: None):
            return sn.node_t2_submit_and_check_sections(state)

    def test_no_attempt_halts_with_remediation(self):
        runner = _FixedRunner(self.d)
        runner.run_script = lambda script_name, args, timeout=300, cwd=None: (
            runner.calls.append((script_name, list(args))) or (1, "", MSG_NO_ATTEMPT))
        patch = self._run(runner)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("处置", patch["error_message"])
        self.assertIn("mark sec1 ready", patch["error_message"])
        self.assertNotIn("repair_target", patch,
                         "契约失败不得派给内容返修专员")

    def test_stale_status_failure_halts(self):
        """submit 报 stale（说明状态已被改）但重读仍是 running：不可自愈 → HALT。"""
        _write_task_status(self.d, "sec1", "running")

        class Runner(_SelfhealRunner):
            def run_script(self, script_name, args, timeout=300, cwd=None):
                self.calls.append((script_name, list(args)))
                if script_name == "pdstate.py" and args and args[1] == "submit":
                    if self.submit_results:
                        return self.submit_results.pop(0)
                return (0, "ok", "")

        runner = Runner(self.d)
        runner.submit_results = [(1, "", MSG_STALE_STATUS), (1, "", MSG_STALE_STATUS)]
        patch = self._run(runner)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("当前状态为 stale", patch["error_message"])


class A4_Classifier(unittest.TestCase):
    """A4：失败分类器对 pdstate 五类检查的覆盖。"""

    def test_contract_markers(self):
        for msg in (MSG_DIGEST_DRIFT, MSG_STALE_STATUS, MSG_NO_ATTEMPT,
                    "[pdstate] [冲突] 缺少冻结的 task.json",
                    "[pdstate] [冲突] task.json 的 input_digest=abc 与当前派发 def 不一致",
                    "[pdstate] [冲突] result.json 复述的 run_id=r1",
                    "[pdstate] [冲突] result.json 输出路径 sec1.tex",
                    "[pdstate] [冲突] 候选文件哈希 与 result.json 不符"):
            self.assertEqual(sn._classify_submit_failure(msg), "contract", msg)

    def test_content_markers(self):
        for msg in (MSG_STRUCTURE, "[pdstate] 候选文件为空"):
            self.assertEqual(sn._classify_submit_failure(msg), "content", msg)

    def test_unknown_failure_is_contract(self):
        """未知失败按 contract：纳管已完成的前提下，写作者改内容修不了。"""
        self.assertEqual(sn._classify_submit_failure("某些没见过的错误"), "contract")

    def test_patch_shapes(self):
        history = []
        contract = sn._submit_failure_patch("sec1", MSG_DIGEST_DRIFT,
                                            "contract", "/tmp/self", history)
        self.assertEqual(contract["routing_action"], "HALT")
        self.assertEqual(contract["stage"], "HALTED")
        self.assertTrue(contract["error_message"].startswith("切片提交契约失败"))

        history2 = []
        content = sn._submit_failure_patch("sec1", MSG_STRUCTURE,
                                           "content", "/tmp/self", history2)
        self.assertEqual(content["routing_action"], "REPAIR_SECTION")
        self.assertEqual(content["repair_target"], "sec1")
        self.assertEqual(content["repair_reason"], "切片提交失败: " + MSG_STRUCTURE)


class A5_HelperSequence(unittest.TestCase):
    """A5：_pdstate_submit_with_selfheal 的脚本时序与历史串。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _touch_section(self.d, "sec1")
        _write_run_json(self.d, "sec1", status="running")
        adir = os.path.join(self.d, ".pd", "candidates", "sec1", "attempt-0001")
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"run_id": "run-15", "task_id": "sec1", "attempt_id": 1,
                       "input_digest": "frozen"}, f)

    def test_sequence_on_drift_selfheal(self):
        runner = _SelfhealRunner(self.d)
        runner.submit_results = [(1, "", MSG_DIGEST_DRIFT), (0, "ok", "")]
        runner.mark_stale_on_first_submit = True
        history = []
        code, msg, kind = sn._pdstate_submit_with_selfheal(
            self.d, "sec1", runner, history)
        self.assertEqual((code, kind), (0, ""))
        cmds = _script_names(runner)
        self.assertEqual(cmds, ["submit sec1", "mark sec1 ready", "start sec1",
                                "submit sec1"], cmds)
        self.assertTrue(any("纳管" in h for h in history), history)
        self.assertTrue(any("成功提升正式切片" in h for h in history), history)

    def test_sequence_when_selfheal_fails(self):
        runner = _SelfhealRunner(self.d)
        runner.submit_results = [(1, "", MSG_DIGEST_DRIFT),
                                 (1, "", MSG_DIGEST_DRIFT)]
        runner.mark_stale_on_first_submit = True
        history = []
        code, msg, kind = sn._pdstate_submit_with_selfheal(
            self.d, "sec1", runner, history)
        self.assertEqual(code, 1)
        self.assertEqual(kind, "contract")
        self.assertIn("输入摘要已变化", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
