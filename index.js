import { execFile } from 'node:child_process';
import * as path from 'node:path';
import * as fs from 'node:fs';
import os from 'node:os';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import {
  resolveStageRoute, buildDispatchPrompt, parseSubagentFiles, appendDebugRecord, safeJoinSelf,
} from './scripts/dispatch_runtime.mjs';
import {
  resolveAttemptPolicy, classifyDispatchError, shouldRetryAttempt, createAttemptAbort,
  attemptExecWithSignal, settleWithCancel, formatAttemptId, writeAttemptRecord, delayMs,
} from './scripts/dispatch_attempts.mjs';
import {
  resolveDispatchPolicy, DispatchPolicyError,
} from './scripts/dispatch_policy.mjs';
import {
  sanitizeId, assertWithin, isWithin, PathSafetyError, normalizeRel,
} from './scripts/path_safety.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workspaceRoot = __dirname;

// 子 Agent 回复 → 纯文本（仅用于解析失败时的原始留档，不参与解析决策）
function rawSubagentText(raw) {
  if (!raw) return '';
  if (typeof raw === 'string') return raw;
  if (typeof raw === 'object') {
    const blocks = raw.output || (raw.result && raw.result.output) || raw.content;
    if (Array.isArray(blocks)) {
      return blocks.map((b) => {
        if (!b) return '';
        if (typeof b === 'string') return b;
        if (typeof b === 'object') {
          const tag = b.type ? `[${String(b.type)}]` : '';
          return tag + String(b.text || b.content || b.value || '');
        }
        return String(b);
      }).join('\n');
    }
    if (typeof blocks === 'string') return blocks;
    if (raw.text) return String(raw.text);
  }
  try { return JSON.stringify(raw); } catch (_) { return ''; }
}

function resolveEngineRoot(customPath) {
  if (customPath && fs.existsSync(customPath)) {
    return path.resolve(customPath);
  }
  if (fs.existsSync(path.join(__dirname, 'workflow', 'runner.py'))) {
    return __dirname;
  }
  if (fs.existsSync(path.join(process.cwd(), 'workflow', 'runner.py'))) {
    return process.cwd();
  }
  throw new Error(
    '无法定位题链流水线引擎：未找到 workflow/runner.py' +
    '（已依次尝试自定义路径、插件目录与当前工作目录）。' +
    '请通过配置 projectRoot 显式指定引擎根目录。'
  );
}

function resolvePythonBin(customBin) {
  if (customBin && customBin !== 'python' && fs.existsSync(customBin)) return customBin;
  if (process.env.PYTHON_PATH && fs.existsSync(process.env.PYTHON_PATH)) return process.env.PYTHON_PATH;
  if (process.platform === 'win32') {
    const candidates = [
      'C:\\Python312\\python.exe',
      'C:\\Python311\\python.exe',
      'C:\\Python310\\python.exe',
      process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Programs\\Python\\Python312\\python.exe') : null,
      process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Programs\\Python\\Python311\\python.exe') : null,
      'python.exe',
    ].filter(Boolean);
    for (const cand of candidates) {
      if (path.isAbsolute(cand) && fs.existsSync(cand)) return cand;
    }
    return 'python.exe';
  }
  return 'python3';
}

function runPython(args, cwd, timeoutMs = 180000, pythonBin = 'python', extraEnv = {}) {
  const bin = resolvePythonBin(pythonBin);
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      ...extraEnv,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    };
    execFile(
      bin,
      args,
      { cwd, env, timeout: timeoutMs, maxBuffer: 10 * 1024 * 1024, encoding: 'utf-8' },
      (err, stdout, stderr) => {
        resolve({
          success: !err,
          code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
          stdout: stdout ? stdout.trim() : '',
          stderr: stderr ? stderr.trim() : '',
          error: err ? `${err.message} (bin: ${bin}, cwd: ${cwd}, args: ${JSON.stringify(args)})` : null,
        });
      }
    );
  });
}

function resolveCurrentProjectDir(ctx, hintPath) {
  const cwd = process.cwd();
  if (hintPath) {
    if (path.isAbsolute(hintPath) && fs.existsSync(hintPath)) {
      return fs.statSync(hintPath).isDirectory() ? hintPath : path.dirname(hintPath);
    }
    const resolvedInCwd = path.resolve(cwd, hintPath);
    if (fs.existsSync(resolvedInCwd)) {
      return cwd;
    }
  }

  try {
    const registry = ctx?.get?.('workspaceRegistry') || ctx?.workspaceRegistry;
    if (registry && typeof registry.list === 'function') {
      const list = registry.list();
      for (const item of (list || [])) {
        if (item?.path && fs.existsSync(item.path)) {
          if (hintPath && fs.existsSync(path.resolve(item.path, hintPath))) {
            return item.path;
          }
        }
      }
    }
  } catch (_) {}

  return cwd;
}

// ---- 人工台账工具（qchain_approve / qchain_defer）共用 ---------------------

/**
 * A14：判断一个目录是否**真的**是 qchain self 工作区。
 *
 * 旧实现在 self_dir 探测里只做 `fs.existsSync(cwd + '/self')`：宿主进程的
 * cwd 指向哪里完全不可控（实测误命中 D:\Scripts\self），于是 qchain_status /
 * qchain_gate / qchain_dispatch 会在用户毫不知情的情况下对**另一个工作区**
 * 报 PASS/FAIL、甚至往别人的 .pd 里写台账。同名目录不是工作区。
 *
 * 判定采用「有产物」口径：.pd/、config.yaml、coverage-map.md、
 * overview-guide/、任一 sec*.tex 至少命中一项；纯空壳 self/ 不算。
 */
function looksLikeSelfWorkspace(dir) {
  if (!dir) return false;
  try {
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return false;
    const markers = ['.pd', 'config.yaml', 'coverage-map.md', 'overview-guide.md', 'overview-guide'];
    for (const m of markers) {
      if (fs.existsSync(path.join(dir, m))) return true;
    }
    return fs.readdirSync(dir).some((f) => /^sec\d+\.tex$/i.test(f));
  } catch (_) {
    return false;
  }
}

/** 定位 self 工作区（显式 → cwd/self → workspaceRegistry → 当前工程/self）。 */
function resolveSelfWorkspaceDir(ctx, hintSelfDir) {
  const candidates = [];
  if (hintSelfDir) {
    candidates.push(path.isAbsolute(hintSelfDir)
      ? path.resolve(hintSelfDir)
      : path.resolve(process.cwd(), hintSelfDir));
  }
  candidates.push(path.join(process.cwd(), 'self'));
  try {
    const registry = ctx?.get?.('workspaceRegistry') || ctx?.workspaceRegistry;
    if (registry && typeof registry.list === 'function') {
      for (const entry of (registry.list() || [])) {
        if (entry && entry.path) candidates.push(path.join(entry.path, 'self'));
      }
    }
  } catch (_) {}
  candidates.push(path.join(resolveCurrentProjectDir(ctx), 'self'));
  // A14：隐式候选一律过工作区形态校验（cwd 下的同名 self 不算工作区）；
  // 显式 hintSelfDir 由用户自己负责、且只要求它是目录，但它不是"隐式"候选，
  // 绝不能靠"下标 > 0"这种巧合免检（下标会随显式参数是否传入而平移）。
  const hasExplicit = !!hintSelfDir;
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    try {
      if (!fs.existsSync(c) || !fs.statSync(c).isDirectory()) continue;
      if (!(hasExplicit && i === 0) && !looksLikeSelfWorkspace(c)) continue;
      return c;
    } catch (_) {}
  }
  return '';
}

/** 跑 scripts/ledger.py（人工批准 / advisory 延期的唯一写入通路）。 */
async function runLedger(ctx, selfDir, argv) {
  if (!selfDir) {
    return { success: false, output: '❌ 未找到 self 工作区目录（请指定 self_dir）', payload: null };
  }
  const res = await runPython(
    [path.join(engineRoot, 'scripts', 'ledger.py'), ...argv, '--self', selfDir, '--json'],
    selfDir, 60000, pythonBin);
  const raw = [res.stdout, res.stderr].filter(Boolean).join('\n').trim();
  let payload = null;
  try {
    payload = JSON.parse(res.stdout);
  } catch (_) { /* 非 JSON 输出（例如 argparse 报错）原样回传 */ }
  const detail = payload && payload.success === false
    ? String(payload.error || '')
    : '';
  return {
    success: res.success && (!payload || payload.success !== false),
    output: (detail ? `❌ ${detail}\n` : '') + raw,
    payload,
  };
}

// ---- qchain_dispatch 辅助（q_audit.md 多 Agent 证据链） --------------------

function qdReadJson(absPath) {
  try {
    return JSON.parse(fs.readFileSync(absPath, 'utf8'));
  } catch (_) {
    return null;
  }
}

function qdSafeJoin(selfDir, rel) {
  return safeJoinSelf(selfDir, rel);
}

/** 白名单产物里第一个非法路径（非法即整单拒绝，不跳过、不静默）。 */
function qdFirstInvalidArtifact(selfDir, request) {
  const arts = Array.isArray(request && request.artifacts) ? request.artifacts : [];
  for (const a of arts) {
    if (typeof a !== 'string' || !a.trim()) continue;
    if (!qdSafeJoin(selfDir, a)) return a;
  }
  return '';
}

function qdSha256File(absPath) {
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(absPath)).digest('hex');
  } catch (_) {
    return null;
  }
}

function qdAtomicWrite(absPath, content) {
  const dir = path.dirname(absPath);
  if (dir && !fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  const tmp = `${absPath}.tmp-${crypto.randomBytes(4).toString('hex')}`;
  fs.writeFileSync(tmp, content, 'utf8');
  fs.renameSync(tmp, absPath);
}

/**
 * 修复 D3：白名单产物的双阶段提交。
 *
 * 阶段 1「准备」：全部内容写进同目录 tmp 文件（此时工作区里没有任何产物被改动）；
 * 阶段 2「备份」：读走每个目标文件的原始内容（含"原本不存在"的事实）；
 * 阶段 3「提交」：逐个 renameSync；任一 rename 抛错 ⇒ 还原所有已提交文件、删净 tmp。
 *
 * 之所以必须先备份再 rename：rename 是逐文件的，一旦第 2 个目标失败，第 1 个已经
 * 落在磁盘上；没有备份就只能把工作区留在"一半新一半旧"的状态。
 * 返回 {ok, digests, error, rolled_back}；digests 只在 ok 时可用。
 */
function qdCommitArtifacts(selfDir, pending) {
  const tmps = [];
  const backups = [];
  const committed = [];
  try {
    for (const item of pending) {
      assertWithin(selfDir, item.abs, `产物 ${item.rel}`);
      const dir = path.dirname(item.abs);
      if (dir && !fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
        assertWithin(selfDir, dir, `产物目录 ${item.rel}`);
      }
      const tmp = `${item.abs}.tmp-${crypto.randomBytes(4).toString('hex')}`;
      fs.writeFileSync(tmp, item.toWrite, 'utf8');
      tmps.push(tmp);
    }
    for (const item of pending) {
      const prev = fs.existsSync(item.abs) ? fs.readFileSync(item.abs) : null;
      backups.push({ abs: item.abs, prev });
    }
    for (let i = 0; i < pending.length; i += 1) {
      fs.renameSync(tmps[i], pending[i].abs);
      committed.push(pending[i].abs);
    }
    const digests = {};
    for (const item of pending) digests[item.rel] = qdSha256File(item.abs);
    return { ok: true, digests, error: '', rolled_back: false };
  } catch (err) {
    let rollbackErr = '';
    for (const b of backups) {
      if (!committed.includes(b.abs)) continue;
      try {
        if (b.prev === null) fs.rmSync(b.abs, { force: true });
        else fs.writeFileSync(b.abs, b.prev);
      } catch (e2) {
        rollbackErr += ` ${b.abs}: ${e2 && e2.message}`;
      }
    }
    for (const t of tmps) {
      try { fs.rmSync(t, { force: true }); } catch (_) { /* 清 tmp 失败不影响回滚结论 */ }
    }
    const base = err && err.message ? err.message : String(err);
    return {
      ok: false,
      digests: {},
      error: rollbackErr ? `${base} [回滚异常:${rollbackErr}]` : base,
      rolled_back: !rollbackErr,
    };
  }
}

/** 请求指纹：同一 dispatch_id 只有在「请求内容完全一致」时才允许复用旧产出。 */
function qdRequestFingerprint(request) {
  const canon = {
    stage: String(request.stage || ''),
    role: String(request.role || ''),
    scope: String(request.scope || ''),
    prompt_file: String(request.prompt_file || ''),
    instruction: String(request.instruction || ''),
    input_path: String(request.input_path || ''),
    artifacts: (Array.isArray(request.artifacts) ? request.artifacts : [])
      .map((a) => String(a).replace(/\\/g, '/')).sort(),
    context: request.context === undefined ? null : request.context,
  };
  return crypto.createHash('sha256').update(JSON.stringify(canon)).digest('hex');
}

// ---- 批次选择（任务20，C12.2） ------------------------------------------
// 解析待派发请求时必须「批次索引优先、语义计划兼容、绝不扫描全部历史 pending」。
// 一切异常一律以带 code 的显式错误返回（由 execute 包装为 success:false），
// 不允许静默退化为「把 .pd/agent-requests 下所有 pending 全派一遍」。

class DispatchSelectionError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'DispatchSelectionError';
    this.code = code;
  }
}

function qdBatchIndexPath(selfDir, batchId) {
  return path.join(selfDir, '.pd', 'dispatch-batches', `${batchId}.json`);
}

function qdLoadBatchRequests(selfDir, batchId, ids, runId) {
  const out = [];
  for (const rawId of (ids || [])) {
    const id = String(rawId || '');
    if (!id) continue;
    const full = qdReadJson(path.join(selfDir, '.pd', 'agent-requests', `${id}.json`));
    if (!full) {
      throw new DispatchSelectionError('BATCH_REQUEST_MISSING',
        `批次 ${batchId} 的派发单缺失: .pd/agent-requests/${id}.json`);
    }
    const rb = String(full.batch_id || '');
    if (rb && rb !== String(batchId)) {
      throw new DispatchSelectionError('BATCH_REQUEST_INCONSISTENT',
        `派发单 ${id} 的 batch_id=${rb} 与所选批次 ${batchId} 不一致。`);
    }
    const rr = String(full.run_id || '');
    if (runId && rr && rr !== String(runId)) {
      throw new DispatchSelectionError('STALE_OR_AMBIGUOUS_BATCH',
        `派发单 ${id} 的 run_id=${rr} 与批次 ${batchId} 的 run=${runId} 不一致（疑似历史残留）。`);
    }
    out.push(full);
  }
  return out;
}

function qdResolveBatch(selfDir, batchId) {
  if (!batchId) {
    throw new DispatchSelectionError('BATCH_REQUIRED', 'batch_id 不能为空。');
  }
  // 1) 批次索引（首选，可核对 run/stage/请求集合/计划摘要）
  const index = qdReadJson(qdBatchIndexPath(selfDir, batchId));
  if (index) {
    if (String(index.batch_id || '') !== String(batchId)) {
      throw new DispatchSelectionError('BATCH_INDEX_CONFLICT',
        `批次索引 ${batchId} 内部 batch_id=${index.batch_id} 不符（索引串批/损坏）。`);
    }
    const planRel = String(index.plan_rel || '');
    if (planRel) {
      const planAbs = path.join(selfDir, planRel);
      if (!fs.existsSync(planAbs)) {
        throw new DispatchSelectionError('STALE_OR_AMBIGUOUS_BATCH',
          `批次 ${batchId} 的计划文件不存在: ${planRel}（疑似过期批次）。`);
      }
      const digest = String(index.plan_digest || '');
      if (digest) {
        const actual = qdSha256File(planAbs);
        if (actual !== digest) {
          throw new DispatchSelectionError('STALE_OR_AMBIGUOUS_BATCH',
            `批次 ${batchId} 的计划摘要不符（计划已被改动，疑似过期/串批）。`);
        }
      }
    }
    const ids = Array.isArray(index.request_ids) ? index.request_ids : [];
    return qdLoadBatchRequests(selfDir, batchId, ids, String(index.run_id || ''));
  }
  // 2) 旧项目兼容：通用计划 + 语义计划里按 batch_id 精确匹配
  const matches = [];
  for (const rel of ['.pd/dispatch-plan.json', '.pd/dispatch-plan-semantic.json']) {
    const plan = qdReadJson(path.join(selfDir, rel));
    if (plan && String(plan.batch_id || '') === String(batchId)) matches.push({ rel, plan });
  }
  if (matches.length === 0) {
    throw new DispatchSelectionError('BATCH_NOT_FOUND',
      `未找到批次 ${batchId}（无批次索引，且通用/语义计划均无该 batch_id）。`);
  }
  if (matches.length > 1) {
    throw new DispatchSelectionError('STALE_OR_AMBIGUOUS_BATCH',
      `批次 ${batchId} 同时出现在多个计划文件中，归属不明。`);
  }
  const plan = matches[0].plan;
  const planBatch = String(plan.batch_id || '');
  const reqs = Array.isArray(plan.requests) ? plan.requests : [];
  const ids = reqs
    .filter((r) => String((r || {}).batch_id || planBatch) === String(batchId))
    .map((r) => String((r || {}).dispatch_id || ''));
  if (ids.length === 0) {
    throw new DispatchSelectionError('BATCH_NOT_FOUND', `批次 ${batchId} 的计划中没有请求。`);
  }
  return qdLoadBatchRequests(selfDir, batchId, ids, String(plan.run_id || ''));
}

