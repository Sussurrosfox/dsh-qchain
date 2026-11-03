// 任务19（C12）：JS 侧派发并发策略回归测试。
//
// 与 scripts/test_dispatch_policy.py 共用同一份 fixtures
// （scripts/fixtures/dispatch-policy-cases.json）：同一组输入必须产出与
// Python 完全一致的语义字段（PARCFG-06）。任何规则改动都必须两边同步。
//
// 运行：node --test scripts/test_dispatch_policy.mjs
// 供 Python 侧做跨语言比对：node scripts/test_dispatch_policy.mjs --dump
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  resolveDispatchPolicy, DispatchPolicyError,
  DISPATCH_CONTRACT_VERSION, MAX_CONCURRENCY_LIMIT, EXECUTOR_KINDS,
  CONFIG_CONCURRENCY_CONFLICT, DISPATCH_CONTRACT_VERSION_UNSUPPORTED,
} from './dispatch_policy.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_REL = path.join('fixtures', 'dispatch-policy-cases.json');

export function loadFixtures() {
  const abs = path.join(__dirname, FIXTURE_REL);
  return JSON.parse(fs.readFileSync(abs, 'utf8'));
}

function policyOf(testCase) {
  return resolveDispatchPolicy({
    config: testCase.config || {},
    runScope: testCase.run_scope || '',
    executionProfile: testCase.execution_profile || null,
    toolOverride: testCase.tool_override,
    workerCapabilities: testCase.worker_capabilities === undefined
      ? null : testCase.worker_capabilities,
    frozen: testCase.frozen || null,
  });
}

/** 把解析结果投影成 fixtures 声明的语义字段（诊断只比错误码）。 */
export function project(policy, fields) {
  const out = {};
  for (const field of fields) {
    if (field === 'diagnostic_codes') {
      out[field] = (policy.diagnostics || []).map((d) => d.code);
    } else {
      out[field] = policy[field];
    }
  }
  return out;
}

/** 对所有 case 求值：正常返回投影字段，抛错返回 {error: code}。 */
export function evaluateAll() {
  const fixture = loadFixtures();
  const results = {};
  for (const testCase of fixture.cases) {
    try {
      results[testCase.name] = project(policyOf(testCase), fixture.fields);
    } catch (err) {
      if (!(err instanceof DispatchPolicyError)) throw err;
      results[testCase.name] = { error: err.code };
    }
  }
  return results;
}

// --dump：只打印结果供 Python 跨语言比对，不跑测试
if (process.argv.includes('--dump')) {
  process.stdout.write(JSON.stringify(evaluateAll()));
  process.exit(0);
}

const fixture = loadFixtures();

for (const testCase of fixture.cases) {
  test(`dispatch-policy: ${testCase.name}`, () => {
    if (testCase.error) {
      assert.throws(() => policyOf(testCase), (err) => {
        assert.ok(err instanceof DispatchPolicyError,
          `期望 DispatchPolicyError，实际 ${err && err.constructor && err.constructor.name}`);
        assert.equal(err.code, testCase.error, `${testCase.name}: 错误码不符`);
        return true;
      });
      return;
    }
    const actual = project(policyOf(testCase), fixture.fields);
    assert.deepEqual(actual, testCase.expect, `${testCase.name}: ${testCase.why}`);
  });
}

test('dispatch-policy: fixtures 覆盖 C12 全部断言维度', () => {
  const names = fixture.cases.map((c) => c.name);
  // PARCFG-01：新 faithful_fast 默认 4 / 显式 safe 仍 1 / 工具覆盖带来源
  assert.ok(names.includes('v2_faithful_fast_without_scope'));
  assert.ok(names.includes('v2_explicit_safe_kept'));
  assert.ok(names.includes('tool_override_wins'));
  // PARCFG-02：冲突点名 + 0=自动 + 非法值
  assert.ok(names.includes('v2_nested_wins_over_flat'));
  assert.ok(names.includes('v2_zero_is_auto'));
  // PARCFG-03：旧契约保持旧优先级与冻结值
  assert.ok(names.includes('legacy_flat_overrides_nested'));
  assert.ok(names.includes('frozen_legacy_not_widened'));
  // PARCFG-05：宿主能力未知记 null，Python worker 能力不被突破
  assert.ok(names.includes('host_unknown_capability_null'));
  assert.ok(names.includes('python_worker_cap_one'));
  assert.ok(names.includes('python_worker_cap_four'));
});

test('dispatch-policy: 未知契约版本硬拒绝，不按最新版本解释', () => {
  assert.throws(
    () => resolveDispatchPolicy({ config: { dispatch_contract_version: 99 } }),
    (err) => err.code === DISPATCH_CONTRACT_VERSION_UNSUPPORTED);
});

test('dispatch-policy: 契约版本常量与执行器枚举稳定', () => {
  assert.equal(DISPATCH_CONTRACT_VERSION, 2);
  assert.equal(MAX_CONCURRENCY_LIMIT, 16);
  assert.deepEqual([...EXECUTOR_KINDS].sort(), ['host_subagent', 'python_registry']);
});

test('dispatch-policy: 嵌套/扁平冲突点名两个键', () => {
  assert.throws(
    () => resolveDispatchPolicy({
      config: {
        dispatch_contract_version: 2,
        agent_dispatch: { max_concurrency: 4 },
        agent_max_concurrency: 3,
      },
    }),
    (err) => {
      assert.equal(err.code, CONFIG_CONCURRENCY_CONFLICT);
      const detail = String(err.detail || err.message || '');
      assert.ok(detail.includes('agent_dispatch.max_concurrency'), detail);
      assert.ok(detail.includes('agent_max_concurrency'), detail);
      return true;
    });
});
