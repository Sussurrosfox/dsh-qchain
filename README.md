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
   - `execution_profile=legacy`（缺省，3.3.0 之前的全部行为）：严格执行**“一节一写作者”**边界与独占 attempt 契约，逐节双审。
   - `execution_profile=faithful_fast`（3.3.0 新增）：一个作者可写**最多 2 个相邻节**（`writer_batch_max_sections=2`），
     T1 一次调用产出三份映射，终审合并为一次（`1+K+R` 派发预算）。教学偏好与课时预算转为 advisory，但数学/忠实性/必需资源仍是硬门禁。
   - 认知派发统一抽象为 `*_request`（记录修改前摘要快照）与 `*_await`（比对快照，杜绝无变更空转）；
     并发档位（`interactive`/`safe`/`fast`/`budget`）与执行图**分开配置**，详见 [docs/faithful-fast-migration.md](docs/faithful-fast-migration.md)。

3. **安全与执行基底 (Execution Enclave) — 只读工具基底**
   - `tikz_rasterize.py`：源文档侧 TikZ 预栅格化（run 之前把源文档中的 tikzcd/tikzpicture 逐块编译为 PNG 进 assets/，切片改以 `\includegraphics` 引用（`--rewrite OUT.tex` 一键生成重写源文件：块替换为引用、块外逐字节不变、失败即闭不写文件；源导言区宏定义与 `\usetikzlibrary` 依赖默认自动提取注入，`--no-auto-preamble` 可关），杜绝切片子 Agent 静默丢图）；
   - `pdstate.py`：运行契约、独占 attempt、原子提升；
   - `shuffle_options.py`：选择题选项随机打乱（确定性/幂等，防“所有答案都是 A”）；
   - `estimate_minutes.py` & `check_budget.py`：确定性结构耗时估算引擎（基于切片小问与填空）与课时预算刚性硬约束（取消 20% 软顶豁免）；
   - `gen_classroom_plan.py`：教案门禁纯只读体检（`--check`），检测教案占位符残留，绝不破坏性覆盖教师手改教案；
   - `check_numbering.py` & `check_dialogue.py`：结构合规与防泄露硬门禁；
   - `check_fidelity.py`（1.1）：来源忠实性门禁——升级后新增**资产守恒对账**：核对源文档图/表/TikZ/公式环境数量与切片保留数量，gap 清单非空或缺失即拦截；
   - `build.py`：XeLaTeX 双遍编译与排版告警校验（编译前先校验打乱机制生效）；
   - `check_render.py`（1.1）：渲染质量门禁（§19）——结构门禁只查“卡片数/槽位数对得上”，本门禁查“渲染出来的字是不是字”：HTML 双重转义、center/tabular 压平残留、.qc-math 段 $ 不成对、KaTeX 宏白名单、选项汉字守恒（源级 \fillin 汉字必须出现在 HTML 按钮与 PDF 文本层中）、KaTeX 本地自足（禁外网 CDN），以及 PDF 日志 Missing character、U+FFFD 不可见字形、空选项组；1.1 起新增 H7 槽位守恒、CJK 缺字聚合与 tikz 泄漏检测（切片残留的 tikzcd/tikzpicture 环境直接拦截）。须在 build.py 之后运行，一票否决；
   - `render_smoke.py`：构建后双模（PDF/HTML）视觉冒烟——渲染 student/teacher 关键页（首页 + 首个含选项组的页）截图 PNG 落 `.pd/review/smoke/`（`--pdfs` 可选 PDF 子集）；`make_review_pack.py` 自动调用并把结果记入审读包 manifest 的 smoke 区（最佳努力，不阻断审读包）；`deliver.py` 将其作为硬门槛——PDF 交付缺冒烟证据（报告非 pass 或零截图）一律拒绝发布
   - `compare_versions.py`：学生版答案与教师标记防泄露比对；
   - `redeliver.py`：一键重交付——串联 build → review-pack → 证据哈希对齐 → deliver，返修后一条命令重走交付链；
   - `deliver.py`：schema_version 2 全证据门禁汇总与原子发布到 `release/`（双向核验 `.pd/confirmed-spec.json` 的 SHA-256 签名）；含渲染冒烟硬门槛（`--skip-smoke` 可显式跳过，跳过事实须记入交付说明，不推荐）

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

**执行配置（3.3.0）：`content_policy` 与 `execution_profile` 是两个开关**
```yaml
# <self>/config.yaml
content_policy: faithful          # faithful | authoring —— 内容取向（是否原样忠实呈现）
execution_profile: faithful_fast  # legacy | faithful_fast —— 编译哪张执行图
agent_dispatch:
  profile: fast                   # interactive | safe | fast | budget —— 并发档位（与执行图无关）
  max_concurrency: 0              # 0=自动；显式正整数优先；非法值硬拒绝
```
- **缺省即 `legacy`**：不写 `execution_profile` 的新旧项目、以及全部旧检查点，继续 `legacy-v1` 旧图，
  不做静默迁移。旧会话只能由冻结版本 legacy 图恢复；已冻结 `faithful_fast` 的会话不能被旧代码接管。
- 新项目想要“忠实 + 轻量”：`content_policy: faithful` + `execution_profile: faithful_fast`，
  Phase 0 由真人确认后冻结（新 run 的建议值，不自动生效）。
- 新 fast 全量请求的目标并发为 4（`fast`）；担心宿主承压就显式写 `safe`（=1）。
  **本插件只保证宿主调用窗口重叠，不承诺供应商必然同时推理**（该项为环境限制，见下）。
- 派发预算 `1+K+R`（1 次统一规划 + K 个作者批 + R 次终审）的适用条件、实测值（1 节=3、2 节=3、3 节=5、长输入 3 节=6，
  旧图对照 12）、串行诊断六码与检查点兼容规则：见 [docs/faithful-fast-migration.md](docs/faithful-fast-migration.md)。
- 逐卡验收清单、真实三格式构建结论与全量回归数值：见 [docs/repair-validation.md](docs/repair-validation.md)。


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
