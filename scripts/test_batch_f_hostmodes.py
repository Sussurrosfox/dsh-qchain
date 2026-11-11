#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_f_hostmodes.py — 派发链路「宿主执行模式」回归（N3 生效路由 / N6 桥接归因）

两个审计发现：

1. **N3 路由不可归因**：台账原先只记 ``provider/model``，而这两个值来自
   ``agent_dispatch.model_map``——默认整块注释掉，于是永远是 null。宿主
   ``resolveChildAgentOptions`` 在请求未给路由时**继承父会话当前路由**，而
   父会话的模型可以在会话中途被切换。结果：台账写着 null，实际可能跑在
   一个慢模型上（生产证据：首 token 890s、连续三次 900s 超时零输出），
   事后无法归因。修复后在结果落地时读子 Agent 会话的
   ``requestHeader().config``，把**生效路由**单独记账。

2. **N6 桥接不可用被误判为内容错误**：宿主 ``subagent`` 工具是异步激活语义
   （``execute`` 立即返回 ``started subagent <id>``）。旧实现桥接失败就回落到
   它，拿回一句 "started subagent …" 当作产物，判成
   ``content_error``（不可重试）并归咎「未按 FILE 协议返回」——真正的环境
   问题被算到子 Agent 头上，且后台子 Agent 无人 await、继续烧 token。

驱动方式：node 侧加载真实 index.js 的 ``apply``，只 mock 宿主能力
（``tools.get('subagent')`` / ``get('subagents')``），不启动 Python 流水线。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
INDEX_JS = os.path.join(PLUGIN_ROOT, "index.js")
NODE = shutil.which("node")

