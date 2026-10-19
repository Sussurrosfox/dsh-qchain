#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_worker_pool.py — WorkerPool 并发派发执行器回归测试（unittest + 内存替身）

覆盖 T1–T13；全部使用内存 executor 替身与合成派发请求，不触碰真实 self/、
不调用 interrupt()、不起真实 LLM。T12–T13 覆盖 audit §17 任务8 的 worker
能力声明（真实并行度压薄 / cancelled 与 timeout_deferred 分流）。

T6 的 deferred 分支通过 patch 模块级 ``_worker_registry_available`` 强制
「无可用执行器」，与 worker_registry 默认未注册的真实状态一致。

运行: python -m unittest discover scripts -p "test_worker_pool.py" -v
"""
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

try:  # 常规路径：随 workflow 包导入
    from workflow.adapters import worker_pool as wp
except Exception:  # pragma: no cover - langgraph 缺失时按文件路径直载
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "qchain_worker_pool",
        os.path.join(PLUGIN_ROOT, "workflow", "adapters", "worker_pool.py"))
    wp = importlib.util.module_from_spec(_spec)
    sys.modules["qchain_worker_pool"] = wp
    _spec.loader.exec_module(wp)

DispatchResult = wp.DispatchResult
WorkerPool = wp.WorkerPool


def make_request(dispatch_id, **overrides):
    """最小派发请求（沿用 build_request 的 FIXED-P0 键集合）。"""
    request = {
        "dispatch_id": dispatch_id,
        "batch_id": "bat-test",
        "stage": "TEST_STAGE",
        "scope": "sec1",
        "role": "切片撰写专员",
        "prompt_file": "subagent_prompt.md",
        "artifacts": ["sec1.tex"],
        "instruction": "请撰写 sec1.tex。",
        "command": "python scripts/pdstate.py self/ submit sec1",
        "context": {"section_id": "sec1"},
        "run_id": "test-run",
        "input_digest": "0" * 64,
        "timeout_seconds": 900,
        "max_retries": 2,
        "retry_class": "transient",
        "attempt": 0,
        "status": "pending",
    }
    request.update(overrides)
    return request


class PoolTestCase(unittest.TestCase):
    """共用：登记池、tearDown 里 reset 并排空 qchain-worker 线程。"""

    def setUp(self):
        self._pools = []

    def tearDown(self):
        for pool in self._pools:
            self._shutdown(pool)

    def _pool(self, executor=None, **kwargs):
        pool = WorkerPool(executor=executor, **kwargs)
        self._pools.append(pool)
        return pool

    def _shutdown(self, pool):
        """reset（wait=False）并等待 qchain-worker 线程真正退出。"""
        pool.reset()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            workers = [t for t in threading.enumerate()
                       if t.name.startswith(wp.THREAD_NAME_PREFIX)]
            if not workers:
                return
            for thread in workers:
                thread.join(0.05)
        self.fail("qchain-worker 线程未能在 5s 内退出")

    @staticmethod
    def _worker_threads():
        return [t for t in threading.enumerate() if t.name.startswith(wp.THREAD_NAME_PREFIX)]


class T01_T03_ConcurrencyAndRetry(PoolTestCase):
    """T1 并发上限 / T2 瞬时重试 / T3 无变更不重试。"""

    def test_t01_peak_concurrency_bounded(self):
        lock = threading.Lock()
        state = {"cur": 0, "peak": 0}

        def executor(request):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.15)
            with lock:
                state["cur"] -= 1
            return {"status": "success", "files": {"sec1.tex": "% v2\n"}}

        pool = self._pool(executor, max_concurrency=2)
        ids = [pool.submit(make_request("dsp-t1-%d" % i)) for i in range(3)]
        results = pool.join()

        self.assertEqual(len(results), 3)
        for dispatch_id in ids:
            result = results[dispatch_id]
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.attempts, 1)
            self.assertIsNone(result.error)
            self.assertEqual(result.scope, "sec1")
        stats = pool.stats()
        self.assertEqual(stats["ok"], 3)
        self.assertEqual(stats["submitted"], 3)
        self.assertLessEqual(stats["max_in_flight"], 2)
        self.assertGreaterEqual(stats["max_in_flight"], 1)
        # 替身自记的峰值并发同样被 max_concurrency 压制
        self.assertLessEqual(state["peak"], 2)
        self.assertGreaterEqual(state["peak"], 2)
        self.assertEqual(pool.pending(), 0)

    def test_t02_transient_retry_then_ok(self):
        calls = {"n": 0}

        def executor(request):
            calls["n"] += 1
            if calls["n"] <= 2:
                return {"_error": "Failed to parse LLM JSON output: Expecting value",
                        "raw": "<not json>"}
            return {"status": "success", "files": {"sec1.tex": "% v3\n"}}

        pool = self._pool(executor, max_concurrency=1)
        dispatch_id = pool.submit(make_request("dsp-t2"))
        results = pool.join()

        result = results[dispatch_id]
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts, 3)
        self.assertIsNone(result.error)
        self.assertEqual(result.output, {"status": "success",
                                         "files": {"sec1.tex": "% v3\n"}})
        self.assertEqual(pool.stats()["retried"], 2)
        self.assertEqual(calls["n"], 3)

    def test_t03_no_change_not_retried(self):
        calls = {"n": 0}

        def executor(request):
            calls["n"] += 1
            return {"status": "no_change", "unchanged": ["sec1.tex"],
                    "dispatched_via": "worker"}

        pool = self._pool(executor, max_concurrency=1)
        dispatch_id = pool.submit(make_request("dsp-t3"))
        results = pool.join()

        result = results[dispatch_id]
        self.assertEqual(result.status, "no_change")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(calls["n"], 1)
        stats = pool.stats()
        self.assertEqual(stats["no_change"], 1)
        self.assertEqual(stats["retried"], 0)
        self.assertEqual(stats["ok"], 0)

    def test_t03b_run_dispatch_halt_shape_is_no_change(self):
        """run_dispatch 真实返回形态：HALT + 嵌套 dispatch_results/no_change。"""
        def executor(request):
            return {
                "stage": "HALTED",
                "routing_action": "HALT",
                "error_message": "派发 dsp-t3b 的产物未发生变化: sec1.tex",
                "dispatch_results": {
                    "dsp-t3b": {"status": "no_change", "unchanged": ["sec1.tex"],
                                "dispatched_via": "worker"}
                },
                "history": [],
            }

        pool = self._pool(executor, max_concurrency=1)
        dispatch_id = pool.submit(make_request("dsp-t3b"))
        results = pool.join()
        self.assertEqual(results[dispatch_id].status, "no_change")
        self.assertEqual(results[dispatch_id].attempts, 1)
        self.assertEqual(pool.stats()["retried"], 0)


class T04_T06_RetryPolicy(PoolTestCase):
    """T4 契约故障不重试 / T5 重试耗尽 / T6 无执行器即 deferred。"""

    def test_t04_contract_failure_single_attempt(self):
        calls = {"n": 0}

        def executor(request):
            calls["n"] += 1
            raise RuntimeError("contract violation: artifacts not allowed")

        pool = self._pool(executor, max_concurrency=1)
        dispatch_id = pool.submit(make_request("dsp-t4", retry_class="contract"))
        results = pool.join()

        result = results[dispatch_id]
        self.assertEqual(result.attempts, 1)
        self.assertEqual(calls["n"], 1)
        self.assertIn(result.status, ("failed", "error"))
        self.assertIn("contract violation", result.error)
        self.assertEqual(pool.stats()["retried"], 0)
        self.assertEqual(pool.stats()["failed"], 1)

    def test_t05_retry_exhaustion(self):
        calls = {"n": 0}

        def executor(request):
            calls["n"] += 1
            raise ValueError("worker always blows up")

        pool = self._pool(executor, max_concurrency=1, max_retries=2)
        dispatch_id = pool.submit(make_request("dsp-t5"))
        results = pool.join()

        result = results[dispatch_id]
        self.assertIn(result.status, ("failed", "error"))
        self.assertEqual(result.attempts, 3)  # max_retries + 1
        self.assertEqual(calls["n"], 3)
        self.assertIn("worker always blows up", result.error)
        self.assertEqual(pool.stats()["retried"], 2)
        self.assertEqual(pool.stats()["failed"], 1)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_t06_no_executor_defers_without_threads(self):
        before = {t.name for t in self._worker_threads()}
        with mock.patch.object(wp, "_worker_registry_available", return_value=False):
            pool = self._pool(executor=None, max_concurrency=2)
            ids = [pool.submit(make_request("dsp-t6-%d" % i)) for i in range(3)]
            started = time.monotonic()
            results = pool.join()  # 必须立即返回
            elapsed = time.monotonic() - started
        after = {t.name for t in self._worker_threads()}

        self.assertLess(elapsed, 1.0)
        self.assertTrue(after.issubset(before))  # 没有新起任何 worker 线程
        self.assertEqual(len(results), 3)
        for dispatch_id in ids:
            self.assertEqual(results[dispatch_id].status, "deferred")
            self.assertEqual(results[dispatch_id].attempts, 0)
            self.assertIsNotNone(results[dispatch_id].error)
        stats = pool.stats()
        self.assertEqual(stats["submitted"], 3)
        self.assertEqual(stats["deferred"], 3)
        self.assertEqual(stats["completed"], 3)
        self.assertEqual(stats["max_in_flight"], 0)
        self.assertEqual(stats["ok"], 0)
        self.assertEqual(pool.pending(), 0)


class T07_T08_DeadlineAndDuplicates(PoolTestCase):
    """T7 join 截止强转 timeout / T8 重复 dispatch_id 不重复执行。"""

    def test_t07_join_timeout_marks_in_flight(self):
        def executor(request):
            time.sleep(0.6)
            return {"status": "success", "files": {"sec1.tex": "% late\n"}}

        pool = self._pool(executor, max_concurrency=1)
        for i in range(2):
            pool.submit(make_request("dsp-t7-%d" % i))

        started = time.monotonic()
        results = pool.join(timeout=0.2)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)  # 整个用例不得挂死
        self.assertEqual(len(results), 2)
        for result in results.values():
            # 未声明 cancellable → timeout_deferred（线程不可抢占，结果延后；
            # audit §17 任务8：不支持取消的 worker 绝不伪装 cancelled）
            self.assertEqual(result.status, "timeout_deferred")
            self.assertIn("join", result.error)
            # dsp-t7-0 已开始执行（attempts==1），dsp-t7-1 仍在排队（attempts==0）；
            # 线程不可抢占，故 join 到点后一律转 timeout_deferred，attempts 只反映已发生的尝试次数
            self.assertIn(result.attempts, (0, 1))
        # timeout_deferred 计入 timeout 计数（对调用方同为「须重派」的失败态）
        self.assertGreaterEqual(pool.stats()["timeout"], 1)
        self.assertEqual(pool.pending(), 0)

        # 迟到结果不得覆盖 timeout，必须落入 orphans：
        # max_concurrency=1 ⇒ dsp-t7-0 正在执行、无法取消 → 迟到结果进 orphans；
        # dsp-t7-1 仍在队列 → 被 future.cancel() 取消，不会产生迟到结果（避免无谓执行）
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and pool.stats()["orphans"] < 1:
            time.sleep(0.02)
        orphans = pool.orphans()
        self.assertEqual({item["dispatch_id"] for item in orphans}, {"dsp-t7-0"})
        self.assertTrue(all(item["reason"] == "already_finalized" for item in orphans))
        self.assertTrue(all(item["status"] == "ok" for item in orphans))
        # join 幂等：重复调用返回同一结果
        self.assertEqual(pool.join(timeout=0.2), results)

    def test_t08_duplicate_submit_executes_once(self):
        calls = []

        def executor(request):
            calls.append(request["dispatch_id"])
            time.sleep(0.05)
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=2)
        first = pool.submit(make_request("dsp-t8-dup"))
        second = pool.submit(make_request("dsp-t8-dup"))
        third = pool.submit(make_request("dsp-t8-dup"))

        self.assertEqual((first, second, third), ("dsp-t8-dup",) * 3)
        results = pool.join()

        self.assertEqual(calls, ["dsp-t8-dup"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results["dsp-t8-dup"].status, "ok")
        stats = pool.stats()
        self.assertEqual(stats["submitted"], 1)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(pool.pending(), 0)


class T09_T11_Bookkeeping(PoolTestCase):
    """T9 join 幂等 + reset / T10 stats 契约 / T11 默认执行器签名适配。"""

    def test_t09_join_idempotent_and_reset(self):
        def executor(request):
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=2)
        for i in range(3):
            pool.submit(make_request("dsp-t9-%d" % i))
        first = pool.join()
        second = pool.join(timeout=5.0)
        third = pool.join()
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(len(first), 3)

        pool.reset()
        self.assertEqual(pool.pending(), 0)
        self.assertEqual(pool.stats(), {key: 0 for key in wp.STAT_KEYS})
        self.assertEqual(pool.orphans(), [])
        # reset 后可再次提交（executor 以 wait=False 关闭后重建）
        dispatch_id = pool.submit(make_request("dsp-t9-after-reset"))
        self.assertEqual(pool.join()[dispatch_id].status, "ok")

    def test_t10_stats_contract_keys(self):
        def executor(request):
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=1)
        pool.submit(make_request("dsp-t10"))
        pool.join()
        stats = pool.stats()
        self.assertEqual(set(stats), set(wp.STAT_KEYS))
        for key, value in stats.items():
            self.assertIsInstance(value, int, key)
        # 状态分布自洽：ok + failed + timeout + no_change + deferred == completed
        self.assertEqual(
            stats["ok"] + stats["failed"] + stats["timeout"]
            + stats["no_change"] + stats["deferred"], stats["completed"])

    def test_t11_default_executor_adapts_run_dispatch_signature(self):
        """默认执行器：惰性 import + run_dispatch(state, request, *, ...) 适配。"""
        try:
            import workflow.nodes.agent_nodes  # noqa: F401
        except Exception as exc:  # pragma: no cover - 无 langgraph 环境
            self.skipTest("workflow.nodes.agent_nodes 不可用: %s" % exc)

        seen = {}

        def run_dispatch(state, request, *, success_stage, success_history):
            seen["state"] = state
            seen["request_id"] = request["dispatch_id"]
            seen["stage"] = success_stage
            return {
                "stage": success_stage,
                "routing_action": "PROCEED",
                "dispatch_results": {"dsp-t11": {"status": "success"}},
                "history": [],
                "pending_dispatch": None,
            }

        fake_module = types.ModuleType("workflow.nodes.agent_nodes")
        fake_module.run_dispatch = run_dispatch
        with mock.patch.dict(sys.modules, {"workflow.nodes.agent_nodes": fake_module}), \
                mock.patch.object(wp, "_worker_registry_available", return_value=True):
            pool = self._pool(executor=None, max_concurrency=1)
            dispatch_id = pool.submit(make_request("dsp-t11", timeout_seconds=30))
            results = pool.join()

        result = results[dispatch_id]
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts, 1)
        self.assertIsNone(result.error)
        self.assertEqual(result.output["routing_action"], "PROCEED")
        self.assertEqual(seen["request_id"], "dsp-t11")
        self.assertEqual(seen["stage"], "DISPATCH_COMPLETED")
        self.assertEqual(seen["state"]["project_root"], ".")

    def test_t11b_default_executor_halt_and_no_change(self):
        """HALT(missing_artifacts) → failed；HALT(no_change) → no_change。"""
        try:
            import workflow.nodes.agent_nodes  # noqa: F401
        except Exception as exc:  # pragma: no cover - 无 langgraph 环境
            self.skipTest("workflow.nodes.agent_nodes 不可用: %s" % exc)

        def run_dispatch(state, request, *, success_stage, success_history):
            if request["dispatch_id"] == "dsp-t11-missing":
                return {
                    "stage": "HALTED", "routing_action": "HALT",
                    "error_message": "派发 dsp-t11-missing 的产物未生成: sec1.tex",
                    "dispatch_results": {"dsp-t11-missing": {
                        "status": "missing_artifacts", "missing": ["sec1.tex"]}},
                    "history": [],
                }
            return {
                "stage": "HALTED", "routing_action": "HALT",
                "error_message": "派发 dsp-t11-nochange 的产物未发生变化: sec1.tex",
                "dispatch_results": {"dsp-t11-nochange": {
                    "status": "no_change", "unchanged": ["sec1.tex"]}},
                "history": [],
            }

        fake_module = types.ModuleType("workflow.nodes.agent_nodes")
        fake_module.run_dispatch = run_dispatch
        with mock.patch.dict(sys.modules, {"workflow.nodes.agent_nodes": fake_module}), \
                mock.patch.object(wp, "_worker_registry_available", return_value=True):
            pool = self._pool(executor=None, max_concurrency=2)
            pool.submit(make_request("dsp-t11-missing", timeout_seconds=30))
            pool.submit(make_request("dsp-t11-nochange", timeout_seconds=30))
            results = pool.join()

        missing = results["dsp-t11-missing"]
        self.assertIn(missing.status, ("failed", "error"))
        self.assertEqual(missing.attempts, 3)  # missing_artifacts 属瞬时失败，可重试
        self.assertIn("产物未生成", missing.error)
        nochange = results["dsp-t11-nochange"]
        self.assertEqual(nochange.status, "no_change")
        self.assertEqual(nochange.attempts, 1)
        self.assertEqual(pool.stats()["no_change"], 1)
        self.assertEqual(pool.stats()["failed"], 1)

    def test_t11c_default_executor_single_arg_form(self):
        """规格字面的 run_dispatch(request) 单参形态同样支持。"""
        try:
            import workflow.nodes.agent_nodes  # noqa: F401
        except Exception as exc:  # pragma: no cover - 无 langgraph 环境
            self.skipTest("workflow.nodes.agent_nodes 不可用: %s" % exc)

        def run_dispatch(request):
            return {"status": "success", "dispatch_id": request["dispatch_id"]}

        fake_module = types.ModuleType("workflow.nodes.agent_nodes")
        fake_module.run_dispatch = run_dispatch
        with mock.patch.dict(sys.modules, {"workflow.nodes.agent_nodes": fake_module}), \
                mock.patch.object(wp, "_worker_registry_available", return_value=True):
            pool = self._pool(executor=None, max_concurrency=1)
            dispatch_id = pool.submit(make_request("dsp-t11c", timeout_seconds=30))
            results = pool.join()

        self.assertEqual(results[dispatch_id].status, "ok")
        self.assertEqual(results[dispatch_id].output["dispatch_id"], "dsp-t11c")


# ---------------------------------------------------------------------------
# T12-T13 worker 能力声明（audit §17 任务8 / §12.2 / §16）
# ---------------------------------------------------------------------------

class T12_WorkerCapabilities(PoolTestCase):
    """T12: 能力声明决定真实并行度与超时语义。"""

    def test_t12a_effective_concurrency_min_of_profile_and_declared(self):
        pool = self._pool(max_concurrency=4, worker_capabilities={
            "sync": False, "async": True, "cancellable": True, "max_concurrency": 2})
        # 未 submit 前即可查询（plan 节点据此承诺 min_parallelism）
        self.assertEqual(pool.effective_concurrency, 2)
        self.assertEqual(pool.worker_capabilities["max_concurrency"], 2)

        serial = self._pool(max_concurrency=4, worker_capabilities={
            "sync": True, "max_concurrency": 1})
        self.assertEqual(serial.effective_concurrency, 1)

    def test_t12b_declared_limit_caps_thread_pool(self):
        lock = threading.Lock()
        state = {"cur": 0, "peak": 0}

        def executor(request):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.1)
            with lock:
                state["cur"] -= 1
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=4, worker_capabilities={
            "async": True, "cancellable": True, "max_concurrency": 2})
        for i in range(4):
            pool.submit(make_request("dsp-t12b-%d" % i))
        pool.join()
        self.assertEqual(state["peak"], 2)
        self.assertLessEqual(pool.stats()["max_in_flight"], 2)

    def test_t12c_invalid_capabilities_rejected(self):
        with self.assertRaises(ValueError):
            self._pool(max_concurrency=2, worker_capabilities={"unknown": True})
        with self.assertRaises(ValueError):
            self._pool(max_concurrency=2, worker_capabilities={"max_concurrency": 0})

    def test_t12d_registry_declaration_used_when_no_explicit_executor(self):
        from workflow.adapters import worker_registry

        def executor(request):  # pragma: no cover - 不执行
            return {"status": "success"}

        worker_registry.set(executor, name="test-registry", capabilities={
            "sync": True, "async": False, "cancellable": False, "max_concurrency": 1})
        try:
            pool = self._pool(executor=None, max_concurrency=4)
            self.assertEqual(pool.effective_concurrency, 1)
        finally:
            worker_registry.clear()

    def test_t12e_no_declaration_means_serial(self):
        from workflow.adapters import worker_registry

        worker_registry.clear()
        pool = self._pool(executor=None, max_concurrency=4)
        self.assertEqual(pool.effective_concurrency, 1)
        self.assertFalse(pool.worker_capabilities["cancellable"])

    def test_t12f_reset_clears_capability_cache(self):
        pool = self._pool(max_concurrency=4, worker_capabilities={
            "async": True, "cancellable": True, "max_concurrency": 3})
        self.assertEqual(pool.effective_concurrency, 3)
        pool.reset()
        # reset 只清「已解析缓存」，显式声明是构造参数（与 max_concurrency 同级），
        # 惰性重解析后仍生效——不得因 reset 静默放宽并行度
        self.assertEqual(pool.effective_concurrency, 3)


class T13_CancellableWorker(PoolTestCase):
    """T13: 声明 cancellable 的 worker 超时态为 cancelled，其余为 timeout_deferred。"""

    def test_t13a_cancellable_marks_cancelled(self):
        def executor(request):
            time.sleep(0.6)
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=1, worker_capabilities={
            "async": True, "cancellable": True, "max_concurrency": 1})
        pool.submit(make_request("dsp-t13a"))
        results = pool.join(timeout=0.2)
        result = results["dsp-t13a"]
        self.assertEqual(result.status, "cancelled")
        self.assertIn("取消", result.error)
        self.assertEqual(pool.stats()["cancelled"], 1)
        self.assertEqual(pool.stats()["timeout"], 0)

    def test_t13b_non_cancellable_marks_timeout_deferred(self):
        def executor(request):
            time.sleep(0.6)
            return {"status": "success"}

        pool = self._pool(executor, max_concurrency=1, worker_capabilities={
            "async": True, "cancellable": False, "max_concurrency": 1})
        pool.submit(make_request("dsp-t13b"))
        results = pool.join(timeout=0.2)
        result = results["dsp-t13b"]
        self.assertEqual(result.status, "timeout_deferred")
        self.assertEqual(pool.stats()["cancelled"], 0)
        self.assertEqual(pool.stats()["timeout"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