function qdResolveNoArg(selfDir) {
  const known = new Set();
  let indexNames = [];
  try {
    indexNames = fs.readdirSync(path.join(selfDir, '.pd', 'dispatch-batches'))
      .filter((f) => f.endsWith('.json')).sort();
  } catch (_) {}
  for (const name of indexNames) {
    const idx = qdReadJson(path.join(selfDir, '.pd', 'dispatch-batches', name));
    const bid = String((idx && idx.batch_id) || name.replace(/\.json$/, ''));
    if (bid) known.add(bid);
  }
  if (known.size === 0) {
    for (const rel of ['.pd/dispatch-plan.json', '.pd/dispatch-plan-semantic.json']) {
      const plan = qdReadJson(path.join(selfDir, rel));
      const bid = String((plan && plan.batch_id) || '');
      if (bid) known.add(bid);
    }
  }
  if (known.size === 0) {
    throw new DispatchSelectionError('BATCH_REQUIRED',
      '未提供 batch_id/request_ref，且工作区没有可识别的当前批次；'
      + '请显式传入 batch_id（严禁扫描 .pd/agent-requests 下全部历史 pending）。');
  }
  if (known.size > 1) {
    throw new DispatchSelectionError('AMBIGUOUS_BATCH',
      `检测到多个批次 (${[...known].sort().join(', ')})，no-arg 调用有歧义；请显式传入 batch_id。`);
  }
  return qdResolveBatch(selfDir, String([...known][0]));
}

function qdResolveRequests(selfDir, args) {
  const wantRequest = !!args.request_ref;
  const wantBatch = !!args.batch_id;
  if (wantRequest && wantBatch) {
    throw new DispatchSelectionError('BATCH_AND_REQUEST_CONFLICT',
      'request_ref 与 batch_id 不能同时提供（二者互斥：整批入口用 batch_id，单请求返修用 request_ref）。');
  }
  if (wantRequest) {
    const abs = path.isAbsolute(args.request_ref)
      ? args.request_ref
      : path.join(selfDir, args.request_ref);
    const one = qdReadJson(abs);
    return one ? [one] : [];
  }
  if (wantBatch) {
    return qdResolveBatch(selfDir, String(args.batch_id));
  }
  return qdResolveNoArg(selfDir);
}

// ---- 幂等认领 / 重放（任务20，C12.2） ------------------------------------
// .pd/dispatch-claims/<dispatch_id>.json  —— 以 fs.openSync(...,'wx') 原子创建
// （Node 单线程 + O_EXCL ⇒ 同进程内并发二次调用必有一次拿到 EEXIST）。

// 修复 D1：dispatch_id 直接拼进文件名，含 `../../x` 可把 claim 写到 self 之外。
// 现在先过标识符白名单，再经 realpath 越界断言；非法 id 一律不落盘。
function qdClaimPath(selfDir, dispatchId) {
  const safeId = sanitizeId(dispatchId, { label: 'dispatch_id' });
  const dir = path.join(selfDir, '.pd', 'dispatch-claims');
  const abs = path.join(dir, `${safeId}.json`);
  assertWithin(selfDir, abs, '派发认领文件');
  return abs;
}

function qdPidAlive(pid) {
  const n = Number(pid);
  if (!Number.isInteger(n) || n <= 0) return false;
  try {
    process.kill(n, 0);
    return true;
  } catch (err) {
    return !!(err && err.code === 'EPERM');
  }
}

function qdCurrentDigests(selfDir, request) {
  const out = {};
  const arts = (Array.isArray(request.artifacts) ? request.artifacts : [])
    .filter((a) => typeof a === 'string' && a);
  for (const rel of arts) {
    let safe = '';
    try { safe = qdSafeJoin(selfDir, rel); } catch (_) { safe = ''; }
    if (!safe) continue;
    const d = qdSha256File(safe);
    if (d) out[rel] = d;
  }
  return out;
}

function qdReceiptOk(selfDir, dispatchId) {
  const ev = qdReadJson(path.join(selfDir, '.pd', 'agent-calls', `${dispatchId}.json`));
  return !!(ev && String(ev.status || '') === 'ok');
}

/**
 * 修复 D4（A15 未闭合）：同一个 dispatch_id 的旧产出能否复用。
 *
 * 旧实现只遍历 existing.output_digests 的键比对当前文件摘要，于是三种情况都会被
 * 误判为"可复用"：①本次白名单新增了产物（新键没有旧摘要，循环压根不看它）；
 * ②换了 run（跨轮复用上一轮的结果）；③改了 instruction/prompt 但 id 没变。
 * 现在四个条件全部满足才复用：同 status=done、同 run_id、同请求指纹、
 * 本次白名单是旧摘要键的**子集**且摘要逐一同值。
 * 用户拍板：严格口径，无宽限——缺指纹（历史 claim）一律视为不可复用。
 */
function qdClaimIsReusable(selfDir, request, existing) {
  if (!existing || String(existing.status || '') !== 'done') return false;
  const dispatchId = String(request.dispatch_id || '');
  if (!qdReceiptOk(selfDir, dispatchId)) return false;
  if (!(dispatchId && !qdFirstInvalidArtifact(selfDir, request))) return false;
  if (String(existing.run_id || '') !== String(request.run_id || '')) return false;
  const fingerprint = String(existing.request_fingerprint || '');
  if (!fingerprint || fingerprint !== qdRequestFingerprint(request)) return false;
  const expected = (existing.output_digests && typeof existing.output_digests === 'object')
    ? existing.output_digests : {};
  const keys = Object.keys(expected);
  if (keys.length === 0) return false;
  const arts = (Array.isArray(request.artifacts) ? request.artifacts : [])
    .filter((a) => typeof a === 'string' && a);
  if (arts.length === 0) return false;
  for (const rel of arts) {
    if (!Object.prototype.hasOwnProperty.call(expected, rel)) return false;
  }
  const actual = qdCurrentDigests(selfDir, request);
  for (const k of Object.keys(expected)) {
    if (String(actual[k] || '') !== String(expected[k] || '')) return false;
  }
  return true;
}

function qdAcquireClaim(selfDir, request) {
  const dispatchId = String(request.dispatch_id || '');
  // 修复 D1：非法 dispatch_id 一律**不落盘**，直接以显式错误返回（旧实现先
  // mkdirSync + openSync('wx') 落 claim，之后才在 buildDispatchPrompt 报错，
  // 于是 `../../../x` 会把 claim 写到 self 的父目录）。
  let claimPath;
  try {
    claimPath = qdClaimPath(selfDir, dispatchId);
  } catch (err) {
    if (err instanceof PathSafetyError) {
      return { mode: 'invalid_id', code: err.code, detail: err.message };
    }
    throw err;
  }
  const writeClaim = (status, extra) => {
    const payload = {
      claim_version: 1,
      dispatch_id: dispatchId,
      batch_id: String(request.batch_id || ''),
      run_id: String(request.run_id || ''),
      status,
      request_fingerprint: qdRequestFingerprint(request),
      host: os.hostname(),
      pid: process.pid,
      claimed_at: new Date().toISOString(),
      heartbeat_at: new Date().toISOString(),
      attempt: Number(request.attempt || 0),
      receipt_rel: '',
      output_digests: {},
      ...extra,
    };
    qdAtomicWrite(claimPath, `${JSON.stringify(payload, null, 2)}\n`);
    return payload;
  };
  // 原子创建
  try {
    fs.mkdirSync(path.dirname(claimPath), { recursive: true });
    const fd = fs.openSync(claimPath, 'wx');
    try {
      fs.writeSync(fd, `${JSON.stringify({
        claim_version: 1, dispatch_id: dispatchId,
        batch_id: String(request.batch_id || ''), run_id: String(request.run_id || ''),
        status: 'active', host: os.hostname(), pid: process.pid,
        claimed_at: new Date().toISOString(), heartbeat_at: new Date().toISOString(),
        attempt: Number(request.attempt || 0), receipt_rel: '', output_digests: {},
        request_fingerprint: qdRequestFingerprint(request),
      }, null, 2)}\n`);
    } finally {
      fs.closeSync(fd);
    }
    return { mode: 'claimed', claim: { status: 'active' } };
  } catch (err) {
    if (!err || err.code !== 'EEXIST') throw err;
  }
  // 已存在：读取并判定
  const existing = qdReadJson(claimPath) || {};
  const status = String(existing.status || '');
  if (qdClaimIsReusable(selfDir, request, existing)) {
    return { mode: 'reuse', receipt_rel: String(existing.receipt_rel || '') };
  }
  if (status === 'active') {
    const sameHost = String(existing.host || '') === os.hostname();
    if (sameHost && qdPidAlive(existing.pid)) {
      return { mode: 'busy', detail: `DISPATCH_BUSY: 派发单 ${dispatchId} 正在被 pid=${existing.pid} 执行（并发重复派发被拒）。` };
    }
    if (sameHost && !qdPidAlive(existing.pid)) {
      // 旧执行已终止（进程不存在）→ 允许接管
      writeClaim('active', { recovered_from_pid: existing.pid });
      return { mode: 'claimed', claim: { status: 'active', recovered: true } };
    }
    return { mode: 'busy', detail: `DISPATCH_BUSY: 派发单 ${dispatchId} 由主机 ${existing.host} 认领中，拒绝重复派发。` };
  }
  // failed / 其它 → 重新认领
  writeClaim('active');
  return { mode: 'claimed', claim: { status: 'active', reclaimed: true } };
}

function qdFinishClaim(selfDir, dispatchId, status, extra) {
  try {
    const claimPath = qdClaimPath(selfDir, dispatchId);
    const existing = qdReadJson(claimPath) || {};
    const payload = {
      ...existing,
      status,
      heartbeat_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      ...(extra || {}),
    };
    qdAtomicWrite(claimPath, `${JSON.stringify(payload, null, 2)}\n`);
  } catch (_) {}
}

// ---- 任务21（C12.4）：并发观测口径 -----------------------------------------
// 单调时钟只用于同进程耗时（elapsed_ms）；UTC 时点仅用于持久化时序。
// 绝不把「宿主调用窗口」命名为「模型推理窗口」：供应商排队/首 token/推理时间
// 只有在真正拿到供应商侧数据时才填，否则一律 null（未知 ≠ 实测 0）。
function qdMonoMs() {
  const perf = globalThis.performance;
  if (perf && typeof perf.now === 'function') return perf.now();
  return Date.now();
}

/** C12.4 允许的串行原因机器码（未知必须落 host_queue_or_unknown 并带来源）。 */
const QD_SERIAL_REASON_CODES = Object.freeze([
  'explicit_safe', 'single_ready', 'cache_reuse', 'artifact_dependency',
  'worker_cap', 'host_queue_or_unknown',
]);

/**
 * 依据实际事件判定「为什么没有并行」。
 * 返回 {code, source}：code 为空串表示实测并行达成；source 永远写明结论来自哪条证据，
 * 让「配置串行 / 队列只有一单 / 执行器侧限制 / 宿主排队或未知」四类原因可区分。
 */
function qdSerialReason(obs = {}) {
  const requestCount = Number(obs.requestCount) || 0;
  const reusedCount = Number(obs.reusedCount) || 0;
  const effective = Number(obs.effectiveConcurrency) || 0;
  const configured = Number(obs.configuredConcurrency) || 0;
  const peak = (typeof obs.peakHostInFlight === 'number' && Number.isFinite(obs.peakHostInFlight))
    ? obs.peakHostInFlight : null;
  if (requestCount <= 1) return { code: 'single_ready', source: 'observed_request_count' };
  if (reusedCount >= requestCount) return { code: 'cache_reuse', source: 'observed_claim_reuse' };
  if (obs.declaredDependency) {
    return { code: 'artifact_dependency', source: 'declared_request_dependency' };
  }
  if (Number(obs.undispatchedCount) > 0) {
    return { code: 'host_queue_or_unknown', source: 'observed_slot_held_unconfirmed' };
  }
  if (effective <= 1) {
    if (obs.capabilityVerified && Number(obs.workerMaxConcurrency) === 1) {
      return { code: 'worker_cap', source: 'executor_worker_cap' };
    }
    if (configured > 1 && effective < configured) {
      return { code: 'worker_cap', source: 'effective_concurrency_clamped' };
    }
    const src = String(obs.concurrencySource || '');
    if (src.startsWith('config:') || src.startsWith('tool:') || src.startsWith('frozen:')) {
      return { code: 'explicit_safe', source: src };
    }
    return { code: 'explicit_safe', source: `policy_profile:${obs.profile || 'unknown'}` };
  }
  if (peak === null) return { code: 'host_queue_or_unknown', source: 'unknown:peak_not_observed' };
  if (peak < Math.min(effective, requestCount)) {
    return { code: 'host_queue_or_unknown', source: 'observed_low_peak' };
  }
  return { code: '', source: 'observed_parallel' };
}

export const inject = ['tools'];

