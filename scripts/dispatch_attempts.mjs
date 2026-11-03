/**
 * 逐尝试调度运行时（任务卡 09）：可取消超时 + 结构化错误分型 + 不可覆盖的 attempt 台账。
 *
 * 设计红线（DATA-CONTRACTS C3/C4）：
 * 1. 每个 attempt 一个 AbortController，父 signal 与截止时刻都折叠进同一个
 *    attempt signal，再交给宿主 subagent（三个适配分支共用同一 signal）。
 *    仅用 Promise.race 停止等待不算取消——宿主仍在跑、仍可能写文件。
 * 2. 中止后必须等宿主确认结束（settle）才允许重派；确认不了就停，
 *    记 cancellation_unconfirmed，绝不启动重叠子代理。晚到结果一律丢弃。
 * 3. 错误按结构化错误码分型：transient 可重试且不超过 max_retries；
 *    content 交返修节点（本层不重试）；auth/config/cancel 不重试。
 *    字符串启发式只是「宿主没有结构码」时的兼容层，必须有测试覆盖。
 * 4. 每个 attempt 落一份不可覆盖的台账 .pd/agent-attempts/<dispatch_id>/attempt-0001.json；
 *    未知指标（token/queue）写 null，绝不把字符数当 token。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import crypto from 'node:crypto';

export const DEFAULT_TIMEOUT_SECONDS = 900;
export const DEFAULT_MAX_RETRIES = 2;
export const DEFAULT_CANCEL_GRACE_MS = 5000;
export const ATTEMPT_SCHEMA_VERSION = 1;

/** 只有 transient 会在调度层重试；其余（content/contract/none/auth/config）单次。 */
export const TRANSIENT_RETRY_CLASS = 'transient';
export const RETRY_CLASSES = Object.freeze([
  'transient', 'content', 'contract', 'none', 'auth', 'config',
]);

export const ERROR_CLASSES = Object.freeze([
  'transient', 'timeout', 'cancel', 'content', 'auth', 'config', 'unknown',
]);

/** C4 规定的逐尝试台账必备字段（缺失一律补 null，不允许静默省略）。 */
export const ATTEMPT_REQUIRED_FIELDS = Object.freeze([
  'attempt_id', 'dispatch_id', 'scope', 'scopes', 'provider', 'model',
  'started_at', 'finished_at', 'elapsed_ms', 'queue_ms', 'status',
  'error_class', 'cancel_requested', 'cancel_confirmed', 'prompt_chars',
  'output_chars', 'input_digest', 'output_digests', 'cached',
]);

function toPositiveNumber(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return n;
}

function toNonNegativeInt(value, fallback) {
  if (value === null || value === undefined || value === '') return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.trunc(n));
}

/**
 * 解析派发单的运输策略：超时、重试上限、重试分类、取消确认宽限。
 * 非正/非法超时回落到默认（不静默变成立即超时），max_retries 取非负整数。
 */
export function resolveAttemptPolicy(request, defaults = {}) {
  const req = (request && typeof request === 'object') ? request : {};
  const timeoutSeconds = toPositiveNumber(
    req.timeout_seconds,
    toPositiveNumber(defaults.timeout_seconds, DEFAULT_TIMEOUT_SECONDS),
  );
  const maxRetries = toNonNegativeInt(
    req.max_retries,
    toNonNegativeInt(defaults.max_retries, DEFAULT_MAX_RETRIES),
  );
  const rawClass = String(req.retry_class || defaults.retry_class || TRANSIENT_RETRY_CLASS)
    .trim().toLowerCase();
  const retryClass = rawClass || TRANSIENT_RETRY_CLASS;
  const cancelGraceMs = toPositiveNumber(
    req.cancel_grace_ms,
    toPositiveNumber(defaults.cancel_grace_ms, DEFAULT_CANCEL_GRACE_MS),
  );
  return {
    timeout_seconds: timeoutSeconds,
    timeout_ms: Math.round(timeoutSeconds * 1000),
    max_retries: maxRetries,
    retry_class: retryClass,
    attempts_allowed: retryClass === TRANSIENT_RETRY_CLASS ? maxRetries + 1 : 1,
    cancel_grace_ms: cancelGraceMs,
  };
}

