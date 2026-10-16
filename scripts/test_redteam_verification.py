#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
test_redteam_verification.py — 对 qchain 修复工程进行攻击性红队回归复测
针对 qchain-teaching-adversarial-review.md 中发现的 F1~F10 漏洞进行实弹攻击演练：
- ATTACK-1 (F1): 验证 45 分钟课时确认态能否正确贯通至 config.yaml 与 confirmed-spec.json；
- ATTACK-2 (F2-A): 篡改 config.yaml 将 lesson_minutes 改为 9999（试图突破上限或绕过硬预算），验证被拦截；
- ATTACK-3 (F2-B): 篡改 config.yaml 将 delivery_profile 由 teaching 降级为 self_study，验证被拦截；
- ATTACK-4 (F3/F4): 攻击者将 core 任务改标为 support，验证 support 课内耗时被预算公式计入且无法豁免；
- ATTACK-5 (F5): 攻击者在 coverage-map.md 中故意自报 planned_minutes=1，验证被结构估算引擎覆盖；
- ATTACK-6 (F6): 运行 gen_classroom_plan.py --check，验证为纯只读模式且绝不破坏教师手改教案；
- ATTACK-7 (F7): 教案包含占位符时运行 --check，验证被成功拦截；
- ATTACK-8 (F10): Phase 0 断点通过后，验证令牌文件已被物理销毁无法重放；
- ATTACK-9 (F9): 验证 check_budget 报告输出结构化 category 字段。
"""
import os
import sys
import shutil
import tempfile
import json
import hashlib
import unittest
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, ENGINE_ROOT)

import gen_config
import setup_self
import check_budget
import gen_classroom_plan
import estimate_minutes
import task_struct
from workflow.nodes import human_nodes, init_nodes


class TestRedteamAttacks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="qchain_redteam_")
        self.self_dir = os.path.join(self.tmpdir, "self")
        os.makedirs(self.self_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_attack_1_f1_confirmation_plumbing(self):
        """ATTACK-1 (F1): 验证 45 分钟课时确认态能否正确贯通至 config.yaml 与 confirmed-spec.json"""
        setup_self.setup_self(self.self_dir, profile="teaching", title="红队测试", lesson_minutes=45)
        cfg_path = os.path.join(self.self_dir, "config.yaml")
        self.assertTrue(os.path.exists(cfg_path))
        cfg = gen_config.load_config(cfg_path)
        self.assertEqual(int(cfg.get("lesson_minutes")), 45, "F1 失败: config.yaml 未写入确认态 45 分钟！")

        # 模拟 init_nodes 写入 confirmed-spec.json
        state = {
            "self_dir": self.self_dir,
            "project_root": self.tmpdir,
            "user_spec": {
                "main_title": "红队测试",
                "delivery_profile": "teaching",
                "lesson_minutes": 45,
                "core_task_limit": 12,
                "max_student_pages": 6,
                "phase0_token_digest": "sha256:testtoken"
            }
        }
        res = init_nodes.node_t0_init_scaffold(state)
        cs_path = os.path.join(self.self_dir, ".pd", "confirmed-spec.json")
        self.assertTrue(os.path.exists(cs_path))
        with open(cs_path, "r", encoding="utf-8") as f:
            cs = json.load(f)
        self.assertEqual(cs["lesson_minutes"], 45)
        self.assertEqual(cs["delivery_profile"], "teaching")

    def test_attack_2_f2_tamper_lesson_minutes_and_bound(self):
        """ATTACK-2 (F2-A): 篡改 config.yaml 将 lesson_minutes 改为 9999 或与 confirmed-spec 不符"""
        setup_self.setup_self(self.self_dir, profile="teaching", title="红队测试", lesson_minutes=45)
        # 初始化 confirmed-spec.json
        pd_dir = os.path.join(self.self_dir, ".pd")
        os.makedirs(pd_dir, exist_ok=True)
        cs_data = {
            "schema_version": 1,
            "delivery_profile": "teaching",
            "lesson_minutes": 45,
            "core_task_limit": 12,
            "max_student_pages": 6,
            "confirmed_at": "2026-10-14T12:00:00Z",
            "phase0_token_digest": "sha256:dummy",
            "spec_digest": "dummy"
        }
        with open(os.path.join(pd_dir, "confirmed-spec.json"), "w", encoding="utf-8") as f:
            json.dump(cs_data, f)

        cfg_path = os.path.join(self.self_dir, "config.yaml")
        # 攻击 1: 突破上界 9999
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("delivery_profile: teaching\nlesson_minutes: 9999\nmain_title: 恶意测试\n")
        
        cfg = gen_config.load_config(cfg_path)
        errs = gen_config.validate(cfg)
        self.assertTrue(any("超过最大允许上限" in e for e in errs), "F2 上界突破未被拦截！")

        # 攻击 2: 篡改为 90（与 confirmed-spec 的 45 不一致）
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("delivery_profile: teaching\nlesson_minutes: 90\nmain_title: 恶意测试\n")
        cfg = gen_config.load_config(cfg_path)
        errs = gen_config.verify_confirmed_spec(self.self_dir, cfg)
        self.assertTrue(len(errs) > 0, "F2 时长篡改未被 verify_confirmed_spec 拦截！")
        self.assertTrue(any("不匹配" in e or "篡改" in e for e in errs))

    def test_attack_3_f2_tamper_profile_downgrade(self):
        """ATTACK-3 (F2-B): 篡改 config.yaml 将 delivery_profile 降级为 self_study"""
        pd_dir = os.path.join(self.self_dir, ".pd")
        os.makedirs(pd_dir, exist_ok=True)
        cs_data = {
            "schema_version": 1,
            "delivery_profile": "teaching",
            "lesson_minutes": 45,
            "core_task_limit": 12,
            "max_student_pages": 6,
            "confirmed_at": "2026-10-14T12:00:00Z",
            "phase0_token_digest": "sha256:dummy",
            "spec_digest": "dummy"
        }
        with open(os.path.join(pd_dir, "confirmed-spec.json"), "w", encoding="utf-8") as f:
            json.dump(cs_data, f)

        cfg_path = os.path.join(self.self_dir, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("delivery_profile: self_study\nmain_title: 恶意降级\n")

        cfg = gen_config.load_config(cfg_path)
        errs = gen_config.verify_confirmed_spec(self.self_dir, cfg)
        self.assertTrue(len(errs) > 0, "F2 未授权降级未被 verify_confirmed_spec 拦截！")
        self.assertTrue(any("降级" in e for e in errs))

    def test_attack_4_f3_f4_support_counted_in_budget(self):
        """ATTACK-4 (F3/F4): 攻击者将大量题目改标为 support，验证 support 课内耗时被计入且导致超预算"""
        setup_self.setup_self(self.self_dir, profile="teaching", title="红队测试", lesson_minutes=30)
        # 写 coverage-map.md：1 个 core 题，4 个 support 题
        # 结构估算：
        # opening(5) + closing(5) + buffer(5) = 15
        # core 题: base 1 + 2*2 + 1*1 = 6
        # 4 个 support 题 (practice_mode=inclass): 每个 base 2 + 2*2 + 1*1 = 7. 4*7 = 28
        # 总耗时 = 15 + 6 + 28 = 49 分钟 > 30 分钟！
        sec1_tex = r"""
