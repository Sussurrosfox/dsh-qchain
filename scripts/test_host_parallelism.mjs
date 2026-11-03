/**
 * 任务卡 22（C12.5 第 ①/② 层）：真实批工具入口 + 真实宿主调度路径的并发验收。
 *
 * 这一层证明的**只有**「宿主请求窗口重叠」：
 *   - 用两个**独立阻塞 stub**（barrier 控制释放）证明第二单在第一单结束前启动，
 *     而不是统计 Promise 数量或产物文件数；
 *   - 用真实 `qchain_dispatch` 工具入口（不是池函数直调）跑整批，
 *     核对 lane 归属、claim、receipt、批摘要与峰值；
 *   - 供应商排队 / 首 token / 模型推理时间**一律 null**：本环境观测不到，
 *     未知就是未知，不许写 0，也不许把宿主调用窗口说成推理并发。
 *
 * 真实模型供应商侧的推理并发与真实模型样本耗时属于第 ③ 层，需外部授权，
 * 本文件**不**声称、不代跑（见 repair-work/evidence/concurrency-acceptance.json）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fsDefault from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

function w(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text, 'utf8');
}
const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

/** 临时项目（系统临时目录，绝不写项目/学案目录）。 */
function mkProject(name, configYaml) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `qchain-t22-${name}-`));
  const selfDir = path.join(root, 'self');
  fs.mkdirSync(path.join(selfDir, '.pd', 'agent-requests'), { recursive: true });
  w(path.join(selfDir, 'config.yaml'),
    configYaml || 'agent_dispatch: fast\nagent_max_concurrency: 4\nagent_batch_window: 2\n');
  return { root, selfDir };
}

/** 写保护：任何越界写都被拦下并记录（保证测试不污染真实项目）。 */
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
      const targets = (name === 'renameSync' || name === 'copyFileSync')
        ? [args[0], args[1]] : [args[0]];
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

/** 真实 index.js 的工具入口（subagent 提供方用可控 stub 顶替）。 */
async function loadTool(subagentCalls, replyFn) {
  const tools = {};
  const ctx = {
    tools: {
      register(def) { tools[def.name] = def; },
      get(name) {
        if (name !== 'subagent') return undefined;
        return { execute: async (subArgs, exec) => {
          subagentCalls.push(subArgs);
          return replyFn(subArgs, exec);
        } };
      },
    },
    get() { return undefined; },
  };
  const mod = await import(pathToFileURL(path.join(ROOT, 'index.js')).href);
  mod.apply(ctx, { projectRoot: ROOT });
  return tools.qchain_dispatch;
}

const fileBlocks = (arts) =>
  arts.map((a) => `<<<FILE:${a}>>>\n% content of ${a}\n<<<END:${a}>>>`).join('\n');
const MARK_RE = /MARK-([A-Za-z0-9_-]+)/;
const markOf = (subArgs) => MARK_RE.exec(String(subArgs.prompt || ''))[1];

/**
 * 批次夹具：与 Python 侧 `_disk_plan` + `write_batch_index` 同 schema 的
 * 四节两批作者批次（每单 2 个相邻节，schema_version=2 + scopes/artifact_scopes）。
 */
