#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_worker_registry.py — worker 能力声明注册表回归测试（unittest）。

覆盖 workflow/adapters/worker_registry.py（audit §17 任务8）：
    T01  normalize_capabilities 归一化与未知键/非法值拒绝
    T02  set/clear/capabilities/available/name/call/describe
    T03  未注册时返回保守默认（sync=True, cancellable=False, 并发 1）
    T04  set 的 capabilities=None 等价于保守默认（不静默放大并行度）
    T05  描述串（describe）含 worker_capabilities，供 UI/日志观测
"""

import importlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

# workflow/adapters/__init__.py 把子模块名重绑定为单例实例，必须按全限定
# 路径 importlib 取模块本体（测试要测 normalize_capabilities 与单例两类对象）
wr = importlib.import_module("workflow.adapters.worker_registry")


class T01_NormalizeCapabilities(unittest.TestCase):
    """T01: 归一化边界——布尔 coercion、并发 <1、未知键一律显式失败。"""

    def test_t01a_defaults_when_empty(self):
        caps = wr.normalize_capabilities(None)
        self.assertEqual(caps, {"sync": True, "async": False,
                                "cancellable": False, "max_concurrency": 1})

    def test_t01b_bool_coercion(self):
        caps = wr.normalize_capabilities({"async": 1, "cancellable": 0})
        self.assertTrue(caps["async"])
        self.assertFalse(caps["cancellable"])

    def test_t01c_concurrency_must_be_positive_int(self):
        with self.assertRaises(ValueError):
            wr.normalize_capabilities({"max_concurrency": 0})
        with self.assertRaises(ValueError):
            wr.normalize_capabilities({"max_concurrency": True})
        with self.assertRaises(ValueError):
            wr.normalize_capabilities({"max_concurrency": "4"})

    def test_t01d_unknown_key_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            wr.normalize_capabilities({"teleport": True})
        self.assertIn("teleport", str(ctx.exception))

    def test_t01e_non_dict_rejected(self):
        with self.assertRaises(TypeError):
            wr.normalize_capabilities(["async"])


class T02_SetAndClear(unittest.TestCase):
    """T02: 注册/清除生命周期；单例状态跨断言隔离。"""

    def setUp(self):
        wr.worker_registry.clear()
        self.addCleanup(wr.worker_registry.clear)

    def test_t02a_unregistered_conservative_default(self):
        self.assertFalse(wr.worker_registry.available)
        # name 未注册时返回哨兵串 "none"（describe 直接透出，UI 可读）
        self.assertEqual(wr.worker_registry.name, "none")
        self.assertEqual(wr.worker_registry.capabilities, {
            "sync": True, "async": False, "cancellable": False,
            "max_concurrency": 1})

    def test_t02b_set_stores_callable_and_capabilities(self):
        def fake(request):  # pragma: no cover - 不真实调用
            return "{}"

        wr.worker_registry.set(fake, name="fake-llm", capabilities={
            "sync": False, "async": True, "cancellable": True,
            "max_concurrency": 6})
        self.assertTrue(wr.worker_registry.available)
        self.assertEqual(wr.worker_registry.name, "fake-llm")
        caps = wr.worker_registry.capabilities
        self.assertEqual(caps["max_concurrency"], 6)
        self.assertTrue(caps["async"])
        self.assertTrue(caps["cancellable"])
        # 返回副本：外部篡改不得污染注册表
        caps["max_concurrency"] = 99
        self.assertEqual(wr.worker_registry.capabilities["max_concurrency"], 6)

    def test_t02c_call_dispatches_to_registered_callable(self):
        seen = []

        def fake(system_prompt, user_content):
            seen.append((system_prompt, user_content))
            return "echo:" + user_content

        wr.worker_registry.set(fake)
        self.assertEqual(wr.worker_registry.call("sys", "ping"), "echo:ping")
        self.assertEqual(seen, [("sys", "ping")])
        self.assertEqual(wr.worker_registry.name, "external-llm")

    def test_t02d_clear_resets_everything(self):
        wr.worker_registry.set(lambda req: "x", capabilities={
            "max_concurrency": 4})
        wr.worker_registry.clear()
        self.assertFalse(wr.worker_registry.available)
        self.assertEqual(wr.worker_registry.name, "none")
        self.assertEqual(wr.worker_registry.capabilities["max_concurrency"], 1)

    def test_t02e_call_without_worker_raises(self):
        with self.assertRaises(RuntimeError):
            wr.worker_registry.call("sys", "nope")

    def test_t02f_set_without_capabilities_is_conservative(self):
        """§10 红线：不因注册 worker 就静默承诺并行。"""
        wr.worker_registry.set(lambda req: "{}")
        self.assertEqual(wr.worker_registry.capabilities["max_concurrency"], 1)
        self.assertFalse(wr.worker_registry.capabilities["async"])


class T03_Describe(unittest.TestCase):
    """T03: describe() 供 UI/日志观测，必须含能力声明。"""

    def setUp(self):
        wr.worker_registry.clear()
        self.addCleanup(wr.worker_registry.clear)

    def test_t03a_describe_empty(self):
        info = wr.worker_registry.describe()
        self.assertFalse(info["worker_available"])
        self.assertEqual(info["worker_capabilities"], {
            "sync": True, "async": False, "cancellable": False,
            "max_concurrency": 1})

    def test_t03b_describe_with_capabilities(self):
        wr.worker_registry.set(lambda req: "{}", name="cap-llm",
                               capabilities={"async": True,
                                             "cancellable": True,
                                             "max_concurrency": 3})
        info = wr.worker_registry.describe()
        self.assertTrue(info["worker_available"])
        self.assertEqual(info["worker_name"], "cap-llm")
        self.assertEqual(info["worker_capabilities"]["max_concurrency"], 3)
        self.assertTrue(info["worker_capabilities"]["cancellable"])


if __name__ == "__main__":
    unittest.main()
