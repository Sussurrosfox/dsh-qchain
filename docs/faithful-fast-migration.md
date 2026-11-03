# faithful_fast 迁移指南（qchain 3.3.0）

本文件说明 **3.3.0** 新增的两个正交开关、faithful_fast 的默认矩阵、1+K+R 派发预算的适用条件与实测值、
并发档位与 v2 派发契约、以及**检查点（checkpoint）兼容规则**。

修订记录：3.3.0 首次发布。卡 18 归档（证据见
`.pd/audits/qchain-review/repair-work/evidence/task-18.json`，逐卡验收见 `docs/repair-validation.md`）。

> 阅读前提：本文所有默认值都取自运行时代码，不是纸面约定。
> `workflow/execution_policy.py` 的 `EXECUTION_DEFAULTS` / `LEGACY_EXECUTION_DEFAULTS`、
> `workflow/dispatch_policy.py` 的 `PROFILE_CONCURRENCY` 是唯一事实来源；两者不一致时以代码为准并视为缺陷。

---

## 1. 两个开关，不要混用

| 开关 | 取值 | 决定什么 | 出现位置 |
|---|---|---|---|
| `content_policy` | `faithful` / `authoring` | **内容取向**：原料是否必须原样忠实呈现（faithful）还是允许再创作（authoring） | `config.yaml` 扁平键；Phase 0 冻结进 `intent-contract.json` |
| `execution_profile` | `legacy` / `faithful_fast` | **执行图**：编译哪张 StateGraph（`legacy-v1` / `faithful-fast-v1`） | `config.yaml` 扁平键 `execution_profile`；Phase 0 冻结为执行契约 |
| `agent_dispatch.profile` | `interactive` / `safe` / `fast` / `budget` | **并发档位**：单批同时放行几个子 Agent | `config.yaml` 的 `agent_dispatch:` 嵌套块，或扁平 `agent_max_concurrency` |

易错点：

- `content_policy=faithful` **不会**自动把执行图换成 fast，也不改变检查点判定。
  历史上把"忠实"和"快速"混成一个开关，正是 3.2.0 之前的缺陷来源。
- `execution_profile` 只决定图；`agent_dispatch.profile` 只决定调度。
  想让忠实会话跑得快，两个都要显式设：`content_policy: faithful` + `execution_profile: faithful_fast` +
  `agent_dispatch: {profile: fast}`（或留空，由 faithful_fast 推得 fast，见 §5）。
- `qchain_pipeline` / `setup_self.py` 的 `--profile` 是**交付模式**（`self_study` / `teaching` / `classroom`），
  与上面两个 `profile` 都不是一回事：它决定课时预算与交付清单，不决定图也不决定并发。

`execution_profile` 的缺省语义：**缺省即 `legacy`**。旧项目、旧 `config.yaml`、旧检查点一律继续 legacy，
不做静默迁移（见 §6）。新 run 若 `content_policy: faithful`，Phase 0 会**建议** `faithful_fast`，
但必须由真人在 Phase 0 确认后才冻结。

---

## 2. faithful_fast 默认矩阵

Phase 0 冻结执行契约时，未显式配置的键取 `EXECUTION_DEFAULTS`（faithful_fast）或
`LEGACY_EXECUTION_DEFAULTS`（legacy）：

| 配置键 | legacy 默认 | faithful_fast 默认 | faithful_fast 下的含义 |
|---|---|---|---|
| `pedagogy_policy` | `strict` | `advisory` | 教学法偏好（难度梯度、问题密度等）只给建议，不阻断 |
| `budget_policy` | `strict` | `advisory` | 课时预算/时长估算只报告，不阻断 |
| `require_classroom_plan` | `true` | `false` | 不强制先生成 `classroom-plan.md` |
| `writer_batch_max_sections` | `1` | `2` | 一次作者派发最多写几个**相邻**节 |
| `writer_batch_max_source_chars` | 不限 | `16000` | 单批原料字符上限，超限自动拆批 |
| `agent_prompt_max_chars` | 不限 | `32000` | 单次派发 prompt 上限，超限显式报错而不是静默截断 |
| `repair_max_per_scope` | `3` | `2` | 同一 scope 的内容返修轮次上限 |
| `repair_max_total` | 不限 | `6` | 整轮返修总次数上限（无进展熔断另计） |
| `dialogue_enabled` | 沿用旧值 | `false`（用户未另选时） | 新 faithful_fast 默认不产出对话层块 |

