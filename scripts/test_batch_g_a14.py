#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_g_a14.py — A14：self 工作区探测不得误命中同名目录。

A14 原文（bug.md）::

    index.js:1082-1105 self_dir 探测 cwd/self 误命中（D:\Scripts\self）；
    :582-601 无参 resume 时 proj=plugin_dir。

旧实现在 ``self_dir`` 缺省时只做 ``fs.existsSync(cwd + '/self')``：宿主进程的
cwd 指向哪里完全不可控，于是 qchain_status / qchain_gate / qchain_dispatch 会在
用户毫不知情的情况下对**另一个工作区**报 PASS/FAIL，甚至往别人的 .pd 里写台账
——「目录存在」被当成了「这是我的工作区」。本次修复引入工作区形态校验
（.pd / config.yaml / coverage-map.md / overview-guide / secN.tex 至少命中一项），
隐式候选过不了校验就直接报错，绝不静默落到一个陌生目录上。

驱动方式：node 侧加载真实 index.js 的 ``apply``，只 mock 宿主能力；不启动
Python 流水线、不派发真实 Agent。
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
cp.execFile = (_bin, args, _opts, cb) => {
  cb(null, JSON.stringify({ config: { agent_dispatch: 'safe' }, model_map: {} }), '');
};
syncBuiltinESMExports();

const { apply } = await import(pathToFileURL(path.join(cfg.pluginRoot, 'index.js')).href);
const tools = {};
const ctx = {
  tools: { register(d) { tools[d.name] = d; }, get() { return undefined; } },
  get() { return undefined; },
};
apply(ctx, { projectRoot: cfg.pluginRoot });

const runRoot = cfg.runRoot;
const real = path.join(runRoot, 'real', 'self');
const decoy = path.join(runRoot, 'decoy', 'self');
const stranger = path.join(runRoot, 'stranger', 'self');   // 同名但是真工作区
fs.mkdirSync(path.join(real, '.pd'), { recursive: true });
fs.mkdirSync(decoy, { recursive: true });
fs.mkdirSync(path.join(stranger, '.pd'), { recursive: true });
fs.writeFileSync(path.join(decoy, 'notes.txt'), 'not a workspace\n', 'utf8');
fs.writeFileSync(path.join(stranger, 'sec1.tex'), 'x\n', 'utf8');
fs.writeFileSync(path.join(real, 'sec1.tex'), 'x\n', 'utf8');
fs.writeFileSync(path.join(real, '.pd', 'input.md'), '# src\n', 'utf8');

const request = {
  dispatch_id: 'dsp-a14',
  batch_id: 'bat-a14',
  run_id: 'run-a14',
  stage: 'T2_WRITE',
  role: 'author',
  scope: 'sec1',
  prompt_file: 'subagent_prompt.md',
  artifacts: ['sec1.tex'],
  context: {},
  instruction: 'A14 fixture',
  max_retries: 0,
};
fs.writeFileSync(path.join(real, '.pd', 'request.json'), JSON.stringify(request, null, 2), 'utf8');
fs.writeFileSync(path.join(stranger, '.pd', 'request.json'), JSON.stringify(request, null, 2), 'utf8');

const parentAgent = { session: { requestHeader: () => ({ config: {} }) } };
const out = { cases: {} };

// ---- case 1：cwd 下有个同名 self，但它不是工作区 → 必须报错，不得静默使用 ----
process.chdir(path.join(runRoot, 'decoy'));
try {
  const r = await tools.qchain_dispatch.execute({ request_ref: 'request.json' },
                                                 { signal: new AbortController().signal,
                                                   agent: parentAgent });
  out.cases.decoy = { ok: r.success, report: r.report, self_dir: r.self_dir,
                      decoy_pd: fs.existsSync(path.join(decoy, '.pd')) };
} catch (e) {
  out.cases.decoy = { threw: String(e) };
}

// ---- case 2：显式 self_dir 仍然优先（用户自己负责） ----
try {
  const r = await tools.qchain_dispatch.execute(
    { self_dir: real, request_ref: path.join(real, '.pd', 'request.json'), max_concurrency: 1 },
    { signal: new AbortController().signal, agent: parentAgent });
  out.cases.explicit = { ok: r.success, self_dir: r.self_dir,
                         evidence: fs.existsSync(path.join(real, '.pd', 'agent-calls',
                                                           'dsp-a14.json')) };
} catch (e) {
  out.cases.explicit = { threw: String(e) };
}

// ---- case 3：cwd 下的 self 确实是工作区 → 隐式选中它（正常路径不被误伤） ----
process.chdir(path.join(runRoot, 'stranger'));
try {
  const r = await tools.qchain_dispatch.execute(
    { request_ref: path.join(stranger, '.pd', 'request.json'), max_concurrency: 1 },
    { signal: new AbortController().signal, agent: parentAgent });
  out.cases.genuine = { ok: r.success, self_dir: r.self_dir,
                         report: r.report || '' };
} catch (e) {
  out.cases.genuine = { threw: String(e) };
}

process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "需要 node")
class TestSelfDirDetection(unittest.TestCase):
    """index.js 的 self 工作区探测（真实 apply，只 mock 宿主能力）。"""

    def _run(self):
        run_root = tempfile.mkdtemp(prefix="batch-g-a14-")
        self.addCleanup(shutil.rmtree, run_root, True)
        with tempfile.TemporaryDirectory() as td:
            driver = os.path.join(td, "driver.mjs")
            with open(driver, "w", encoding="utf-8") as fh:
                fh.write(_DRIVER)
            payload = json.dumps({"pluginRoot": PLUGIN_ROOT, "runRoot": run_root},
                                 ensure_ascii=False)
            proc = subprocess.run([NODE, driver, payload], capture_output=True,
                                  text=True, encoding="utf-8")
            if proc.returncode != 0:
                raise AssertionError("driver 失败: %s\n%s" % (proc.stdout, proc.stderr))
            return json.loads(proc.stdout)

    def test_same_named_dir_that_is_not_a_workspace_is_rejected(self):
        """A14：cwd 下有个 self 但不像工作区 → 显式报错，不静默接手。"""
        out = self._run()["cases"]["decoy"]
        self.assertNotIn("threw", out)
        self.assertFalse(out["ok"], "误命中同名目录时必须失败，而不是继续跑")
        self.assertIn("未找到 self", out["report"])
        self.assertEqual(out["self_dir"], "")
        self.assertFalse(out["decoy_pd"], "不得在陌生目录里造 .pd/")

    def test_explicit_self_dir_still_wins(self):
        out = self._run()["cases"]["explicit"]
        self.assertNotIn("threw", out)
        self.assertTrue(out["evidence"], "显式 self_dir 的正常路径必须照旧工作")

    def test_genuine_workspace_in_cwd_still_auto_selected(self):
        """A14：cwd 下的 self 确实是工作区 → 隐式选中它（正常路径不被误伤）。"""
        out = self._run()["cases"]["genuine"]
        self.assertNotIn("threw", out)
        self.assertTrue(out["self_dir"].replace("\\", "/").endswith("/stranger/self"),
                        "隐式候选通过形态校验时必须被选中，实际 self_dir=%r" % out["self_dir"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
