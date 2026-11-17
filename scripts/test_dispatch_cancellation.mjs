// scripts/test_dispatch_cancellation.mjs — 任务卡 09：可取消超时 / 错误分型 / 逐 attempt 台账
// 运行：node --test scripts/test_dispatch_runtime.mjs scripts/test_dispatch_cancellation.mjs
//
// 约定（与卡 08 测试一致）：
// - 单元测试直接调用生产 helper（scripts/dispatch_attempts.mjs）。
// - E2E 测试通过真实 index.js 的 apply(ctx) 取得真实 qchain_dispatch 工具，仅替换宿主
//   subagent，并分别覆盖三个真实适配分支：ctx.tools.get('subagent') / ctx.tools.execute
//   / ctx.subagents.start('spawn')。
// - 所有写盘被「写保护」拦截：落在临时目录之外的写入一律记录并丢弃，测试据此断言
//   没有越界写入；测试全程不碰真实项目目录、不启动流水线、不调用真实模型、不读令牌。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fsDefault, * as fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import * as os from 'node:os';
import * as path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

// 模块可能尚不存在（本卡要新增）：用容错导入，让 E2E 用例仍能跑出「真实缺陷」失败原因。
let ATT = null;
try {
  ATT = await import(pathToFileURL(path.join(HERE, 'dispatch_attempts.mjs')).href);
} catch (_) {
  ATT = null;
}
function att() {
  assert.ok(ATT, 'scripts/dispatch_attempts.mjs 缺失：卡 09 必须提供逐 attempt 取消/分型/台账模块');
  return ATT;
}

const HOST_SOURCE = path.join(
  process.env.DSH_CHECKOUT || 'C:\\Users\\63254\\AppData\\Roaming\\npm\\node_modules\\@deepseek-ai\\dsh',
  'node_modules', '@deepseek-ai', 'dsh-tool-subagent', 'lib', 'index.js',
);

// ---------------------------------------------------------------- 夹具工具
function mkProject(name) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `qchain-t09-${name}-`));
  const selfDir = path.join(root, 'self');
  fs.mkdirSync(path.join(selfDir, '.pd', 'agent-requests'), { recursive: true });
  return { root, selfDir };
}
function w(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text, 'utf8');
}
function rmrf(p) { try { fs.rmSync(p, { recursive: true, force: true }); } catch (_) { /* ignore */ } }
const delay = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/** 给被测行为加硬上限：修复前「超时只是停止等待」会挂死，这里把它变成可读的失败。 */
function withDeadline(promise, ms, message) {
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, guard]).finally(() => { if (timer) clearTimeout(timer); });
}

/** 记录并拦截落在 allowedRoot 之外的写入（不真正执行）。 */
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
      const targets = name === 'renameSync' || name === 'copyFileSync' ? [args[0], args[1]] : [args[0]];
      if (!targets.every(inside)) { blocked.push({ op: name, target: String(targets.find((t) => !inside(t))) }); return undefined; }
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