显式值永远优先于上表；但**只有 faithful_fast 独有**的键（如上表后五行）在 legacy 下不生效，
legacy 仍走 `一节一写作者` 的旧语义。

**硬门禁不随 profile 降级。** `advisory` 只作用于教学偏好与课时预算两类策略：

- 永远硬失败：严格数学校验（KaTeX/TeX 公式）、忠实性检查（`check_fidelity`）、
  必需资源缺失（模板/宏包/字体/`math-config.json`）、编号结构、构建失败、`QCHAIN_ALLOW_SKIP_CHECKS` 未设时的全部门禁。
- 可降为 advisory：`pedagogy_policy`（教学偏好）、`budget_policy`（课时预算）。
- 显式写 `pedagogy_policy: strict` / `budget_policy: strict` 即恢复阻断行为；faithful_fast 不是"降低质量的档位"，
  而是"不把偏好当否决权"。

---

## 3. 检查点兼容规则（最重要的一节）

Phase 0 冻结进检查点的执行元数据（`execution_schema_version: 2`）：

```yaml
execution_schema_version: 2
execution_profile: legacy | faithful_fast
graph_version: legacy-v1 | faithful-fast-v1
policy_digest: sha256:<canonical-effective-config>
execution_config: { ...上表全部生效值... }
```

恢复（resume）时的解析顺序是**先读检查点、后编译图**，不是先读 `config.yaml`：

1. 读检查点冻结块 → 用其中的 `graph_version` 编译对应图
   （`workflow/graph.py:712 create_graph_for_version`）。
2. 检查点**没有**执行版本（3.2.0 及更早写的旧检查点）→ 按 `legacy-v1` 恢复，
   保留旧节点名、旧 `next`、旧 pending batch 语义；**不因为 `config.yaml` 写了 `content_policy: faithful` 就换图**。
3. 冻结版本未知或与冻结 profile 矛盾 → 明确停止，不猜：
   `UNKNOWN_GRAPH_VERSION` / `EXECUTION_PROFILE_GRAPH_MISMATCH` / `EXECUTION_METADATA_INCOMPLETE`。
4. `config.yaml` 试图改写已冻结的 profile → `EXECUTION_PROFILE_OVERRIDE_REJECTED`
   （`execution_policy.assert_profile_not_overriding`）。
5. 想换图/换冻结策略：**开新 thread**（新目录或新 `thread_id`）重新走 Phase 0，
   不允许原地重写旧 SQLite 检查点。

两条不对称的方向，必须记住：

- **旧检查点只能由冻结版本 legacy 图恢复。** 把 3.3.0 装上去重启，旧会话仍会在 `legacy-v1` 上继续，
  不会因为你新写了 `execution_profile: faithful_fast` 而"升级"到轻量图。
- **新 fast 检查点不能由旧代码（≤3.2.0）接管。** 旧代码里没有 `faithful-fast-v1` 这条职责序列，
  也不认识执行契约冻结块；它的节点名、`next`、pending batch 语义与 fast 图不同。
  这类接管**不受支持**：回退到 3.2.0 时，**已冻结 faithful_fast 的会话必须按新会话重跑**，
  不要指望旧代码"半截 legacy 继续跑"（不保证能跑，也不保证报错好看）。
  判定依据只看检查点冻结块里的 `graph_version`，不看 `config.yaml` 现在写了什么。

---

## 4. 1+K+R 派发预算：适用条件与实测

faithful_fast 的冷缓存、无返工派发次数为：

```
calls = 1 + K + R
 1 = T1 统一规划（一次 agent 出三份既有映射：小节计划 / 素材映射 / 验收要点）
 K = 作者批次数 = ceil(节数 / writer_batch_max_sections)（且受 writer_batch_max_source_chars 约束）
 R = 终审轮次（T4 合并终审；REVISE 只返修未通过的 scope，返修轮各计一次）
```

**适用条件**（缺一条就不要拿这个公式做承诺）：

- faithful_fast（`faithful-fast-v1`）新会话；legacy 图口径不同（下表）。
- 冷缓存且零返工；缓存全命中时真实调用数更少（见 §5 的 `cache_reuse`）。
- 节数 ≤ 批容量时 K 才等于 1；长输入会先被 `writer_batch_max_source_chars=16000` 拆批，K 随之上升。
- 该公式只计**派发次数**，不含 wall-clock、排队、模型耗时。**不得**把"调用数下降"表述为"时长下降 67%"。

