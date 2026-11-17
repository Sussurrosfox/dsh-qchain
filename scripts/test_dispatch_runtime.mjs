// scripts/test_dispatch_runtime.mjs — 任务卡 08：精确模型路由 / 角色化上下文 / 输出协议 / 无跨项目写入
// 运行：node --test scripts/test_dispatch_runtime.mjs（在插件根目录）
//
// 约定：
// - 单元测试直接调用生产 helper（scripts/dispatch_runtime.mjs）。
// - E2E 测试通过真实 index.js 的 apply(ctx) 取得真实 qchain_dispatch 工具，
//   仅替换宿主 subagent（fake）；所有写盘被“写保护”拦截：落在临时目录外的写入
//   一律记录并丢弃（不会真正写出），测试据此断言没有跨项目/越界写入。
// - 不启动 qchain 流水线、不调用真实模型、不读 Phase 0 令牌。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fsDefault, * as fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import * as os from 'node:os';
import * as path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const RT = await import(pathToFileURL(path.join(HERE, 'dispatch_runtime.mjs')).href);

// ---------------------------------------------------------------- 夹具工具
function mkProject(name) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `qchain-t08-${name}-`));
  const selfDir = path.join(root, 'self');
  fs.mkdirSync(path.join(selfDir, '.pd', 'agent-requests'), { recursive: true });
  return { root, selfDir };
}
function w(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text, 'utf8');
}
function rmrf(p) { try { fs.rmSync(p, { recursive: true, force: true }); } catch (_) { /* ignore */ } }

