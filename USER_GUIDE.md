# 问题驱动自学案流水线 (daily) 用户操作指南

本手册指导教师与教研人员如何使用 `daily` 流水线制作高质量、问题驱动的大8开横版双栏自学案（单源双输出：学生版学案与教师版讲义）。

---

## 一、 快速开始

流水线支持命令行 (CLI) 与 Agent 工具调用。

> **Agent 接入层（DSH 插件）说明**：
> - `qchain_pipeline` 工具的 `resume` 必须携带真人提供的 Phase 0 令牌（`token` 参数）；令牌由流水线写入 `.langgraph/phase0_token.txt`，严禁 Agent 自行读取。
> - `qchain_gate` 工具为**严格只读体检**：`classroom_plan` 采用纯只读模式（`--check`），检测教案中的未替换占位符残留（如 `______`），不再破坏性覆盖教师的手改教案；`shuffle` 走 `--verify` 不改写切片源文件，`delivery` 走 `--dry-run` 只检查不发布 `release/`，`compare` 为 `--diagnostic` 诊断模式（报告会醒目标注"已跳过部分核对"，不得按完整通过解读）。
> - `qchain_dispatch` 的 `request_ref` 是**单请求返修入口**，不是整批复原入口：逐单调用时每次并发槽只有 1 个请求（`laneCount=1`），同批 N 个请求会被逐条串行，白丢并行能力。整批复原一律改用 `batch_id`（详见本节第 5 小节）。

### 1. 启动流水线
在 Windows 终端中执行：
```powershell
python workflow/runner.py --project . --input <原材料路径> --run --title "学案主标题"
```
流水线将完成工作区脚手架初始化，并在 Phase 0 物理人机断点处挂起。

### 2. 设定自学案参数并从断点恢复 (Phase 0)
向流水线提交确认的学案主标题、交付模式与输出格式并恢复执行：
```powershell
python workflow/runner.py --project . --resume --title "学案主标题" --profile self_study --format both
```
- **交付模式 (`--profile`)**（注意：此为 CLI 交付模式参数，与配置中的 `execution_profile` 执行图及 `agent_dispatch.profile` 并发档位正交，切勿混淆）：
  - `self_study`：自主学习模式（**默认模式**），侧重循序渐进探究，**不再设总时长上限**。在 `legacy` 模式下受 3 节精炼体系与连续小问台阶刚性约束；在 `faithful_fast`（3.3.0）模式下不强制固定三节限制，教学法偏好与课时预算转为 advisory 建议（不阻断执行），但严格数学、忠实性与编号结构永远是硬门禁；
  - `teaching`（或 `classroom`）：课堂教学模式，保留课时时间预算刚性上限约束（默认 90 分钟，公式 `opening + closing + buffer + core + support_inclass <= lesson_minutes`，取消 20% 软顶豁免）。课时预算采用确定性结构估算（由 `scripts/estimate_minutes.py` 基于小问台阶与填空动作自动计算，非人工编数自报），真人确认的时长与配置落盘为 `.pd/confirmed-spec.json`（带有 SHA-256 签名），在门禁与交付脚本间实现双向防篡改对账。
- **输出格式 (`--format`)**：
  - `both`：同时输出 XeLaTeX PDF 与 HTML 卡片自学案；
  - `pdf`：仅输出 XeLaTeX PDF；
  - `html`：仅输出 HTML 卡片自学案。

### 3. 查看流水线状态
```powershell
python workflow/runner.py --project . --status
```

### 4. 含 TikZ 源文档的前置处理

源文档若含 `tikzcd` / `tikzpicture` 环境，切片子 Agent 无法直接处理这类图形代码，存在静默丢图风险。必须在 `run` 之前做**源文档侧预栅格化**：

1. **勘察**：列出源文档中的全部 TikZ 块（环境名、行号、字符数），不编译：
   ```powershell
   python scripts/tikz_rasterize.py --source <源文档.tex> --list --json
   ```
2. **栅格化**：逐块编译为 PNG（默认输出到 `<源文档目录>/assets/`，命名 `<主名>-NN.png`，DPI 默认 300）：
   ```powershell
   python scripts/tikz_rasterize.py --source <源文档.tex>
   ```
   若个别块用到项目特有宏或额外 TikZ 库（如 `calc`）导致编译失败，把兜底定义写入一个文件并用 `--preamble` 注入后重跑（失败块会在报告中附 log 尾部 30 行，可用 `--only NN` 只重跑指定块）：
   ```powershell
   # preamble.tex 内容示例：\providecommand{\quotgrp}[2]{#1/#2} 与 \usetikzlibrary{calc}
   python scripts/tikz_rasterize.py --source <源文档.tex> --only 8 --preamble preamble.tex
   ```
   全部成功退出码为 0，任一失败为 2；`--json` 输出机器可读报告。
