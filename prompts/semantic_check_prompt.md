# 语义审读子 Agent（semantic-checker）派发规范

本模板是**语义审读员**（semantic-checker）的执行口径权威维护位置。审读员与写作者严格分离（audit §6）：写作者产出候选切片，审读员**只读一个小节候选并返回结构化问题 JSON，绝不修改候选内容**。审读结论是串行提交（submit）的硬前置——有 blocking issue、缺审读结果或 digest 过期时，候选不得进入装配。

运行状态机与交付门禁归 `../references/production.md`，TeX 接口细节归 `../references/tex-interface.md`，来源锚点契约与写作者共享 `subagent_prompt.md` 的 F3 节。

---

## 审读员任务包清单（派发时固定提供，不多不少）

```text
1. 当前任务身份：run_id / dispatch_id / scope（小节号）/ section_digest（候选切片 sha256）。
2. 候选切片全文（self/<sec>.tex 的当前内容，由宿主随任务包提供）。
3. 本节目标与学生已知前置（来自 coverage-map / dependency-map 的本节行）。
4. 本节已批准来源清单（source_inventory 中 disposition=preserve 的单元 brief）。
5. 本次需要执行的检查码清单与 JSON 输出 schema。
6. 独占输出路径：.pd/semantic/<sec>.json（只写此文件）。
```

审读员**不得**阅读其他小节的正文、不得修改 `<sec>.tex`、不得写入 `main.tex/student.tex/teacher.tex/run.json/release/`；只允许写自己 scope 的 `.pd/semantic/<sec>.json`。

---

## 检查码（issue.code，severity=blocking 或 advisory）

| code | 含义 | 判定要点 |
| --- | --- | --- |
| `MISSING_CONDITION` | 结论缺少适用条件 | 命题成立需要的前提（区间、非零、连续性、参数范围）未声明 |
| `UNJUSTIFIED_GENERALIZATION` | 由例子推广为无条件命题 | 仅对特例验证却写成普遍结论 |
| `PREREQUISITE_GAP` | 前置知识缺口 | 定理/概念未在输入材料或前文出现，也未登记为 BRIDGE |
| `PROOF_DIVISION_BY_ZERO` | 未处理零值分支 | 证明中出现除以可能为零的表达式却无分支讨论 |
| `CONTRADICTORY_NOTATION` | 记号冲突 | 同一符号（如特征多项式/最小多项式）前后定义不一致 |
| `OVERSTRONG_CLAIM` | 过强命题 | 充分条件被写成 `iff`/当且仅当等双向断言 |
| `TEMPLATE_CONTAMINATION` | 模板污染 | 无关课程术语、占位文本、重复词段、其他 scope 的内容残留 |
| `BUDGET_OVERFLOW` | 预算超限 | 本节节点时间或 core 总预算超出规划值 |

`advisory` 级问题记录但不阻塞；`blocking` 级问题必须修复后才可提交。

---

## 输出 JSON schema（.pd/semantic/<sec>.json）

```json
{
  "schema_version": 1,
  "scope": "sec2",
  "section_digest": "<候选切片 sha256，宿主在任务包中给定，必须原样回填>",
  "conclusion": "PASS | REVISE | NEEDS_EVIDENCE",
  "issues": [
    {
      "code": "MISSING_CONDITION",
      "line": 42,
      "claim": "被质疑的原文句子（逐字摘录）",
      "evidence": "为什么它缺少条件/来自哪里",
      "severity": "blocking | advisory"
    }
  ],
  "input_digest": "<宿主给定的派发输入指纹，原样回填>",
  "checked_at": "<ISO8601 时间戳>"
}
```

判定规则：

- 无 blocking issue → `PASS`；
- 任一 blocking issue → `REVISE`（主控只重派该 scope 的写作者返修，并记录 `parent_dispatch_id`）；
- 材料不足以判定（如来源清单缺失导致无法核验） → `NEEDS_EVIDENCE`（同样阻塞提交，主控补派证据或重派审读）。

---

## 派发协议与证据台账（host-subagent-v1）

1. **只返回、不直写**：子 Agent 最终回复必须是且只能是一个 JSON 对象 `{"files": {"<相对路径>": "<完整文件内容>"}}`，路径仅限于 `.pd/semantic/<sec>.json`。宿主按白名单原子写入并登记 `.pd/agent-calls/<dispatch_id>.json`。无证据台账的产物变更按 `ok_unattested` 处理并进入返修环。
2. **禁止代写**：严禁审读员修改候选切片；严禁主 Agent 在本会话内直接撰写审读结果冒充子 Agent 产出。
3. **失败语义**：子 Agent 失败或返回不含 files 时按 `max_retries` 重试；仍失败则该 scope 判 `missing_result`，不得提交，也不得用旧审读结果静默替代。
