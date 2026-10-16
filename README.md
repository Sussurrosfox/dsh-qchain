# qchain (题链)：问题驱动数学自学案流水线 (LangGraph 架构改造版)

本项目是针对原 `problem-shrink-courseware` 进行 **LangGraph 状态机重构** 的工业级改造版本，更名为 **题链流水线 (qchain)**。
仿照 `comp`（竞赛教研流水线）的三层解耦架构，将传统的单一 Prompt Skill 重构为由代码级状态机驱动的自动化与半自动化教研流水线。

**v2.0 起，问题主体由填空题升级为“选择题化填空”**：保留原有 `\fillin[答案]{宽度}` 下划线填空框架，每个空之后追加 `（选项1/选项2/选项3）` 形式的简短选项，学生可直接圈选选项完成填空（选项数量、干扰项质量由 check_numbering.py 门禁校验）。

---

## 一、 改造背景与核心收益

原 `problem-shrink-courseware` 拥有极其完善的数学规范、课时预算控制、单源双输出宏系统与 XeLaTeX 排版门禁，但由于采用主 Agent 纯 Prompt 交互模式承载，导致**主 Agent 必须兼任状态调度器、命令运行者、文件监视者与门禁协调者**，极易引发上下文膨胀、状态遗忘与死循环。

在 `daily` 中，我们完成了**三层解耦与控制权外生化**：

1. **控制平面 (Control Plane) — LangGraph StateGraph**
   - 强类型状态机 `ShrinkCoursewareState`：覆盖从 Phase 0 用户确认、T0 脚手架初始化、T1 三张映射表规划、T2 逐节切片生产、T3 XeLaTeX 双源编译排版、到 T4 教学独立验收与原子交付的全流程。
   - 物理级人机断点 `interrupt()`：在 Phase 0 收集确认学案主标题、交付模式与对话层开关，杜绝大模型随意跳步伪造确认。
   - 检查点持久化 `SqliteSaver`：断点与执行快照跨进程落盘，随时支持进程退出、重启与多端恢复推进。

2. **算力与认知层 (Cognitive Workers) — 专职子 Agent**
   - 严格执行“一节一写作者”边界与独占 attempt 契约。
   - 认知派发统一抽象为 `*_request`（记录修改前摘要快照）与 `*_await`（比对快照，杜绝无变更空转）。

3. **安全与执行基底 (Execution Enclave) — 只读工具基底**
   - `pdstate.py`：运行契约、独占 attempt、原子提升；
   - `shuffle_options.py`：选择题选项随机打乱（确定性/幂等，防“所有答案都是 A”）；
   - `estimate_minutes.py` & `check_budget.py`：确定性结构耗时估算引擎（基于切片小问与填空）与课时预算刚性硬约束（取消 20% 软顶豁免）；
   - `gen_classroom_plan.py`：教案门禁纯只读体检（`--check`），检测教案占位符残留，绝不破坏性覆盖教师手改教案；
   - `check_numbering.py` & `check_dialogue.py`：结构合规与防泄露硬门禁；
   - `build.py`：XeLaTeX 双遍编译与排版告警校验（编译前先校验打乱机制生效）；
   - `compare_versions.py`：学生版答案与教师标记防泄露比对；
   - `deliver.py`：schema_version 2 全证据门禁汇总与原子发布到 `release/`（双向核验 `.pd/confirmed-spec.json` 的 SHA-256 签名）。

---

## 二、 目录架构