function writeAuthorBatch(selfDir, { batchId = 'bat-t2-aabbccdd', sections = ['sec1', 'sec2', 'sec3', 'sec4'] } = {}) {
  const groups = [sections.slice(0, 2), sections.slice(2)];
  const requests = groups.map((group, i) => ({
    schema_version: 2,
    dispatch_id: `dsp-t2-${group.join('-')}-${i}${i}`.replace(/-sec/g, '-sec'),
    batch_id: batchId,
    run_id: 'run-22',
    stage: `T2_SEC${i + 1}`,
    role: 'author',
    prompt_file: 'subagent_prompt.md',
    artifacts: group.map((s) => `${s}.tex`),
    scope: group[0],
    scopes: group,
    artifact_scopes: Object.fromEntries(group.map((s) => [`${s}.tex`, s])),
    instruction: `MARK-${group.join('_')} 写作 ${group.join('、')}`,
    command: 'true',
    before: {},
    status: 'pending',
    max_retries: 0,
    context: { section_ids: group },
  }));
  const plan = {
    batch_id: batchId,
    stage: 'T2',
    sections,
    required_scope_count: sections.length,
    schema_version: 2,
    dispatch_contract_version: 2,
    max_concurrency: 4,
    effective_concurrency: 4,
    executor_kind: 'host_subagent',
    capability_verified: false,
    worker_max_concurrency: null,
    requests: requests.map((r) => ({
      dispatch_id: r.dispatch_id, batch_id: batchId, scope: r.scope,
      scopes: r.scopes, artifact_scopes: r.artifact_scopes,
      role: r.role, artifacts: r.artifacts, status: 'pending',
    })),
  };
  const planRel = '.pd/dispatch-plan.json';
  w(path.join(selfDir, planRel), JSON.stringify(plan, null, 2));
  for (const r of requests) {
    w(path.join(selfDir, '.pd', 'agent-requests', `${r.dispatch_id}.json`),
      JSON.stringify(r, null, 2));
  }
  const index = {
    schema_version: 1,
    batch_id: batchId,
    run_id: 'run-22',
    stage: 'T2',
    plan_rel: planRel,
    plan_digest: sha256(fs.readFileSync(path.join(selfDir, planRel))),
    request_ids: requests.map((r) => r.dispatch_id),
    request_refs: requests.map((r) => `.pd/agent-requests/${r.dispatch_id}.json`),
    sections,
    required_scope_count: sections.length,
    dispatch_contract_version: 2,
  };
  w(path.join(selfDir, '.pd', 'dispatch-batches', `${batchId}.json`),
    JSON.stringify(index, null, 2));
  return requests;
}

const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const readRun = (selfDir, batchId) =>
  readJson(path.join(selfDir, '.pd', 'dispatch-runs', `${batchId}.json`));
const claimOf = (selfDir, dispatchId) =>
  readJson(path.join(selfDir, '.pd', 'dispatch-claims', `${dispatchId}.json`));

/** 宿主侧事件账：start/end 都带单调时钟，用于判「是否真重叠」。 */
function hostEventLedger() {
  const events = [];
  const t0 = process.hrtime.bigint();
  const now = () => Number(process.hrtime.bigint() - t0) / 1e6;
  return {
    events,
    push(kind, id, extra) { events.push({ kind, id, at_ms: now(), ...(extra || {}) }); },
    starts() { return events.filter((e) => e.kind === 'start'); },
    ends() { return events.filter((e) => e.kind === 'end'); },
    /** 真实重叠：存在一单的 start 严格早于另一单的 end 且两单不串行。 */
    overlapPairs() {
      const pairs = [];
      for (const a of this.starts()) {
        for (const b of this.starts()) {
          if (a.id >= b.id) continue;
          const endA = this.ends().find((e) => e.id === a.id);
          const endB = this.ends().find((e) => e.id === b.id);
          const overlap = endA && endB
            && a.at_ms < endB.at_ms && b.at_ms < endA.at_ms;
          if (overlap) pairs.push([a.id, b.id]);
        }
      }
      return pairs;
    },
  };
}

/** 双单 barrier：两单都到达后才一起释放（证明 cap 允许时是并发而非串行）。 */
function makeBarrier(expected) {
  let arrived = 0;
  let release;
  const gate = new Promise((r) => { release = r; });
  return async () => {
    arrived += 1;
    if (arrived >= expected) release();
    await gate;
  };
}

