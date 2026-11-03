#!/usr/bin/env node
// validate_math.cjs — 严格数学校验器（任务卡02 / DATA-CONTRACTS C8）
//
// 一次 Node 进程批量校验全部公式；用随包可信 KaTeX UMD（vm 沙箱执行）与共享
// 配置 templates/html/math-config.json 调用
//   katex.renderToString(tex, {throwOnError: true, trust: false, ...})
// 不执行输入材料中的任何 JS：输入仅是 JSON 数据，公式只作为字符串传给 KaTeX。
//
// 用法:  node validate_math.cjs [--katex <katex.min.js>] [--input <file.json>]
//        输入缺省从 stdin 读取 JSON：
//          {"config": {...math-config...},
//           "items": [{"index":1,"id":"qcm-1","card":"c1","tex":"x^2","display":false}, ...]}
// 输出 (stdout, 单个 JSON):
//   {"status":"ok"|"fail"|"env_error","engine":{"name":"katex","version":"..."},
//    "total":N,"checked":N,"failures":[{index,id,card,message,position,length}],
//    "env_error":null|"..."}
// 退出码: 0=全部通过, 1=有公式失败, 2=环境/输入错误（KaTeX 不可加载、JSON 非法等）
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function parseArgs(argv) {
  const out = { katex: null, input: null };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--katex') out.katex = argv[++i];
    else if (argv[i] === '--input') out.input = argv[++i];
  }
  return out;
}

function emit(obj, code) {
  process.stdout.write(JSON.stringify(obj) + '\n');
  process.exitCode = code;
}

function envError(message, extra) {
  emit(Object.assign({
    status: 'env_error', engine: null, total: 0, checked: 0, failures: [],
    env_error: message,
  }, extra || {}), 2);
}

function loadKatex(file) {
  const code = fs.readFileSync(file, 'utf8');
  const moduleObject = { exports: {} };
  const context = { module: moduleObject, exports: moduleObject.exports };
  vm.runInNewContext(code, context, { timeout: 10000, filename: file });
  const katex = moduleObject.exports;
  if (!katex || typeof katex.renderToString !== 'function') {
    throw new Error('KaTeX UMD 未导出 renderToString：' + file);
  }
  return katex;
}

function readInput(file) {
  const text = file ? fs.readFileSync(file, 'utf8') : fs.readFileSync(0, 'utf8');
  return JSON.parse(text);
}

const ERROR_COLOR = '#fe01fe';   // 与 math-config.json 的 errorColor 一致；浏览器端同值

function main() {
  const args = parseArgs(process.argv.slice(2));
  const katexFile = args.katex ||
    path.join(__dirname, '..', 'templates', 'html', 'katex', 'katex.min.js');

  let payload;
  try {
    payload = readInput(args.input);
  } catch (e) {
    return envError('输入 JSON 无法读取/解析: ' + e.message);
  }
  if (!payload || !Array.isArray(payload.items)) {
    return envError('输入缺少 items 数组');
  }

  let katex;
  try {
    katex = loadKatex(katexFile);
  } catch (e) {
    return envError('随包 KaTeX 无法加载（' + katexFile + '）: ' + e.message);
  }

  const cfg = payload.config || {};
  const baseOptions = {
    throwOnError: true,
    trust: false,                       // 契约：trust 恒为 false，配置不可放宽
    maxExpand: typeof cfg.maxExpand === 'number' ? cfg.maxExpand : 1000,
    strict: cfg.strict === undefined ? 'ignore' : cfg.strict,
    output: 'html',
    errorColor: ERROR_COLOR,            // 哨兵色：trust=false 时 \href 等被降级为该色文本而非抛错
  };
  if (typeof cfg.maxSize === 'number') baseOptions.maxSize = cfg.maxSize;
  const macroSrc = cfg.macros && typeof cfg.macros === 'object' ? cfg.macros : {};

  const failures = [];
  let checked = 0;
  for (const item of payload.items) {
    checked++;
    // KaTeX 会就地修改 macros（\gdef 等），故每个公式使用独立拷贝
    const options = Object.assign({}, baseOptions, {
      displayMode: !!item.display,
      macros: JSON.parse(JSON.stringify(macroSrc)),
    });
    try {
      const html = katex.renderToString(String(item.tex === undefined ? '' : item.tex), options);
      if (html.indexOf(ERROR_COLOR) >= 0) {
        // KaTeX 在 trust=false 下不抛错，而是把受限命令渲染成错误色文本；严格校验视为失败
        throw new Error('KaTeX 把部分内容降级为错误色文本（受限/不支持的命令，如 \\href \\url \\includegraphics）');
      }
    } catch (e) {
      failures.push({
        index: item.index,
        id: item.id,
        card: item.card === undefined ? null : item.card,
        message: String(e && e.message ? e.message : e),
        position: (e && typeof e.position === 'number') ? e.position : null,
        length: (e && typeof e.length === 'number') ? e.length : null,
      });
    }
  }

  emit({
    status: failures.length ? 'fail' : 'ok',
    engine: { name: 'katex', version: String(katex.version) },
    total: payload.items.length,
    checked,
    failures,
    env_error: null,
  }, failures.length ? 1 : 0);
}

main();
