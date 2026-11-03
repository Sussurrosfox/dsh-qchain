# 修复验收汇总（qchain 3.3.0 候选包）

本文件是 23 张修复卡（00–22）的**验收台账**：每卡一行结论、证据文件路径，并严格区分
`PASS` / `completed_with_pending_external` / `BLOCKED_ENV` / `NOT_EXECUTED` 四种状态。
状态语义按 `repair-plan/ACCEPTANCE.md`：mock/stub 通过只证明**控制流**，不证明真实宿主、真实模型与真实成本。

证据根目录：`<self>/.pd/audits/qchain-review/repair-work/evidence/`（下文简写 `evidence/`）。

---

## 1. 逐卡结论

| 卡 | 结论 | 状态 | 证据 |
|---|---|---|---|
| 00 | 保留工作树并建立候选副本与测试基线：生产目录 191 文件哈希快照落盘，候选副本独立于真实学案 | PASS | `evidence/production-hashes-before.json`、`evidence/progress.json`（本卡无独立 `task-00.json`） |
| 01 | 补齐发布资源并按内容哈希更新图片引用（C8）；新增 `scripts/test_release_bundle.py` 17 项（REL-01..04） | PASS | `evidence/task-01.json` |
| 02 | 用真实 KaTeX 严格校验全部数学段（MATH-01..05）：渲染器打不开即失败，不降级为字符串匹配 | PASS（浏览器 staging 待卡 04 已闭合） | `evidence/task-02.json`、`evidence/task-02-e2e.json` |
| 03 | 目录定位到真实卡片（`data-goto` 用完整 DOM id）+ 窄屏 390px 溢出治理（NAV-01..03） | PASS | `evidence/task-03.json` |
| 04 | 发布切换前验收实际离线交付包（DEL-01..04）：解包后可离线运行、断网可用 | PASS | `evidence/task-04.json`、`evidence/task-04-e2e.json` |
| 05 | 规范问题路由并补零派发图接线：infra ERROR 统一停机，零派发路径不断链 | PASS | `evidence/task-05.json` |
| 06 | 内容返修持久上限与无进展熔断（REPAIR-01..04，C3） | PASS | `evidence/task-06.json` |
| 07 | 修复教案前后顺序与错误产物返修目标（`CLASSROOM_PLAN_MISSING` 定点 HALT） | PASS（仅候选，未动生产） | `evidence/task-07.json` |
| 08 | 精确模型路由与角色化上下文：prompt 不截断（`truncated=false`）、超限显式报错 | PASS | `evidence/task-08.json`、`evidence/task-08-metrics.json` |
| 09 | 可取消超时、分型重试、逐 attempt 台账（C4）；真实宿主受控取消见 EXT-2 | PASS（真实取消 PENDING） | `evidence/task-09.json` |
| 10 | 新增执行契约并保留旧检查点语义（CFG-01..04，C1）：`execution_profile` / `graph_version` / `policy_digest` 冻结 | PASS | `evidence/task-10.json`、`evidence/task-10-work/` |
| 11 | 教学偏好与容量预算改为可选策略（POL-01..05）：strict→advisory，显式 strict 仍阻断 | PASS | `evidence/task-11.json` |
| 12 | 一次规划产生三份既有映射（MAP-01..04，C5/C6/C7）：T1 单次调用出三份表 | PASS | `evidence/task-12.json`、`evidence/task-12-work/` |
| 13 | 单作者多节 + 逐节认证恢复：一批最多两相邻节，失败节不回滚已过节点 | PASS | `evidence/task-13.json` |
| 14 | 合并终审与可验证证据投影（`review_projection`），T4 只返修未过 scope | PASS | `evidence/task-14.json` |
| 15 | 接通轻量图 `faithful-fast-v1`，分开构建前/终审门禁；4 次调用账由真实图+fake 子 Agent 得到 | PASS（真实宿主 PENDING） | `evidence/task-15.json` |
| 16 | 精确缓存失效与复用证据：模板/宏/prompt 变更只失效相关条目，全命中时零派发单 | PASS | `evidence/task-16.json`、`evidence/task-16-candidate-digest.json` |
| 17 | 全链验收与真实成本采样（E2E-01..05）：1+K+R 恒等式成立、三格式真实构建、全量回归 | completed_with_pending_external | `evidence/task-17.json`、`evidence/task-17-format-build.json`、`evidence/performance-sample.json`、`evidence/acceptance-results.json` |
| 18 | 打包候选并交付可回滚升级步骤（PKG-01..03）：全量资源入包、隔离解包可用、回滚不丢未提交改动 | 见 `evidence/task-18.json`（本卡） | `evidence/task-18.json`、`evidence/task-18-delivery.md` |
| 19 | 统一并发配置、默认值与执行器计划契约（PARCFG-01..06，C12）：v2 契约、0=自动、非法值硬拒绝 | PASS | `evidence/task-19.json`、`evidence/task-19-work/` |
| 20 | 各阶段整批派发与请求幂等：同批 section 一次 `qchain_dispatch` 进并发池、重复调用幂等认领 | PASS（真实宿主 PENDING） | `evidence/task-20.json`、`evidence/task-20-work/` |
| 21 | 并发观测口径：配置串行/只有一单/Python 限制/宿主排队四类分离，`unknown` 不伪装成实测 0 | PASS（真实测量 PENDING） | `evidence/task-21.json` |
| 22 | 验收 section 并发全链与宿主能力边界（C12.5）：L1 stub PASS、L2 宿主调用 PASS、L3 BLOCKED_ENV | PASS（L3 缺外部证据） | `evidence/task-22.json`、`evidence/task-22-verify.json` |