const CODE_CLASS = {
  DISPATCH_TIMEOUT: 'timeout',
  TIMEOUT: 'timeout',
  ETIMEDOUT: 'transient',
  DISPATCH_CANCELLED: 'cancel',
  ABORT_ERR: 'cancel',
  CANCELLED: 'cancel',
  CONTENT_ERROR: 'content',
  PARSE_ERROR: 'content',
  ARTIFACT_MISSING: 'content',
  MISSING_ARTIFACTS: 'content',
  AUTH_ERROR: 'auth',
  AUTH_FAILED: 'auth',
  UNAUTHORIZED: 'auth',
  FORBIDDEN: 'auth',
  PERMISSION_DENIED: 'auth',
  CONFIG_ERROR: 'config',
  CONFIG_MISSING: 'config',
  MODEL_NOT_CONFIGURED: 'config',
  TRANSIENT_ERROR: 'transient',
  ECONNRESET: 'transient',
  ECONNREFUSED: 'transient',
  EAI_AGAIN: 'transient',
  EPIPE: 'transient',
  RATE_LIMITED: 'transient',
  HTTP_429: 'transient',
};

// 兼容层：宿主没有结构码时按文案/状态码判定（有测试锁定行为）
const TEXT_RULES = [
  ['auth', [/unauthor/i, /forbidden/i, /permission denied/i, /api[_ -]?key/i,
    /invalid[_ ]?(token|credential)/i, /鉴权失败/, /未授权/]],
  ['config', [/config(uration)?[ _-]?(missing|invalid|error|required)/i,
    /model[_ ]?not[_ ]?configured/i, /route[_ ]?not[_ ]?found/i, /未配置/, /配置缺失/]],
  ['cancel', [/aborted/i, /cancell?ed/i, /已取消/]],
  ['timeout', [/timed? ?out/i, /timeout/i, /超时/]],
  ['content', [/parse/i, /missing[_ ]?artifacts?/i, /产物缺失/, /未提交任何白名单产物/,
    /未返回可用产物/, /<<<FILE/]],
  ['transient', [/econnreset/i, /etimedout/i, /eai_again/i, /socket hang up/i,
    /fetch failed/i, /network/i, /rate.?limit/i, /temporar/i, /overload/i, /繁忙/]],
];

/**
 * 结构化错误分型：优先 error.code / error_class，其次 HTTP 状态码，
 * 最后才回落到文案启发式（仅兼容无结构码的宿主错误）。
 */
export function classifyDispatchError(err) {
  if (err === null || err === undefined) {
    return { error_class: 'unknown', retryable: false, source: 'empty' };
  }
  if (typeof err === 'string') {
    return { error_class: classifyText(err), retryable: classifyText(err) === 'transient', source: 'text' };
  }
  const code = String(err.code || err.error_code || err.error_class || '').trim().toUpperCase();
  const name = String(err.name || '');
  const status = Number(err.status || err.statusCode || 0);

  if (name === 'TimeoutError' && !code) {
    return { error_class: 'timeout', retryable: true, source: 'name' };
  }
  if (name === 'AbortError' && !code) {
    return { error_class: 'cancel', retryable: false, source: 'name' };
  }
  if (code && CODE_CLASS[code]) {
    const cls = CODE_CLASS[code];
    return { error_class: cls, retryable: cls === 'transient' || cls === 'timeout', source: 'code' };
  }
  if (status) {
    if (status === 401 || status === 403) return { error_class: 'auth', retryable: false, source: 'status' };
    if (status === 408 || status === 429 || status >= 500) {
      return { error_class: 'transient', retryable: true, source: 'status' };
    }
    if (status >= 400) return { error_class: 'config', retryable: false, source: 'status' };
  }
  const msg = String(err.message || err.detail || err.reason || '');
  if (msg) {
    const cls = classifyText(msg);
    return { error_class: cls, retryable: cls === 'transient' || cls === 'timeout', source: 'text' };
  }
  return { error_class: 'unknown', retryable: false, source: 'fallback' };
}

function classifyText(text) {
  const raw = String(text || '');
  for (const [cls, patterns] of TEXT_RULES) {
    if (patterns.some((re) => re.test(raw))) return cls;
  }
  return 'unknown';
}

/** 是否允许再开一个 attempt（transport 层重试；content/auth/config/cancel 一律不重试）。 */
export function shouldRetryAttempt({ error_class: errorClass, attempt, policy, cancel_confirmed: cancelConfirmed } = {}) {
  const cls = String(errorClass || 'unknown');
  if (!policy || !Number.isFinite(Number(attempt))) return false;
  if (policy.retry_class !== TRANSIENT_RETRY_CLASS) return false;
  if (Number(attempt) >= Number(policy.attempts_allowed)) return false;
  if (cls === 'timeout') return cancelConfirmed === true; // 取消未确认 → 不许重派
  return cls === 'transient';
}