\section{第一节}
\begin{microknowledge}[定义]微知识\end{microknowledge}
\begin{probchain}
% qid: q01
\item \qtype{题目1} 题目1
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\end{enumerate}
% qid: q02
\item \qtype{题目2} 题目2
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\end{enumerate}
% qid: q03
\item \qtype{题目3} 题目3
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\end{enumerate}
% qid: q04
\item \qtype{题目4} 题目4
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\end{enumerate}
% qid: q05
\item \qtype{题目5} 题目5
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\end{enumerate}
\end{probchain}
\begin{knowledgebox}[总结]结论\end{knowledgebox}
"""
        with open(os.path.join(self.self_dir, "sec1.tex"), "w", encoding="utf-8") as f:
            f.write(sec1_tex)

        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题/材料 | delivery_role | practice_mode | carrier_qid | planned_minutes | 是否同知识点首题 | 预计时间 | 推荐讲法 | 易错点 | 来源 | 删改理由 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N1 | sec1 | 目标1 | 问题1 | core | inclass | q01 | 5 | 是 | 5 | 讲法1 | 易错1 | 源1 | 无 |
| N2 | sec1 | 目标2 | 问题2 | support | inclass | q02 | 5 | 否 | 5 | 讲法2 | 易错2 | 源2 | 无 |
| N3 | sec1 | 目标3 | 问题3 | support | inclass | q03 | 5 | 否 | 5 | 讲法3 | 易错3 | 源3 | 无 |
| N4 | sec1 | 目标4 | 问题4 | support | inclass | q04 | 5 | 否 | 5 | 讲法4 | 易错4 | 源4 | 无 |
| N5 | sec1 | 目标5 | 问题5 | support | inclass | q05 | 5 | 否 | 5 | 讲法5 | 易错5 | 源5 | 无 |
"""
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w", encoding="utf-8") as f:
            f.write(cov_md)

        # 运行 check_budget
        res = check_budget.check_budget(self.self_dir, stage="delivery")
        self.assertFalse(res.passed, "F3/F4 攻击失败: support 题未被预算公式纳入，超预算未被拦截！")
        self.assertTrue(any("超标" in p or "超预算" in p for p in res.problems))

    def test_attack_5_f5_self_reported_minutes_overridden_by_ast(self):
        """ATTACK-5 (F5): 攻击者在 coverage-map.md 中虚假自报 planned_minutes=1，验证结构估算引擎按 AST 真实算值"""
        sec1_tex = r"""
\section{大题}
\begin{microknowledge}[定义]微知识\end{microknowledge}
\begin{probchain}
% qid: q01
\item \qtype{大题} 核心大定理推导
\begin{enumerate}[(a)]
\item 小问1 \fillin[A]{2em}[A/B/C]
\item 小问2 \fillin[B]{2em}[A/B/C]
\item 小问3 \fillin[C]{2em}[A/B/C]
\item 小问4 \fillin[D]{2em}[A/B/C]
\end{enumerate}
\end{probchain}
\begin{knowledgebox}[总结]结论\end{knowledgebox}
"""
        with open(os.path.join(self.self_dir, "sec1.tex"), "w", encoding="utf-8") as f:
            f.write(sec1_tex)

        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题/材料 | delivery_role | practice_mode | carrier_qid | planned_minutes | 是否同知识点首题 | 预计时间 | 推荐讲法 | 易错点 | 来源 | 删改理由 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N1 | sec1 | 核心大定理证明 | 核心大定理推导 | core | inclass | q01 | 1 | 是 | 1 | 详讲 | 易错 | 源 | 无 |
