"""test_faithful_policy.py — 任务卡 11 回归：教学偏好与容量预算改为可选策略。

对应 DATA-CONTRACTS C1（execution_profile / pedagogy_policy / budget_policy /
require_classroom_plan）、C2（问题→默认动作表）、C10（教师课堂选用范围）。

覆盖五组验收断言：
  POL-01 忠实原文的单问核心定理切片在 advisory 下通过，不被强制扩成 4~7 问；
  POL-02 课堂任务按唯一 (section, carrier_qid) 计数，不按原文节点行数；缺 carrier
         不得伪装成一题，必须报数据不足并定位；
  POL-03 全材料超量但教师已选范围达标时通过且原文未删；strict 真实超量仍失败；
  POL-04 硬事实/交付缺陷（结构坏、漏原文主体、确认态被篡改）在任何策略下仍失败，
         关掉教案要求不关闭来源与数学门禁；
  POL-05 缺新策略的旧项目行为不变（legacy=strict）；advisory 警告不派发修复 Agent。

全部用例在 tempfile 虚拟目录内构造工程，绝不写真实项目目录。
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ENGINE_ROOT = os.path.abspath(os.path.join(SCRIPTS, ".."))
for _p in (SCRIPTS, ENGINE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_budget as cb              # noqa: E402
import check_fidelity as cf            # noqa: E402
import check_pedagogy as cp            # noqa: E402
import gen_classroom_plan as gcp       # noqa: E402
import source_inventory as inv         # noqa: E402
import test_issue_routing as tir       # noqa: E402
from workflow import contracts         # noqa: E402
from workflow import execution_policy as ep  # noqa: E402
from workflow import issues as iss     # noqa: E402
from workflow.nodes import build_nodes as bn  # noqa: E402


# ---------------------------------------------------------------------------
# 配置与 fixture 常量
# ---------------------------------------------------------------------------

# 旧项目（无任何新策略键）→ 必须保持 legacy 语义：strict / strict / 要求教案
LEGACY_CFG = ("delivery_profile: teaching\nlesson_minutes: 90\n"
              "max_student_pages: 6\n")
# 新任务：Phase 0 明示 faithful_fast → 教学偏好/预算默认 advisory、教案不要求
FAST_CFG = LEGACY_CFG + "execution_profile: faithful_fast\n"
# 用户显式要求 strict → 逐条恢复硬阻断（必须显式，不得静默）
STRICT_FAST_CFG = (FAST_CFG + "pedagogy_policy: strict\n"
                   "budget_policy: strict\n")

COVERAGE_HEADER = (
    "| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | "
    "practice_mode | carrier_qid | planned_minutes | 推荐讲法 | 易错点 |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)


def cov_row(node, sec, carrier, minutes=10, role="core",
            method="先讲定义再练一题", pitfall="忽略定义域限制"):
    return ("| %s | %s | 目标 | 问题 | %s | guided | %s | %dmin | %s | %s |\n"
            % (node, sec, role, carrier, minutes, method, pitfall))


# 忠实原文的“单问核心定理”切片：勾股定理 3/4 求 5，只有一个小问、答案正确、
# 有具体数值算例。旧门禁会因为“定理 + 小问 < 4”直接判死（必须扩成 4~7 问）。
POL01_SLICE = r"""% -*- coding: utf-8 -*-
% source: SRC-001
\section*{I 勾股定理}

\begin{microknowledge}[勾股定理]
直角三角形两直角边 $a,b$ 与斜边 $c$ 满足 $a^{2}+b^{2}=c^{2}$。
\end{microknowledge}

\begin{probchain}
% qid: sec1-q1 | 题型: 进入题
% source: SRC-001
\item 用勾股定理计算：直角三角形两直角边为 3 与 4（勾股数组 $(3,4,5)$），求斜边 $c$。\fillin[5]
\begin{solution}
由 $c^{2}=3^{2}+4^{2}=25$ 得 $c=5$。
\end{solution}
\end{probchain}