/** 记录并拦截落在 allowedRoot 之外的写入（不真正执行）。 */
function installWriteGuard(allowedRoot) {
  const originals = {};
  const blocked = [];
  const root = path.resolve(allowedRoot).toLowerCase();
  const inside = (p) => {
    if (p == null || typeof p === 'number') return true; // fd 写入由 open 阶段决定
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

function writeRequest(selfDir, req) {
  const rel = `.pd/agent-requests/${req.dispatch_id}.json`;
  w(path.join(selfDir, rel), JSON.stringify({ status: 'pending', max_retries: 0, before: {}, ...req }, null, 2));
  return rel;
}

// ---------------------------------------------------------------- PROMPT-01 路由
test('PROMPT-01 路由：专用 stage 优先于 T2_CHECK 与 T2；缺失时按最长前缀回退', () => {
  const map = {
    T2: { provider: 'p-t2', model: 'm-t2' },
    T2_CHECK: { provider: 'p-chk', model: 'm-chk' },
    T2_CHECK_SEC1: { provider: 'p-sec1', model: 'm-sec1' },
  };
  assert.equal(RT.resolveStageRoute('T2_CHECK_SEC1', map).model, 'm-sec1');
  assert.equal(RT.resolveStageRoute('t2_check_sec2', map).model, 'm-chk', '无 SEC2 专用项 → 最长前缀 T2_CHECK');
  assert.equal(RT.resolveStageRoute('T2_SEC1', map).model, 'm-t2', '→ 更短前缀 T2');
  assert.deepEqual(RT.resolveStageRoute('T4_REVIEW_SEC1', map), {}, '无任何匹配 → 空路由（宿主默认）');
  assert.equal(RT.resolveStageRoute('T4_REVIEW_SEC1', { ...map, default: { provider: 'pd', model: 'md' } }).model, 'md');
  // 残缺条目（缺 model）不算已配置，继续回退
  const partial = { T2_CHECK_SEC1: { provider: 'x' }, T2_CHECK: { provider: 'p-chk', model: 'm-chk' } };
  assert.equal(RT.resolveStageRoute('T2_CHECK_SEC1', partial).model, 'm-chk');
});

// ---------------------------------------------------------------- PROMPT-02 来源
const SRC_MARK = 'UNIQUE_SOURCE_LINE_7f3a9c';

test('PROMPT-02 文件输入：来源只出现一次（模板含来源占位符时也不在尾部重复）', () => {
  const { root, selfDir } = mkProject('src1');
  try {
    const src = path.join(root, 'ch1.tex');
    w(src, `\\section{A}\n${SRC_MARK}\n$x^2+y^2=z^2$\n`);
    const req = {
      dispatch_id: 'd1', stage: 'T1_SOURCE_AUDIT', role: 'source-auditor', prompt_file: 'research_prompt.md',
      artifacts: ['source-map.md'], instruction: 'x', scope: '',
      context: { input_path: src },
    };
    const { prompt, meta } = RT.buildDispatchPrompt(req, { selfDir, engineRoot: ROOT });
    assert.equal(prompt.split(SRC_MARK).length - 1, 1, '来源行恰好一次');
    assert.equal(meta.source_block_count, 1);
    assert.equal(meta.source_placement, 'template', 'research 模板含 {{SOURCE_EXCERPTS}}，来源放在模板位置');
    assert.equal(meta.source_files[0].path, 'ch1.tex');
    assert.match(meta.source_files[0].sha256, /^[0-9a-f]{64}$/);
    // 作者模板没有来源占位符 → 来源在尾部，仍只一次
    const a = RT.buildDispatchPrompt({
      ...req, stage: 'T2_SEC1', prompt_file: 'subagent_prompt.md', artifacts: ['sec1.tex'], scope: 'sec1',
      context: { input_path: src, section_id: 'sec1' },
    }, { selfDir, engineRoot: ROOT });
    assert.equal(a.prompt.split(SRC_MARK).length - 1, 1);
    assert.equal(a.meta.source_placement, 'tail');
  } finally { rmrf(root); }
});

test('PROMPT-02 同一模板含多个来源占位符时来源块仍只出现一次', () => {
  const { root } = mkProject('src2');
  try {
    const out = RT.renderTemplate('A {{SOURCE_EXCERPTS}} B {{RAW_INPUT}} C {{MATERIAL_CONTENT}}', {}, { sourceBlock: 'BLOCK' });
    assert.equal(out.text.split('BLOCK').length - 1, 1);
  } finally { rmrf(root); }
});

test('PROMPT-02 目录输入 + 数组 inventory：不抛 EISDIR，来源完整', () => {
  const { root, selfDir } = mkProject('src3');
  try {
    const dir = path.join(root, 'material');
    w(path.join(dir, 'a.tex'), `A-${SRC_MARK}\n`);
    w(path.join(dir, 'sub', 'b.md'), 'B-body\n');
    w(path.join(dir, 'ignored.png'), 'binary');
    const inventory = [
      { source_id: 'SRC-001', source_file: 'a.tex', locator: 'L1-1', kind: 'claim', disposition: 'preserve' },
      { source_id: 'SRC-002', source_file: 'sub/b.md', locator: 'L1-1', kind: 'claim', disposition: 'preserve' },
      { source_id: 'SRC-003', source_file: 'a.tex', locator: 'L2-2', kind: 'claim', disposition: 'preserve' },
    ];
    const got = RT.collectSources({ inputPath: dir, inventory, baseDir: root });
    assert.deepEqual(got.sources.map((s) => s.rel), ['a.tex', 'sub/b.md'], 'inventory 去重并保序');
    assert.equal(got.missing.length, 0);
    // 无 inventory：按目录枚举文本文件（忽略 png）
    const walked = RT.collectSources({ inputPath: dir, inventory: [], baseDir: root });
    assert.deepEqual(walked.sources.map((s) => s.rel).sort(), ['a.tex', 'sub/b.md']);
    // 数组输入（文件 + 目录混合）
    const mixed = RT.collectSources({ inputPath: [path.join(dir, 'a.tex'), path.join(dir, 'sub')], inventory: [], baseDir: root });
    assert.deepEqual(mixed.sources.map((s) => s.rel).sort(), ['a.tex', 'b.md']);
    // 整条 prompt：清单表 + 两个文件各一次
    const { prompt } = RT.buildDispatchPrompt({
      dispatch_id: 'd2', stage: 'T1_SOURCE_AUDIT', role: 'r', prompt_file: 'research_prompt.md',
      artifacts: ['source-map.md'], instruction: 'x',
      context: { input_path: dir, source_inventory: inventory, source_inventory_digest: 'sha256:abc' },
    }, { selfDir, engineRoot: ROOT });
    assert.equal(prompt.split(`A-${SRC_MARK}`).length - 1, 1);
    assert.ok(prompt.includes('B-body'));
    assert.ok(prompt.includes('| SRC-002 | sub/b.md |'), '清单表保留 source_id/路径');
  } finally { rmrf(root); }
});

test('PROMPT-02 声明了但读不到的来源 → SOURCE_UNREADABLE（不静默跳过）', () => {
  const { root, selfDir } = mkProject('src4');
  try {
    assert.throws(() => RT.buildDispatchPrompt({
      dispatch_id: 'd', stage: 'T2_SEC1', role: 'r', prompt_file: 'subagent_prompt.md',
      artifacts: ['sec1.tex'], instruction: 'x', scope: 'sec1',
      context: { input_path: path.join(root, 'missing.tex') },
    }, { selfDir, engineRoot: ROOT }), (e) => e.code === 'SOURCE_UNREADABLE');
  } finally { rmrf(root); }
});

// ---------------------------------------------------------------- PROMPT-03 无截断
test('PROMPT-03 映射表：全文优先，末尾关键行可见；超长按 scope 过滤并显式标注；仍超限则报错', () => {
  const header = '| source_id | scope | action |\n| --- | --- | --- |\n';
  const filler = Array.from({ length: 400 }, (_, i) => `| SRC-${String(i).padStart(3, '0')} | sec9 | PRESERVED filler-row-${i} |`).join('\n');
  const small = `${header}${filler.split('\n').slice(0, 20).join('\n')}\n| SRC-TAIL | sec1 | TAIL_KEY_ROW |`;
  assert.ok(small.length > 100 && small.length < 3000 + 5000);
  assert.ok(RT.selectMapRows(small, ['sec1']).text.includes('TAIL_KEY_ROW'), '未超限：全文');
  // 超过旧的 3000 字符上限，但低于新上限：关键尾行仍在（旧 slice(0,3000) 会丢）
  const mid = `${header}${filler}\n| SRC-TAIL | sec1 | TAIL_KEY_ROW |`;
  assert.ok(mid.length > 3000);
  const full = RT.selectMapRows(mid, ['sec1']);
  assert.equal(full.filtered, false);
  assert.ok(full.text.includes('TAIL_KEY_ROW'));
  // 超限：按 scope 过滤，尾部相关行保留，且有省略标注
  const sel = RT.selectMapRows(mid, ['sec1'], { limit: 2000 });
  assert.equal(sel.filtered, true);
  assert.ok(sel.text.includes('TAIL_KEY_ROW'));
  assert.ok(sel.text.includes('| source_id | scope | action |'), '表头保留');
  assert.ok(!sel.text.includes('filler-row-399'));
  assert.match(sel.text, /此处省略 \d+ 行不相关记录/);
  assert.ok(sel.omitted_lines > 300);
  // scope 精确匹配：sec1 不命中 sec10
  const sec10 = `${header}| A | sec10 | x |\n| B | sec1 | y |\n${filler}`;
  const s2 = RT.selectMapRows(sec10, ['sec1'], { limit: 1500 });
  assert.ok(s2.text.includes('| B | sec1 | y |') && !s2.text.includes('| A | sec10 | x |'));
  // 没有 scope 且超限 → 明确报错
  assert.throws(() => RT.selectMapRows(mid, [], { limit: 2000 }), (e) => e.code === 'MAP_TOO_LARGE');
  // 过滤后仍超限 → 明确报错
  assert.throws(() => RT.selectMapRows(`${header}${filler.replaceAll('sec9', 'sec1')}`, ['sec1'], { limit: 2000 }), (e) => e.code === 'MAP_TOO_LARGE');
});

test('PROMPT-03 完整 prompt 超限明确报错 PROMPT_TOO_LARGE；未超限时大来源不被截断', () => {
  const { root, selfDir } = mkProject('big');
  try {
    const src = path.join(root, 'big.tex');
    const body = Array.from({ length: 6000 }, (_, i) => `line ${i} $a_${i}$`).join('\n') + `\n${SRC_MARK}-END`;
    w(src, body);
    const req = {
      dispatch_id: 'd', stage: 'T2_SEC1', role: 'r', prompt_file: 'subagent_prompt.md',
      artifacts: ['sec1.tex'], instruction: 'x', scope: 'sec1', context: { input_path: src },
    };
    const ok = RT.buildDispatchPrompt(req, { selfDir, engineRoot: ROOT });
    assert.ok(ok.prompt.includes(`${SRC_MARK}-END`), '末行公式/标记完整保留');
    assert.equal(ok.meta.truncated, false);
    assert.equal(ok.meta.source_chars, body.length);
    assert.throws(() => RT.buildDispatchPrompt(req, { selfDir, engineRoot: ROOT, maxPromptChars: 5000 }),
      (e) => e.code === 'PROMPT_TOO_LARGE' && e.detail.prompt_chars > 5000);
  } finally { rmrf(root); }
});

test('PROMPT-03 作者映射表：旧版 slice(0,3000) 会丢的尾行现在出现在 prompt 中', () => {
  const { root, selfDir } = mkProject('maps');
  try {
    const rows = Array.from({ length: 200 }, (_, i) => `| SRC-${i} | sec9 | PRESERVED filler-${i} |`).join('\n');
    w(path.join(selfDir, 'source-map.md'), `| id | scope | action |\n| - | - | - |\n${rows}\n| SRC-TAIL | sec1 | TAIL_MAP_ROW |\n`);
    const { prompt, meta } = RT.buildDispatchPrompt({
      dispatch_id: 'd', stage: 'T2_SEC1', role: 'r', prompt_file: 'subagent_prompt.md',
      artifacts: ['sec1.tex'], instruction: 'x', scope: 'sec1', context: { section_id: 'sec1' },
    }, { selfDir, engineRoot: ROOT });
    assert.ok(prompt.includes('TAIL_MAP_ROW'));
    assert.equal(meta.maps['source-map.md'].filtered, false);
  } finally { rmrf(root); }
});

// ---------------------------------------------------------------- PROMPT-04 批次产物
test('PROMPT-04 两节批次：任务书与输出协议列出两个产物；越界路径与 scope 不一致被拒绝', () => {
  const { selfDir } = mkProject('batch');
  const req = {
    dispatch_id: 'd', stage: 'T2_WRITE_BATCH', role: 'batch author', prompt_file: 'subagent_prompt.md',
    artifacts: ['sec1.tex', 'sec2.tex'], instruction: 'x', scope: 'sec1', scopes: ['sec1', 'sec2'],
    artifact_scopes: { 'sec1.tex': 'sec1', 'sec2.tex': 'sec2' }, context: {},
  };
  const { prompt, meta } = RT.buildDispatchPrompt(req, { selfDir, engineRoot: ROOT });
  assert.deepEqual(meta.artifacts, ['sec1.tex', 'sec2.tex']);
  for (const a of ['sec1.tex', 'sec2.tex']) {
    assert.ok(prompt.includes(`<<<FILE:${a}>>>`), `${a} FILE 块`);
    assert.ok(prompt.includes(`<<<END:${a}>>>`));
    assert.ok(prompt.includes(`- ${a}（scope: ${a.slice(0, 4)}）`));
  }
  assert.ok(prompt.includes('scopes（本派发单覆盖的全部节，须逐节完成）: sec1, sec2'));
  assert.ok(prompt.includes('白名单共 2 个产物'));
  for (const bad of ['../evil.tex', '/abs/evil.tex', 'C:\\evil.tex', 'a/../../evil.tex']) {
    assert.throws(() => RT.buildDispatchPrompt({ ...req, artifacts: ['sec1.tex', bad], artifact_scopes: undefined }, { selfDir, engineRoot: ROOT }),
      (e) => e.code === 'ARTIFACT_PATH_INVALID', bad);
  }
  assert.throws(() => RT.buildDispatchPrompt({ ...req, artifact_scopes: { 'sec1.tex': 'sec1' } }, { selfDir, engineRoot: ROOT }),
    (e) => e.code === 'ARTIFACT_SCOPE_MISMATCH');
  assert.throws(() => RT.buildDispatchPrompt({ ...req, artifact_scopes: { 'sec1.tex': 'sec1', 'sec2.tex': 'sec7' } }, { selfDir, engineRoot: ROOT }),
    (e) => e.code === 'ARTIFACT_SCOPE_MISMATCH');
  assert.throws(() => RT.buildDispatchPrompt({ ...req, artifacts: ['sec1.tex', 'sec1.tex'], artifact_scopes: undefined }, { selfDir, engineRoot: ROOT }),
    (e) => e.code === 'ARTIFACT_PATH_INVALID');
});

// ---------------------------------------------------------------- 角色化协议与上下文
test('输出协议按角色：作者/规划=FILE 块（无 JSON 指令），审读=FILE 块首选 + JSON 备选', () => {
  const { root, selfDir } = mkProject('proto');
  try {
    w(path.join(selfDir, 'sec1.tex'), '\\section*{I x}\nCANDIDATE_BODY\n');
    const author = RT.buildDispatchPrompt({
      dispatch_id: 'a', stage: 'T2_SEC1', role: 'r', prompt_file: 'subagent_prompt.md',
      artifacts: ['sec1.tex'], instruction: 'x', scope: 'sec1', context: {},
    }, { selfDir, engineRoot: ROOT });
    assert.equal(author.meta.role, 'author');
    assert.ok(author.prompt.includes('<<<FILE:sec1.tex>>>'));
    assert.ok(!/\{"files"\s*:/.test(author.prompt), '作者 prompt 不再含 {"files": ...} JSON 指令');
    assert.ok(!author.prompt.includes('备选输出格式'));
    const planning = RT.buildDispatchPrompt({
      dispatch_id: 'p', stage: 'T1_SOURCE_AUDIT', role: 'r', prompt_file: 'research_prompt.md',
      artifacts: ['source-map.md'], instruction: 'x', context: {},
    }, { selfDir, engineRoot: ROOT });
    assert.equal(planning.meta.role, 'planning');
    assert.ok(planning.prompt.includes('<<<FILE:source-map.md>>>'));
    assert.ok(!/\{"files"\s*:/.test(planning.prompt));
    const review = RT.buildDispatchPrompt({
      dispatch_id: 'r', stage: 'T2_CHECK_SEC1', role: 'r', prompt_file: 'semantic_check_prompt.md',
      artifacts: ['.pd/semantic/sec1.json'], instruction: 'x', scope: 'sec1',
      context: { section_id: 'sec1', section_digest: 'abc' },
    }, { selfDir, engineRoot: ROOT });
    assert.equal(review.meta.role, 'review');
    // P2-2：审读角色的首选协议改为 FILE 块（大 JSON 的括号/转义错误是 T4 批
    // 5 轮里 4 轮失败的直接原因），单一 JSON 对象仍作为备选被接受——解析器
    // 两条路都认，故这里断言「两种格式都在、且 FILE 块在前」。
    assert.ok(review.prompt.includes('<<<FILE:.pd/semantic/sec1.json>>>'),
      '审读 prompt 首选 FILE 块指令');
    assert.ok(review.prompt.includes('{"files": {"<相对路径>": "<完整文件内容>"}}'),
      '单一 JSON 对象作为备选仍被说明（解析器兼容）');
    // 只看协议段内部（模板正文里也可能出现 {"files"，那不算协议顺序）
    const proto = review.prompt.slice(review.prompt.lastIndexOf('## 输出协议'));
    assert.ok(proto.indexOf('<<<FILE:') < proto.indexOf('{"files":'),
      'FILE 块必须排在 JSON 备选之前，否则模型仍会先试大 JSON');
    assert.ok(review.prompt.includes('- .pd/semantic/sec1.json'));
    assert.ok(review.prompt.includes('CANDIDATE_BODY'), '被审候选全文必须在 prompt 中（缺失时旧版根本没有）');
    assert.equal(review.meta.review_targets[0].scope, 'sec1');
  } finally { rmrf(root); }
});

test('P2-1 提示词分层：layered 只内嵌摘要 + 附录索引，full 保持全量', () => {
  const { root, selfDir } = mkProject('layered');
  try {
    w(path.join(selfDir, 'sec1.tex'), 'CANDIDATE');
    // 大映射表：足够长，超出 layeredMapChars 才会触发过滤/截断
    const rows = [];
    for (let i = 0; i < 400; i += 1) {
      rows.push(`| SRC-${String(i).padStart(3, '0')} | sec${(i % 2) + 1} | x | y |`);
    }
    w(path.join(selfDir, 'source-map.md'), `| 原文节点 | 学案位置 |\n| --- | --- |\n${rows.join('\n')}\n`);
    w(path.join(selfDir, '.pd', 'review', 'student-pages.txt'), 'P'.repeat(30000));
    const base = {
      dispatch_id: 'd', stage: 'T4_REVIEW_SEC1', role: 'r',
      prompt_file: 'acceptance_prompt.md',
      artifacts: ['.pd/acceptance/sec1.json'], instruction: 'x', scope: 'sec1',
      context: { section_id: 'sec1' },
    };
    const layered = RT.buildDispatchPrompt(base, { selfDir, engineRoot: ROOT });
    const full = RT.buildDispatchPrompt(
      { ...base, context: { ...base.context, prompt_profile: 'full' } },
      { selfDir, engineRoot: ROOT });
    assert.equal(layered.meta.prompt_profile, 'layered');
    assert.equal(full.meta.prompt_profile, 'full');
    assert.ok(layered.meta.prompt_chars < full.meta.prompt_chars,
      'layered 必须比 full 短（否则分层没生效）');
    assert.ok(layered.prompt.includes('## 附录索引'),
      'layered 必须给出附录索引（路径 + 尺寸）');
    assert.ok(layered.prompt.includes('source-map.md'), '附录索引须列出映射表路径');
    assert.ok(layered.prompt.includes('投递通道的展示折叠'),
      '必须提示"可见载荷被折叠 ≠ 内容缺失"，阻断子 Agent 的通读冲动');
    assert.ok(!full.prompt.includes('## 附录索引'), 'full 档不出现附录索引');
    // 分层不改必读层：被审候选全文仍在
    assert.ok(layered.prompt.includes('CANDIDATE'), '必须保留被审对象全文');
    // 字数账可用于运维定位
    assert.equal(typeof layered.meta.layers.maps, 'number');
    assert.ok(layered.meta.layers.maps <= RT.buildDispatchPrompt(base, { selfDir, engineRoot: ROOT }).meta.layers.maps + 1);
  } finally { rmrf(root); }
});

test('终审缺少被审候选全文 → REVIEW_TARGET_MISSING', () => {
  const { selfDir } = mkProject('rev-missing');
  assert.throws(() => RT.buildDispatchPrompt({
    dispatch_id: 'r', stage: 'T2_CHECK_SEC1', role: 'r', prompt_file: 'semantic_check_prompt.md',
    artifacts: ['.pd/semantic/sec1.json'], instruction: 'x', scope: 'sec1', context: {},
  }, { selfDir, engineRoot: ROOT }), (e) => e.code === 'REVIEW_TARGET_MISSING');
});

test('合并终审：被审 scope 全部出现，来源仍只一次', () => {
  const { root, selfDir } = mkProject('rev-multi');
  try {
    w(path.join(selfDir, 'sec1.tex'), 'SEC1_BODY');
    w(path.join(selfDir, 'sec2.tex'), 'SEC2_BODY');
    const src = path.join(root, 's.tex');
    w(src, SRC_MARK);
    const { prompt } = RT.buildDispatchPrompt({
      dispatch_id: 'r', stage: 'T4_REVIEW_BATCH', role: 'r', prompt_file: 'acceptance_prompt.md',
      artifacts: ['.pd/acceptance/sec1.json', '.pd/acceptance/sec2.json'], instruction: 'x',
      scope: 'sec1', scopes: ['sec1', 'sec2'], context: { input_path: src },
    }, { selfDir, engineRoot: ROOT });
    assert.ok(prompt.includes('SEC1_BODY') && prompt.includes('SEC2_BODY'));
    assert.equal(prompt.split(SRC_MARK).length - 1, 1);
  } finally { rmrf(root); }
});

test('作者拿到完整 TeX 速查段（不再固定截 2500 字符）', () => {
  const guide = fs.readFileSync(path.join(ROOT, 'references', 'tex-interface.md'), 'utf8');
  const idx = guide.indexOf('## 三、 写作者速查');
  if (idx === -1) return; // 参考文档无该节则无可断言
  const next = guide.slice(idx + 10).search(/\n## /);
  const section = next === -1 ? guide.slice(idx) : guide.slice(idx, idx + 10 + next);
  const { selfDir } = mkProject('guide');
  const { prompt } = RT.buildDispatchPrompt({
    dispatch_id: 'a', stage: 'T2_SEC1', role: 'r', prompt_file: 'subagent_prompt.md',
    artifacts: ['sec1.tex'], instruction: 'x', scope: 'sec1', context: {},
  }, { selfDir, engineRoot: ROOT });
  assert.ok(prompt.includes(section.trimEnd()));
});

// ---------------------------------------------------------------- 无副作用 / 无跨项目写入
test('parseSubagentFiles 无任何磁盘副作用；FILE 块与 JSON 两种格式均可解析', () => {
  const guard = installWriteGuard(os.tmpdir() + path.sep + 'qchain-t08-parse-never');
  try {
    const a = RT.parseSubagentFiles({ runId: 'r1', output: [{ type: 'text', text: '<<<FILE:sec1.tex>>>\n\\a\n<<<END:sec1.tex>>>' }] });
    assert.deepEqual(a.files, { 'sec1.tex': '\\a' });
    const b = RT.parseSubagentFiles('{"files":{"x.json":"{}"}}');
    assert.deepEqual(b.files, { 'x.json': '{}' });
    const c = RT.parseSubagentFiles('');
    assert.equal(c.files, null);
    assert.deepEqual(guard.blocked, [], '解析过程不得发起任何写入');
  } finally { guard.restore(); }
});

test('appendDebugRecord 只落当前 self/.pd，且只含元数据', () => {
  const { root, selfDir } = mkProject('dbg');
  const guard = installWriteGuard(root);
  try {
    assert.equal(RT.appendDebugRecord(selfDir, { event: 'attempt_start', prompt_chars: 10, prompt_sha256: 'x' }), true);
    const p = path.join(selfDir, '.pd', 'debug_subagent.log');
    assert.ok(fs.existsSync(p));
    assert.deepEqual(guard.blocked, []);
    const rec = JSON.parse(fs.readFileSync(p, 'utf8').trim());
    assert.equal(rec.event, 'attempt_start');
  } finally { guard.restore(); rmrf(root); }
});

test('源码卫生：index.js 与 dispatch_runtime.mjs 不含跨项目硬编码路径', () => {
  for (const f of ['index.js', path.join('scripts', 'dispatch_runtime.mjs')]) {
    const text = fs.readFileSync(path.join(ROOT, f), 'utf8');
    assert.ok(!/Yau/.test(text), `${f} 含 Yau 项目路径`);
    assert.ok(!/subagent_parsed_keys|subagent_last_reply/.test(text), `${f} 仍含旧调试文件名`);
    // 允许 resolvePythonBin 的 C:\PythonXXX 解释器候选；禁止指向项目/调试目录的盘符路径
    assert.ok(!/['"`][A-Za-z]:(\\\\|\/)[^'"`\n]*(teaching|[\\/]\.pd|[\\/]self|just|linear)/i.test(text), `${f} 含指向项目/调试目录的盘符硬编码路径`);
  }
});

// ---------------------------------------------------------------- E2E：真实 index.js 的 qchain_dispatch 工具
test('E2E 路由：专用 T2_CHECK_SEC1 被宿主 subagent 实际收到，且无越界写入', async () => {
  const { root, selfDir } = mkProject('e2e-route');
  const guard = installWriteGuard(root);
  try {
    w(path.join(selfDir, 'config.yaml'), [
      'agent_dispatch:',
      '  profile: safe',
      '  model_map:',
      '    T2: {provider: p-low, model: m-low}',
      '    T2_CHECK: {provider: p-chk, model: m-chk}',
      '    T2_CHECK_SEC1: {provider: p-sec1, model: m-sec1}',
      '',
    ].join('\n'));
    w(path.join(selfDir, 'sec1.tex'), 'E2E_CANDIDATE');
    const calls = [];
    const tool = await loadTool(calls, () => ({
      runId: 'rr', output: [{ type: 'text', text: JSON.stringify({ files: { '.pd/semantic/sec1.json': { conclusion: 'PASS' } } }) }],
    }));
    const rel = writeRequest(selfDir, {
      dispatch_id: 'dsp-chk-sec1', stage: 'T2_CHECK_SEC1', role: 'checker', prompt_file: 'prompts/semantic_check_prompt.md',
      artifacts: ['.pd/semantic/sec1.json'], instruction: 'check', scope: 'sec1', context: { section_id: 'sec1' },
    });
    const res = await tool.execute({ self_dir: selfDir, request_ref: rel }, {});
    assert.equal(res.success, true, res.report);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].provider, 'p-sec1');
    assert.equal(calls[0].model, 'm-sec1');
    assert.ok(calls[0].prompt.includes('E2E_CANDIDATE'));
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
    const ev = JSON.parse(fs.readFileSync(path.join(selfDir, '.pd', 'agent-calls', 'dsp-chk-sec1.json'), 'utf8'));
    assert.equal(ev.model, 'm-sec1');
  } finally { guard.restore(); rmrf(root); }
});

test('E2E 来源/协议/批产物/映射尾行：目录输入、两节批次、无越界写入', async () => {
  const { root, selfDir } = mkProject('e2e-batch');
  const guard = installWriteGuard(root);
  try {
    const dir = path.join(root, 'material');
    w(path.join(dir, 'a.tex'), `A-${SRC_MARK}\n`);
    const rows = Array.from({ length: 200 }, (_, i) => `| SRC-${i} | sec9 | filler-${i} |`).join('\n');
    w(path.join(selfDir, 'source-map.md'), `| id | scope | action |\n| - | - | - |\n${rows}\n| SRC-TAIL | sec1 | E2E_TAIL_ROW |\n`);
    const calls = [];
    const tool = await loadTool(calls, (a) => {
      const arts = ['sec1.tex', 'sec2.tex'];
      return { runId: 'rr', output: [{ type: 'text', text: fileBlocks(arts) }] };
    });
    const rel = writeRequest(selfDir, {
      dispatch_id: 'dsp-batch', stage: 'T2_WRITE_BATCH', role: 'batch author', prompt_file: 'prompts/subagent_prompt.md',
      artifacts: ['sec1.tex', 'sec2.tex'], instruction: 'write', scope: 'sec1', scopes: ['sec1', 'sec2'],
      artifact_scopes: { 'sec1.tex': 'sec1', 'sec2.tex': 'sec2' },
      context: { input_path: dir, source_inventory: [{ source_id: 'SRC-1', source_file: 'a.tex', locator: 'L1', kind: 'claim', disposition: 'preserve' }] },
    });
    const res = await tool.execute({ self_dir: selfDir, request_ref: rel }, {});
    assert.equal(res.success, true, res.report);
    const prompt = calls[0].prompt;
    assert.equal(prompt.split(`A-${SRC_MARK}`).length - 1, 1, '目录输入的来源出现且只出现一次');
    assert.ok(prompt.includes('E2E_TAIL_ROW'), '映射表尾部关键行可见');
    assert.ok(prompt.includes('<<<FILE:sec1.tex>>>') && prompt.includes('<<<FILE:sec2.tex>>>'));
    assert.ok(fs.existsSync(path.join(selfDir, 'sec2.tex')));
    assert.deepEqual(guard.blocked, [], `越界写入: ${JSON.stringify(guard.blocked)}`);
  } finally { guard.restore(); rmrf(root); }
});

test('E2E prompt 构造失败（越界产物/来源缺失）不会调用 subagent，也不落假证据', async () => {
  const { root, selfDir } = mkProject('e2e-fail');
  const guard = installWriteGuard(root);
  try {
    const calls = [];
    const tool = await loadTool(calls, () => ({ output: [{ type: 'text', text: '' }] }));
    const rel = writeRequest(selfDir, {
      dispatch_id: 'dsp-bad', stage: 'T2_SEC1', role: 'author', prompt_file: 'prompts/subagent_prompt.md',
      artifacts: ['../escape.tex'], instruction: 'x', scope: 'sec1', context: {},
    });
    const res = await tool.execute({ self_dir: selfDir, request_ref: rel }, {});
    assert.equal(res.success, false);
    assert.equal(calls.length, 0, '构造失败不得调用 subagent');
    assert.match(res.results[0].error, /ARTIFACT_PATH_INVALID/);
    assert.ok(!fs.existsSync(path.join(selfDir, '.pd', 'agent-calls', 'dsp-bad.json')));
    assert.deepEqual(guard.blocked, []);
  } finally { guard.restore(); rmrf(root); }
});
