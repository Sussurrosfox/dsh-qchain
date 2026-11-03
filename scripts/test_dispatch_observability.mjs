// scripts/test_dispatch_observability.mjs — 任务卡21：并发可观测性与口径回归测试
// 运行：node --test scripts/test_dispatch_observability.mjs（插件根目录）
//
// 覆盖（对照 repair-plan/tasks/21.md 与 DATA-CONTRACTS C12.4）：
//   PAROBS-01  cap=4 / 8 单：真实宿主调用计数峰值恰为 4，且每个 attempt 各有独立账（重试不覆盖）
//   PAROBS-03  单请求 / 全缓存命中 / 显式 safe 档位：serial_reason 明确且不返修正文
//   PAROBS-04  取消未获宿主确认：槽位保持占用、不再起新单（不借假释放超 cap）、晚结果不写
//   PAROBS-05  观测口径必须显式标为 host_call，供应商排队/首 token/推理时间一律 null
//   PAROBS-06  单协调者写批摘要，lane 不互相覆盖；同 batch_id 换请求集后指纹变化可被识别
//
// 所有写盘一律落在 os.tmpdir() 的临时工程内，越界写入被写保护拦截并记录。
// 不启动 qchain 流水线、不调用真实模型、不读 Phase 0 令牌。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fsDefault, * as fs from 'node:fs';
import crypto from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import * as os from 'node:os';
import * as path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

// ---------------------------------------------------------------- 夹具工具
function w(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text, 'utf8');
}
const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(cond, label, ms = 5000) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error(`等待超时: ${label}`);
    await sleep(5);
  }
}

function mkProject(name, configYaml) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `qchain-t21-${name}-`));
  const selfDir = path.join(root, 'self');
  fs.mkdirSync(path.join(selfDir, '.pd', 'agent-requests'), { recursive: true });
  w(path.join(selfDir, 'config.yaml'),
    configYaml || 'agent_dispatch: safe\nagent_max_concurrency: 0\nagent_batch_window: 2\n');
  return { root, selfDir };
}

function installWriteGuard(allowedRoot) {
  const originals = {};
  const blocked = [];
  const root = path.resolve(allowedRoot).toLowerCase();
  const inside = (p) => {
    if (p == null || typeof p === 'number') return true;
    const abs = path.resolve(String(p)).toLowerCase();
    return abs === root || abs.startsWith(root + path.sep);
  };
  for (const name of ['writeFileSync', 'appendFileSync', 'mkdirSync', 'renameSync', 'copyFileSync']) {
    originals[name] = fsDefault[name];
    fsDefault[name] = function guarded(...args) {
      const targets = (name === 'renameSync' || name === 'copyFileSync') ? [args[0], args[1]] : [args[0]];
      if (!targets.every(inside)) {
        blocked.push({ op: name, target: String(targets.find((t) => !inside(t))) });
        return undefined;
      }
      return originals[name].apply(this, args);
    };
  }
  syncBuiltinESMExports();
  return {
    blocked,
    restore() {
      for (const [name, fn] of Object.entries(originals)) fsDefault[name] = fn;
      syncBuiltinESMExports();
    },
  };
}

async function loadTool(subagentCalls, replyFn) {
  const tools = {};
  const ctx = {
    tools: {
      register(def) { tools[def.name] = def; },
      get(name) {
        if (name !== 'subagent') return undefined;
        return {
          execute: async (subArgs, exec) => {
            subagentCalls.push(subArgs);
            return replyFn(subArgs, exec);
          },
        };
      },
    },
    get() { return undefined; },
  };
  const mod = await import(pathToFileURL(path.join(ROOT, 'index.js')).href);
  mod.apply(ctx, { projectRoot: ROOT });
  return tools.qchain_dispatch;
}

const fileBlocks = (arts) => arts.map((a) => `<<<FILE:${a}>>>\n% content of ${a}\n<<<END:${a}>>>`).join('\n');
const MARK_RE = /MARK-([A-Za-z0-9_-]+)/;
const markOf = (subArgs) => MARK_RE.exec(String(subArgs.prompt || ''))[1];