实测对照（卡 17，样本见 `evidence/performance-sample.json`；均由真实节点函数 + 真实 `qchain_dispatch` 入口、
fake 子 Agent 执行，**不是**真实宿主+真模型）：

| 场景 | 1+K+R | 实测派发次数 | 拆解 |
|---|---|---|---|
| 1 节 | 1+1+1 | **3** | K=1（1 节一批）、R=1（一次终审） |
| 2 节 | 1+1+1 | **3** | K=1（一个两节批）、R=1 |
| 3 节 | 1+2+2 | **5** | K=2（批 1=节 1–2，批 2=节 3）、R=2 |
| 长输入 3 节（K=3） | 1+3+2 | **6** | 原料超字符上限 → 三个作者批、两次终审 |
| 3 节（legacy 对照，未实测） | 3+3N=12 | — | 旧图"规划+逐节作者+逐节审"共 3 个固定调用 + 每节 3 次 |

> **文档口径修正（重要）**：`DATA-CONTRACTS` C9 早期文本写"三节、每批最多两节、一批终审时等于 4"。
> 卡 17 实测为 **5**（1+2+2：终审按批各计一次，不是全轮只计一次）。差额 1 次的来源是终审轮次 R=2。
> 本文以实测 5 为准；C9 的"=4"应理解为"K+R 各按最乐观计一次"的理想下限，不作为承诺值。
> 该偏差已在卡 17 证据登记为 `E2E-02 DEVIATION`，是"改 C9 口径还是改审读批策略"的设计层待决项，不影响公式恒等式。

配套观测（每批必记，缺一即可疑）：真实 attempt 数、排队时间、模型耗时（不可观测则写 `null` 而不是 0）、
重试原因、缓存命中数、prompt 字符数。token/金额**只**在供应商返回真实用量时填；
`evidence/performance-sample.json` 中 `wall_clock_improvement_claimed=false` 是刻意的：
本环境没有真实模型调用，任何加速比声明都属伪造。

---

## 5. 并发档位与 v2 派发契约

`execution_profile` 决定图，**并发档位单独配**。新会话冻结 `dispatch_contract_version: 2`
（独立于检查点的 `execution_schema_version`）。

档位（`workflow/dispatch_policy.py: PROFILE_CONCURRENCY`，上限 `MAX_CONCURRENCY_LIMIT=16`）：

| profile | 目标并发 | 典型用途 |
|---|---|---|
| `interactive` | 1 | 单节返修、交互式局部编辑 |
| `safe` | 1 | 显式保守；调试、不可控宿主机 |
| `fast` | 4 | faithful_fast 新全量请求的默认目标（宿主侧通常 2–4 个槽） |
| `budget` | 2 | 需要并发但不想打满 |

解析优先级（v2，高→低）：

1. 工具调用显式正整数（`qchain_dispatch` 的 `max_concurrency`）；
2. 配置嵌套 `agent_dispatch.max_concurrency`；
3. 扁平 `agent_max_concurrency`；
4. `agent_dispatch.profile` 推得的目标值；
5. 兜底 `safe`。

`0` 表示"自动"（交给解析链），不是"无限"。以下一律**明确拒绝**而不是夹取：
负数、小数、`NaN`、布尔、`>16`（`CONFIG_CONCURRENCY_INVALID` / `CONFIG_CONCURRENCY_OUT_OF_RANGE` /
`TOOL_CONCURRENCY_*`）；嵌套与扁平同时为正且不相等 → `CONFIG_CONCURRENCY_CONFLICT`，**不静默择一**。
`run_scope` 缺省档位：`full`→`fast`，`focused`→`interactive`，`edit`→`interactive`；
faithful_fast 未被显式覆盖时按 `fast`（`FAST_EXECUTION_PROFILE`），旧会话继续旧优先级且带来源诊断
（`CONFIG_CONCURRENCY_SHADOWED` 属诊断，不失败）。

**批选择：用 `batch_id` 单调用，不要无参扫描。**
AGENT_DISPATCH 中断的载荷里带 `dispatch_args: {self_dir, batch_id}`，批索引落在
`.pd/dispatch-batches/<batch_id>.json`。调用 `qchain_dispatch` 时：

- 传 `batch_id` → 只派发该批；`batch_id` 与 `request_ref` 同时传 → 拒绝（`BATCH_REQUEST_INCONSISTENT`）；
- 无参 → **只允许**在"当前只有一个待派发批次"时成功，否则 `BATCH_REQUIRED` / `AMBIGUOUS_BATCH`；
- 重复调用同一批 → 幂等认领（`DISPATCH_BUSY`），不会重复起子 Agent；批索引不一致 → `BATCH_INDEX_CONFLICT`。

