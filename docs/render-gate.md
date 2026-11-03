# 渲染质量门禁接线补充（§19）

`scripts/check_render.py`（TOOL_VERSION 1.0）已于本轮新建并接入五处：

1. `workflow/adapters/gate_runner.py`：`PURE_PYTHON_GATES` 新增 `"check_render": "check_render.py"`（由 `node_t3_build` 单独执行，因其需渲染产物就绪）；
2. `workflow/nodes/build_nodes.py`：`_run_render_gate()` 在 `_run_build()` 成功后运行，FAIL 即阻断本切片；
3. `scripts/deliver.py`：`CHECKS` 表在 `build.py` 之后插入 `("check_render.py", "check_render.json", ["--format", "{fmt}"])`，`MUST_PASS` 纳入 `check_render.json`，`{fmt}` 在运行期替换；
4. `index.js`（qchain_gate）：新增 `render` 门禁项与 enum 值，`all` 汇总在 `check_html` 之后、`compare` 之前调用；
5. `scripts/test_runtime.py`：`seed_reports()` 预置 `check_render.json` PASS 证据，保持 `--skip-checks` 回归路径完整。

设计要点：结构门禁（check_numbering / check_html）只校验卡片数、槽位数与源码级约束，
查不出"渲染出来的字是不是字"。本门禁补上六类渲染级失败：HTML 双重转义、center/tabular
压平残留、.qc-math 段 $ 不成对、KaTeX 宏白名单、选项汉字守恒（源级 \fillin 汉字必须出现在
HTML 按钮与 PDF 文本层中）、KaTeX 本地自足（禁外网 CDN），外加 PDF 日志 Missing
character、U+FFFD 不可见字形、空选项组（）。

## v1.1 三项补强（真实事故复盘）

1. **H2 扩充：TikZ 泄漏即 FAIL。** `RESIDUAL` 追加 `\begin{tikzcd}` / `\end{tikzcd}` /
   `\begin{tikzpicture}` / `\end{tikzpicture}` / `\usetikzlibrary` / `\arrow[` 六个模式，
   任一命中 HTML body 即 problem。不变量：TikZ 必须栅格化为 PNG 经 `\includegraphics`
   进 sec*.tex，HTML 中出现原始 tikz 源码永远是泄漏。
2. **P1 增强：CJK 缺字形聚合报告。** 解析日志行
   `Missing character: There is no 必 (U+5FC5) in font ...` 提取缺失字符，用
   `collections.Counter` 按字符聚合，problem 消息列出 top 10（如 `必×12, 然×8`），
   无字符明细的行计入 `（未识别）`；聚合同步落入 evidence 的 `missing_chars` 字段。
   判定阈值不变（>0 即 problem），但报告可直接指导修字体/修 `\text{}` 包裹。
3. **H7 新增：cards.json 槽位守恒。** 事故中 tex2cards 丢失编号列表槽位（HTML 实际
   42 槽 vs PDF 66 槽），H4 的粗粒度 `<` 检查没抓住。H7 做三层对账：
   - 牌组层：`cards.json`（schema `qchain-html-deck/1`，学生/教师两版 HTML 的共同
     数据源）全部卡片 `slots` 总数必须 == sec*.tex 的 `\fillin` 数（`RENDER_SLOT_LOST`）；
   - 选项层：牌组选项按钮总数必须 == 源级选项数（`RENDER_OPTION_LOST`，精确相等，
     比 H4 的 `<` 更严）；
   - 渲染层：学生/教师版 HTML 各自的 `qc-slot` span 数必须 == 源级 `\fillin` 数。
   cards.json 缺失、非法 JSON 或 schema 不符预期（缺 `cards` 数组 / `slots` 非数组）
   即 problem——门禁输入不完整，不许静默跳过。对账计数落入 evidence 的
   `cards_slots` / `cards_options` / `html_slot_spans` 字段。

TOOL_VERSION 升 1.1。回归：`scripts/test_check_render.py`（9 例，含守恒基线不误报）
与 `scripts/test_render.py`（10 例）全绿；GR_4 self/ 实测 PASS（cards 槽位 69 ==
源级 69，选项 208 == 208，HTML 双版各 69 槽）。