// ---------------------------------------------------------------- HOSTPAR-01
test('HOSTPAR-01 四节两批作者：cap=4 时两个 writer 宿主请求真实重叠（barrier 双阻塞）', async () => {
  const { root, selfDir } = mkProject('overlap');
  const guard = installWriteGuard(root);
  try {
    const requests = writeAuthorBatch(selfDir);
    const ledger = hostEventLedger();
    const barrier = makeBarrier(requests.length);
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const mark = markOf(subArgs);
      ledger.push('start', mark);
      await barrier();
      ledger.push('end', mark);
      const req = requests.find((r) => r.instruction.includes(mark));
      return { runId: `run-${mark}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
    });

    const res = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });

    assert.equal(res.dispatched, 2, '四节应合成 2 个作者请求（每单 2 相邻节）');
    assert.equal(res.ok, 2, `两单都应成功: ${JSON.stringify(res.results?.map((r) => r.status))}`);
    assert.equal(calls.length, 2, '两次宿主调用（不许多派）');

    // ① 活动时间重叠：第二单在第一单结束前启动（不是 Promise 计数）
    const pairs = ledger.overlapPairs();
    assert.equal(pairs.length, 1,
      `必须证明两个宿主请求窗口重叠，实际事件: ${JSON.stringify(ledger.events)}`);
    const [first, second] = [ledger.starts()[0], ledger.starts()[1]];
    assert.ok(second.at_ms <= ledger.ends()[0].at_ms,
      `第二单 start(${second.at_ms}ms) 必须早于第一单 end(${ledger.ends()[0].at_ms}ms)`);
    assert.ok(first.at_ms < second.at_ms, '两单不得是同一时刻的重复记账');

    // ② 峰值与 cap：peak=2、cap=4，未越界
    assert.equal(res.peak_observed_host_in_flight, 2, '宿主调用峰值应为 2');
    assert.equal(res.cap_respected, true, '峰值不得越过 cap');
    assert.equal(res.effective_concurrency, 4, '执行并发槽位取配置 4');
    assert.equal(res.executor_kind, 'host_subagent');

    // ③ 能力未核实：未知记 null/false，不许伪装成实测 0
    assert.equal(res.capability_verified, false, '宿主能力未核实时不得宣称已核实');
    assert.equal(res.worker_max_concurrency, null, '未知宿主能力必须是 null 而不是 0');

    // ④ 产物 + claim + receipt：两单各自落 2 节，无跨域覆盖
    for (const sec of ['sec1', 'sec2', 'sec3', 'sec4']) {
      assert.ok(fs.existsSync(path.join(selfDir, `${sec}.tex`)), `${sec}.tex 必须落盘`);
    }
    for (const req of requests) {
      const claim = claimOf(selfDir, req.dispatch_id);
      assert.equal(claim.status, 'done', `${req.dispatch_id} 认领应完成`);
      assert.ok(claim.receipt_rel, `${req.dispatch_id} 认领须挂回执`);
      const attempt = readJson(path.join(
        selfDir, '.pd', 'agent-attempts', req.dispatch_id, 'attempt-0001.json'));
      assert.equal(attempt.observation_scope, 'host_call');
      assert.equal(attempt.batch_id, 'bat-t2-aabbccdd');
      assert.equal(attempt.attempt, 1);
      assert.deepEqual(attempt.scopes, req.scopes);
      assert.ok(attempt.lane && Number.isInteger(attempt.lane.index), '缺 lane 归属');
    }
    // 两条 lane 各自记账（同一请求不得被两 lane 重复执行）
    const lanes = new Set(requests.map((req) => readJson(path.join(
      selfDir, '.pd', 'agent-attempts', req.dispatch_id, 'attempt-0001.json')).lane.index));
    assert.equal(lanes.size, 2, `两条独立 lane，实际 ${[...lanes].join(',')}`);

    // ⑤ 批摘要：峰值/上限/来源齐备
    const run = readRun(selfDir, 'bat-t2-aabbccdd');
    assert.equal(run.peak_observed_host_in_flight, 2);
    assert.equal(run.executor_kind, 'host_subagent');
    assert.equal(run.capability_verified, false);
    assert.ok(run.serial_reason_source, '批摘要必须交代串行原因来源');

    assert.deepEqual(guard.blocked, [], `越界写: ${JSON.stringify(guard.blocked)}`);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- HOSTPAR-02
test('HOSTPAR-02 重复整批调用：receipt 复用、不重复执行、不新增宿主调用', async () => {
  const { root, selfDir } = mkProject('reuse');
  const guard = installWriteGuard(root);
  try {
    const requests = writeAuthorBatch(selfDir);
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const mark = markOf(subArgs);
      const req = requests.find((r) => r.instruction.includes(mark));
      return { runId: `run-${mark}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
    });

    const first = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });
    assert.equal(first.ok, 2);
    assert.equal(calls.length, 2, '首次整批 2 次宿主调用');

    const receipts = requests.map((r) => {
      const p = path.join(selfDir, '.pd', 'agent-calls', `${r.dispatch_id}.json`);
      return fs.existsSync(p) ? sha256(fs.readFileSync(p)) : null;
    });

    for (let i = 0; i < 2; i += 1) {
      const again = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });
      assert.equal(again.ok, 2, `第 ${i + 2} 次调用仍应全 ok（回执复用）`);
      assert.equal(calls.length, 2,
        `第 ${i + 2} 次调用不得再次执行已认证请求（实际宿主调用 ${calls.length}）`);
    }
    // 回执内容不被重写
    const after = requests.map((r) => {
      const p = path.join(selfDir, '.pd', 'agent-calls', `${r.dispatch_id}.json`);
      return fs.existsSync(p) ? sha256(fs.readFileSync(p)) : null;
    });
    assert.deepEqual(after, receipts, '重复调用不得重写既有回执');
    assert.ok(receipts.every(Boolean), '两单都必须有回执');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- HOSTPAR-03
