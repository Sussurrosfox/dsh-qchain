import { execFile } from 'node:child_process';
import * as path from 'node:path';
import * as fs from 'node:fs';
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

function runPython(args, cwd, timeoutMs = 180000, pythonBin = 'python', extraEnv = {}) {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      ...extraEnv,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    };
    execFile(
      pythonBin,
      args,
      { cwd, env, timeout: timeoutMs, maxBuffer: 10 * 1024 * 1024, encoding: 'utf-8' },
      (err, stdout, stderr) => {
        resolve({
          success: !err,
          code: err ? (err.code ?? 1) : 0,
          stdout: stdout ? stdout.trim() : '',
          stderr: stderr ? stderr.trim() : '',
          error: err ? err.message : null,
        });
      }
    );
  });
}

export const inject = ['tools'];

export function apply(ctx, config = {}) {
  const pythonBin = config.pythonPath || 'python';
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
      const targetInput = args.input_path ? path.resolve(args.input_path) : '';
      let proj = args.project_root ? path.resolve(args.project_root) : '';
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
    description: '运行题链流水线质量与安全门禁校验（pedagogy, numbering, budget, classroom_plan, shuffle, build, check_html, compare, dialogue, review_pack, delivery, test_runtime 或 all）。',
    parameters: {
      type: 'object',
      properties: {
        gate: {
          type: 'string',
          enum: [
            'all',
            'pedagogy',
            'numbering',
            'budget',
            'classroom_plan',
            'shuffle',
            'build',
            'check_html',
            'compare',
            'dialogue',
            'review_pack',
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
      // 强制规范化待校验的目标工作区，确保门禁始终指向 self 文件夹
      let rawTarget = args.target_dir ? path.resolve(args.target_dir) : '';
      if (!rawTarget) {
        if (fs.existsSync(path.join(process.cwd(), 'self'))) {
          rawTarget = path.join(process.cwd(), 'self');
        } else if (fs.existsSync(path.join(proj, 'self'))) {
          rawTarget = path.join(proj, 'self');
        } else {
          rawTarget = path.join(process.cwd(), 'self');
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
        delivery: () => runScript(
          '交付门禁汇总（dry-run，只检查不发布 release/） (deliver.py)',
          'scripts/deliver.py', [targetDir, '--dry-run'],
          '⚠ 干跑模式（--dry-run）：执行全部交付检查但不真实发布 release/。'),
        test_runtime: () => runScript('运行时契约自动化套件 (test_runtime.py)', 'scripts/test_runtime.py', []),
      };

      if (args.gate === 'all') {
        await gateHandlers.pedagogy();
        await gateHandlers.numbering();
        await gateHandlers.budget();
        await gateHandlers.classroom_plan();
        await gateHandlers.dialogue();
        await gateHandlers.shuffle();
        await gateHandlers.build();
        await gateHandlers.check_html();
        await gateHandlers.compare();
        await gateHandlers.review_pack();
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
      ].join('\n'),
    });
  }
}
