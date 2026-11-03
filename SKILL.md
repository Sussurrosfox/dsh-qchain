# 题链流水线制作自学案 (LangGraph 改造版)

本技能是将原 `problem-shrink-courseware` 迁移至 **LangGraph 强类型状态机编排引擎** 的现代化工程改造版。

通过将控制平面外生化，消除了单一 Prompt 模式下主 Agent 承担繁重调度与状态记忆导致的上下文膨胀、状态遗忘与脆弱执行闭环。

---

## 一、 核心架构与三层解耦

```
+-----------------------------------------------------------------------------------+
| 1. 控制平面 (Control Plane) - 由 LangGraph 驱动                                     |
|    - 强类型状态 ShrinkCoursewareState: 状态转移、条件分支、时间回溯、检查点持久化      |
|    - 原生断点 interrupt(): 物理级挂起 Phase 0 用户确认，防跳步伪造                    |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 2. 算力与认知层 (Cognitive Workers) - 专职子 Agent                                  |
|    - Mapping / Section-Authoring / Review / Acceptance                            |
|    - 纯粹的无状态 Worker，仅关注特定小节切片的问题设计与数学推导                      |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 3. 安全与执行基底 (Execution Enclave) - scripts/ 保持只读                           |
|    - tikz_rasterize.py: 源文档侧 TikZ 预栅格化（tikzcd/tikzpicture → assets/*.png）|
|    - pdstate.py: 运行契约、独占 attempt、原子提升                                   |
|    - shuffle_options.py: 选择题选项随机打乱（确定性/幂等，防“所有答案都是 A”）      |
|    - estimate_minutes.py & check_budget.py: 确定性结构估算与课时预算硬约束          |
|    - check_*.py: 严格的质量门禁与 XeLaTeX 双源排版校验                               |
|    - deliver.py: 全证据门禁汇总与 release 原子发布（双向对账 confirmed-spec.json）   |
+-----------------------------------------------------------------------------------+
```

---

## 二、 目录结构概览

```
daily/
├── workflow/                       # LangGraph 核心编排引擎
│   ├── state.py                    # 强类型状态定义 ShrinkCoursewareState
│   ├── graph.py                    # StateGraph 组装 (节点注册、单向推进、条件路由)
│   ├── edges.py                    # 条件路由分流器
│   ├── runner.py                   # 统一 CLI 与 Python API 运行入口
│   ├── checkpoints/                # SQLite 持久化检查点 (跨进程断点恢复)
│   ├── nodes/                      # 流程执行节点 (Phase 0, T0~T4, 定点返修)
│   └── adapters/                   # 隔离适配器 (ScriptRunner, LLMWorker)
├── scripts/                        # 核心验证、构建与状态脚本 (build, check_*, deliver, pdstate, tikz_rasterize, redeliver, render_smoke)
├── templates/                      # LaTeX (大8开横版双栏) 与配置模板
├── prompts/                        # 子 Agent 提示词模板 (subagent_prompt, acceptance_prompt)
├── references/                     # 生产规范与 TeX 接口说明
├── examples/                       # 历史示例
└── tests/                          # 自动化测试套件
```

---

## 三、 使用方式

### 1. 命令行 CLI 模式

> **含 TikZ 源文档的前置处理**：源文档若含 `tikzcd`/`tikzpicture`，先运行
> `python scripts/tikz_rasterize.py --source <源.tex> --list` 勘察，再以
> `python scripts/tikz_rasterize.py --source <源.tex> --rewrite <源.raster.tex>` 一键栅格化为
> `<源目录>/assets/*.png` 并生成把 tikz 块替换为 `\includegraphics` 引用的重写源文件
> （源导言区宏定义与 `\usetikzlibrary` 依赖默认自动提取注入，`--preamble` 仍可追加兜底、
> `--no-auto-preamble` 可关），把流水线输入指向重写源文件，杜绝切片子 Agent 静默丢图。

```powershell
# 启动流水线
python workflow/runner.py --project . --run --title "极限与连续"

# 从断点恢复（--profile 缺省 self_study 无时长上限；教学模式显式指定 --profile teaching 或 classroom 保留课时上限约束，由 scripts/estimate_minutes.py 确定性结构估算并双向对账 .pd/confirmed-spec.json 签名）
python workflow/runner.py --project . --resume --title "极限与连续" --profile self_study

# 打印 Mermaid 状态机拓扑
python workflow/runner.py --visualize

# 查看流水线状态
python workflow/runner.py --project . --status
```