"""
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w", encoding="utf-8") as f:
            f.write(cov_md)

        est = estimate_minutes.estimate_project(self.self_dir)
        task = est["tasks"][0]
        # 结构估算: base(1) + 2*4(sub_items) + 1*4(fillins) + 2(proof_bonus) = 15 分钟
        self.assertEqual(task["est_minutes"], 15.0, "F5 失败: 结构估算未能覆盖自报的 1 分钟！")
        self.assertEqual(task["source"], "estimated")
        self.assertIsNone(task["override_minutes"])

    def test_attack_6_f6_classroom_plan_check_is_readonly(self):
        """ATTACK-6 (F6): 运行 gen_classroom_plan.py --check，绝不修改或覆盖教师手改教案"""
        plan_path = os.path.join(self.self_dir, "classroom-plan.md")
        manual_content = """# 教师亲手打磨的精品翻转课堂教案

- **本课核心结果**：学生能够独立推导定理并完成核心变形计算。
- **时间分配**：引入 5 分钟，核心推导 25 分钟，自学拓展 15 分钟。
- **节点清单**：
  1. 概念建构与微知识点拨
  2. 关键引理的分步填空与互动验证
  3. 典型例题变式演练与易错点分析总结
本教案由备课组老师经过精心设计与教学实践打磨，确保每一分钟都有明确的师生认知动作，绝不应被任何自动化脚本静默重写或破坏覆盖。
"""
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(manual_content)

        # 跑 --check 模式
        code = gen_classroom_plan.check_plan(self.self_dir)
        self.assertEqual(code, 0)

        # 检查文件内容是否原封未动
        with open(plan_path, "r", encoding="utf-8") as f:
            current_content = f.read()
        self.assertEqual(current_content, manual_content, "F6 失败: --check 破坏或重写了教师教案！")

    def test_attack_7_f7_classroom_plan_placeholders_blocked(self):
        """ATTACK-7 (F7): 教案包含占位符时运行 --check，成功拦截"""
        plan_path = os.path.join(self.self_dir, "classroom-plan.md")
        placeholder_content = "# 尚未填写的教案\n本节核心结果：______\n时间安排：______\n" + "B" * 200
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(placeholder_content)

        code = gen_classroom_plan.check_plan(self.self_dir)
        self.assertEqual(code, 1, "F7 失败: 包含未替换占位符的教案未被 --check 拦截！")

    def test_attack_8_f10_token_consumed_after_confirm(self):
        """ATTACK-8 (F10): Phase 0 断点通过后，验证令牌文件已被物理销毁"""
        token_dir = os.path.join(self.tmpdir, ".langgraph")
        os.makedirs(token_dir, exist_ok=True)
        token_path = os.path.join(token_dir, "phase0_token.txt")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write("secret123456\n")

        self.assertTrue(os.path.exists(token_path))
        human_nodes._consume_token(self.tmpdir)
        self.assertFalse(os.path.exists(token_path), "F10 失败: 确认后令牌文件未被物理销毁！")

    def test_attack_9_f9_budget_report_structured_category(self):
        """ATTACK-9 (F9): 验证 check_budget 报告输出符合 C3 契约的 category 字段"""
        setup_self.setup_self(self.self_dir, profile="teaching", title="红队测试", lesson_minutes=90)
        cov_md = """| 原文节点 | 学案位置 | 学习目标 | 对应问题/材料 | delivery_role | practice_mode | carrier_qid | planned_minutes | 是否同知识点首题 | 预计时间 | 推荐讲法 | 易错点 | 来源 | 删改理由 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N1 | sec1 | 目标1 | 问题1 | core | inclass | q01 | 5 | 是 | 5 | 推荐讲法 | 易错点 | 源 | 无 |
"""
        with open(os.path.join(self.self_dir, "coverage-map.md"), "w", encoding="utf-8") as f:
            f.write(cov_md)

        res = check_budget.check_budget(self.self_dir, stage="planning")
        d = res.to_dict()
        self.assertIn("category", d, "F9 失败: check_budget 报告未包含 category 字段！")
        self.assertIn(d["category"], ("ok", "coverage", "section", "artifact", "config"))


if __name__ == "__main__":
    unittest.main()
