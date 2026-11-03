// scripts/test_dispatch_batch_selection.mjs — 任务卡20：批次入口与并发池回归测试
// 运行：node --test scripts/test_dispatch_batch_selection.mjs（插件根目录）
//
// 覆盖（对照 repair-plan/tasks/20.md 验收矩阵）：
//   PARSEL-01  T2_WRITE / T2_CHECK(语义) / T4 三类批次索引均可按 batch_id 一次取全
//   PARSEL-02  no-arg 多批/错批/缺请求/索引冲突 一律显式拒绝，绝不扫描全部历史 pending
//   PARSEL-04  两个阻塞 stub + cap>=2：第二个必须在第一个释放前启动；cap=1 不得重叠
//   PARSEL-05  8 请求 cap4：观测峰值恰为 4、不超限、槽位结束立即补位
//   PARSEL-06  同一 dispatch_id 并发两次最多执行一次；已完成重放核对收据后复用
//   PARSEL-07  乱序完成只写各自候选域；request_ref+batch_id 同时给出被拒；单请求返修仍可用
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

function mkProject(name) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `qchain-t20-${name}-`));
  const selfDir = path.join(root, 'self');
  fs.mkdirSync(path.join(selfDir, '.pd', 'agent-requests'), { recursive: true });
  // 派发配置：safe 档位，显式并发由 tool override 提供
  w(path.join(selfDir, 'config.yaml'),
    'agent_dispatch: safe\nagent_max_concurrency: 0\nagent_batch_window: 2\n');
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

/** 批次夹具：写计划文件 + 批次索引 + 各派发单（与 Python 侧同 schema）。 */
function writeBatch(selfDir, {
  batchId, stage = 'T2', sections, planRel = '.pd/dispatch-plan.json',
  runId = 'run-1', writeIndex = true, digestOverride = null, idPrefix = 'dsp',
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
    const digest = digestOverride
      || sha256(fs.readFileSync(path.join(selfDir, planRel)));
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
    const m = MARK_RE.exec(String(subArgs.prompt || ''));
    assert.ok(m, '提示词里必须能取到 MARK 标记');
    const req = byId.get(m[1]);
    assert.ok(req, `未知 marker: ${m[1]}`);
    return {
      runId: `run-${m[1]}`,
      output: [{ type: 'text', text: fileBlocks(req.artifacts) }],
    };
  };
}

// ---------------------------------------------------------------- PARSEL-01
test('PARSEL-01a T2_WRITE：batch_id 一次取全同批请求（索引优先）', async () => {
  const { root, selfDir } = mkProject('sel-t2');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-aaaa1111';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2', 'sec3'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.dispatched, 3);
    assert.equal(res.ok, 3);
    assert.equal(calls.length, 3, '一次调用必须派发整批三条请求');
    assert.equal(res.batch_size, 3);
    for (const name of ['sec1.tex', 'sec2.tex', 'sec3.tex']) {
      assert.ok(fs.existsSync(path.join(selfDir, name)), `${name} 应已写出`);
    }
    assert.deepEqual(guard.blocked, [], '不得有越界写入');
  } finally {
    guard.restore();
  }
});

