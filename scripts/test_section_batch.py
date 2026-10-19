#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_section_batch.py — P1 T2 fan-out 派发节点回归测试（unittest + 临时目录）

覆盖 T01–T12 + 一个真实 pdstate/check_dispatch 集成用例；只构造临时工程与替身
（FakeRunner / FakePool / monkeypatch interrupt），不触碰真实 self/。

测试对象：workflow/nodes/section_batch_nodes.py
    node_t2_plan_dispatch / node_t2_dispatch_sections / node_t2_join_sections

运行: python -m unittest discover scripts -p "test_section_batch.py" -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import check_dispatch  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes.section_batch_nodes import (  # noqa: E402
    node_t2_dispatch_sections,
    node_t2_join_sections,
    node_t2_plan_dispatch,
)
from workflow.adapters.worker_pool import DispatchResult  # noqa: E402

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 治具
# ---------------------------------------------------------------------------

def write_config(d, **overrides):
    """写一份带 agent_* 派发配置的 config.yaml（扁平单行标量）。

    嵌套键（allow_unattested / model_map）自动改写为 ``agent_dispatch:``
    映射块（q_audit.md 证据链改造新增的多态配置形态）。
    """
    nested = {
        key: overrides.pop(key)
        for key in ("allow_unattested", "model_map")
        if key in overrides
    }
    cfg = {
        "agent_dispatch": "safe",
        "agent_max_concurrency": 0,
        "agent_batch_window": 2,
        "agent_timeout_seconds": 900,
        "agent_max_retries": 2,
    }
    cfg.update(overrides)
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
        for key, value in cfg.items():
            f.write(f"{key}: {value}\n")
        if nested:
            f.write("agent_dispatch:\n")
            f.write(f"  profile: {cfg['agent_dispatch']}\n")
            for key, value in nested.items():
                if isinstance(value, dict):
                    f.write(f"  {key}:\n")
                    for sub_key, sub_val in value.items():
                        if isinstance(sub_val, dict):
                            f.write(f"    {sub_key}:\n")
                            for k3, v3 in sub_val.items():
                                f.write(f"      {k3}: {v3}\n")
                        else:
                            f.write(f"    {sub_key}: {sub_val}\n")
                else:
                    f.write(f"  {key}: {value}\n")
    return os.path.join(d, "config.yaml")


def seed_agent_calls(d, requests, worker="subagent:test-agent"):
    """为每个请求落一份代理证据台账（.pd/agent-calls/<dispatch_id>.json）。

    模拟 qchain_dispatch 派发宿主子 Agent 完成产物写入后的证据链落盘——
    q_audit.md 证据链改造后，磁盘重推导的唯一放行依据。摘要按盘面现值计算，
    故必须在产物写盘之后调用。
    """
    from workflow.nodes import agent_evidence

    for req in requests:
        digests = {}
        for rel in (req.get("artifacts") or []):
            if not isinstance(rel, str):
                continue
            path = os.path.join(d, rel)
            if os.path.isfile(path):
                digests[rel] = sbn.sha256_file(path)
        agent_evidence.write_evidence(
            d,
            dispatch_id=str(req.get("dispatch_id") or ""),
            batch_id=req.get("batch_id"),
            worker=worker,
            artifacts_written=[
                rel for rel in (req.get("artifacts") or [])
                if isinstance(rel, str) and digests.get(rel)
            ],
            output_digests=digests,
        )


def make_state(d, sections=("sec1", "sec2", "sec3"), statuses=None, **extra):
    """最小 state：派发根目录指向临时工程。"""
    state = {
        "self_dir": d,
        "project_root": d,
        "run_id": "test-run",
        "expected_sections": list(sections),
        "section_statuses": dict(statuses or {}),
        "user_spec": {"main_title": "测试学案"},
        "history": [],
        "dispatch_results": {},
        "dispatch_metrics": {},
        "dispatch_history": [],
    }
    state.update(extra)
    return state


def gate_report(status="PASS", errors=None, exit_code=0):
    """GateReport 形状的门禁结果替身（键与 script_runner.GateReport 一致）。"""
    raw = {"tool": "check_dispatch.py", "tool_version": "1.0",
           "status": "pass" if status == "PASS" else "fail",
           "errors": errors or [], "warnings": [], "checked": {}}
    return {
        "gate_name": "check_dispatch",
        "status": status,
        "passed": status == "PASS",
        "issues": [],
        "raw_report": raw,
        "exit_code": exit_code,
        "report_path": ".pd/reports/check_dispatch.json",
        "report_generated_at": "1970-01-01T00:00:00+00:00",
        "report_is_fresh": True,
        "run_id": "test-run",
    }


def register_capable_worker(max_concurrency=4):
    """注册一个声明「异步+可取消+并发 N」的 LLM worker（任务8 治具）。

    audit §17 任务8：真实并行度 = min(档位并发, 声明并发)。不注册时
    worker_registry 保守按串行执行，计划只能承诺 min_parallelism=1。
    返回清理回调（unregister）。
    """
    from workflow.adapters import worker_registry

    def executor(request):  # pragma: no cover - 只用于能力声明，不执行
        raise AssertionError("治具 executor 不应被真实调用")

    worker_registry.set(executor, name="test-capable-llm", capabilities={
        "sync": False, "async": True, "cancellable": True,
        "max_concurrency": max_concurrency})
    return worker_registry.clear


class FakeRunner:
    """ScriptRunner 替身：不起子进程，记录调用并返回预置结果。"""

    gate_result = gate_report("PASS")

    def __init__(self, project_root, scripts_dir=None):
        self.project_root = project_root
        self.calls = []

    def run_script(self, script_name, args, timeout=300, cwd=None):
        self.calls.append(("run_script", script_name, list(args)))
        return (0, "ok", "")

    def run_gate_script(self, gate_name, script_name, args, report_rel_path,
                        timeout=300, run_id=None):
        self.calls.append(("run_gate_script", gate_name, script_name, list(args),
                           report_rel_path, run_id))
        return self.gate_result


class FakePool:
    """WorkerPool 替身：记录 submit，返回预置结果与统计。"""

    def __init__(self, results=None, stats=None):
        self.results = dict(results or {})
        self.stats_snapshot = dict(stats or {})
        self.submitted = []
        self.join_calls = []

    def submit(self, request):
        self.submitted.append(request)
        return request["dispatch_id"]

    def join(self, timeout=None):
        # 幂等：与真实 WorkerPool.join 语义一致——已完成批次立即返回，
        # 不重复 join（audit 任务4：每 batch 只允许一次 join）
        if not self.join_calls:
            self.join_calls.append(timeout)
        return dict(self.results)

    def stats(self):
        base = {"submitted": len(self.submitted), "completed": 0, "ok": 0,
                "failed": 0, "timeout": 0, "no_change": 0, "deferred": 0,
                "retried": 0, "max_in_flight": 0, "orphans": 0}
        base.update(self.stats_snapshot)
        return base

    def pending(self):
        return 0


def run_plan(state, gate=None):
    """用 FakeRunner 跑一次 plan 节点；返回 (patch, FakeRunner 实例)。"""
    FakeRunner.gate_result = gate if gate is not None else gate_report("PASS")
    original = sbn.ScriptRunner
    sbn.ScriptRunner = FakeRunner
    try:
        patch = node_t2_plan_dispatch(state)
    finally:
        sbn.ScriptRunner = original
    return patch


def plan_file(d):
    return os.path.join(d, ".pd", "dispatch-plan.json")


def request_files(d):
    directory = os.path.join(d, ".pd", "agent-requests")
    if not os.path.isdir(directory):
        return []
    return sorted(os.listdir(directory))


# ---------------------------------------------------------------------------
# T01 配置装载
# ---------------------------------------------------------------------------