/** 批次夹具：写计划文件 + 批次索引 + 各派发单（与 Python 侧同 schema）。 */
function writeBatch(selfDir, {
  batchId, stage = 'T2', sections, planRel = '.pd/dispatch-plan.json',
  runId = 'run-1', writeIndex = true, digestOverride = null, idPrefix = 'dsp', extraRequest = {},
}) {
  const requests = sections.map((sec, i) => ({
    dispatch_id: `${idPrefix}-${batchId}-${i}`,
    batch_id: batchId,
    run_id: runId,
    stage: stage === 'T2_CHECK' ? `T2_CHECK_${sec.toUpperCase()}` : `${stage}_${sec.toUpperCase()}`,
    role: 'author',
    prompt_file: 'subagent_prompt.md',
    artifacts: [`${sec}.tex`],
    instruction: `MARK-${idPrefix}-${batchId}-${i} 写作 ${sec}`,
    command: 'true',
    before: {},
    status: 'pending',
    max_retries: 0,
    context: { section_id: sec },
    ...extraRequest,
  }));
  const plan = {
    batch_id: batchId,
    stage,
    sections,
    required_scope_count: sections.length,
    dispatch_contract_version: 2,
    requests: requests.map((r) => ({
      dispatch_id: r.dispatch_id, batch_id: batchId, scope: r.context.section_id,
      role: r.role, artifacts: r.artifacts, status: 'pending',
    })),
  };
  w(path.join(selfDir, planRel), JSON.stringify(plan, null, 2));
  for (const r of requests) {
    w(path.join(selfDir, `.pd/agent-requests/${r.dispatch_id}.json`), JSON.stringify(r, null, 2));
  }
  if (writeIndex) {
    const digest = digestOverride || sha256(fs.readFileSync(path.join(selfDir, planRel)));
    const index = {
      schema_version: 1,
      batch_id: batchId,
      run_id: runId,
      stage,
      plan_rel: planRel,
      plan_digest: digest,
      request_ids: requests.map((r) => r.dispatch_id),
      request_refs: requests.map((r) => `.pd/agent-requests/${r.dispatch_id}.json`),
      sections,
      required_scope_count: sections.length,
      dispatch_contract_version: 2,
    };
    w(path.join(selfDir, `.pd/dispatch-batches/${batchId}.json`), JSON.stringify(index, null, 2));
  }
  return requests;
}