> 卡 00 没有单独的证据 JSON（实施时以哈希快照与 `progress.json` 登记为准）；其余 22 张均有 `task-NN.json`。

---

## 2. 按状态分组

### PASS（本环境可复现的验收全部通过）

卡 00, 01, 02, 03, 04, 05, 06, 07, 08, 10, 11, 12, 13, 14, 15, 16, 19, 20, 21, 22。
其中 09/15/16/20/21/22 标注了"真实宿主/真实模型"的 PENDING 前缀，见下节——那部分不计入 PASS。

### completed_with_pending_external（本体完成，外部证据未取得）

- **卡 17**：E2E 全链用 stub 子 Agent 穿过真实节点与真实派发入口，1+K+R 与三格式构建均跑通；
  但真实 agent 质量/成本（EXT-1）、真实宿主取消（EXT-2）、供应商推理并发（ENV-1）未取得，故整体不判 PASS。
- **卡 09 / 15 / 16 / 20 / 21 / 22**：真实宿主子代理与真实模型未演练（本环境禁止启动流水线、禁止真实 agent）。

### BLOCKED_ENV（环境不可观测，非代码缺陷）

- **ENV-1 / L3 供应商推理并发**：`.pd/dispatch-runs` 能观测"同时有几个子 Agent 在跑"，
  不能观测"几个模型在同时推理"。任何加速比声明都缺依据，只能声称**宿主调用窗口重叠**。
  安装到生产插件后需用真实台账复核；不得用 stub 证据替代。

### NOT_EXECUTED（未执行，不得标 PASS）

- **EXT-1 真实 agent 试跑**（需真人授权；约束：`call_cap ≤4` 次、单 prompt ≤32000 字符）。
- **EXT-2 真实宿主取消**（需要真实宿主 subagent 服务与可控非 LLM provider）。
- **E2E-01 的 B/C 分支**、**E2E-05**：未执行（卡 17 已如实登记）。
- **E2E-02 的文档口径偏差**：实测三节 5 次 vs C9 文"=4"，属待决设计项（见 §3）。

---

## 3. 真实三格式构建结论（卡 17）

环境：MiKTeX 26.5 xelatex + Chrome/Playwright；在**临时副本**跑（`real_self_written=false`、
`production_written=false`），证据 `evidence/task-17-format-build.json`。

| 格式 | 结果 | 说明 |
|---|---|---|
| `html` | **PASS**（exit 0，0 错误） | 学生/教师 HTML 均出包，`html_sha256` 非空 |
| `pdf` | FAIL（技术跑通，门禁拦下） | xelatex 真实出包、`pdf_sha256` 非空；2 条错误均为既有学案内容的**严重 Overfull \hbox**（最大 43.9pt 与 270.5pt，阈值 10.0pt） |
| `both` | FAIL（同上） | PDF 腿继承上述 Overfull 判定 |

判读：**pdf/both 的技术链路是通的，失败来自既有内容排版超宽被严格门禁按设计判 FAIL，不是插件回归。**
`html` 全通过，说明卡片双模交付链路正常。

---

## 4. 全量回归与回滚演练

全量回归（候选根，`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、`PYTHONDONTWRITEBYTECODE=1`）：

| 套件 | 结果 |
|---|---|
| `python -m pytest -q -p no:cacheprovider scripts` | **1000 passed + 86 subtests**（卡 17 实测；任务书预估 981，差异来自卡 17 新增的测试文件） |
| `node --test`（dispatch_runtime / cancellation / batch_selection / observability / policy / host_parallelism） | **103 pass / 0 fail**（19 + 19 + 18 + 9 + 33 + 5） |

回滚演练（隔离副本，生产目录只读复制）：命令、退出码与"38 条未提交改动不丢失"的核对结果见
`evidence/task-18.json` 的 `rollback_drill` 与 `evidence/task-18-delivery.md`。
**当前生产插件未安装本候选包**（本轮只交付候选包与步骤）。

---

## 5. 已知残留

- `workflow/execution_policy.py:54` 的 `IMPLEMENTED_GRAPH_VERSIONS` 仍只列 `legacy-v1`（过时展示元数据，
  不影响图选择；卡 18 写域不含该文件）。
- `templates/config.yaml` 注释块仍写"3 节精炼自足体系 / 强制 4~7 问"（不在卡 18 写域）。
- `repair-plan/tasks/22.md` 的锚点清单未包含 `workflow/nodes/fast_review_nodes.py`（该卡已记录，待方案目录维护者更新）。
- 失败回滚的真实生产演练需真人授权；本包已提供命令序列与隔离副本证据。