class T01_DispatchConfig(unittest.TestCase):
    """T01: 档位映射 / 显式覆盖 / 缺省值 / 畸形行容忍。"""

    def test_profile_map(self):
        with tempfile.TemporaryDirectory() as d:
            for profile, expected in (("safe", 1), ("fast", 4), ("budget", 2),
                                      ("interactive", 1)):
                write_config(d, agent_dispatch=profile)
                cfg = sbn._load_dispatch_config(d)
                self.assertEqual(cfg["profile"], profile)
                self.assertEqual(cfg["max_concurrency"], expected)
                self.assertEqual(cfg["configured_max_concurrency"], 0)

    def test_run_scope_default_profile(self):
        """§12.1：未显式配置 agent_dispatch 时按 run_scope 推导缺省档位。"""
        with tempfile.TemporaryDirectory() as d:
            cfg = sbn._load_dispatch_config(d, run_scope="full")
            self.assertEqual(cfg["profile"], "fast")
            self.assertEqual(cfg["max_concurrency"], 4)
            for scope in ("focused", "edit"):
                cfg = sbn._load_dispatch_config(d, run_scope=scope)
                self.assertEqual(cfg["profile"], "interactive")
                self.assertEqual(cfg["max_concurrency"], 1)
            # 未知 run_scope 回落 safe（保守）
            cfg = sbn._load_dispatch_config(d, run_scope="bogus")
            self.assertEqual(cfg["profile"], "safe")
            # 显式 agent_dispatch 永远优先于推导（向后兼容旧工程）
            write_config(d, agent_dispatch="budget")
            cfg = sbn._load_dispatch_config(d, run_scope="full")
            self.assertEqual(cfg["profile"], "budget")
            self.assertEqual(cfg["max_concurrency"], 2)

    def test_explicit_override(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="safe", agent_max_concurrency=5)
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["max_concurrency"], 5)
            self.assertEqual(cfg["configured_max_concurrency"], 5)
            # 显式覆盖优先于档位表
            write_config(d, agent_dispatch="budget", agent_max_concurrency=3)
            self.assertEqual(sbn._load_dispatch_config(d)["max_concurrency"], 3)

    def test_missing_file_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["profile"], "safe")
            self.assertEqual(cfg["max_concurrency"], 1)
            self.assertEqual(cfg["batch_window"], 2)
            self.assertEqual(cfg["timeout_seconds"], 900)
            self.assertEqual(cfg["max_retries"], 2)

    def test_malformed_lines_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
                f.write("# 注释行\n"
                        "agent_dispatch: fast   # 行内注释\n"
                        "这一行没有冒号，应当被跳过\n"
                        "\n"
                        "agent_max_concurrency: 4\n"
                        "- 列表项不是标量\n"
                        "agent_timeout_seconds: 120\n")
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["profile"], "fast")
            self.assertEqual(cfg["max_concurrency"], 4)
            self.assertEqual(cfg["timeout_seconds"], 120)
            self.assertEqual(cfg["max_retries"], 2)  # 缺失键回落默认

    def test_flat_fallback_parser(self):
        """PyYAML 缺失时的兜底解析器：单行标量语义一致、畸形行跳过。"""
        parsed = sbn._parse_flat_yaml(
            "# head\n"
            "agent_dispatch: budget # 注释\n"
            "agent_max_concurrency: 0\n"
            "agent_batch_window: 3\n"
            "无冒号行\n"
            "main_title: \"带: 冒号的标题\"\n"
            "dialogue_enabled: true\n")
        self.assertEqual(parsed["agent_dispatch"], "budget")
        self.assertEqual(parsed["agent_max_concurrency"], 0)
        self.assertEqual(parsed["agent_batch_window"], 3)
        self.assertEqual(parsed["main_title"], "带: 冒号的标题")
        self.assertIs(parsed["dialogue_enabled"], True)
        self.assertNotIn("无冒号行", parsed)

    def test_invalid_profile_and_bad_scalars(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
                f.write("agent_dispatch: turbo\n"
                        "agent_max_concurrency: not-a-number\n"
                        "agent_batch_window: -3\n"
                        "agent_timeout_seconds: 0\n")
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["profile"], "safe")      # 非法档位回落
            self.assertEqual(cfg["max_concurrency"], 1)   # 非法整数回落档位表
            self.assertEqual(cfg["batch_window"], 1)      # 负窗口夹到 1
            self.assertEqual(cfg["timeout_seconds"], 1)   # 非正超时夹到 1


# ---------------------------------------------------------------------------
# T02 pending 过滤与派发顺序
# ---------------------------------------------------------------------------

