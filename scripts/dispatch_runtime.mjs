// scripts/dispatch_runtime.mjs — qchain_dispatch 的可测试运行时 helper（任务卡 08）
//
// 生产入口 index.js 直接 import 本模块；测试（scripts/test_dispatch_runtime.mjs）
// 复用同一份实现，不另写仅测试用的模拟。所有文件系统访问经 env.fs / env.path
// 注入，便于测试使用虚拟 fs。本模块不写任何磁盘，唯一的写入是 appendDebugRecord，
// 且只落当前 self/.pd/ 下。
//
// 提供：
//   resolveStageRoute     完整 stage → 最长已配置前缀 → 更短前缀 → DEFAULT → {}
//   classifyRole          planning | author | review（输出协议与上下文按角色唯一化）
//   collectSources        文件 / 目录 / 数组来源 → 带路径与摘要的完整来源列表（不抛 EISDIR）
//   selectMapRows         三张映射表：不超限则全文；超限按 scope 过滤并显式标注，禁止固定 slice
//   buildDispatchPrompt   角色化上下文包 + 唯一 SOURCE 块 + 唯一输出协议
//   parseSubagentFiles    子 Agent 回复 → {files}（FILE 块 / JSON；无任何磁盘副作用）
//   appendDebugRecord     仅元数据的调试记录（self/.pd/debug_subagent.log）

import * as nodeFs from 'node:fs';
import * as nodePath from 'node:path';
import nodeCrypto from 'node:crypto';
import { normalizeRel } from './path_safety.mjs';

export const DEFAULTS = Object.freeze({
  maxPromptChars: 600000, // 完整 prompt 字符上限；超出明确报错，不静默截断
  maxMapChars: 40000, // 单张映射表全文上限；超出才按 scope 过滤
  maxSourceFiles: 200, // 目录输入无 inventory 时最多枚举的文本文件数
  // P2-1 提示词分层：layered 把"必读层"压到约 40K 以内（任务书 + 被审对象 +
  // 证据摘要），映射表与学生版页文本降为"附录层：只给路径 + 摘要 + 行数"。
  // 宿主投递通道会折叠超长载荷的中段（实测 T4 233K 提示词里任务书被折掉约
  // 90K token，子 Agent 察觉后转去读盘补全、耗尽预算），故必读层必须自足。
  promptProfile: 'layered', // full | layered
  layeredMapChars: 12000, // 分层模式下每张映射表最多内嵌的字符数
  layeredPackChars: 8000, // 分层模式下 student-pages.txt 最多内嵌的字符数
});

export const PROMPT_PROFILES = ['full', 'layered'];

export class DispatchPromptError extends Error {
  constructor(code, message, detail = {}) {
    super(`${code}: ${message}`);
    this.name = 'DispatchPromptError';
    this.code = code;
    this.detail = detail;
  }
}

function mkEnv(env = {}) {
  return {
    fs: env.fs || nodeFs,
    path: env.path || nodePath,
    crypto: env.crypto || nodeCrypto,
  };
}

function sha256Text(crypto, text) {
  return crypto.createHash('sha256').update(String(text), 'utf8').digest('hex');
}

// ---------------------------------------------------------------------------
// 1. 模型路由
// ---------------------------------------------------------------------------

/** 'T2_CHECK_SEC1' → ['T2_CHECK_SEC1', 'T2_CHECK', 'T2']（最具体者在前）。 */
export function stageRouteCandidates(stage) {
  const s = String(stage || '').trim().toUpperCase();
  if (!s) return [];
  const parts = s.split('_').filter((p) => p !== '');
  const out = [];
  for (let n = parts.length; n >= 1; n -= 1) out.push(parts.slice(0, n).join('_'));
  return out;
}

/**
 * 完整 stage → 最长已配置前缀 → 更短前缀 → DEFAULT/* → {}。
 * 只有同时带 provider 与 model 的条目才算“已配置”，残缺条目跳过并继续回退。
 */
export function resolveStageRoute(stage, modelMap) {
  const norm = {};
  if (modelMap && typeof modelMap === 'object') {
    for (const [k, v] of Object.entries(modelMap)) norm[String(k).trim().toUpperCase()] = v;
  }
  for (const key of [...stageRouteCandidates(stage), 'DEFAULT', '*']) {
    const route = norm[key];
    if (route && route.provider && route.model) {
      // 额外键（可选）：reasoning_effort（'lite'|'light'|'balanced'|'deep' 等，按供应商档位）、
      // max_tokens（正整数，覆盖默认输出预算；小输出上限模型可显式调小避免 API 拒绝）。
      const extra = {};
      if (route.reasoning_effort) extra.reasoning_effort = String(route.reasoning_effort);
      if (route.max_tokens) {
        const mt = Number(route.max_tokens);
        if (Number.isFinite(mt) && mt > 0) extra.max_tokens = mt;
      }
      return { provider: String(route.provider), model: String(route.model), matched_key: key, ...extra };
    }
  }
  return {};
}

// ---------------------------------------------------------------------------
// 2. 角色与输出协议
// ---------------------------------------------------------------------------

export function classifyRole(request) {
  const req = request || {};
  const stage = String(req.stage || '').toUpperCase();
  const promptFile = String(req.prompt_file || '').replace(/\\/g, '/').split('/').pop().toLowerCase();
  if (promptFile === 'semantic_check_prompt.md' || promptFile === 'acceptance_prompt.md'
      || stage.startsWith('T2_CHECK') || stage.startsWith('T4') || stage.includes('REVIEW')) {
    return 'review';
  }
  if (promptFile === 'research_prompt.md' || stage.startsWith('T1')) return 'planning';
  return 'author';
}