test('HOSTPAR-03 部分成功：失败单不牵连成功单，也不跨域覆盖已完成产物', async () => {
  const { root, selfDir } = mkProject('partial');
  const guard = installWriteGuard(root);
  try {
    const requests = writeAuthorBatch(selfDir);
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const mark = markOf(subArgs);
      const req = requests.find((r) => r.instruction.includes(mark));
      if (req.scopes.includes('sec3')) {
        // 第二单：只回话不落产物（模拟宿主返回但漏写）
        return { runId: `run-${mark}`, output: [{ type: 'text', text: '（未写文件）' }] };
      }
      return { runId: `run-${mark}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
    });

    const res = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });
    assert.equal(res.ok, 1, `一单成功: ${JSON.stringify(res.results?.map((r) => [r.dispatch_id, r.status]))}`);
    assert.equal(res.failed, 1, '一单失败');
    assert.equal(res.success, false, '整批不得宣称成功');

    assert.ok(fs.existsSync(path.join(selfDir, 'sec1.tex')));
    assert.ok(fs.existsSync(path.join(selfDir, 'sec2.tex')));
    assert.ok(!fs.existsSync(path.join(selfDir, 'sec3.tex')), '失败单不得留下伪产物');
    assert.ok(!fs.existsSync(path.join(selfDir, 'sec4.tex')));

    const okReq = requests.find((r) => r.scopes.includes('sec1'));
    const badReq = requests.find((r) => r.scopes.includes('sec3'));
    assert.equal(claimOf(selfDir, okReq.dispatch_id).status, 'done');
    assert.equal(claimOf(selfDir, badReq.dispatch_id).status, 'failed');
    // 已完成单的回执仍在（失败单不得覆盖/删除）
    assert.ok(fs.existsSync(path.join(selfDir, '.pd', 'agent-calls',
      `${okReq.dispatch_id}.json`)), '成功单回执必须保留');
    // 失败单可以留下「失败尝试」的账（审计需要），但绝不是可认证的回执
    const badCall = path.join(selfDir, '.pd', 'agent-calls', `${badReq.dispatch_id}.json`);
    if (fs.existsSync(badCall)) {
      const rec = readJson(badCall);
      assert.notEqual(rec.status, 'ok', '失败单的回执状态不得记为 ok');
      assert.deepEqual(rec.artifacts_written || [], [],
        '失败单的回执不得声称写入过产物');
    }
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- HOSTPAR-04
test('HOSTPAR-04 显式 safe 档位：串行且原因可解释；峰值永不超过 cap', async () => {
  const { root, selfDir } = mkProject('safe', 'agent_dispatch: safe\nagent_max_concurrency: 0\nagent_batch_window: 2\n');
  const guard = installWriteGuard(root);
  try {
    const requests = writeAuthorBatch(selfDir);
    const ledger = hostEventLedger();
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const mark = markOf(subArgs);
      ledger.push('start', mark);
      await new Promise((r) => setTimeout(r, 5));
      ledger.push('end', mark);
      const req = requests.find((r) => r.instruction.includes(mark));
      return { runId: `run-${mark}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
    });

    const res = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });
    assert.equal(res.ok, 2);
    assert.equal(res.effective_concurrency, 1, '显式 safe 档位执行并发为 1');
    assert.equal(res.peak_observed_host_in_flight, 1, '串行档位峰值只能是 1');
    assert.equal(ledger.overlapPairs().length, 0, 'safe 档位不得出现窗口重叠');
    assert.ok(String(res.serial_reason || '').length > 0 || String(res.serial_reason_code || '').length > 0,
      '串行必须给出可解释原因（不得只留空字段）');
    assert.equal(res.capability_verified, false);
    assert.equal(res.worker_max_concurrency, null);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- HOSTPAR-05