test('PARSEL-01b 语义审读批次：计划在 dispatch-plan-semantic.json 也不漏读', async () => {
  const { root, selfDir } = mkProject('sel-sem');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-check-bbbb2222';
    const requests = writeBatch(selfDir, {
      batchId, stage: 'T2_CHECK', sections: ['sec1', 'sec2'],
      planRel: '.pd/dispatch-plan-semantic.json', idPrefix: 'chk',
    });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.dispatched, 2, '语义计划里的请求不得漏读');
    assert.equal(calls.length, 2);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-01c 旧项目兼容：无索引但语义计划里有该 batch_id 仍可解析', async () => {
  const { root, selfDir } = mkProject('sel-legacy');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-check-cccc3333';
    const requests = writeBatch(selfDir, {
      batchId, stage: 'T2_CHECK', sections: ['sec1', 'sec2'],
      planRel: '.pd/dispatch-plan-semantic.json', idPrefix: 'chk', writeIndex: false,
    });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.dispatched, 2);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-01d T4 验收批次：batch_id 可解析（索引 stage=T4）', async () => {
  const { root, selfDir } = mkProject('sel-t4');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t4-dddd4444';
    const requests = writeBatch(selfDir, { batchId, stage: 'T4', sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.dispatched, 2);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PARSEL-02
test('PARSEL-02a batch_id 与 request_ref 同时给出 → 拒绝（互斥）', async () => {
  const { root, selfDir } = mkProject('sel-mutex');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-eeee5555';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({
      self_dir: selfDir,
      batch_id: batchId,
      request_ref: `.pd/agent-requests/${requests[0].dispatch_id}.json`,
    }, {});
    assert.equal(res.success, false);
    assert.match(res.report, /BATCH_AND_REQUEST_CONFLICT/);
    assert.equal(calls.length, 0, '参数冲突时不得调用子 Agent');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-02b no-arg 且存在多个批次 → 歧义拒绝，绝不合并历史 pending', async () => {
  const { root, selfDir } = mkProject('sel-ambig');
  const guard = installWriteGuard(root);
  try {
    const batchA = 'bat-t2-ffff6666';
    const batchB = 'bat-t2-ffff7777';
    const reqA = writeBatch(selfDir, { batchId: batchA, sections: ['sec1'] });
    const reqB = writeBatch(selfDir, { batchId: batchB, sections: ['sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests([...reqA, ...reqB]));
    const res = await tool.execute({ self_dir: selfDir }, {});
    assert.equal(res.success, false);
    assert.match(res.report, /AMBIGUOUS_BATCH/);
    assert.equal(calls.length, 0);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-02c no-arg 且无任何批次 → BATCH_REQUIRED（不再扫描 agent-requests）', async () => {
  const { root, selfDir } = mkProject('sel-noarg');
  const guard = installWriteGuard(root);
  try {
    // 只有裸 pending 派发单，没有计划/索引
    w(path.join(selfDir, '.pd/agent-requests/dsp-orphan.json'), JSON.stringify({
      dispatch_id: 'dsp-orphan', stage: 'T2_SEC1', role: 'author',
      artifacts: ['sec1.tex'], instruction: 'MARK-dsp-orphan', command: 'true',
      before: {}, status: 'pending', batch_id: 'bat-t2-orphan',
    }, null, 2));
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests([]));
    const res = await tool.execute({ self_dir: selfDir }, {});
    assert.equal(res.success, false);
    assert.match(res.report, /BATCH_REQUIRED/);
    assert.equal(calls.length, 0, '不得扫描全部历史 pending 强行派发');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-02d 错批次号 → BATCH_NOT_FOUND；缺派发单 → BATCH_REQUEST_MISSING', async () => {
  const { root, selfDir } = mkProject('sel-badbatch');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-9999aaaa';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));

    const wrong = await tool.execute({ self_dir: selfDir, batch_id: 'bat-t2-nope' }, {});
    assert.equal(wrong.success, false);
    assert.match(wrong.report, /BATCH_NOT_FOUND/);

    fs.rmSync(path.join(selfDir, `.pd/agent-requests/${requests[1].dispatch_id}.json`));
    const missing = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(missing.success, false);
    assert.match(missing.report, /BATCH_REQUEST_MISSING/);
    assert.equal(calls.length, 0, '完整性校验失败时不得调用子 Agent');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-02e 计划摘要不符（过期索引）→ STALE_OR_AMBIGUOUS_BATCH', async () => {
  const { root, selfDir } = mkProject('sel-stale');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-8888bbbb';
    const requests = writeBatch(selfDir, {
      batchId, sections: ['sec1'], digestOverride: 'deadbeef'.repeat(8),
    });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, false);
    assert.match(res.report, /STALE_OR_AMBIGUOUS_BATCH/);
    assert.equal(calls.length, 0);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-02f 派发单 batch_id 与所选批次不一致 → BATCH_REQUEST_INCONSISTENT', async () => {
  const { root, selfDir } = mkProject('sel-incons');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-7777cccc';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const bad = { ...requests[0], batch_id: 'bat-t2-other' };
    w(path.join(selfDir, `.pd/agent-requests/${bad.dispatch_id}.json`), JSON.stringify(bad, null, 2));
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId }, {});
    assert.equal(res.success, false);
    assert.match(res.report, /BATCH_REQUEST_INCONSISTENT/);
    assert.equal(calls.length, 0);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PARSEL-04
test('PARSEL-04a 屏障：cap>=2 时第二个请求在第一个释放前已启动', async () => {
  const { root, selfDir } = mkProject('barrier2');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-ba111111';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const reply = replyFromRequests(requests);
    const started = [];
    const gates = new Map();
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const id = MARK_RE.exec(String(subArgs.prompt || ''))[1];
      started.push(id);
      await new Promise((res) => gates.set(id, res));
      return reply(subArgs);
    });
    const p = tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    await waitFor(() => started.length >= 2, '两个请求都应已启动');
    // 此刻第一个仍未释放 → 证明第二任务在第一任务结束前启动（真屏障）
    assert.equal(started.length, 2);
    for (const id of started) gates.get(id)();
    const res = await p;
    assert.equal(res.success, true, res.report);
    assert.equal(res.ok, 2);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-04b cap=1 时两个请求不得重叠启动', async () => {
  const { root, selfDir } = mkProject('barrier1');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-ba222222';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const reply = replyFromRequests(requests);
    const started = [];
    const gates = new Map();
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const id = MARK_RE.exec(String(subArgs.prompt || ''))[1];
      started.push(id);
      await new Promise((res) => gates.set(id, res));
      return reply(subArgs);
    });
    const p = tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 1 }, {});
    await waitFor(() => started.length >= 1, '第一个请求应已启动');
    await sleep(60);
    assert.equal(started.length, 1, 'cap=1 时第二个请求不得与第一个重叠');
    gates.get(started[0])();
    await waitFor(() => started.length >= 2, '释放后第二个请求应补位启动');
    gates.get(started[1])();
    const res = await p;
    assert.equal(res.ok, 2);
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PARSEL-05
test('PARSEL-05 8 请求 cap4：峰值恰为 4、不超限、结束即补位', async () => {
  const { root, selfDir } = mkProject('peak4');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-8a8a8a8a';
    const sections = ['s1', 's2', 's3', 's4', 's5', 's6', 's7', 's8'];
    const requests = writeBatch(selfDir, { batchId, sections });
    const reply = replyFromRequests(requests);
    let active = 0;
    let peak = 0;
    const started = [];
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const id = MARK_RE.exec(String(subArgs.prompt || ''))[1];
      started.push(id);
      active += 1;
      peak = Math.max(peak, active);
      // 交错耗时：偶数先完成，验证乱序完成时槽位立即补位
      await sleep(id.endsWith('0') || id.endsWith('2') || id.endsWith('4') || id.endsWith('6') ? 20 : 45);
      active -= 1;
      return reply(subArgs);
    });
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 4 }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.ok, 8);
    assert.equal(calls.length, 8);
    assert.equal(peak, 4, `观测并发峰值应为 4，实际 ${peak}`);
    assert.ok(peak <= 4, '并发不得超过配置槽位');
    assert.equal(new Set(started).size, 8, '8 个请求都必须被启动（槽位补位）');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PARSEL-06
