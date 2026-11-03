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

export const DEFAULTS = Object.freeze({
  maxPromptChars: 600000, // 完整 prompt 字符上限；超出明确报错，不静默截断
  maxMapChars: 40000, // 单张映射表全文上限；超出才按 scope 过滤
  maxSourceFiles: 200, // 目录输入无 inventory 时最多枚举的文本文件数
});

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
      return { provider: String(route.provider), model: String(route.model), matched_key: key };
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
    return [
      '## 输出协议（本角色唯一协议：单个 JSON 对象；不要使用 FILE 块）',
      `最终回复必须只是一个可解析的 JSON 对象，形如 {"files": {"<相对路径>": "<完整文件内容>"}}，不要包裹多余文字。`,
      `files 的键必须恰好等于下列 ${artifacts.length} 个白名单路径（缺任何一个即判该派发单失败）：`,
      list,
      '.json 产物的内容可直接写成对象；其余产物为完整文件文本（JSON 字符串内的反斜杠需转义）。',
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
].join('\n');

// ---------------------------------------------------------------------------
// 3. 产物白名单校验
// ---------------------------------------------------------------------------

export function safeJoinSelf(selfDir, rel, env) {
  const { path } = mkEnv(env);
  if (typeof rel !== 'string' || !rel.trim()) return '';
  const norm = rel.replace(/\\/g, '/');
  if (path.isAbsolute(norm) || /^[A-Za-z]:/.test(norm) || norm.split('/').includes('..')) return '';
  const normSelf = path.resolve(selfDir);
  const abs = path.resolve(normSelf, norm);
  if (abs !== normSelf && !abs.startsWith(normSelf + path.sep)) return '';
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
  }, null, 2);
  assign('EVIDENCE', baseline);
  assign('VERSION_HASHES', baseline);
  assign('OBJECTIVES', Array.isArray(ctxObj.expected_qids) ? ctxObj.expected_qids.join(', ') : '');
  assign('APPROVED_MATERIAL_PACK', ctxObj.approved_material_pack || '');

  // ---- 来源：只收一次、完整、带路径与摘要 ----
  const isRepair = String(req.stage || '') === 'REPAIR' || Boolean(ctxObj.repair_reason);
  let inputPath = ctxObj.input_file || ctxObj.input_path || spec.input_file || spec.input_path || '';
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
      const sel = selectMapRows(t, mapKeys, { limit: maxMapChars, label: file });
      mapMeta[file] = { chars: sel.text.length, filtered: sel.filtered, omitted_lines: sel.omitted_lines };
      mapBlocks.push(`\n## ${title}${sel.filtered ? '（已按本批 scope 过滤，关键行完整保留）' : '（全文）'}\n\`\`\`markdown\n${sel.text}\n\`\`\``);
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
    const rptDir = path.join(selfDir, '.pd', 'reports');
    const rptSt = statSafe(fs, rptDir);
    if (rptSt && rptSt.isDirectory()) {
      const stems = artifacts.map((a) => a.replace(/\.tex$/i, ''));
      for (const f of fs.readdirSync(rptDir).slice().sort()) {
        if (!f.endsWith('.json')) continue;
        try {
          const obj = JSON.parse(fs.readFileSync(path.join(rptDir, f), 'utf8'));
          if (Array.isArray(obj.issues)) {
            for (const iss of obj.issues) {
              const msg = typeof iss === 'string' ? iss : (iss.message || JSON.stringify(iss));
              if (stems.some((s) => msg.includes(s))) issueList.push(`[${f.replace('.json', '')}] ${msg}`);
            }
          }
        } catch (_) { /* 单个损坏报告不阻断 */ }
      }
    }
    const existing = artifacts.map((a) => {
      const abs = safeJoinSelf(selfDir, a, e);
      const t = abs ? readIfFile(fs, abs) : '';
      return t ? `## 当前待修复文件内容（${a}）\n\`\`\`latex\n${t}\n\`\`\`` : '';
    }).filter(Boolean).join('\n\n');
    repairBlock = [
      '## 【返修专项指令与门禁报错详情】',
      `- 返修原因: ${reason}`,
      issueList.length > 0 ? `- 门禁具体命中问题:\n  ${issueList.join('\n  ')}` : '',
      '- 语法避坑指南: \\fillin[参考答案]{预留宽度}[选项1/选项2/选项3] 必须严格有3组定界符；若参考答案或选项内部包含中括号（如闭区间记号 [a, b]），必须用花括号包裹以防 LaTeX 宏提早闭合：如 \\fillin[{[a, b]}]{25mm}[{[a, b]}/{[c, d]}/{[e, f]}] 或改写为不等式形式。',
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

  const tail = [
    repairBlock ? `\n${repairBlock}` : '',
    ...mapBlocks,
    texGuide ? `\n## TeX 接口写作者速查\n${texGuide}` : '',
    // 模板没有来源占位符时，来源块仍只出现一次——放在尾部
    (!rendered.sourceTemplatePlaced && sourceBlock) ? `\n${sourceBlock}` : '',
    reviewTargets,
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
  return {
    prompt,
    meta: {
      role,
      scopes,
      artifacts,
      prompt_chars: prompt.length,
      prompt_sha256: sha256Text(crypto, prompt),
      source_files: collected.sources.map((s) => ({ path: s.rel, chars: s.chars, sha256: s.sha256 })),
      source_chars: collected.sources.reduce((n, s) => n + s.chars, 0),
      source_block_count: occurrences(SOURCE_BEGIN),
      source_placement: !sourceBlock ? 'none' : (rendered.sourceTemplatePlaced ? 'template' : 'tail'),
      maps: mapMeta,
      review_targets: reviewMeta,
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

export function parseSubagentFiles(raw) {
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
  } catch (_) { /* 落到 JSON 解析 */ }

  // 1. 直接整段解析
  try {
    const p = JSON.parse(trimmed);
    if (pickFiles(p)) parsed = p;
  } catch (_) { /* 继续 */ }

  // 2. markdown 代码块，倒序优先
  if (!parsed) {
    const codeBlocks = [...trimmed.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)];
    for (let i = codeBlocks.length - 1; i >= 0; i -= 1) {
      try {
        const p = JSON.parse(codeBlocks[i][1].trim());
        if (pickFiles(p)) { parsed = p; break; }
      } catch (_) { /* 继续 */ }
    }
  }

  // 3. 兜底提取最外层含 "files" 的 JSON 块
  if (!parsed) {
    const unfenced = trimmed.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
    try {
      const p = JSON.parse(unfenced);
      if (pickFiles(p)) parsed = p;
    } catch (_) {
      const m = unfenced.match(/\{[\s\S]*\}/);
      if (m) {
        try {
          const p = JSON.parse(m[0]);
          if (pickFiles(p)) parsed = p;
        } catch (_) { /* 放弃 */ }
      }
    }
  }
  if (!pickFiles(parsed)) {
    return {
      files: null,
      runId,
      error: `子 Agent 返回不可解析为 {"files": {...}}：${trimmed.slice(0, 200)}`,
    };
  }
  return { files: parsed.files, runId, error: '' };
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
