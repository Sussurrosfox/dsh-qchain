// dispatch_policy.mjs — 派发并发配置解析器（JS 侧镜像，任务19 / C12）
//
// 与 workflow/dispatch_policy.py **同一套规则**：两边的单元测试共享
// scripts/fixtures/dispatch-policy-cases.json，逐例结果必须完全一致
// （PARCFG-06）。规则文本见 Python 模块的模块文档；这里只做等价实现，
// 任何规则改动必须两边同步 + 补 fixture。
//
// 为什么 JS 也要一份：qchain_dispatch 工具在宿主侧调度子 Agent，工具入参
// max_concurrency 的校验/来源追踪、以及「配置读取失败必须报真实原因」都必须
// 在 JS 侧完成；旧实现 catch(_){} → 并发 2 的静默回落是本卡要消除的缺陷。

export const DISPATCH_CONTRACT_VERSION = 2;
export const LEGACY_DISPATCH_CONTRACT_VERSION = 1;
export const SUPPORTED_DISPATCH_CONTRACT_VERSIONS = [1, 2];

export const DISPATCH_PROFILES = ['interactive', 'safe', 'fast', 'budget'];
export const PROFILE_CONCURRENCY = {
  interactive: 1, safe: 1, fast: 4, budget: 2,
};
export const DEFAULT_PROFILE = 'safe';
export const RUN_SCOPE_DEFAULT_PROFILE = {
  full: 'fast', focused: 'interactive', edit: 'interactive',
};
export const MAX_CONCURRENCY_LIMIT = 16;

export const EXECUTOR_PYTHON = 'python_registry';
export const EXECUTOR_HOST = 'host_subagent';
export const EXECUTOR_KINDS = [EXECUTOR_PYTHON, EXECUTOR_HOST];

export const FAST_EXECUTION_PROFILE = 'faithful_fast';

export const INVALID_DISPATCH_PROFILE = 'INVALID_DISPATCH_PROFILE';
export const CONFIG_CONCURRENCY_INVALID = 'CONFIG_CONCURRENCY_INVALID';
export const CONFIG_CONCURRENCY_OUT_OF_RANGE = 'CONFIG_CONCURRENCY_OUT_OF_RANGE';
export const CONFIG_CONCURRENCY_CONFLICT = 'CONFIG_CONCURRENCY_CONFLICT';
export const TOOL_CONCURRENCY_INVALID = 'TOOL_CONCURRENCY_INVALID';
export const TOOL_CONCURRENCY_OUT_OF_RANGE = 'TOOL_CONCURRENCY_OUT_OF_RANGE';
export const DISPATCH_CONTRACT_VERSION_UNSUPPORTED =
  'DISPATCH_CONTRACT_VERSION_UNSUPPORTED';
export const CONFIG_CONCURRENCY_SHADOWED = 'CONFIG_CONCURRENCY_SHADOWED';

export class DispatchPolicyError extends Error {
  constructor(code, detail) {
    super(`${code}: ${detail}`);
    this.name = 'DispatchPolicyError';
    this.code = code;
    this.detail = detail;
  }

  asObject() {
    return { code: this.code, detail: this.detail };
  }
}

// --- 标量归一化（与 Python _int_like 等价） --------------------------------

function intLike(value) {
  if (typeof value === 'boolean') return { value: null, reason: '布尔值' };
  if (typeof value === 'number') {
    if (Number.isNaN(value)) return { value: null, reason: 'NaN' };
    if (!Number.isFinite(value)) return { value: null, reason: '非数值' };
    if (Number.isInteger(value)) return { value, reason: '' };
    return { value: null, reason: '小数' };
  }
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return { value: null, reason: '' };
    if (/^[+-]?\d+$/.test(text)) return { value: parseInt(text, 10), reason: '' };
    const asFloat = Number(text);
    if (Number.isNaN(asFloat)) return { value: null, reason: '非数值' };
    if (!Number.isFinite(asFloat)) return { value: null, reason: '非数值' };
    if (Number.isInteger(asFloat)) return { value: asFloat, reason: '' };
    return { value: null, reason: '小数' };
  }
  if (value === null || value === undefined) return { value: null, reason: '' };
  return { value: null, reason: `类型 ${typeof value}` };
}