test('PARSEL-06a 同一 dispatch_id 并发两次：至多执行一次（原子认领）', async () => {
  const { root, selfDir } = mkProject('claim');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-c1a1c1a1';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const reply = replyFromRequests(requests);
    let active = 0;
    const gates = new Map();
    const calls = [];
    const tool = await loadTool(calls, async (subArgs) => {
      const id = MARK_RE.exec(String(subArgs.prompt || ''))[1];
      active += 1;
      await new Promise((res) => gates.set(id, res));
      active -= 1;
      return reply(subArgs);
    });
    const p1 = tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    await waitFor(() => gates.size >= 2, '第一轮应已启动两个请求');
    const p2 = tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    // 让第二次调用走到认领点
    await sleep(150);
    assert.equal(calls.length, 2, '第二次并发调用不得新增子 Agent 调用');
    for (const id of [...gates.keys()]) gates.get(id)();
    const [r1, r2] = await Promise.all([p1, p2]);
    assert.equal(r1.success, true, r1.report);
    assert.equal(calls.length, 2, '每个 dispatch_id 至多执行一次（并发第二次命中认领）');
    // 第二轮要么命中 active 认领报 DISPATCH_BUSY，要么（第一轮已落账）核对收据后复用；
    // 两者都满足「至多执行一次」，绝不允许再次真正执行并重复写。
    assert.ok(r2.results.every((x) => x.status === 'dispatch_busy' || (x.status === 'ok' && x.reused === true)),
      JSON.stringify(r2.results.map((x) => [x.dispatch_id, x.status, x.reused])));
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-06c 命中有效 active 认领（同主机存活 pid）→ 显式 DISPATCH_BUSY，不重复执行', async () => {
  const { root, selfDir } = mkProject('busy');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-b0b0b0b0';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    // 预置一个「本机存活进程」持有的 active 认领 → 第一条必被判忙
    w(path.join(selfDir, `.pd/dispatch-claims/${requests[0].dispatch_id}.json`), JSON.stringify({
      claim_version: 1, dispatch_id: requests[0].dispatch_id,
      batch_id: batchId, run_id: 'run-1', status: 'active',
      host: os.hostname(), pid: process.pid,
      claimed_at: new Date().toISOString(), heartbeat_at: new Date().toISOString(),
      attempt: 0, receipt_rel: '', output_digests: {},
    }, null, 2));
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(res.success, false, '含忙派发单时整体不得视为全部成功');
    const busy = res.results.find((x) => x.dispatch_id === requests[0].dispatch_id);
    assert.equal(busy.status, 'dispatch_busy');
    assert.match(String(busy.error), /DISPATCH_BUSY/);
    // 只应为第二条（无认领）调用子 Agent
    assert.equal(calls.length, 1, '被 active 认领的派发单不得再执行');
    assert.ok(fs.existsSync(path.join(selfDir, 'sec2.tex')));
    assert.ok(!fs.existsSync(path.join(selfDir, 'sec1.tex')), '被认领的派发单不得写产物');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-06b 已完成批次重放：核对收据与摘要后复用，不再调用子 Agent', async () => {
  const { root, selfDir } = mkProject('replay');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-c2b2c2b2';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const first = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(first.success, true, first.report);
    assert.equal(calls.length, 2);
    const second = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 2 }, {});
    assert.equal(second.success, true, second.report);
    assert.equal(calls.length, 2, '重放不得再次调用子 Agent');
    assert.ok(second.results.every((x) => x.status === 'ok'), JSON.stringify(second.results.map((x) => x.status)));
    assert.ok(second.results.every((x) => x.reused === true), '重放结果必须标记 reused');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

