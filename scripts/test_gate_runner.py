#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_gate_runner.py — audit §17 任务7 进程内批量门禁执行器回归测试

覆盖：
    T01  进程内执行 check_dispatch（合法计划 → PASS，报告落盘）
    T02  进程内执行失败计划 → REJECT，错误码透出
    T03  与子进程路径语义一致（force_subprocess → execution_mode=subprocess）
    T04  批量化：一批多 gate 全部产出，共享 batch_input_digest，
         单个 gate 失败不影响其余 gate
    T05  旧报告隔离：.stale 归档（run_id 子目录）+ 新鲜度判定
    T06  进程内崩溃/SystemExit → 收敛为 REJECT/ERROR GateReport 不上抛
    T07  进程内装载失败 → 子进程兜底（execution_mode=subprocess-fallback）
    T08  参数透传：argparse 拒绝非法 --stage → 退出码 2 → REJECT
    T09  模块缓存与 sys.modules 注册
    T10  批量上限护栏（MAX_BATCH_SIZE）
    T11  batch_input_digest：同内容同摘要，输入变更即变
    T12  node_t3_checks 接线：五个纯 Python 门禁全部进程内执行

运行: python -m unittest discover scripts -p "test_gate_runner.py" -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow.adapters.gate_runner import (  # noqa: E402
    GateRunner,
    MAX_BATCH_SIZE,
    PURE_PYTHON_GATES,
)


DIGEST = "a" * 64