class T02_PendingOrder(unittest.TestCase):
    """T02: pending 过滤 / expected 顺序 / 依赖表拓扑 / 垃圾文件回退。"""

    def test_pending_filter_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            # 注意：staged 会命中 plan 节点的 ADVANCE 分支（先于派发），
            # 故这里用 checked/accepted/running 三种非 pending 状态验证过滤。
            state = make_state(
                d,
                sections=("sec0", "sec1", "sec2", "sec3", "sec4", "sec5"),
                statuses={"sec0": "checked", "sec1": "accepted",
                          "sec2": "running"},
            )
            patch = run_plan(state)
            plan = patch["dispatch_plan"]
            # checked/accepted/running 全部排除，只剩 sec3/sec4/sec5
            self.assertEqual(plan["sections"], ["sec3", "sec4", "sec5"])
            self.assertEqual(plan["required_scope_count"], 3)
            # expected 顺序保持
            self.assertEqual([r["scope"] for r in plan["requests_full"]],
                             ["sec3", "sec4", "sec5"])

    def test_expected_order_preserved_without_dependency_map(self):
        with tempfile.TemporaryDirectory() as d:
            pending = ["sec3", "sec1", "sec2"]
            expected = ["sec1", "sec2", "sec3"]
            self.assertEqual(
                sbn._order_pending_sections(d, pending, expected), expected)

    def test_dependency_map_topo_order(self):
        """真实表格格式：`| 节点 | 必须先知道什么 | 首次出现位置 | 是否已闭合 |`。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".pd"), exist_ok=True)
            with open(os.path.join(d, ".pd", "dependency-map.md"), "w",
                      encoding="utf-8") as f:
                f.write("# 前置依赖表\n\n"
                        "| 节点编号 | 必须先知道什么 | 学案中首次出现位置 | 是否已闭合 |\n"
                        "|---|---|---|---|\n"
                        "| sec1 | sec3 | 第 1 节 | 是 |\n"
                        "| sec2 | 无 | 第 2 节 | 是 |\n"
                        "| sec3 | sec2 | 第 3 节 | 是 |\n")
            pending = ["sec1", "sec2", "sec3"]
            expected = ["sec1", "sec2", "sec3"]
            # sec1 依赖 sec3，sec3 依赖 sec2 → 拓扑序 sec2 -> sec3 -> sec1
            self.assertEqual(
                sbn._order_pending_sections(d, pending, expected),
                ["sec2", "sec3", "sec1"])

    def test_dependency_map_garbage_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".pd"), exist_ok=True)
            with open(os.path.join(d, ".pd", "dependency-map.md"), "w",
                      encoding="utf-8") as f:
                f.write("这不是表格\n也没有管道符\n[[[[|\n")
            pending = ["sec2", "sec1"]
            self.assertEqual(
                sbn._order_pending_sections(d, pending, ["sec1", "sec2"]),
                ["sec1", "sec2"])

    def test_dependency_map_cycle_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".pd"), exist_ok=True)
            with open(os.path.join(d, ".pd", "dependency-map.md"), "w",
                      encoding="utf-8") as f:
                f.write("| sec1 | sec2 | x | 是 |\n| sec2 | sec1 | x | 是 |\n")
            self.assertEqual(
                sbn._order_pending_sections(d, ["sec1", "sec2"],
                                            ["sec1", "sec2"]),
                ["sec1", "sec2"])


# ---------------------------------------------------------------------------
# T03 计划构建（含自门禁）
# ---------------------------------------------------------------------------

class T03_PlanBuilding(unittest.TestCase):
    """T03: 批大小 / batch_id 一致 / 请求字段 / 磁盘计划 / 自门禁通过。"""

    def test_plan_building_and_self_gate(self):
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(register_capable_worker(4))
            write_config(d, agent_dispatch="fast")  # fast: 并发4 窗口2 → batch_size = min(8, 3) = 3
            state = make_state(d, sections=("sec0", "sec1", "sec2"))
            patch = run_plan(state)

            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertEqual(patch["active_section"], None)
            self.assertEqual(patch["pending_dispatch"], None)
            self.assertEqual(patch["retry_count"], 0)

            plan = patch["dispatch_plan"]
            batch_id = patch["batch_id"]
            self.assertTrue(batch_id.startswith("bat-t2-"))
            self.assertEqual(plan["batch_id"], batch_id)
            self.assertEqual(plan["stage"], "T2")
            self.assertEqual(plan["sections"], ["sec0", "sec1", "sec2"])
            self.assertEqual(plan["required_scope_count"], 3)
            self.assertEqual(plan["min_parallelism"], 2)
            # R7：磁盘声明的并行上限必须 >= 请求数
            self.assertGreaterEqual(plan["max_concurrency"], 3)
            # 任务8：真实并行度 = min(档位 4, 声明 4) = 4；能力声明落盘留痕
            self.assertEqual(plan["effective_concurrency"], 4)
            self.assertEqual(plan["worker_capabilities"]["max_concurrency"], 4)
            self.assertTrue(plan["worker_capabilities"]["cancellable"])

            requests = plan["requests_full"]
            # 3 个 pending 节 → >= 2 个请求（窗口放大后整批派发）
            self.assertGreaterEqual(len(requests), 2)
            self.assertEqual(len(requests), 3)
            for req in requests:
                self.assertEqual(req["batch_id"], batch_id)
                self.assertRegex(req["input_digest"], HEX64)
                self.assertEqual(req["artifacts"], [f"{req['scope']}.tex"])
                self.assertEqual(req["scope"], req["context"]["section_id"])
                self.assertEqual(req["timeout_seconds"], 900)
                self.assertEqual(req["max_retries"], 2)
                self.assertEqual(req["retry_class"], "transient")
                self.assertTrue(req["dispatch_id"])
            # sec0 用「开篇引入专员」，其余用「切片制作专员」
            roles = {r["scope"]: r["role"] for r in requests}
            self.assertEqual(roles["sec0"], "开篇引入专员")
            self.assertEqual(roles["sec1"], "sec1 切片制作专员")
            self.assertEqual(roles["sec2"], "sec2 切片制作专员")
            # 派发单全部落盘
            self.assertEqual(len(request_files(d)), 3)

            # 状态推进
            self.assertEqual(patch["section_statuses"],
                             {"sec0": "running", "sec1": "running",
                              "sec2": "running"})
            self.assertEqual(patch["dispatch_metrics"],
                             {"submitted": 3, "completed": 0, "ok": 0,
                              "failed": 0, "retried": 0, "max_in_flight": 0,
                              "deferred": 0, "reused": 0})
            self.assertEqual(patch["agent_dispatch_profile"], "fast")
            self.assertEqual(patch["dispatch_history"], [])
            # 全量 JSON 可序列化（LangGraph checkpoint 要求）
            json.dumps(patch, ensure_ascii=False)

            # 磁盘计划：门禁子集 + 原子写结果
            with open(plan_file(d), "r", encoding="utf-8") as f:
                disk = json.load(f)
            for key in ("batch_id", "stage", "sections", "requests",
                        "max_concurrency", "min_parallelism",
                        "required_scope_count", "created_at"):
                self.assertIn(key, disk)
            self.assertEqual(disk["sections"], ["sec0", "sec1", "sec2"])
            self.assertEqual(len(disk["requests"]), 3)
            for entry in disk["requests"]:
                self.assertEqual(
                    sorted(entry.keys()),
                    sorted(["dispatch_id", "scope", "role", "input_digest",
                            "artifacts", "status", "batch_id"]))
                self.assertEqual(entry["batch_id"], batch_id)
                self.assertEqual(entry["status"], "pending")
            # 磁盘计划不含完整派发单（只保留门禁子集）
            self.assertNotIn("requests_full", disk)

            # 自门禁：生成的计划必须通过 check_dispatch
            result = check_dispatch.check_dispatch_project(d)
            self.assertEqual(result["errors"], [], msg=str(result["errors"]))
            self.assertEqual(result["checked"]["requests"], 3)
            self.assertEqual(result["checked"]["sections"], 3)

    def test_batch_size_capped_by_window(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="budget")  # 并发2 窗口2 → 上限4
            state = make_state(d, sections=tuple(f"sec{i}" for i in range(1, 8)))
            patch = run_plan(state)
            self.assertEqual(len(patch["dispatch_plan"]["requests_full"]), 4)

        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="budget", agent_batch_window=1)
            state = make_state(d, sections=tuple(f"sec{i}" for i in range(1, 8)))
            patch = run_plan(state)
            # 并发2 * 窗口1 = 2 < 7 个 pending → 只派 2 个
            self.assertEqual(len(patch["dispatch_plan"]["requests_full"]), 2)
            self.assertEqual(patch["dispatch_plan"]["sections"],
                             ["sec1", "sec2"])

    def test_history_archived_on_second_round(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="budget", agent_batch_window=1)
            sections = tuple(f"sec{i}" for i in range(1, 8))
            state = make_state(d, sections=sections)
            first = run_plan(state)
            state.update(first)
            # 第一批 2 节标为 checked，剩余 5 节中再取 2 节进入下一轮
            for sec in first["dispatch_plan"]["sections"]:
                state["section_statuses"][sec] = "checked"
            second = run_plan(state)
            self.assertEqual(len(second["dispatch_plan"]["sections"]), 2)
            self.assertNotEqual(second["batch_id"], first["batch_id"])
            self.assertEqual(len(second["dispatch_history"]), 1)
            self.assertEqual(second["dispatch_history"][0]["batch_id"],
                             first["batch_id"])
            self.assertEqual(second["dispatch_history"][0]["metrics"]["submitted"], 2)
            # 新一轮度量重置
            self.assertEqual(second["dispatch_metrics"]["submitted"], 2)


# ---------------------------------------------------------------------------
# T04 门禁失败路径
# ---------------------------------------------------------------------------

class T04_GateFailure(unittest.TestCase):
    """T04: 门禁非 PASS → HALTED / HALT / error_message 含错误码。"""

    def test_gate_failure_halts(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            patch = run_plan(state, gate=gate_report(
                "REJECT",
                errors=[{"code": "OVER_CONCURRENCY",
                         "detail": "请求数 4 超过声明的并行上限 2"}],
                exit_code=1))
            self.assertEqual(patch["stage"], "HALTED")
            self.assertEqual(patch["routing_action"], "HALT")
            self.assertIn("派发门禁未通过", patch["error_message"])
            self.assertIn("OVER_CONCURRENCY", patch["error_message"])
            # 硬停止：不返回派发计划、不推进状态
            self.assertNotIn("dispatch_plan", patch)
            self.assertNotIn("batch_id", patch)

    def test_gate_error_without_raw_report(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(d)
            report = gate_report("ERROR")
            report["raw_report"] = {}
            report["issues"] = [{"code": "REPORT_STALE", "detail": "报告不新鲜"}]
            patch = run_plan(state, gate=report)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertIn("REPORT_STALE", patch["error_message"])


# ---------------------------------------------------------------------------
# T05 / T06 ADVANCE 与 DONE
# ---------------------------------------------------------------------------

class T05_AdvancePath(unittest.TestCase):
    """T05: 已有 staged 切片 → ADVANCE，不发起新派发。"""

    def test_staged_section_advances(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d)
            state = make_state(
                d, sections=("sec1", "sec2"),
                statuses={"sec1": "staged", "sec2": "planned"})
            patch = node_t2_plan_dispatch(state)
            self.assertEqual(patch["active_section"], "sec1")
            self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
            self.assertEqual(patch["routing_action"], "PROCEED")
            # 不派发：无派发单、无磁盘计划、无 batch_id
            self.assertNotIn("dispatch_plan", patch)
            self.assertNotIn("batch_id", patch)
            self.assertEqual(request_files(d), [])
            self.assertFalse(os.path.exists(plan_file(d)))

    def test_first_staged_in_expected_order(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(
                d, sections=("sec1", "sec2", "sec3"),
                statuses={"sec2": "staged", "sec3": "staged",
                          "sec1": "planned"})
            patch = node_t2_plan_dispatch(state)
            self.assertEqual(patch["active_section"], "sec2")


class T06_DonePath(unittest.TestCase):
    """T06: 全部 checked → DONE，PROCEED。"""

    def test_all_checked_done(self):
        with tempfile.TemporaryDirectory() as d:
            state = make_state(
                d, sections=("sec1", "sec2"),
                statuses={"sec1": "checked", "sec2": "accepted"})
            patch = node_t2_plan_dispatch(state)
            self.assertIsNone(patch["active_section"])
            self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertEqual(request_files(d), [])


# ---------------------------------------------------------------------------
# T07 派发节点
# ---------------------------------------------------------------------------

class T07_DispatchNode(unittest.TestCase):
    """T07: stale plan → HALT；deferred → manual；否则 parallel。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        write_config(self.d, agent_dispatch="fast")
        self.state = make_state(self.d, sections=("sec1", "sec2", "sec3"))
        self.plan_patch = run_plan(self.state)
        self.state.update(self.plan_patch)
        self.batch_id = self.state["batch_id"]

    def _patch_pool(self, pool):
        original = sbn._get_pool
        sbn._get_pool = lambda *a, **kw: pool
        self.addCleanup(lambda: setattr(sbn, "_get_pool", original))

    def test_stale_plan_halts(self):
        state = dict(self.state)
        state["batch_id"] = "bat-t2-other"
        patch = node_t2_dispatch_sections(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("stale plan", patch["error_message"])

    def test_missing_plan_halts(self):
        state = dict(self.state)
        state["dispatch_plan"] = None
        patch = node_t2_dispatch_sections(state)
        self.assertEqual(patch["stage"], "HALTED")

    def test_parallel_mode(self):
        pool = FakePool(stats={"deferred": 0, "ok": 3, "max_in_flight": 2})
        self._patch_pool(pool)
        patch = node_t2_dispatch_sections(self.state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "parallel")
        self.assertEqual(patch["dispatch_metrics"]["ok"], 3)
        self.assertEqual(patch["dispatch_metrics"]["max_in_flight"], 2)
        # 全部请求已提交，且 batch_id 一致
        self.assertEqual(len(pool.submitted), 3)
        self.assertEqual({r["batch_id"] for r in pool.submitted}, {self.batch_id})
        # 任务4：dispatch 节点绝不调 join（join 是 join 节点专属职责）
        self.assertEqual(pool.join_calls, [])
        # 池对象绝不进入返回 patch
        self.assertNotIn("pool", json.dumps(patch, ensure_ascii=False, default=str))

    def test_manual_mode_when_deferred(self):
        pool = FakePool(stats={"deferred": 3})
        self._patch_pool(pool)
        patch = node_t2_dispatch_sections(self.state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_RUNNING")
        self.assertEqual(patch["dispatch_mode"], "manual")
        self.assertEqual(patch["dispatch_metrics"]["deferred"], 3)
        self.assertEqual(len(pool.submitted), 3)
        # 任务4：manual 模式同样不得在 dispatch 节点 join
        self.assertEqual(pool.join_calls, [])


# ---------------------------------------------------------------------------
# T08 join：并行结果
# ---------------------------------------------------------------------------

class T08_JoinParallel(unittest.TestCase):
    """T08: 活池结果汇总；失败 → REPAIR_SECTION；全 ok → PROCEED。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.addCleanup(register_capable_worker(4))
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        write_config(self.d, agent_dispatch="fast")
        self.state = make_state(self.d, sections=("sec1", "sec2", "sec3"))
        self.plan_patch = run_plan(self.state)
        self.state.update(self.plan_patch)
        self.batch_id = self.state["batch_id"]
        self.requests = self.state["dispatch_plan"]["requests_full"]

    def _install_pool(self, results, stats=None):
        pool = FakePool(results=results, stats=stats)
        sbn._POOL_STATE["pool"] = pool
        sbn._POOL_STATE["batch_id"] = self.batch_id
        return pool

    def test_partial_failure_routes_to_repair(self):
        results = {
            self.requests[0]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[0]["dispatch_id"], scope="sec1",
                status="ok", output={"files": {}}, attempts=1),
            self.requests[1]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[1]["dispatch_id"], scope="sec2",
                status="ok", output={"files": {}}, attempts=2),
            self.requests[2]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[2]["dispatch_id"], scope="sec3",
                status="failed", error="认知 worker 调用失败: boom", attempts=3),
        }
        self._install_pool(results, stats={"deferred": 0, "ok": 2, "failed": 1, "max_in_flight": 2})
        patch = node_t2_join_sections(self.state)

        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec3")
        self.assertEqual(patch["stage"], "REPAIR_LOOP")
        self.assertIn("sec3=failed", patch["repair_reason"])
        # ok 的两个节已暂存
        self.assertEqual(patch["section_statuses"]["sec1"], "staged")
        self.assertEqual(patch["section_statuses"]["sec2"], "staged")
        self.assertEqual(patch["staged_sections"]["sec1"],
                         os.path.join(self.d, "sec1.tex"))
        self.assertEqual(patch["staged_sections"]["sec2"],
                         os.path.join(self.d, "sec2.tex"))
        # dispatch_results 以 dispatch_id 为键
        self.assertEqual(len(patch["dispatch_results"]), 3)
        entry = patch["dispatch_results"][self.requests[2]["dispatch_id"]]
        self.assertEqual(entry["scope"], "sec3")
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 3)
        self.assertEqual(entry["batch_id"], self.batch_id)
        self.assertEqual(patch["dispatch_metrics"]["ok"], 2)

    def test_all_ok_proceeds(self):
        results = {
            req["dispatch_id"]: DispatchResult(
                dispatch_id=req["dispatch_id"], scope=req["scope"],
                status="ok", output={"files": {}}, attempts=1)
            for req in self.requests
        }
        pool = self._install_pool(results, stats={"deferred": 0, "ok": 3, "max_in_flight": 3})
        patch = node_t2_join_sections(self.state)
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch["active_section"], "sec1")
        self.assertEqual(patch["section_statuses"],
                         {"sec1": "staged", "sec2": "staged", "sec3": "staged"})
        self.assertEqual(len(patch["dispatch_results"]), 3)
        # 任务4：join 节点是本 batch 唯一 join 者，且只 join 一次
        self.assertEqual(len(pool.join_calls), 1)

    def test_join_node_idempotent_second_entry(self):
        """图级回边再次进入 join 节点不得重复 join（每 batch 一次）。"""
        results = {
            req["dispatch_id"]: DispatchResult(
                dispatch_id=req["dispatch_id"], scope=req["scope"],
                status="ok", output={"files": {}}, attempts=1)
            for req in self.requests
        }
        pool = self._install_pool(results, stats={"deferred": 0, "ok": 3, "max_in_flight": 3})
        first = node_t2_join_sections(self.state)
        second = node_t2_join_sections(self.state)
        self.assertEqual(first["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(second["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(len(pool.join_calls), 1)

    def test_under_parallelism_halts(self):
        """audit §6 硬门禁：池全 ok 但 max_in_flight < min_parallelism → 禁止伪装完成。"""
        results = {
            req["dispatch_id"]: DispatchResult(
                dispatch_id=req["dispatch_id"], scope=req["scope"],
                status="ok", output={"files": {}}, attempts=1)
            for req in self.requests
        }
        # fast 档 + 3 节 → min_parallelism=2；伪造实际并发 1
        self._install_pool(results, stats={"deferred": 0, "ok": 3, "max_in_flight": 1})
        patch = node_t2_join_sections(self.state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("禁止伪装完成", patch["error_message"])
        # 主控仍须看到完整结果以便排查
        self.assertEqual(len(patch["dispatch_results"]), 3)

    def test_no_change_and_timeout_are_failures(self):
        results = {
            self.requests[0]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[0]["dispatch_id"], scope="sec1",
                status="no_change", attempts=1),
            self.requests[1]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[1]["dispatch_id"], scope="sec2",
                status="timeout", error="join 截止", attempts=1),
            self.requests[2]["dispatch_id"]: DispatchResult(
                dispatch_id=self.requests[2]["dispatch_id"], scope="sec3",
                status="ok", attempts=1),
        }
        self._install_pool(results, stats={"deferred": 0, "ok": 1,
                                           "no_change": 1, "timeout": 1,
                                           "max_in_flight": 2})
        patch = node_t2_join_sections(self.state)
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec1")  # expected 顺序首个失败
        self.assertEqual(patch["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["status"], "no_change")

    def test_huge_output_is_omitted(self):
        big = {"blob": "x" * (sbn.MAX_OUTPUT_CHARS + 100)}
        results = {
            req["dispatch_id"]: DispatchResult(
                dispatch_id=req["dispatch_id"], scope=req["scope"],
                status="ok", output=big, attempts=1)
            for req in self.requests
        }
        self._install_pool(results, stats={"deferred": 0, "ok": 3, "max_in_flight": 3})
        patch = node_t2_join_sections(self.state)
        for req in self.requests:
            self.assertNotIn("output", patch["dispatch_results"][req["dispatch_id"]])
        # 状态仍然可序列化
        json.dumps(patch, ensure_ascii=False)

    def test_stale_plan_halts(self):
        state = dict(self.state)
        state["batch_id"] = "bat-t2-other"
        patch = node_t2_join_sections(state)
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("stale plan", patch["error_message"])


# ---------------------------------------------------------------------------
# T09 / T10 manual 模式与断点安全
# ---------------------------------------------------------------------------

class T09_JoinManual(unittest.TestCase):
    """T09: interrupt 只发一次；恢复后从磁盘验证（ok / no_change）。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        write_config(self.d, agent_dispatch="fast")
        self.state = make_state(self.d, sections=("sec1", "sec2", "sec3"))
        self.plan_patch = run_plan(self.state)
        self.state.update(self.plan_patch)
        self.batch_id = self.state["batch_id"]
        self.requests = self.state["dispatch_plan"]["requests_full"]
        # manual 模式：无活池
        sbn._reset_pool()
        self.captured = []
        original = sbn.interrupt
        sbn.interrupt = lambda payload: self.captured.append(payload) or {}
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))

    def test_interrupt_payload_and_resume(self):
        # --- 第一次进入：中断一次，携带整批 ---
        patch = node_t2_join_sections(self.state)
        self.assertEqual(len(self.captured), 1)
        payload = self.captured[0]
        self.assertEqual(payload["batch_id"], self.batch_id)
        self.assertEqual(payload["type"], "AGENT_DISPATCH")
        self.assertEqual(payload["stage"], "T2")
        self.assertEqual(payload["prompt_file"], "prompts/subagent_prompt.md")
        self.assertEqual(payload["dispatch_protocol"], "host-subagent-v1")
        self.assertEqual(payload["required_tool"], "qchain_dispatch")
        self.assertEqual(payload["forbidden"],
                         ["主 Agent 直写 artifacts", "同会话自行撰写产物内容"])
        self.assertEqual(len(payload["requests"]), 3)
        self.assertEqual(payload["artifacts"],
                         ["sec1.tex", "sec2.tex", "sec3.tex"])
        self.assertEqual(sorted(payload["before_digests"].keys()),
                         ["sec1.tex", "sec2.tex", "sec3.tex"])
        for req, item in zip(self.requests, payload["requests"]):
            self.assertEqual(item["dispatch_id"], req["dispatch_id"])
            self.assertEqual(item["scope"], req["scope"])
            self.assertEqual(item["artifacts"], req["artifacts"])
            self.assertEqual(item["instruction"], req["instruction"])
        self.assertEqual(patch["manual_interrupted_batch"], self.batch_id)
        self.assertEqual(patch["stage"], "T2_SECTIONS_RUNNING")

        # --- 第二次进入（人工已写完 sec1/sec2，sec3 未写）：不重复中断 ---
        state = dict(self.state)
        state.update(patch)
        for sec in ("sec1", "sec2"):
            with open(os.path.join(self.d, f"{sec}.tex"), "w",
                      encoding="utf-8") as f:
                f.write(f"% {sec} 内容\n")
        # 证据链：qchain_dispatch 派发子 Agent 写完即落证据（唯一放行依据）
        seed_agent_calls(self.d, self.requests[:2])
        patch2 = node_t2_join_sections(state)
        self.assertEqual(len(self.captured), 1)  # 没有第二次 interrupt
        self.assertEqual(patch2["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch2["repair_target"], "sec3")
        self.assertEqual(patch2["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["status"], "ok")
        self.assertEqual(patch2["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["verified_via"], "agent")
        self.assertEqual(patch2["section_statuses"]["sec1"], "staged")
        self.assertEqual(patch2["staged_sections"]["sec1"],
                         os.path.join(self.d, "sec1.tex"))
        self.assertEqual(patch2["dispatch_results"]
                         [self.requests[2]["dispatch_id"]]["status"],
                         "missing_artifacts")

        # --- 第三次进入（产物全部缺失）：missing_artifacts → 返修，仍不重复中断 ---
        state3 = dict(self.state)
        state3.update(patch)
        for sec in ("sec1", "sec2"):   # 清掉上一步写下的产物，回到"未写"状态
            path = os.path.join(self.d, f"{sec}.tex")
            if os.path.exists(path):
                os.remove(path)
        patch3 = node_t2_join_sections(state3)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(patch3["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch3["repair_target"], "sec1")  # expected 顺序首个失败
        for req in self.requests:
            self.assertEqual(patch3["dispatch_results"][req["dispatch_id"]]["status"],
                             "missing_artifacts")

    def test_resume_all_unchanged_is_no_change(self):
        """产物已存在但恢复后毫无变更 → no_change（run_dispatch 同口径）。"""
        for sec in ("sec1", "sec2", "sec3"):
            with open(os.path.join(self.d, f"{sec}.tex"), "w",
                      encoding="utf-8") as f:
                f.write(f"% {sec} 旧内容\n")
        state = make_state(self.d, sections=("sec1", "sec2", "sec3"))
        plan_patch = run_plan(state)   # snapshot 记录的是"旧内容"摘要
        state.update(plan_patch)
        requests = state["dispatch_plan"]["requests_full"]
        self.assertIsNotNone(requests[0]["before"]["sec1.tex"])

        patch = node_t2_join_sections(state)          # 第一次：中断一次
        self.assertEqual(len(self.captured), 1)
        state.update(patch)
        patch2 = node_t2_join_sections(state)         # 恢复后什么都没改
        self.assertEqual(len(self.captured), 1)       # 不重复中断
        self.assertEqual(patch2["routing_action"], "REPAIR_SECTION")
        for req in requests:
            self.assertEqual(patch2["dispatch_results"][req["dispatch_id"]]["status"],
                             "no_change")
        self.assertEqual(patch2["dispatch_metrics"]["no_change"], 3)

    def test_parallel_crash_resume_rederives_without_interrupt(self):
        """并行批次执行中进程崩溃→重启→resume：活池已失，但不得退化为人工 interrupt。

        checkpoint 里 dispatch_mode=="parallel"（派发节点已完成并固化），
        manual_interrupted_batch 从未写过；已写完的节按磁盘 sha256 判 ok，
        未写完的判 missing → 返修环重派（audit_agent.md §5 恢复语义）。
        """
        state = dict(self.state)
        state["dispatch_mode"] = "parallel"
        state.pop("manual_interrupted_batch", None)
        # 模拟 worker 已写完 sec1/sec2，sec3 尚未写（崩溃时点）
        for sec in ("sec1", "sec2"):
            with open(os.path.join(self.d, f"{sec}.tex"), "w",
                      encoding="utf-8") as f:
                f.write(f"% {sec} 内容\n")
        seed_agent_calls(self.d, self.requests[:2])
        patch = node_t2_join_sections(state)

        self.assertEqual(len(self.captured), 0)  # 绝不 interrupt
        self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
        self.assertEqual(patch["repair_target"], "sec3")
        self.assertEqual(patch["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["status"], "ok")
        self.assertEqual(patch["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["verified_via"], "agent")
        self.assertEqual(patch["dispatch_results"]
                         [self.requests[1]["dispatch_id"]]["status"], "ok")
        self.assertEqual(patch["dispatch_results"]
                         [self.requests[2]["dispatch_id"]]["status"],
                         "missing_artifacts")
        self.assertEqual(patch["section_statuses"]["sec1"], "staged")
        self.assertEqual(patch["section_statuses"]["sec2"], "staged")

    def test_parallel_crash_resume_all_done_proceeds(self):
        """崩溃时全部节已写完 → 磁盘重推导全 ok → PROCEED，仍不 interrupt。"""
        state = dict(self.state)
        state["dispatch_mode"] = "parallel"
        for sec in ("sec1", "sec2", "sec3"):
            with open(os.path.join(self.d, f"{sec}.tex"), "w",
                      encoding="utf-8") as f:
                f.write(f"% {sec} 内容\n")
        seed_agent_calls(self.d, self.requests)
        patch = node_t2_join_sections(state)
        self.assertEqual(len(self.captured), 0)
        self.assertEqual(patch["routing_action"], "PROCEED")
        self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
        self.assertEqual(patch["active_section"], "sec1")

    def test_resume_response_files_are_written(self):
        """中断响应带回 files 时按 artifacts 白名单落盘。"""
        payload_seen = []
        original = sbn.interrupt

        def fake_interrupt(payload):
            payload_seen.append(payload)
            return {
                "dispatch_protocol": "host-subagent-v1",
                "files": {"sec1.tex": "% sec1 from response\n",
                          "not-allowed.tex": "应当被忽略\n"},
            }

        sbn.interrupt = fake_interrupt
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))
        patch = node_t2_join_sections(self.state)
        self.assertEqual(len(payload_seen), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "sec1.tex")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "not-allowed.tex")))
        self.assertEqual(patch["manual_interrupted_batch"], self.batch_id)

        # qchain_dispatch 落盘产物后同批落证据，随后 checkpoint 重推导放行
        seed_agent_calls(self.d, self.requests[:1])
        state = dict(self.state)
        state.update(patch)
        patch2 = node_t2_join_sections(state)
        self.assertEqual(patch2["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["status"], "ok")
        self.assertEqual(patch2["dispatch_results"]
                         [self.requests[0]["dispatch_id"]]["verified_via"], "agent")
        self.assertEqual(patch2["section_statuses"]["sec1"], "staged")

    def test_resume_response_legacy_no_protocol_not_blocked(self):
        """旧 checkpoint 中断响应缺 dispatch_protocol 字段 → 不拦截（向后兼容）。"""
        original = sbn.interrupt

        def fake_interrupt(payload):
            return {"files": {"sec1.tex": "% sec1 legacy\n"}}

        sbn.interrupt = fake_interrupt
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))
        patch = node_t2_join_sections(self.state)
        self.assertEqual(patch["stage"], "T2_SECTIONS_RUNNING")
        self.assertTrue(os.path.isfile(os.path.join(self.d, "sec1.tex")))

    def test_resume_response_bad_protocol_halts(self):
        """伪造/非 host-subagent-v1 协议 → HALT（BAD_DISPATCH_PROTOCOL）。"""
        original = sbn.interrupt

        def fake_interrupt(payload):
            return {"dispatch_protocol": "forged-protocol-v9", "files": {}}

        sbn.interrupt = fake_interrupt
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))
        patch = node_t2_join_sections(self.state)
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertEqual(patch["stage"], "HALTED")
        self.assertIn("BAD_DISPATCH_PROTOCOL", patch["error_message"])
        self.assertTrue(any("BAD_DISPATCH_PROTOCOL" in h
                            for h in patch["history"]))

    def test_idempotent_no_duplicate_results(self):
        state = dict(self.state)
        state.update(node_t2_join_sections(self.state))
        with open(os.path.join(self.d, "sec1.tex"), "w", encoding="utf-8") as f:
            f.write("% sec1\n")
        with open(os.path.join(self.d, "sec2.tex"), "w", encoding="utf-8") as f:
            f.write("% sec2\n")
        with open(os.path.join(self.d, "sec3.tex"), "w", encoding="utf-8") as f:
            f.write("% sec3\n")
        seed_agent_calls(self.d, self.requests)
        first = node_t2_join_sections(state)
        second = node_t2_join_sections(state)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(first["dispatch_results"], second["dispatch_results"])
        self.assertEqual(first["section_statuses"], second["section_statuses"])
        self.assertEqual(second["active_section"], "sec1")


class T10_ResumeSafety(unittest.TestCase):
    """T10: 无活池 + manual_interrupted_batch 已等于 batch_id → 磁盘验证，不中断。"""

    def test_verify_from_disk_without_pool(self):
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(sbn._reset_pool)
            write_config(d)
            state = make_state(d, sections=("sec1", "sec2"))
            plan_patch = run_plan(state)
            state.update(plan_patch)
            # plan 节点会建池（任务8：计划须按真实并行度承诺 min_parallelism）；
            # 进程重启后内存池为空——模拟 plan 与 join 之间崩溃重启
            sbn._reset_pool()
            requests = state["dispatch_plan"]["requests_full"]
            state["manual_interrupted_batch"] = state["batch_id"]
            state["dispatch_mode"] = "manual"

            captured = []
            original = sbn.interrupt
            sbn.interrupt = lambda payload: captured.append(payload) or {}
            self.addCleanup(lambda: setattr(sbn, "interrupt", original))

            # 进程重启后内存池为空：_live_pool 返回 None
            self.assertIsNone(sbn._live_pool(state["batch_id"]))

            for req in requests:
                with open(os.path.join(d, f"{req['scope']}.tex"), "w",
                          encoding="utf-8") as f:
                    f.write(f"% {req['scope']}\n")
            seed_agent_calls(d, requests)
            patch = node_t2_join_sections(state)
            self.assertEqual(captured, [])          # 绝不重复 interrupt
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
            self.assertEqual(patch["active_section"], "sec1")
            self.assertEqual(patch["dispatch_metrics"]["ok"], 2)
            for req in requests:
                self.assertEqual(
                    patch["dispatch_results"][req["dispatch_id"]]["verified_via"],
                    "agent")

    def test_verify_from_disk_unattested_fails_by_default(self):
        """主 Agent 单会话直写产物（无证据）→ ok_unattested 失败，进返修环。"""
        with tempfile.TemporaryDirectory() as d:
            sbn._reset_pool()
            self.addCleanup(sbn._reset_pool)
            write_config(d)
            state = make_state(d, sections=("sec1", "sec2"))
            plan_patch = run_plan(state)
            state.update(plan_patch)
            requests = state["dispatch_plan"]["requests_full"]
            state["manual_interrupted_batch"] = state["batch_id"]
            state["dispatch_mode"] = "manual"

            captured = []
            original = sbn.interrupt
            sbn.interrupt = lambda payload: captured.append(payload) or {}
            self.addCleanup(lambda: setattr(sbn, "interrupt", original))

            for req in requests:   # 直写但不落证据（q_audit.md 审计发现的违规形态）
                with open(os.path.join(d, f"{req['scope']}.tex"), "w",
                          encoding="utf-8") as f:
                    f.write(f"% {req['scope']} 主线程直写\n")
            patch = node_t2_join_sections(state)
            self.assertEqual(captured, [])
            self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
            self.assertEqual(patch["repair_target"], "sec1")
            for req in requests:
                entry = patch["dispatch_results"][req["dispatch_id"]]
                self.assertEqual(entry["status"], "ok_unattested")
                self.assertEqual(entry["verified_via"], "unattested")
            self.assertTrue(any("产物无代理证据" in h for h in patch["history"]))

    def test_verify_from_disk_unattested_escape_valve_allows(self):
        """allow_unattested=true 逃生阀：放行但打 unattested 标记。"""
        with tempfile.TemporaryDirectory() as d:
            sbn._reset_pool()
            self.addCleanup(sbn._reset_pool)
            write_config(d, allow_unattested=True)
            state = make_state(d, sections=("sec1", "sec2"))
            plan_patch = run_plan(state)
            state.update(plan_patch)
            requests = state["dispatch_plan"]["requests_full"]
            state["manual_interrupted_batch"] = state["batch_id"]
            state["dispatch_mode"] = "manual"

            original = sbn.interrupt
            sbn.interrupt = lambda payload: {}
            self.addCleanup(lambda: setattr(sbn, "interrupt", original))

            for req in requests:
                with open(os.path.join(d, f"{req['scope']}.tex"), "w",
                          encoding="utf-8") as f:
                    f.write(f"% {req['scope']} 直写放行\n")
            patch = node_t2_join_sections(state)
            self.assertEqual(patch["routing_action"], "PROCEED")
            for req in requests:
                entry = patch["dispatch_results"][req["dispatch_id"]]
                self.assertEqual(entry["status"], "ok_unattested")
                self.assertTrue(entry["unattested"])
                self.assertEqual(entry["verified_via"], "unattested")

    def test_verify_from_disk_evidence_mismatch_fails(self):
        """证据在案但摘要对不上（产物被证据落盘后改写）→ attested_mismatch 失败。"""
        with tempfile.TemporaryDirectory() as d:
            sbn._reset_pool()
            self.addCleanup(sbn._reset_pool)
            write_config(d)
            state = make_state(d, sections=("sec1", "sec2"))
            plan_patch = run_plan(state)
            state.update(plan_patch)
            requests = state["dispatch_plan"]["requests_full"]
            state["manual_interrupted_batch"] = state["batch_id"]
            state["dispatch_mode"] = "manual"

            original = sbn.interrupt
            sbn.interrupt = lambda payload: {}
            self.addCleanup(lambda: setattr(sbn, "interrupt", original))

            for req in requests:
                with open(os.path.join(d, f"{req['scope']}.tex"), "w",
                          encoding="utf-8") as f:
                    f.write(f"% {req['scope']} 初版\n")
            seed_agent_calls(d, requests)
            # 证据落盘后主 Agent 又改写了产物 → 摘要漂移
            with open(os.path.join(d, "sec1.tex"), "w", encoding="utf-8") as f:
                f.write("% sec1 被事后改写\n")
            patch = node_t2_join_sections(state)
            self.assertEqual(patch["routing_action"], "REPAIR_SECTION")
            self.assertEqual(
                patch["dispatch_results"][requests[0]["dispatch_id"]]["status"],
                "attested_mismatch")
            # attested_mismatch 无逃生阀：即使开 allow_unattested 也失败
            write_config(d, allow_unattested=True)
            state2 = dict(state)
            plan_patch2 = run_plan(state2)
            state2.update(plan_patch2)
            state2["manual_interrupted_batch"] = state2["batch_id"]
            state2["dispatch_mode"] = "manual"
            patch2 = node_t2_join_sections(state2)
            self.assertEqual(patch2["routing_action"], "REPAIR_SECTION")

    def test_load_dispatch_config_nested_block(self):
        """agent_dispatch 映射块多态解析：profile/allow_unattested/model_map。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(
                d,
                agent_dispatch="fast",
                allow_unattested=True,
                model_map={"T2": {"provider": "spawn", "model": "cheap-fast"}},
            )
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["profile"], "fast")
            self.assertTrue(cfg["allow_unattested"])
            self.assertEqual(cfg["max_concurrency"],
                             sbn.PROFILE_CONCURRENCY["fast"])
            self.assertEqual(cfg["model_map"],
                             {"T2": {"provider": "spawn", "model": "cheap-fast"}})

    def test_load_dispatch_config_flat_defaults(self):
        """旧扁平配置缺省形态：allow_unattested=False、model_map={}。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="budget")
            cfg = sbn._load_dispatch_config(d)
            self.assertEqual(cfg["profile"], "budget")
            self.assertFalse(cfg["allow_unattested"])
            self.assertEqual(cfg["model_map"], {})


# ---------------------------------------------------------------------------
# T11 真实 pdstate + 真实 check_dispatch 门禁集成
# ---------------------------------------------------------------------------

MAIN_TEX = (r"\input{header.tex}" + "\n\n\\begin{document}\n"
            r"\pdBeginColumns" + "\n\n"
            + "\n".join(r"\input{sec%d.tex}" % i for i in (1, 2, 3))
            + "\n\n\\input{footer.tex}\n")


class T11_PdstateIntegration(unittest.TestCase):
    """T11: 临时工程 + 真实 pdstate init/start + 真实 check_dispatch 门禁。

    pdstate init 只需要 main.tex（切片从 \\input{secN.tex} 提取；其余模板文件
    缺失时只记摘要为 None，不报错），因此这里无需复制整套 templates。
    """

    def test_real_pdstate_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "self")
            os.makedirs(d)
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
                f.write(MAIN_TEX)
            write_config(d, agent_dispatch="fast", agent_timeout_seconds=120)

            init = subprocess.run(
                [sys.executable, os.path.join(HERE, "pdstate.py"), d, "init"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace")
            self.assertEqual(init.returncode, 0, msg=init.stdout + init.stderr)
            with open(os.path.join(d, ".pd", "run.json"), "r",
                      encoding="utf-8") as f:
                run_data = json.load(f)
            self.assertEqual(run_data["slices"], ["sec1", "sec2", "sec3"])

            state = make_state(d, sections=("sec1", "sec2", "sec3"))
            patch = node_t2_plan_dispatch(state)   # 真实 ScriptRunner + 真实门禁

            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertEqual(patch["agent_dispatch_profile"], "fast")
            # fast 档位：并发4 窗口2 → 3 个 pending 整批派发
            self.assertEqual(len(patch["dispatch_plan"]["sections"]), 3)

            # 磁盘派发计划存在且被真实门禁判为通过
            self.assertTrue(os.path.isfile(plan_file(d)))
            result = check_dispatch.check_dispatch_project(d)
            self.assertEqual(result["errors"], [], msg=str(result["errors"]))
            report_path = os.path.join(d, ".pd", "reports",
                                       "check_dispatch.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            self.assertEqual(report["status"], "pass")

            # pdstate 已为每一节创建独占 attempt（ready + start 真实生效）
            with open(os.path.join(d, ".pd", "run.json"), "r",
                      encoding="utf-8") as f:
                run_after = json.load(f)
            for sec in ("sec1", "sec2", "sec3"):
                task = run_after["tasks"][sec]
                self.assertTrue(task["attempts"], msg=f"{sec} 未创建 attempt")
            # 派发单落盘
            self.assertEqual(len(request_files(d)), 3)

    def test_gate_rejects_hand_broken_plan(self):
        """反向证据：手工破坏磁盘计划 → 真实门禁必须报错（证明门禁真的在跑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "self")
            os.makedirs(d)
            with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
                f.write(MAIN_TEX)
            write_config(d)
            init = subprocess.run(
                [sys.executable, os.path.join(HERE, "pdstate.py"), d, "init"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace")
            self.assertEqual(init.returncode, 0, msg=init.stdout + init.stderr)

            state = make_state(d, sections=("sec1", "sec2", "sec3"))
            patch = node_t2_plan_dispatch(state)
            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")

            # 手工把并发上限改成 1（< 请求数 2）→ R7 OVER_CONCURRENCY
            with open(plan_file(d), "r", encoding="utf-8") as f:
                plan = json.load(f)
            plan["max_concurrency"] = 1
            with open(plan_file(d), "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            result = check_dispatch.check_dispatch_project(d)
            codes = [e["code"] for e in result["errors"]]
            self.assertIn("OVER_CONCURRENCY", codes)


# ---------------------------------------------------------------------------
# T12 任务3：工作集选择（run_scope full/focused/edit）
# ---------------------------------------------------------------------------

class T12_WorkSetSelection(unittest.TestCase):
    """T12: 只派发选定节；未选中标记 reused/skipped；非法选择 HALTED。"""

    def _make_file(self, d, sec):
        with open(os.path.join(d, f"{sec}.tex"), "w", encoding="utf-8") as f:
            f.write(f"% qid: {sec}-q1\n\\begin{{probchain}}\n\\end{{probchain}}\n")

    def test_focused_only_dispatches_selected(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="focused", target_sections=["sec2"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
            # 只派发选定的 sec2
            self.assertEqual(patch["dispatch_plan"]["sections"], ["sec2"])
            self.assertEqual(len(patch["dispatch_plan"]["requests_full"]), 1)
            self.assertEqual(len(request_files(d)), 1)
            # 未选中：sec1 有文件→reused；sec2 选中→running；sec3 无文件→skipped
            self.assertEqual(patch["section_statuses"],
                             {"sec1": "skipped", "sec2": "running",
                              "sec3": "skipped"})
            # 磁盘计划记录工作集留痕
            with open(plan_file(d), "r", encoding="utf-8") as f:
                disk = json.load(f)
            self.assertEqual(disk["work_set"]["run_scope"], "focused")
            self.assertEqual(disk["work_set"]["selected"], ["sec2"])

    def test_focused_marks_reused_when_file_exists(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            self._make_file(d, "sec1")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="focused", target_sections=["sec2"])
            patch = run_plan(state)
            self.assertEqual(patch["dispatch_plan"]["sections"], ["sec2"])
            self.assertEqual(patch["section_statuses"]["sec1"], "reused")
            self.assertEqual(patch["section_statuses"]["sec3"], "skipped")

    def test_focused_qid_targets_derive_section(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="focused", target_qids=["sec2-q3"])
            patch = run_plan(state)
            self.assertEqual(patch["dispatch_plan"]["sections"], ["sec2"])

    def test_edit_requires_existing_section(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            self._make_file(d, "sec1")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="edit", target_sections=["sec1"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING")
            self.assertEqual(patch["dispatch_plan"]["sections"], ["sec1"])
            self.assertEqual(patch["section_statuses"]["sec1"], "running")
            self.assertEqual(patch["section_statuses"]["sec2"], "skipped")

    def test_edit_target_not_existing_halts(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="edit", target_sections=["sec2"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertEqual(patch["routing_action"], "HALT")
            self.assertIn("TARGET_NOT_EXISTING", patch["error_message"])
            # 拒绝时不得留下派发计划
            self.assertFalse(os.path.isfile(plan_file(d)))

    def test_invalid_run_scope_halts(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2"),
                               run_scope="bogus", target_sections=["sec1"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertIn("BAD_RUN_SCOPE", patch["error_message"])

    def test_focused_without_targets_halts(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2"),
                               run_scope="focused", target_sections=[])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertIn("EMPTY_TARGETS", patch["error_message"])

    def test_focused_unknown_target_halts(self):
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2"),
                               run_scope="focused", target_sections=["sec99"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "HALTED")
            self.assertIn("UNKNOWN_TARGET", patch["error_message"])

    def test_config_yaml_fallback(self):
        """state 无 run_scope 时回读 config.yaml（resume/重启路径）。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast",
                         run_scope="focused", target_sections="sec1")
            state = make_state(d, sections=("sec1", "sec2", "sec3"))
            patch = run_plan(state)
            self.assertEqual(patch["dispatch_plan"]["sections"], ["sec1"])
            self.assertEqual(patch["section_statuses"]["sec2"], "skipped")

    def test_full_scope_ignores_targets(self):
        """full 档忽略目标列表，行为与原全量派发完全一致。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="full", target_sections=["sec1"])
            patch = run_plan(state)
            self.assertEqual(patch["dispatch_plan"]["sections"],
                             ["sec1", "sec2", "sec3"])
            self.assertEqual(patch["section_statuses"],
                             {"sec1": "running", "sec2": "running",
                              "sec3": "running"})

    def test_work_set_empty_after_filter_short_circuits(self):
        """工作集内切片已完成 → 直接 STAGED/PROCEED，不空转派发。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast")
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               statuses={"sec1": "checked"},
                               run_scope="focused", target_sections=["sec1"])
            patch = run_plan(state)
            self.assertEqual(patch["stage"], "T2_SECTIONS_STAGED")
            self.assertEqual(patch["routing_action"], "PROCEED")
            self.assertNotIn("dispatch_plan", patch)
            self.assertEqual(patch["section_statuses"]["sec1"], "checked")
            self.assertEqual(patch["section_statuses"]["sec2"], "skipped")

    def test_skipped_section_not_redispatched_next_round(self):
        """reused/skipped 是终态：下一轮 plan 不得再派发（DONE 分支收敛）。"""
        with tempfile.TemporaryDirectory() as d:
            write_config(d, agent_dispatch="fast", agent_batch_window=1)
            state = make_state(d, sections=("sec1", "sec2", "sec3"),
                               run_scope="focused", target_sections=["sec2"])
            first = run_plan(state)
            self.assertEqual(first["dispatch_plan"]["sections"], ["sec2"])
            state.update(first)
            state["section_statuses"]["sec2"] = "checked"
            second = run_plan(state)
            # 全部终态 → (b) DONE 分支：无新批次
            self.assertEqual(second["stage"], "T2_SECTIONS_STAGED")
            self.assertNotIn("dispatch_plan", second)
            self.assertEqual(len(request_files(d)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