/** 批次夹具的返回内容：每个 marker → 输出 FILE 块。 */
function replyFromRequests(requests) {
  const byId = new Map(requests.map((r) => [r.dispatch_id, r]));
  return (subArgs) => {
    const id = markOf(subArgs);
    const req = byId.get(id);
    assert.ok(req, `未知 marker: ${id}`);
    return { runId: `run-${id}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
  };
}

// ---------------------------------------------------------------- 证据读取
function readJson(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

function readRun(selfDir, batchId) {
  const abs = path.join(selfDir, '.pd', 'dispatch-runs', `${batchId}.json`);
  assert.ok(fs.existsSync(abs), `批摘要必须落盘: ${abs}`);
  return readJson(abs);
}

function attemptDir(selfDir, dispatchId) {
  return path.join(selfDir, '.pd', 'agent-attempts', dispatchId);
}

function attemptFiles(selfDir, dispatchId) {
  const dir = attemptDir(selfDir, dispatchId);
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir).filter((n) => /^attempt-\d+.*\.json$/.test(n)).sort();
}

function readAttempt(selfDir, dispatchId, n = 1) {
  const files = attemptFiles(selfDir, dispatchId);
  const hit = files.find((f) => f.startsWith(`attempt-${String(n).padStart(4, '0')}`));
  assert.ok(hit, `${dispatchId} 缺少第 ${n} 次 attempt 账（现有: ${files.join(',') || '无'}）`);
  return readJson(path.join(attemptDir(selfDir, dispatchId), hit));
}

/** C12.4 逐 attempt 必填观测字段（缺一即视为口径不合格）。 */
const C12_FIELDS = [
  'queued_at', 'host_call_started_at', 'host_call_finished_at',
  'dispatch_id', 'batch_id', 'attempt', 'executor_kind',
  'parent_run_id', 'child_run_id',
  'configured_concurrency', 'effective_concurrency',
];

function assertC12Attribution(rec, label) {
  for (const key of C12_FIELDS) {
    assert.ok(Object.prototype.hasOwnProperty.call(rec, key), `${label} 缺 C12.4 字段 ${key}`);
  }
  assert.equal(rec.observation_scope, 'host_call',
    `${label}: 观测口径必须显式标为 host_call（宿主调用窗口）`);
  // 不得把宿主开始时间伪装成模型推理开始
  assert.equal(rec.inference_started_at, null,
    `${label}: 推理开始时间不可冒充为宿主调用开始时间，必须 null`);
  assert.equal(rec.vendor_queue_ms, null, `${label}: 供应商排队时间不可臆造`);
  assert.equal(rec.vendor_first_token_ms, null, `${label}: 首 token 时间不可臆造`);
  assert.equal(rec.vendor_inference_ms, null, `${label}: 模型推理时间不可臆造`);
  assert.ok(rec.lane && typeof rec.lane === 'object', `${label}: 缺 lane 观测`);
  assert.ok(Number.isInteger(rec.lane.index) || Number.isInteger(rec.lane.id),
    `${label}: lane.index 必须是整数`);
  assert.ok(Array.isArray(rec.lifecycle) && rec.lifecycle.length >= 3,
    `${label}: 缺生命周期事件序列（至少 开始/宿主调用/结束）`);
  assert.ok(['finished', 'unfinished'].includes(rec.lifecycle_outcome),
    `${label}: lifecycle_outcome 只能是 finished/unfinished，实际 ${rec.lifecycle_outcome}`);
  assert.ok(['released', 'held'].includes(rec.host_slot),
    `${label}: host_slot 只能是 released/held，实际 ${rec.host_slot}`);
}

// ---------------------------------------------------------------- PAROBS-01
test('PAROBS-01a cap=4 / 8 单：宿主调用峰值恰为 4，8 份 attempt 账独立', async () => {
  const { root, selfDir } = mkProject('peak4');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs01';
    const sections = ['s1', 's2', 's3', 's4', 's5', 's6', 's7', 's8'];
    const requests = writeBatch(selfDir, { batchId, sections });
    const reply = replyFromRequests(requests);
    const gates = new Map();
    const started = [];
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const id = markOf(subArgs);
      started.push(id);
      await new Promise((res) => gates.set(id, res));
      return reply(subArgs);
    });
    const p = tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 4 }, {});
    await waitFor(() => started.length >= 4, 'cap=4 时应同时启动 4 单');
    assert.equal(started.length, 4, `cap=4 时不得并行超过 4，实际已启动 ${started.length}`);
    const released = new Set();
    while (released.size < 8) {
      const pending = started.filter((id) => !released.has(id));
      if (pending.length === 0) { await sleep(10); continue; }
      for (const id of pending) { released.add(id); gates.get(id)(); }
      await sleep(10);
    }
    const res = await p;
    assert.equal(res.success, true, res.report);
    assert.equal(res.ok, 8);
    assert.equal(calls.length, 8, '8 单都必须真正调用宿主（无重复执行）');
    // 工具返回口径
    assert.equal(res.observed_scope, 'host_call', '工具返回必须标明观测口径为宿主调用');
    assert.equal(res.peak_observed_host_in_flight, 4,
      `宿主在飞峰值应为 4，实际 ${res.peak_observed_host_in_flight}`);
    assert.equal(res.real_start_count, 8);
    assert.equal(res.cap_respected, true);
    // 批摘要（单协调者落盘）
    const run = readRun(selfDir, batchId);
    assert.equal(run.schema_version, 2, '批摘要 schema 必须是 v2（含 C12.4 归属字段）');
    assert.deepEqual(run.request_ids, requests.map((r) => r.dispatch_id), '批摘要必须记录请求集');
    assert.equal(run.request_count, 8);
    assert.equal(run.peak_observed_host_in_flight, 4);
    assert.equal(run.observed_host_in_flight, 4, '必须同时提供 observed_host_in_flight 口径名');
    assert.equal(run.peak_ready, 8, '批次开始时 8 单全部 ready');
    assert.equal(run.observed_scope, 'host_call');
    assert.equal(run.vendor_queue_ms, null, '供应商排队时间未知必须为 null');
    assert.equal(run.inference_ms, null, '模型推理时间未知必须为 null');
    assert.equal(run.slots_held_unconfirmed, 0);
    assert.equal(run.undispatched, 0);
    assert.ok(run.queue_wait_ms && run.queue_wait_ms.source === 'coordinator_lane_queue',
      '排队等待时间必须带来源');
    assert.equal(typeof run.lanes, 'object');
    assert.equal(Array.isArray(run.lanes) ? run.lanes.length : -1, 4, '4 条 lane 各自留痕');
    for (const lane of run.lanes) {
      assert.ok(lane.entered_at && lane.exited_at, `lane 进出时点必须留痕: ${JSON.stringify(lane)}`);
    }
    assert.equal(run.serial_reason_code, '', '峰值达到 cap 时不得虚报串行原因');
    assert.equal(run.serial_reason_source, 'observed_parallel');
    // 逐 attempt 独立账 + C12.4 归属字段
    for (const r of requests) {
      const files = attemptFiles(selfDir, r.dispatch_id);
      assert.deepEqual(files, ['attempt-0001.json'],
        `${r.dispatch_id} 应恰有 1 份 attempt 账，实际 ${files.join(',')}`);
      const rec = readAttempt(selfDir, r.dispatch_id, 1);
      assertC12Attribution(rec, r.dispatch_id);
      assert.equal(rec.status, 'ok');
      assert.equal(rec.lifecycle_outcome, 'finished');
      assert.equal(rec.host_slot, 'released');
      assert.equal(rec.batch_id, batchId);
      assert.equal(rec.parent_run_id, 'run-1', 'parent_run_id 必须是批次 run_id');
      assert.ok(rec.child_run_id, 'child_run_id 必须记录宿主返回的子 run id');
      assert.equal(rec.effective_concurrency, 4);
    }
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PAROBS-01b 重试：失败 attempt 的账不被覆盖，两次 attempt 各自完整', async () => {
  const { root, selfDir } = mkProject('retry');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs02';
    const requests = writeBatch(selfDir, {
      batchId, sections: ['sec1'], extraRequest: { max_retries: 1, timeout_seconds: 30 },
    });
    const reply = replyFromRequests(requests);
    let n = 0;
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      n += 1;
      if (n === 1) {
        const err = new Error('socket hang up');
        err.code = 'ECONNRESET';
        throw err;
      }
      return reply(subArgs);
    });
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(calls.length, 2, '瞬时失败必须重试一次');
    const files = attemptFiles(selfDir, requests[0].dispatch_id);
    assert.deepEqual(files, ['attempt-0001.json', 'attempt-0002.json'],
      `两次 attempt 必须各自留账（不可覆盖），实际 ${files.join(',')}`);
    const a1 = readAttempt(selfDir, requests[0].dispatch_id, 1);
    const a2 = readAttempt(selfDir, requests[0].dispatch_id, 2);
    assertC12Attribution(a1, 'attempt-1');
    assertC12Attribution(a2, 'attempt-2');
    assert.equal(a1.status, 'failed', '失败 attempt 的结局必须保留');
    assert.equal(a1.error_class, 'transient');
    assert.equal(a1.lifecycle_outcome, 'finished', '失败的 attempt 也要有完整结束状态');
    assert.equal(a2.status, 'ok');
    assert.equal(a2.attempt, 2);
    // 观测口径不得把宿主开始时间写成推理开始时间
    assert.notEqual(a1.host_call_started_at, a2.host_call_started_at);
    const run = readRun(selfDir, batchId);
    assert.equal(run.real_start_count, 2, '真实宿主调用次数应计入重试（2 次）');
    assert.equal(run.peak_observed_host_in_flight, 1);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PAROBS-03
test('PAROBS-03a 单请求批次：serial_reason=single_ready，且不返修正文', async () => {
  const { root, selfDir } = mkProject('single');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs03';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 4 }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.serial_reason_code, 'single_ready');
    assert.equal(res.serial_reason_source, 'observed_request_count');
    const run = readRun(selfDir, batchId);
    assert.equal(run.serial_reason_code, 'single_ready');
    assert.equal(run.peak_observed_host_in_flight, 1, '单请求峰值只能是 1（实测，不是推算 0）');
    assert.equal(run.cache_reuse_count, 0);
    assert.equal(run.real_start_count, 1);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PAROBS-03b 全部缓存命中：reason=cache_reuse，真实 start 计数不增加', async () => {
  const { root, selfDir } = mkProject('reuse');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs04';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const first = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(first.success, true, first.report);
    const run1 = readRun(selfDir, batchId);
    assert.equal(run1.real_start_count, 2);
    const second = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(second.success, true, second.report);
    assert.equal(calls.length, 2, '重放不得再次调用宿主');
    assert.equal(second.serial_reason_code, 'cache_reuse');
    assert.equal(second.real_start_count, 0, '缓存复用不得计入真实 start');
    const run2 = readRun(selfDir, batchId);
    assert.equal(run2.serial_reason_code, 'cache_reuse');
    assert.equal(run2.cache_reuse_count, 2);
    assert.equal(run2.real_start_count, 0);
    assert.equal(run2.request_count, 2);
    assert.equal(run2.peak_observed_host_in_flight, 0,
      '零次真实宿主调用应实测为 0（原因已明确为 cache_reuse，不得伪造成并发实测）');
    assert.equal(run2.cap_respected, true);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PAROBS-03c 显式 safe 档位（并发=1）：reason=explicit_safe，不返修正文', async () => {
  const { root, selfDir } = mkProject('safe1');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs05';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const reply = replyFromRequests(requests);
    let active = 0;
    let peak = 0;
    const tool = await loadTool(calls, async (subArgs) => {
      active += 1;
      peak = Math.max(peak, active);
      await sleep(20);
      active -= 1;
      return reply(subArgs);
    });
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.serial_reason_code, 'explicit_safe');
    assert.match(String(res.serial_reason_source), /^(policy_profile:safe|config:|tool:|frozen:)/);
    assert.equal(peak, 1, 'safe 档位并发为 1');
    const run = readRun(selfDir, batchId);
    assert.equal(run.serial_reason_code, 'explicit_safe');
    assert.equal(run.peak_observed_host_in_flight, 1);
    assert.equal(run.peak_ready, 2, 'ready 数不得因为并发 1 而被写成 1');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PAROBS-03d 显式配置并发=1：reason 来源指向配置键而非臆测', async () => {
  const { root, selfDir } = mkProject('cfg1', 'agent_max_concurrency: 1\n');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs06';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.serial_reason_code, 'explicit_safe');
    assert.equal(res.serial_reason_source, 'config:agent_max_concurrency');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PAROBS-04
test('PAROBS-04 取消未确认：槽位保持占用、不起新单、晚结果不写', async () => {
  const { root, selfDir } = mkProject('unconfirmed');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs07';
    const requests = writeBatch(selfDir, {
      batchId,
      sections: ['sec1', 'sec2', 'sec3'],
      extraRequest: { timeout_seconds: 0.2, cancel_grace_ms: 100, max_retries: 0 },
    });
    const reply = replyFromRequests(requests);
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      // 宿主忽略取消信号：300ms 后才返回（必然晚于超时 + 宽限）
      await sleep(900);
      return reply(subArgs);
    });
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(res.success, false, '未确认取消的批次不得视为成功');
    assert.equal(calls.length, 2,
      `槽位未确认释放时不得再起新单（最多 cap=2），实际启动 ${calls.length}`);
    const statuses = res.results.map((r) => r.status);
    assert.deepEqual(statuses.slice(0, 2), ['cancellation_unconfirmed', 'cancellation_unconfirmed'],
      JSON.stringify(statuses));
    assert.equal(statuses[2], 'not_dispatched', JSON.stringify(statuses));
    assert.match(String(res.results[2].error), /host_slot_unconfirmed_hold/);
    assert.equal(res.slots_held_unconfirmed, 2);
    assert.equal(res.undispatched, 1);
    assert.equal(res.cap_respected, true, '峰值不得超过 cap');
    assert.equal(res.peak_observed_host_in_flight, 2);
    // 晚结果一律丢弃
    await sleep(1200);
    for (const rel of ['sec1.tex', 'sec2.tex', 'sec3.tex']) {
      assert.ok(!fs.existsSync(path.join(selfDir, rel)), `晚到的 ${rel} 不得写入`);
    }
    const run = readRun(selfDir, batchId);
    assert.equal(run.slots_held_unconfirmed, 2);
    assert.equal(run.undispatched, 1);
    assert.equal(run.peak_observed_host_in_flight, 2);
    assert.equal(run.cap_respected, true);
    assert.equal(run.serial_reason_code, 'host_queue_or_unknown',
      `cap=2 但峰值未达 2 时必须给出 host_queue_or_unknown 原因，实际 ${run.serial_reason_code}`);
    for (const r of requests.slice(0, 2)) {
      const rec = readAttempt(selfDir, r.dispatch_id, 1);
      assertC12Attribution(rec, r.dispatch_id);
      assert.equal(rec.status, 'cancellation_unconfirmed');
      assert.equal(rec.host_slot, 'held', '未确认取消必须把槽位记为 held');
      assert.equal(rec.lifecycle_outcome, 'unfinished', '未结束事件必须显式记为 unfinished');
      assert.equal(rec.cancel_confirmed, false);
    }
    assert.equal(attemptFiles(selfDir, requests[2].dispatch_id).length, 0,
      '未派发的单不得留下伪 attempt 账');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PAROBS-05
test('PAROBS-05 观测口径：宿主窗口与供应商/推理时间严格分离', async () => {
  const { root, selfDir } = mkProject('scope');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs08';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(res.success, true, res.report);
    for (const r of requests) {
      const rec = readAttempt(selfDir, r.dispatch_id, 1);
      assertC12Attribution(rec, r.dispatch_id);
      // 时长必须用单调时钟，且必须标明时钟类型
      assert.equal(rec.elapsed_clock, 'monotonic_ms');
      assert.ok(typeof rec.elapsed_ms === 'number' && rec.elapsed_ms >= 0);
      // 宿主窗口 => UTC 时点可对时序，但不得声称是推理时间
      assert.match(String(rec.host_call_started_at), /^\d{4}-\d{2}-\d{2}T/);
      assert.match(String(rec.host_call_finished_at), /^\d{4}-\d{2}-\d{2}T/);
      assert.match(String(rec.queued_at), /^\d{4}-\d{2}-\d{2}T/);
    }
    const run = readRun(selfDir, batchId);
    assert.equal(run.observed_scope, 'host_call');
    assert.equal(run.elapsed_clock, 'monotonic_ms');
    assert.equal(run.inference_ms, null);
    assert.equal(run.vendor_queue_ms, null);
    assert.equal(run.vendor_first_token_ms, null);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PAROBS-06
test('PAROBS-06 批摘要单点写入：lane 不互相覆盖，换请求集后指纹变化', async () => {
  const { root, selfDir } = mkProject('summary');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-0bs0bs09';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2', 'sec3'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 3 }, {});
    assert.equal(res.success, true, res.report);
    const runDir = path.join(selfDir, '.pd', 'dispatch-runs');
    const files = fs.readdirSync(runDir).filter((n) => n.endsWith('.json'));
    assert.deepEqual(files, [`${batchId}.json`],
      `批摘要只允许一个文件（单协调者写），实际 ${files.join(',')}`);
    const first = readRun(selfDir, batchId);
    assert.equal(typeof first.batch_fingerprint, 'string');
    assert.match(first.batch_fingerprint, /^[0-9a-f]{64}$/);
    assert.equal(first.fingerprint_algorithm, 'sha256');
    // 同一 batch_id 换成另一个请求集（模拟还原/旧批残留）
    const sections2 = ['sec1', 'sec2'];
    const requests2 = writeBatch(selfDir, { batchId, sections: sections2, idPrefix: 'dsp2' });
    const calls2 = [];
    const tool2 = await loadTool(calls2, replyFromRequests(requests2));
    const res2 = await tool2.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(res2.success, true, res2.report);
    const second = readRun(selfDir, batchId);
    assert.deepEqual(second.request_ids, requests2.map((r) => r.dispatch_id));
    assert.notEqual(second.batch_fingerprint, first.batch_fingerprint,
      '换请求集后指纹必须变化，否则还原会把旧批峰值套到新批');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});