3. **替换引用**：人工或脚本把源文档中的 tikz 块替换为 `\includegraphics{assets/<主名>-NN.png}`（tex2cards 对 `\includegraphics` 已有成熟支持，流水线无需其他改动）。
4. **正常跑流水线**：按本节第 1 步启动即可。

### 5. AGENT_DISPATCH 中断的批量恢复（硬约束）

流水线在 T1/T2/T4 节点以 `AGENT_DISPATCH` 中断挂起时，载荷会给出 `.pd/agent-requests/*.json` 派发单。恢复方式是**硬约束**，不是偏好：

| 场景 | 调用方式 | 并发 |
|---|---|---|
| **整批复原（默认、绝大多数情况）** | `qchain_dispatch({ batch_id: "<批次 id>" })` | 同批 N 个请求进入同一并发池，`laneCount = min(并发上限, N)` |
| 单个请求返修（只重派失败的那一个） | `qchain_dispatch({ request_ref: ".pd/agent-requests/<id>.json" })` | `laneCount = 1`，必然串行 |

- **批次 id 从哪来**：`.pd/dispatch-batches/<batch_id>.json` 索引文件（内含 `request_ids`、`run_id`、`plan_rel`、`plan_digest`），或直接由中断载荷的批次字段给出。批量入口据此索引解析，不会去扫 `.pd/agent-requests` 下的历史 pending。
- **为什么不能逐单恢复**：每个批次（T2 写作、T2 语义审读、T4 逐节验收、T4 合并终审）内的请求彼此独立，本来就该并行；逐条 `request_ref` 会把「一批」拆成 N 次串行调用，墙钟时间放大近 N 倍，且每轮都要重新等待人机交互。只有确实只需要重做失败的那一节时才用 `request_ref`。
- **宿主侧保护**：通过 `request_ref` 调用时，若该派发单经 `.pd/dispatch-batches/` 索引确认属于某个多请求批次，工具报告会追加一条 `💡 该派发单属于批次 …` 提示，提醒改用 `batch_id`。该提示宁可不显示也不猜测（命中歧义、`run_id` 不一致、批次只有一个请求时均不提示）。
- **批次必须一次派发完整**：同一批次的全部 `dispatch_id` 都为 `ok` 之后，才能 `qchain_pipeline` 以 `action: "resume"` 恢复执行；缺任一即为半途状态。

---

## 二、 执行配置、预算模型与门禁分级 (v3.3.0)

### 1. 四维配置与开关区分

流水线包含四个维度的配置开关，职责正交，**绝对不能混用**：

| 开关 | 典型取值 | 职责与决定内容 | 配置或调用位置 |
|---|---|---|---|
| `content_policy` | `faithful` / `authoring` | **内容取向**：原料是否原样忠实呈现（faithful）还是允许增删与重构创作（authoring） | `config.yaml` 扁平键；Phase 0 冻结进 `intent-contract.json` |
| `execution_profile` | `legacy` / `faithful_fast` | **执行图版本**：编译哪张 StateGraph（`legacy-v1` 还是 `faithful-fast-v1`） | `config.yaml` 扁平键；Phase 0 冻结为执行元数据 |
| `agent_dispatch.profile` | `interactive` / `safe` / `fast` / `budget` | **并发调度档位**：同批最大并发请求数（`fast` 目标 4，`safe`/`interactive` 目标 1） | `config.yaml` 的 `agent_dispatch:` 块，或扁平 `agent_max_concurrency` |
| `--profile` (CLI) | `self_study` / `teaching` / `classroom` | **交付模式**：是否设总时长硬上限及课堂教案生成规则 | CLI 命令行参数 `--profile` |

**缺省行为与旧会话兼容**：
- `execution_profile` 缺省即为 `legacy`。未声明该字段的新旧项目、以及所有历史检查点，一律继续沿用 `legacy-v1` 图执行，**绝不做静默换图迁移**。
- 旧检查点恢复逻辑：恢复流程优先从检查点快照读取冻结的 `graph_version`。无版本元数据的旧检查点按 `legacy-v1` 恢复；已冻结 `faithful_fast` 的检查点无法由旧代码（≤3.2.0）接管。

### 2. 1+K+R 派发预算模型与实测值