export function normalizeScopes(request) {
  const req = request || {};
  const raw = Array.isArray(req.scopes) && req.scopes.length ? req.scopes : [req.scope];
  const out = [];
  for (const s of raw) {
    const v = String(s == null ? '' : s).trim();
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}

export function outputProtocol(role, artifacts) {
  const list = artifacts.map((a) => `- ${a}`).join('\n');
  if (role === 'review') {
    // P2-2：审读产物改以 FILE 块为首选（哨兵协议），大 JSON 仍接受但降为备选。
    // 实测 T4 批 5 轮里 4 轮死在 JSON 结构上（括号未闭合、键放在 files 之外、
    // 字符串内未转义反斜杠）；FILE 块没有嵌套与转义问题，且解析器两条路都认。
    const blocks = artifacts.map((a) => `<<<FILE:${a}>>>\n<${a} 的完整内容>\n<<<END:${a}>>>`);
    return [
      '## 输出协议（本角色首选 FILE 块；单一 JSON 对象也接受）',
      '【硬性约束，凌驾于本任务书一切其他表述之上】你的最终回复中严禁出现任何',
      '分析、推理、审读笔记、计划、复述或解释性文字——只要出现非产物内容，',
      '本次派发即判失败。所有思考必须在内部完成；先在内部审读完，再一次性给出',
      `下列 ${artifacts.length} 个白名单产物。`,
      '**首选格式（无嵌套、无转义，最不易出错）**：每个产物一个 FILE 块，文件名与白名单逐字一致：',
      blocks.join('\n'),
      `**备选格式**：也可以只给一个可解析的 JSON 对象 {"files": {"<相对路径>": "<完整内容>"}}，键必须恰好等于下列 ${artifacts.length} 个路径。两种格式都不会被额外加分，FILE 块更稳。`,
      '白名单路径（缺任何一个即判该派发单失败）：',
      list,
    ].join('\n');
  }
  const blocks = artifacts.map((a) => `<<<FILE:${a}>>>\n<${a} 的完整原始内容，直接粘贴，不做任何转义>\n<<<END:${a}>>>`);
  return [
    '## 输出协议（本角色唯一协议：FILE 块；不要输出 JSON）',
    `白名单共 ${artifacts.length} 个产物，必须各输出一个 FILE 块；LaTeX/Markdown 内容原样粘贴（反斜杠只写一个 \\），文件名与白名单逐字一致；块外不要任何解释文字：`,
    blocks.join('\n'),
    `缺少任何一个白名单产物，将整批判失败。`,
  ].join('\n');
}

const EXECUTION_RULES = [
  '## 执行约束（最高优先级，覆盖上文任何相反表述）',
  '- 严禁调用 subagent 或任何“派发/二次委托”类工具；你本人就是执行者，必须亲自完成本任务。',
  '- 严禁以相同或近似参数反复调用同一个工具；所需信息一次读取到位，之后直接产出结果。',
  '- 允许使用 read/grep/glob 只读工具查看输入；不得使用 write/edit/apply_patch 等写盘工具（宿主会按白名单原子写盘）。',
  '- 【反探索】严禁阅读或运行本任务书之外的任何「流水线自身实现」——包括但不限于 scripts/ 下的 check_*.py、pdstate.py、workflow/ 目录、prompts/ 模版、插件 index.js、宿主调度器源码。本任务书已经包含你需要的全部规则与输入；评分与校验逻辑与你无关，理解它们不会帮助交付。此类阅读只会消耗你的时间与输出预算，并直接导致超时或截断失败。',
  '- 【反探索】不要试图“先弄清整套系统如何工作再动手”。按任务书逐条执行即可：读懂输入 → 直接产出白名单产物。对系统的任何好奇心都是本任务失败的最大风险。',
  '- 【产出优先】先把全部白名单产物完整写出来，这是唯一的交付物；写完之后若还有余力，再对照任务书自查一遍，绝不要在产出之前做任何“准备工作”。',
].join('\n');

// ---------------------------------------------------------------------------
// 3. 产物白名单校验
// ---------------------------------------------------------------------------

/** 最近已存在祖先的 realpath + 剩余段回填（目标不存在也能判定；junction 会被解开）。 */
function realKeyOf(fs, path, target) {
  let cur = path.resolve(target);
  const rest = [];
  for (;;) {
    try {
      const real = typeof fs.realpathSync.native === 'function'
        ? fs.realpathSync.native(cur)
        : fs.realpathSync(cur);
      return rest.length ? path.join(real, ...rest.slice().reverse()) : real;
    } catch (err) {
      if (!err || (err.code !== 'ENOENT' && err.code !== 'ENOTDIR')) throw err;
      const parent = path.dirname(cur);
      if (parent === cur) {
        return rest.length ? path.join(cur, ...rest.slice().reverse()) : cur;
      }
      rest.push(path.basename(cur));
      cur = parent;
    }
  }
}

function realWithin(fs, path, root, target) {
  const rk = realKeyOf(fs, path, root);
  const tk = realKeyOf(fs, path, target);
  const rel = path.relative(rk, tk);
  if (rel === '') return true;
  if (path.isAbsolute(rel)) return false;
  return !(rel === '..' || rel.startsWith(`..${path.sep}`) || rel.startsWith('../'));
}

/**
 * 白名单产物路径：规范化（解一次 percent-encoding，拒绝 .. / 绝对路径）+ realpath 越界断言。
 *
 * 修复 D2：旧实现只做 `path.resolve` + 字符串前缀比较，`self/assets` 若是 junction/
 * symlink 指向工作目录之外，词法上「在 self 内」而实际写到 self 外。realpath 归一后
 * junction 会被解开，越界随即暴露。
 *
 * 返回 '' 表示非法（调用方显式报错），绝不返回一个「看似合法」的越界路径。
 */
export function safeJoinSelf(selfDir, rel, env) {
  const { fs, path } = mkEnv(env);
  if (typeof rel !== 'string' || !rel.trim()) return '';
  let norm;
  try {
    norm = normalizeRel(rel, { label: '产物路径' });
  } catch (_) {
    return '';
  }
  const normSelf = path.resolve(selfDir);
  const abs = path.join(normSelf, ...norm.split('/'));
  if (!realWithin(fs, path, normSelf, abs)) return '';
  return abs;
}

/** 白名单产物：非空、相对、落在 self 内、无重复；artifact_scopes（若给）与白名单一致。 */
export function validateArtifacts(request, selfDir, env) {
  const raw = Array.isArray(request && request.artifacts) ? request.artifacts : [];
  const artifacts = [];
  for (const a of raw) {
    if (typeof a !== 'string' || !a.trim()) continue;
    if (!safeJoinSelf(selfDir, a, env)) {
      throw new DispatchPromptError('ARTIFACT_PATH_INVALID',
        `产物路径非法（绝对路径/含 .. /越出当前 self）：${a}`, { artifact: a });
    }
    const key = a.replace(/\\/g, '/');
    if (artifacts.some((x) => x.replace(/\\/g, '/') === key)) {
      throw new DispatchPromptError('ARTIFACT_PATH_INVALID', `产物路径重复：${a}`, { artifact: a });
    }
    artifacts.push(a);
  }
  if (artifacts.length === 0) {
    throw new DispatchPromptError('ARTIFACT_PATH_INVALID', '派发单没有任何可写白名单产物', {});
  }
  const scopes = normalizeScopes(request);
  const map = request && request.artifact_scopes;
  if (map && typeof map === 'object' && !Array.isArray(map)) {
    const keys = Object.keys(map).map((k) => k.replace(/\\/g, '/')).sort();
    const want = artifacts.map((a) => a.replace(/\\/g, '/')).sort();
    if (JSON.stringify(keys) !== JSON.stringify(want)) {
      throw new DispatchPromptError('ARTIFACT_SCOPE_MISMATCH',
        `artifact_scopes 的键与 artifacts 白名单不一致：${keys.join(',')} ≠ ${want.join(',')}`,
        { keys, want });
    }
    for (const [k, v] of Object.entries(map)) {
      if (scopes.length && !scopes.includes(String(v))) {
        throw new DispatchPromptError('ARTIFACT_SCOPE_MISMATCH',
          `artifact_scopes[${k}]=${v} 不在 scopes ${scopes.join(',')} 内`, { artifact: k });
      }
    }
  }
  return artifacts;
}

// ---------------------------------------------------------------------------
// 4. 来源收集（文件 / 目录 / 数组；不 readFileSync 目录）
// ---------------------------------------------------------------------------

const TEXT_EXTS = new Set(['.tex', '.md', '.markdown', '.txt']);

function statSafe(fs, p) {
  try { return fs.statSync(p); } catch (_) { return null; }
}

function toList(v) {
  if (Array.isArray(v)) return v.filter((x) => typeof x === 'string' && x.trim());
  if (typeof v === 'string' && v.trim()) return [v];
  return [];
}

function walkTextFiles(fs, path, root, limit, out, depth = 0) {
  if (depth > 6 || out.length >= limit) return;
  let names = [];
  try { names = fs.readdirSync(root).slice().sort(); } catch (_) { return; }
  for (const name of names) {
    if (out.length >= limit) return;
    if (name.startsWith('.') || name === 'node_modules') continue;
    if (depth === 0 && name === 'self') continue; // 制成文件目录不是原料
    const abs = path.join(root, name);
    const st = statSafe(fs, abs);
    if (!st) continue;
    if (st.isDirectory()) walkTextFiles(fs, path, abs, limit, out, depth + 1);
    else if (st.isFile() && TEXT_EXTS.has(path.extname(name).toLowerCase())) out.push(abs);
  }
}

/**
 * 返回 {sources:[{rel,abs,chars,sha256,text}], missing:[...], inventory:[brief...]}。
 * inputPath：文件 / 目录 / 路径数组；inventory：source_inventory 数组（带 source_file）。
 * 声明了但读不到的来源进入 missing，调用方据此明确报错，绝不静默跳过。
 */
export function collectSources({ inputPath, inventory, baseDir, limit }, envIn) {
  const { fs, path, crypto } = mkEnv(envIn);
  const maxFiles = limit || DEFAULTS.maxSourceFiles;
  const base = baseDir ? path.resolve(baseDir) : process.cwd();
  const inv = Array.isArray(inventory) ? inventory.filter((u) => u && typeof u === 'object') : [];
  const invFiles = [];
  for (const u of inv) {
    const f = typeof u.source_file === 'string' ? u.source_file.trim() : '';
    if (f && !invFiles.includes(f)) invFiles.push(f);
  }
  const inputs = toList(inputPath);
  const targets = []; // {abs, rel, declared}
  const missing = [];
  const pushTarget = (abs, rel) => {
    if (!targets.some((t) => t.abs === abs)) targets.push({ abs, rel });
  };
  const relTo = (root, abs) => path.relative(root, abs).replace(/\\/g, '/') || path.basename(abs);

  const roots = inputs.length ? inputs : [];
  for (const p of roots) {
    const abs = path.isAbsolute(p) ? path.resolve(p) : path.resolve(base, p);
    const st = statSafe(fs, abs);
    if (!st) { missing.push(p); continue; }
    if (st.isFile()) { pushTarget(abs, path.basename(abs)); continue; }
    if (st.isDirectory()) {
      if (invFiles.length) {
        for (const f of invFiles) {
          const fa = path.isAbsolute(f) ? path.resolve(f) : path.resolve(abs, f);
          const fst = statSafe(fs, fa);
          if (fst && fst.isFile()) pushTarget(fa, relTo(abs, fa));
          else missing.push(f);
        }
      } else {
        const found = [];
        walkTextFiles(fs, path, abs, maxFiles, found);
        for (const fa of found) pushTarget(fa, relTo(abs, fa));
      }
    }
  }
  if (!roots.length && invFiles.length) {
    // 没有显式输入路径：inventory 的 source_file 相对 baseDir（项目根）解析
    for (const f of invFiles) {
      const fa = path.isAbsolute(f) ? path.resolve(f) : path.resolve(base, f);
      const fst = statSafe(fs, fa);
      if (fst && fst.isFile()) pushTarget(fa, f.replace(/\\/g, '/'));
      else missing.push(f);
    }
  }
  const sources = [];
  for (const t of targets) {
    let text;
    try { text = fs.readFileSync(t.abs, 'utf8'); } catch (_) { missing.push(t.rel); continue; }
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    sources.push({
      rel: t.rel, abs: t.abs, chars: text.length, sha256: sha256Text(crypto, text), text,
    });
  }
  return { sources, missing, inventory: inv };
}

function fenceFor(text) {
  let longest = 2;
  for (const m of String(text).matchAll(/`+/g)) longest = Math.max(longest, m[0].length);
  return '`'.repeat(longest + 1);
}

export const SOURCE_BEGIN = '<<<QCHAIN_SOURCE_BEGIN>>>';
export const SOURCE_END = '<<<QCHAIN_SOURCE_END>>>';

export function renderInventoryTable(inventory, digest) {
  if (!Array.isArray(inventory) || inventory.length === 0) return '';
  const rows = inventory.map((u) => `| ${u.source_id || ''} | ${u.source_file || ''} | ${u.locator || ''} | ${u.kind || ''} | ${u.disposition || ''} |`);
  return [
    `### 来源清单 source_inventory（${inventory.length} 单元${digest ? `，digest=${digest}` : ''}）`,
    '| source_id | source_file | locator | kind | disposition |',
    '| --- | --- | --- | --- | --- |',
    ...rows,
  ].join('\n');
}

/** 唯一 SOURCE 块：每个来源文件完整出现一次，带路径、字符数与 sha256。 */
export function renderSourceBlock(sources, { inventory, inventoryDigest } = {}) {
  const hasSources = sources && sources.length > 0;
  const table = renderInventoryTable(inventory, inventoryDigest);
  if (!hasSources && !table) return '';
  const total = (sources || []).reduce((n, s) => n + s.chars, 0);
  const out = [`## 来源材料（唯一来源块：${(sources || []).length} 个文件，共 ${total} 字符，完整未截断）`];
  if (table) out.push(table);
  if (hasSources) {
    out.push(SOURCE_BEGIN);
    sources.forEach((s, i) => {
      const fence = fenceFor(s.text);
      const lang = s.rel.toLowerCase().endsWith('.tex') ? 'latex' : 'markdown';
      out.push(`### 来源文件 ${i + 1}/${sources.length}：${s.rel}（${s.chars} 字符，sha256=${s.sha256}）`);
      out.push(`${fence}${lang}\n${s.text}\n${fence}`);
    });
    out.push(SOURCE_END);
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// 5. 映射表：全文优先，超限按 scope 过滤（显式标注，不固定 slice）
// ---------------------------------------------------------------------------

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function selectMapRows(text, keys, { limit = DEFAULTS.maxMapChars, label = 'map' } = {}) {
  const full = String(text || '');
  const lines = full.split(/\r?\n/);
  if (full.length <= limit) {
    return { text: full, filtered: false, omitted_lines: 0, total_lines: lines.length };
  }
  const ks = (keys || []).map(String).filter(Boolean);
  if (ks.length === 0) {
    throw new DispatchPromptError('MAP_TOO_LARGE',
      `${label} 共 ${full.length} 字符超过上限 ${limit}，且没有可用于过滤的 scope`, { label, chars: full.length, limit });
  }
  const res = ks.map((k) => new RegExp(`(?<![A-Za-z0-9_])${escapeRe(k)}(?![A-Za-z0-9_])`, 'i'));
  const hit = (ln) => res.some((re) => re.test(ln));
  const out = [];
  let omittedRun = 0;
  let omittedTotal = 0;
  let tableRow = 0;
  const flush = () => {
    if (omittedRun > 0) {
      out.push(`（…已按本批 scope [${ks.join(', ')}] 过滤 ${label}：此处省略 ${omittedRun} 行不相关记录…）`);
      omittedRun = 0;
    }
  };
  for (const ln of lines) {
    const isTable = ln.trimStart().startsWith('|');
    tableRow = isTable ? tableRow + 1 : 0;
    const structural = /^\s*#/.test(ln) || ln.trim() === '' || (isTable && tableRow <= 2);
    if (structural || hit(ln)) { flush(); out.push(ln); } else { omittedRun += 1; omittedTotal += 1; }
  }
  flush();
  const result = out.join('\n');
  if (result.length > limit) {
    throw new DispatchPromptError('MAP_TOO_LARGE',
      `${label} 按 scope 过滤后仍有 ${result.length} 字符，超过上限 ${limit}；需拆分请求`,
      { label, chars: result.length, limit });
  }
  return { text: result, filtered: true, omitted_lines: omittedTotal, total_lines: lines.length };
}

function extractSection(text, heading) {
  const idx = text.indexOf(heading);
  if (idx === -1) return '';
  const rest = text.slice(idx);
  const next = rest.slice(heading.length).search(/\n## /);
  return next === -1 ? rest : rest.slice(0, heading.length + next);
}

// ---------------------------------------------------------------------------
// 6. 模板渲染（来源占位符整体最多出现一次）
// ---------------------------------------------------------------------------

const PLACEHOLDER_RE = /\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g;
export const SOURCE_PLACEHOLDERS = ['SOURCE_EXCERPTS', 'RAW_INPUT', 'MATERIAL_CONTENT'];

export function renderTemplate(tpl, values, { sourceBlock = '' } = {}) {
  let placed = false;
  const unresolved = [];
  const text = String(tpl || '').replace(PLACEHOLDER_RE, (m, key) => {
    if (SOURCE_PLACEHOLDERS.includes(key)) {
      if (!sourceBlock) return '';
      if (!placed) { placed = true; return `\n${sourceBlock}\n`; }
      return '（来源见本提示词中的唯一来源块）';
    }
    const v = values[key];
    if (v == null || v === '') { if (!unresolved.includes(key)) unresolved.push(key); return ''; }
    return String(v);
  });
  return { text, sourceTemplatePlaced: placed, unresolved };
}

// ---------------------------------------------------------------------------
// 7. 角色化 prompt
// ---------------------------------------------------------------------------

function readIfFile(fs, p) {
  const st = statSafe(fs, p);
  if (!st || !st.isFile()) return '';
  try { return fs.readFileSync(p, 'utf8'); } catch (_) { return ''; }
}

/**
 * 组装一份发给子 Agent 的完整 prompt。
 * env: {selfDir, engineRoot, fs?, path?, crypto?, maxPromptChars?, maxMapChars?}
 * 返回 {prompt, meta}；任何会导致必需上下文被截断/丢失的情形抛 DispatchPromptError。
 */
export function buildDispatchPrompt(request, envIn) {
  const e = mkEnv(envIn);
  const { fs, path, crypto } = e;
  const selfDir = envIn.selfDir;
  const engineRoot = envIn.engineRoot;
  const req = request || {};
  const role = classifyRole(req);
  const scopes = normalizeScopes(req);
  const artifacts = validateArtifacts(req, selfDir, e);
  const ctxObj = (req.context && typeof req.context === 'object') ? req.context : {};
  const spec = (ctxObj.user_spec && typeof ctxObj.user_spec === 'object') ? ctxObj.user_spec : {};
  const maxPromptChars = Number(ctxObj.max_prompt_chars || req.max_prompt_chars
    || envIn.maxPromptChars) || DEFAULTS.maxPromptChars;
  const maxMapChars = Number(envIn.maxMapChars) || DEFAULTS.maxMapChars;
  // P2-1：提示词分层档位（per-request 覆盖 > config.yaml > 默认 layered）
  const rawProfile = String(
    ctxObj.prompt_profile || req.prompt_profile || envIn.promptProfile
    || DEFAULTS.promptProfile).toLowerCase();
  const promptProfile = PROMPT_PROFILES.includes(rawProfile) ? rawProfile : DEFAULTS.promptProfile;
  const layered = promptProfile === 'layered';
  const mapCharLimit = layered ? Math.min(maxMapChars, DEFAULTS.layeredMapChars) : maxMapChars;
  // 附录层：只登记路径 + 尺寸，供子 Agent 按需只读（而不是把正文塞进提示词）
  const appendix = [];
  const noteAppendix = (rel, chars, why) => {
    appendix.push(`- ${rel}（${chars} 字符）${why ? `：${why}` : ''}`);
  };

  const promptFile = String(req.prompt_file || 'subagent_prompt.md').replace(/^prompts[\\/]/, '');
  const tpl = readIfFile(fs, path.join(engineRoot, 'prompts', promptFile));
  if (!tpl) {
    throw new DispatchPromptError('TEMPLATE_MISSING', `提示词模板不可读：prompts/${promptFile}`, { promptFile });
  }

  // ---- 占位符取值（沿用既有规则：context/user_spec 标量 + 派发元数据） ----
  const values = {};
  const assign = (key, val) => {
    if (key && val != null && val !== '' && values[key] == null) values[key] = val;
  };
  for (const [k, v] of Object.entries(ctxObj)) {
    if (v == null || typeof v === 'object') continue;
    assign(k, v);
    assign(k.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toUpperCase(), v);
  }
  for (const [k, v] of Object.entries(spec)) {
    if (v == null || typeof v === 'object') continue;
    assign(k, v);
    assign(k.toUpperCase(), v);
  }
  assign('SECTION_TITLE', ctxObj.section_title || scopes[0] || '');
  assign('OUTPUT_FILE', artifacts.join('、'));
  assign('OUTPUT_FILES', artifacts.join(', '));
  assign('SCOPE', req.scope || scopes[0] || '');
  assign('SCOPES', scopes.join(', '));
  assign('STAGE', req.stage || '');
  assign('ROLE', req.role || '');
  assign('BATCH_ID', req.batch_id || '');
  assign('DISPATCH_ID', req.dispatch_id || '');
  const baseline = JSON.stringify({
    content_digest: ctxObj.content_digest || '',
    student_pdf_sha256: ctxObj.student_pdf_sha256 || '',
    student_html_sha256: ctxObj.student_html_sha256 || '',
    // A24：HTML 内容身份（卡片 ID + 可见文本，渲染层无关）。字节哈希每次
    // 构建都因 render_html.py:356 的 GENERATED_AT 而变，验收记录须同时登记
    // 它，否则每次重渲染都被判整体过期。
    student_html_content_digest: ctxObj.student_html_content_digest || '',
  }, null, 2);
  assign('EVIDENCE', baseline);
  assign('VERSION_HASHES', baseline);
  assign('OBJECTIVES', Array.isArray(ctxObj.expected_qids) ? ctxObj.expected_qids.join(', ') : '');
  assign('APPROVED_MATERIAL_PACK', ctxObj.approved_material_pack || '');

  // ---- 来源：只收一次、完整、带路径与摘要 ----
  const isRepair = String(req.stage || '') === 'REPAIR' || Boolean(ctxObj.repair_reason);
  // 来源解析优先级：显式 context > user_spec 显式键 > user_spec.source_files
  // （Phase 0 冻结的用户声明原料，单文件场景即 1.3.tex）> workspace.json 回退
  // （其 input_path 可能是整个基准目录，会把 hw1.3/ 等无关原料一并扫入，
  // 导致 prompt 超限——见 T1_DEPENDENCY PROMPT_TOO_LARGE 故障）。
  let inputPath = ctxObj.input_file || ctxObj.input_path || spec.input_file || spec.input_path || spec.source_files || '';
  if (!inputPath) {
    const wsText = readIfFile(fs, path.join(selfDir, '.pd', 'workspace.json'));
    if (wsText) {
      try { const ws = JSON.parse(wsText); if (ws.input_path) inputPath = ws.input_path; } catch (_) { /* 忽略损坏 */ }
    }
  }
  const inputList = toList(inputPath);
  assign('INPUT_FILE', inputList.join(', '));
  const inventory = Array.isArray(ctxObj.source_inventory) ? ctxObj.source_inventory : [];
  let collected = { sources: [], missing: [], inventory };
  if (!isRepair) {
    collected = collectSources({
      inputPath, inventory, baseDir: path.dirname(path.resolve(selfDir)),
    }, e);
    if (collected.missing.length) {
      throw new DispatchPromptError('SOURCE_UNREADABLE',
        `声明的来源材料不可读：${collected.missing.join(', ')}`, { missing: collected.missing });
    }
  }
  const sourceBlock = isRepair ? '' : renderSourceBlock(collected.sources, {
    inventory, inventoryDigest: ctxObj.source_inventory_digest,
  });

  const rendered = renderTemplate(tpl, values, { sourceBlock });

  // ---- 映射表（相关行） ----
  const mapKeys = role === 'planning' ? [] : [...scopes, String(ctxObj.section_id || '')].filter(Boolean);
  const mapMeta = {};
  const mapBlocks = [];
  if (!isRepair) {
    for (const [file, title] of [
      ['source-map.md', '章节映射骨架 source-map.md'],
      ['dependency-map.md', '依赖拓扑 dependency-map.md'],
      ['coverage-map.md', '覆盖与题链规划 coverage-map.md'],
    ]) {
      const t = readIfFile(fs, path.join(selfDir, file));
      if (!t) continue;
      const sel = selectMapRows(t, mapKeys, { limit: mapCharLimit, label: file });
      mapMeta[file] = { chars: sel.text.length, full_chars: t.length,
                        filtered: sel.filtered, omitted_lines: sel.omitted_lines };
      const why = sel.omitted_lines
        ? `已按本批 scope 过滤，其余 ${sel.omitted_lines} 行未内嵌`
        : (sel.text.length < t.length ? '已截断' : '');
      mapBlocks.push(`\n## ${title}${sel.filtered ? '（已按本批 scope 过滤，关键行完整保留）' : '（全文）'}\n\`\`\`markdown\n${sel.text}\n\`\`\``);
      if (layered) noteAppendix(file, t.length, why || '全文已内嵌，如需其它 scope 的行可只读该文件');
    }
  }

  // ---- 作者：TeX 接口速查（按标题截到下一个 ## 为止，不再固定 2500 字符） ----
  let texGuide = '';
  if (role === 'author') {
    const full = readIfFile(fs, path.join(engineRoot, 'references', 'tex-interface.md'));
    if (full) texGuide = extractSection(full, '## 三、 写作者速查');
  }

  // ---- 返修：现有产物全文 + 门禁命中问题 ----
  let repairBlock = '';
  if (isRepair) {
    const reason = ctxObj.repair_reason || req.instruction || '';
    const issueList = [];
    const stems = artifacts.map((a) => a.replace(/\.tex$/i, ''));
    const rptDir = path.join(selfDir, '.pd', 'reports');
    const rptSt = statSafe(fs, rptDir);
    if (rptSt && rptSt.isDirectory()) {
      for (const f of fs.readdirSync(rptDir).slice().sort()) {
        if (!f.endsWith('.json')) continue;
        try {
          const obj = JSON.parse(fs.readFileSync(path.join(rptDir, f), 'utf8'));
          // 兼容两种报告形态：结构化 issues（check_dialogue 等）与原始
          // errors 明细（check_fidelity 等只有 code/detail 的原始报告）。
          const rawList = Array.isArray(obj.issues) ? obj.issues
            : (Array.isArray(obj.errors) ? obj.errors : null);
          if (rawList) {
            for (const iss of rawList) {
              const msg = typeof iss === 'string' ? iss
                : (iss.message || iss.detail || JSON.stringify(iss));
              if (msg && stems.some((s) => msg.includes(s)))
                issueList.push(`[${f.replace('.json', '')}] ${msg}`);
            }
          }
        } catch (_) { /* 单个损坏报告不阻断 */ }
      }
    }
    const semDir = path.join(selfDir, '.pd', 'semantic');
    const semSt = statSafe(fs, semDir);
    if (semSt && semSt.isDirectory()) {
      for (const f of fs.readdirSync(semDir).slice().sort()) {
        if (!f.endsWith('.json')) continue;
        if (!stems.some((s) => f === `${s}.json`)) continue;
        try {
          const obj = JSON.parse(fs.readFileSync(path.join(semDir, f), 'utf8'));
          if (!obj || (!obj.conclusion && !Array.isArray(obj.issues))) continue;
          // N2：advisory 不再"一律下一轮升级成 blocking"。人工登记延期的
          // 条目本轮不必修（延期是人的决定，见 .pd/deferrals.json）。
          issueList.push(`[semantic/${f} conclusion=${obj.conclusion || 'n/a'}]（下列 blocking 必须本轮全部修复；advisory 视情况修复；标注 [deferred] 的已由人工批准延期，本轮不要动它，也不要改写成别的问题）`);
          for (const iss of (obj.issues || [])) {
            const sev = iss.severity || '?';
            const code = iss.code || '?';
            const ln = iss.line ? ` L${iss.line}` : '';
            const ev = String(iss.evidence || iss.message || '').trim();
            const df = iss.deferred ? ` [deferred${(iss.deferral && iss.deferral.reason) ? `: ${iss.deferral.reason}` : ''}]` : '';
            issueList.push(`[semantic/${f} ${sev} ${code}${ln}]${df} ${ev}`);
          }
        } catch (_) { /* 损坏的审读 JSON 不阻断 */ }
      }
    }
    // T4 教学验收记录（.pd/acceptance/<stem>.json）：验收专员判 REVISE 时，
    // 返修子 Agent 必须看到每条 open issue 的 code/qid/part/evidence 与建议处置，
    // 否则只有一句「教学验收未通过: REVISE」无法定位真实缺陷。
    const accDir = path.join(selfDir, '.pd', 'acceptance');
    const accSt = statSafe(fs, accDir);
    if (accSt && accSt.isDirectory()) {
      for (const f of fs.readdirSync(accDir).slice().sort()) {
        if (!f.endsWith('.json')) continue;
        if (!stems.some((s) => f === `${s}.json`)) continue;
        try {
          const obj = JSON.parse(fs.readFileSync(path.join(accDir, f), 'utf8'));
          if (!obj || (!obj.conclusion && !Array.isArray(obj.issues))) continue;
          const open = (obj.issues || []).filter(
            (i) => i && String(i.status || '').toLowerCase() !== 'fixed');
          issueList.push(`[acceptance/${f} conclusion=${obj.conclusion || 'n/a'}]`
            + `（下列教学验收 open 问题必须一次性全部修复，已 fixed 的跳过）`);
          for (const iss of open) {
            const code = iss.code || iss.id || '?';
            const q = iss.qid ? ` qid=${iss.qid}` : '';
            const part = iss.part ? ` @ ${iss.part}` : '';
            const ev = String(iss.evidence || iss.message || '').trim();
            const res = iss.resolution
              ? `；建议处置: ${String(iss.resolution).trim()}` : '';
            issueList.push(`[acceptance/${f} ${code}${q}${part}] ${ev}${res}`);
          }
        } catch (_) { /* 损坏的验收 JSON 不阻断 */ }
      }
    }
    const existing = artifacts.map((a) => {
      const abs = safeJoinSelf(selfDir, a, e);
      const t = abs ? readIfFile(fs, abs) : '';
      return t ? `## 当前待修复文件内容（${a}）\n\`\`\`latex\n${t}\n\`\`\`` : '';
    }).filter(Boolean).join('\n\n');
    // 图资产提示：self/assets/ 下已有预栅格化图（tikz_rasterize.py 产物）时
    // 列出文件名，图迁移类返修可直接 \includegraphics 引用——手写 tikz
    // 在 HTML 卡片构建（tex2cards）中不被支持，必须走位图。
    const assetsDir = path.join(selfDir, 'assets');
    let assetHint = '';
    try {
      const ast = statSafe(fs, assetsDir);
      if (ast && ast.isDirectory()) {
        const pngs = fs.readdirSync(assetsDir)
          .filter((f) => /\.(png|jpg|jpeg)$/i.test(f)).sort();
        if (pngs.length > 0) {
          const list = pngs.map((p) => `assets/${p}`).join('、');
          assetHint = `- 工作区已有预栅格化图资产（按 assets/README.md 的映射`
            + ` 对应源文档图，直接 \\includegraphics 引用，禁止手写 tikz）：${list}`;
        }
      }
    } catch (_) { /* 无 assets 目录不影响 */ }
    repairBlock = [
      '## 【返修专项指令与门禁报错详情】',
      `- 返修原因: ${reason}`,
      issueList.length > 0 ? `- 门禁具体命中问题:\n  ${issueList.join('\n  ')}` : '',
      assetHint,
      '- 语法避坑指南（必读，违反即编译失败）:\n'
      + '  * \\fillin[参考答案]{预留宽度}[选项1/选项2/选项3] 必须严格有3组定界符；若参考答案或选项内部包含中括号（如闭区间记号 [a, b]），必须用花括号包裹以防 LaTeX 宏提早闭合：如 \\fillin[{[a, b]}]{25mm}[{[a, b]}/{[c, d]}/{[e, f]}] 或改写为不等式形式。\n'
      + '  * \\fillin 的参考答案与选项内部【禁止使用 $ 定界符】：宏已自动置于数学模式（\\pdCJKTextWrap 会把连续汉字段包进 \\text{}），直接裸写数学即可。错误示例: \\fillin[$f(\\xi_{i})\\Delta x_{i}$]{2.4cm}[$\\xi_{i}\\Delta x_{i}$/$f(\\xi_{i})\\Delta x_{i}$]；正确写法: \\fillin[f(\\xi_{i})\\Delta x_{i}]{2.4cm}[\\xi_{i}\\Delta x_{i}/f(\\xi_{i})\\Delta x_{i}/f(x_{i})\\Delta x_{i}]。若现有文件里 \\fillin 参数含 $，必须全部去掉（仅去掉定界符 $，保留内部数学内容）。\n'
      + '  * 空洞的答案是汉字时直接写汉字（无需 \\text）：如 \\fillin[成立]{20mm}[成立/不成立/无法判定]。',
      existing ? `${existing}\n请直接在上述现有文件基础上进行精准修正，保持其余题目、qid 注释、宏和结构完好，返回修改后的完整文件内容。` : '',
    ].filter(Boolean).join('\n\n');
  }

  // ---- 审读：被审产物（候选切片）全文，必需，缺失即报错 ----
  let reviewTargets = '';
  const reviewMeta = [];
  if (role === 'review') {
    const parts = [];
    for (const sc of scopes) {
      let text = '';
      if (scopes.length === 1 && typeof ctxObj.section_text === 'string') text = ctxObj.section_text;
      if (!text) {
        const abs = safeJoinSelf(selfDir, `${sc}.tex`, e);
        text = abs ? readIfFile(fs, abs) : '';
      }
      if (!text && String(req.stage || '').toUpperCase().startsWith('T2_CHECK')) {
        throw new DispatchPromptError('REVIEW_TARGET_MISSING',
          `语义审读缺少被审候选切片 ${sc}.tex 全文`, { scope: sc });
      }
      if (text) {
        const fence = fenceFor(text);
        parts.push(`### 被审产物 ${sc}.tex（${text.length} 字符，sha256=${sha256Text(crypto, text)}）\n${fence}latex\n${text}\n${fence}`);
        reviewMeta.push({ scope: sc, chars: text.length });
      }
    }
    if (parts.length) reviewTargets = `\n## 被审产物（全文，覆盖本派发单全部 scope：${scopes.join(', ')}）\n${parts.join('\n\n')}`;
  }

  // ---- 审读包与预算证据（验收 DoD 的可核对输入，全文内嵌） ----
  // 宿主子 Agent 为一次性单轮作答（其工具调用不会被回灌，实测首轮即被宿主当作
  // 终态截断），而 acceptance_prompt.md 要求核对 .pd/review/ 审读包、冒烟证据与
  // .pd/reports/estimate.json / check_budget.json 结构化预算报告；不内嵌时验收员
  // 只能判 missing-evidence（NEEDS_EVIDENCE），并把「payload 未含 X」写成 open issue。
  let reviewPack = '';
  const packMeta = [];
  if (role === 'review') {
    const packFiles = [
      ['.pd', 'reports', 'estimate.json'],
      ['.pd', 'reports', 'check_budget.json'],
      ['.pd', 'review', 'manifest.json'],
      ['.pd', 'review', 'smoke', 'smoke-manifest.json'],
      ['.pd', 'review', 'smoke', 'human-review.json'],
      ['.pd', 'review', 'smoke', 'machine-check.json'],
    ];
    const packParts = [];
    for (const segs of packFiles) {
      const t = readIfFile(fs, path.join(selfDir, ...segs));
      if (!t) continue;
      packParts.push(`### ${segs.join('/')}（${t.length} 字符）\n\`\`\`json\n${t}\n\`\`\``);
      packMeta.push({ path: segs.join('/'), chars: t.length });
    }
    const pagesTxt = readIfFile(fs, path.join(selfDir, '.pd', 'review', 'student-pages.txt'));
    if (pagesTxt) {
      const cap = layered ? DEFAULTS.layeredPackChars : 24000;
      const truncated = pagesTxt.length > cap;
      const t = truncated ? `${pagesTxt.slice(0, cap)}\n…（截断：共 ${pagesTxt.length} 字符，仅前 ${cap} 字符；渲染冒烟 DoD 只需首页与首个含 \\fillin 选项组的页面，均在开头；其余部分可按路径只读）` : pagesTxt;
      packParts.push(`### .pd/review/student-pages.txt（学生版 PDF 文本层抽取，${pagesTxt.length} 字符${truncated ? `，前 ${cap} 字符` : '，全文'}）\n\`\`\`\n${t}\n\`\`\``);
      packMeta.push({ path: '.pd/review/student-pages.txt', chars: pagesTxt.length });
      if (layered && truncated) noteAppendix('.pd/review/student-pages.txt', pagesTxt.length,
        `已内嵌前 ${cap} 字符，需要后文可按路径只读`);
    }
    if (packParts.length) {
      reviewPack = `\n## 审读包与预算证据（验收 DoD 可核对输入，已全文内嵌，禁止再读文件）\n`
        + `下列文件即为验收契约提到的 .pd/review/ 审读包、冒烟证据与预算报告的全部可核对内容；`
        + `渲染冒烟 DoD（含无视觉模型环境条款）与预算核对（estimate.json/check_budget.json）`
        + `均以此为唯一依据，不得以「载荷未含」判 missing-evidence。\n${packParts.join('\n\n')}`;
    }
  }

  // ---- coverage-map 表头 schema 硬约束 ----
  // T1_COVERAGE 派发单的 instruction 只要求登记 source_ids/action/new_objects/
  // reason，未给出 check_budget → gen_classroom_plan.parse_coverage_map 能解析的
  // 表头；子 Agent 自造格式（首个命中表是节级总览表，列名无 原文节点/node）导致
  // rows=[] 被 REJECT（"coverage-map.md 表格为空或格式未包含有效节点"）。
  // 这里对产出 coverage-map.md 的派发统一追加规范表头与排列要求。
  let schemaBlock = '';
  if (artifacts.some((a) => /coverage-map\.md$/i.test(String(a)))) {
    schemaBlock = [
      '## coverage-map.md 表头 schema 硬约束（门禁解析器唯一可识别格式，违反即 REJECT）',
      '- 文件中【第一个】含 `planned_minutes`（或 `delivery_role`/`est_minutes`/`原文节点` 任一）的 Markdown 表格必须是主覆盖表，且必须逐列使用下列表头（列名与顺序完全一致）：',
      '  `原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes | 推荐讲法 | 易错点 | source_ids | action | new_objects | reason | tier`',
      '- 不得在该主表之前放置任何含 `planned_minutes`/`delivery_role`/`est_minutes` 的总览表或统计表（解析器只认第一个命中表头的表；若该表列名不含 `原文节点`/`node`，全表解析失败）。节级总览/统计表只能放在主表之后。',
      '- `原文节点` 列每行必须非空，直接抄 source-map 的 SRC 编号或原文 locator（如 `SRC-070`）。',
      '- `学案位置` 列填节名（sec0/sec1/…，与依赖表一致）；`对应问题` 列写该节点在学案中的落点题号或材料名。',
      '- `carrier_qid` 列每行必填：独立题填本行唯一 qid（如 `sec2-q1`）；多个原文节点共用一道载体题时，各行填同一个主载体 qid。',
      '- `planned_minutes` 列填整数分钟（自学模式长链定理/引理推导题须 >= 12）。',
      '- `delivery_role` 只能取 `core`/`support`/`self-study`/`reference`；`tier` 只能取 `core`/`optional`/`extension`；`action` 只能取 `PRESERVE`/`EXPLAIN`/`BRIDGE`/`EXTEND`；`new_objects` 非空时只登记 BRIDGE/EXTEND 请求。',
      '- `推荐讲法` 与 `易错点` 两列对每个 core/support 行必须实质填写，严禁留空或写“待定/TBD/略”等占位符。',
      '- core 任务的唯一 (学案位置, carrier_qid) 总数不得超过 core_task_limit（默认 12）道。',
    ].join('\n');
  }

  // ---- 任务书 ----
  const artScopes = (req.artifact_scopes && typeof req.artifact_scopes === 'object') ? req.artifact_scopes : {};
  const taskBlock = [
    '',
    '## 派发任务书（机器可读）',
    `- dispatch_id: ${req.dispatch_id || ''}`,
    `- batch_id: ${req.batch_id || ''}`,
    `- stage: ${req.stage || ''}`,
    `- scope: ${req.scope || ''}`,
    scopes.length > 1 ? `- scopes（本派发单覆盖的全部节，须逐节完成）: ${scopes.join(', ')}` : '',
    `- role: ${req.role || ''}（上下文角色: ${role}）`,
    '## 项目路径',
    selfDir,
    '## 执行指令',
    String(req.instruction || ''),
    req.command ? `## 提交命令\n${req.command}` : '',
    `## 只允许写这些产物（白名单共 ${artifacts.length} 个，越界写入将被拒绝）`,
    ...artifacts.map((a) => `- ${a}${artScopes[a] ? `（scope: ${artScopes[a]}）` : ''}`),
  ].filter((s) => s !== '').join('\n');

  // ---- 附录索引（P2-1 分层：把大载荷挪出必读层，只留路径 + 尺寸） ----
  let appendixBlock = '';
  if (layered && appendix.length) {
    appendixBlock = [
      '\n## 附录索引（未全文内嵌的输入；如确需细节，用 read 工具按路径只读，不要通读全文）',
      ...appendix,
      '提示：本任务书已包含完成任务所需的全部规则与判断依据；附录只用于核对个别数字或行。'
        + '若你的可见载荷看起来被缩短了，那是投递通道的展示折叠，不是内容缺失——不要为了"补全载荷"去通读文件。',
    ].join('\n');
  }


  const tail = [
    repairBlock ? `\n${repairBlock}` : '',
    schemaBlock ? `\n${schemaBlock}` : '',
    ...mapBlocks,
    texGuide ? `\n## TeX 接口写作者速查\n${texGuide}` : '',
    // 模板没有来源占位符时，来源块仍只出现一次——放在尾部
    (!rendered.sourceTemplatePlaced && sourceBlock) ? `\n${sourceBlock}` : '',
    reviewPack,
    reviewTargets,
    appendixBlock,
    `\n${outputProtocol(role, artifacts)}`,
    `\n${EXECUTION_RULES}`,
  ].filter((s) => s !== '').join('\n');

  const prompt = rendered.text + taskBlock + '\n' + tail;
  if (prompt.length > maxPromptChars) {
    throw new DispatchPromptError('PROMPT_TOO_LARGE',
      `完整 prompt ${prompt.length} 字符超过上限 ${maxPromptChars}（模板 ${rendered.text.length}、来源 ${sourceBlock.length}、映射 ${Object.values(mapMeta).reduce((n, m) => n + m.chars, 0)}）；不会截断原文，请拆分请求`,
      {
        prompt_chars: prompt.length,
        max_prompt_chars: maxPromptChars,
        source_chars: sourceBlock.length,
        template_chars: rendered.text.length,
      });
  }
  const occurrences = (needle) => prompt.split(needle).length - 1;
  const sumChars = (arr, key = 'chars') => arr.reduce((n, x) => n + (Number(x[key]) || 0), 0);
  const mapChars = Object.values(mapMeta).reduce((n, m) => n + m.chars, 0);
  return {
    prompt,
    meta: {
      role,
      scopes,
      artifacts,
      prompt_chars: prompt.length,
      prompt_sha256: sha256Text(crypto, prompt),
      prompt_profile: promptProfile,
      // P2-1：分层字数账（必读层 = 模板 + 任务书 + 被审对象 + 来源 + 协议）
      layers: {
        template: rendered.text.length,
        task: taskBlock.length,
        source: sourceBlock.length,
        maps: mapChars,
        review_targets: sumChars(reviewMeta),
        review_pack: sumChars(packMeta),
        repair: repairBlock.length,
        appendix: appendixBlock.length,
        protocol: outputProtocol(role, artifacts).length + EXECUTION_RULES.length,
      },
      source_files: collected.sources.map((s) => ({ path: s.rel, chars: s.chars, sha256: s.sha256 })),
      source_chars: collected.sources.reduce((n, s) => n + s.chars, 0),
      source_block_count: occurrences(SOURCE_BEGIN),
      source_placement: !sourceBlock ? 'none' : (rendered.sourceTemplatePlaced ? 'template' : 'tail'),
      maps: mapMeta,
      review_targets: reviewMeta,
      review_pack: packMeta,
      unresolved_placeholders: rendered.unresolved,
      truncated: false,
    },
  };
}

// ---------------------------------------------------------------------------
// 8. 子 Agent 回复解析（无任何磁盘副作用）
// ---------------------------------------------------------------------------

function pickFiles(obj) {
  return obj && typeof obj === 'object' && obj.files && typeof obj.files === 'object' ? obj : null;
}

// 容错 JSON 解析：仅在严格解析失败时启用，修复三类子 Agent 常见 JSON 瑕疵
//   1) 字符串内出现非法转义（如 "\pause"）→ 转为转义的字面反斜杠 "\\pause"
//   2) 字符串内含未转义的控制字符（\n \r \t）或内部双引号 → 转义
//   3) 尾部缺少若干个 "}" → 最多补 3 个
// 严格解析成功时绝不改动原文。
const JSON_VALID_ESCAPES = new Set(['"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u']);

function repairJsonString(raw) {
  let out = '';
  let inStr = false;
  // 串内引号配对修复：当某个串内引号因「后跟非结构符」被判为串内原文而转义时，
  // 它的配对闭引号（紧随其后的下一个引号，如 ["sec1","sec2"] 中的后续引号）
  // 应一并转义，否则字符串会被它提前关闭、后续文本整体错位。
  let pendingClose = false;
  let i = 0;
  const n = raw.length;
  while (i < n) {
    const ch = raw[i];
    if (inStr) {
      if (ch === '\\') {
        const nx = i + 1 < n ? raw[i + 1] : '';
        if (JSON_VALID_ESCAPES.has(nx)) {
          out += '\\' + nx;
          i += 2;
          continue;
        }
        // 非法转义：把孤立的反斜杠转义为字面反斜杠
        out += '\\\\';
        i += 1;
        continue;
      }
      if (ch === '"') {
        if (pendingClose) {
          out += '\\"';
          pendingClose = false;
          i += 1;
          continue;
        }
        // 内部双引号启发式：后面的下一个非空白字符不是 JSON 结构符（:,}]）也不是换行，
        // 说明这个引号多半是串内原文（如 "5 of 9"），而非字符串结束。
        let j = i + 1;
        while (j < n && (raw[j] === ' ' || raw[j] === '\t')) j += 1;
        const nx = j < n ? raw[j] : '';
        if (nx && nx !== '\n' && !':,}]'.includes(nx)) {
          out += '\\"';
          pendingClose = true;
          i += 1;
          continue;
        }
        out += '"';
        inStr = false;
        i += 1;
        continue;
      }
      if (ch === '\n') { out += '\\n'; i += 1; continue; }
      if (ch === '\r') { out += '\\r'; i += 1; continue; }
      if (ch === '\t') { out += '\\t'; i += 1; continue; }
      out += ch;
      i += 1;
      continue;
    }
    pendingClose = false;
    if (ch === '"') { inStr = true; out += '"'; i += 1; continue; }
    out += ch;
    i += 1;
  }
  return out;
}

export function tolerantJsonParse(s) {
  if (!s) return null;
  try { return JSON.parse(s); } catch (_) { /* 严格解析失败才进入修复 */ }
  let cur = s;
  for (let round = 0; round < 32; round += 1) {
    const repaired = repairJsonString(cur);
    for (let extra = 0; extra <= 3; extra += 1) {
      try { return JSON.parse(repaired + '}'.repeat(extra)); } catch (e) {
        // 错误位置回保险杠：解析仍失败时，把错误位置之前最近的未转义引号
        // 转义（它多半是把字符串提前关闭的串内引号），再重试一轮。
        const m = /at position (\d+) \(line \d+ column \d+\)/.exec(String(e && e.message || ''));
        const pos = m ? Number(m[1]) : -1;
        if (!Number.isFinite(pos) || pos < 0 || pos > repaired.length) break;
        let k = -1;
        for (let p2 = pos - 1; p2 >= 0; p2 -= 1) {
          if (repaired[p2] !== '"') continue;
          let bs = 0;
          for (let b = p2 - 1; b >= 0 && repaired[b] === '\\'; b -= 1) bs += 1;
          if (bs % 2 === 0) { k = p2; break; }
        }
        if (k < 0) break;
        cur = repaired.slice(0, k) + '\\"' + repaired.slice(k + 1);
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// 8b. 输出截断判据（N5）
// ---------------------------------------------------------------------------
// 审计发现：子 Agent 输出被 max_tokens 截断时，只要全文没有 <<<END>>>，0b 宽松分支
// 就会把截断的正文当成完整产物接收（LaTeX 环境都不配平也照样落盘），要么污染切片，
// 要么等下游渲染/门禁才炸。这里给宽松分支加一道结构判据：不完整就不算产物。

/** 去掉 TeX 注释（保留 \% 与转义序列），避免注释里的花括号干扰配平判定。 */
function stripTexComments(text) {
  return String(text).split(/\r?\n/).map((line) => {
    let out = '';
    for (let i = 0; i < line.length; i += 1) {
      const ch = line[i];
      if (ch === '\\' && i + 1 < line.length) { out += ch + line[i + 1]; i += 1; continue; }
      if (ch === '%') break;
      out += ch;
    }
    return out;
  }).join('\n');
}

function braceDepth(text) {
  let depth = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '\\') { i += 1; continue; }
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth < 0) return { depth, negative: true };
    }
  }
  return { depth, negative: false };
}

function unbalancedEnvs(text) {
  const open = {};
  const close = {};
  for (const m of text.matchAll(/\\begin\s*\{([^}]+)\}/g)) open[m[1]] = (open[m[1]] || 0) + 1;
  for (const m of text.matchAll(/\\end\s*\{([^}]+)\}/g)) close[m[1]] = (close[m[1]] || 0) + 1;
  const bad = [];
  for (const k of new Set([...Object.keys(open), ...Object.keys(close)])) {
    if ((open[k] || 0) !== (close[k] || 0)) bad.push(`${k} ${open[k] || 0}/${close[k] || 0}`);
  }
  return bad;
}

/**
 * 判断一段被宽松分支切出来的正文是否"中途截断"。
 * 返回 '' 表示结构完整；返回非空字符串表示截断原因（用于证据台账）。
 * 判据只认结构性事实（环境/花括号/JSON 收尾/代码围栏），不做长度猜测。
 */
export function looksTruncated(name, body) {
  const n = String(name || '').toLowerCase();
  const text = String(body || '');
  if (!text.trim()) return '空内容';
  if (n.endsWith('.tex')) {
    const clean = stripTexComments(text);
    if (!/\\[A-Za-z]/.test(clean)) return '不含任何 LaTeX 命令（疑似正文被截去）';
    const b = braceDepth(clean);
    if (b.negative) return '花括号提前闭合';
    if (b.depth !== 0) return `花括号未配平（余 ${b.depth} 个未闭合的 { ）`;
    const envs = unbalancedEnvs(clean);
    if (envs.length) return `环境未配平（${envs.slice(0, 3).join(', ')}）`;
    return '';
  }
  if (n.endsWith('.json')) {
    const t = text.trim();
    if (!/^[[{]/.test(t)) return '不是 JSON 开头';
    if (!/[\]}]\s*$/.test(t)) return 'JSON 未收尾';
    return '';
  }
  if (n.endsWith('.md')) {
    const fences = (text.match(/^```/gm) || []).length;
    if (fences % 2 === 1) return '代码围栏未闭合';
    return '';
  }
  return '';
}

/** 宿主/适配器侧的截断信号（有则直接用，比结构推断更可靠）。 */
function truncationHint(raw) {
  if (!raw || typeof raw !== 'object') return false;
  if (raw.truncated === true) return true;
  for (const k of ['stop_reason', 'stopReason', 'finish_reason', 'finishReason']) {
    const v = String(raw[k] || '').toLowerCase();
    if (v === 'max_tokens' || v === 'length' || v === 'truncated') return true;
  }
  const inner = raw.result && typeof raw.result === 'object' ? raw.result : null;
  if (inner) {
    if (inner.truncated === true) return true;
    for (const k of ['stop_reason', 'stopReason', 'finish_reason', 'finishReason']) {
      const v = String(inner[k] || '').toLowerCase();
      if (v === 'max_tokens' || v === 'length' || v === 'truncated') return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// 8c. 平衡对象扫描（解析回退用，纯字符串、无副作用）
// ---------------------------------------------------------------------------
// 子 Agent 有时在合法 JSON 之后粘一个游离引号或半句解释
// （"{"files": {...}}" 后面跟一个多余的 '"'），strict parse 报
// "Unexpected non-whitespace character after JSON"，repairJsonString 修不了
// （它只修串内引号）。这里按括号深度（含字符串内豁免）找到首个完整闭合的
// 顶层对象，截取前缀再走 strict parse。
function balancedObjectEnd(s) {
  if (!s || s[0] !== '{') return -1;
  let depth = 0;
  let inStr = false;
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (inStr) {
      if (ch === '\\') { i += 1; continue; }
      if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') { inStr = true; continue; }
    if (ch === '{' || ch === '[') depth += 1;
    else if (ch === '}' || ch === ']') {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

export function parseSubagentFiles(raw, artifacts) {
  if (!raw) return { files: null, runId: '', error: '子 Agent 无返回' };
  let runId = '';
  let text = '';
  if (typeof raw === 'string') {
    text = raw;
  } else if (typeof raw === 'object') {
    runId = String(raw.runId || raw.id || raw.subagentId || '');
    const blocks = raw.output || (raw.result && raw.result.output) || raw.content;
    if (Array.isArray(blocks)) {
      // 优先只提取 type === 'text' 的正文块，过滤掉 reasoning / thinking 块
      const textBlocks = blocks.filter((b) => b && typeof b === 'object' && b.type === 'text');
      const targetBlocks = textBlocks.length > 0 ? textBlocks : blocks;
      text = targetBlocks
        .map((b) => {
          if (!b) return '';
          if (typeof b === 'string') return b;
          if (typeof b === 'object') return b.text || b.content || b.value || '';
          return String(b);
        })
        .join('\n');
    } else if (typeof blocks === 'string') {
      text = blocks;
    } else if (raw.text) {
      text = String(raw.text);
    } else if (raw.result && typeof raw.result === 'string') {
      text = raw.result;
    }
  }
  const trimmed = text.trim();
  if (!trimmed) {
    const rawPreview = JSON.stringify(raw || {}).slice(0, 300);
    return { files: null, runId, error: `子 Agent 返回空内容 (raw: ${rawPreview})` };
  }
  let parsed = null;

  // 0. 优先解析 <<<FILE:path>>> ... <<<END:path>>> 原样文本包裹（免 JSON 转义）
  try {
    const sentinelRe = /<<<FILE:([^>\r\n]+)>>>\r?\n([\s\S]*?)\r?\n?<<<END(?::[^>\r\n]+)?>>>/g;
    const collected = {};
    let m;
    let found = 0;
    while ((m = sentinelRe.exec(trimmed)) !== null) {
      const name = String(m[1] || '').trim().replace(/^\.\//, '');
      if (!name) continue;
      collected[name] = m[2];
      found += 1;
    }
    if (found > 0) parsed = { files: collected };
    else {
      // 0b. 容错：子 Agent 常忘记写 <<<END:...>>> 收尾哨兵。此时按
      // 「FILE 头 → 下一个 FILE 头或文本末尾」切块，并剥掉可能存在的残留 END 行。
      const looseRe = /<<<FILE:([^>\r\n]+)>>>\r?\n([\s\S]*?)(?=\r?\n?<<<FILE:|$)/g;
      let lm;
      let looseFound = 0;
      while ((lm = looseRe.exec(trimmed)) !== null) {
        const name = String(lm[1] || '').trim().replace(/^\.\//, '');
        if (!name) continue;
        const body = String(lm[2] || '')
          .replace(/\r?\n?<<<END(?::[^>\r\n]+)?>>>\s*$/, '')
          .replace(/\s+$/, '');
        if (!body) continue;
        collected[name] = body;
        looseFound += 1;
      }
      if (looseFound > 0) {
        // N5：宽松分支不认"没写 END"就当完整——逐块做结构判据，截断的一律剔除。
        const truncatedFiles = [];
        for (const [name, body] of Object.entries(collected)) {
          const why = looksTruncated(name, body);
          if (why) {
            truncatedFiles.push(`${name}（${why}）`);
            delete collected[name];
          }
        }
        parsed = {
          files: collected,
          truncated: truncatedFiles.length > 0,
          truncatedFiles,
        };
      }
    }
  } catch (_) { /* 落到 JSON 解析 */ }

  // 1. 直接整段解析
  try {
    const p = tolerantJsonParse(trimmed);
    if (pickFiles(p)) parsed = p;
  } catch (_) { /* 继续 */ }

  // 2. markdown 代码块，倒序优先
  if (!parsed) {
    const codeBlocks = [...trimmed.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)];
    for (let i = codeBlocks.length - 1; i >= 0; i -= 1) {
      const p = tolerantJsonParse(codeBlocks[i][1].trim());
      if (pickFiles(p)) { parsed = p; break; }
    }
  }

  // 3. 兜底提取最外层含 "files" 的 JSON 块
  if (!parsed) {
    const unfenced = trimmed.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
    let p = tolerantJsonParse(unfenced);
    if (pickFiles(p)) {
      parsed = p;
    } else {
      const m = unfenced.match(/\{[\s\S]*\}/);
      if (m) {
        p = tolerantJsonParse(m[0]);
        if (pickFiles(p)) parsed = p;
      }
    }
  }

  // 3b. 平衡对象前缀回退：合法 JSON + 尾部多余字符（游离引号/半句解释）。
  //     按括号深度截首个完整闭合对象再 strict parse。
  if (!parsed) {
    const end = balancedObjectEnd(trimmed);
    if (end > 0) {
      try {
        const p = JSON.parse(trimmed.slice(0, end));
        if (pickFiles(p)) parsed = p;
      } catch (_) { /* 落到下一档 */ }
    }
  }

  // 3c. 顶层 artifact 键提升：子 Agent 把某个白名单产物路径写到了 files
  //     之外（{"files": {...}, ".pd\acceptance\x-source-vs-output.md": "..."}）。
  //     仅提升命中白名单的键，其余顶层键一律保持原样（绝不在 files 里注入
  //     未声明的产物名）。
  if (parsed && Array.isArray(artifacts) && artifacts.length > 0) {
    const wanted = new Set(artifacts.map((a) => String(a).replace(/^\.\//, '')));
    if (!parsed.files || typeof parsed.files !== 'object') parsed.files = {};
    for (const k of Object.keys(parsed)) {
      if (k === 'files' || !wanted.has(k)) continue;
      if (parsed.files[k] === undefined) parsed.files[k] = parsed[k];
      delete parsed[k];
    }
  }

  // 4. 单 TeX 产物围栏回退：子 Agent 未遵守 <<<FILE:>>> 协议，但把唯一产物
  //    完整地放在一个 ```latex 围栏里返回。此时取「最长且以 TeX 结构开头」的
  //    围栏作为该产物——完整文件围栏必然长于任何代码示例/摘录围栏。
  if (!parsed && Array.isArray(artifacts) && artifacts.length === 1) {
    const name = String(artifacts[0]).replace(/^\.\//, '').trim();
    if (name && /\.tex$/i.test(name)) {
      const fences = [...trimmed.matchAll(/```(?:latex|tex)\s*\r?\n([\s\S]*?)```/gi)];
      let best = '';
      for (const f of fences) {
        const body = String(f[1] || '').replace(/\s+$/, '');
        if (body.length <= best.length) continue;
        // 必须看起来是 TeX 文件本体（允许前置注释行），排除零星公式片段
        if (!/^(?:%[^\r\n]*\r?\n)*\\(?:section|begin|documentclass|chapter|item)/.test(body)) continue;
        best = body;
      }
      if (best.length >= 2000) parsed = { files: { [name]: best } };
    }
  }
  if (!pickFiles(parsed)) {
    return {
      files: null,
      runId,
      error: `子 Agent 返回不可解析为 {"files": {...}}：${trimmed.slice(0, 200)}`,
    };
  }
  const out = { files: parsed.files, runId, error: parsed.error || '' };
  if (parsed.truncated) {
    out.truncated = true;
    out.truncatedFiles = Array.isArray(parsed.truncatedFiles) ? parsed.truncatedFiles : [];
  } else if (truncationHint(raw)) {
    // 解析看似成功，但适配器明确报告了 max_tokens 截断：产物完整性不可信
    out.truncated = true;
    out.truncatedFiles = [];
  }
  return out;
}

// ---------------------------------------------------------------------------
// 9. 调试记录：只落当前 self/.pd/，只含元数据，不含原料或产物全文
// ---------------------------------------------------------------------------

export function appendDebugRecord(selfDir, record, envIn) {
  const { fs, path } = mkEnv(envIn);
  try {
    const dir = path.join(selfDir, '.pd');
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, 'debug_subagent.log'),
      `${JSON.stringify({ ts: new Date().toISOString(), ...record })}\n`, 'utf8');
    return true;
  } catch (_) {
    return false;
  }
}
