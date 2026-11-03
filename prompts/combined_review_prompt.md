# 合并终审子 Agent（combined-reviewer）派发规范

本模板是**合并终审员**（combined-reviewer）的执行口径权威维护位置。修复卡 14（契约 C6）把原来「每节一个语义审读员 + 每节一个验收员」的两轮重复审读，合并为**一次独立审读覆盖多节**：同一位审读员在同一次运行里，对一批小节（R 批覆盖全部待审 scope/qid）同时完成

1. **来源比对**（source comparison）：本批每节的内容能否逐条回溯到已批准来源单元，覆盖是否完整、有无凭空补写；
2. **数学正确性**（math correctness）：定义/定理的适用条件、证明步骤、符号一致性、结论强弱；
3. **呈现完整性**（presentation completeness）：学生可见文本是否自足（前置知识、结论框、编号、图表），有无模板污染；
4. **交付证据**（delivery evidence）：本批交付产物（student.pdf / student.html / cards.json 等）的真实 sha256 是否与本轮构建结果一致。

审读员与写作者严格分离（INV-2）：**同一次运行、同一条 prompt、同一时间**都只读同一批候选，不存在「先盲审再揭原稿」的两次体验差——本模板一次给全候选、来源与产物指纹。

运行状态机与交付门禁归 `../references/production.md`，来源锚点契约与写作者共享 `subagent_prompt.md` 的 F3 节；语义审读码表沿用 `semantic_check_prompt.md`。

---

## 审读员任务包清单（派发时固定提供，不多不少）

```text
1. 任务身份：run_id / dispatch_id / batch_id / reviewer_role / scopes（本批小节列表）。
2. 逐节候选切片全文 self/<sec>.tex 及其 sha256（section_digest，必须原样回填）。
3. 逐节已批准来源单元 brief（source_inventory 中 disposition=preserve 的条目）。
4. 逐节期望 qid 清单（reviewed_qids 必须逐题覆盖，缺一题即失败）。
5. 本轮交付产物指纹 artifact_digests（student.pdf / student.html / cards.json 的当前 sha256）
   与 output_format（html / pdf / both）——按格式逐文件绑定，html-only 显式不含 PDF。
6. 独占输出路径：.pd/combined-review/<batch_id>-r<N>.json（只写此文件）。
```

审读员**不得**修改 `self/<sec>.tex`、`main.tex`、`student.tex`、`teacher.tex`、`run.json`、`release/`，**不得**写 `.pd/semantic/` 或 `.pd/acceptance/`（那两份是宿主用本产物做确定性投影得到的，不是审读员写的），**不得**写 `.pd/agent-calls/`（证据台账由宿主登记）。

---

## 检查码（issue.code）

数学/语义侧沿用 `semantic_check_prompt.md` 的 8 个码（`MISSING_CONDITION` / `UNJUSTIFIED_GENERALIZATION` / `PREREQUISITE_GAP` / `PROOF_DIVISION_BY_ZERO` / `CONTRADICTORY_NOTATION` / `OVERSTRONG_CLAIM` / `TEMPLATE_CONTAMINATION` / `BUDGET_OVERFLOW`）；本模板新增交付与忠实性码：

| code | 含义 | 判定要点 |
| --- | --- | --- |
| `SOURCE_MISSING_IN_OUTPUT` | 来源单元未落到产物 | 已批准来源单元在产物中找不到对应内容 |
| `UNSOURCED_CONTENT` | 无来源新增内容 | 产物出现输入材料与来源清单都没有的断言/例题 |
| `FIDELITY_DRIFT` | 忠实性漂移 | 结论、条件、数值或证明思路与来源不一致 |
| `MATH_ERROR` | 数学错误 | 证明步骤不成立、推理跳跃、结论错误 |
| `NUMBERING_BREAK` | 编号/引用断裂 | 定理号、公式号、交叉引用错位或指向不存在目标 |
| `DELIVERY_HASH_MISMATCH` | 交付指纹不符 | 产物 sha256 与任务包给定的 artifact_digests 不一致 |
| `DELIVERY_FILE_MISSING` | 交付文件缺失 | output_format 要求的交付文件没产出（both 必须含 PDF 与 HTML） |

`severity` 由 `status` 推出：`status=open` → 阻塞（必须 `REVISE` 或 `NEEDS_EVIDENCE`）；`status=fixed|withdrawn` → 记录但不阻塞。

---

## 输出 JSON schema（.pd/combined-review/<batch_id>-r<N>.json，schema_version=2）