_DRIVER = r"""
import fs from 'node:fs';
import path from 'node:path';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';

const cfg = JSON.parse(process.argv[2]);
const pluginRoot = cfg.pluginRoot;
const runRoot = cfg.runRoot;

// 配置读取走子进程（read_dispatch_inputs）；这里只回配置，不跑流水线。
cp.execFile = (_bin, args, _opts, cb) => {
  cb(null, JSON.stringify({ config: { agent_dispatch: 'safe', agent_max_concurrency: 0 },
                            model_map: cfg.modelMap || {} }), '');
};
syncBuiltinESMExports();

const { apply } = await import(pathToFileURL(path.join(pluginRoot, 'index.js')).href);
const tools = {};
const calls = { host: 0, activation: 0 };
const mode = cfg.hostMode;   // 'async-activation' | 'bridge-ok'
const effective = cfg.effectiveRoute || null;

const activationResult = {
  output: [{ type: 'text', text: '<<<FILE:sec1.tex>>>\n正文\n<<<END:sec1.tex>>>' }],
};
const subagentsService = {
  getProvider: () => true,
  startActivation: async () => {
    calls.activation += 1;
    return {
      result: Promise.resolve(activationResult),
      handle: { agent: { session: { requestHeader: () => ({ config: effective }) } } },
      dispose() {},
    };
  },
};
const ctx = {
  tools: {
    register(d) { tools[d.name] = d; },
    get(n) {
      if (n !== 'subagent') return undefined;
      // 典型宿主：execute 立即返回 "started subagent <id>"，正文经 settlement 回流
      return { execute: async () => { calls.host += 1; return 'started subagent deadbeef'; } };
    },
  },
  get(n) { return n === 'subagents' && mode === 'bridge-ok' ? subagentsService : undefined; },
};
apply(ctx, { projectRoot: pluginRoot });

const self = path.join(runRoot, 'self');
fs.mkdirSync(path.join(self, '.pd'), { recursive: true });
fs.writeFileSync(path.join(self, '.pd', 'input.md'), '# 源材料\n', 'utf8');
const request = {
  dispatch_id: 'dsp-' + mode,
  batch_id: 'bat-f-' + mode,
  run_id: 'run-f',
  stage: 'T2_WRITE',
  role: 'author',
  scope: 'sec1',
  prompt_file: 'subagent_prompt.md',
  artifacts: ['sec1.tex'],
  context: {},
  instruction: 'batch F host-mode fixture',
  max_retries: 0,
};
const reqPath = path.join(self, '.pd', 'request.json');
fs.writeFileSync(reqPath, JSON.stringify(request, null, 2), 'utf8');

// 桥接需要「父 Agent」句柄（startActivation 的 request.parent）；宿主在真实
// 调用里由工具执行上下文带入，这里给一个最小桩。
const parentAgent = { session: { requestHeader: () => ({ config: { provider: 'parent', model: 'parent-model' } }) } };
const result = await tools.qchain_dispatch.execute(
  { self_dir: self, request_ref: reqPath, max_concurrency: 1 },
  { signal: new AbortController().signal, agent: parentAgent });

const evPath = path.join(self, '.pd', 'agent-calls', request.dispatch_id + '.json');
const evidence = fs.existsSync(evPath) ? JSON.parse(fs.readFileSync(evPath, 'utf8')) : null;
const out = {
  status: result.results[0].status,
  error: result.results[0].error || '',
  error_class: result.results[0].error_class || '',
  host_calls: calls.host,
  activation_calls: calls.activation,
  evidence: evidence && {
    provider: evidence.provider,
    model: evidence.model,
    requested_provider: evidence.requested_provider,
    requested_model: evidence.requested_model,
    effective_provider: evidence.effective_provider,
    effective_model: evidence.effective_model,
    route_source: evidence.route_source,
    activation_provider: evidence.activation_provider,
  },
  evidence_exists: !!evidence,
  artifact_written: fs.existsSync(path.join(self, 'sec1.tex')),
};
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "需要 node")
class TestDispatchHostModes(unittest.TestCase):
    """index.js 的宿主模式识别（真实 apply，mock 宿主能力）。"""

    def _run(self, host_mode, effective_route=None):
        run_root = tempfile.mkdtemp(prefix="batch-f-host-")
        self.addCleanup(shutil.rmtree, run_root, True)
        with tempfile.TemporaryDirectory() as td:
            driver = os.path.join(td, "driver.mjs")
            with open(driver, "w", encoding="utf-8") as fh:
                fh.write(_DRIVER)
            payload = json.dumps({"pluginRoot": PLUGIN_ROOT, "runRoot": run_root,
                                  "hostMode": host_mode,
                                  "effectiveRoute": effective_route},
                                 ensure_ascii=False)
            proc = subprocess.run([NODE, driver, payload], capture_output=True,
                                  text=True, encoding="utf-8")
            if proc.returncode != 0:
                raise AssertionError("driver 失败: %s\n%s" % (proc.stdout, proc.stderr))
            return json.loads(proc.stdout)

    def test_async_activation_fallback_is_environment_error(self):
        """N6：桥接不可用、宿主只回 "started subagent …" → 环境归因，不当内容错误。"""
        out = self._run("async-activation")
        self.assertEqual(out["host_calls"], 1)
        self.assertEqual(out["status"], "bridge_unavailable")
        self.assertEqual(out["error_class"], "config")
        self.assertIn("同步桥接不可用", out["error"])
        # 不再把环境问题算成「未按 FILE 协议返回产物」
        self.assertNotIn("FILE", out["error"])
        self.assertFalse(out["artifact_written"])

    def test_bridge_ok_records_effective_route(self):
        """N3：桥接可用时把生效路由记进台账（请求值保持 null）。"""
        out = self._run("bridge-ok", {"provider": "scnet", "model": "SCNet-Max",
                                      "reasoningEffort": "high"})
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["evidence_exists"])
        ev = out["evidence"]
        self.assertIsNone(ev["provider"], "请求路由为空时应保持 null")
        self.assertIsNone(ev["requested_provider"])
        self.assertEqual(ev["effective_provider"], "scnet")
        self.assertEqual(ev["effective_model"], "SCNet-Max")
        self.assertEqual(ev["route_source"], "host-session")
        self.assertEqual(ev["activation_provider"], "spawn")

    def test_bridge_ok_without_route_is_marked_unknown(self):
        """N3：读不到生效路由时标 inherited-unknown，绝不把请求值冒充生效值。"""
        out = self._run("bridge-ok", None)
        ev = out["evidence"]
        self.assertIsNone(ev["effective_provider"])
        self.assertEqual(ev["route_source"], "inherited-unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