```
daily/
├── SKILL.md                        # 高阶技能操作指南
├── README.md                       # 本白皮书
├── USER_GUIDE.md                   # 详细操作指南
├── workflow/                       # 【核心】LangGraph 编排引擎
│   ├── state.py                    # 强类型状态 ShrinkCoursewareState 与 GateReport
│   ├── graph.py                    # 拓扑组装 (StateGraph 节点与条件路由)
│   ├── edges.py                    # 条件边分流函数
│   ├── runner.py                   # 统一运行接口与 CLI
│   ├── adapters/                   # 隔离适配器 (ScriptRunner, LLMWorker, WorkerRegistry)
│   ├── checkpoints/                # 跨进程持久化 SQLite 检查点
│   └── nodes/                      # 执行节点
│       ├── human_nodes.py          # Phase 0 人机断点
│       ├── init_nodes.py           # T0 脚手架初始化
│       ├── mapping_nodes.py        # T1 三张映射表与材料索引
│       ├── section_nodes.py        # T2 逐节切片任务与 main.tex 装配
│       ├── shuffle_nodes.py        # T2 末段：全部题目制成后选择题选项随机打乱
│       ├── build_nodes.py          # T3 编号/对话检查与 XeLaTeX 双源编译
│       ├── delivery_nodes.py       # T4 审读包、教学验收与 deliver 原子发布
│       ├── repair_nodes.py         # 定点小节返修回路
│       └── agent_nodes.py          # 认知工位调度与快照比对
├── scripts/                        # 原 13+ 核心构建与检查脚本
├── templates/                      # LaTeX 模板、YAML 配置与课堂文档
├── prompts/                        # 子 Agent 提示词模板
├── references/                     # 生产规范与宏接口协议
├── examples/                       # 历史案例
└── tests/                          # 单元与集成测试套件
```

---

## 三、 使用指引

### 1. 命令行运行
```powershell
# 启动流水线（注意：无论是否携带 --title，都会在 Phase 0 强制挂起等待真人确认）
python workflow/runner.py --project . --run --title "导数与微分自学案"

# 从断点恢复推进（必须携带真人从 .langgraph/phase0_token.txt 读取的令牌；
# 该文件严禁 Agent 自行读取）
python workflow/runner.py --project . --resume --title "导数与微分自学案" --token <真人提供的令牌>

# 查看当前状态
python workflow/runner.py --project . --status

# 导出 Mermaid 拓扑图
python workflow/runner.py --visualize
```

**交付模式与课时预算机制**：
- `self_study`（自主学习模式，默认）：侧重循序渐进探究，**不再设总时长上限**，自动启用自学规划红线；核心推导长链题通过 `check_pedagogy` 门禁（强制拆解为至少 4~7 个连续小问台阶）及确定性结构估算确保认知密度下限，严禁为缩短篇幅省略推导台阶。
- `teaching` / `classroom`（课堂教学模式）：保留课时时间预算刚性上限约束（默认 90 分钟，公式 `opening + closing + buffer + core + support_inclass <= lesson_minutes`，取消 20% 软顶豁免）。课时预算采用确定性结构估算（由 `scripts/estimate_minutes.py` 基于小问台阶与填空动作自动计算，非人工编数自报），真人确认的时长与配置落盘为 `.pd/confirmed-spec.json`（带有 SHA-256 签名），在门禁与交付脚本间实现双向防篡改对账。

**门禁工具说明 (`qchain_gate`)**：
- `classroom_plan` 门禁为严格只读体检模式（`gen_classroom_plan.py --check`），扫描检测教案 Markdown 中的未替换占位符残留（如 `______`），检测到残留即阻断，不再擅自改写工作区或覆盖教师手改教案。
- `shuffle` 走 `--verify` 只读核对；`delivery` 走 `--dry-run` 只检查不发布；`compare` 走 `--diagnostic` 诊断模式。

**安全语义（v3.1 加固）**：
- Phase 0 断点不可跳过：恢复载荷必须携带匹配令牌，令牌不匹配则保持挂起（伪造确认被拒绝）。
- `--run` 拒绝复用已有检查点的 thread_id（防跨任务状态串线）；确需覆盖时显式加 `--reuse-thread`。
- `--resume` 校验检查点绑定的 self 工作区与本次一致，不一致拒绝恢复。
- 检查点采用 JSON/msgpack 限定 serde，拒绝反序列化 pickle 数据（防投毒检查点 RCE）。
- `deliver.py --dry-run` 只校验不发布；`--skip-checks` 需 `QCHAIN_ALLOW_SKIP_CHECKS=1` 显式确认。
- 交付格式缺失产物（如 both 缺 PDF）为硬失败，不再静默降级交付范围。

### 2. 运行测试
```powershell
python -m unittest tests/test_langgraph_pipeline.py
```
