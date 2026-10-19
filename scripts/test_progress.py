#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_progress.py — audit §14 UI 进度指标（scripts/progress.py）回归

覆盖：
    T01  未初始化工程 → 空快照 + 退出码 0（不误报失败）
    T02  run.json 任务状态 → scope 完成度计数（queued/running/done/reused）
    T03  派发计划在盘 → 阶段标签（写作轮 / 语义审读轮）
    T04  派发单状态 → in_flight / queue_depth
    T05  证据台账 duration → ETA 估算
    T06  --json 结构化快照落盘

运行: python -m unittest discover scripts -p "test_progress.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import progress  # noqa: E402
from workflow.nodes import agent_evidence  # noqa: E402


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def make_run(self_dir, statuses):
    return _write(os.path.join(self_dir, ".pd", "run.json"), {
        "schema_version": 1,
        "run_id": "run-progress",
        "created": "2024-01-01T00:00:00+0000",
        "slices": list(statuses),
        "digests": {},
        "tasks": {tid: {"task_id": tid, "status": status, "attempts": [],
                        "current_attempt": None, "input_sources": None,
                        "input_digest": None, "usage": {}}
                  for tid, status in statuses.items()},
    })


class T01Uninitialized(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_missing_run_json(self):
        snap = progress.progress_snapshot(self.d)
        self.assertIsNone(snap["run_id"])
        self.assertEqual(snap["stage"], "T1 规划/派发前")
        rc = progress.main([self.d])
        self.assertEqual(rc, 0)


class T02ScopeCounts(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        make_run(self.self_dir, {"sec1": "checked", "sec2": "running",
                                 "sec3": "ready", "sec4": "reused",
                                 "sec5": "planned"})

    def test_scope_buckets(self):
        snap = progress.progress_snapshot(self.self_dir)
        self.assertEqual(snap["scopes"], {"total": 5, "queued": 2,
                                          "running": 1, "done": 2,
                                          "reused": 1})


class T03StageLabels(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        make_run(self.self_dir, {"sec1": "running"})

    def test_author_round_label(self):
        _write(os.path.join(self.self_dir, ".pd", "dispatch-plan.json"),
               {"batch_id": "bat-1", "stage": "T2", "round": "author",
                "sections": ["sec1"], "requests": [{"scope": "sec1"}],
                "max_concurrency": 4})
        snap = progress.progress_snapshot(self.self_dir)
        self.assertEqual(snap["stage"], "T2 写作轮")

    def test_semantic_round_label(self):
        _write(os.path.join(self.self_dir, ".pd", "dispatch-plan.json"),
               {"batch_id": "bat-1", "stage": "T2", "round": "author",
                "sections": ["sec1"], "requests": [], "max_concurrency": 4})
        _write(os.path.join(self.self_dir, ".pd",
                            "dispatch-plan-semantic.json"),
               {"batch_id": "bat-2", "stage": "T2_CHECK", "round": "semantic",
                "sections": ["sec1"], "requests": [{"scope": "sec1"}],
                "max_concurrency": 4})
        snap = progress.progress_snapshot(self.self_dir)
        self.assertEqual(snap["stage"], "T2 语义审读轮")


class T04QueueDepth(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        make_run(self.self_dir, {"sec1": "running", "sec2": "running"})

    def test_request_status_counts(self):
        for sec, status in (("sec1", "running"), ("sec2", "pending")):
            _write(os.path.join(self.self_dir, ".pd", "agent-requests",
                                "dsp-%s.json" % sec),
                   {"dispatch_id": "dsp-%s" % sec, "scope": sec,
                    "status": status})
        snap = progress.progress_snapshot(self.self_dir)
        self.assertEqual(snap["in_flight"], 1)
        self.assertEqual(snap["queue_depth"], 1)


class T05Eta(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        make_run(self.self_dir, {"sec1": "running", "sec2": "ready",
                                 "sec3": "ready"})
        _write(os.path.join(self.self_dir, ".pd", "dispatch-plan.json"),
               {"batch_id": "bat-1", "stage": "T2", "round": "author",
                "sections": ["sec1", "sec2", "sec3"], "requests": [],
                "max_concurrency": 2})

    def test_eta_from_evidence_durations(self):
        for i in (1, 2):
            agent_evidence.write_evidence(
                self.self_dir, dispatch_id="dsp-sec%d" % i,
                batch_id="bat-old", worker="subagent:test",
                duration_ms=10000)
        snap = progress.progress_snapshot(self.self_dir)
        self.assertEqual(snap["avg_dispatch_ms"], 10000)
        # 剩余 3 个 scope（running+queued），并发 2 → ≈15s
        self.assertEqual(snap["eta_seconds"], 15.0)


class T06JsonSnapshot(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_prog_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        make_run(self.self_dir, {"sec1": "checked"})

    def test_json_report_written(self):
        report = os.path.join(self.self_dir, ".pd", "reports",
                              "progress.json")
        rc = progress.main([self.self_dir, "--json", report])
        self.assertEqual(rc, 0)
        with open(report, encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["run_id"], "run-progress")
        self.assertEqual(snap["scopes"]["done"], 1)
        self.assertIn("stage", snap)
        self.assertIn("eta_seconds", snap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
