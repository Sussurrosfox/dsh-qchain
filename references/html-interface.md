# HTML 卡片交付模版与填写规范（qchain-html）

本文件是 qchain-html（题链 HTML）交付层的权威规范：交付物不再是 PDF，而是两个自包含
HTML —— `student.html`（学生版）与 `teacher.html`（教师版）。**每个组件、
每道题目都是 HTML 的一页（一张卡）**，按钮翻页、答案揭示、选择题点击即时
红/绿判定。

创作侧（sec*.tex 的写作纪律）不变，仍见 `tex-interface.md` 与
`prompts/subagent_prompt.md`；本文件规定 TeX 源如何被解析为卡片、卡片如何
渲染为 HTML 页面、页面必须满足的交互契约与版本纪律。写作判定的最终依据是
渲染结果：写进 sec*.tex 的每个组件，必须能在 HTML 中成为独立、可交互、
不泄露的一页。

---

## 一、交付物与模版

### 1.1 交付物

| 文件 | 说明 |
|---|---|
| `student.html` | 学生版：参考解答收入隐藏揭示区（按钮/键盘揭示），无教学要点、无题型/难度标签、教师专属对白默认隐藏 |
| `teacher.html` | 教师版：参考解答默认可见（可一键隐藏），含教学要点、题型/难度/练习模式标签与教师专属对白 |
| `cards.json` | 中间牌组（排查/审计用，不参与交付哈希绑定） |
| `assets/` | 切片引用图片的拷贝（`\includegraphics` 的静态参数） |

两个文件均为**自包含单文件**：CSS/JS 内联，仅数学公式渲染依赖 KaTeX CDN
（断网时公式退化为 `$...$` 源码文本，翻页与交互不受影响）。

### 1.2 模版

模版文件：`templates/html/deck.template.html`。构建时由
`scripts/render_html.py` 用牌组数据填充占位符生成最终 HTML。**生成物不得
手改**；需要调整样式/交互时改模版，并同步本规范与 `scripts/check_html.py`
的契约检查。

占位符（双花括号；模版头部注释用单花括号免替换写法）：

| 占位符 | 内容 |
|---|---|
| `{{LANG}}` | 文档语言（固定 `zh-CN`） |
| `{{META_TITLE}}` | 浏览器标签标题（`<title>（学生版/教师版 · qchain-html 题链 HTML）`） |
| `{{MAIN_TITLE}}` | 页眉主标题（取自 config.yaml 的 `main_title`） |
| `{{VERSION}}` | `student` \| `teacher`，驱动 JS 默认答案显隐 |
| `{{VERSION_LABEL}}` | `学生版` \| `教师版` |
| `{{GENERATED_AT}}` | 生成时间 |
| `{{TOC_HTML}}` | 目录抽屉条目（按节分组） |
| `{{CARDS_HTML}}` | 全部卡片（一张卡 = 一页） |

渲染器强制校验：占位符全部填充（正文区任何形式的已知占位符残留即失败）。

### 1.3 组件 → 页面映射（一组件一页）

| TeX 组件 | 卡片类型 `kind` | 渲染形态 |
|---|---|---|
| `\section*{...}` + 前置导语 | `section` | 节标题页（大标题 + 引导语） |
| `sectiondialogue` / `exampledialogue` | `dialogue` | 对话页：一条 `\speaker` 一行台词（角色徽章 + 台词），`exampledialogue` 标注绑定题 `qid` |
| probchain 一级 `\item`（题目） | `question` | 题目页：题干 + (a)(b)… 子题 + 交互槽位 + 答题留白；参考解答/教学要点按版本显隐 |
| `microknowledge` | `knowledge` | 微型知识点页（编号列表） |
| `knowledgebox` | `conclusion` | 节末结论页（编号列表） |

页面顺序 = 文档顺序（`main.tex` 的 `\input` 顺序 + 切片内顺序）。卡片 id
为 `<sec>-<NN>`（如 `sec1-04`），HTML 中为 `qc-card-<id>`；`seq` 从 1 连续
编号，即"第 N / M 页"。

---

## 二、卡片数据规范（填写规范的机器侧）

`scripts/tex2cards.py` 把 sec*.tex 解析为牌组（`cards.json`，schema
`qchain-html-deck/1`）。卡片字段即"填写规范"：作者写在 TeX 里的内容，只能落在
这些字段里。

```json
{
  "schema": "qchain-html-deck/1",
  "title": "第一章 有限域",
  "source_files": ["sec1.tex", "sec2.tex"],
  "counts": {"cards": 27, "questions": 17, "slots": 48,
             "solutions": 17, "teacher_notes": 17},
  "cards": [
    {
      "id": "sec1-04", "kind": "question", "section": "sec1", "seq": 4,
      "source": "sec1.tex:27",
      "qid": "sec1-q01", "qtype": "进入题", "difficulty": "★☆☆",
      "practice_mode": "guided",
      "lead_html": "（引入本段的过渡语，可空）",
      "stem_html": "题干（含 \\fillin 交互槽位与 \\ansspace 留白）",
      "parts": [{"label": "(a)", "html": "子题内容"}],
      "slots": [{"id": "sec1-q01-s1", "answer": "整环", "width": "20mm",
                 "options": ["整环", "除环", "非交换环"]}],
      "solution_html": "（1）整环；（2）素数；（3）0。",
      "teacher_note_html": "引导学生明确……"
    }
  ]
}
```

字段纪律：

1. **qid 是稳定身份**：每个题目卡必须带 `% qid:` 注释且全章唯一；卡片 id
   只是渲染序号，跨节关联与验收记录一律用 qid。