在 `faithful_fast` 模式下，冷缓存零返修的子 Agent 派发预算满足：
$$\text{calls} = 1 + K + R$$
- **1** = T1 统一规划：一次 Agent 调用直接产出小节计划、素材映射、验收要点三份映射表；
- **$K$** = 作者批次数：$K = \lceil \text{节数} / \text{writer\_batch\_max\_sections} \rceil$（默认每批最多 2 个相邻节，单批超 16000 字符自动拆批）；
- **$R$** = 终审轮次：$R = \lceil \text{节数} / 2 \rceil$（合并终审，每批最多审读 2 节）。

**实测值与纠偏说明**（消除历史 “=4” 误读）：
- 1 节：$1 + 1 + 1 = 3$ 次派发；
- 2 节：$1 + 1 + 1 = 3$ 次派发；
- 3 节：$1 + 2 + 2 = \mathbf{5}$ 次派发（T1 一次 + 两个作者批 + 两次终审批；历史文档草案中“三节=4”隐含了终审单批覆盖 3 节的错误假设，已在 3.3.0 正式纠偏为实测值 **5**）；
- 长输入 3 节（超字符拆批）：$1 + 3 + 2 = 6$ 次派发；
- 对照 legacy 模式：3 节共需 $3 + 3N = 12$ 次派发（节省超过 58% 派发调用）。

### 3. 教学 Advisory 与刚性硬门禁分级

流水线将门禁严格划分为**教学偏好建议 (Advisory)** 与**刚性一票否决门禁 (Hard Gates)**：

- **教学建议项 (Advisory，仅在 faithful_fast 下生效)**：
  - `pedagogy_policy: advisory`（自学小问台阶数、问题密度等教学偏好仅在报告中给建议，不阻断流程）；
  - `budget_policy: advisory`（课时时间预算仅作估算报告，不阻断流程）；
  - `require_classroom_plan: false`（不强求预先存在课堂教案）。
  - 注：若显式配置 `pedagogy_policy: strict` 或 `budget_policy: strict`，则恢复阻断行为。
- **刚性一票否决门禁 (Hard Gates，永远不可降级，任何模式均一票否决)**：
  - **严格数学校验 (`validate_math.cjs` / KaTeX / TeX 公式解析)**：公式宏解析错误或渲染异常必阻断；
  - **来源忠实性门禁 (`check_fidelity`)**：原文主体资产守恒、关键推导不漏项；
  - **结构与编号门禁 (`check_numbering`)**：`% qid:` 唯一性、环境闭合与槽位配平；
  - **必需资源完整性**：模板、字体、KaTeX 离线包、`math-config.json` 缺一不可；
  - **构建与渲染门禁 (`build.py` / `check_render.py`)**：编译严重排版告警、丢失字符一律阻断。

---

## 三、 核心门禁体检中心 (Gate Inspector)

切换至 **“🛡️ 质量门禁体检中心”** 选项卡，点击“刷新门禁报告”，可全景查看各项硬门禁的实时状态：

1. **编号与逐题关联门禁 (`check_numbering`)**
   - 验证每道题一级 `\item` 前是否存在全章唯一 `% qid:`；
   - 验证每题是否恰好对应一个 `solution` 和一个 `teacherNote`；
   - 检查切片内部严禁重置计数器或版本开关宏。

2. **课堂对话层防泄露门禁 (`check_dialogue`)**
   - 验证启用对话层的节是否包含合法的 `sectiondialogue` 与 `exampledialogue`；
   - 验证对话中是否存在答案泄露（实质提示必须包裹在 `\dlgteacher{...}` 中）。

3. **选择题选项随机打乱门禁 (`shuffle_options`)**
   - 在所有题目制成并装配 `main.tex` 之后执行（流水线 `t2_shuffle_options` 节点），
     对全部 `\fillin[答案]{宽度}[选项...]` 的选项做确定性随机重排（幂等，重复执行结果不变）；
   - 修复 LLM 切片专员“参考答案恒写在选项首位”的惯常写法——否则学生版 PDF 中
     每个选择题排在第一位（下划线之后）的候选项永远正确（“所有答案都是 A”）；
   - 报告 `.pd/reports/shuffle_options.json` 记录逐槽位打乱前后顺序与答案位置分布，
     并绑定打乱后的受审输入摘要（deliver 核对旧证据不得冒充当前结果）；
   - `build.py` 编译前会做源级复核：任何未按机制排序的选项都不得进入编译。

