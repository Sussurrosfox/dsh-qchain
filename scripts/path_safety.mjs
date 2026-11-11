// -*- coding: utf-8 -*-
// path_safety.mjs — 路径安全的唯一原语（修复 T1/D1/D2）
//
// 背景（审计发现）：
//   * 旧 safeJoinSelf 只做 path.resolve + 字符串前缀比较 ⇒ self/assets 若是 junction/
//     symlink 指向工作目录之外，词法上仍"在 self 内"，写盘会真实落到 self 外（D2）。
//   * 旧 dispatch_id 直接拼进 .pd/dispatch-claims/<id>.json，含 ../ 即可把 claim 写到
//     self 的父目录（D1）。
//   * 旧 asset_bundle 先按 ".." 过滤再 unquote，%2e%2e 绕过（T1）。
//
// 本模块是上述三处（以及今后任何新增写盘点）的唯一实现：
//   normalizeRel(rel)            → 相对路径规范化（解一次 percent-encoding，拒绝 .. / 绝对路径）
//   sanitizeId(value)            → 标识符白名单（dispatch_id / scope / batch_id 等）
//   safeJoinUnder(root, rel)     → 规范化 + 拼接到 root 下 + realpath 越界断言
//   assertWithin(root, target)   → 写盘前最后一道闸（realpath 归一后判定）
//   isWithin(root, target)       → 布尔版本（读取侧使用，不抛错）
//
// 关键设计：realpath 归一 + 「最近已存在祖先」回填，因此**目标文件尚不存在**也能判定；
// junction / symlink / 大小写差异 / 8.3 短名都会在 realpath 后暴露为真实位置。
import * as path from 'node:path';
import * as fs from 'node:fs';

export class PathSafetyError extends Error {
  constructor(code, message, detail = {}) {
    super(message);
    this.name = 'PathSafetyError';
    this.code = code;
    this.detail = detail;
  }
}

// ---- 相对路径规范化 -------------------------------------------------------

/** 单段 percent 解码；解得 '/' 或 '\\' 视为段落走私，直接拒绝。 */
function decodeSegment(seg, label) {
  let out = seg;
  if (out.includes('%')) {
    try {
      out = decodeURIComponent(seg);
    } catch (_) {
      throw new PathSafetyError('PATH_ENCODING_INVALID',
        `${label} 含非法 percent 编码：${seg}`, { segment: seg });
    }
  }
  if (out.includes('/') || out.includes('\\')) {
    throw new PathSafetyError('PATH_SEGMENT_SMUGGLED',
      `${label} 的路径段解码后含分隔符（编码穿越）：${seg}`, { segment: seg });
  }
  return out;
}

/**
 * 把外部输入规范成 root 下的相对路径（以 '/' 分隔）。
 * 拒绝：空串、NUL、绝对路径、盘符、任何 `..` 段、解码后走私分隔符。
 * 注意：percent 解码只做一次（%252e → %2e 不会被二次还原成 .）。
 */
export function normalizeRel(rel, opts = {}) {
  const label = opts.label || '路径';
  if (typeof rel !== 'string') {
    throw new PathSafetyError('PATH_NOT_STRING', `${label} 必须是字符串`, {});
  }
  if (!rel.trim()) {
    throw new PathSafetyError('PATH_EMPTY', `${label} 不能为空`, {});
  }
  if (rel.includes('\0')) {
    throw new PathSafetyError('PATH_NUL', `${label} 含 NUL 字符`, {});
  }
  const raw = rel.replace(/\\/g, '/');
  if (raw.startsWith('/') || /^[A-Za-z]:/.test(raw)) {
    throw new PathSafetyError('PATH_ABSOLUTE', `${label} 不能是绝对路径：${rel}`, { path: rel });
  }
  const parts = [];
  for (const seg of raw.split('/')) {
    if (seg === '' || seg === '.') continue;
    const dec = decodeSegment(seg, label);
    if (dec === '' || dec === '.') continue;
    if (dec === '..') {
      throw new PathSafetyError('PATH_TRAVERSAL',
        `${label} 不能含上跳段（..）：${rel}`, { path: rel });
    }
    if (path.isAbsolute(dec) || /^[A-Za-z]:/.test(dec)) {
      throw new PathSafetyError('PATH_ABSOLUTE', `${label} 解码后成为绝对路径：${rel}`, { path: rel });
    }
    parts.push(dec);
  }
  if (!parts.length) {
    throw new PathSafetyError('PATH_EMPTY', `${label} 规范化后为空：${rel}`, { path: rel });
  }
  return parts.join('/');
}

// ---- 标识符白名单 ---------------------------------------------------------

/** dispatch_id / batch_id / scope 等用于拼文件名的标识符：字母数字开头，仅 . _ - */
export function sanitizeId(value, opts = {}) {
  const label = opts.label || '标识符';
  const max = Number.isInteger(opts.max) ? opts.max : 128;
  const s = typeof value === 'string' ? value.trim() : (value == null ? '' : String(value).trim());
  if (!s) {
    throw new PathSafetyError('ID_EMPTY', `${label} 不能为空`, {});
  }
  if (s.length > max) {
    throw new PathSafetyError('ID_TOO_LONG', `${label} 超长（>${max}）：${s.slice(0, 24)}…`, {});
  }
  if (s === '.' || s === '..') {
    throw new PathSafetyError('ID_RESERVED', `${label} 不能是 . 或 ..：${s}`, {});
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(s)) {
    throw new PathSafetyError('ID_CHARSET_INVALID',
      `${label} 只允许字母/数字开头，且只含字母数字与 . _ -：${s}`, { value: s });
  }
  return s;
}

// ---- realpath 归一 -------------------------------------------------------

/** 最近已存在祖先的 realpath + 剩余段回填（目标不存在也能得到真实位置）。 */
export function realKey(target) {
  let cur = path.resolve(target);
  const rest = [];
  for (;;) {
    try {
      const real = fs.realpathSync.native
        ? fs.realpathSync.native(cur)
        : fs.realpathSync(cur);
      return rest.length
        ? path.join(real, ...rest.slice().reverse())
        : real;
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

/** target 的 realpath 是否落在 root 的 realpath 之内（含 target===root）。 */
export function isWithin(root, target) {
  if (typeof root !== 'string' || !root || typeof target !== 'string' || !target) return false;
  const rk = realKey(root);
  const tk = realKey(target);
  const rel = path.relative(rk, tk);
  if (rel === '') return true;
  if (path.isAbsolute(rel)) return false;
  return !(rel === '..' || rel.startsWith(`..${path.sep}`) || rel.startsWith('../'));
}

/** 写盘前最后一道闸：越界即抛 PathSafetyError（不允许静默跳过）。 */
export function assertWithin(root, target, opts = {}) {
  const label = opts.label || '写入目标';
  if (isWithin(root, target)) return realKey(target);
  throw new PathSafetyError('PATH_ESCAPES_ROOT',
    `${label} 越出 ${root}（realpath 归一后判定，junction/symlink 不例外）：${target}`,
    { root: realKey(root), target: realKey(target) });
}

/** 规范化 + 拼接 + 越界断言，一步到位（写盘点首选）。 */
export function safeJoinUnder(root, rel, opts = {}) {
  const norm = normalizeRel(rel, opts);
  const abs = path.join(path.resolve(root), ...norm.split('/'));
  assertWithin(root, abs, { label: opts.label || '写入目标' });
  return abs;
}