function coerceConfigConcurrency(value, key, strict, diagnostics) {
  const { value: number, reason } = intLike(value);
  if (reason) {
    const detail = `${key}=${JSON.stringify(value)} 非法（${reason}）：`
      + `并发必须是 1..${MAX_CONCURRENCY_LIMIT} 的整数，0 表示按档位自动`;
    if (strict) throw new DispatchPolicyError(CONFIG_CONCURRENCY_INVALID, detail);
    diagnostics.push({ code: CONFIG_CONCURRENCY_INVALID, detail });
    return 0;
  }
  if (number === null) return 0;
  if (number < 0) {
    const detail = `${key}=${JSON.stringify(value)} 非法：不允许负数（0 表示按档位自动）`;
    if (strict) throw new DispatchPolicyError(CONFIG_CONCURRENCY_INVALID, detail);
    diagnostics.push({ code: CONFIG_CONCURRENCY_INVALID, detail });
    return 0;
  }
  if (number > MAX_CONCURRENCY_LIMIT) {
    const detail = `${key}=${JSON.stringify(value)} 超出上限 ${MAX_CONCURRENCY_LIMIT}`
      + '（宿主/worker 并发不得虚报）';
    if (strict) throw new DispatchPolicyError(CONFIG_CONCURRENCY_OUT_OF_RANGE, detail);
    diagnostics.push({ code: CONFIG_CONCURRENCY_OUT_OF_RANGE, detail });
  }
  return number;
}

function coerceToolOverride(value) {
  const { value: number, reason } = intLike(value);
  if (reason) {
    throw new DispatchPolicyError(
      TOOL_CONCURRENCY_INVALID,
      `max_concurrency=${JSON.stringify(value)} 非法（${reason}）：`
      + `必须是 1..${MAX_CONCURRENCY_LIMIT} 的整数，0 表示按配置自动`,
    );
  }
  if (number === null) return 0;
  if (number < 0) {
    throw new DispatchPolicyError(
      TOOL_CONCURRENCY_INVALID,
      `max_concurrency=${JSON.stringify(value)} 非法：不允许负数（0 表示按配置自动）`,
    );
  }
  if (number > MAX_CONCURRENCY_LIMIT) {
    throw new DispatchPolicyError(
      TOOL_CONCURRENCY_OUT_OF_RANGE,
      `max_concurrency=${JSON.stringify(value)} 超出上限 ${MAX_CONCURRENCY_LIMIT}`
      + '（宿主 subagent 通道的客户端上限）',
    );
  }
  return number;
}

export function resolveContractVersion(value) {
  const { value: number, reason } = intLike(value);
  const known = SUPPORTED_DISPATCH_CONTRACT_VERSIONS.join(' / ');
  if (reason) {
    throw new DispatchPolicyError(
      DISPATCH_CONTRACT_VERSION_UNSUPPORTED,
      `dispatch_contract_version=${JSON.stringify(value)} 不受支持（${reason}）：`
      + `已知版本 ${known}；拒绝按猜测的版本解析派发并发`,
    );
  }
  if (number === null) return LEGACY_DISPATCH_CONTRACT_VERSION;
  if (!SUPPORTED_DISPATCH_CONTRACT_VERSIONS.includes(number)) {
    throw new DispatchPolicyError(
      DISPATCH_CONTRACT_VERSION_UNSUPPORTED,
      `dispatch_contract_version=${JSON.stringify(value)} 不受支持：已知版本 ${known}；`
      + '升级请先读 MIGRATION 说明，不允许静默按最新版本解释旧计划',
    );
  }
  return number;
}

function executionProfileOf(config, explicit) {
  const raw = explicit !== undefined && explicit !== null
    ? explicit : (config || {}).execution_profile;
  const text = String(raw === undefined || raw === null ? '' : raw).trim().toLowerCase();
  return text || 'legacy';
}

function jsonish(value) {
  // 与 Python %r 的可读性保持接近：字符串带引号，其余原样
  if (typeof value === 'string') return JSON.stringify(value);
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  return String(value);
}

// --- 唯一解析入口（与 Python resolve_dispatch_policy 等价） ----------------