```json
{
  "schema_version": 2,
  "review_kind": "combined_source_fidelity",
  "dispatch_id": "<宿主在任务包中给定的派发单 ID，原样回填>",
  "batch_id": "<宿主在任务包中给定的批次 ID，原样回填>",
  "reviewer_role": "<宿主给定的角色名，原样回填>",
  "reviewed_at": "<ISO8601 时间戳>",
  "input_digest": "<宿主给定的派发输入指纹，原样回填>",
  "output_format": "html",
  "scopes": ["sec1", "sec2"],
  "artifact_digests": {
    "student.html": "<本轮交付产物真实 sha256>",
    "cards.json": "<同上>"
  },
  "sections": [
    {
      "scope": "sec1",
      "section_digest": "<候选切片 sha256，原样回填>",
      "reviewed_qids": ["sec1-01", "sec1-02"],
      "semantic_conclusion": "PASS | REVISE | NEEDS_EVIDENCE",
      "acceptance_conclusion": "PASS | REVISE | NEEDS_EVIDENCE",
      "issues": [
        {
          "id": "sec1-i1",
          "qid": "sec1-01",
          "part": "proof | statement | figure | delivery",
          "code": "MATH_ERROR",
          "detail": "被质疑的原文句子（逐字摘录）+ 为什么错",
          "evidence": "来源定位（文件/行/定理号）或推导说明",
          "status": "open | fixed | withdrawn",
          "resolution": "如何修复（fixed/withdrawn 必填）",
          "verified_digest": "<修复后候选 sha256（fixed 必填）>"
        }
      ],
      "source_comparison": "本节的来源定位与覆盖情况（逐条说明哪些来源单元落到了哪一段，缺什么）"
    }
  ]
}
```

判定规则（宿主零信任校验，任一不满足即整批失败）：

- `scopes` 必须**完整覆盖**任务包给定的本批小节集合，`sections[].scope` 必须与之一一对应（缺节 / 多节 / 重复节都失败）；
- `scopes` 内每一节的 `reviewed_qids` 必须覆盖任务包给定的期望 qid（缺 qid 失败）；
- `section_digest` / `input_digest` / `artifact_digests` 必须是真实 64 位 sha256，必须与任务包给定值一致（候选事后被打乱时，只有 `.pd/shuffle-map.json` 血缘能证明是同一候选的确定性变换）；
- 两个结论都只能是 `PASS / REVISE / NEEDS_EVIDENCE`；任一节任一结论非 PASS、或存在 `status=open` 的问题 → 整批不得判 PASS；
- `output_format=html` 时**不得**声明 `student.pdf`（PDF 字段按格式显式不适用；写零 hash 或占位 PDF 一律按伪造证据失败）；`both` 必须同时给出 PDF 与 HTML 的真实指纹；
- `source_comparison` 必须是真实审读文本（不得为空或占位）。

---

## 宿主侧投影（本模板的产物如何变成两份证据）

审读员**只**产出上面这一个主产物。宿主随后用 `scripts/review_projection.py` 做确定性投影（同一条审读 → 两套证据）：

- `.pd/semantic/<sec>.json`（schema_version=1 基础字段 + `derived_from`）——供语义硬闸门使用；
- `.pd/acceptance/<sec>.json`（schema_version=2 + `review_kind` + `content_review_refs` / `delivery_check_ref` + `derived_from`）——供验收硬闸门使用。

两份投影都必须能由主产物**重算**出来：任何字段被改写、父产物字节被改动、或证据台账里的产物摘要与父产物不符，都判失败（无 receipt 的投影不得认证）。

---

## 派发协议与证据台账（host-subagent-v1）

1. **只返回、不直写**：子 Agent 最终回复必须是且只能是一个 JSON 对象 `{"files": {"<相对路径>": "<完整文件内容>"}}`，路径仅限于本批的 `.pd/combined-review/<batch_id>-r<N>.json`。宿主按白名单原子写入并登记 `.pd/agent-calls/<dispatch_id>.json`（`batch_id` 以 `bat-t4c` 开头，供创审分离校验识别）。
2. **禁止代写**：严禁审读员修改候选切片或交付产物；严禁主 Agent 在本会话内直接撰写合并终审结果冒充子 Agent 产出——合并终审的结论只能来自 `.pd/combined-review/` 主产物 + 对应证据台账。
3. **失败语义**：子 Agent 失败或返回不含 files 时按 `max_retries` 重试；仍失败则本批全部小节判 `missing_result`，不得交付，也不得用旧审读结果静默替代（旧审读的 `artifact_digests` 不得改写成新产物指纹来假装审过新文件）。
4. **血缘不翻案**：`.pd/shuffle-map.json` 的打乱血缘只能证明「当前切片 = 被审读候选 + 确定性选项重排」，**不得**把 `REVISE` / `NEEDS_EVIDENCE` 翻成 PASS。