// ---------------------------------------------------------------- PARSEL-07
test('PARSEL-07a 乱序完成：各自只写自己的候选域（无串写）', async () => {
  const { root, selfDir } = mkProject('doms');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-d3d3d3d3';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2', 'sec3'] });
    const reply = replyFromRequests(requests);
    const calls = [];
    const delays = new Map([[requests[0].dispatch_id, 60], [requests[1].dispatch_id, 15], [requests[2].dispatch_id, 35]]);
    const tool = await loadTool(calls, async (subArgs) => {
      const id = MARK_RE.exec(String(subArgs.prompt || ''))[1];
      await sleep(delays.get(id) || 10);
      return reply(subArgs);
    });
    const res = await tool.execute({ self_dir: selfDir, batch_id: batchId, max_concurrency: 3 }, {});
    assert.equal(res.success, true, res.report);
    for (const [i, sec] of ['sec1', 'sec2', 'sec3'].entries()) {
      const text = fs.readFileSync(path.join(selfDir, `${sec}.tex`), 'utf8');
      assert.ok(text.includes(`${sec}.tex`), `${sec}.tex 内容应属于自己`);
      assert.ok(!text.includes(`sec${((i + 1) % 3) + 1}.tex`), `${sec}.tex 不得串写其他小节`);
    }
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});

test('PARSEL-07b 单请求返修入口仍可用（request_ref 只派发这一份）', async () => {
  const { root, selfDir } = mkProject('single');
  const guard = installWriteGuard(root);
  try {
    const batchId = 'bat-t2-e4e4e4e4';
    const requests = writeBatch(selfDir, { batchId, sections: ['sec1', 'sec2'] });
    const calls = [];
    const tool = await loadTool(calls, replyFromRequests(requests));
    const rel = `.pd/agent-requests/${requests[1].dispatch_id}.json`;
    const res = await tool.execute({ self_dir: selfDir, request_ref: rel }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(res.dispatched, 1);
    assert.equal(calls.length, 1);
    assert.ok(fs.existsSync(path.join(selfDir, 'sec2.tex')));
    assert.ok(!fs.existsSync(path.join(selfDir, 'sec1.tex')), '单请求返修不得顺手写其他小节');
    assert.deepEqual(guard.blocked, []);
  } finally {
    guard.restore();
  }
});