export function resolveDispatchPolicy(options = {}) {
  const config = { ...(options.config || {}) };
  const runScopeArg = options.runScope;
  const executionProfile = options.executionProfile;
  const toolOverride = options.toolOverride;
  const workerCapabilities = options.workerCapabilities;
  const frozen = options.frozen ? { ...options.frozen } : null;

  const diagnostics = [];

  let version;
  if (frozen && frozen.dispatch_contract_version !== undefined
      && frozen.dispatch_contract_version !== null) {
    version = resolveContractVersion(frozen.dispatch_contract_version);
  } else {
    version = resolveContractVersion(config.dispatch_contract_version);
  }
  const legacy = version < DISPATCH_CONTRACT_VERSION;

  const dispatchRaw = config.agent_dispatch;
  const dispatchMap = (dispatchRaw && typeof dispatchRaw === 'object'
    && !Array.isArray(dispatchRaw)) ? dispatchRaw : {};

  let profile;
  let profileSource;
  if (frozen && String(frozen.profile || '').trim()) {
    profile = String(frozen.profile).trim().toLowerCase();
    if (!Object.prototype.hasOwnProperty.call(PROFILE_CONCURRENCY, profile)) {
      throw new DispatchPolicyError(
        INVALID_DISPATCH_PROFILE,
        `冻结的派发档位 ${jsonish(frozen.profile)} 非法（只能是: ${DISPATCH_PROFILES.join(' / ')}）`,
      );
    }
    profileSource = 'frozen:dispatch.profile';
  } else {
    const candidates = [
      [dispatchMap.profile, 'config:agent_dispatch.profile'],
      [typeof dispatchRaw === 'string' ? dispatchRaw : null, 'config:agent_dispatch'],
    ];
    let explicit = '';
    let explicitSource = '';
    for (const [rawValue, source] of candidates) {
      if (rawValue === null || rawValue === undefined) continue;
      const text = String(rawValue).trim().toLowerCase();
      if (!text) continue;
      if (!Object.prototype.hasOwnProperty.call(PROFILE_CONCURRENCY, text)) {
        const detail = `${source}=${jsonish(rawValue)} 不是合法派发档位`
          + `（只能是: ${DISPATCH_PROFILES.join(' / ')}）`;
        if (!legacy) throw new DispatchPolicyError(INVALID_DISPATCH_PROFILE, detail);
        diagnostics.push({
          code: INVALID_DISPATCH_PROFILE,
          detail: `${detail}；旧契约回落 ${DEFAULT_PROFILE}`,
        });
        continue;
      }
      explicit = text;
      explicitSource = source;
      break;
    }
    if (explicit) {
      profile = explicit;
      profileSource = explicitSource;
    } else {
      const scope = String(runScopeArg || config.run_scope || '').trim().toLowerCase();
      const scopeProfile = RUN_SCOPE_DEFAULT_PROFILE[scope];
      const execProfile = executionProfileOf(config, executionProfile);
      if (scopeProfile) {
        profile = scopeProfile;
        profileSource = `run_scope:${scope}`;
      } else if (!legacy && execProfile === FAST_EXECUTION_PROFILE) {
        profile = 'fast';
        profileSource = `execution_profile:${FAST_EXECUTION_PROFILE}`;
      } else {
        profile = DEFAULT_PROFILE;
        profileSource = 'default';
      }
    }
  }

  const nested = coerceConfigConcurrency(
    dispatchMap.max_concurrency, 'agent_dispatch.max_concurrency', !legacy, diagnostics);
  const flat = coerceConfigConcurrency(
    config.agent_max_concurrency, 'agent_max_concurrency', !legacy, diagnostics);

  let configured;
  let concurrencySource;
  if (frozen && frozen.configured_concurrency !== undefined
      && frozen.configured_concurrency !== null) {
    configured = intLike(frozen.configured_concurrency).value || 0;
    concurrencySource = String(frozen.concurrency_source || 'frozen:dispatch');
  } else if (!legacy) {
    if (nested > 0 && flat > 0 && nested !== flat) {
      throw new DispatchPolicyError(
        CONFIG_CONCURRENCY_CONFLICT,
        `agent_dispatch.max_concurrency=${nested} 与 agent_max_concurrency=${flat} `
        + '同时为正且不一致：请只保留一处（嵌套块优先，扁平键仅作兼容），拒绝静默挑一个',
      );
    }
    if (nested > 0) {
      configured = nested;
      concurrencySource = 'config:agent_dispatch.max_concurrency';
    } else if (flat > 0) {
      configured = flat;
      concurrencySource = 'config:agent_max_concurrency';
    } else {
      configured = 0;
      concurrencySource = 'auto';
    }
  } else {
    if (flat > 0) {
      configured = flat;
      concurrencySource = 'config:agent_max_concurrency';
    } else if (nested > 0) {
      configured = nested;
      concurrencySource = 'config:agent_dispatch.max_concurrency';
    } else {
      configured = 0;
      concurrencySource = 'auto';
    }
    if (nested > 0 && flat > 0 && nested !== flat) {
      diagnostics.push({
        code: CONFIG_CONCURRENCY_SHADOWED,
        detail: `旧契约：扁平 agent_max_concurrency=${flat} 覆盖嵌套 `
          + `agent_dispatch.max_concurrency=${nested}（保持旧优先级）`,
      });
    }
  }

  const override = coerceToolOverride(toolOverride);
  if (override > 0) {
    configured = override;
    concurrencySource = 'tool:max_concurrency';
  }

  const concurrencyLimit = configured > 0
    ? configured : PROFILE_CONCURRENCY[profile];

  let executorKind;
  let workerCap = null;
  if (workerCapabilities && typeof workerCapabilities === 'object'
      && !Array.isArray(workerCapabilities)) {
    executorKind = EXECUTOR_PYTHON;
    const rawCap = workerCapabilities.max_concurrency;
    if (typeof rawCap === 'boolean' || rawCap === null || rawCap === undefined) {
      workerCap = null;
    } else {
      const parsed = intLike(rawCap).value;
      workerCap = (parsed !== null && parsed >= 1) ? parsed : null;
    }
  } else {
    executorKind = EXECUTOR_HOST;
    workerCap = null;
  }

  let effective;
  let capabilityVerified;
  if (workerCap === null) {
    effective = concurrencyLimit;
    capabilityVerified = false;
  } else {
    effective = Math.max(1, Math.min(concurrencyLimit, workerCap));
    capabilityVerified = true;
  }

  let serialReason;
  if (effective > 1) {
    serialReason = '';
  } else if (concurrencyLimit <= 1) {
    serialReason = `并发上限为 1（来源 ${concurrencySource}，档位 ${profile}）`;
  } else if (workerCap === 1) {
    serialReason = 'worker 声明的 max_concurrency=1（串行执行器）';
  } else {
    serialReason = `生效并发为 1（上限 ${concurrencyLimit}）`;
  }

  return {
    dispatch_contract_version: version,
    legacy_contract: legacy,
    profile,
    profile_source: profileSource,
    profile_concurrency: PROFILE_CONCURRENCY[profile],
    configured_concurrency: configured,
    concurrency_source: concurrencySource,
    concurrency_limit: concurrencyLimit,
    executor_kind: executorKind,
    worker_max_concurrency: workerCap,
    capability_verified: capabilityVerified,
    effective_concurrency: effective,
    serial_reason: serialReason,
    diagnostics,
  };
}

/**
 * 从工具入参 + 配置解析输入得到策略；配置读取失败必须抛真实原因。
 * @param {{config?:object, run_scope?:string, execution_profile?:string,
 *          model_map?:object, frozen?:object}} inputs read_dispatch_inputs 的结果
 * @param {{toolOverride?:any, workerCapabilities?:object}} options
 */
export function resolveFromInputs(inputs = {}, options = {}) {
  const policy = resolveDispatchPolicy({
    config: inputs.config || {},
    runScope: inputs.run_scope || '',
    executionProfile: inputs.execution_profile || '',
    frozen: inputs.frozen || null,
    toolOverride: options.toolOverride,
    workerCapabilities: options.workerCapabilities,
  });
  const modelMap = (inputs.model_map && typeof inputs.model_map === 'object')
    ? inputs.model_map : {};
  return { policy, modelMap };
}
