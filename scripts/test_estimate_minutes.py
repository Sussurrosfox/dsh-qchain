#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
test_estimate_minutes.py — 题链学案课时估算器核心单测

覆盖 8 个核心测试用例及 CLI 回归：
1. core 题基础估算（小问 + 填空）
2. 定理证明长链题加成（sub_items >= 4 + theorem keyword -> +2.0）
3. support 题阅读基础时限（base_reading=2.0）
4. reference 题 0 分钟
5. self-study 题不计入 core totals
6. teacher_override 优先级覆盖
7. 对话轮次加成（启用 vs 关闭）
8. 完整输出 JSON 结构符合 estimate_schema.json 字段校验
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# 引入被测模块
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
QCHAIN_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import estimate_minutes  # noqa: E402
import task_struct  # noqa: E402

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class TestEstimateMinutes(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.schema_path = os.path.join(
            QCHAIN_DIR, "references", "contracts", "estimate_schema.json"
        )

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_file(self, rel_path, content):
        p = os.path.join(self.test_dir, rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_01_core_basic_estimation(self):
        """1. core 题基础估算（小问 + 填空）: base(1) + 2*sub_items + 1*fillins"""
        sec_tex = r"""\begin{probchain}
% qid: t-core-1
\item \qtype{基础运算} 计算下列两问：
\begin{enumerate}[(a)]
  \item 计算 $1+2=$\fillin[$3$]{10mm}[1/2/3/4]。
  \item 计算 $2\times 3=$\fillin[$6$]{10mm}[4/5/6/7] 并求平方为 \fillin[$36$]{10mm}[34/35/36/37]。
\end{enumerate}
\begin{solution}
解答区答案 \fillin[fake]{10mm}[a/b/c]
\end{solution}
\begin{teacherNote}
教师讲解注记
\end{teacherNote}
\end{probchain}
"""
        cov_md = """# 覆盖表
| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_1 | sec1 | 运算基础 | t-core-1 | core | guided | t-core-1 | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        tasks = res["tasks"]
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        # sub_items = 2, sub_fillins = 3 (排除 solution 区内填空)
        self.assertEqual(t["sub_items"], 2)
        self.assertEqual(t["sub_fillins"], 3)
        # est_minutes = 1.0 (base) + 2.0 * 2 + 1.0 * 3 = 8.0
        self.assertEqual(t["est_minutes"], 8.0)
        self.assertIsNone(t["override_minutes"])
        self.assertEqual(t["source"], "estimated")

    def test_02_theorem_proof_bonus(self):
        """2. 定理证明长链题加成（sub_items >= 4 + theorem keyword -> +2.0）"""
        sec_tex = r"""\begin{probchain}
% qid: t-thm-long
\item \qtype{长链证明} 证明如下核心定理：
\begin{enumerate}[(a)]
  \item 步骤一 \fillin[$a$]{10mm}[a/b/c]
  \item 步骤二 \fillin[$b$]{10mm}[a/b/c]
  \item 步骤三 \fillin[$c$]{10mm}[a/b/c]
  \item 步骤四 \fillin[$d$]{10mm}[a/b/c]
\end{enumerate}
\begin{solution}
完成证明。
\end{solution}
\begin{teacherNote}
引理注记
\end{teacherNote}

% qid: t-thm-short
\item \qtype{定理特例} 运用上述定理验证特例：
\begin{enumerate}[(a)]
  \item 验证一 \fillin[$1$]{10mm}[1/2/3]
  \item 验证二 \fillin[$2$]{10mm}[1/2/3]
\end{enumerate}
\begin{solution}
特例成立。
\end{solution}
\begin{teacherNote}
说明
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_thm1 | sec1 | 证明推导 | t-thm-long | core | guided | t-thm-long | 5 |
| node_thm2 | sec1 | 特例检验 | t-thm-short | core | guided | t-thm-short | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        tasks = {t["qid"]: t for t in res["tasks"]}

        # 长链题: sub_items=4 >= 4, fillins=4, is_thm=True -> bonus=2.0
        # 1.0 (base) + 2.0*4 (8) + 1.0*4 (4) + 2.0 (proof_bonus) = 15.0
        self.assertEqual(tasks["t-thm-long"]["sub_items"], 4)
        self.assertEqual(tasks["t-thm-long"]["sub_fillins"], 4)
        self.assertEqual(tasks["t-thm-long"]["est_minutes"], 15.0)

        # 短题: sub_items=2 < 4, 虽含"定理"关键词但不触发 bonus
        # 1.0 (base) + 2.0*2 (4) + 1.0*2 (2) + 0.0 = 7.0
        self.assertEqual(tasks["t-thm-short"]["sub_items"], 2)
        self.assertEqual(tasks["t-thm-short"]["sub_fillins"], 2)
        self.assertEqual(tasks["t-thm-short"]["est_minutes"], 7.0)

    def test_03_support_reading_time(self):
        """3. support 题阅读基础时限（base_reading=2.0）"""
        sec_tex = r"""\begin{probchain}
% qid: t-supp-1
\item \qtype{预备支撑} 支撑阅读材料练习：
\begin{enumerate}[(a)]
  \item 试算第一步 \fillin[$x$]{10mm}[x/y/z]。
\end{enumerate}
\begin{solution}
解答
\end{solution}
\begin{teacherNote}
注记
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_supp | sec1 | 预备知识 | t-supp-1 | support | 课内演练 | t-supp-1 | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        t = res["tasks"][0]
        # support: base_reading = 2.0
        # est_minutes = 2.0 (base) + 2.0 * 1 + 1.0 * 1 = 5.0
        self.assertEqual(t["delivery_role"], "support")
        self.assertEqual(t["est_minutes"], 5.0)
        # support 且包含"课内"演练，应计入 totals["support_inclass"]
        self.assertEqual(res["totals"]["support_inclass"], 5.0)

    def test_04_reference_zero_minutes(self):
        """4. reference 题 0 分钟"""
        sec_tex = r"""\begin{probchain}
% qid: t-ref-1
\item \qtype{拓展阅读} 纯课外参考附录：
\begin{enumerate}[(a)]
  \item 自行阅读并思考 \fillin[$opt$]{10mm}[opt/alt/none]。
\end{enumerate}
\begin{solution}
参考内容
\end{solution}
\begin{teacherNote}
附注
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_ref | sec1 | 拓展阅读 | t-ref-1 | reference | 自读 | t-ref-1 | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        t = res["tasks"][0]
        self.assertEqual(t["delivery_role"], "reference")
        self.assertEqual(t["est_minutes"], 0.0)

    def test_05_self_study_not_in_core_totals(self):
        """5. self-study 题不计入 core totals"""
        sec_tex = r"""\begin{probchain}
% qid: t-core-a
\item \qtype{课堂核心} 核心题目：
\begin{enumerate}[(a)]
  \item 算式一 \fillin[$1$]{10mm}[1/2/3]
\end{enumerate}
\begin{solution}
解
\end{solution}
\begin{teacherNote}
注
\end{teacherNote}

% qid: t-self-b
\item \qtype{课后自学} 自学习题：
\begin{enumerate}[(a)]
  \item 自学一 \fillin[$A$]{10mm}[A/B/C]
  \item 自学二 \fillin[$B$]{10mm}[A/B/C]
\end{enumerate}
\begin{solution}
解
\end{solution}
\begin{teacherNote}
注
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_core | sec1 | 核心 | t-core-a | core | guided | t-core-a | 5 |
| node_self | sec1 | 自学巩固 | t-self-b | self-study | 自主完成 | t-self-b | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        tasks = {t["qid"]: t for t in res["tasks"]}

        # core 题: 1 (base) + 2*1 + 1*1 = 4.0
        self.assertEqual(tasks["t-core-a"]["est_minutes"], 4.0)
        # self-study 题: base=0, 2*2 + 1*2 = 6.0
        self.assertEqual(tasks["t-self-b"]["est_minutes"], 6.0)

        # 验证 totals: core totals 仅包含 core 题的 4.0，绝不包含 self-study 的 6.0！
        self.assertEqual(res["totals"]["core"], 4.0)
        # grand_total = opening(5) + closing(5) + buffer(5) + core(4) + support_inclass(0) = 19.0
        self.assertEqual(res["totals"]["grand_total"], 19.0)

    def test_06_teacher_override_priority(self):
        """6. teacher_override 优先级覆盖"""
        sec_tex = r"""\begin{probchain}
% qid: t-over-1
\item \qtype{小题} 虽结构简单但教师指定 18 分钟研讨：
\begin{enumerate}[(a)]
  \item 小问 \fillin[$1$]{10mm}[1/2/3]
\end{enumerate}
\begin{solution}
解
\end{solution}
\begin{teacherNote}
注
\end{teacherNote}

% qid: t-over-2
\item \qtype{小题2} 显式 override 列填 12.5 分钟：
\begin{enumerate}[(a)]
  \item 小问 \fillin[$2$]{10mm}[1/2/3]
\end{enumerate}
\begin{solution}
解
\end{solution}
\begin{teacherNote}
注
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes | override_minutes |
|---|---|---|---|---|---|---|---|---|
| node_o1 | sec1 | 深度研讨 | t-over-1 | core | guided | t-over-1 | 18 min | |
| node_o2 | sec1 | 分组讨论 | t-over-2 | core | guided | t-over-2 | 5 | 12.5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        tasks = {t["qid"]: t for t in res["tasks"]}

        # 任务 1: planned_minutes 填 18，非默认 5，触发 teacher_override
        self.assertEqual(tasks["t-over-1"]["source"], "teacher_override")
        self.assertEqual(tasks["t-over-1"]["override_minutes"], 18.0)
        self.assertEqual(tasks["t-over-1"]["est_minutes"], 18.0)

        # 任务 2: override_minutes 显式为 12.5
        self.assertEqual(tasks["t-over-2"]["source"], "teacher_override")
        self.assertEqual(tasks["t-over-2"]["override_minutes"], 12.5)
        self.assertEqual(tasks["t-over-2"]["est_minutes"], 12.5)

    def test_07_dialogue_turns_bonus(self):
        """7. 对话轮次加成（启用 vs 关闭）"""
        sec_tex = r"""\begin{exampledialogue}[引例认知冲突]
% dialogue: qid=t-dlg-1
\speaker{$\Psi$}{问题的第一步是什么？}
\speaker{$\alpha$}{先画辅助线。}
\speaker{$\beta$}{难道辅助线只有这一种？}
\speaker{$\Psi$}{动笔检验便知！}
\end{exampledialogue}
\begin{probchain}
% qid: t-dlg-1
\item \qtype{引导题} 检验上述猜测：
\begin{enumerate}[(a)]
  \item 第一问 \fillin[$a$]{10mm}[a/b/c]
\end{enumerate}
\begin{solution}
解答
\end{solution}
\begin{teacherNote}
注记
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_dlg | sec1 | 对话引例 | t-dlg-1 | core | guided | t-dlg-1 | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        # 基础题耗时: 1 (base) + 2*1 + 1*1 = 4.0
        # 对话轮次: 4 句 speaker
        # Case A: 对话层启用 dialogue_enabled: true -> 加成 4 * 0.5 = 2.0 分钟 -> 总计 6.0
        self._write_file("config.yaml", "dialogue_enabled: true\n")
        res_on = estimate_minutes.estimate_project(self.test_dir)
        t_on = res_on["tasks"][0]
        self.assertEqual(t_on["est_minutes"], 6.0)

        # Case B: 对话层关闭 dialogue_enabled: false -> 加成 0.0 -> 总计 4.0
        self._write_file("config.yaml", "dialogue_enabled: false\n")
        res_off = estimate_minutes.estimate_project(self.test_dir)
        t_off = res_off["tasks"][0]
        self.assertEqual(t_off["est_minutes"], 4.0)

    def test_08_schema_validation(self):
        """8. 完整输出 JSON 结构符合 estimate_schema.json 字段校验"""
        self.assertTrue(
            os.path.isfile(self.schema_path),
            f"Schema 文件未找到: {self.schema_path}",
        )
        with open(self.schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

        sec_tex = r"""\begin{probchain}
% qid: prob-schema-demo
\item \qtype{证明题} 证明大同构定理：
\begin{enumerate}[(a)]
  \item 步骤一 \fillin[$1$]{10mm}[1/2/3]
  \item 步骤二 \fillin[$2$]{10mm}[1/2/3]
  \item 步骤三 \fillin[$3$]{10mm}[1/2/3]
  \item 步骤四 \fillin[$4$]{10mm}[1/2/3]
  \item 步骤五 \fillin[$5$]{10mm}[1/2/3]
  \item 步骤六 \fillin[$6$]{10mm}[1/2/3]
\end{enumerate}
\begin{solution}
解答
\end{solution}
\begin{teacherNote}
注记
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| N1 | sec1 | 演示同构 | prob-schema-demo | core | guided | prob-schema-demo | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        # 同时也测试自动在 .pd/reports 目录下写入 estimate.json 的能力
        reports_dir = os.path.join(self.test_dir, ".pd", "reports")
        os.makedirs(reports_dir, exist_ok=True)

        res = estimate_minutes.estimate_project(self.test_dir)

        # 1. 验证生成的 .pd/reports/estimate.json 文件存在且内容一致
        est_file = os.path.join(reports_dir, "estimate.json")
        self.assertTrue(os.path.isfile(est_file))
        with open(est_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertEqual(disk_data, res)

        # 2. Schema 深度校验
        if HAS_JSONSCHEMA:
            jsonschema.validate(instance=res, schema=schema)
        else:
            # 手工兜底契约结构校验
            self.assertEqual(res["schema_version"], 1)
            self.assertEqual(res["tool"], "estimate_minutes")
            self.assertIn("coefficients", res)
            self.assertIn("tasks", res)
            self.assertIn("totals", res)
            for k in ("core", "support_inclass", "opening", "closing", "buffer", "grand_total"):
                self.assertIn(k, res["totals"])
                self.assertIsInstance(res["totals"][k], (int, float))

    def test_09_cli_execution(self):
        """9. 命令行 CLI 执行与 --json 产物输出回归测试"""
        sec_tex = r"""\begin{probchain}
% qid: t-cli
\item \qtype{计算} \fillin[$0$]{10mm}[0/1/2]
\begin{solution}
0
\end{solution}
\begin{teacherNote}
注
\end{teacherNote}
\end{probchain}
"""
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_cli | sec1 | 测试 | t-cli | core | guided | t-cli | 5 |
"""
        self._write_file("sec1.tex", sec_tex)
        self._write_file("coverage-map.md", cov_md)

        output_json = os.path.join(self.test_dir, "custom_out.json")
        cmd = [
            sys.executable,
            os.path.join(SCRIPTS_DIR, "estimate_minutes.py"),
            self.test_dir,
            "--json",
            output_json,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"CLI error: {proc.stderr}")
        self.assertTrue(os.path.isfile(output_json))

        with open(output_json, "r", encoding="utf-8") as f:
            cli_data = json.load(f)
        self.assertEqual(cli_data["tool"], "estimate_minutes")
        self.assertEqual(len(cli_data["tasks"]), 1)

    def test_10_missing_tex_fallback_default(self):
        """10. 当切片 TeX 文件不存在或题目无法解析时，回退默认：sub_items=1, sub_fillins=1"""
        # 只提供 coverage-map.md，不写任何 .tex 文件
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | practice_mode | carrier_qid | planned_minutes |
|---|---|---|---|---|---|---|---|
| node_missing | sec_not_found | 缺失文件兜底 | prob-missing-1 | core | guided | prob-missing-1 | 5 |
"""
        self._write_file("coverage-map.md", cov_md)

        res = estimate_minutes.estimate_project(self.test_dir)
        self.assertEqual(len(res["tasks"]), 1)
        t = res["tasks"][0]
        # 回退默认值：sub_items=1, sub_fillins=1
        self.assertEqual(t["sub_items"], 1)
        self.assertEqual(t["sub_fillins"], 1)
        # core 角色: base_reading(1.0) + 2.0*1 + 1.0*1 = 4.0
        self.assertEqual(t["est_minutes"], 4.0)

    def test_11_task_struct_project_scan(self):
        """11. task_struct 多切片工程扫描 (scan_project_structures) 与台账映射 (get_qid_map)"""
        sec1_tex = r"""\begin{probchain}
% qid: p-sec1-1
\item 题目一 \fillin[$a$]{10mm}[a/b]
\end{probchain}
"""
        sec2_tex = r"""\begin{probchain}
% qid: p-sec2-1
\item 题目二
\begin{enumerate}[(a)]
  \item 问1 \fillin[$1$]{10mm}[1/2]
  \item 问2 \fillin[$2$]{10mm}[1/2]
\end{enumerate}
\end{probchain}
"""
        self._write_file("sec1.tex", sec1_tex)
        self._write_file("sec2.tex", sec2_tex)

        structures = task_struct.scan_project_structures(self.test_dir)
        self.assertIn("sec1.tex", structures)
        self.assertIn("sec2.tex", structures)
        self.assertEqual(len(structures["sec1.tex"]), 1)
        self.assertEqual(len(structures["sec2.tex"]), 1)

        qid_map = task_struct.get_qid_map(structures)
        self.assertIn("p-sec1-1", qid_map)
        self.assertIn("p-sec2-1", qid_map)
        self.assertEqual(qid_map["p-sec2-1"]["sub_items"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
