import { execFile } from 'node:child_process';
import * as path from 'node:path';
import * as fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

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
  if (typeof rel !== 'string' || !rel.trim()) return '';
  const norm = rel.replace(/\\/g, '/');
  if (path.isAbsolute(norm) || norm.split('/').includes('..')) return '';
  const normSelf = path.resolve(selfDir);
  const abs = path.resolve(normSelf, norm);
  if (abs !== normSelf && !abs.startsWith(normSelf + path.sep)) return '';
  return abs;
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

const QD_PLACEHOLDER_RE = /\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g;

function qdRenderTemplate(tpl, values) {
  if (!tpl) return '';
  return tpl.replace(QD_PLACEHOLDER_RE, (m, key) => {
    const v = values[key];
    return v == null ? '' : String(v);
  });
}

function qdParseSubagentFiles(raw) {
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
          if (typeof b === 'object') {
            return b.text || b.content || b.value || '';
          }
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
  try {
    fs.writeFileSync('D:\\teaching\\Yau\\0\\just\\01-linear\\6\\self\\.pd\\subagent_last_reply.txt', String(text), 'utf-8');
  } catch (_) {}
  const trimmed = text.trim();
  if (!trimmed) {
    const rawPreview = JSON.stringify(raw || {}).slice(0, 300);
    return { files: null, runId, error: `子 Agent 返回空内容 (raw: ${rawPreview})` };
  }
  let parsed = null;

  // 1. 尝试直接整段解析
  try {
    const p = JSON.parse(trimmed);
    if (p && typeof p === 'object' && p.files && typeof p.files === 'object') {
      parsed = p;
    }
  } catch (_) {}

  // 2. 扫描所有 markdown 代码块，倒序优先（取最后的代码块）
  if (!parsed) {
    const codeBlocks = [...trimmed.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)];
    for (let i = codeBlocks.length - 1; i >= 0; i--) {
      try {
        const p = JSON.parse(codeBlocks[i][1].trim());
        if (p && typeof p === 'object' && p.files && typeof p.files === 'object') {
          parsed = p;
          break;
        }
      } catch (_) {}
    }
  }

  // 3. 兜底提取最外层含 "files" 的 JSON 块
  if (!parsed) {
    const unfenced = trimmed
      .replace(/^```(?:json)?\s*/i, '')
      .replace(/```\s*$/,'')
      .trim();
    try {
      const p = JSON.parse(unfenced);
      if (p && typeof p === 'object' && p.files && typeof p.files === 'object') {
        parsed = p;
      }
    } catch (_) {
      const m = unfenced.match(/\{[\s\S]*\}/);
      if (m) {
        try {
          const p = JSON.parse(m[0]);
          if (p && typeof p === 'object' && p.files && typeof p.files === 'object') {
            parsed = p;
          }
        } catch (_) {}
      }
    }
  }
  if (!parsed || typeof parsed !== 'object'
      || !parsed.files || typeof parsed.files !== 'object') {
    return {
      files: null,
      runId,
      error: `子 Agent 返回不可解析为 {"files": {...}}：${trimmed.slice(0, 200)}`,
    };
  }
  return { files: parsed.files, runId, error: '' };
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
    description: '运行题链流水线质量与安全门禁校验（pedagogy, numbering, fidelity, semantic, scope, provenance, budget, classroom_plan, shuffle, build, check_html, compare, dialogue, review_pack, agent_audit, delivery, test_runtime 或 all）。',
    parameters: {
      type: 'object',
      properties: {
        gate: {
          type: 'string',
          enum: [
            'all',
            'pedagogy',
            'numbering',
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

      const runScript = async (name, scriptRel, scriptArgs = [], note = '') => {
        const fullArgs = [path.join(engineRoot, scriptRel), ...scriptArgs];
        const res = await runPython(fullArgs, proj, 120000, pythonBin);
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
          '⚠ 干跑模式（--dry-run）：执行全部交付检查但不真实发布 release/。'),
        test_runtime: () => runScript('运行时契约自动化套件 (test_runtime.py)', 'scripts/test_runtime.py', []),
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
    description: '把题链派发单派发给宿主 subagent 子 Agent 执行：渲染派发提示词（prompts/subagent_prompt.md 或 acceptance_prompt.md）→ 子 Agent 生成产物 → 按 request.artifacts 白名单原子写入 self/ → 落证据台账 .pd/agent-calls/<dispatch_id>.json（含 worker/provider/model/摘要）。AGENT_DISPATCH 中断恢复后必须先调本工具；全部 dispatch_id 为 ok 后才可 qchain_pipeline action=resume。',
    parameters: {
      type: 'object',
      properties: {
        request_ref: {
          type: 'string',
          description: '单个派发单 JSON 路径（相对 self_dir 或绝对路径）；与 batch_id 二选一',
        },
        batch_id: {
          type: 'string',
          description: '批次 ID：派发 .pd/dispatch-plan.json 中该批次全部派发单；与 request_ref 二选一',
        },
        self_dir: {
          type: 'string',
          description: 'self 工作区目录（缺省探测：cwd/self → workspaceRegistry[0]/self）',
        },
        max_concurrency: {
          type: 'integer',
          description: '子 Agent 并发上限（缺省读 config.yaml agent_dispatch.max_concurrency，再缺省 2）',
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
      const resolveRequests = () => {
        if (args.request_ref) {
          const abs = path.isAbsolute(args.request_ref)
            ? args.request_ref
            : path.join(selfDir, args.request_ref);
          const one = qdReadJson(abs);
          return one ? [one] : [];
        }
        if (args.batch_id) {
          const plan = qdReadJson(path.join(selfDir, '.pd', 'dispatch-plan.json'));
          const planBatch = String((plan && plan.batch_id) || '');
          const ids = ((plan && Array.isArray(plan.requests)) ? plan.requests : [])
            .filter((r) => String((r || {}).batch_id || planBatch) === String(args.batch_id))
            .map((r) => String((r || {}).dispatch_id || ''));
          const out = [];
          for (const id of ids) {
            const full = qdReadJson(path.join(selfDir, '.pd', 'agent-requests', `${id}.json`));
            if (full) out.push(full);
          }
          return out;
        }
        const dir = path.join(selfDir, '.pd', 'agent-requests');
        let names = [];
        try {
          names = fs.readdirSync(dir).filter((f) => f.endsWith('.json')).sort();
        } catch (_) {}
        const out = [];
        for (const name of names) {
          const r = qdReadJson(path.join(dir, name));
          if (r && String(r.status || '') === 'pending') out.push(r);
        }
        return out;
      };

      const requests = resolveRequests();
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

      // ---- 派发配置（与 Python 状态机同源：_load_dispatch_config） ---------
      const readDispatchConfig = async () => {
        const snippet = [
          'import json, sys',
          'sys.path.insert(0, sys.argv[1])',
          'from workflow.nodes.section_batch_nodes import _load_dispatch_config',
          'print(json.dumps(_load_dispatch_config(sys.argv[2]), ensure_ascii=False))',
        ].join('\n');
        try {
          const res = await runPython(['-c', snippet, engineRoot, selfDir],
            resolveEngineRoot(engineRoot), 60000, pythonBin);
          const lines = String(res.stdout || '').trim().split('\n').filter(Boolean);
          if (lines.length) return JSON.parse(lines[lines.length - 1]);
        } catch (_) {}
        return { max_concurrency: 2, model_map: {} };
      };
      const cfg = await readDispatchConfig();
      const maxConcurrency = Math.max(1, Math.min(
        Number(args.max_concurrency) || Number(cfg.max_concurrency) || 2, 16));
      const modelMap = (cfg.model_map && typeof cfg.model_map === 'object') ? cfg.model_map : {};
      const stageRoute = (stage) => {
        const s = String(stage || '').toUpperCase();
        for (const key of [s.split('_')[0], s]) {
          const route = modelMap[key];
          if (route && route.provider && route.model) {
            return { provider: String(route.provider), model: String(route.model) };
          }
        }
        return {};
      };

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
      const callSubagent = async (subArgs) => {
        if (subagentDef) return subagentDef.execute(subArgs, exec);
        try {
          const tools = ctx?.tools || ctx?.get?.('tools');
          if (tools && typeof tools.execute === 'function') {
            return tools.execute({
              name: 'subagent',
              arguments: subArgs,
              agent: exec && exec.agent,
              signal: exec && exec.signal,
              callId: exec && exec.callId,
            });
          }
        } catch (_) {}
        const subagents = ctx?.get?.('subagents') || ctx?.subagents;
        if (subagents && typeof subagents.start === 'function') {
          const run = await subagents.start('spawn', {
            label: subArgs.description || 'qchain subagent',
            prompt: [{ type: 'text', text: subArgs.prompt }],
            parent: exec && exec.agent,
            signal: exec && exec.signal,
          });
          const result = await run.result;
          if (typeof run.dispose === 'function') {
            try { run.dispose(); } catch (_) {}
          }
          return result;
        }
        throw new Error('宿主 subagent 工具不可用（ctx.tools.get("subagent") 与 subagents 服务均缺失）');
      };

      // ---- 提示词渲染 ------------------------------------------------------
      const buildPrompt = (request) => {
        const promptFile = String(request.prompt_file || 'subagent_prompt.md')
          .replace(/^prompts\//, '');
        let tpl = '';
        try {
          tpl = fs.readFileSync(path.join(engineRoot, 'prompts', promptFile), 'utf8');
        } catch (_) { tpl = ''; }
        const ctxObj = (request.context && typeof request.context === 'object')
          ? request.context : {};
        const spec = (ctxObj.user_spec && typeof ctxObj.user_spec === 'object')
          ? ctxObj.user_spec : {};
        const artifacts = (Array.isArray(request.artifacts) ? request.artifacts : [])
          .filter((a) => typeof a === 'string' && a);
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
        assign('SECTION_TITLE', ctxObj.section_title || request.scope || '');
        assign('OUTPUT_FILE', artifacts[0] || '');
        assign('SCOPE', request.scope || '');
        assign('STAGE', request.stage || '');
        assign('ROLE', request.role || '');
        assign('BATCH_ID', request.batch_id || '');
        assign('DISPATCH_ID', request.dispatch_id || '');
        let inputFile = ctxObj.input_file || ctxObj.input_path
          || spec.input_file || spec.input_path || '';
        if (!inputFile) {
          try {
            const wsPath = path.join(selfDir, '.pd', 'workspace.json');
            if (fs.existsSync(wsPath)) {
              const ws = JSON.parse(fs.readFileSync(wsPath, 'utf8'));
              if (ws.input_path) inputFile = ws.input_path;
            }
          } catch (_) {}
        }
        assign('INPUT_FILE', inputFile);
        let rawContent = '';
        if (inputFile && fs.existsSync(inputFile)) {
          try {
            rawContent = fs.readFileSync(inputFile, 'utf8');
            assign('SOURCE_EXCERPTS', rawContent);
            assign('RAW_INPUT', rawContent);
            assign('MATERIAL_CONTENT', rawContent);
          } catch (_) {}
        }
        let sourceMapText = '';
        let depMapText = '';
        let covMapText = '';
        try {
          const smp = path.join(selfDir, 'source-map.md');
          if (fs.existsSync(smp)) sourceMapText = fs.readFileSync(smp, 'utf8');
        } catch (_) {}
        try {
          const dmp = path.join(selfDir, 'dependency-map.md');
          if (fs.existsSync(dmp)) depMapText = fs.readFileSync(dmp, 'utf8');
        } catch (_) {}
        try {
          const cmp = path.join(selfDir, 'coverage-map.md');
          if (fs.existsSync(cmp)) covMapText = fs.readFileSync(cmp, 'utf8');
        } catch (_) {}

        let texGuide = '';
        try {
          const tgp = path.join(engineRoot, 'references', 'tex-interface.md');
          if (fs.existsSync(tgp)) {
            const fullTex = fs.readFileSync(tgp, 'utf8');
            const idx = fullTex.indexOf('## 三、 写作者速查');
            if (idx !== -1) texGuide = fullTex.slice(idx, idx + 2500);
          }
        } catch (_) {}
        const baseline = JSON.stringify({
          content_digest: ctxObj.content_digest || '',
          student_pdf_sha256: ctxObj.student_pdf_sha256 || '',
          student_html_sha256: ctxObj.student_html_sha256 || '',
        }, null, 2);
        assign('EVIDENCE', baseline);
        assign('VERSION_HASHES', baseline);
        assign('OBJECTIVES', Array.isArray(ctxObj.expected_qids)
          ? ctxObj.expected_qids.join(', ') : '');
        assign('APPROVED_MATERIAL_PACK', ctxObj.approved_material_pack || '');
        let existingArtifactContent = '';
        if (artifacts[0]) {
          try {
            const artPath = path.join(selfDir, artifacts[0]);
            if (fs.existsSync(artPath)) {
              existingArtifactContent = fs.readFileSync(artPath, 'utf8');
            }
          } catch (_) {}
        }
        let repairContextText = '';
        if (request.stage === 'REPAIR' || ctxObj.repair_reason) {
          const reason = ctxObj.repair_reason || request.instruction || '';
          let issueList = [];
          try {
            const rptDir = path.join(selfDir, '.pd', 'reports');
            if (fs.existsSync(rptDir)) {
              for (const rptFile of fs.readdirSync(rptDir)) {
                if (!rptFile.endsWith('.json')) continue;
                try {
                  const rptObj = JSON.parse(fs.readFileSync(path.join(rptDir, rptFile), 'utf8'));
                  if (Array.isArray(rptObj.issues)) {
                    for (const iss of rptObj.issues) {
                      const msg = typeof iss === 'string' ? iss : (iss.message || JSON.stringify(iss));
                      if (artifacts[0] && msg.includes(artifacts[0].replace('.tex', ''))) {
                        issueList.push(`[${rptFile.replace('.json', '')}] ${msg}`);
                      }
                    }
                  }
                } catch (_) {}
              }
            }
          } catch (_) {}
          repairContextText = [
            '## 【返修专项指令与门禁报错详情】',
            `- 返修原因: ${reason}`,
            issueList.length > 0 ? `- 门禁具体命中问题:\n  ${issueList.join('\n  ')}` : '',
            '- 语法避坑指南: \\fillin[参考答案]{预留宽度}[选项1/选项2/选项3] 必须严格有3组定界符；若参考答案或选项内部包含中括号（如闭区间记号 [a, b]），必须用花括号包裹以防 LaTeX 宏提早闭合：如 \\fillin[{[a, b]}]{25mm}[{[a, b]}/{[c, d]}/{[e, f]}] 或改写为不等式形式。',
            existingArtifactContent ? `## 当前待修复文件内容（${artifacts[0]}）\n\`\`\`latex\n${existingArtifactContent}\n\`\`\`\n请直接在上述现有文件基础上进行精准修正，保持其余题目、qid 注释、宏和结构完好，返回修改后的完整文件内容。` : '',
          ].filter(Boolean).join('\n\n');
        }
        const rendered = qdRenderTemplate(tpl, values);
        const taskBlock = [
          '',
          '## 派发任务书（机器可读）',
          `- dispatch_id: ${request.dispatch_id || ''}`,
          `- batch_id: ${request.batch_id || ''}`,
          `- stage: ${request.stage || ''}`,
          `- scope: ${request.scope || ''}`,
          `- role: ${request.role || ''}`,
          '## 项目路径',
          selfDir,
          '## 执行指令',
          String(request.instruction || ''),
          request.command ? `## 提交命令\n${request.command}` : '',
          '## 只允许写这些产物（白名单，越界写入将被拒绝）',
          ...artifacts.map((a) => `- ${a}`),
          '## 返回格式（必须是可解析 JSON，勿包裹多余文字）',
          '{"files": {"<相对路径>": "<完整文件内容>"}}',
          '注意：files 的键只能是上面白名单中的相对路径；内容为完整文件文本。',
          repairContextText ? `\n${repairContextText}` : '',
          (!repairContextText && sourceMapText) ? `\n## 章节映射骨架 (source-map.md 节选)\n\`\`\`markdown\n${sourceMapText.slice(0, 3000)}\n\`\`\`` : '',
          (!repairContextText && depMapText) ? `\n## 依赖拓扑 (dependency-map.md 节选)\n\`\`\`markdown\n${depMapText.slice(0, 3000)}\n\`\`\`` : '',
          (!repairContextText && covMapText) ? `\n## 覆盖与题链规划 (coverage-map.md 节选)\n\`\`\`markdown\n${covMapText.slice(0, 3000)}\n\`\`\`` : '',
          texGuide ? `\n## TeX 接口写作者速查\n${texGuide}` : '',
          (!repairContextText && rawContent) ? `\n## 原材料全文（${path.basename(inputFile)}）\n\`\`\`markdown\n${rawContent}\n\`\`\`` : '',
        ].filter((s) => s !== '').join('\n');
        return rendered + taskBlock;
      };

      // ---- 单派发单执行（含重试与证据落账） --------------------------------
      const dispatchOne = async (request) => {
        const dispatchId = String(request.dispatch_id || '');
        const artifacts = (Array.isArray(request.artifacts) ? request.artifacts : [])
          .filter((a) => typeof a === 'string' && a);
        const before = (request.before && typeof request.before === 'object')
          ? request.before : {};
        const maxRetries = Number.isFinite(Number(request.max_retries))
          ? Math.max(0, Number(request.max_retries)) : 2;
        const route = stageRoute(request.stage);
        const prompt = buildPrompt(request);
        let status = 'worker_error';
        let error = '未执行';
        let evidenceAbs = '';
        let attempts = 0;
        for (let attempt = 1; attempt <= maxRetries + 1; attempt += 1) {
          attempts = attempt;
          const startedAt = new Date();
          let parsed = { files: null, runId: '', error: '' };
          try {
            const dbgFile = path.join(selfDir, '.pd', 'debug_subagent.log');
            fs.appendFileSync(dbgFile, `\n\n[${new Date().toISOString()}] === dispatchOne attempt ${attempt} start ===\nPrompt len: ${prompt.length}\nPrompt head:\n${prompt.slice(0, 300)}\nPrompt tail:\n${prompt.slice(-300)}\n`);
            const raw = await callSubagent({
              description: `qchain ${request.stage || ''} ${request.scope || dispatchId}（${request.role || '子 Agent'}）`,
              prompt,
              run_in_background: false,
              ...(route.provider && route.model
                ? { provider: route.provider, model: route.model } : {}),
            });
            fs.appendFileSync(dbgFile, `\n[${new Date().toISOString()}] raw result:\n` + JSON.stringify(raw, null, 2) + '\n');
            parsed = qdParseSubagentFiles(raw);
            fs.appendFileSync(dbgFile, `\n[${new Date().toISOString()}] parsed result: files=${parsed.files ? Object.keys(parsed.files).join(',') : 'null'}, err=${parsed.error}\n`);
          } catch (err) {
            const dbgFile = path.join(selfDir, '.pd', 'debug_subagent.log');
            try {
              fs.appendFileSync(dbgFile, `\n[${new Date().toISOString()}] callSubagent error:\n` + (err && err.stack ? err.stack : String(err)) + '\n');
            } catch (_) {}
            parsed = {
              files: null,
              runId: '',
              error: `子 Agent 调用失败: ${err && err.message ? err.message : String(err)}`,
            };
          }
          const finishedAt = new Date();
          const isReview = String(request.stage || '').toUpperCase().includes('T4')
            || String(request.stage || '').toUpperCase().includes('REVIEW')
            || String(request.role || '').includes('验收');
          const worker = isReview
            ? (parsed.runId ? `subagent:reviewer-${parsed.runId}` : `subagent:reviewer-${dispatchId}`)
            : (parsed.runId ? `subagent:author-${parsed.runId}` : `subagent:author-${dispatchId}`);
          const written = [];
          const outputDigests = {};
          status = 'ok';
          error = '';
          try {
            fs.writeFileSync('D:\\teaching\\Yau\\0\\just\\01-linear\\6\\self\\.pd\\subagent_parsed_keys.txt', JSON.stringify(Object.keys(parsed.files || {})), 'utf-8');
          } catch (_) {}
          if (!parsed.files) {
            status = 'worker_error';
            error = parsed.error || '子 Agent 未返回可用产物';
          } else {
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
                error = `产物路径非法（越界或绝对路径）: ${rel}`;
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
                outputDigests[rel] = qdSha256File(abs);
              } catch (err) {
                status = 'worker_error';
                error = `写入 ${rel} 失败: ${err && err.message ? err.message : String(err)}`;
                break;
              }
            }
            if (status === 'ok') {
              const missing = artifacts.filter((rel) => findContent(rel) === undefined);
              if (written.length === 0) {
                status = 'missing_artifacts';
                error = `子 Agent 未提交任何白名单产物: ${artifacts.join(', ')}`;
              } else if (missing.length > 0) {
                status = 'missing_artifacts';
                error = `部分产物缺失: ${missing.join(', ')}`;
              } else if (artifacts.every((rel) => before[rel]
                && before[rel] === outputDigests[rel])) {
                // 产物与原文件内容一致，属于合法产出，不判为错误
                status = 'ok';
                error = '';
              }
            }
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
              duration_ms: finishedAt.getTime() - startedAt.getTime(),
              artifacts_written: written,
              output_digests: outputDigests,
              status,
              attempts: attempt,
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
        }
        return {
          dispatch_id: dispatchId,
          scope: request.scope || '',
          stage: request.stage || '',
          status,
          attempts,
          evidence: evidenceAbs,
          error: error || null,
        };
      };

      // ---- 并发执行（信号量 ≤ max_concurrency） ----------------------------
      const results = new Array(requests.length);
      let cursor = 0;
      const runners = [];
      const laneCount = Math.max(1, Math.min(maxConcurrency, requests.length));
      for (let i = 0; i < laneCount; i += 1) {
        runners.push((async () => {
          while (cursor < requests.length) {
            const idx = cursor;
            cursor += 1;
            try {
              results[idx] = await dispatchOne(requests[idx]);
            } catch (err) {
              results[idx] = {
                dispatch_id: String((requests[idx] || {}).dispatch_id || ''),
                scope: String((requests[idx] || {}).scope || ''),
                stage: String((requests[idx] || {}).stage || ''),
                status: 'worker_error',
                attempts: 0,
                evidence: '',
                error: err && err.message ? err.message : String(err),
              };
            }
          }
        })());
      }
      await Promise.all(runners);

      const okCount = results.filter((r) => r.status === 'ok').length;
      const lines = results.map((r) => `- [${r.status === 'ok' ? 'ok' : 'FAIL'}] `
        + `${r.dispatch_id} (${r.scope}) → ${r.status}`
        + (r.error ? `: ${r.error}` : '')
        + (r.evidence ? ` ｜ 证据: ${r.evidence}` : ''));
      const report = [
        `=== qchain_dispatch 派发汇总（self_dir: ${selfDir}）===`,
        `派发单: ${results.length}，ok: ${okCount}，失败: ${results.length - okCount}（并发上限 ${maxConcurrency}）`,
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
        hint: 'status | run [--title <t>] [--profile <p>] [--format <both|pdf|html>] | resume [--title <t>] [--token <t>] | gates',
      },
      async handler(inv) {
        const raw = (inv.rawInput || '').trim();
        // 支持双引号包裹含空格的值（如 --title "我的 学案"）
        const parts = raw.match(/"[^"]*"|\S+/g) || [];
        const sub = (parts[0] || 'help').toLowerCase();
        const proj = resolveEngineRoot(engineRoot);
        const runnerScript = path.join(engineRoot, 'workflow', 'runner.py');

        const parseFlags = (tokens) => {
          const allowed = new Set(['--title', '--profile', '--format', '--token']);
          const opts = {};
          for (let i = 0; i < tokens.length; i++) {
            const tok = tokens[i];
            if (!tok.startsWith('--')) {
              return { error: `无法解析的参数: ${tok}（值必须跟随 --title/--profile/--format/--token 出现）` };
            }
            if (!allowed.has(tok)) {
              return { error: `不支持的选项: ${tok}（仅支持 --title/--profile/--format/--token）` };
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
          return { opts };
        };

        if (sub === 'help') {
          return {
            kind: 'success',
            text: [
              '📖 **题链流水线指令指南**',
              '- `/qchain status` : 查看当前题链流水线状态与检查点',
              '- `/qchain run [--title <标题>] [--profile <self_study|teaching|classroom，默认 self_study>] [--format <both|pdf|html>]` : 启动流水线',
              '- `/qchain resume [--title <标题>] [--profile <self_study|teaching|classroom，缺省沿用已确认值>] [--format <both|pdf|html>] [--token <Phase 0 令牌>]` : 从 Phase 0 断点恢复执行（令牌由真人用户提供）',
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
