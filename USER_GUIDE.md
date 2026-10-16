# 问题驱动自学案流水线 (daily) 用户操作指南

本手册指导教师与教研人员如何使用 `daily` 流水线制作高质量、问题驱动的大8开横版双栏自学案（单源双输出：学生版学案与教师版讲义）。

---

## 一、 快速开始

流水线支持命令行 (CLI) 与 Agent 工具调用。

> **Agent 接入层（DSH 插件）说明**：
> - `qchain_pipeline` 工具的 `resume` 必须携带真人提供的 Phase 0 令牌（`token` 参数）；令牌由流水线写入 `.langgraph/phase0_token.txt`，严禁 Agent 自行读取。
> - `qchain_gate` 工具为**严格只读体检**：`classroom_plan` 采用纯只读模式（`--check`），检测教案中的未替换占位符残留（如 `______`），不再破坏性覆盖教师的手改教案；`shuffle` 走 `--verify` 不改写切片源文件，`delivery` 走 `--dry-run` 只检查不发布 `release/`，`compare` 为 `--diagnostic` 诊断模式（报告会醒目标注"已跳过部分核对"，不得按完整通过解读）。

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
- **交付模式 (`--profile`)**：
  - `self_study`：自主学习模式（**默认模式**），侧重循序渐进探究，**不再设总时长上限**，自动启用自学规划红线（禁习题选讲独立成节、3 节精炼体系、核心推导长链题通过 `check_pedagogy` 门禁强制拆解为至少 4~7 个连续小问台阶及确定性结构估算确保认知密度下限，严禁为缩短篇幅省略推导台阶）；
  - `teaching`（或 `classroom`）：课堂教学模式，保留课时时间预算刚性上限约束（默认 90 分钟，公式 `opening + closing + buffer + core + support_inclass <= lesson_minutes`，取消 20% 软顶豁免）。课时预算采用确定性结构估算（由 `scripts/estimate_minutes.py` 基于小问台阶与填空动作自动计算，非人工编数自报），真人确认的时长与配置落盘为 `.pd/confirmed-spec.json`（带有 SHA-256 签名），在门禁与交付脚本间实现双向防篡改对账。
- **输出格式 (`--format`)**：
  - `both`：同时输出 XeLaTeX PDF 与 HTML 卡片自学案；
  - `pdf`：仅输出 XeLaTeX PDF；
  - `html`：仅输出 HTML 卡片自学案。

### 3. 查看流水线状态
```powershell
python workflow/runner.py --project . --status
```

---

## 二、 核心门禁体检中心 (Gate Inspector)

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

---

## 三、 双源 PDF 下载与查看

切换至 **“📦 双源 PDF 成果与交付”** 选项卡：
- **学生版学案 (`student.pdf`)**：留有充足的书写空白框、姓名学号栏与微知识点卡片，隐藏解题步骤；
- **教师版讲义 (`teacher.pdf`)**：紧跟题目呈现彩色解答、教学要点、易错分析与评分标准；
- 可直接在网页端点击下载或外链预览。

---

## 四、 命令行操作 (CLI) 快速查阅

| 命令 | 描述 |
|---|---|
| `python workflow/runner.py --run --title "学案标题"` | 启动教研流水线 |
| `python workflow/runner.py --resume --title "学案标题"` | 提交参数并从断点恢复 |
| `python workflow/runner.py --status` | 查询流水线状态与检查点 |
| `python workflow/runner.py --visualize` | 打印状态机 Mermaid 拓扑图 |
| `python scripts/estimate_minutes.py self/` | 运行切片确定性结构耗时估算（提取小问/填空） |
| `python scripts/check_budget.py self/` | 校验课时预算硬约束（取消 20% 软顶豁免） |
| `python scripts/gen_classroom_plan.py self/ --check` | 教案纯只读体检（检测占位符残留，不覆盖手改教案） |
| `python scripts/build.py self` | 仅重建双版 PDF 并验收（需先执行打乱） |
| `python scripts/shuffle_options.py self` | 选择题选项随机打乱（幂等；也可 `--verify` 只读复核） |
| `python -m unittest tests/test_langgraph_pipeline.py` | 运行端到端流水线单元测试 |