test('HOSTPAR-05 三层口径边界：宿主窗口 ≠ 供应商推理，未知一律 null', async () => {
  const { root, selfDir } = mkProject('layers');
  const guard = installWriteGuard(root);
  try {
    const requests = writeAuthorBatch(selfDir);
    const barrier = makeBarrier(requests.length);
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const mark = markOf(subArgs);
      await barrier();
      const req = requests.find((r) => r.instruction.includes(mark));
      return { runId: `run-${mark}`, output: [{ type: 'text', text: fileBlocks(req.artifacts) }] };
    });

    const res = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-aabbccdd' });
    assert.equal(res.ok, 2);

    // 宿主层：可观测（有明确 scope 与单调时钟）
    assert.equal(res.observed_scope, 'host_call', '观测口径必须显式标注为宿主调用');
    assert.equal(typeof res.peak_observed_host_in_flight, 'number');

    // 供应商/推理层：本环境观测不到 → null（不是 0，也不是宿主时间的复用）
    assert.equal(res.vendor_queue_ms, null, '供应商排队时间未知必须 null');
    assert.equal(res.inference_ms, null, '模型推理时间未知必须 null');
    for (const req of requests) {
      const attempt = readJson(path.join(
        selfDir, '.pd', 'agent-attempts', req.dispatch_id, 'attempt-0001.json'));
      assert.equal(attempt.inference_started_at, null);
      assert.equal(attempt.vendor_queue_ms, null);
      assert.equal(attempt.vendor_first_token_ms, null);
      assert.equal(attempt.vendor_inference_ms, null);
      assert.equal(attempt.elapsed_clock, 'monotonic_ms');
      // 宿主时间字段必须是真实时间戳，且 finished >= started
      assert.ok(attempt.host_call_started_at && attempt.host_call_finished_at);
      assert.ok(String(attempt.host_call_finished_at) >= String(attempt.host_call_started_at));
    }

    const run = readRun(selfDir, 'bat-t2-aabbccdd');
    assert.equal(run.observed_scope, 'host_call');
    assert.equal(run.capability_verified, false,
      '真实宿主并发能力未核实 → 只能记 false/null，不得宣称已验证');
    // 已知缺口（记入 remaining，不在本卡越界改 index.js）：批摘要未落
    // worker_max_concurrency 键；一旦落盘必须是 null（未知），不得写 0。
    assert.ok(run.worker_max_concurrency === undefined || run.worker_max_concurrency === null,
      `批摘要里的未知宿主能力只能缺省或 null，实际 ${JSON.stringify(run.worker_max_concurrency)}`);
    assert.ok(run.vendor_queue_ms === null && run.inference_ms === null,
      '批摘要不得凭空填供应商/推理耗时');
    assert.ok(run.serial_reason !== undefined || run.serial_reason_source,
      '未知并发必须留下可解释来源');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});
