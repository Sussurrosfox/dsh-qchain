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

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workspaceRoot = __dirname;

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

function qdClaimPath(selfDir, dispatchId) {
  return path.join(selfDir, '.pd', 'dispatch-claims', `${dispatchId}.json`);
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

function qdClaimIsReusable(selfDir, request, existing) {
  if (!existing || String(existing.status || '') !== 'done') return false;
  const dispatchId = String(request.dispatch_id || '');
  if (!qdReceiptOk(selfDir, dispatchId)) return false;
  const expected = (existing.output_digests && typeof existing.output_digests === 'object')
    ? existing.output_digests : {};
  const actual = qdCurrentDigests(selfDir, request);
  const keys = Object.keys(expected);
  if (keys.length === 0) return false;
  for (const k of keys) {
    if (String(actual[k] || '') !== String(expected[k] || '')) return false;
  }
  return true;
}

function qdAcquireClaim(selfDir, request) {
  const dispatchId = String(request.dispatch_id || '');
  const claimPath = qdClaimPath(selfDir, dispatchId);
  const writeClaim = (status, extra) => {
    const payload = {
      claim_version: 1,
      dispatch_id: dispatchId,
      batch_id: String(request.batch_id || ''),
      run_id: String(request.run_id || ''),
      status,
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
  const claimPath = qdClaimPath(selfDir, dispatchId);
  const existing = qdReadJson(claimPath) || {};
  const payload = {
    ...existing,
    status,
    heartbeat_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    ...(extra || {}),
  };
  try {
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
          proj = resolveEngineRoot(engineRoot);
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
        if (fs.existsSync(path.join(baseDir, 'self'))) {
          rawTarget = path.join(baseDir, 'self');
        } else if (fs.existsSync(path.join(process.cwd(), 'self'))) {
          rawTarget = path.join(process.cwd(), 'self');
        } else if (fs.existsSync(path.join(proj, 'self'))) {
          rawTarget = path.join(proj, 'self');
        } else {
          rawTarget = path.join(baseDir, 'self');
        }
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
      for (const c of candidates) {
        try {
          if (fs.existsSync(c) && fs.statSync(c).isDirectory()) { selfDir = c; break; }
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
            + ' > 档位表（fast=4/safe=1/budget=2/interactive=1）。'
            + '这是同时活跃的请求数，不是必须派发的请求数。',
        },
      },
    },
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
        for (const c of candidates) {
          try {
            if (fs.existsSync(c) && fs.statSync(c).isDirectory()) return c;
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
      const callSubagent = async (subArgs, attemptExec) => {
        if (subagentDef) return subagentDef.execute(subArgs, attemptExec);
        try {
          const tools = ctx?.tools || ctx?.get?.('tools');
          if (tools && typeof tools.execute === 'function') {
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
            const hostCall = callSubagent({
              description: `qchain ${request.stage || ''} ${request.scope || dispatchId}（${request.role || '子 Agent'}）`,
              prompt,
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
            } else {
              hostOutcomeUsable = true;
              parsed = parseSubagentFiles(outcome.value);
              appendDebugRecord(selfDir, {
                event: 'attempt_parsed', dispatch_id: dispatchId, attempt, attempt_id: attemptId,
                run_id: parsed.runId || '',
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
              try {
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
                qdAtomicWrite(abs, toWrite);
                written.push(rel);
                outputChars += typeof toWrite === 'string' ? toWrite.length : 0;
                outputDigests[rel] = qdSha256File(abs);
              } catch (err) {
                status = 'worker_error';
                errorClass = 'unknown';
                error = `写入 ${rel} 失败: ${err && err.message ? err.message : String(err)}`;
                break;
              }
            }
            if (status === 'ok') {
              const missing = artifacts.filter((rel) => findContent(rel) === undefined);
              if (written.length === 0) {
                status = 'missing_artifacts';
                errorClass = 'content';
                error = `子 Agent 未提交任何白名单产物: ${artifacts.join(', ')}`;
                feedback = error;
              } else if (missing.length > 0) {
                status = 'missing_artifacts';
                errorClass = 'content';
                error = `部分产物缺失: ${missing.join(', ')}`;
                feedback = `返修请补齐白名单产物: ${missing.join(', ')}`;
              } else if (artifacts.every((rel) => before[rel]
                && before[rel] === outputDigests[rel])) {
                // 产物与原文件内容一致，属于合法产出，不判为错误
                status = 'ok';
                error = '';
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
              artifacts_written: written,
              output_digests: outputDigests,
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
          if (status === 'ok' || !retryable) break;
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
        const claim = qdAcquireClaim(selfDir, request);
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