function makeAbortReason(why, timeoutMs) {
  const isTimeout = why === 'timeout';
  const err = new Error(isTimeout
    ? `DISPATCH_TIMEOUT: 派发超时（${timeoutMs}ms）已请求取消`
    : 'DISPATCH_CANCELLED: 派发被父级取消');
  err.code = isTimeout ? 'DISPATCH_TIMEOUT' : 'DISPATCH_CANCELLED';
  err.name = isTimeout ? 'TimeoutError' : 'AbortError';
  err.error_class = isTimeout ? 'timeout' : 'cancel';
  return err;
}

/**
 * 每个 attempt 的中止器：父 signal 与截止定时器折叠进同一个 AbortController。
 * cleanup() 必须清掉定时器并摘掉父监听（否则多 attempt 会累积监听）。
 */
export function createAttemptAbort({ parentSignal = null, timeoutMs = 0 } = {}) {
  const controller = new AbortController();
  const state = { requested: false, reason: null, why: null };
  const request = (why) => {
    if (state.requested) return false;
    state.requested = true;
    state.why = why;
    state.reason = makeAbortReason(why, timeoutMs);
    try { controller.abort(state.reason); } catch (_) { /* 老运行时无 reason 参数 */ }
    return true;
  };
  const onParentAbort = () => { request('parent_cancel'); };
  let parentAttached = false;
  let timer = null;
  if (parentSignal && typeof parentSignal.addEventListener === 'function') {
    if (parentSignal.aborted) {
      request('parent_cancel');
    } else {
      parentSignal.addEventListener('abort', onParentAbort, { once: true });
      parentAttached = true;
    }
  }
  if (Number.isFinite(Number(timeoutMs)) && Number(timeoutMs) > 0) {
    // 不 unref：截止定时器必须真的能触发（即便宿主挂死）；由 cleanup() 负责清除。
    timer = setTimeout(() => { request('timeout'); }, Number(timeoutMs));
  }
  return {
    signal: controller.signal,
    get requested() { return state.requested; },
    get reason() { return state.reason; },
    get why() { return state.why; },
    cancel: request,
    /** 父监听是否仍挂着（测试用来断言清理彻底） */
    listenerAttached: () => parentAttached,
    cleanup() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (parentAttached && typeof parentSignal.removeEventListener === 'function') {
        parentSignal.removeEventListener('abort', onParentAbort);
        parentAttached = false;
      }
    },
  };
}

export function formatAttemptId(index) {
  const n = Math.max(1, Math.trunc(Number(index) || 1));
  return `attempt-${String(n).padStart(4, '0')}`;
}

export function safeDispatchKey(dispatchId) {
  const raw = String(dispatchId || '').trim();
  const safe = raw.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '_');
  return safe || 'unknown-dispatch';
}

/** .pd/agent-attempts/<dispatch_id>/ —— 逐尝试台账目录 */
export function attemptsDir(selfDir, dispatchId) {
  return path.join(selfDir, '.pd', 'agent-attempts', safeDispatchKey(dispatchId));
}

/** 补全合同字段：未知指标写 null，禁止把字符数当 token。 */
export function normalizeAttemptRecord(record) {
  const rec = (record && typeof record === 'object') ? record : {};
  if (!rec.attempt_id) throw new Error('attempt 台账缺少 attempt_id');
  if (!rec.dispatch_id) throw new Error('attempt 台账缺少 dispatch_id');
  const out = { schema_version: ATTEMPT_SCHEMA_VERSION, ...rec };
  for (const field of ATTEMPT_REQUIRED_FIELDS) {
    if (!(field in out)) out[field] = null;
  }
  if (out.queue_ms === undefined) out.queue_ms = null;
  // token 计数一律未知 → null（字符数不是 token）
  for (const field of ['input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens']) {
    if (!(field in out)) out[field] = null;
  }
  if (!('output_digests' in out) || out.output_digests === undefined) out.output_digests = {};
  return out;
}

/**
 * 写一份不可覆盖的 attempt 台账：已存在同名文件时改用 .rN 修订名，
 * 绝不覆盖既有 attempt 记录。
 */
export function writeAttemptRecord(selfDir, dispatchId, record) {
  const dir = attemptsDir(selfDir, dispatchId);
  fs.mkdirSync(dir, { recursive: true });
  const payload = normalizeAttemptRecord({ ...record, dispatch_id: dispatchId });
  const base = String(payload.attempt_id);
  const data = `${JSON.stringify(payload, null, 2)}\n`;
  let revision = 1;
  for (;;) {
    const name = revision === 1 ? `${base}.json` : `${base}.r${revision}.json`;
    const abs = path.join(dir, name);
    try {
      fs.writeFileSync(abs, data, { encoding: 'utf8', flag: 'wx' });
      return { path: abs, revision, record: payload };
    } catch (err) {
      if (!err || err.code !== 'EEXIST') throw err;
      revision += 1;
      if (revision > 99) throw new Error(`attempt 台账修订过多: ${base}`);
    }
  }
}