def make_plan(**overrides):
    plan = {
        "batch_id": "bat-t2-0001",
        "stage": "T2",
        "created_at": "2024-01-01T00:00:00+00:00",
        "max_concurrency": 4,
        "min_parallelism": 1,
        "sections": ["sec0", "sec1", "sec2"],
        "required_scope_count": 3,
        "requests": [
            {"dispatch_id": "dsp-t2-sec0-0000000001", "scope": "sec0",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec0.tex"], "status": "pending"},
            {"dispatch_id": "dsp-t2-sec1-0000000002", "scope": "sec1",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec1.tex"], "status": "pending"},
            {"dispatch_id": "dsp-t2-sec2-0000000003", "scope": "sec2",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec2.tex"], "status": "pending"},
        ],
    }
    plan.update(overrides)
    return plan


def write_plan(d, plan):
    pd_dir = os.path.join(d, ".pd")
    os.makedirs(pd_dir, exist_ok=True)
    path = os.path.join(pd_dir, "dispatch-plan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


def dispatch_spec(d, report_rel=".pd/reports/check_dispatch.json",
                  gate_name="check_dispatch"):
    return {
        "gate_name": gate_name,
        "script_name": "check_dispatch.py",
        "args": [d],
        "report_rel_path": report_rel,
    }


def write_scripts_file(d, name, text):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class T01_InProcessPass(unittest.TestCase):
    def test_valid_plan_passes_in_process(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            gr = GateRunner(d)
            rep = gr.run_gate(
                "check_dispatch", "check_dispatch.py", [d],
                ".pd/reports/check_dispatch.json")
            self.assertEqual(rep["status"], "PASS")
            self.assertTrue(rep["passed"])
            self.assertEqual(rep["exit_code"], 0)
            self.assertTrue(rep["report_is_fresh"])
            self.assertEqual(rep["execution_mode"], "in-process")
            # 报告按 report_rel_path 落盘且内容正确
            rp = os.path.join(d, ".pd", "reports", "check_dispatch.json")
            self.assertTrue(os.path.isfile(rp))
            with open(rp, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["status"], "pass")
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["checked"], {"sections": 3, "requests": 3})


class T02_InProcessFail(unittest.TestCase):
    def test_under_dispatch_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = make_plan()["requests"]
            write_plan(d, make_plan(requests=reqs[:1]))
            gr = GateRunner(d)
            rep = gr.run_gate(
                "check_dispatch", "check_dispatch.py", [d],
                ".pd/reports/check_dispatch.json")
            self.assertEqual(rep["status"], "REJECT")
            self.assertFalse(rep["passed"])
            self.assertEqual(rep["exit_code"], 1)
            codes = [e["code"] for e in (rep["raw_report"].get("errors") or [])]
            self.assertIn("UNDER_DISPATCH", codes)

    def test_missing_plan_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            gr = GateRunner(d)
            rep = gr.run_gate(
                "check_dispatch", "check_dispatch.py", [d],
                ".pd/reports/check_dispatch.json")
            self.assertEqual(rep["status"], "REJECT")
            codes = [e["code"] for e in (rep["raw_report"].get("errors") or [])]
            self.assertEqual(codes, ["PLAN_MISSING"])


class T03_SubprocessParity(unittest.TestCase):
    def test_force_subprocess_same_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            gr = GateRunner(d)
            rep = gr.run_gate(
                "check_dispatch", "check_dispatch.py", [d],
                ".pd/reports/check_dispatch.json", force_subprocess=True)
            self.assertEqual(rep["status"], "PASS")
            self.assertEqual(rep["execution_mode"], "subprocess")
            self.assertEqual(rep["raw_report"]["checked"],
                             {"sections": 3, "requests": 3})


class T04_Batch(unittest.TestCase):
    def test_batch_runs_all_and_shares_digest(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            gr = GateRunner(d)
            reports = gr.run_gates([
                dispatch_spec(d, ".pd/reports/check_dispatch.json"),
                dict(dispatch_spec(d, ".pd/reports/check_dispatch_alt.json"),
                     gate_name="check_dispatch_alt"),
            ])
            self.assertEqual(sorted(reports), ["check_dispatch",
                                               "check_dispatch_alt"])
            for rep in reports.values():
                self.assertEqual(rep["status"], "PASS")
                self.assertEqual(rep["execution_mode"], "in-process")
                self.assertIsNotNone(rep.get("batch_input_digest"))
            self.assertEqual(reports["check_dispatch"]["batch_input_digest"],
                             reports["check_dispatch_alt"]["batch_input_digest"])

    def test_batch_failure_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as d:
            # 两个 spec 都缺计划文件 → 均 REJECT；互不阻断、各自留证
            gr = GateRunner(d)
            reports = gr.run_gates([
                dispatch_spec(d, ".pd/reports/a.json", gate_name="dispatch_a"),
                dispatch_spec(d, ".pd/reports/b.json", gate_name="dispatch_b"),
            ])
            self.assertEqual(sorted(reports), ["dispatch_a", "dispatch_b"])
            for rep in reports.values():
                self.assertEqual(rep["status"], "REJECT")

    def test_batch_too_large_raises(self):
        with tempfile.TemporaryDirectory() as d:
            gr = GateRunner(d)
            specs = [dict(dispatch_spec(d, ".pd/reports/x%d.json" % i),
                          gate_name="g%d" % i)
                     for i in range(MAX_BATCH_SIZE + 1)]
            with self.assertRaises(ValueError):
                gr.run_gates(specs)


class T05_StaleQuarantine(unittest.TestCase):
    def test_old_report_quarantined_and_fresh_written(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            rp = os.path.join(d, ".pd", "reports", "check_dispatch.json")
            os.makedirs(os.path.dirname(rp), exist_ok=True)
            with open(rp, "w", encoding="utf-8") as f:
                f.write('{"status": "pass", "stale": true}')
            gr = GateRunner(d)
            rep = gr.run_gate("check_dispatch", "check_dispatch.py", [d],
                              ".pd/reports/check_dispatch.json",
                              run_id="run-42")
            self.assertTrue(rep["report_is_fresh"])
            self.assertFalse(rep["raw_report"].get("stale", False))
            # run_id 不可变归档目录
            stale_dir = os.path.join(d, ".pd", "reports", ".stale", "run-42")
            self.assertTrue(os.path.isdir(stale_dir))
            archived = os.listdir(stale_dir)
            self.assertEqual(len(archived), 1)
            with open(os.path.join(stale_dir, archived[0]),
                      encoding="utf-8") as f:
                self.assertIn("stale", f.read())

    def test_no_stale_dir_without_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            rp = os.path.join(d, ".pd", "reports", "check_dispatch.json")
            os.makedirs(os.path.dirname(rp), exist_ok=True)
            with open(rp, "w", encoding="utf-8") as f:
                f.write('{"status": "pass", "stale": true}')
            gr = GateRunner(d)
            gr.run_gate("check_dispatch", "check_dispatch.py", [d],
                        ".pd/reports/check_dispatch.json")
            stale_dir = os.path.join(d, ".pd", "reports", ".stale")
            self.assertTrue(os.path.isdir(stale_dir))
            # 旧布局：归档文件直接躺在 .stale/ 下，无 run_id 子目录
            entries = os.listdir(stale_dir)
            self.assertTrue(any(n.startswith("check_dispatch.json.")
                                for n in entries), entries)
            self.assertFalse(any(os.path.isdir(os.path.join(stale_dir, n))
                                 for n in entries), entries)


class T06_InProcessCrash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scripts = os.path.join(self.tmp.name, "scripts")
        os.makedirs(self.scripts, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_raises_converges_to_reject(self):
        write_scripts_file(self.scripts, "crash_gate.py",
                           "def main():\n    raise RuntimeError('boom')\n")
        d = os.path.join(self.tmp.name, "proj")
        os.makedirs(d, exist_ok=True)
        gr = GateRunner(d, scripts_dir=self.scripts)
        rep = gr.run_gate("crash", "crash_gate.py", [d],
                          ".pd/reports/crash.json")
        self.assertEqual(rep["status"], "REJECT")
        self.assertFalse(rep["passed"])
        self.assertEqual(rep["exit_code"], 1)
        cats = [i["category"] for i in rep["issues"]]
        self.assertIn("EXECUTION_FAILURE", cats)

    def test_system_exit_code_honoured(self):
        write_scripts_file(self.scripts, "exit_gate.py",
                           "import sys\n\n\ndef main():\n"
                           "    sys.exit(2)\n")
        d = os.path.join(self.tmp.name, "proj2")
        os.makedirs(d, exist_ok=True)
        gr = GateRunner(d, scripts_dir=self.scripts)
        rep = gr.run_gate("exit2", "exit_gate.py", [d],
                          ".pd/reports/exit.json")
        self.assertEqual(rep["status"], "REJECT")  # code 2 < 120 → REJECT
        self.assertEqual(rep["exit_code"], 2)

    def test_missing_script_in_batch_converges_to_error(self):
        d = os.path.join(self.tmp.name, "proj3")
        os.makedirs(d, exist_ok=True)
        gr = GateRunner(d)
        reports = gr.run_gates([
            {"gate_name": "ghost", "script_name": "no_such_gate.py",
             "args": [d], "report_rel_path": ".pd/reports/ghost.json"},
        ])
        rep = reports["ghost"]
        self.assertEqual(rep["status"], "ERROR")
        self.assertFalse(rep["passed"])
        cats = [i["category"] for i in rep["issues"]]
        self.assertIn("EXECUTION_FAILURE", cats)


class T07_ImportFallback(unittest.TestCase):
    def test_import_failure_falls_back_to_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            gr = GateRunner(d)

            def boom(name):
                raise ImportError("simulated import failure")

            gr._import_module = boom  # 进程内装载失败 → 子进程兜底
            rep = gr.run_gate("check_dispatch", "check_dispatch.py", [d],
                              ".pd/reports/check_dispatch.json")
            self.assertEqual(rep["status"], "PASS")
            self.assertEqual(rep["execution_mode"], "subprocess-fallback")
            self.assertIn("fallback_reason", rep)


class T08_ArgsForwarded(unittest.TestCase):
    def test_invalid_stage_arg_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            gr = GateRunner(d)
            rep = gr.run_gate("check_budget", "check_budget.py",
                              [d, "--stage", "bogus"],
                              ".pd/reports/check_budget.json")
            # argparse 拒绝非法 --stage → SystemExit(2) → REJECT
            self.assertEqual(rep["exit_code"], 2)
            self.assertEqual(rep["status"], "REJECT")
            cats = [i["category"] for i in rep["issues"]]
            self.assertIn("EXECUTION_FAILURE", cats)


class T09_ModuleCache(unittest.TestCase):
    def test_import_is_cached_and_registered(self):
        with tempfile.TemporaryDirectory() as d:
            gr = GateRunner(d)
            m1 = gr._import_module("check_dispatch.py")
            m2 = gr._import_module("check_dispatch.py")
            self.assertIs(m1, m2)
            self.assertIs(sys.modules.get("check_dispatch"), m1)

    def test_pure_python_gate_table(self):
        self.assertIn("check_dialogue", PURE_PYTHON_GATES)
        self.assertIn("check_numbering", PURE_PYTHON_GATES)
        self.assertIn("check_budget", PURE_PYTHON_GATES)
        self.assertIn("check_pedagogy", PURE_PYTHON_GATES)
        self.assertIn("check_fidelity", PURE_PYTHON_GATES)
        # XeLaTeX / HTML 构建不得入表（进程内不跑编译器）
        for name in PURE_PYTHON_GATES.values():
            self.assertTrue(name.startswith("check_"))


class T10_InputDigest(unittest.TestCase):
    def test_digest_stable_and_input_sensitive(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            gr = GateRunner(d)
            d1 = gr.batch_input_digest()
            self.assertIsNotNone(d1)
            self.assertEqual(len(d1), 64)
            self.assertEqual(gr.batch_input_digest(), d1)
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
                f.write("\\input{sec1}\n")
            d2 = gr.batch_input_digest()
            self.assertNotEqual(d1, d2)


class T11_T3Integration(unittest.TestCase):
    """node_t3_checks 接线：五个纯 Python 门禁全部进程内执行。"""

    def test_t3_runs_all_gates_in_process(self):
        from workflow.nodes import build_nodes
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
                f.write("\\documentclass{article}\n\\input{sec1}\n"
                        "\\end{document}\n")
            with open(os.path.join(d, "sec1.tex"), "w", encoding="utf-8") as f:
                f.write("% qid: sec1-entry\n\\begin{probchain}\n"
                        "\\item 示例\\begin{solution}解\\end{solution}\n"
                        "\\end{probchain}\n")
            state = {"self_dir": d, "project_root": d, "history": [],
                     "replan_count": 0}
            patch = build_nodes.node_t3_checks(state)
            # 批量执行：五份报告全部进 state（级联早失败也保留全量证据），
            # 且都是进程内执行、共享同一输入快照摘要
            for key in ("pedagogy_gate", "check_numbering_gate",
                        "dialogue_gate", "fidelity_gate", "budget_gate"):
                self.assertIn(key, patch)
                self.assertEqual(patch[key]["execution_mode"], "in-process")
                self.assertIsNotNone(patch[key].get("batch_input_digest"))
            digests = {patch[k]["batch_input_digest"] for k in
                       ("pedagogy_gate", "check_numbering_gate",
                        "dialogue_gate", "fidelity_gate", "budget_gate")}
            self.assertEqual(len(digests), 1)
            # 缺 Phase 0 契约/预算输入的裸工程必然进修复回路（REPLAN 或修节）
            self.assertEqual(patch["stage"], "REPAIR_LOOP")
            self.assertIn(patch["routing_action"],
                          ("REPAIR_SECTION", "REPLAN", "HALT"))
            # 级联路由只判定到首个失败门禁，history 至少记录首个失败门
            history_text = " | ".join(patch["history"])
            self.assertIn("check_pedagogy", history_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