4. **确定性结构估算与课时预算门禁 (`check_budget`)**
   - 由 `scripts/estimate_minutes.py` 提取切片小问台阶（sub_items）与填空动作（fillins）确定性自动估算耗时（`.pd/reports/estimate.json`）；
   - `check_budget.py` 进行刚性硬约束判定：`opening + closing + buffer + core + support_inclass <= lesson_minutes`，取消 20% 软顶豁免，超支阻断交付（`.pd/reports/check_budget.json`）。

5. **教案占位符只读体检门禁 (`classroom_plan`)**
   - `qchain_gate classroom_plan` 采用严格只读体检模式（`gen_classroom_plan.py --check`）；
   - 扫描 `self/classroom-plan.md` 中的未替换占位符残留（如 `______`），检测到残留即报警，绝不破坏性覆写教师已手动修改优化的教案正文。

6. **XeLaTeX 双源编译排版门禁 (`build.py`)**
   - 自动执行双遍编译，生成大8开横版双栏 `student.pdf` 与 `teacher.pdf`；
   - 严格拦截未定义引用、`Overfull \hbox > 10pt`、栏数自动变更及严重排版警告。

7. **双版防泄露比对门禁 (`compare_versions`)**
   - 通过文本与宏语法扫描，确保学生版学案绝对不包含【解】、【证明】、参考解答与教师专用批注。

8. **独立教学验收门禁 (`pedagogical_acceptance`)**
   - 站在学生实际可见内容视角，审读知识依赖是否闭合、是否存在隐性偷步。

9. **最终原子交付门禁 (`deliver.py`)**
   - 先跑随机打乱（幂等）再跑编号/对话/构建/比对，核验 `.pd/confirmed-spec.json` 的 SHA-256 签名防篡改对账，汇总全部门禁证据，原子发布至 `release/`，生成具备密码学数字摘要的 `manifest.json`。
   - **渲染冒烟硬门槛（复盘 §4.4.1）**：PDF 交付前自动执行 `render_smoke.py` 并校验冒烟证据（报告 pass 且截图 ≥1 张）；缺证据一律拒绝发布。`--skip-smoke` 可显式跳过（不推荐，跳过事实记入交付说明）。

---

## 四、 双源 PDF 下载与查看

切换至 **“📦 双源 PDF 成果与交付”** 选项卡：
- **学生版学案 (`student.pdf`)**：留有充足的书写空白框、姓名学号栏与微知识点卡片，隐藏解题步骤；
- **教师版讲义 (`teacher.pdf`)**：紧跟题目呈现彩色解答、教学要点、易错分析与评分标准；
- 可直接在网页端点击下载或外链预览。

---

## 五、 命令行操作 (CLI) 快速查阅

| 命令 | 描述 |
|---|---|
| `python workflow/runner.py --run --title "学案标题"` | 启动教研流水线 |
| `python workflow/runner.py --resume --title "学案标题"` | 提交参数并从断点恢复 |
| `python workflow/runner.py --status` | 查询流水线状态与检查点 |
| `python workflow/runner.py --visualize` | 打印状态机 Mermaid 拓扑图 |
| `python scripts/tikz_rasterize.py --source <源.tex> --list --json` | 勘察源文档 TikZ 块（env/行号/字符数） |
| `python scripts/tikz_rasterize.py --source <源.tex>` | 预栅格化 TikZ 块为 PNG（`<源目录>/assets/`） |
| `python scripts/tikz_rasterize.py --source <源.tex> --rewrite <源.raster.tex>` | 预栅格化 TikZ 块并生成重写源文件（tikz 块 → `\includegraphics`，块外逐字节不变；失败即闭） |
| `python scripts/redeliver.py self/` | 一键重交付（build → review-pack（含冒烟自动挂接） → 证据哈希对齐 → deliver（含冒烟硬门槛）） |
| `python scripts/render_smoke.py self/` | 手动双模视觉冒烟（PNG 落 .pd/review/smoke/；`--pdfs student` 可限定范围；审读包已自动调用） |
| `python scripts/estimate_minutes.py self/` | 运行切片确定性结构耗时估算（提取小问/填空） |
| `python scripts/check_budget.py self/` | 校验课时预算硬约束（取消 20% 软顶豁免） |
| `python scripts/gen_classroom_plan.py self/ --check` | 教案纯只读体检（检测占位符残留，不覆盖手改教案） |
| `python scripts/build.py self` | 仅重建双版 PDF 并验收（需先执行打乱） |
| `python scripts/shuffle_options.py self` | 选择题选项随机打乱（幂等；也可 `--verify` 只读复核） |
| `python -m unittest tests/test_langgraph_pipeline.py` | 运行端到端流水线单元测试 |