/** 读回某派发单的全部 attempt 台账（按 attempt 序号 + 修订号排序）。 */
export function readAttemptRecords(selfDir, dispatchId) {
  const dir = attemptsDir(selfDir, dispatchId);
  if (!fs.existsSync(dir)) return [];
  const out = [];
  for (const name of fs.readdirSync(dir).sort()) {
    if (!name.endsWith('.json')) continue;
    try {
      const raw = fs.readFileSync(path.join(dir, name), 'utf8');
      out.push({ file: name, ...JSON.parse(raw) });
    } catch (_) { /* 忽略不可读台账 */ }
  }
  return out;
}

export function delayMs(ms) {
  const wait = Math.max(0, Number(ms) || 0);
  return new Promise((resolve) => { setTimeout(resolve, wait); });
}

export function sha256Text(text) {
  return crypto.createHash('sha256').update(String(text), 'utf8').digest('hex');
}

/**
 * 把 attempt 的 signal 叠到父 exec 上。
 * 用 Proxy 而不是 {...exec}：父 exec 可能是带原型/私有字段的宿主上下文对象，
 * 展开会丢方法、丢 getter；这里只替换 signal 一个属性，其余访问原样透传。
 */
export function attemptExecWithSignal(baseExec, signal) {
  const base = (baseExec && typeof baseExec === 'object') ? baseExec : {};
  try {
    return new Proxy(base, {
      get(target, prop) {
        if (prop === 'signal') return signal;
        const value = Reflect.get(target, prop, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
      set(target, prop, value) {
        if (prop === 'signal') return true;
        return Reflect.set(target, prop, value, target);
      },
      has(target, prop) {
        return prop === 'signal' ? true : Reflect.has(target, prop);
      },
    });
  } catch (_) {
    return { ...base, signal };
  }
}

/**
 * 宿主调用与「取消/超时」的汇合点（DISP-01/DISP-02 的核心）。
 *
 * 三种结局：
 *  - settled     宿主先正常结束（成功或抛错），且此刻 attempt 仍然有效 → 结果可用；
 *  - cancelled   已请求中止，且宿主确认结束 → 结果一律丢弃（迟到产物不得落盘）；
 *  - unconfirmed 已请求中止，但宽限期内宿主没结束 → 无法确认停止，调用方必须停派、不得重试。
 */
export async function settleWithCancel({ abort, hostPromise, graceMs = DEFAULT_CANCEL_GRACE_MS }) {
  let hostFinished = false;
  let hostDone = null;
  const hostSettled = Promise.resolve(hostPromise).then(
    (value) => { hostFinished = true; hostDone = { ok: true, value }; return hostDone; },
    (error) => { hostFinished = true; hostDone = { ok: false, error }; return hostDone; },
  );
  let onAbort = null;
  const abortFired = new Promise((resolve) => {
    if (abort.signal.aborted) { resolve(); return; }
    onAbort = () => resolve();
    abort.signal.addEventListener('abort', onAbort, { once: true });
  });
  const base = { cancelRequested: false, cancelConfirmed: false, lateOutputDiscarded: false, why: null, reason: null };
  try {
    const first = await Promise.race([
      hostSettled.then(() => 'settled'),
      abortFired.then(() => 'abort'),
    ]);
    if (first === 'settled' && !abort.requested) {
      return { ...base, outcome: 'settled', value: hostDone.value, error: hostDone.ok ? null : hostDone.error };
    }
    // 走到这里：要么 abort 先到，要么宿主已结束但 attempt 令牌已失效（结果不可用）。
    const pending = hostFinished ? 'settled' : await Promise.race([
      hostSettled.then(() => 'settled'),
      delayMs(graceMs).then(() => 'grace'),
    ]);
    const common = {
      ...base,
      cancelRequested: true,
      why: abort.why || null,
      reason: abort.reason || null,
    };
    if (pending === 'settled') {
      return {
        ...common,
        outcome: 'cancelled',
        cancelConfirmed: true,
        lateOutputDiscarded: !!(hostDone && hostDone.ok),
        value: hostDone ? hostDone.value : undefined,
        error: hostDone && !hostDone.ok ? hostDone.error : null,
      };
    }
    return { ...common, outcome: 'unconfirmed', cancelConfirmed: false };
  } finally {
    if (onAbort && typeof abort.signal.removeEventListener === 'function') {
      try { abort.signal.removeEventListener('abort', onAbort); } catch (_) {}
    }
  }
}