**串行诊断六码**（每批 `serial_reason_code`，取值可机读，`index.js:436-477`）：
配置串行或"只有一单"必须被区分开，不许拿"实测 0 并发"糊过去：

| 码 | 何时出现 | 含义 |
|---|---|---|
| `single_ready` | 待派发请求 ≤1 | 只有一单，谈不上并发（`source=observed_request_count`） |
| `cache_reuse` | 全部命中缓存 | 真实派发 0 次（`cache_reuse_count` 记录命中数） |
| `artifact_dependency` | 请求声明了产物依赖 | 结构上必须串行（`declared_request_dependency`） |
| `worker_cap` | 执行器能力/夹取低于配置 | 能力上限所致（`executor_worker_cap` / `effective_concurrency_clamped`） |
| `explicit_safe` | 档位是 safe/1 | 用户显式选择保守（`policy_profile:*`） |
| `host_queue_or_unknown` | 槽位占用未确认、峰值未观测或峰值低于上限 | **不得**伪装成"已实测并发"，未知就写未知 |

未知宿主能力一律写 `null`，不允许把 Python 侧延迟池的保守值 1 当作宿主上限。

**不能承诺供应商必然同时推理。** 上表证明的是**宿主调用窗口重叠**（L1 stub / L2 真实宿主调用，
卡 20/21/22 证据）。供应商是否真在并行推理属 L3 不可观测层，状态为
`BLOCKED_ENV`（ENV-1）；`.pd/dispatch-runs` 的峰值只能说明"同时在跑几个子 Agent"，
不能说明"几个模型同时在算"。任何"并发加速比"表述都必须附这一条限制。

每条批记录同时带：`dispatch_contract_version`、`batch_id`、`run_id`、`executor_kind`
（`python_registry` / `host_subagent`）、`profile`、`configured_concurrency`、`concurrency_source`、
`batch_size`、`worker_max_concurrency`、`effective_concurrency`、`serial_reason`；
其中 `max_concurrency` 字段**始终等于实际执行上限**，不是"配置愿望值"。

---

## 6. 迁移决策表

| 你的处境 | 建议 |
|---|---|
| 已有项目、已完成或进行中的旧会话，想继续 | **legacy**：什么都不改，3.3.0 启动后旧检查点仍按 `legacy-v1` 恢复 |
| 想做小范围局部返修（`run_scope: focused/edit`） | **legacy** 或 faithful_fast 均可；并发档位留 `interactive` |
| 新项目、原料已足够好、只要求忠实呈现与自足 PDF/HTML | **faithful_fast**：`content_policy: faithful` + `execution_profile: faithful_fast`，Phase 0 确认后冻结 |
| 新项目但仍要"一节一写作者"的旧审读节奏 | **legacy**（`execution_profile: legacy` 或干脆不写） |
| 教学偏好/课时预算必须当否决权 | 任一 profile + 显式 `pedagogy_policy: strict` / `budget_policy: strict` |
| 团队要先评估再切换 | 先在候选包与临时副本上演练（`docs/repair-validation.md` §4 有命令），通过后再装 |

回滚到 3.2.0 的步骤与"未提交改动不丢失"的演练见
`docs/repair-validation.md` §4；简版命令在 `evidence/task-18-delivery.md`。

---

## 7. 已知残留（本版本记录、未修）

- `workflow/execution_policy.py:54` 的 `IMPLEMENTED_GRAPH_VERSIONS = (GRAPH_VERSION_LEGACY,)`
  是**过时元数据**：`faithful-fast-v1` 自卡 15 起已可由 `create_graph_for_version` 编译运行，
  但该常量仍只声明 legacy。它当前唯一用途是 `describe_for_phase0` 的 `implemented_graph_versions` 展示，
  不影响图选择与恢复判定。卡 18 写域不含该文件，故只记录（详见 `evidence/task-18.json` 的 `residual`）。
- `templates/config.yaml` 的注释块仍写着"3 节精炼自足体系 / 强制 4~7 问"的旧口径，
  与本 profile 的批次语义不一致；该文件不在卡 18 写域，留待后续文档卡统一。
- 真实宿主 + 真模型的并发与成本（EXT-1 / EXT-2 / ENV-1）仍需真人授权试跑，本包不含该证据。