2. **slot 是交互契约**：`\fillin[答案]{宽度}[选项1/选项2/选项3]` 必须提供
   参考答案并在宽度参数之后配置至少 3 个选项，参考答案（归一化后）必须是
   选项之一，选项不得重复（`check_numbering.py` 与牌组校验双重硬检查）。
   学生版可见形态为"下划线空位 + 选项按钮"，答案文本**不进入可见文本**，
   正确性以 `data-correct="true"` 标记（交互判定所必需）。
3. **solution / teacherNote 每题恰好一个**：缺失即牌组校验失败。学生版
   solution 进入隐藏揭示区，teacherNote 不进入学生版 DOM。
4. **行内转换范围**：`$...$`/`$$...$$`/`\[...\]`/`\(...\)` 数学原样保留
   （KaTeX 渲染）；`\textbf/\textit/\emph/\underline` 等文本宏转 HTML 标签；
   `\ansspace{高度}` → 答题留白；`\includegraphics{路径}` → `assets/` 图片；
   `\dlgteacher{...}` → 教师专属台词（学生版默认隐藏，config
   `dialogue_student_reveal: full` 时显示）；未知宏按源码文本保留并记入
   解析提示（内容不丢失，但应优先改为已支持写法）。
5. **题间文本**：两个题块之间的文本（如"证明策略"引导段）归属下一个题目卡
   的 `lead_html`；节末残留归属节卡。

---

## 三、页面交互契约（必须满足）

check_html.py 与 test_render.py 对以下契约做机械校验：

1. **翻页**：顶栏"下一页"、底部导航"上一页/下一页"、目录抽屉跳转、键盘
   `←/→`、`PgUp/PgDn`、`Home/End`；页码指示 `第 N / M 页`与顶部进度条；
   URL hash `#/<卡片id>` 定位（刷新保持）。
2. **答案揭示**：顶栏"显示答案/隐藏答案"按钮（快捷键 `A`）切换当前页全部
   `.qc-reveal`；教师版初始展开，学生版初始隐藏。按钮 id 固定：
   `qc-btn-prev / qc-btn-next / qc-btn-next2 / qc-btn-answer / qc-btn-toc`，
   舞台 `qc-stage`、进度条 `qc-progress`、指示器 `qc-indicator`。
3. **选择题即时判定**：点击 `.qc-option` 立即以红/绿反馈——正确项绿色 +`✓`，
   错误项红色删除线 +`✗`，同时以虚线高亮正确项；同组仅判定一次；卡片底部
   "重置本页"可重做；每页显示作答统计 `已答 x/y`。
4. **目录**：`qc-btn-toc`（快捷键 `T`/`Esc`）开关抽屉；条目按节分组并高亮
   当前页；学生版目录不显示题型标签。
5. **打印**：`@media print` 展开全部卡片、每卡一页；学生版隐藏揭示区在打印
   中保持隐藏。
6. **数学公式**：KaTeX CDN（jsdelivr，SRI 固定）渲染 `.qc-math` 内的
   `$...$`；加载失败时保留源码文本，一切交互可用。

---

## 四、版本纪律（双源单渲染）

同一牌组渲染两版，差异只允许来自版本开关：

| 内容 | 学生版 | 教师版 |
|---|---|---|
| 参考解答（solution） | 隐藏揭示区（`hidden`），按钮/键盘揭示 | 默认可见，可隐藏 |
| 教学要点（teacherNote） | **不进入 DOM** | 可见（青色区块） |
| 题型 `\qtype` / 难度 `★` | 隐藏 | 可见（卡头标签） |
| `\practicemode{unprompted}` 标签 | 隐藏 | 可见【无提示练习】 |
| `\dlgteacher` 台词 | 默认隐藏（`reveal: full` 时显示） | 可见（灰调区分） |
| `\ansspace` 留白 | 实际书写高度（横线纹理） | 压缩为微小间距 |
| 选择题判定交互 | ✔ | ✔ |
| 目录题型标签 | 不显示 | 显示 |

防泄露底线（compare_versions / build / check_html 三重核查）：学生版
**可见文本**（浏览器默认呈现内容，隐藏揭示区不计）不得出现教师专属标记
（【解】/参考解答/教学要点/评分/Solution/Answer/Proof 等），不得出现任何
solution/teacherNote 正文；两版卡片页集合必须逐卡一致。

---

## 五、反模式（写作与构建两侧）

- 不得手改 `student.html` / `teacher.html`（生成物；改模版或改 TeX 源）。
- 不得把答案、结论写进题干可见区（选项序列之外出现答案骨架 = 疑似泄露，
  compare_versions 报待复核）。
- 不得给 `\fillin` 配少于 3 个选项、或答案不在选项内（硬失败）。
- 不得在切片内改写全局状态（`\setcounter{pdprob}`、`\def\TeacherVersion`
  等，check_numbering 硬检查）；双版差异只能由版本开关产生。
- 图片只用 `\includegraphics` 静态路径；`.pdf` 格式图片无法在 HTML 中显示，
  请用 png/jpg/svg（解析提示会登记缺失资产）。
- 需要更丰富的公式表现时优先写成 TeX 数学（`$...$`），不要手写 HTML 实体。

---

## 六、相关文件

- 模版：`templates/html/deck.template.html`
- 解析器：`scripts/tex2cards.py`（含牌组校验 `validate_deck`）
- 渲染器：`scripts/render_html.py`（版本过滤 + 占位符填充）
- 构建验收：`scripts/build.py`；结构门禁：`scripts/check_html.py`；
  版本比对：`scripts/compare_versions.py`；审读包：
  `scripts/make_review_pack.py`；交付门禁：`scripts/deliver.py`
- 测试：`scripts/test_render.py`（渲染契约）、`scripts/test_runtime.py`
  （证据链回归）
- 创作规范：`references/tex-interface.md`、`prompts/subagent_prompt.md`