### 2. DSH 插件接入层（Agent 工具与 /qchain 指令）
- `qchain_pipeline` 工具：`run` / `resume` / `status` / `visualize`。**流水线每次启动都会在 Phase 0 强制挂起**；`resume` 必须携带真人提供的 Phase 0 令牌（`token` 参数透传 `--token`，令牌由流水线写入 `.langgraph/phase0_token.txt`，严禁 Agent 自行读取）。
- `qchain_dispatch` 工具：收到 `AGENT_DISPATCH` 中断后派发宿主子 Agent。中断载荷内的 `dispatch_args`（`{self_dir, batch_id}`）即**可直接执行的整批调用参数**——同批独立小节在一次 `qchain_dispatch` 调用内进入并发池，**一次 `batch_id` 取回全批派发单**，切勿按 `request_ref` 逐条串行派发（那会退化为串行、拖长关键路径）。`batch_id` 与 `request_ref` **互斥**；仅单条返修/补漏才用 `request_ref`。批次解析优先读取生产者为该批写下的 `.pd/dispatch-batches/<batch_id>.json` 索引（含 run/stage/请求清单/计划摘要），并核对计划摘要与 run 归属：错批次、过期、缺请求、批次号冲突一律显式报错（`BATCH_NOT_FOUND` / `STALE_OR_AMBIGUOUS_BATCH` / `BATCH_REQUEST_MISSING` / `BATCH_REQUEST_INCONSISTENT` / `BATCH_INDEX_CONFLICT`），**绝不回退为扫描全部历史 pending**。无参调用仅在唯一可确认的当前批次下允许（多个批次→`AMBIGUOUS_BATCH`，无→`BATCH_REQUIRED`）。重复派发幂等：`dispatch_id` 原子认领，已完成项核对收据与产物摘要后复用，并发撞上有效认领返回 `DISPATCH_BUSY`。
- `qchain_gate` 工具：门禁为**严格只读体检**，不改写工作区——`classroom_plan` 采用纯只读模式（`--check`），检测教案 Markdown 中的未替换占位符残留（如 `______`），绝不破坏性覆盖教师手改教案；`budget` 基于切片 AST 结构化估算引擎（`estimate_minutes.py`，基础阅读+2*小问+1*填空+证明加成）核验课时硬预算（`opening + closing + buffer + core + support_inclass <= lesson_minutes`，取消 20% 软顶豁免），无需作者手动标注 `planned_minutes`；`delivery` 与 `budget` 双向对账核验 `.pd/confirmed-spec.json` 的 SHA-256 签名防篡改；`shuffle` 以 `--verify` 只读核对（落盘 `.pd/reports/shuffle_options.json`），`delivery` 以 `--dry-run` 跑全部检查但不发布 `release/`，`compare` 为 `--diagnostic` 诊断模式（报告醒目标注"已跳过部分核对"）。`all` 汇总运行全部门禁（含 classroom_plan、check_html、review_pack、delivery dry-run 与 test_runtime）。
- `/qchain` 指令：支持 `status | run [--title] [--profile] [--format] | resume [--title] [--token] | gates | help`，未知子指令返回错误。

### 3. 运行自动化测试
```powershell
python -m unittest discover tests
```

---

## 四、 执行配置、1+K+R 预算与门禁分级 (v3.3.0)

### 1. 配置开关区分与旧检查点兼容
- **`content_policy`（内容取向）**：`faithful`（原样忠实）/ `authoring`（自由创作）；
- **`execution_profile`（执行图）**：`legacy`（默认旧图 `legacy-v1`，逐节双审）/ `faithful_fast`（轻量图 `faithful-fast-v1`）；两者正交，`content_policy: faithful` 不会自动切换执行图；
- **`agent_dispatch.profile`（并发调度档位）**：`interactive`(1) / `safe`(1) / `fast`(4) / `budget`(2)；
- **缺省与兼容保证**：`execution_profile` 缺省即为 `legacy`。旧会话及无元数据旧检查点严格按 `legacy-v1` 恢复推进，严禁静默换图；已冻结 `faithful_fast` 的检查点不可由旧版本（≤3.2.0）接管。

### 2. 1+K+R 派发预算模型与实测值
在 `faithful_fast` 下，冷缓存无返工调用预算满足：
$$\text{calls} = 1 + K + R$$
- **1** = T1 统一规划（一次 Agent 出小节计划、素材映射、验收要点三表）；
- **$K$** = 作者批次数（$K = \lceil \text{节数} / 2 \rceil$，单批超 16000 字符拆批）；
- **$R$** = 终审批次数（$R = \lceil \text{节数} / 2 \rceil$，每批至多 2 节）。
- **实测值**：1 节 = 3 次；2 节 = 3 次；3 节 = **5 次**（1+2+2，正式纠偏历史草案 "=4" 的误读）；长输入 3 节 = 6 次；对比 legacy 的 12 次。

### 3. 教学 Advisory 与刚性硬门禁
- **教学 Advisory（仅 faithful_fast 默认）**：`pedagogy_policy`（台阶数/密度）、`budget_policy`（课时预算）仅给出建议报告，不阻断流程（显式设 strict 可恢复阻断）；
- **刚性硬门禁（永远一票否决）**：严格数学公式校验（KaTeX/TeX AST）、来源忠实性（`check_fidelity`）、编号结构（`check_numbering`）、离线自足资源完整性与排版构建错误永远是硬门禁，不可被任何 profile 或 advisory 降级。