\begin{knowledgebox}[本节结论]
勾股定理：$a^{2}+b^{2}=c^{2}$；反之亦可用于判定直角三角形。
\end{knowledgebox}
"""


class Base(unittest.TestCase):
    """临时工程基座：所有写入都落在 tempfile 目录。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_pol_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(os.path.join(self.self_dir, ".pd"), exist_ok=True)

    def write(self, rel, text, root=None):
        path = os.path.join(root or self.self_dir, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def read(self, rel, root=None):
        with open(os.path.join(root or self.self_dir, rel),
                  encoding="utf-8") as f:
            return f.read()

    def run_tool(self, entry, extra=(), workdir=None):
        """跑一次门禁脚本 main，返回 (rc, report)。"""
        out = os.path.join(self.d, "report-%s.json" % entry.__module__)
        rc = entry([workdir or self.self_dir, "--json", out] + list(extra))
        with open(out, encoding="utf-8") as f:
            return rc, json.load(f)

    @staticmethod
    def errors(rep):
        return [i for i in rep["issues"] if i["severity"] == "error"]

    @staticmethod
    def warnings(rep):
        return [i for i in rep["issues"] if i["severity"] == "warning"]

    @staticmethod
    def texts(items):
        return "\n".join(i["message"] for i in items)

    def write_selection(self, carriers, schema_version=1):
        payload = {"schema_version": schema_version,
                   "selected_carriers": [{"section": s, "qid": q}
                                         for s, q in carriers]}
        self.write(os.path.join(".pd", "classroom-selection.json"),
                   json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# POL-01：单问核心定理切片（忠实原文）在 advisory 下通过
# ---------------------------------------------------------------------------

class POL01_SingleQuestionTheorem(Base):

    def project(self, cfg):
        self.write("config.yaml", cfg)
        self.write("sec1.tex", POL01_SLICE)

    def test_advisory_does_not_force_four_subquestions(self):
        """POL-01：正确的 3/4→5 单问切片在 faithful_fast+advisory 下通过（只提示）。"""
        self.project(FAST_CFG)
        rc, rep = self.run_tool(cp.main)
        self.assertEqual(self.errors(rep), [], rep["issues"])
        self.assertEqual(rc, 0, rep["issues"])
        self.assertEqual(rep["status"], "pass")
        warn = self.texts(self.warnings(rep))
        self.assertIn("认知步长", warn)          # 仍提示，但不再是阻断
        self.assertEqual(rep["policy"]["pedagogy_policy"], "advisory")

    def test_legacy_project_still_blocks_single_question_theorem(self):
        """POL-05：缺新策略的旧项目保持 legacy 语义（单问定理仍阻断）。"""
        self.project(LEGACY_CFG)
        rc, rep = self.run_tool(cp.main)
        self.assertEqual(rc, 1)
        err = self.texts(self.errors(rep))
        self.assertIn("认知步长", err)
        self.assertIn("至少 4~7", err)
        self.assertEqual(rep["policy"]["pedagogy_policy"], "strict")

    def test_explicit_strict_keeps_blocking_under_fast_profile(self):
        """POL-01：strict 必须显式声明，声明后逐条恢复硬阻断。"""
        self.project(STRICT_FAST_CFG)
        rc, rep = self.run_tool(cp.main)
        self.assertEqual(rc, 1)
        self.assertIn("认知步长", self.texts(self.errors(rep)))

    def test_correct_answer_and_numeric_example_are_not_preference_items(self):
        """POL-01：数值算例存在时不得再报“缺算例”，答案正确不得被改写要求触发。"""
        self.project(FAST_CFG)
        rc, rep = self.run_tool(cp.main)
        self.assertNotIn("缺少具体数值", self.texts(self.errors(rep)))
        self.assertNotIn("缺少具体数值", self.texts(self.warnings(rep)))


# ---------------------------------------------------------------------------
# POL-02：任务计数按唯一 (section, carrier_qid)
# ---------------------------------------------------------------------------

class POL02_UniqueCarrierCounting(Base):

    def budget(self, rows_text, cfg="delivery_profile: self_study\n"):
        self.write("config.yaml", cfg)
        self.write("coverage-map.md", COVERAGE_HEADER + rows_text)
        return cb.check_budget_project(self.self_dir, "planning")

    def test_thirteen_source_nodes_sharing_one_carrier_count_as_one_task(self):
        """POL-02：13 个原文节点共用同一 carrier → 只算 1 个课堂任务。"""
        rows = "".join(cov_row("SRC-%02d" % i, "sec1", "q1")
                       for i in range(1, 14))
        res = self.budget(rows)
        self.assertEqual(res.stats["core_task_count"], 1, res.stats)
        self.assertEqual(res.stats["core_task_rows"], 13, res.stats)
        self.assertEqual(res.problems, [], res.problems)

    def test_distinct_carriers_still_hit_core_task_limit(self):
        """POL-02：去重不能把真实题量也吞掉——13 个不同 carrier 仍超 12 上限。"""
        rows = "".join(cov_row("SRC-%02d" % i, "sec1", "q%d" % i)
                       for i in range(1, 14))
        res = self.budget(rows)
        self.assertEqual(res.stats["core_task_count"], 13, res.stats)
        self.assertIn("core 核心任务数量超标", "\n".join(res.problems))

    def test_same_qid_in_different_sections_counts_twice(self):
        """POL-02：不同 section 的同名 qid 是两个任务。"""
        rows = cov_row("SRC-01", "sec1", "q1") + cov_row("SRC-02", "sec2", "q1")
        res = self.budget(rows)
        self.assertEqual(res.stats["core_task_count"], 2, res.stats)

    def test_empty_carrier_cell_is_data_insufficiency_not_a_fake_task(self):
        """POL-02：有 carrier 列但留空 → 报数据不足并定位，不得回落成一道题。"""
        rows = cov_row("SRC-01", "sec1", "") + cov_row("SRC-02", "sec1", "q2")
        res = self.budget(rows)
        joined = "\n".join(res.problems)
        self.assertIn("carrier_qid", joined)
        self.assertIn("SRC-01", joined)
        self.assertEqual(res.issue_meta[res.problems[0]]["code"],
                         "BUDGET_CARRIER_MISSING")

    def test_plan_without_carrier_column_keeps_legacy_fallback(self):
        """POL-02：整张表没有 carrier 列时沿用旧回落（不新增阻断）。"""
        header = ("| 原文节点 | 学案位置 | 学习目标 | 对应问题 | delivery_role | "
                  "planned_minutes | 推荐讲法 | 易错点 |\n"
                  "|---|---|---|---|---|---|---|---|\n")
        row = ("| SRC-01 | sec1 | 目标 | 问题 | core | 10min | 先讲再练 | 符号错 |\n")
        self.write("config.yaml", "delivery_profile: self_study\n")
        self.write("coverage-map.md", header + row)
        res = cb.check_budget_project(self.self_dir, "planning")
        self.assertEqual(res.stats["core_task_count"], 1, res.stats)
        self.assertEqual(res.problems, [], res.problems)


# ---------------------------------------------------------------------------
# POL-03：教师选用范围（C10）
# ---------------------------------------------------------------------------

class POL03_TeacherSelectionScope(Base):

    def project(self, cfg, n_rows=10, minutes=10):
        self.write("config.yaml", cfg)
        rows = "".join(cov_row("SRC-%02d" % i, "sec%d" % ((i - 1) % 3 + 1),
                               "q%d" % i, minutes=minutes)
                       for i in range(1, n_rows + 1))
        self.write("coverage-map.md", COVERAGE_HEADER + rows)
        return rows

    def test_advisory_reports_full_capacity_without_blocking(self):
        """POL-03：全材料 115 分钟 > 90，advisory 只提示全量容量与未指定选择。"""
        self.project(FAST_CFG)
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(self.errors(rep), [], rep["issues"])
        self.assertEqual(rc, 0)
        warn = self.texts(self.warnings(rep))
        self.assertIn("未指定课堂选择", warn)
        self.assertEqual(rep["evidence"]["budget_stats"]["classroom_selection"],
                         "unspecified")

    def test_selected_scope_within_budget_passes_and_original_is_kept(self):
        """POL-03：已选 3 个载体（45 分钟）达标 → 通过，且原文/未选项未被删除。"""
        rows = self.project(FAST_CFG)
        before = self.read("coverage-map.md")
        self.write_selection([("sec1", "q1"), ("sec2", "q2"), ("sec3", "q3")])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(self.errors(rep), [], rep["issues"])
        self.assertEqual(rc, 0)
        stats = rep["evidence"]["budget_stats"]
        self.assertEqual(stats["classroom_selection"], "specified")
        self.assertEqual(stats["classroom_selection_tasks"], 3, stats)
        self.assertLessEqual(stats["selected_minutes"], 90)
        self.assertEqual(self.read("coverage-map.md"), before)   # 原文未删未改
        self.assertEqual(before.count("\n| SRC-"), 10)

    def test_strict_uses_selected_scope_and_passes(self):
        """POL-03：strict 按已确认选用范围算预算 → 45 <= 90 通过。"""
        self.project(STRICT_FAST_CFG)
        self.write_selection([("sec1", "q1"), ("sec2", "q2"), ("sec3", "q3")])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(self.errors(rep), [], rep["issues"])
        self.assertEqual(rc, 0)

    def test_strict_without_selection_uses_full_scope_and_fails(self):
        """POL-03：strict 且未指定选择 → 按全量算（115 > 90）必须失败。"""
        self.project(STRICT_FAST_CFG)
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(rc, 1)
        self.assertIn("课堂教学耗时超标", self.texts(self.errors(rep)))

    def test_strict_selected_scope_that_really_overflows_fails(self):
        """POL-03：strict 下已选范围真实超量（8 题 95 分钟）仍然失败。"""
        self.project(STRICT_FAST_CFG)
        self.write_selection([("sec%d" % ((i - 1) % 3 + 1), "q%d" % i)
                              for i in range(1, 9)])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(rc, 1)
        self.assertIn("课堂教学耗时超标", self.texts(self.errors(rep)))

    def test_unknown_selected_carrier_is_rejected_with_location(self):
        """C10：所选 (section,qid) 必须存在，不存在项拒绝并定位。"""
        self.project(FAST_CFG)
        self.write_selection([("sec1", "q1"), ("sec9", "q404")])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        self.assertEqual(rc, 1)
        err = self.texts(self.errors(rep))
        self.assertIn("q404", err)
        codes = {i.get("code") for i in self.errors(rep)}
        self.assertIn("SELECTION_CARRIER_UNKNOWN", codes, rep["issues"])

    def test_duplicate_selection_entries_are_deduped_and_reported(self):
        """C10：重复条目去重后统计，并在 stats 中报告重复数。"""
        self.project(FAST_CFG)
        self.write_selection([("sec1", "q1"), ("sec1", "q1"), ("sec2", "q2")])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        stats = rep["evidence"]["budget_stats"]
        self.assertEqual(stats["classroom_selection_tasks"], 2, stats)
        self.assertEqual(stats["classroom_selection_duplicates"], 1, stats)
        self.assertEqual(rc, 0, rep["issues"])

    def test_empty_selection_means_zero_selected_not_missing(self):
        """C10：空数组=明确选择零题，与“文件不存在”不同。"""
        self.project(FAST_CFG)
        self.write_selection([])
        rc, rep = self.run_tool(cb.main, extra=["--stage", "delivery"])
        stats = rep["evidence"]["budget_stats"]
        self.assertEqual(stats["classroom_selection"], "specified", stats)
        self.assertEqual(stats["classroom_selection_tasks"], 0, stats)
        self.assertEqual(rc, 0, rep["issues"])


# ---------------------------------------------------------------------------
# POL-04：硬事实/交付缺陷在任何策略下仍失败
# ---------------------------------------------------------------------------

class POL04_FactGatesSurviveAdvisory(Base):

    def test_unclosed_knowledgebox_still_fails_under_advisory(self):
        """POL-04：坏结构（环境未闭合）不是教学偏好 → advisory 下仍失败。"""
        broken = POL01_SLICE.replace("\\end{knowledgebox}", "")
        self.write("config.yaml", FAST_CFG)
        self.write("sec1.tex", broken)
        rc, rep = self.run_tool(cp.main)
        self.assertEqual(rc, 1)
        self.assertIn("未闭合", self.texts(self.errors(rep)))

    def test_missing_microknowledge_still_fails_under_advisory(self):
        """POL-04：缺前置知识卡（学生无法自学）→ advisory 下仍失败。"""
        broken = POL01_SLICE.replace("microknowledge", "notacard")
        self.write("config.yaml", FAST_CFG)
        self.write("sec1.tex", broken)
        rc, rep = self.run_tool(cp.main)
        self.assertEqual(rc, 1)
        self.assertIn("前置微知识卡", self.texts(self.errors(rep)))

    def test_confirmed_spec_tamper_still_fails_under_advisory(self):
        """POL-04：确认态被篡改（90→120）→ advisory 下仍阻断。"""
        self.write("config.yaml", FAST_CFG.replace("lesson_minutes: 90",
                                                   "lesson_minutes: 120"))
        self.write(os.path.join(".pd", "confirmed-spec.json"),
                   json.dumps({"delivery_profile": "teaching",
                               "lesson_minutes": 90}, ensure_ascii=False))
        self.write("coverage-map.md", COVERAGE_HEADER + cov_row("SRC-01", "sec1", "q1"))
        res = cb.check_budget_project(self.self_dir, "delivery")
        self.assertEqual(res.category, "config")
        self.assertIn("确认态对账失败", "\n".join(res.problems))

    def test_no_classroom_plan_required_but_source_gate_stays_closed(self):
        """POL-04：关掉教案要求不关闭来源门禁——漏原文主体仍 FAIL。"""
        root = os.path.join(self.d, "proj")
        os.makedirs(root)
        with open(os.path.join(root, "8.md"), "w", encoding="utf-8") as f:
            f.write("# 08-矩阵函数\n\n## 一、矩阵函数\n\n"
                    "1. $f(A)=Pf(D)P^{-1}$ 与幂级数定义。\n"
                    "2. $M'=AM$ 与谱方法。\n")
        manifest = contracts.build_source_manifest(
            project_root=root, source_files=["8.md"], excluded_sources=[])
        contract = contracts.build_intent_contract(
            user_spec={"content_policy": "faithful"})
        contracts.write_contracts(self.self_dir, manifest, contract)
        inventory = inv.build_inventory(manifest, root)
        inv.write_inventory(self.self_dir, inventory)
        # 只覆盖第一个 preserve 单元，故意漏掉其余原文主体
        preserve_ids = [u["source_id"] for u in inventory["units"]
                        if u["disposition"] == "preserve"]
        self.assertGreaterEqual(len(preserve_ids), 2, preserve_ids)
        self.write("source-map.md",
                   "%s -> sec1 -> EXPLAIN -> EXPLAINED -> 改写讲解\n"
                   % preserve_ids[0])
        self.write("coverage-map.md", COVERAGE_HEADER + cov_row("SRC-01", "sec1", "q1"))
        res = cf.check_fidelity_project(self.self_dir)
        codes = sorted({e["code"] for e in res["errors"]})
        self.assertIn("FIDELITY_SOURCE_NOT_COVERED", codes, codes)

    def test_delivery_without_plan_passes_when_plan_not_required(self):
        """POL-04：require_classroom_plan=false → 缺教案不再阻断（只提示）。"""
        self.write("config.yaml", FAST_CFG)
        self.write("coverage-map.md", COVERAGE_HEADER + cov_row("SRC-01", "sec1", "q1"))
        res = cb.check_budget_project(self.self_dir, "delivery")
        self.assertEqual(res.problems, [], res.problems)
        self.assertEqual(res.stats["classroom_plan"], "not-required")
        self.assertIn("未要求课堂计划", "\n".join(res.warnings))

    def test_delivery_without_plan_still_fails_when_plan_required(self):
        """POL-04：legacy（要求教案）→ 缺教案仍按 CLASSROOM_PLAN_MISSING 阻断。"""
        self.write("config.yaml", LEGACY_CFG)
        self.write("coverage-map.md", COVERAGE_HEADER + cov_row("SRC-01", "sec1", "q1"))
        res = cb.check_budget_project(self.self_dir, "delivery")
        self.assertIn("CLASSROOM_PLAN_MISSING",
                      json.dumps(res.issue_meta, ensure_ascii=False))
        self.assertEqual(res.category, "artifact")

    def test_generate_plan_does_not_block_when_plan_not_required(self):
        """POL-04：教案不要求时，生成失败不再让确定性步骤 HALT（返回 0）。"""
        self.write("config.yaml", FAST_CFG)
        self.write("coverage-map.md", "# 覆盖表（无表格）\n\n无有效节点。\n")
        self.assertEqual(gcp.generate(self.self_dir), 0)
        self.write("config.yaml", LEGACY_CFG)
        self.assertNotEqual(gcp.generate(self.self_dir), 0)


# ---------------------------------------------------------------------------
# POL-05：legacy 兼容 + advisory 警告不派发修复 Agent
# ---------------------------------------------------------------------------

class POL05_LegacyCompatAndNoAgent(Base):

    def test_missing_policy_keys_resolve_to_legacy_defaults(self):
        """POL-05：缺新策略的旧项目 → legacy / strict / strict / 要求教案。"""
        policy = cp.resolve_policy({})
        self.assertEqual(policy["execution_profile"], "legacy")
        self.assertEqual(policy["pedagogy_policy"], "strict")
        self.assertEqual(policy["budget_policy"], "strict")
        self.assertIs(policy["require_classroom_plan"], True)
        self.assertEqual(policy["sources"]["pedagogy_policy"], "default")

    def test_fast_profile_defaults_to_advisory(self):
        """POL-05：faithful_fast 默认 advisory，不要求教案。"""
        policy = cp.resolve_policy({"execution_profile": "faithful_fast"})
        self.assertEqual(policy["pedagogy_policy"], "advisory")
        self.assertEqual(policy["budget_policy"], "advisory")
        self.assertIs(policy["require_classroom_plan"], False)

    def test_explicit_key_overrides_profile_default(self):
        """POL-05：显式键优先于 profile 默认，逐条可混搭。"""
        policy = cp.resolve_policy({"execution_profile": "faithful_fast",
                                    "budget_policy": "strict"})
        self.assertEqual(policy["pedagogy_policy"], "advisory")
        self.assertEqual(policy["budget_policy"], "strict")
        self.assertEqual(policy["sources"]["budget_policy"], "explicit")

    def test_unknown_profile_or_value_is_rejected(self):
        """POL-05：未知 profile/非法取值必须显式报错，不得静默降级。"""
        with self.assertRaises(ep.ExecutionPolicyError):
            cp.resolve_policy({"execution_profile": "turbo"})
        with self.assertRaises(ep.ExecutionPolicyError):
            cp.resolve_policy({"pedagogy_policy": "warn"})

    def test_advisory_warning_report_does_not_dispatch_agent(self):
        """POL-05：advisory 警告（status=pass）→ PROCEED，不派修复 Agent。"""
        rep = {"status": "pass", "passed": True, "gate_name": "check_pedagogy",
               "issues": [{"severity": "warning",
                           "message": "sec1.tex:5: 核心定理/引理大题认知步长过大"}]}
        decision = iss.route_gate_report(rep, "check_pedagogy")
        self.assertEqual(decision["action"], "PROCEED")
        self.assertFalse(decision["dispatch_agent"])

    def test_same_warning_blocks_only_when_strict_is_explicit(self):
        """POL-05：同一警告只在 strict 显式模式下阻断（strict_blocks 规则）。"""
        raw = {"severity": "warning",
               "message": "sec1.tex:5: 核心定理/引理大题认知步长过大"}
        advisory = iss.normalize_issue(dict(raw), gate="check_pedagogy")
        self.assertFalse(advisory["blocking"])
        self.assertEqual(advisory["repair_kind"], "none")
        strict = iss.normalize_issue(dict(raw), gate="check_pedagogy", strict=True)
        self.assertTrue(strict["blocking"])


# ---------------------------------------------------------------------------
# POL-06（T3 集成）：教案可选、advisory 只提示不派 Agent、硬门禁不受影响
# ---------------------------------------------------------------------------

class POL06_T3OptionalPlanAndAdvisoryNotRepair(unittest.TestCase):
    """在 T3 节点层验证：关掉教案要求 ≠ 关掉来源/数学门禁。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_pol_t3_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(os.path.join(self.self_dir, ".pd", "reports"), exist_ok=True)
        tir.FakeGateRunner.overrides = {}
        tir.FakeGateRunner.calls = []
        tir.FakeScriptRunner.gate_reports = {}
        tir.FakeScriptRunner.scripts = []
        for name, fake in (("GateRunner", tir.FakeGateRunner),
                           ("ScriptRunner", tir.FakeScriptRunner)):
            p = mock.patch.object(bn, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.state = {"self_dir": self.self_dir, "history": [], "run_id": "r1",
                      "expected_sections": ["sec1", "sec2", "sec3"]}

    def project(self, cfg):
        with open(os.path.join(self.self_dir, "config.yaml"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(cfg)
        rows = [cov_row("SRC-%02d" % i, "sec%d" % ((i - 1) % 3 + 1),
                        "q%d" % i, minutes=10) for i in range(1, 11)]
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(COVERAGE_HEADER + "".join(rows))

    def real_budget_report(self):
        """真实 check_budget.main(--stage delivery) → GateReport 形状。"""
        path = os.path.join(self.self_dir, ".pd", "reports", "budget.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cb.main([self.self_dir, "--stage", "delivery", "--json", path])
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return tir.rep("check_budget", "PASS" if code == 0 else "REJECT", raw=raw,
                       issues=[{"category": raw.get("category", "UNKNOWN"),
                                "severity": i["severity"],
                                "message": i["message"]}
                               for i in raw["issues"]],
                       category=raw.get("category"))

    def test_fast_profile_skips_plan_and_advisory_warning_is_not_a_repair(self):
        """faithful_fast：不预生成教案；超量只提示 → PROCEED，不派修复 Agent。"""
        self.project(FAST_CFG)
        tir.FakeGateRunner.overrides = {"check_budget": self.real_budget_report()}
        patch = bn.node_t3_checks(dict(self.state))
        self.assertNotIn("gen_classroom_plan.py", tir.FakeScriptRunner.scripts)
        self.assertEqual(patch["routing_action"], "PROCEED", patch)
        raw = tir.FakeGateRunner.overrides["check_budget"]["raw_report"]
        severities = {i["severity"] for i in raw["issues"]}
        self.assertEqual(severities, {"warning"}, raw["issues"])
        self.assertIn("未指定课堂选择",
                      json.dumps(raw["issues"], ensure_ascii=False))

    def test_legacy_profile_still_prepares_plan_before_check(self):
        """legacy：仍先确定性生成教案再检查（旧行为不变）。"""
        self.project(LEGACY_CFG)
        patch = bn.node_t3_checks(dict(self.state))
        self.assertIn("gen_classroom_plan.py", tir.FakeScriptRunner.scripts)
        self.assertEqual(patch["routing_action"], "PROCEED", patch)

    def test_source_gate_still_blocks_when_plan_not_required(self):
        """faithful_fast：来源门禁报错仍然阻断（教案可选不放松硬门禁）。"""
        self.project(FAST_CFG)
        tir.FakeGateRunner.overrides = {
            "check_fidelity": tir.raw_issue_report(
                "check_fidelity", "SRC-003 原文主体未覆盖")}
        patch = bn.node_t3_checks(dict(self.state))
        self.assertNotEqual(patch["routing_action"], "PROCEED", patch)
        self.assertEqual(patch["routing_action"], "REPLAN", patch)
        self.assertEqual(patch["repair_target"], "source-map.md", patch)
        self.assertEqual(patch["stage"], "REPAIR_LOOP", patch)


if __name__ == "__main__":
    unittest.main()