/** 可观察的假父 signal（EventTarget 形状），用于断言监听清理彻底。 */
function fakeParentSignal() {
  const listeners = [];
  const state = { aborted: false, maxListeners: 0, addCalls: 0, removeCalls: 0 };
  const sig = {
    get aborted() { return state.aborted; },
    get listeners() { return listeners; },
    get state() { return state; },
    addEventListener(type, fn) {
      if (type !== 'abort') return;
      state.addCalls += 1;
      listeners.push(fn);
      state.maxListeners = Math.max(state.maxListeners, listeners.length);
    },
    removeEventListener(type, fn) {
      if (type !== 'abort') return;
      state.removeCalls += 1;
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    fire(reason) {
      state.aborted = true;
      for (const fn of [...listeners]) fn({ type: 'abort', reason });
    },
  };
  return sig;
}

const fileBlocks = (arts) => arts.map((a) => `<<<FILE:${a}>>>\n% content of ${a}\n<<<END:${a}>>>`).join('\n');
const reply = (arts, runId = 'rr') => ({ runId, output: [{ type: 'text', text: fileBlocks(arts) }] });

function writeRequest(selfDir, req) {
  const rel = `.pd/agent-requests/${req.dispatch_id}.json`;
  w(path.join(selfDir, rel), JSON.stringify({
    status: 'pending', max_retries: 0, before: {}, ...req,
  }, null, 2));
  return rel;
}

/**
 * 装载真实 index.js 的 qchain_dispatch 工具，按 mode 提供三个真实适配分支之一：
 *  - 'def'       → ctx.tools.get('subagent').execute(subArgs, exec)
 *  - 'tools'     → ctx.tools.execute({name:'subagent', arguments, signal, agent, callId})
 *  - 'subagents' → ctx.subagents.start('spawn', {label, prompt, parent, signal}) → run.result
 */
async function loadHost(mode, handler) {
  const tools = {};
  const calls = [];
  const record = { mode, calls, disposed: 0 };
  const callHandler = (subArgs, exec) => {
    calls.push({ subArgs, exec, signal: exec && exec.signal });
    return handler(subArgs, exec, record);
  };
  const ctx = {
    tools: {
      register(def) { tools[def.name] = def; },
      get(name) {
        if (mode !== 'def' || name !== 'subagent') return undefined;
        return { execute: (subArgs, exec) => callHandler(subArgs, exec) };
      },
      ...(mode === 'tools'
        ? {
          execute(call) {
            return callHandler(call.arguments, {
              signal: call.signal, agent: call.agent, callId: call.callId,
            });
          },
        }
        : {}),
    },
    get(name) {
      if (mode !== 'subagents' || name !== 'subagents') return undefined;
      return {
        async start(_kind, opts) {
          const text = (opts.prompt || []).map((p) => p.text).join('');
          return {
            result: callHandler({ prompt: text, description: opts.label }, { signal: opts.signal }),
            dispose() { record.disposed += 1; },
          };
        },
      };
    },
  };
  const mod = await import(pathToFileURL(path.join(ROOT, 'index.js')).href);
  mod.apply(ctx, { projectRoot: ROOT });
  return { tool: tools.qchain_dispatch, record };
}

async function runDispatch({ mode, selfDir, req, handler, exec = {} }) {
  const { tool, record } = await loadHost(mode, handler);
  const rel = writeRequest(selfDir, req);
  const res = await tool.execute({ self_dir: selfDir, request_ref: rel }, exec);
  return { res, record, result: res.results[0] };
}

function readLedgers(selfDir, dispatchId) {
  const dir = path.join(selfDir, '.pd', 'agent-attempts', dispatchId);
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir).filter((n) => n.endsWith('.json')).sort()
    .map((name) => ({ file: name, ...JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8')) }));
}
function readSummary(selfDir, dispatchId) {
  const p = path.join(selfDir, '.pd', 'agent-calls', `${dispatchId}.json`);
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const baseReq = (id, extra = {}) => ({
  dispatch_id: id,
  stage: 'T2_SEC1',
  role: 'author',
  prompt_file: 'prompts/subagent_prompt.md',
  artifacts: ['sec1.tex'],
  instruction: 'write sec1',
  scope: 'sec1',
  context: { section_id: 'sec1' },
  ...extra,
});

/** 收到 signal 立即取消（模拟真实宿主 throwIfAborted / settleForegroundRun）。 */
function honoringHost(record) {
  return (subArgs, exec) => new Promise((resolve, reject) => {
    const sig = exec && exec.signal;
    if (!sig) {
      record.noSignal = true;
      setTimeout(() => reject(new Error('宿主未收到 attempt signal')), 1200);
      return;
    }
    record.signalSeen = true;
    const reasonLabel = (r) => (r ? (r.code || r.message || String(r)) : null);
    if (sig.aborted) {
      record.abortObserved = true;
      record.abortReason = reasonLabel(sig.reason);
      reject(sig.reason || new Error('aborted'));
      return;
    }
    sig.addEventListener('abort', () => {
      record.abortObserved = true;
      record.abortReason = reasonLabel(sig.reason);
      reject(sig.reason || Object.assign(new Error('aborted'), { name: 'AbortError' }));
    }, { once: true });
  });
}

/** 忽略 signal、超时后仍返回完整 FILE 块（最危险的宿主形态：迟到写盘）。 */
function ignoringHost(record, lateMs, arts) {
  return async () => {
    record.signalSeen = !!(record.lastSignal);
    await delay(lateMs);
    record.returnedLate = true;
    return reply(arts, 'late-run');
  };
}

/** 永不 settle 的宿主（无法确认取消）。 */
function hangingHost(record) {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  record.release = (value) => { release(value); };
  return () => promise;
}

// ================================================================ DISP-03（单元）
test('DISP-03 结构化错误分型：错误码优先，状态码次之，文案启发式仅兜底', () => {
  const { classifyDispatchError } = att();
  const cls = (e) => classifyDispatchError(e).error_class;
  // 结构码
  assert.equal(cls({ code: 'DISPATCH_TIMEOUT' }), 'timeout');
  assert.equal(cls({ code: 'DISPATCH_CANCELLED' }), 'cancel');
  assert.equal(cls({ name: 'AbortError' }), 'cancel');
  assert.equal(cls({ code: 'PARSE_ERROR' }), 'content');
  assert.equal(cls({ code: 'MISSING_ARTIFACTS' }), 'content');
  assert.equal(cls({ code: 'AUTH_FAILED' }), 'auth');
  assert.equal(cls({ code: 'CONFIG_MISSING' }), 'config');
  assert.equal(cls({ code: 'ECONNRESET' }), 'transient');
  // HTTP 状态码
  assert.equal(cls({ status: 401 }), 'auth');
  assert.equal(cls({ status: 403 }), 'auth');
  assert.equal(cls({ status: 429 }), 'transient');
  assert.equal(cls({ status: 503 }), 'transient');
  assert.equal(cls({ status: 404 }), 'config');
  // 兼容层：无结构码时按文案
  assert.equal(cls(new Error('Request unauthorized: bad api key')), 'auth');
  assert.equal(cls(new Error('model not configured for stage')), 'config');
  assert.equal(cls(new Error('socket hang up')), 'transient');
  assert.equal(cls(new Error('worker 未返回可用产物')), 'content');
  assert.equal(cls(new Error('某种未知故障')), 'unknown');
  // 重试许可
  assert.equal(classifyDispatchError({ code: 'ECONNRESET' }).retryable, true);
  assert.equal(classifyDispatchError({ code: 'AUTH_FAILED' }).retryable, false);
  assert.equal(classifyDispatchError({ code: 'PARSE_ERROR' }).retryable, false);
});

test('DISP-03 策略解析：timeout/max_retries/retry_class 生效，非法值回落默认', () => {
  const { resolveAttemptPolicy } = att();
  const p = resolveAttemptPolicy({ timeout_seconds: 1, max_retries: 2, retry_class: 'transient' });
  assert.equal(p.timeout_ms, 1000);
  assert.equal(p.attempts_allowed, 3);
  assert.equal(p.retry_class, 'transient');
  const fallback = resolveAttemptPolicy({ timeout_seconds: 0, max_retries: -3, retry_class: '' });
  assert.equal(fallback.timeout_seconds, 3600, '非正超时回落默认 3600s，不静默变成立即超时');
  assert.equal(fallback.max_retries, 0, '负数 max_retries 夹到 0');
  assert.equal(fallback.retry_class, 'transient');
  assert.equal(resolveAttemptPolicy({ retry_class: 'content', max_retries: 2 }).attempts_allowed, 1,
    'content 类不在调度层重试');
  assert.equal(resolveAttemptPolicy({ retry_class: 'contract', max_retries: 2 }).attempts_allowed, 1,
    '契约类不重试（与 worker_pool 语义一致）');
});

test('DISP-03 重试许可：transient 可重试、content/auth/config/cancel 不重试、取消未确认不重派', () => {
  const { resolveAttemptPolicy, shouldRetryAttempt } = att();
  const policy = resolveAttemptPolicy({ max_retries: 2 });
  const ok = (error_class, attempt, extra = {}) => shouldRetryAttempt({ error_class, attempt, policy, ...extra });
  assert.equal(ok('transient', 1), true);
  assert.equal(ok('transient', 3), false, '不得超过 1+max_retries');
  assert.equal(ok('content', 1), false);
  assert.equal(ok('auth', 1), false);
  assert.equal(ok('config', 1), false);
  assert.equal(ok('cancel', 1), false);
  assert.equal(ok('unknown', 1), false);
  assert.equal(ok('timeout', 1, { cancel_confirmed: true }), true);
  assert.equal(ok('timeout', 1, { cancel_confirmed: false }), false, '取消未确认不得重派');
});

// ================================================================ DISP-02（单元）
test('DISP-02 attempt 中止器：父取消与截止时刻折叠进同一 signal，cleanup 摘净监听与定时器', async () => {
  const { createAttemptAbort } = att();
  const parent = fakeParentSignal();
  const abort = createAttemptAbort({ parentSignal: parent, timeoutMs: 10000 });
  assert.equal(parent.listeners.length, 1, '父 signal 上挂且仅挂一个监听');
  assert.equal(abort.signal.aborted, false);
  parent.fire(new Error('parent gone'));
  assert.equal(abort.signal.aborted, true, '父取消必须折叠到 attempt signal');
  assert.equal(abort.requested, true);
  assert.equal(abort.why, 'parent_cancel');
  assert.equal(abort.reason.code, 'DISPATCH_CANCELLED');
  abort.cleanup();
  assert.equal(parent.listeners.length, 0, 'cleanup 必须摘掉父监听');
  assert.equal(abort.listenerAttached(), false);

  // 超时路径
  const abort2 = createAttemptAbort({ parentSignal: null, timeoutMs: 30 });
  await delay(90);
  assert.equal(abort2.signal.aborted, true, '截止时刻必须真的 abort');
  assert.equal(abort2.why, 'timeout');
  assert.equal(abort2.reason.code, 'DISPATCH_TIMEOUT');
  abort2.cleanup();

  // 已中止的父 signal → 立即失效，不挂监听
  const dead = fakeParentSignal();
  dead.fire(new Error('already gone'));
  const abort3 = createAttemptAbort({ parentSignal: dead, timeoutMs: 1000 });
  assert.equal(abort3.requested, true, '父 signal 已中止时必须立刻失效');
  assert.equal(dead.listeners.length, 0, '已中止的父 signal 不必再挂监听');
  abort3.cleanup();
});

// ================================================================ DISP-04（单元）
test('DISP-04 attempt 台账：必备字段齐全、未知指标 null、绝不覆盖既有 attempt', () => {
  const { writeAttemptRecord, readAttemptRecords, ATTEMPT_REQUIRED_FIELDS } = att();
  const { root, selfDir } = mkProject('ledger');
  const guard = installWriteGuard(root);
  try {
    const first = writeAttemptRecord(selfDir, 'dsp-ledger', {
      attempt_id: 'attempt-0001', dispatch_id: 'dsp-ledger', status: 'ok',
    });
    assert.ok(fs.existsSync(first.path));
    assert.equal(path.basename(first.path), 'attempt-0001.json');
    assert.equal(path.dirname(first.path), path.join(selfDir, '.pd', 'agent-attempts', 'dsp-ledger'));
    const rec = JSON.parse(fs.readFileSync(first.path, 'utf8'));
    for (const field of ATTEMPT_REQUIRED_FIELDS) {
      assert.ok(field in rec, `台账缺必备字段 ${field}`);
    }
    assert.equal(rec.queue_ms, null, '未知排队指标写 null');
    assert.equal(rec.input_tokens, null, 'token 未知写 null（不得用字符数冒充）');
    assert.equal(rec.output_tokens, null);
    assert.equal(rec.schema_version, 1);
    // 同 attempt 再写 → 新修订文件，旧文件字节不变
    const before = fs.readFileSync(first.path, 'utf8');
    const second = writeAttemptRecord(selfDir, 'dsp-ledger', {
      attempt_id: 'attempt-0001', dispatch_id: 'dsp-ledger', status: 'timeout',
    });
    assert.notEqual(second.path, first.path, '同 attempt 重写必须换修订名');
    assert.equal(fs.readFileSync(first.path, 'utf8'), before, '既有 attempt 台账不得被覆盖');
    assert.equal(readAttemptRecords(selfDir, 'dsp-ledger').length, 2);
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

// ================================================================ DISP-01 E2E
for (const mode of ['def', 'tools', 'subagents']) {
  test(`DISP-01 [${mode}] 1 秒超时真的 abort 宿主并确认停止：晚到产物不落盘、不重试`, async () => {
    const { root, selfDir } = mkProject(`disp01-${mode}`);
    const guard = installWriteGuard(root);
    try {
      const rec = { mode };
      const handler = honoringHost(rec);
      const { res, result, record } = await withDeadline(runDispatch({
        mode, selfDir, handler,
        req: baseReq('dsp-01', { timeout_seconds: 1, max_retries: 0 }),
      }), 8000, '超时未真正生效：派发一直等待宿主，说明 timeout 没有传到宿主');
      assert.equal(rec.noSignal, undefined, '宿主必须收到 attempt signal（而不是父 exec 的 undefined）');
      assert.equal(rec.signalSeen, true);
      assert.equal(rec.abortObserved, true, '超时必须真的 abort 宿主，而不只是停止等待');
      assert.equal(rec.abortReason, 'DISPATCH_TIMEOUT');
      assert.equal(record.calls.length, 1, 'max_retries=0 → 只允许一次 attempt');
      assert.equal(result.status, 'timeout', `超时应记 timeout，实得 ${result.status}: ${result.error}`);
      assert.equal(result.attempts, 1);
      assert.equal(fs.existsSync(path.join(selfDir, 'sec1.tex')), false, '取消后不得落盘任何产物');
      const ledgers = readLedgers(selfDir, 'dsp-01');
      assert.equal(ledgers.length, 1, '超时也必须写逐 attempt 台账');
      assert.equal(ledgers[0].status, 'timeout');
      assert.equal(ledgers[0].error_class, 'timeout');
      assert.equal(ledgers[0].cancel_requested, true);
      assert.equal(ledgers[0].cancel_confirmed, true);
      assert.equal(ledgers[0].elapsed_ms >= 900, true, 'elapsed 应覆盖到超时时刻');
      assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
      assert.equal(res.success, false);
    } finally { guard.restore(); rmrf(root); }
  });

  test(`DISP-01 [${mode}] 忽略 signal 的宿主晚返回 FILE 块：内容不得落盘，且如实记账`, async () => {
    const { root, selfDir } = mkProject(`disp01-late-${mode}`);
    const guard = installWriteGuard(root);
    try {
      const rec = { mode };
      const handler = ignoringHost(rec, 700, ['sec1.tex']);
      const { result, record } = await withDeadline(runDispatch({
        mode, selfDir, handler,
        req: baseReq('dsp-01-late', { timeout_seconds: 0.4, max_retries: 0 }),
      }), 8000, '忽略 signal 的宿主未被超时取消：派发一直等到宿主自然返回');
      assert.equal(rec.returnedLate, true, '宿主确实在超时之后才返回内容');
      assert.equal(record.calls.length, 1);
      assert.equal(result.status, 'timeout', `迟到结果不得被当作成功，实得 ${result.status}`);
      assert.equal(fs.existsSync(path.join(selfDir, 'sec1.tex')), false,
        '迟到 FILE 内容绝不能落盘');
      const ledgers = readLedgers(selfDir, 'dsp-01-late');
      assert.equal(ledgers.length, 1);
      assert.equal(ledgers[0].cancel_requested, true);
      assert.equal(ledgers[0].late_output_discarded, true, '迟到结果必须显式标记为丢弃');
      assert.deepEqual(ledgers[0].output_digests, {});
      assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
    } finally { guard.restore(); rmrf(root); }
  });
}

// ================================================================ DISP-02 E2E
test('DISP-02 取消未确认：停止派发、不启动下一 attempt、迟到结果仍不落盘', async () => {
  const { root, selfDir } = mkProject('disp02-unconfirmed');
  const guard = installWriteGuard(root);
  const parent = fakeParentSignal();
  try {
    const rec = { mode: 'def' };
    const handler = hangingHost(rec);
    const pending = runDispatch({
      mode: 'def', selfDir, handler, exec: { signal: parent },
      req: baseReq('dsp-02-unconf', { timeout_seconds: 5, max_retries: 2, cancel_grace_ms: 150 }),
    });
    await delay(80);
    parent.fire(new Error('父级取消'));
    const { result, record } = await withDeadline(pending, 5000,
      '父取消未传递到 attempt：派发一直等待宿主，无法确认取消也无法停止');
    assert.equal(record.calls.length, 1, '取消未确认时绝不允许启动重叠子代理');
    assert.equal(result.attempts, 1, '不得进入第二次 attempt');
    assert.equal(result.status, 'cancellation_unconfirmed',
      `无法确认取消必须显式记账，实得 ${result.status}`);
    const ledgers = readLedgers(selfDir, 'dsp-02-unconf');
    assert.equal(ledgers.length, 1);
    assert.equal(ledgers[0].cancel_requested, true);
    assert.equal(ledgers[0].cancel_confirmed, false);
    assert.equal(ledgers[0].status, 'cancellation_unconfirmed');
    assert.equal(parent.listeners.length, 0, '父监听必须摘净');
    // 之后宿主才返回 → 仍不得落盘
    rec.release(reply(['sec1.tex'], 'very-late'));
    await delay(120);
    assert.equal(fs.existsSync(path.join(selfDir, 'sec1.tex')), false,
      '放弃后的迟到结果不得写盘');
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

test('DISP-02 父取消：清理全部监听、取消不重试、只 abort 一次', async () => {
  const { root, selfDir } = mkProject('disp02-parent');
  const guard = installWriteGuard(root);
  const parent = fakeParentSignal();
  try {
    const rec = { mode: 'tools' };
    const handler = honoringHost(rec);
    const pending = runDispatch({
      mode: 'tools', selfDir, handler, exec: { signal: parent },
      req: baseReq('dsp-02-parent', { timeout_seconds: 10, max_retries: 2 }),
    });
    await delay(80);
    parent.fire(new Error('父级取消'));
    const { result, record } = await withDeadline(pending, 5000,
      '父取消未传递到 attempt：派发一直等待宿主，无法确认取消也无法停止');
    assert.equal(rec.abortObserved, true, '父取消必须让宿主看到 signal 已 abort');
    // 宿主看到的 reason 允许是父级原始原因，也允许被包装成 DISPATCH_CANCELLED 错误码
    assert.ok(rec.abortReason, `宿主必须拿到 abort reason（实得 ${rec.abortReason}）`);
    assert.equal(record.calls.length, 1, '父取消不重试');
    assert.equal(result.status, 'cancelled');
    assert.equal(parent.listeners.length, 0, '父取消后必须摘净监听');
    assert.equal(parent.state.maxListeners <= 1, true,
      `父 signal 监听不得累积（实测峰值 ${parent.state.maxListeners}）`);
    const ledgers = readLedgers(selfDir, 'dsp-02-parent');
    assert.equal(ledgers[0].error_class, 'cancel');
    assert.equal(ledgers[0].cancel_confirmed, true);
    assert.equal(ledgers[0].cancel_reason, 'parent_cancel');
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

// ================================================================ DISP-03 E2E
test('DISP-03 内容错误：只调用一次，交返修节点并附具体反馈与 attempt 身份', async () => {
  const { root, selfDir } = mkProject('disp03-content');
  const guard = installWriteGuard(root);
  try {
    const { result, record: hostRecord } = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-03-content', { max_retries: 2 }),
      handler: async () => ({ runId: 'r-content', output: [{ type: 'text', text: '我写好了，但没有按协议输出 FILE 块。' }] }),
    });
    assert.equal(hostRecord.calls.length, 1, '内容错误不得在原 prompt 上反复空转');
    assert.equal(result.status, 'content_error', `实得 ${result.status}: ${result.error}`);
    assert.equal(result.error_class, 'content');
    assert.ok(result.feedback && result.feedback.length > 0, '返修必须带具体反馈');
    assert.equal(result.attempt_id, 'attempt-0001', '返修必须带新 attempt 身份');
    const ledgers = readLedgers(selfDir, 'dsp-03-content');
    assert.equal(ledgers.length, 1);
    assert.equal(ledgers[0].error_class, 'content');
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

test('DISP-03 瞬时失败：最多 1+max_retries 次，逐 attempt 落账', async () => {
  const { root, selfDir } = mkProject('disp03-transient');
  const guard = installWriteGuard(root);
  try {
    let n = 0;
    const { result, record } = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-03-transient', { max_retries: 2 }),
      handler: async () => {
        n += 1;
        if (n <= 2) throw Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' });
        return reply(['sec1.tex']);
      },
    });
    assert.equal(record.calls.length, 3, 'transient 允许 1+max_retries 次尝试');
    assert.equal(result.status, 'ok');
    assert.equal(result.attempts, 3);
    const ledgers = readLedgers(selfDir, 'dsp-03-transient');
    assert.equal(ledgers.length, 3, '每次尝试都要有一份台账');
    assert.deepEqual(ledgers.map((l) => l.status), ['failed', 'failed', 'ok']);
    assert.deepEqual(ledgers.map((l) => l.error_class), ['transient', 'transient', 'none']);
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

test('DISP-03 认证/配置失败与契约类派发单：一次即止', async () => {
  const { root, selfDir } = mkProject('disp03-auth');
  const guard = installWriteGuard(root);
  try {
    let n = 0;
    const auth = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-03-auth', { max_retries: 2 }),
      handler: async () => { n += 1; throw Object.assign(new Error('401 unauthorized'), { code: 'AUTH_FAILED' }); },
    });
    assert.equal(auth.record.calls.length, 1, '认证失败不重试');
    assert.equal(auth.result.status, 'auth_error');
    assert.equal(auth.result.error_class, 'auth');

    let m = 0;
    const cfg = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-03-config', { max_retries: 2 }),
      handler: async () => { m += 1; throw Object.assign(new Error('model not configured'), { code: 'CONFIG_MISSING' }); },
    });
    assert.equal(cfg.record.calls.length, 1, '配置失败不重试');
    assert.equal(cfg.result.status, 'config_error');

    let k = 0;
    const contract = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-03-contract', { max_retries: 2, retry_class: 'contract' }),
      handler: async () => { k += 1; throw Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' }); },
    });
    assert.equal(contract.record.calls.length, 1, 'retry_class 非 transient → 单次');
    assert.equal(n, 1);
    assert.equal(m, 1);
    assert.equal(k, 1);
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

// ================================================================ DISP-04 E2E
test('DISP-04 三次 attempt 三份台账 + 汇总总时长不等于仅最后一次', async () => {
  const { root, selfDir } = mkProject('disp04-summary');
  const guard = installWriteGuard(root);
  try {
    let n = 0;
    const { result } = await runDispatch({
      mode: 'def', selfDir,
      req: baseReq('dsp-04', { max_retries: 2, timeout_seconds: 30 }),
      handler: async () => {
        n += 1;
        await delay(40);
        if (n <= 2) throw Object.assign(new Error('ECONNRESET during transport'), { code: 'ECONNRESET' });
        return reply(['sec1.tex'], 'run-ok');
      },
    });
    assert.equal(result.status, 'ok');
    assert.equal(result.attempts, 3);
    const ledgers = readLedgers(selfDir, 'dsp-04');
    assert.deepEqual(ledgers.map((l) => l.attempt_id), ['attempt-0001', 'attempt-0002', 'attempt-0003']);
    for (const l of ledgers) {
      assert.ok(l.elapsed_ms >= 35, `attempt 时长必须实测（实得 ${l.elapsed_ms}）`);
      assert.ok(l.started_at && l.finished_at);
    }
    const summary = readSummary(selfDir, 'dsp-04');
    assert.equal(summary.status, 'ok');
    assert.equal(summary.attempts, 3);
    assert.deepEqual(summary.attempt_ids, ['attempt-0001', 'attempt-0002', 'attempt-0003'],
      '汇总必须保留 attempt 索引');
    const sumAttempts = ledgers.reduce((acc, l) => acc + l.elapsed_ms, 0);
    assert.ok(summary.total_elapsed_ms >= sumAttempts - 5,
      `总时长 ${summary.total_elapsed_ms} 应覆盖全部 attempt（累计 ${sumAttempts}）`);
    assert.ok(summary.total_elapsed_ms > summary.duration_ms,
      `总时长 ${summary.total_elapsed_ms} 不得等于最后一次 attempt 的 ${summary.duration_ms}`);
    assert.equal(fs.existsSync(path.join(selfDir, 'sec1.tex')), true);
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

// ================================================================ DISP-05 宿主链路
test('DISP-05 生产代码把 attempt signal 交给三个适配分支（源码静态核验）', () => {
  const src = fs.readFileSync(path.join(ROOT, 'index.js'), 'utf8');
  assert.match(src, /from '\.\/scripts\/dispatch_attempts\.mjs'/,
    'index.js 必须使用逐 attempt 运行时模块');
  assert.match(src, /createAttemptAbort\(/, 'index.js 必须为每个 attempt 建 AbortController');
  assert.match(src, /writeAttemptRecord\(/, 'index.js 必须落逐 attempt 台账');
  assert.match(src, /attemptExec/, 'index.js 必须把 attemptExec 传给 callSubagent');
  assert.ok(!/signal:\s*exec\s*&&\s*exec\.signal/.test(src),
    '三个宿主分支不得再直接用父 exec.signal（那样超时永远传不到宿主）');
  // 三个分支都必须出现 attemptExec.signal
  const occurrences = (src.match(/attemptExec\.signal/g) || []).length;
  assert.ok(occurrences >= 2, `attemptExec.signal 应出现在 tools.execute 与 subagents.start 分支（实测 ${occurrences}）`);
  // 分支一：tools.get("subagent")。形参名不写死——生产实现里该分支传的是
  // 剥去过宿主 schema 的 toolArgs（删掉 max_tokens），语义要求是「第二实参
  // 必须是 attemptExec」，写死形参名会让任何局部改名都误报。
  assert.match(src, /subagentDef\.execute\([A-Za-z_$][\w$]*,\s*attemptExec\)/,
    '分支一（tools.get("subagent")）必须收到 attemptExec');
  assert.match(src, /\{\s*max_tokens\s*:[^}]*\.\.\.[A-Za-z_$][\w$]*\s*\}\s*=\s*subArgs/,
    '分支一不得把 max_tokens 透传给宿主 subagent schema（须解构剔除）');
});

test('DISP-05 真实宿主取消契约：execute(args, exec) 消费 exec.signal（静态核验，宿主源码只读）', (t) => {
  if (!fs.existsSync(HOST_SOURCE)) {
    t.diagnostic(`宿主源码不可达，跳过静态核验: ${HOST_SOURCE}`);
    return;
  }
  const host = fs.readFileSync(HOST_SOURCE, 'utf8');
  assert.match(host, /exec\.signal/, '宿主 subagent 工具必须消费 exec.signal');
  assert.match(host, /throwIfAborted/, '宿主必须在取消时抛 AbortError（我们据此确认取消已生效）');
  assert.match(host, /signal:\s*exec\.signal/, '宿主必须把 signal 透传给 runtimeCtx.subagents.start');
});