export function apply(ctx, config = {}) {
  const pythonBin = resolvePythonBin(config.pythonPath);
  const engineRoot = resolveEngineRoot(config.projectRoot);

  // 1. 注册 Tool: qchain_pipeline
  ctx.tools.register({
    name: 'qchain_pipeline',
    description: '题链流水线驱动工具 (LangGraph)。支持启动流水线(run)、断点恢复(resume)、查询阶段状态与检查点(status)与导出计算图(visualize)。resume 需携带真人提供的 Phase 0 令牌（经 token 参数透传）。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['status', 'run', 'resume', 'visualize'],
          description: '流水线动作：status(查询状态与检查点) | run(启动) | resume(断点恢复) | visualize(导出Mermaid计算图)',
        },
        project_root: {
          type: 'string',
          description: '项目目录根路径（默认为输入文件所在目录或当前工作区）',
        },
        input_path: {
          type: 'string',
          description: '原材料文件或目录路径（run 时使用；将在输入文件所在处生成 self 文件夹，且所有制成文件强制位于 self 文件夹中）',
        },
        title: {
          type: 'string',
          description: '学案主标题（run 或 resume 时提供）',
        },
        profile: {
          type: 'string',
          enum: ['self_study', 'teaching', 'classroom'],
          description: '交付模式：self_study(自主学习模式，默认，不设时长上限) | teaching(课堂教学模式，保留课时预算上限) | classroom(同 teaching)',
        },
        lesson_minutes: {
          type: 'integer',
          description: '单课总预算时长(分钟，仅 teaching/classroom 模式生效，默认 90)',
        },
        format: {
          type: 'string',
          enum: ['both', 'pdf', 'html'],
          description: '输出格式：both(同时输出 PDF 与 HTML) | pdf(仅 XeLaTeX PDF) | html(仅 HTML 卡片)，默认为 both',
        },
        thread_id: {
          type: 'string',
          description: '会话线程 ID（默认为 qchain_session）',
        },
        execution_profile: {
          type: 'string',
          enum: ['legacy', 'faithful_fast'],
          description: '执行策略（与内容策略 content_policy 无关）：legacy(默认，旧执行图) | faithful_fast(轻量执行图；接通前显式报未实现，绝不静默回退)。resume 时以检查点冻结版本为准，本参数不得覆盖已冻结值。',
        },
        token: {
          type: 'string',
          description: 'Phase 0 断点恢复令牌（resume 时使用；必须由真人用户提供，严禁 Agent 自行读取 .langgraph/phase0_token.txt）',
        },
      },
      required: ['action'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string' },
          success: { type: 'boolean' },
          code: { type: 'number' },
          output: { type: 'string' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.output || (value.success ? '操作执行成功' : '操作执行失败'),
      }],
    },
    async execute(args) {
      // 确定输入位置与基准工程目录
      const baseDir = resolveCurrentProjectDir(ctx, args.input_path || args.project_root);
      const targetInput = args.input_path
        ? (path.isAbsolute(args.input_path) ? args.input_path : path.resolve(baseDir, args.input_path))
        : '';
      let proj = args.project_root
        ? (path.isAbsolute(args.project_root) ? args.project_root : path.resolve(baseDir, args.project_root))
        : '';
      if (!proj) {
        if (targetInput) {
          try {
            proj = fs.existsSync(targetInput) && fs.statSync(targetInput).isDirectory()
              ? targetInput
              : path.dirname(targetInput);
          } catch (_) {
            proj = path.dirname(targetInput);
          }
        } else {
          // A14：无参 run/resume 时把 --project 指向**插件自身目录**会让
          // runner 去 C:\...\dsh-qchain\self 里找流水线（那里只有脚本），
          // resume 于是对一份空工作区从头跑。缺省改取当前工程目录
          // （cwd → workspaceRegistry → cwd），至少落在用户真正的工作区上。
          proj = resolveCurrentProjectDir(ctx);
        }
      }

      const threadId = args.thread_id || 'qchain_session';
      const runnerScript = path.join(engineRoot, 'workflow', 'runner.py');
      const cliArgs = [runnerScript, '--project', proj, '--thread-id', threadId];

      // 强制在输入文件所在处生成 self 文件夹并初始化脚手架
      const setupScript = path.join(engineRoot, 'scripts', 'setup_self.py');
      if (args.action === 'run' || args.action === 'resume') {
        const setupArgs = [setupScript, targetInput || proj];
        if (args.title) setupArgs.push('--title', args.title);
        if (args.profile) setupArgs.push('--profile', args.profile);
        if (args.format) setupArgs.push('--format', args.format);
        if (args.lesson_minutes !== undefined && args.lesson_minutes !== null) {
          setupArgs.push('--lesson-minutes', String(args.lesson_minutes));
        }
        await runPython(setupArgs, proj, 60000, pythonBin);
      }

      if (args.action === 'status') {
        cliArgs.push('--status');
        if (targetInput) cliArgs.push('--input', targetInput);
        const res = await runPython(cliArgs, proj, 60000, pythonBin);
        const combined = [res.stdout, res.stderr].filter(Boolean).join('\n');
        return {
          action: 'status',
          success: res.success,
          code: res.code,
          output: combined || (res.success ? '流水线无活跃状态' : `查询状态失败: ${res.error}`),
        };
      }

      if (args.action === 'visualize') {
        cliArgs.push('--visualize');
        const res = await runPython(cliArgs, proj, 60000, pythonBin);
        return {
          action: 'visualize',
          success: res.success,
          code: res.code,
          output: res.stdout || res.stderr || '无法导出计算图',
        };
      }

      if (args.action === 'run') {
        cliArgs.push('--run');
        if (targetInput) cliArgs.push('--input', targetInput);
        if (args.title) cliArgs.push('--title', args.title);
        if (args.profile) cliArgs.push('--profile', args.profile);
        if (args.format) cliArgs.push('--format', args.format);
        if (args.lesson_minutes !== undefined && args.lesson_minutes !== null) {
          cliArgs.push('--lesson-minutes', String(args.lesson_minutes));
        }
        // 任务10：执行策略显式透传（缺省由 runner 按 config.yaml/legacy 解析）
        if (args.execution_profile) {
          cliArgs.push('--execution-profile', args.execution_profile);
        }

        const res = await runPython(cliArgs, proj, 240000, pythonBin);
        const combined = [res.stdout, res.stderr].filter(Boolean).join('\n');
        return {
          action: 'run',
          success: res.success,
          code: res.code,
          output: combined || (res.success ? '流水线启动完成' : `流水线运行出错: ${res.error}`),
        };
      }

      if (args.action === 'resume') {
        cliArgs.push('--resume');
        if (targetInput) cliArgs.push('--input', targetInput);
        if (args.title) cliArgs.push('--title', args.title);
        if (args.profile) cliArgs.push('--profile', args.profile);
        if (args.format) cliArgs.push('--format', args.format);
        if (args.lesson_minutes !== undefined && args.lesson_minutes !== null) {
          cliArgs.push('--lesson-minutes', String(args.lesson_minutes));
        }

        const runnerEnv = {};
        if (args.token) {
          runnerEnv.QCHAIN_PHASE0_TOKEN = args.token;
        }
        // 任务10：resume 只透传"请求值"，runner 会先读检查点冻结版本并校验；
        // 不一致时显式拒绝（EXECUTION_PROFILE_OVERRIDE_REJECTED），不静默换图。
        if (args.execution_profile) {
          cliArgs.push('--execution-profile', args.execution_profile);
        }

        const res = await runPython(cliArgs, proj, 240000, pythonBin, runnerEnv);
        const combined = [res.stdout, res.stderr].filter(Boolean).join('\n');
        return {
          action: 'resume',
          success: res.success,
          code: res.code,
          output: combined || (res.success ? '流水线已从断点恢复' : `断点恢复失败: ${res.error}`),
        };
      }

      return {
        action: args.action,
        success: false,
        code: 1,
        output: `未知 action: ${args.action}`,
      };
    },
  });

  // 2. 注册 Tool: qchain_gate
  ctx.tools.register({
    name: 'qchain_gate',
    description: '运行题链流水线质量与安全门禁校验（pedagogy, numbering, render, fidelity, semantic, scope, provenance, budget, classroom_plan, shuffle, build, check_html, compare, dialogue, review_pack, agent_audit, delivery, test_runtime 或 all）。',
    parameters: {
      type: 'object',
      properties: {
        gate: {
          type: 'string',
          enum: [
            'all',
            'pedagogy',
            'numbering',
            'render',
            'fidelity',
            'semantic',
            'scope',
            'provenance',
            'budget',
            'classroom_plan',
            'shuffle',
            'build',
            'check_html',
            'compare',
            'dialogue',
            'review_pack',
            'agent_audit',
            'delivery',
            'test_runtime',
          ],
          description: '待校验门禁项，all 表示运行关键门禁汇总检查',
        },
        target_dir: {
          type: 'string',
          description: '检查目标工作区路径（强制指向 self/ 文件夹；若传入父目录或输入文件路径则自动解析到其 self/ 子目录）',
        },
      },
      required: ['gate'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          gate: { type: 'string' },
          passed: { type: 'boolean' },
          report: { type: 'string' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.report,
      }],
    },
    async execute(args) {
      const proj = resolveEngineRoot(engineRoot);
      const baseDir = resolveCurrentProjectDir(ctx, args.target_dir);
      // 强制规范化待校验的目标工作区，确保门禁始终指向 self 文件夹
      let rawTarget = args.target_dir
        ? (path.isAbsolute(args.target_dir) ? args.target_dir : path.resolve(baseDir, args.target_dir))
        : '';
      if (!rawTarget) {
        // A14：隐式回退按「工作区形态」挑选，而不是 cwd 下碰巧有个 self 就用。
        const implicit = [
          path.join(baseDir, 'self'),
          path.join(process.cwd(), 'self'),
          path.join(proj, 'self'),
        ].filter((c) => looksLikeSelfWorkspace(c));
        rawTarget = implicit[0] || path.join(baseDir, 'self');
      } else {
        if (fs.existsSync(rawTarget) && !fs.statSync(rawTarget).isDirectory()) {
          rawTarget = path.join(path.dirname(rawTarget), 'self');
        } else if (path.basename(rawTarget) !== 'self') {
          const subSelf = path.join(rawTarget, 'self');
          rawTarget = subSelf;
        }
      }
      const targetDir = rawTarget;

      const reports = [];
      let allPassed = true;

      // timeoutMs 必须按脚本实际耗时给足：deliver.py 内部会整轮重跑
      // build.py（双版 XeLaTeX 编译），GR.4 项目实测 4~6 分钟；早期把全部
      // 门禁统一压到 120s，导致 qchain_gate all 的交付门禁恒被超时击杀、
      // 报出与盘面无关的假 FAIL（GR.4 排障实证）。慢门禁一律放宽。
      const runScript = async (name, scriptRel, scriptArgs = [], note = '', timeoutMs = 120000) => {
        const fullArgs = [path.join(engineRoot, scriptRel), ...scriptArgs];
        const res = await runPython(fullArgs, proj, timeoutMs, pythonBin);
        const pass = res.code === 0;
        if (!pass) allPassed = false;
        const icon = pass ? '✔ PASS' : '✖ FAIL';
        const msg = [res.stdout, res.stderr].filter(Boolean).join('\n') || (pass ? '门禁通过' : '门禁未通过');
        const noteLine = note ? `${note}\n` : '';
        reports.push(`[${icon}] ${name}:\n${noteLine}${msg}`);
      };

      const gateHandlers = {
        pedagogy: () => {
          // 必须落盘 --json 报告（BUG #1）：与 build_nodes.run_gate_script 行为
          // 对齐，避免命令行 PASS 与盘面陈旧/失败证据脱节。
          const pedReport = path.join(targetDir, '.pd', 'reports', 'check_pedagogy.json');
          try {
            fs.mkdirSync(path.dirname(pedReport), { recursive: true });
          } catch (_) {}
          return runScript('教学语义与认知门禁 (check_pedagogy.py)', 'scripts/check_pedagogy.py',
            [targetDir, '--json', pedReport]);
        },
        numbering: () => runScript('编号与切片结构检查 (check_numbering.py)', 'scripts/check_numbering.py', [targetDir]),
        render: () => {
          // 渲染质量门禁（audit §19，GR.4 乱码/选项空白缺陷复盘）：
          // HTML 双重转义 / center·tabular 压平 / .qc-math 段 $ 不成对 /
          // KaTeX 宏白名单 / 选项汉字守恒 / KaTeX 本地自足，以及 PDF 日志
          // Missing character、U+FFFD 不可见字形、空选项组（）。结构门禁
          // 全 PASS 也挡不住这类“渲染级”失败，故单列硬门禁。
          // 需在 build.py 之后运行（读 *.html / *.pdf / *.log）。
          const rndReport = path.join(targetDir, '.pd', 'reports', 'check_render.json');
          try {
            fs.mkdirSync(path.dirname(rndReport), { recursive: true });
          } catch (_) {}
          return runScript('渲染质量门禁 (check_render.py)', 'scripts/check_render.py',
            [targetDir, '--json', rndReport]);
        },
        fidelity: () => {
          // 来源忠实性确定性门禁（audit §18.6）：source coverage /
          // output grounding / order / novelty / extension / exclusion /
          // approval 七项检查。
          const fidReport = path.join(targetDir, '.pd', 'reports', 'check_fidelity.json');
          try { fs.mkdirSync(path.dirname(fidReport), { recursive: true }); } catch (_) {}
          return runScript('来源忠实性门禁 (check_fidelity.py)', 'scripts/check_fidelity.py',
            [targetDir, '--json', fidReport]);
        },
        semantic: () => {
          // 语义审读一致性门禁（audit §6/§7 semantic_gate）：Writer→Checker→Gate
          // 的 Gate 层——缺审读记录/候选摘要过期/存在 blocking issue 一律 FAIL。
          const semReport = path.join(targetDir, '.pd', 'reports', 'check_semantic.json');
          try { fs.mkdirSync(path.dirname(semReport), { recursive: true }); } catch (_) {}
          return runScript('语义审读门禁 (check_semantic.py)', 'scripts/check_semantic.py',
            [targetDir, '--json', semReport]);
        },
        scope: () => {
          // 范围与预算一致性门禁（audit §7 scope_gate）：生效配置 vs Phase 0
          // 冻结合同（交付模式/课时/内容策略）零漂移，未批准范围变更阻断。
          const scopeReport = path.join(targetDir, '.pd', 'reports', 'check_scope.json');
          try { fs.mkdirSync(path.dirname(scopeReport), { recursive: true }); } catch (_) {}
          return runScript('范围与预算一致性门禁 (check_scope.py)', 'scripts/check_scope.py',
            [targetDir, '--json', scopeReport]);
        },
        provenance: () => {
          // 派发血缘门禁（audit §7 provenance_gate）：writer attempt 认证当前
          // 切片摘要 + checker 链完整 + 写域白名单 + qid 规划登记。
          const provReport = path.join(targetDir, '.pd', 'reports', 'check_provenance.json');
          try { fs.mkdirSync(path.dirname(provReport), { recursive: true }); } catch (_) {}
          return runScript('派发血缘门禁 (check_provenance.py)', 'scripts/check_provenance.py',
            [targetDir, '--json', provReport]);
        },
        budget: () => {
          const budReport = path.join(targetDir, '.pd', 'reports', 'check_budget.json');
          try { fs.mkdirSync(path.dirname(budReport), { recursive: true }); } catch (_) {}
          return runScript('教学预算与长链密度门禁 (check_budget.py)', 'scripts/check_budget.py',
            [targetDir, '--stage', 'delivery', '--json', budReport]);
        },
        classroom_plan: () => runScript('检查翻转课堂计划卡 (gen_classroom_plan.py)', 'scripts/gen_classroom_plan.py', [targetDir, '--check']),
        shuffle: () => {
          // 只读核对（--verify），严禁 apply 模式改写切片源文件；
          // 落盘 --json 报告与盘面证据对齐。
          const shuReport = path.join(targetDir, '.pd', 'reports', 'shuffle_options.json');
          try {
            fs.mkdirSync(path.dirname(shuReport), { recursive: true });
          } catch (_) {}
          return runScript('选择题选项随机打乱核对（只读 verify，不改写源文件） (shuffle_options.py)',
            'scripts/shuffle_options.py', [targetDir, '--verify', '--json', shuReport]);
        },
        build: () => runScript('双源排版构建检查 (build.py)', 'scripts/build.py', ['--check-env', targetDir]),
        check_html: () => {
          const htmlReport = path.join(targetDir, '.pd', 'reports', 'check_html.json');
          try { fs.mkdirSync(path.dirname(htmlReport), { recursive: true }); } catch (_) {}
          return runScript('HTML 卡片结构与渲染检查 (check_html.py)', 'scripts/check_html.py',
            [targetDir, '--json', htmlReport]);
        },
        compare: () => runScript(
          '学生版防泄露版本比对 (compare_versions.py)',
          'scripts/compare_versions.py', ['--diagnostic', targetDir],
          '⚠ 诊断模式（已跳过部分核对）：本项以 --diagnostic 运行，已跳过源级计数与日志核对，结果不得按完整通过解读。'),
        dialogue: () => {
          const diaReport = path.join(targetDir, '.pd', 'reports', 'check_dialogue.json');
          try { fs.mkdirSync(path.dirname(diaReport), { recursive: true }); } catch (_) {}
          return runScript('课堂对话层结构与泄露 (check_dialogue.py)', 'scripts/check_dialogue.py', [targetDir, '--json', diaReport]);
        },
        review_pack: () => runScript('复核材料包生成 (make_review_pack.py)', 'scripts/make_review_pack.py', [targetDir]),
        agent_audit: () => {
          // 多 Agent 证据链审计（q_audit.md）：派发单 ↔ .pd/agent-calls 证据台账
          // 逐单对账，识别主 Agent 单会话直写与创审分离违规。
          const audReport = path.join(targetDir, '.pd', 'reports', 'check_agent_audit.json');
          try {
            fs.mkdirSync(path.dirname(audReport), { recursive: true });
          } catch (_) {}
          return runScript('多 Agent 证据链审计 (check_agent_audit.py)', 'scripts/check_agent_audit.py',
            [targetDir, '--json', audReport]);
        },
        delivery: () => runScript(
          '交付门禁汇总（dry-run，只检查不发布 release/） (deliver.py)',
          'scripts/deliver.py', [targetDir, '--dry-run'],
          '⚠ 干跑模式（--dry-run）：执行全部交付检查但不真实发布 release/。',
          900000),
        test_runtime: () => runScript('运行时契约自动化套件 (test_runtime.py)', 'scripts/test_runtime.py', [], '', 600000),
      };

      if (args.gate === 'all') {
        await gateHandlers.pedagogy();
        await gateHandlers.numbering();
        await gateHandlers.fidelity();
        await gateHandlers.semantic();
        await gateHandlers.scope();
        await gateHandlers.provenance();
        await gateHandlers.budget();
        await gateHandlers.classroom_plan();
        await gateHandlers.dialogue();
        await gateHandlers.shuffle();
        await gateHandlers.build();
        await gateHandlers.check_html();
        // 渲染质量门禁须在 build/check_html 之后（读 *.html / *.pdf / *.log）
        await gateHandlers.render();
        await gateHandlers.compare();
        await gateHandlers.review_pack();
        await gateHandlers.agent_audit();
        await gateHandlers.delivery();
        await gateHandlers.test_runtime();
      } else if (gateHandlers[args.gate]) {
        await gateHandlers[args.gate]();
      } else {
        reports.push(`[?] 门禁 ${args.gate}: 未知门禁项`);
        allPassed = false;
      }

      const summary = `=== 自学案流水线门禁报告 (${args.gate}) ===\n总体结论: ${allPassed ? '全部通过 (ALL PASS)' : '存在未通过或告警项'}\n\n` + reports.join('\n\n');
      return {
        gate: args.gate,
        passed: allPassed,
        report: summary,
      };
    },
  });

  // 2.4 注册 Tool: qchain_progress（audit §14 UI 进度指标）
  //   只读盘面证据的确定性进度快照：阶段 / scope 完成度 / 队列深度 /
  //   运行中 / 缓存条目 / 预计剩余。UI 可每 1-2 秒轮询，零副作用。
  ctx.tools.register({
    name: 'qchain_progress',
    description: '查询题链流水线派发进度快照（阶段 / scope 完成度 / 队列 / 运行中 / 缓存命中 / 预计剩余秒数）。只读 .pd/ 盘面证据（run.json + 派发计划 + 派发单 + 证据台账 + 缓存条目），可高频轮询，零副作用。',
    parameters: {
      type: 'object',
      properties: {
        self_dir: {
          type: 'string',
          description: 'self 工作区目录（缺省探测：cwd/self → workspaceRegistry[0]/self）',
        },
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          success: { type: 'boolean' },
          stage: { type: 'string' },
          report: { type: 'string' },
          snapshot: { type: 'object' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.report }],
    },
    async execute(args) {
      const baseDir = resolveCurrentProjectDir(ctx, args.self_dir);
      const candidates = [];
      if (args.self_dir) {
        candidates.push(path.isAbsolute(args.self_dir)
          ? path.resolve(args.self_dir)
          : path.resolve(process.cwd(), args.self_dir));
      }
      candidates.push(path.join(process.cwd(), 'self'));
      try {
        const registry = ctx?.get?.('workspaceRegistry') || ctx?.workspaceRegistry;
        if (registry && typeof registry.list === 'function') {
          for (const entry of (registry.list() || [])) {
            if (entry && entry.path) candidates.push(path.join(entry.path, 'self'));
          }
        }
      } catch (_) {}
      candidates.push(path.join(baseDir, 'self'));
      let selfDir = '';
      for (let i = 0; i < candidates.length; i++) {
        const c = candidates[i];
        try {
          if (!fs.existsSync(c) || !fs.statSync(c).isDirectory()) continue;
          // A14：隐式候选必须真的是 qchain 工作区（cwd 下同名目录不算）
          if (i > 0 && !looksLikeSelfWorkspace(c)) continue;
          selfDir = c;
          break;
        } catch (_) {}
      }
      if (!selfDir) {
        return {
          success: false,
          stage: '',
          report: '❌ 未找到 self 工作区目录（请指定 self_dir）',
          snapshot: null,
        };
      }
      const res = await runPython(
        [path.join(engineRoot, 'scripts', 'progress.py'), selfDir, '--json',
         path.join(selfDir, '.pd', 'reports', 'progress.json')],
        selfDir, 60000, pythonBin);
      const out = [res.stdout, res.stderr].filter(Boolean).join('\n').trim();
      let snapshot = null;
      try {
        const snapPath = path.join(selfDir, '.pd', 'reports', 'progress.json');
        if (fs.existsSync(snapPath)) {
          snapshot = JSON.parse(fs.readFileSync(snapPath, 'utf8'));
        }
      } catch (_) {}
      return {
        success: res.success,
        stage: snapshot && snapshot.stage ? snapshot.stage : '',
        report: out || (res.success ? '进度查询完成' : '进度查询失败'),
        snapshot,
      };
    },
  });

  // 2.4 注册 Tool: qchain_approve / qchain_defer（N1/N2 人工台账写入通路）
  //   背景：.pd/approvals.json 原先只有 check_fidelity 在读、**没有任何写入者**，
  //   faithful 模式下 BRIDGE/EXTENSION 永远拿不到批准 → 返修子 Agent 每轮新写
  //   的桥接都被判 FIDELITY_BRIDGE_UNAPPROVED，形成"修复-复发"死循环。
  //   延期台账同理：advisory 会被逐轮升级成 blocking，而"源材料没给这个口径"
  //   一类问题无解，只能由人拍板延期。
  ctx.tools.register({
    name: 'qchain_approve',
    description: '登记人工批准（.pd/approvals.json）：faithful 模式下 BRIDGE / EXTENSION 必须由真人批准才能留在正文，'
      + '否则 check_fidelity 每轮都判 FIDELITY_BRIDGE_UNAPPROVED / FIDELITY_EXTEND_UNAPPROVED。'
      + '★ approved_by 必须是真人的姓名或称呼：严禁填 Agent 名（会被 ledger.py 拒绝）。'
      + '先向用户说明要批准什么、为什么（reason 必填，会写进台账），拿到明确同意与署名后再调用本工具。'
      + 'action=list 可查看现有批准与延期；action=revoke 撤销（撤销后该批准立即失效）。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['approve', 'revoke', 'list'],
          description: 'approve(登记批准，默认) | revoke(撤销一条) | list(查看台账)',
        },
        self_dir: {
          type: 'string',
          description: 'self 工作区目录（缺省探测：cwd/self → workspaceRegistry[0]/self）',
        },
        kind: {
          type: 'string',
          enum: ['bridge', 'extension'],
          description: 'approve 必填：bridge(补充桥接说明) | extension(在原文之外扩展内容)',
        },
        scope: {
          type: 'string',
          description: 'approve 必填：批准针对的小节 scope（如 sec1；source-map 的 BRIDGE 行按 scope 匹配）',
        },
        approval_id: {
          type: 'string',
          description: 'approve 可选：批准 id（正文 % bridge: <id> 锚点要用同一个 id；缺省自动生成 BR-<scope>-NN / EXT-<scope>-NN）',
        },
        qid: { type: 'string', description: 'approve 可选：关联题号' },
        reason: {
          type: 'string',
          description: 'approve 必填：批准理由（写给未来的自己看：为什么这不违背忠实性）',
        },
        approved_by: {
          type: 'string',
          description: 'approve 必填：真人批准人姓名/称呼（由 ask_user_question 取得；Agent 标识会被拒）',
        },
        target_id: {
          type: 'string',
          description: 'revoke 必填：要撤销的台账条目 id（BR-… / EXT-… / DF-…）',
        },
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          success: { type: 'boolean' },
          self_dir: { type: 'string' },
          output: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.output }],
    },
    async execute(args) {
      const selfDir = resolveSelfWorkspaceDir(ctx, args.self_dir);
      const action = String(args.action || 'approve');
      let argv;
      if (action === 'revoke') {
        if (!args.target_id) {
          return { success: false, self_dir: selfDir, output: '❌ revoke 需要 target_id' };
        }
        argv = ['revoke', '--id', String(args.target_id), '--by', String(args.approved_by || '')];
      } else if (action === 'list') {
        argv = ['list'];
      } else {
        argv = ['approve',
          '--kind', String(args.kind || ''),
          '--scope', String(args.scope || ''),
          '--reason', String(args.reason || ''),
          '--approved-by', String(args.approved_by || '')];
        if (args.approval_id) argv.push('--id', String(args.approval_id));
        if (args.qid) argv.push('--qid', String(args.qid));
      }
      const res = await runLedger(ctx, selfDir, argv);
      return { success: res.success, self_dir: selfDir, output: res.output };
    },
  });

  ctx.tools.register({
    name: 'qchain_defer',
    description: '登记/撤销 advisory 延期（.pd/deferrals.json）：把"无解但有理由"的审读 advisory 保持为 advisory，'
      + '不再下一轮机械升级成 blocking。命中延期的 issue 在投影里带 deferred 标记，返修提示词会写明"本轮不要动它"。'
      + '★ deferred_by 必须是真人（Agent 标识会被拒）；reason 必填，且必须指定 code 或 qid（否则会误伤整节 advisory）。'
      + '只在用户明确同意"这条先不修"时调用；延期是人的决定，不是 Agent 的自我豁免。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['defer', 'revoke', 'list'],
          description: 'defer(登记延期，默认) | revoke(撤销) | list(查看台账)',
        },
        self_dir: {
          type: 'string',
          description: 'self 工作区目录（缺省探测：cwd/self → workspaceRegistry[0]/self）',
        },
        scope: { type: 'string', description: 'defer：作用范围（小节 scope；留空 = 全项目级）' },
        code: { type: 'string', description: 'defer：issue code（与 qid 至少给一个）' },
        qid: { type: 'string', description: 'defer：题号（与 code 至少给一个）' },
        reason: { type: 'string', description: 'defer 必填：为什么这条现在不修' },
        deferred_by: { type: 'string', description: 'defer 必填：真人姓名/称呼' },
        expires_at: { type: 'string', description: 'defer 可选：YYYY-MM-DD，到期自动重新阻断' },
        target_id: { type: 'string', description: 'revoke 必填：延期条目 id（DF-…）' },
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          success: { type: 'boolean' },
          self_dir: { type: 'string' },
          output: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.output }],
    },
    async execute(args) {
      const selfDir = resolveSelfWorkspaceDir(ctx, args.self_dir);
      const action = String(args.action || 'defer');
      let argv;
      if (action === 'revoke') {
        if (!args.target_id) {
          return { success: false, self_dir: selfDir, output: '❌ revoke 需要 target_id' };
        }
        argv = ['revoke', '--id', String(args.target_id), '--by', String(args.deferred_by || '')];
      } else if (action === 'list') {
        argv = ['list'];
      } else {
        argv = ['defer',
          '--reason', String(args.reason || ''),
          '--deferred-by', String(args.deferred_by || '')];
        if (args.scope) argv.push('--scope', String(args.scope));
        if (args.code) argv.push('--code', String(args.code));
        if (args.qid) argv.push('--qid', String(args.qid));
        if (args.expires_at) argv.push('--expires-at', String(args.expires_at));
      }
      const res = await runLedger(ctx, selfDir, argv);
      return { success: res.success, self_dir: selfDir, output: res.output };
    },
  });

  // 2.5 注册 Tool: qchain_dispatch（q_audit.md 多 Agent 证据链改造）
  //   AGENT_DISPATCH 中断恢复的唯一合法通道：派发单 → 宿主 subagent 子 Agent
  //   → 按白名单原子写产物 → 落证据台账 .pd/agent-calls/<dispatch_id>.json。
  //   主 Agent 单会话直写产物在该证据链下判 ok_unattested 失败（磁盘重推导
  //   拒收、agent_audit/deliver 门禁拒绝发布）。
  ctx.tools.register({
    name: 'qchain_dispatch',
    description: '把题链派发单派发给宿主 subagent 子 Agent 执行：渲染派发提示词（prompts/subagent_prompt.md 或 acceptance_prompt.md）→ 子 Agent 生成产物 → 按 request.artifacts 白名单原子写入 self/ → 落证据台账 .pd/agent-calls/<dispatch_id>.json（含 worker/provider/model/摘要）。AGENT_DISPATCH 中断载荷的 dispatch_args 即整批调用参数：同批独立小节用一次 batch_id 调用进入同一并发池（勿按 request_ref 逐条串行）；重复派发幂等（dispatch_id 原子认领，已完成项核对收据后复用）。AGENT_DISPATCH 中断恢复后必须先调本工具；全部 dispatch_id 为 ok 后才可 qchain_pipeline action=resume。',
    parameters: {
      type: 'object',
      properties: {
        request_ref: {
          type: 'string',
          description: '单个派发单 JSON 路径（相对 self_dir 或绝对路径）；与 batch_id 二选一',
        },
        batch_id: {
          type: 'string',
          description: '批次 ID：一次取回该批次全部派发单并在同一并发池内派发（优先读 .pd/dispatch-batches/<batch_id>.json 索引，兼容 .pd/dispatch-plan.json 与 .pd/dispatch-plan-semantic.json）；与 request_ref 二选一',
        },
        self_dir: {
          type: 'string',
          description: 'self 工作区目录（缺省探测：cwd/self → workspaceRegistry[0]/self）',
        },
        max_concurrency: {
          type: 'integer',
          description: '本次派发的并发槽位（正整数 1..16；0=自动）。'
            + '缺省时按 config.yaml 解析：agent_dispatch.max_concurrency > agent_max_concurrency'
            + ' > 档位表（fast=4/safe=1/budget=2/interactive=1；未配置且 run_scope 未知'
            + ' 时默认 fast/4，要串行请显式写 agent_dispatch: safe 或传 max_concurrency=1）。'
            + '这是同时活跃的请求数，不是必须派发的请求数。',
        },
      },
    },
    // 派发是并发安全操作：同一消息里的多张派发单（或多张 batch）应进入宿主
    // 并行池同时跑，而不是被当成排他障碍逐张串行（宿主 executionMode 只认
    // isConcurrencySafe；未声明即 exclusive，本插件此前从未声明，导致
    // "并行派发"实际逐张排队）。安全依据：派发单粒度有跨进程 claim 锁
    // （.pd/dispatch-claims/，'wx' 原子认领 + pid 存活裁决 + busy/接管三态）
    // + 迟到内容一律丢弃（绝不启动重叠子代理）+ 产物双阶段提交/全量回滚，
    // 不同 dispatch_id 之间无共享可写状态。
    // 必须位于工具定义顶层；放入 parameters 会使 JSON Schema 无法结构化克隆。
    isConcurrencySafe: () => true,
    output: {
      schema: {
        type: 'object',
        properties: {
          success: { type: 'boolean' },
          self_dir: { type: 'string' },
          dispatched: { type: 'integer' },
          ok: { type: 'integer' },
          failed: { type: 'integer' },
          results: { type: 'array' },
          report: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.report }],
    },
    async execute(args, exec) {
      const baseDir = resolveCurrentProjectDir(ctx, args.self_dir || args.request_ref);
      // ---- self_dir 探测：args.self_dir → cwd/self → workspaceRegistry[0]/self ----
      const selfDir = (() => {
        const candidates = [];
        if (args.self_dir) {
          candidates.push(path.isAbsolute(args.self_dir)
            ? path.resolve(args.self_dir)
            : path.resolve(process.cwd(), args.self_dir));
        }
        candidates.push(path.join(process.cwd(), 'self'));
        try {
          const registry = ctx?.get?.('workspaceRegistry') || ctx?.workspaceRegistry;
          if (registry && typeof registry.list === 'function') {
            for (const entry of (registry.list() || [])) {
              if (entry && entry.path) candidates.push(path.join(entry.path, 'self'));
            }
          }
        } catch (_) {}
        candidates.push(path.join(baseDir, 'self'));
        // A14：隐式候选必须是真工作区（cwd 下同名 self 不算）。注意不能写成
        // "i > 0"——显式 self_dir 不存在时 cwd/self 就是下标 0，那种写法会
        // 把它当成显式输入免检，A14 直接失效。
        const hasExplicit = !!args.self_dir;
        for (let i = 0; i < candidates.length; i++) {
          const c = candidates[i];
          try {
            if (!fs.existsSync(c) || !fs.statSync(c).isDirectory()) continue;
            if (!(hasExplicit && i === 0) && !looksLikeSelfWorkspace(c)) continue;
            return c;
          } catch (_) {}
        }
        return '';
      })();

      const fail = (msg) => ({
        success: false,
        self_dir: selfDir || '',
        dispatched: 0,
        ok: 0,
        failed: 0,
        results: [],
        report: `❌ ${msg}`,
      });
      if (!selfDir) {
        return fail('未找到 self/ 工作区目录（请用 self_dir 参数显式指定）。');
      }

      // ---- 解析待派发请求 -------------------------------------------------
      // 批次索引优先 / 语义计划兼容 / 绝不扫描全部历史 pending（任务20，C12.2）。
      // 选择失败一律带 code 显式返回，不静默退化为「全量 pending 派发」。
      let requests;
      try {
        requests = qdResolveRequests(selfDir, args);
      } catch (err) {
        if (err && err.code) {
          return fail(`[${err.code}] ${err.message}`);
        }
        throw err;
      }

      try {
        fs.writeFileSync(path.join(selfDir, '.pd', 'dispatch_debug.log'),
          `selfDir: ${selfDir}\nargs: ${JSON.stringify(args)}\nrequests: ${requests.length}\n`, 'utf8');
      } catch (_) {}
      if (requests.length === 0) {
        return {
          success: false,
          self_dir: selfDir,
          dispatched: 0,
          ok: 0,
          failed: 0,
          results: [],
          report: '未解析到待派发请求（request_ref/batch_id 无匹配，或 .pd/agent-requests 下无 status=pending 的派发单）。',
        };
      }

      // ---- 派发配置（与 Python 状态机同源：workflow/dispatch_policy.py） -----
      // 任务19（C12）：这里只向 Python 取**原始输入**（config.yaml 原始键、
      // run_scope、execution_profile、已冻结的 dispatch 块），并发解析由
      // scripts/dispatch_policy.mjs 完成——与 Python 解析器同一套规则，由
      // scripts/fixtures/dispatch-policy-cases.json 双向校验。
      // 配置读取/解析失败必须抛出真实原因：绝不退回「猜一个并发 2」。
      const readDispatchInputs = async () => {
        const snippet = [
          'import json, sys',
          'sys.path.insert(0, sys.argv[1])',
          'from workflow import dispatch_policy',
          'print(json.dumps(dispatch_policy.read_dispatch_inputs(sys.argv[2]),',
          '                 ensure_ascii=False))',
        ].join('\n');
        let res;
        try {
          res = await runPython(['-c', snippet, engineRoot, selfDir],
            resolveEngineRoot(engineRoot), 60000, pythonBin);
        } catch (err) {
          const reason = (err && (err.message || err.stderr || String(err))) || '未知错误';
          throw new DispatchPolicyError('CONFIG_UNREADABLE',
            `无法读取派发配置（self_dir=${selfDir}）：${reason}`);
        }
        if (res && res.exit_code !== undefined && res.exit_code !== 0) {
          throw new DispatchPolicyError('CONFIG_UNREADABLE',
            `无法读取派发配置（self_dir=${selfDir}）：${String(res.stderr || '').trim() || 'python 退出码 ' + res.exit_code}`);
        }
        const lines = String((res && res.stdout) || '').trim().split('\n').filter(Boolean);
        if (!lines.length) {
          throw new DispatchPolicyError('CONFIG_UNREADABLE',
            `无法读取派发配置（self_dir=${selfDir}）：解析脚本无输出`);
        }
        try {
          return JSON.parse(lines[lines.length - 1]);
        } catch (err) {
          throw new DispatchPolicyError('CONFIG_UNREADABLE',
            `派发配置解析结果不是合法 JSON（self_dir=${selfDir}）：${String(err && err.message || err)}`);
        }
      };
      let dispatchPolicy;
      let inputs;
      try {
        inputs = await readDispatchInputs();
        dispatchPolicy = resolveDispatchPolicy({
          config: inputs.config || {},
          runScope: inputs.run_scope || '',
          executionProfile: inputs.execution_profile || null,
          frozen: inputs.frozen || null,
          toolOverride: args.max_concurrency,
        });
      } catch (err) {
        const code = (err && err.code) || 'DISPATCH_POLICY_ERROR';
        const detail = (err && (err.detail || err.message)) || String(err);
        return {
          success: false,
          self_dir: selfDir,
          dispatched: 0,
          ok: 0,
          failed: 0,
          results: [],
          report: `派发配置非法 [${code}]: ${detail}\n`
            + '（修复 self/config.yaml 后重试：dispatch_contract_version 只能是 1/2；'
            + 'agent_dispatch.max_concurrency 与 agent_max_concurrency 不能同时为正且不一致；'
            + '并发值只能是 0（自动）或 1..16 的整数）',
        };
      }
      const maxConcurrency = dispatchPolicy.effective_concurrency;
      const modelMap = (inputs.model_map && typeof inputs.model_map === 'object')
        ? inputs.model_map : {};
      // 路由：完整 stage → 最长已配置前缀 → 更短前缀 → DEFAULT（见 scripts/dispatch_runtime.mjs）
      const stageRoute = (stage) => resolveStageRoute(stage, modelMap);

      // ---- 宿主 subagent 工具解析 -----------------------------------------
      const subagentDef = (() => {
        try {
          const tools = ctx?.tools || ctx?.get?.('tools');
          if (tools && typeof tools.get === 'function') {
            const def = tools.get('subagent');
            if (def && typeof def.execute === 'function') return def;
          }
        } catch (_) {}
        return null;
      })();
      // ---- 同步激活桥接 ---------------------------------------------------
      // 宿主 `subagent` 工具是「异步激活」语义：execute 立刻返回
      // `started subagent <id>`（output schema = {kind:'activation', subagentId}），
      // 最终产物只通过 settlement 消息回流，工具返回值里没有任何正文。
      // 派发单却必须在同一次调用中拿到产物才能落盘 + 记台账，因此这里直连
      // subagents 服务：startActivation(delivery:'caller') → await activation.result。
      // delivery='caller' 不向父会话注入 settlement 消息（避免重复占用上下文）。
      const resolveSubagentsService = () => {
        try {
          const svc = ctx?.get?.('subagents');
          if (svc && typeof svc.startActivation === 'function') return svc;
        } catch (_) {}
        try {
          const direct = ctx?.subagents;
          if (direct && typeof direct.startActivation === 'function') return direct;
        } catch (_) {}
        return null;
      };
      const startActivationBridge = async (subArgs, attemptExec) => {
        const svc = resolveSubagentsService();
        if (!svc) return null;
        const parent = attemptExec && attemptExec.agent;
        if (!parent) return null;
        const request = {
          prompt: [{ type: 'text', text: String(subArgs && subArgs.prompt ? subArgs.prompt : '') }],
          parent,
        };
        if (subArgs && subArgs.cwd) request.cwd = subArgs.cwd;
        // 子 Agent 默认继承父级的 maxTokens；产物动辄 2–5 万字符（两个切片合写时更多），
        // 验收类任务还有长篇推理（reasoning）与两件 JSON 产物，因此这里显式抬高出参上限，
        // 默认 65536：实测 32768 时 SCNet-Max 的推理链（~75K 字符）会把最终 JSON 挤出预算、
        // 输出被 maxTokens 截断在 JSON 中段（attempt raw 尾部落在 md 表格行中间）。
        const budget = Number(process.env.QCHAIN_SUBAGENT_MAX_TOKENS || 0);
        request.agentOptions = { maxTokens: Number.isFinite(budget) && budget > 0 ? budget : 65536 };
        if (subArgs && subArgs.provider && subArgs.model) {
          request.agentOptions.provider = subArgs.provider;
          request.agentOptions.model = subArgs.model;
        }
        // 修复 N3：记「生效路由」。宿主在 startActivation 内部用
        // resolveChildAgentOptions 决定子 Agent 的路由：请求里没给 provider/model
        // 时**继承父会话当前路由**，而父会话的模型可以在会话中途被切换。因此
        // 「请求了什么」与「实际用了什么」是两回事——旧台账只记请求值，漂移到
        // 一个慢模型（首 token 890s、连续三次 900s 超时零输出）时无法归因。
        // 这里在结果落地后、dispose 之前读子 Agent 会话的 requestHeader().config，
        // 作为生效路由记进台账（读不到就记 null，绝不猜）。
        const captureEffectiveRoute = (activation) => {
          try {
            const agent = activation && activation.handle && activation.handle.agent;
            if (!agent || !agent.session || typeof agent.session.requestHeader !== 'function') return null;
            const header = agent.session.requestHeader();
            const cfg = header && header.config;
            if (!cfg || (!cfg.provider && !cfg.model)) return null;
            return {
              provider: cfg.provider ? String(cfg.provider) : null,
              model: cfg.model ? String(cfg.model) : null,
              reasoning_effort: cfg.reasoningEffort ? String(cfg.reasoningEffort) : null,
            };
          } catch (_) {
            return null;
          }
        };
        let lastErr = null;
        for (const providerName of ['spawn', 'fork']) {
          try {
            if (typeof svc.getProvider === 'function' && !svc.getProvider(providerName)) continue;
          } catch (_) { /* 查询失败时按可试处理 */ }
          try {
            const activation = await svc.startActivation({
              provider: providerName,
              label: (subArgs && subArgs.description) || 'qchain subagent',
              request,
              signal: attemptExec && attemptExec.signal,
              delivery: 'caller',
            });
            let result;
            let effective = null;
            try {
              result = await activation.result;
              effective = captureEffectiveRoute(activation);
            } finally {
              try {
                if (typeof activation.dispose === 'function') await activation.dispose();
              } catch (_) {}
            }
            return {
              subagentId: String(activation.childId || ''),
              output: (result && Array.isArray(result.output)) ? result.output : [],
              stopReason: (result && result.stopReason) ? String(result.stopReason) : '',
              activation_provider: providerName,
              effective_route: effective,
            };
          } catch (err) {
            lastErr = err;
          }
        }
        if (lastErr) throw lastErr;
        return null;
      };
      // 修复 N6：宿主 subagent 工具是异步激活语义——`execute` 立即返回
      // `started subagent <id>`（或 {kind:'activation'}），正文只会经 settlement
      // 回流。派发链路必须同步拿到正文，因此桥接不可用时**不能**静默回落到它：
      // 旧实现这样做，结果被判成「未按 FILE 协议返回产物」（content_error，
      // 不可重试），真正的环境问题被归咎到子 Agent 头上，而且后台子 Agent 无人
      // await、仍在烧 token。这里改为识别激活形状并显式归因为环境错误。
      const isActivationResult = (value) => {
        if (!value) return false;
        if (typeof value === 'string') return /^\s*started subagent\b/i.test(value);
        if (typeof value === 'object') {
          if (value.kind === 'activation') return true;
          if (value.subagentId && value.output === undefined
            && value.content === undefined && value.text === undefined) return true;
        }
        return false;
      };
      const callSubagent = async (subArgs, attemptExec) => {
        // 优先：同步桥接（拿到子 Agent 最终 output 块）
        const bridged = await startActivationBridge(subArgs, attemptExec);
        if (bridged) {
          if (attemptExec) attemptExec.hostMode = 'bridge';
          return bridged;
        }
        if (subagentDef) {
          if (attemptExec) attemptExec.hostMode = 'async-fallback';
          return subagentDef.execute(subArgs, attemptExec);
        }
        try {
          const tools = ctx?.tools || ctx?.get?.('tools');
          if (tools && typeof tools.execute === 'function') {
            if (attemptExec) attemptExec.hostMode = 'async-fallback';
            return tools.execute({
              name: 'subagent',
              arguments: subArgs,
              agent: attemptExec && attemptExec.agent,
              signal: attemptExec && attemptExec.signal,
              callId: attemptExec && attemptExec.callId,
            });
          }
        } catch (_) {}
        const subagents = ctx?.get?.('subagents') || ctx?.subagents;
        if (subagents && typeof subagents.start === 'function') {
          if (attemptExec) attemptExec.hostMode = 'subagents-service';
          const run = await subagents.start('spawn', {
            label: subArgs.description || 'qchain subagent',
            prompt: [{ type: 'text', text: subArgs.prompt }],
            parent: attemptExec && attemptExec.agent,
            signal: attemptExec && attemptExec.signal,
          });
          const result = await run.result;
          if (typeof run.dispose === 'function') {
            try { run.dispose(); } catch (_) {}
          }
          return result;
        }
        throw new Error('宿主 subagent 工具不可用（ctx.tools.get("subagent") 与 subagents 服务均缺失）');
      };

      // ---- 单派发单执行（含重试与证据落账） --------------------------------
      const dispatchOne = async (request, laneCtx = {}) => {
        const dispatchId = String(request.dispatch_id || '');
        const artifacts = (Array.isArray(request.artifacts) ? request.artifacts : [])
          .filter((a) => typeof a === 'string' && a);
        const before = (request.before && typeof request.before === 'object')
          ? request.before : {};
        // 逐 attempt 策略：timeout_seconds / max_retries / retry_class（见 scripts/dispatch_attempts.mjs）
        const policy = resolveAttemptPolicy(request);
        const route = stageRoute(request.stage);
        // 提示词构造失败（越界产物/来源不可读/超限等）在调用子 Agent 之前显式失败，
        // 不调用宿主 subagent、不落证据台账，避免“缺上下文也派发”。
        let prompt = '';
        let promptMeta = {};
        try {
          const built = buildDispatchPrompt(request, { selfDir, engineRoot });
          prompt = built.prompt;
          promptMeta = built.meta;
        } catch (err) {
          const msg = err && err.message ? err.message : String(err);
          const code = err && err.code ? String(err.code) : 'PROMPT_BUILD_FAILED';
          appendDebugRecord(selfDir, { event: 'prompt_error', dispatch_id: dispatchId, code, error: msg });
          return {
            dispatch_id: dispatchId,
            scope: request.scope || '',
            stage: request.stage || '',
            status: 'prompt_error',
            attempts: 0,
            evidence: '',
            error: msg.startsWith(code) ? msg : `${code}: ${msg}`,
          };
        }
        let status = 'worker_error';
        let errorClass = 'unknown';
        let error = '未执行';
        let feedback = '';
        let evidenceAbs = '';
        let attempts = 0;
        let attemptId = '';
        const attemptIds = [];
        const dispatchStartedMs = Date.now();
        const dispatchStartedMono = qdMonoMs();
        // C12.4：lane/槽位观测（由协调者在批次级持有计数器，逐 attempt 只上报自己的进出）
        const observer = laneCtx.observer || null;
        const laneIndex = Number.isInteger(laneCtx.lane) ? laneCtx.lane : 0;
        const laneEnteredMono = Number.isFinite(laneCtx.lane_enter_mono)
          ? laneCtx.lane_enter_mono : qdMonoMs();
        const laneEnteredAt = Number.isFinite(laneCtx.lane_enter_ms)
          ? new Date(laneCtx.lane_enter_ms) : new Date();
        // 排队起点：请求进入就绪队列的时刻（本进程批次开始时刻），不是宿主开始推理时刻
        const queuedAtMs = Number.isFinite(laneCtx.queued_at_ms) ? laneCtx.queued_at_ms : Date.now();
        const queuedAtIso = new Date(queuedAtMs).toISOString();
        const parentRunId = String(laneCtx.parent_run_id || request.run_id || request.batch_id || '')
          || null;
        let laneWaitMs = null;
        let hostStarts = 0;
        // 输出长度截断补齐轮：上一轮只提交了部分白名单产物时，下一轮只要求补齐缺失项
        let narrowMissing = [];
        const allWritten = [];
        const allDigests = {};
        // 修复 N3：生效路由（每个 attempt 独立记；读不到就是 null）
        let effectiveRoute = null;
        let activationProvider = '';
        // 槽位是否仍被占用（取消未获宿主确认）：跨 attempt 保留，供返回值与协调者判定
        let hostSlotHeld = false;
        for (let attempt = 1; attempt <= policy.attempts_allowed; attempt += 1) {
          attempts = attempt;
          attemptId = formatAttemptId(attempt);
          attemptIds.push(attemptId);
          const startedAt = new Date();
          const startedMs = startedAt.getTime();
          const startedMono = qdMonoMs();
          // 每个 attempt 一个独立中止器：父取消与截止时刻都折叠进同一个 signal，
          // 并且该 signal 必须真的交给宿主（只在自己的 Promise.race 里停止等待是不够的）。
          const abort = createAttemptAbort({
            parentSignal: exec && exec.signal, timeoutMs: policy.timeout_ms,
          });
          const attemptExec = attemptExecWithSignal(exec, abort.signal);
          let parsed = { files: null, runId: '', error: '' };
          let hostOutcomeUsable = false;
          let cancelRequested = false;
          let cancelConfirmed = false;
          let lateOutputDiscarded = false;
          let hostCallStartedAt = null;
          let hostCallStartedMono = null;
          status = 'worker_error';
          errorClass = 'unknown';
          error = '';
          feedback = '';
          try {
            appendDebugRecord(selfDir, {
              event: 'attempt_start', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
              stage: request.stage || '', timeout_seconds: policy.timeout_seconds,
              retry_class: policy.retry_class,
              route: route.matched_key || null, provider: route.provider || null, model: route.model || null,
              prompt_chars: promptMeta.prompt_chars, prompt_sha256: promptMeta.prompt_sha256,
            });
            // C12.4：宿主调用窗口起点 = 请求真正进入宿主的时刻。
            // 这不是「供应商开始推理」的时刻（供应商排队/首 token 只有在拿到真实
            // 数据时才填，否则 null），因此台账里另设 inference_started_at = null。
            hostCallStartedAt = new Date();
            hostCallStartedMono = qdMonoMs();
            laneWaitMs = Math.max(0, Math.round(hostCallStartedMono - laneEnteredMono));
            if (observer) {
              observer.hostStarts += 1;
              hostStarts += 1;
              observer.hostInFlight += 1;
              if (observer.hostInFlight > observer.peakHostInFlight) {
                observer.peakHostInFlight = observer.hostInFlight;
              }
            }
            const attemptPrompt = narrowMissing.length > 0
              ? `${prompt}\n\n---\n【本轮硬约束 · 输出长度截断补齐轮】上一轮回复因输出长度上限被截断，只成功提交了 ${allWritten.join('、') || '（无）'}。本轮**只输出**下列尚未提交的产物，不要重复已提交的内容：${narrowMissing.join('、')}。每个文件仍以 <<<FILE:name>>> 开始、<<<END:name>>> 结束。`
              : prompt;
            const hostCall = callSubagent({
              description: `qchain ${request.stage || ''} ${request.scope || dispatchId}（${request.role || '子 Agent'}）`,
              prompt: attemptPrompt,
              run_in_background: false,
              ...(route.provider && route.model
                ? { provider: route.provider, model: route.model } : {}),
            }, attemptExec);
            const outcome = await settleWithCancel({
              abort, hostPromise: hostCall, graceMs: policy.cancel_grace_ms,
            });
            cancelRequested = outcome.cancelRequested;
            cancelConfirmed = outcome.cancelConfirmed;
            lateOutputDiscarded = outcome.lateOutputDiscarded;
            if (outcome.outcome === 'unconfirmed') {
              // 无法确认宿主已停止：停止派发，绝不启动重叠子代理
              status = 'cancellation_unconfirmed';
              errorClass = 'cancel';
              error = `派发中止未获宿主确认（${outcome.why || 'cancel'}）：已停止派发，不启动重叠子代理`;
              appendDebugRecord(selfDir, {
                event: 'attempt_cancel_unconfirmed', dispatch_id: dispatchId, attempt,
                attempt_id: attemptId, reason: outcome.why || '',
              });
            } else if (outcome.outcome === 'cancelled') {
              status = outcome.why === 'parent_cancel' ? 'cancelled' : 'timeout';
              errorClass = outcome.why === 'parent_cancel' ? 'cancel' : 'timeout';
              error = outcome.why === 'parent_cancel'
                ? '派发被父级取消，已确认宿主停止'
                : `派发超时（${policy.timeout_seconds}s），已取消宿主并确认停止`;
              appendDebugRecord(selfDir, {
                event: 'attempt_cancelled', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                reason: outcome.why || '', late_output_discarded: lateOutputDiscarded,
              });
            } else if (outcome.error) {
              const cls = classifyDispatchError(outcome.error);
              errorClass = cls.error_class;
              status = errorClass === 'content' ? 'content_error'
                : (errorClass === 'auth' ? 'auth_error'
                  : (errorClass === 'config' ? 'config_error'
                    : (errorClass === 'cancel' ? 'cancelled' : 'worker_error')));
              error = `子 Agent 调用失败: ${outcome.error && outcome.error.message ? outcome.error.message : String(outcome.error)}`;
              feedback = errorClass === 'content' ? error : '';
              appendDebugRecord(selfDir, {
                event: 'attempt_error', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                error_class: errorClass, retryable: cls.retryable, source: cls.source || '',
                error: outcome.error && outcome.error.message ? outcome.error.message : String(outcome.error),
              });
            } else if (isActivationResult(outcome.value)) {
              // N6：宿主只回了「activation 已启动」，正文不在返回值里。
              // 这不是子 Agent 的协议错误，是执行环境问题（同步桥接不可用）。
              // 必须显式归因，且不可重试（重试只会再起一个后台子 Agent）。
              hostOutcomeUsable = false;
              status = 'bridge_unavailable';
              errorClass = 'config';
              error = '子 Agent 同步桥接不可用：宿主 subagent 工具只返回了异步激活'
                + '（started subagent …），正文经 settlement 回流而本链路必须同步取回。'
                + '已放弃该 attempt；请检查 ctx.get("subagents").startActivation 是否可用'
                + '（该环境可能缺少 spawn/fork 提供者），而不是重试派发。';
              feedback = '';
              appendDebugRecord(selfDir, {
                event: 'bridge_unavailable', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                host_mode: (attemptExec && attemptExec.hostMode) || 'unknown',
                hint: '宿主 subagent 执行器为异步激活语义，无法在本调用内取回产物',
              });
            } else {
              hostOutcomeUsable = true;
              // 修复 N3：宿主返回的生效路由（桥接路径才有；回落到异步 subagent
              // 工具时宿主不回报，保持 null——绝不把「请求的」当成「生效的」）。
              effectiveRoute = (outcome.value && outcome.value.effective_route) || null;
              activationProvider = (outcome.value && outcome.value.activation_provider) || '';
              if (effectiveRoute && (route.provider || route.model)
                && (effectiveRoute.provider !== (route.provider || null)
                  || effectiveRoute.model !== (route.model || null))) {
                appendDebugRecord(selfDir, {
                  event: 'route_drift', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                  requested_provider: route.provider || null, requested_model: route.model || null,
                  effective_provider: effectiveRoute.provider, effective_model: effectiveRoute.model,
                });
              }
              parsed = parseSubagentFiles(outcome.value, request.artifacts);
              parsed.stopReason = (outcome.value && outcome.value.stopReason)
                ? String(outcome.value.stopReason) : '';
              if (parsed.error) {
                // 解析失败时把原始回复落盘（截断 400KB），否则无法定位协议偏差
                try {
                  const rawText = rawSubagentText(outcome.value);
                  if (rawText) {
                    const dir = path.join(selfDir, '.pd', 'agent-attempts', dispatchId);
                    fs.mkdirSync(dir, { recursive: true });
                    fs.writeFileSync(path.join(dir, `${attemptId}.raw.txt`),
                      rawText.slice(0, 400000), 'utf8');
                  }
                } catch (_) {}
              }
              appendDebugRecord(selfDir, {
                event: 'attempt_parsed', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                run_id: parsed.runId || '',
                stop_reason: parsed.stopReason || '',
                files: parsed.files ? Object.keys(parsed.files) : null, error: parsed.error || '',
              });
            }
          } catch (err) {
            const cls = classifyDispatchError(err);
            errorClass = cls.error_class;
            status = errorClass === 'transient' ? 'worker_error' : 'content_error';
            error = `派发异常: ${err && err.message ? err.message : String(err)}`;
            feedback = status === 'content_error' ? error : '';
            appendDebugRecord(selfDir, {
              event: 'attempt_error', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
              error_class: errorClass, error: err && err.message ? err.message : String(err),
            });
          } finally {
            // 无论成败都摘净父 signal 监听与截止定时器
            abort.cleanup();
            // C12.4：槽位释放必须「确认已释放」。取消未获宿主确认时宿主可能仍在跑，
            // 该槽位继续占用（heldSlots 累加、hostInFlight 不减），协调者据此不再起新单，
            // 绝不借一次假释放让下一单超过 cap。
            if (observer) {
              if (status === 'cancellation_unconfirmed') {
                observer.heldSlots += 1;
              } else {
                observer.hostInFlight = Math.max(0, observer.hostInFlight - 1);
              }
            }
          }
          const finishedAt = new Date();
          const finishedMono = qdMonoMs();
          const hostCallFinishedAt = hostCallStartedAt ? finishedAt : null;
          const hostSlotHeldThisAttempt = status === 'cancellation_unconfirmed';
          if (hostSlotHeldThisAttempt) hostSlotHeld = true;
          const isReview = String(request.stage || '').toUpperCase().includes('T4')
            || String(request.stage || '').toUpperCase().includes('REVIEW')
            || String(request.role || '').includes('验收');
          const worker = isReview
            ? (parsed.runId ? `subagent:reviewer-${parsed.runId}` : `subagent:reviewer-${dispatchId}`)
            : (parsed.runId ? `subagent:author-${parsed.runId}` : `subagent:author-${dispatchId}`);
          const written = [];
          const outputDigests = {};
          let outputChars = 0;
          if (!hostOutcomeUsable) {
            // 宿主结局已定（超时/取消/认证/配置/瞬时失败）：不解析产物、不落盘，
            // 迟到的 FILE 内容一律丢弃（DISP-01/DISP-02）。
          } else if (!parsed.files) {
            // 内容错误：交返修节点，不在原 prompt 上反复空转
            status = 'content_error';
            errorClass = 'content';
            error = parsed.error || '子 Agent 未返回可用产物';
            feedback = `未按 <<<FILE:...>>> 协议返回白名单产物 ${artifacts.join(', ') || '(空)'}`
              + (parsed.error ? `；解析详情: ${parsed.error}` : '');
          } else {
            status = 'ok';
            errorClass = 'none';
            error = '';
            const findContent = (rel) => {
              if (rel in parsed.files) return parsed.files[rel];
              const norm1 = rel.replace(/\\/g, '/');
              if (norm1 in parsed.files) return parsed.files[norm1];
              const norm2 = rel.replace(/\//g, '\\');
              if (norm2 in parsed.files) return parsed.files[norm2];
              const base = path.basename(norm1);
              if (base in parsed.files) return parsed.files[base];
              for (const [k, v] of Object.entries(parsed.files)) {
                if (k.replace(/\\/g, '/') === norm1 || path.basename(k.replace(/\\/g, '/')) === base) {
                  return v;
                }
              }
              return undefined;
            };

            // 修复 D3：双阶段提交。旧实现逐个 qdAtomicWrite，后一个产物失败只 break，
            // 前面的产物已经落到工作区（部分写脏），而证据台账只记 artifacts_written、
            // output_digests 为空——磁盘与台账互相矛盾。现在：全部内容先落 tmp →
            // 备份原文件 → 逐个 rename；任一环节失败全量回滚，工作区回到调用前状态。
            const pending = [];
            for (const rel of artifacts) {
              const content = findContent(rel);
              if (content === undefined) continue;
              const abs = qdSafeJoin(selfDir, rel);
              if (!abs) {
                status = 'worker_error';
                errorClass = 'content';
                error = `产物路径非法（越界或绝对路径）: ${rel}`;
                feedback = error;
                break;
              }
              let toWrite = '';
              if (typeof content === 'string') {
                if (rel.toLowerCase().endsWith('.json')) {
                  try {
                    // 验证是否已为标准 JSON
                    toWrite = JSON.stringify(JSON.parse(content), null, 2);
                  } catch (_) {
                    try {
                      // 尝试修复未转义反斜杠（如 LaTeX 宏）
                      const sanitized = content.replace(/\\([^"\\/bfnrtu])/g, '\\\\$1');
                      toWrite = JSON.stringify(JSON.parse(sanitized), null, 2);
                    } catch (_) {
                      toWrite = content;
                    }
                  }
                } else {
                  toWrite = content;
                }
              } else {
                toWrite = JSON.stringify(content, null, 2);
              }
              outputChars += typeof toWrite === 'string' ? toWrite.length : 0;
              pending.push({ rel, abs, toWrite });
            }
            if (status === 'ok' && pending.length > 0) {
              const commit = qdCommitArtifacts(selfDir, pending);
              if (commit.ok) {
                for (const item of pending) {
                  written.push(item.rel);
                  allWritten.push(item.rel);
                  outputDigests[item.rel] = commit.digests[item.rel];
                }
              } else {
                status = 'worker_error';
                errorClass = 'unknown';
                error = `产物提交失败（${commit.rolled_back ? '已全量回滚，工作区未改变' : '回滚不完整'}）: ${commit.error}`;
                appendDebugRecord(selfDir, {
                  event: 'artifact_commit_failed', dispatch_id: dispatchId, attempt,
                  rolled_back: commit.rolled_back, error: commit.error,
                  artifacts: pending.map((p) => p.rel),
                });
              }
            }
            if (status === 'ok') {
              Object.assign(allDigests, outputDigests);
              // 累计口径：补齐轮里上一轮已提交的产物不再要求重复提交
              const missing = artifacts.filter((rel) => !allWritten.includes(rel));
              if (allWritten.length === 0) {
                status = 'missing_artifacts';
                errorClass = 'content';
                error = `子 Agent 未提交任何白名单产物: ${artifacts.join(', ')}`;
                feedback = error;
              } else if (missing.length > 0) {
                status = 'missing_artifacts';
                errorClass = 'content';
                error = `部分产物缺失: ${missing.join(', ')}`;
                feedback = `返修请补齐白名单产物: ${missing.join(', ')}`;
                narrowMissing = missing;
              } else if (artifacts.every((rel) => before[rel]
                && before[rel] === outputDigests[rel])) {
                // 产物与原文件内容一致，属于合法产出，不判为错误
                status = 'ok';
                error = '';
              }
            }
            // 修复 N5：输出被截断时，不完整内容已在解析层丢弃；这里把截断事实
            // 升级为「补齐轮」（narrowMissing），而不是静默当作成功或笼统报缺失。
            if (parsed.truncated) {
              const listed = Array.isArray(parsed.truncatedFiles) ? parsed.truncatedFiles : [];
              const affected = listed.length ? listed
                : artifacts.filter((rel) => !allWritten.includes(rel));
              const note = `OUTPUT_TRUNCATED: ${affected.join(', ')}`
                + '（输出在产物中途截断，不完整内容已丢弃，未写入工作区）';
              appendDebugRecord(selfDir, {
                event: 'output_truncated', dispatch_id: dispatchId, attempt,
                truncated_files: affected, source: listed.length ? 'structure' : 'host_hint',
              });
              if (status === 'missing_artifacts' || status === 'content_error') {
                error = `${error}；${note}`;
                feedback = feedback ? `${feedback}；${note}` : note;
              } else if (status === 'ok') {
                status = 'missing_artifacts';
                errorClass = 'content';
                error = note;
                feedback = `上一轮输出被截断，请只补交以下产物（其余已完成）：${affected.join(', ')}`;
                narrowMissing = affected.slice();
              }
            }
          }
          const elapsedMs = Math.max(0, Math.round(finishedMono - startedMono));
          const ledgerStatus = status === 'ok' ? 'ok'
            : ((status === 'timeout' || status === 'cancelled' || status === 'cancellation_unconfirmed')
              ? status : 'failed');
          const retryable = status !== 'ok' && status !== 'cancellation_unconfirmed'
            && shouldRetryAttempt({
              error_class: errorClass, attempt, policy, cancel_confirmed: cancelConfirmed,
            });
          const hostWindowStart = hostCallStartedAt || startedAt;
          const hostWindowEnd = hostCallFinishedAt || finishedAt;
          const lifecyclePhase = status === 'ok' ? 'host_call_settled'
            : (hostSlotHeldThisAttempt ? 'cancel_unconfirmed'
              : ((status === 'timeout' || status === 'cancelled')
                ? 'host_call_cancelled' : 'host_call_failed'));
          if (dispatchId) {
            // 逐 attempt 台账：不可覆盖（同一 attempt 再写会生成修订号文件）
            try {
              writeAttemptRecord(selfDir, dispatchId, {
                schema_version: 1,
                attempt,
                attempt_id: attemptId,
                dispatch_id: dispatchId,
                batch_id: request.batch_id || '',
                scope: request.scope || '',
                scopes: Array.isArray(request.scopes)
                  ? request.scopes.filter((s) => typeof s === 'string' && s)
                  : [request.scope || ''].filter(Boolean),
                stage: request.stage || '',
                role: request.role || '',
                worker,
                provider: route.provider || null,
                model: route.model || null,
                route_key: route.matched_key || null,
                // N3：请求路由 vs 生效路由分开记。生效值来自子 Agent 会话的
                // requestHeader().config；桥接不可用时无从得知，一律 null。
                requested_provider: route.provider || null,
                requested_model: route.model || null,
                effective_provider: effectiveRoute ? effectiveRoute.provider : null,
                effective_model: effectiveRoute ? effectiveRoute.model : null,
                effective_reasoning_effort: effectiveRoute ? effectiveRoute.reasoning_effort : null,
                route_source: effectiveRoute ? 'host-session'
                  : ((route.provider && route.model) ? 'requested-only' : 'inherited-unknown'),
                activation_provider: activationProvider || null,
                // C12.4：started_at/finished_at 是「宿主调用窗口」的两个端点，
                // 不是供应商开始推理的时间；推理时间只有真实值时才填，否则 null。
                started_at: hostWindowStart.toISOString(),
                finished_at: hostWindowEnd.toISOString(),
                host_call_started_at: hostWindowStart.toISOString(),
                host_call_finished_at: hostWindowEnd.toISOString(),
                queued_at: queuedAtIso,
                elapsed_ms: elapsedMs,
                elapsed_clock: 'monotonic_ms',
                elapsed_wall_ms: Math.max(0, hostWindowEnd.getTime() - hostWindowStart.getTime()),
                queue_ms: laneWaitMs,
                observation_scope: 'host_call',
                inference_started_at: null,
                vendor_queue_ms: null,
                vendor_first_token_ms: null,
                vendor_inference_ms: null,
                executor_kind: dispatchPolicy.executor_kind,
                parent_run_id: parentRunId,
                child_run_id: parsed.runId || null,
                configured_concurrency: dispatchPolicy.configured_concurrency,
                effective_concurrency: maxConcurrency,
                concurrency_source: dispatchPolicy.concurrency_source,
                profile: dispatchPolicy.profile,
                capability_verified: dispatchPolicy.capability_verified,
                lane: {
                  index: laneIndex,
                  entered_at: laneEnteredAt.toISOString(),
                  exited_at: finishedAt.toISOString(),
                  wait_ms: laneWaitMs,
                },
                host_slot: hostSlotHeldThisAttempt ? 'held' : 'released',
                lifecycle: [
                  'attempt_started',
                  ...(hostCallStartedAt ? ['host_call_started'] : []),
                  lifecyclePhase,
                  status === 'ok' ? 'artifacts_written' : 'artifacts_not_written',
                  'attempt_finished',
                ],
                lifecycle_outcome: hostSlotHeldThisAttempt ? 'unfinished' : 'finished',
                status: ledgerStatus,
                error_class: errorClass || 'unknown',
                error: error || null,
                retryable,
                cancel_requested: cancelRequested,
                cancel_confirmed: cancelConfirmed,
                cancel_reason: cancelRequested ? (abort.why || null) : null,
                late_output_discarded: lateOutputDiscarded,
                prompt_chars: promptMeta.prompt_chars === undefined ? null : promptMeta.prompt_chars,
                output_chars: outputChars,
                input_digest: promptMeta.prompt_sha256 || null,
                output_digests: outputDigests,
                cached: false,
                timeout_seconds: policy.timeout_seconds,
                max_retries: policy.max_retries,
                retry_class: policy.retry_class,
                repair_feedback: feedback || null,
                artifacts_written: written,
              });
            } catch (_) {}
          }
          if (dispatchId) {
            const evidenceRec = {
              schema_version: 1,
              dispatch_id: dispatchId,
              batch_id: request.batch_id || '',
              worker,
              provider: route.provider || null,
              model: route.model || null,
              // N3：请求值与生效值分列；`provider/model` 保持历史语义（请求值），
              // 这样既有审计脚本不必改，新增字段供归因与 INV-2 路由分离核对。
              requested_provider: route.provider || null,
              requested_model: route.model || null,
              effective_provider: effectiveRoute ? effectiveRoute.provider : null,
              effective_model: effectiveRoute ? effectiveRoute.model : null,
              effective_reasoning_effort: effectiveRoute ? effectiveRoute.reasoning_effort : null,
              route_source: effectiveRoute ? 'host-session'
                : ((route.provider && route.model) ? 'requested-only' : 'inherited-unknown'),
              activation_provider: activationProvider || null,
              started_at: startedAt.toISOString(),
              finished_at: finishedAt.toISOString(),
              host_call_started_at: hostWindowStart.toISOString(),
              host_call_finished_at: hostWindowEnd.toISOString(),
              duration_ms: elapsedMs,
              duration_scope: 'host_call',
              elapsed_clock: 'monotonic_ms',
              observation_scope: 'host_call',
              inference_started_at: null,
              executor_kind: dispatchPolicy.executor_kind,
              parent_run_id: parentRunId,
              child_run_id: parsed.runId || null,
              queue_ms: laneWaitMs,
              total_elapsed_ms: Date.now() - dispatchStartedMs,
              artifacts_written: allWritten.slice(),
              output_digests: { ...allDigests },
              attempt_written: written.slice(),
              status,
              error_class: errorClass || 'unknown',
              attempts: attempt,
              attempt_id: attemptId,
              attempt_ids: [...attemptIds],
              timeout_seconds: policy.timeout_seconds,
              max_retries: policy.max_retries,
              retry_class: policy.retry_class,
              cancel_requested: cancelRequested,
              cancel_confirmed: cancelConfirmed,
              late_output_discarded: lateOutputDiscarded,
              repair_feedback: feedback || null,
              error: error || null,
              tool: 'qchain_dispatch',
            };
            try {
              const dir = path.join(selfDir, '.pd', 'agent-calls');
              fs.mkdirSync(dir, { recursive: true });
              const abs = path.join(dir, `${dispatchId}.json`);
              const tmp = `${abs}.tmp-${crypto.randomBytes(4).toString('hex')}`;
              fs.writeFileSync(tmp, JSON.stringify(evidenceRec, null, 2), 'utf8');
              fs.renameSync(tmp, abs);
              evidenceAbs = abs;
            } catch (_) {}
          }
          if (status === 'ok') break;
          // 内容类缺失（输出长度截断）允许补齐轮：只补齐缺失产物，不计入 retry_class 判定
          const narrowRound = status === 'missing_artifacts' && narrowMissing.length > 0
            && attempt < policy.attempts_allowed;
          if (status !== 'ok' && (!retryable && !narrowRound)) break;
          // 瞬时失败重试前留退避窗口（确定性，不引入随机抖动）
          await delayMs(Math.min(1000, 100 * attempt));
        }
        return {
          dispatch_id: dispatchId,
          scope: request.scope || '',
          stage: request.stage || '',
          status,
          error_class: errorClass || 'unknown',
          attempts,
          attempt_id: attemptId,
          attempt_ids: [...attemptIds],
          feedback: feedback || '',
          timeout_seconds: policy.timeout_seconds,
          retry_class: policy.retry_class,
          total_elapsed_ms: Date.now() - dispatchStartedMs,
          total_elapsed_monotonic_ms: Math.max(0, Math.round(qdMonoMs() - dispatchStartedMono)),
          queue_ms: laneWaitMs,
          host_starts: hostStarts,
          host_slot: hostSlotHeld ? 'held' : 'released',
          lane_index: laneIndex,
          evidence: evidenceAbs,
          error: error || null,
        };
      };

      // ---- 幂等认领包装（任务20，C12.2）：同一 dispatch_id 至多执行一次 --------
      const dispatchWithClaim = async (request, laneCtx = {}) => {
        const dispatchId = String(request.dispatch_id || '');
        const base = {
          dispatch_id: dispatchId,
          scope: request.scope || '',
          stage: request.stage || '',
        };
        // 修复 D1/D2：认领文件落盘前先做两道前置校验（旧实现先写 claim 后校验，
        // 非法请求会留下越界写入的文件）。任何一项不合法 ⇒ 不落盘、直接 prompt_error。
        const badArtifact = qdFirstInvalidArtifact(selfDir, request);
        if (badArtifact) {
          appendDebugRecord(selfDir, {
            event: 'claim_precheck_failed', dispatch_id: dispatchId,
            code: 'ARTIFACT_PATH_INVALID', artifact: badArtifact,
          });
          return {
            ...base,
            status: 'prompt_error',
            error_class: 'prompt',
            attempts: 0,
            attempt_id: '',
            attempt_ids: [],
            feedback: '',
            total_elapsed_ms: 0,
            evidence: '',
            error: `ARTIFACT_PATH_INVALID: 产物路径非法（绝对路径/含 .. /越出当前 self/junction 越界）：${badArtifact}`,
            host_starts: 0,
            queue_ms: null,
            host_slot: 'not_acquired',
          };
        }
        const claim = qdAcquireClaim(selfDir, request);
        if (claim.mode === 'invalid_id') {
          appendDebugRecord(selfDir, {
            event: 'claim_id_invalid', dispatch_id: dispatchId, code: claim.code,
          });
          return {
            ...base,
            status: 'prompt_error',
            error_class: 'prompt',
            attempts: 0,
            attempt_id: '',
            attempt_ids: [],
            feedback: '',
            total_elapsed_ms: 0,
            evidence: '',
            error: `${claim.code}: ${claim.detail}`,
            host_starts: 0,
            queue_ms: null,
            host_slot: 'not_acquired',
          };
        }
        if (claim.mode === 'reuse') {
          appendDebugRecord(selfDir, { event: 'claim_reuse', dispatch_id: dispatchId });
          return {
            ...base,
            status: 'ok',
            error_class: '',
            attempts: 0,
            attempt_id: '',
            attempt_ids: [],
            feedback: '',
            total_elapsed_ms: 0,
            evidence: claim.receipt_rel || '',
            error: null,
            reused: true,
            host_starts: 0,
            queue_ms: null,
            host_slot: 'not_acquired',
          };
        }
        if (claim.mode === 'busy') {
          return {
            ...base,
            status: 'dispatch_busy',
            error_class: 'DISPATCH_BUSY',
            attempts: 0,
            attempt_id: '',
            attempt_ids: [],
            feedback: '',
            total_elapsed_ms: 0,
            evidence: '',
            error: claim.detail,
            busy: true,
            host_starts: 0,
            queue_ms: null,
            host_slot: 'not_acquired',
          };
        }
        let result;
        try {
          result = await dispatchOne(request, laneCtx);
        } catch (err) {
          qdFinishClaim(selfDir, dispatchId, 'failed',
            { error: String((err && err.message) || err) });
          throw err;
        }
        const okDone = !!(result && result.status === 'ok');
        qdFinishClaim(selfDir, dispatchId, okDone ? 'done' : 'failed', {
          receipt_rel: (result && result.evidence) || '',
          output_digests: qdCurrentDigests(selfDir, request),
          request_fingerprint: qdRequestFingerprint(request),
          error: (result && result.error) || '',
        });
        return result;
      };

      const runBatchId = String(args.batch_id || (requests[0] && requests[0].batch_id) || '');

      // ---- 并发执行（信号量 ≤ max_concurrency） ----------------------------
      // C12.4：lane 进出与宿主调用在飞数由「协调者」统一计数，逐 attempt 只上报自己的
      // 窗口；进入/退出全部放在 try/finally，失败、重试、取消都不会漏计数。
      const results = new Array(requests.length);
      let cursor = 0;
      const runners = [];
      const laneCount = Math.max(1, Math.min(maxConcurrency, requests.length));
      const batchStartedMs = Date.now();
      const batchStartedMono = qdMonoMs();
      const observer = {
        hostStarts: 0,
        hostInFlight: 0,
        peakHostInFlight: 0,
        laneBusy: 0,
        peakLaneBusy: 0,
        heldSlots: 0,
      };
      const laneLog = [];
      for (let i = 0; i < laneCount; i += 1) {
        const lane = {
          index: i,
          entered_at: new Date().toISOString(),
          exited_at: null,
          requests: [],
        };
        laneLog.push(lane);
        runners.push((async () => {
          try {
            while (cursor < requests.length) {
              // 宿主槽位未确认释放时不得再起新单：否则会借一次假释放超过 cap
              if (observer.heldSlots > 0) break;
              const idx = cursor;
              cursor += 1;
              observer.laneBusy += 1;
              if (observer.laneBusy > observer.peakLaneBusy) {
                observer.peakLaneBusy = observer.laneBusy;
              }
              lane.requests.push(String((requests[idx] || {}).dispatch_id || ''));
              try {
                results[idx] = await dispatchWithClaim(requests[idx], {
                  lane: i,
                  observer,
                  lane_enter_ms: Date.now(),
                  lane_enter_mono: qdMonoMs(),
                  queued_at_ms: batchStartedMs,
                  parent_run_id: String((requests[idx] || {}).run_id || runBatchId || ''),
                });
              } catch (err) {
                results[idx] = {
                  dispatch_id: String((requests[idx] || {}).dispatch_id || ''),
                  scope: String((requests[idx] || {}).scope || ''),
                  stage: String((requests[idx] || {}).stage || ''),
                  status: 'worker_error',
                  attempts: 0,
                  evidence: '',
                  error: err && err.message ? err.message : String(err),
                  host_starts: 0,
                  queue_ms: null,
                  host_slot: 'released',
                };
              } finally {
                observer.laneBusy -= 1;
              }
            }
          } finally {
            // lane 退出（无论正常结束、异常还是 break）都必须留痕
            lane.exited_at = new Date().toISOString();
          }
        })());
      }
      await Promise.all(runners);

      // 未派发（因宿主槽位未确认释放而主动不再起单）显式记账，绝不静默吞掉
      let undispatchedCount = 0;
      for (let idx = 0; idx < requests.length; idx += 1) {
        if (results[idx] !== undefined) continue;
        undispatchedCount += 1;
        results[idx] = {
          dispatch_id: String((requests[idx] || {}).dispatch_id || ''),
          scope: String((requests[idx] || {}).scope || ''),
          stage: String((requests[idx] || {}).stage || ''),
          status: 'not_dispatched',
          error_class: 'host_slot_unconfirmed_hold',
          attempts: 0,
          attempt_id: '',
          attempt_ids: [],
          feedback: '',
          evidence: '',
          error: 'host_slot_unconfirmed_hold: 上一单取消未获宿主确认，槽位仍被占用，本单未派发',
          not_dispatched: true,
          host_starts: 0,
          queue_ms: null,
          host_slot: 'not_acquired',
        };
      }

      // 批次运行留痕（.pd/dispatch-runs/<batch_id>.json）——单协调者写，lane 不各自写
      let batchObservation = null;
      if (runBatchId) {
        const runRel = path.join('.pd', 'dispatch-runs', `${runBatchId}.json`);
        const queueSamples = results
          .map((r) => (r && typeof r.queue_ms === 'number' ? r.queue_ms : null))
          .filter((v) => v !== null);
        const declaredDependency = requests.some((r) => r
          && (r.depends_on || r.dependency || r.depends_on_scope));
        const reason = qdSerialReason({
          requestCount: requests.length,
          reusedCount: results.filter((r) => r && r.reused === true).length,
          effectiveConcurrency: maxConcurrency,
          configuredConcurrency: dispatchPolicy.configured_concurrency,
          workerMaxConcurrency: dispatchPolicy.worker_max_concurrency,
          capabilityVerified: dispatchPolicy.capability_verified,
          concurrencySource: dispatchPolicy.concurrency_source,
          profile: dispatchPolicy.profile,
          peakHostInFlight: observer.peakHostInFlight,
          undispatchedCount,
          declaredDependency,
        });
        const fingerprintBasis = JSON.stringify(requests.map((r) => [
          String((r && r.dispatch_id) || ''), String((r && r.batch_id) || ''),
        ]));
        const runRecord = {
          schema_version: 2,
          batch_id: runBatchId,
          run_id: String((requests[0] && requests[0].run_id) || '') || null,
          stage: String((requests[0] && requests[0].stage) || ''),
          request_count: requests.length,
          request_ids: requests.map((r) => String((r && r.dispatch_id) || '')),
          batch_fingerprint: crypto.createHash('sha256').update(fingerprintBasis).digest('hex'),
          fingerprint_algorithm: 'sha256',
          effective_concurrency: maxConcurrency,
          configured_concurrency: dispatchPolicy.configured_concurrency,
          concurrency_source: dispatchPolicy.concurrency_source,
          concurrency_limit: dispatchPolicy.concurrency_limit,
          profile: dispatchPolicy.profile,
          capability_source: dispatchPolicy.concurrency_source,
          capability_verified: dispatchPolicy.capability_verified,
          // 观测口径：只观测「宿主调用窗口」内的在飞数。供应商排队/首 token/推理
          // 时间属于供应商侧，未拿到真实数据一律 null（未知 ≠ 实测 0）。
          observed_scope: 'host_call',
          observed_host_in_flight: observer.peakHostInFlight,
          peak_observed_host_in_flight: observer.peakHostInFlight,
          peak_host_in_flight_source: 'coordinator_host_call_counter',
          peak_lane_occupied: observer.peakLaneBusy,
          ready_count: requests.length,
          peak_ready: Math.max(requests.length, observer.peakLaneBusy),
          peak_ready_source: 'coordinator_batch_scan',
          queue_wait_ms: {
            source: 'coordinator_lane_queue',
            observed_count: queueSamples.length,
            max_ms: queueSamples.length ? Math.max(...queueSamples) : null,
            total_ms: queueSamples.length ? queueSamples.reduce((a, b) => a + b, 0) : null,
            mean_ms: queueSamples.length
              ? Math.round(queueSamples.reduce((a, b) => a + b, 0) / queueSamples.length)
              : null,
          },
          vendor_queue_ms: null,
          vendor_first_token_ms: null,
          inference_ms: null,
          elapsed_ms: Math.max(0, Math.round(qdMonoMs() - batchStartedMono)),
          elapsed_clock: 'monotonic_ms',
          real_start_count: observer.hostStarts,
          cache_reuse_count: results.filter((r) => r && r.reused === true).length,
          slots_held_unconfirmed: observer.heldSlots,
          undispatched: undispatchedCount,
          cap_respected: observer.peakHostInFlight <= maxConcurrency,
          lanes: laneLog,
          results: results.map((r) => ({
            dispatch_id: r.dispatch_id,
            status: r.status,
            reused: r.reused === true,
            host_starts: r.host_starts || 0,
            host_slot: r.host_slot || null,
            queue_ms: typeof r.queue_ms === 'number' ? r.queue_ms : null,
            receipt_rel: r.evidence || '',
          })),
          serial_reason_code: reason.code,
          serial_reason_source: reason.source,
          serial_reason: dispatchPolicy.serial_reason,
          serial_reason_vocabulary: [...QD_SERIAL_REASON_CODES],
          receipt_refs: results
            .map((r) => (r && r.evidence) || '')
            .filter(Boolean),
          executor_kind: dispatchPolicy.executor_kind,
        };
        try {
          const runAbs = path.join(selfDir, runRel);
          fs.mkdirSync(path.dirname(runAbs), { recursive: true });
          fs.writeFileSync(runAbs, `${JSON.stringify(runRecord, null, 2)}\n`, 'utf8');
          batchObservation = {
            rel: runRel,
            observed_scope: runRecord.observed_scope,
            peak_observed_host_in_flight: runRecord.peak_observed_host_in_flight,
            peak_ready: runRecord.peak_ready,
            real_start_count: runRecord.real_start_count,
            cache_reuse_count: runRecord.cache_reuse_count,
            slots_held_unconfirmed: runRecord.slots_held_unconfirmed,
            undispatched: runRecord.undispatched,
            cap_respected: runRecord.cap_respected,
            serial_reason_code: runRecord.serial_reason_code,
            serial_reason_source: runRecord.serial_reason_source,
            elapsed_ms: runRecord.elapsed_ms,
            vendor_queue_ms: runRecord.vendor_queue_ms,
            inference_ms: runRecord.inference_ms,
          };
        } catch (_) {}
      }

      const okCount = results.filter((r) => r.status === 'ok').length;
      const lines = results.map((r) => `- [${r.status === 'ok' ? 'ok' : 'FAIL'}] `
        + `${r.dispatch_id} (${r.scope}) → ${r.status}`
        + (r.error ? `: ${r.error}` : '')
        + (r.evidence ? ` ｜ 证据: ${r.evidence}` : ''));
      // 任务19（C12）：并发口径必须可追溯——批次大小（排队请求数）与执行并发槽
      // 位是两件事，未知宿主能力记为「未核实」而不是猜成 1 或实测 0。
      const capabilityNote = dispatchPolicy.capability_verified
        ? `已核实（${dispatchPolicy.worker_max_concurrency}）`
        : '未核实（宿主 subagent 能力未知，按配置上限执行）';
      const report = [
        `=== qchain_dispatch 派发汇总（self_dir: ${selfDir}）===`,
        `派发单: ${results.length}，ok: ${okCount}，失败: ${results.length - okCount}`,
        `执行器: ${dispatchPolicy.executor_kind} ｜ 档位: ${dispatchPolicy.profile}`
          + ` ｜ 契约版本: v${dispatchPolicy.dispatch_contract_version}`
          + ` ｜ 并发来源: ${dispatchPolicy.concurrency_source}`,
        `批次大小 batch_size: ${requests.length} ｜ 配置并发: ${dispatchPolicy.configured_concurrency}`
          + ` ｜ 执行并发槽 effective_concurrency: ${maxConcurrency}`
          + ` ｜ 并发上限 concurrency_limit: ${dispatchPolicy.concurrency_limit}`
          + ` ｜ 宿主能力: ${capabilityNote}`,
        ...(batchObservation
          ? [`并发观测（口径 ${batchObservation.observed_scope}，仅宿主调用窗口）:`
            + ` 峰值在飞 ${batchObservation.peak_observed_host_in_flight}`
            + `（来源 coordinator_host_call_counter）`
            + ` ｜ ready ${batchObservation.peak_ready}`
            + ` ｜ 真实起单 ${batchObservation.real_start_count}`
            + ` ｜ 缓存复用 ${batchObservation.cache_reuse_count}`
            + ` ｜ 原因 ${batchObservation.serial_reason_code || '(无串行原因，实际并行)'}`
            + `（${batchObservation.serial_reason_source}）`
            + (batchObservation.slots_held_unconfirmed
              ? ` ｜ 未确认释放槽位 ${batchObservation.slots_held_unconfirmed}`
              : '')
            + (batchObservation.undispatched
              ? ` ｜ 未派发 ${batchObservation.undispatched}`
              : ''),
            '供应商排队 / 首 token / 模型推理时间：null（观测不到，未知不是 0）',
            `批摘要: ${batchObservation.rel}`]
          : ['并发观测: 无 batch_id，未生成批摘要（峰值原因记为 unknown，不臆造）']),
        ...lines,
        okCount === results.length
          ? '全部派发单已由子 Agent 完成并落证据台账；现在可调用 qchain_pipeline action=resume 恢复流水线。'
          : '存在失败派发单：修正后重试本工具或走返修环；切勿由主 Agent 直接撰写产物（将判 ok_unattested 失败并被 agent_audit/deliver 门禁拒绝）。',
      ].join('\n');
      return {
        success: okCount === results.length,
        self_dir: selfDir,
        dispatched: results.length,
        ok: okCount,
        failed: results.length - okCount,
        results,
        report,
        dispatch_contract_version: dispatchPolicy.dispatch_contract_version,
        executor_kind: dispatchPolicy.executor_kind,
        profile: dispatchPolicy.profile,
        batch_size: requests.length,
        configured_concurrency: dispatchPolicy.configured_concurrency,
        concurrency_source: dispatchPolicy.concurrency_source,
        concurrency_limit: dispatchPolicy.concurrency_limit,
        effective_concurrency: maxConcurrency,
        worker_max_concurrency: dispatchPolicy.worker_max_concurrency,
        capability_verified: dispatchPolicy.capability_verified,
        serial_reason: dispatchPolicy.serial_reason,
        batch_id: runBatchId || null,
        run_record_rel: batchObservation ? batchObservation.rel : null,
        observed_scope: batchObservation ? batchObservation.observed_scope : 'host_call',
        peak_observed_host_in_flight: batchObservation
          ? batchObservation.peak_observed_host_in_flight
          : null,
        peak_ready: batchObservation ? batchObservation.peak_ready : null,
        real_start_count: batchObservation ? batchObservation.real_start_count : null,
        cache_reuse_count: batchObservation ? batchObservation.cache_reuse_count : null,
        slots_held_unconfirmed: batchObservation ? batchObservation.slots_held_unconfirmed : null,
        undispatched: batchObservation ? batchObservation.undispatched : null,
        cap_respected: batchObservation ? batchObservation.cap_respected : null,
        serial_reason_code: batchObservation ? batchObservation.serial_reason_code : 'host_queue_or_unknown',
        serial_reason_source: batchObservation ? batchObservation.serial_reason_source : 'no_batch_record',
        vendor_queue_ms: null,
        inference_ms: null,
        diagnostics: dispatchPolicy.diagnostics,
      };
    },
  });

  // 3. 注册 Human Command: /qchain
  const commandsService = ctx.get('commands');
  if (commandsService) {
    commandsService.register({
      name: 'qchain',
      description: '题链流水线交互指令 (/qchain status | run | resume | gates | help)',
      input: {
        hint: 'status | run [--title <t>] [--profile <p>] [--format <both|pdf|html>] [--execution-profile <legacy|faithful_fast>] | resume [--title <t>] [--token <t>] | gates',
      },
      async handler(inv) {
        const raw = (inv.rawInput || '').trim();
        // 支持双引号包裹含空格的值（如 --title "我的 学案"）
        const parts = raw.match(/"[^"]*"|\S+/g) || [];
        const sub = (parts[0] || 'help').toLowerCase();
        const proj = resolveEngineRoot(engineRoot);
        const runnerScript = path.join(engineRoot, 'workflow', 'runner.py');

        const parseFlags = (tokens) => {
          const allowed = new Set(['--title', '--profile', '--format', '--token', '--execution-profile']);
          const opts = {};
          for (let i = 0; i < tokens.length; i++) {
            const tok = tokens[i];
            if (!tok.startsWith('--')) {
              return { error: `无法解析的参数: ${tok}（值必须跟随 --title/--profile/--format/--token/--execution-profile 出现）` };
            }
            if (!allowed.has(tok)) {
              return { error: `不支持的选项: ${tok}（仅支持 --title/--profile/--format/--token/--execution-profile）` };
            }
            const val = tokens[i + 1];
            if (val === undefined || val.startsWith('--')) {
              return { error: `选项 ${tok} 缺少取值` };
            }
            opts[tok.slice(2)] = val.replace(/^"|"$/g, '');
            i++;
          }
          if (opts.profile && !['self_study', 'teaching', 'classroom'].includes(opts.profile)) {
            return { error: `--profile 取值无效: ${opts.profile}（可选 self_study|teaching|classroom）` };
          }
          if (opts.format && !['both', 'pdf', 'html'].includes(opts.format)) {
            return { error: `--format 取值无效: ${opts.format}（可选 both|pdf|html）` };
          }
          if (opts['execution-profile'] && !['legacy', 'faithful_fast'].includes(opts['execution-profile'])) {
            return { error: `--execution-profile 取值无效: ${opts['execution-profile']}（可选 legacy|faithful_fast）` };
          }
          return { opts };
        };

        if (sub === 'help') {
          return {
            kind: 'success',
            text: [
              '📖 **题链流水线指令指南**',
              '- `/qchain status` : 查看当前题链流水线状态与检查点',
              '- `/qchain run [--title <标题>] [--profile <self_study|teaching|classroom，默认 self_study>] [--format <both|pdf|html>] [--execution-profile <legacy|faithful_fast>]` : 启动流水线',
              '- `/qchain resume [--title <标题>] [--profile <self_study|teaching|classroom，缺省沿用已确认值>] [--format <both|pdf|html>] [--execution-profile <legacy|faithful_fast>] [--token <Phase 0 令牌>]` : 从 Phase 0 断点恢复执行（令牌由真人用户提供；执行策略以检查点冻结版本为准）',
              '- `/qchain gates` : 运行关键教研质量门禁体检 (编号、打乱只读核对、构建、版本比对诊断、运行时契约)',
            ].join('\n'),
          };
        }

        if (sub === 'status') {
          const res = await runPython([runnerScript, '--status', '--project', proj], proj, 30000, pythonBin);
          return {
            kind: res.success ? 'success' : 'error',
            text: res.stdout || res.stderr || '无活跃流水线状态',
          };
        }

        if (sub === 'run' || sub === 'resume') {
          const { opts, error } = parseFlags(parts.slice(1));
          if (error) {
            return {
              kind: 'error',
              text: `❌ /qchain ${sub} 参数解析失败：${error}\n请使用 /qchain help 查看完整语法。`,
            };
          }
          if (sub === 'run' && opts.token) {
            return {
              kind: 'error',
              text: '❌ --token 仅用于 /qchain resume（Phase 0 断点恢复），run 不接受该选项。',
            };
          }
          const cliArgs = [runnerScript, `--${sub}`, '--project', proj];
          if (opts.title) cliArgs.push('--title', opts.title);
          if (opts.profile) cliArgs.push('--profile', opts.profile);
          if (opts.format) cliArgs.push('--format', opts.format);
          if (opts['execution-profile']) cliArgs.push('--execution-profile', opts['execution-profile']);
          if (sub === 'resume' && opts.token) cliArgs.push('--token', opts.token);
          const res = await runPython(cliArgs, proj, 240000, pythonBin);
          const combined = [res.stdout, res.stderr].filter(Boolean).join('\n');
          return {
            kind: res.success ? 'success' : 'error',
            text: combined || (res.success
              ? (sub === 'run' ? '流水线启动完成' : '流水线已从断点恢复')
              : `流水线执行失败 (code: ${res.code}): ${res.error || '无输出'}`),
          };
        }

        if (sub === 'gates') {
          const targetDir = fs.existsSync(path.join(proj, 'self')) ? path.join(proj, 'self') : proj;
          const pedReport = path.join(targetDir, '.pd', 'reports', 'check_pedagogy.json');
          const budReport = path.join(targetDir, '.pd', 'reports', 'check_budget.json');
          const shuReport = path.join(targetDir, '.pd', 'reports', 'shuffle_options.json');
          try { fs.mkdirSync(path.dirname(pedReport), { recursive: true }); } catch (_) {}
          const resP = await runPython([path.join(engineRoot, 'scripts/check_pedagogy.py'), targetDir, '--json', pedReport], proj, 30000, pythonBin);
          const resB = await runPython([path.join(engineRoot, 'scripts/check_budget.py'), targetDir, '--stage', 'delivery', '--json', budReport], proj, 30000, pythonBin);
          const res0 = await runPython([path.join(engineRoot, 'scripts/shuffle_options.py'), targetDir, '--verify', '--json', shuReport], proj, 30000, pythonBin);
          const res1 = await runPython([path.join(engineRoot, 'scripts/check_numbering.py'), targetDir], proj, 30000, pythonBin);
          const res2 = await runPython([path.join(engineRoot, 'scripts/compare_versions.py'), '--diagnostic', targetDir], proj, 30000, pythonBin);
          const out = [
            `[教学语义与认知门禁] (code: ${resP.code})\n${resP.stdout || resP.stderr || '通过'}`,
            `[教学预算与长链密度] (code: ${resB.code})\n${resB.stdout || resB.stderr || '通过'}`,
            `[选择题选项随机打乱（只读 verify）] (code: ${res0.code})\n${res0.stdout || res0.stderr || '通过'}`,
            `[切片编号检查] (code: ${res1.code})\n${res1.stdout || res1.stderr || '通过'}`,
            `[防泄露与版本比对] (code: ${res2.code})\n⚠ 诊断模式（已跳过部分核对）：--diagnostic 已跳过源级计数与日志核对，结果不得按完整通过解读。\n${res2.stdout || res2.stderr || '通过'}`,
          ].join('\n\n');
          return {
            kind: resP.success && resB.success && res0.success && res1.success && res2.success ? 'success' : 'error',
            text: out,
          };
        }

        return {
          kind: 'error',
          text: `❌ 未知子指令: ${sub}。请使用 /qchain help 查看完整语法（status | run | resume | gates | help）。`,
        };
      },
    });
  }

  // 4. 注入系统 Prompt 上下文
  const systemPromptService = ctx.get('systemPrompt');
  if (systemPromptService && typeof systemPromptService.context === 'function') {
    systemPromptService.context({
      name: 'qchain-context',
      order: 120,
      text: [
        '【问题驱动题链自学案流水线 (LangGraph) 协作者规则】',
        '1. 当前工作区配备了完整的题链流水线系统 (LangGraph 状态机编排引擎)，支持 XeLaTeX PDF 与 HTML 卡片双模交付。',
        '2. 你可以通过 `qchain_pipeline` 工具启动(run)、从断点恢复(resume)和监控流水线状态(status)。',
        '3. 物理人机断点红线：流水线每次启动都会在 Phase 0 强制挂起；恢复时必须通过 `ask_user_question` 取得真人确认与令牌（令牌由流水线写入 .langgraph/phase0_token.txt，严禁 Agent 自行读取），并将令牌经 `qchain_pipeline` 的 token 参数透传，再调用 action: resume 恢复执行（可一并确认学案主标题、交付模式 self_study/teaching 与输出格式 pdf/html/both）。',
        '4. 零信任质量门禁：教学语义与前置自足性门禁（前置知识卡与结论框强制约束）、学案编号结构、选择题选项随机打乱（防“所有答案都是 A”）、XeLaTeX PDF 与 HTML 卡片双模构建、版本差异与交付清单严格受控，可使用 `qchain_gate` 随时进行多维度质量门禁合规检查。',
        '5. 工作区强制隔离规范：题链流水线所有制成文件（TeX 切片、HTML 卡片、双源 PDF、门禁报告与 release/ 交付清单）强制位于输入文件所在处的 self 文件夹中，严禁向输入原材料目录直接写出制成文件。',
        '6. Agent 派发红线：收到 qchain 流水线 AGENT_DISPATCH 中断（载荷含 `.pd/agent-requests/*.json` 派发单，T1 映射 / T2 切片 / T4 验收均属此类）时，必须调用 `qchain_dispatch` 工具派发独立子 Agent 完成产物；严禁主 Agent 在本会话内直接撰写派发单 artifacts 声明的任何文件（含映射表、sec*.tex、acceptance JSON）。无代理证据的产物在磁盘验证按 `ok_unattested` 失败处理，交付门禁 FAIL。',
        '7. 派发完成后方可调用 `qchain_pipeline action=resume` 恢复流水线；resume 前先用 `qchain_dispatch` 返回的汇总核对全部 dispatch_id 状态为 ok。',
      ].join('\n'),
    });
  }
}
