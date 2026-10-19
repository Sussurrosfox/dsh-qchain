#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_scope.py — audit §7 scope_gate（scripts/check_scope.py）回归

覆盖：
    T01  合同缺失 / 损坏 → SCOPE_CONTRACT_MISSING + fatal
    T02  合同被改写 → SCOPE_CONTRACT_TAMPERED（规范化摘要对账）
    T03  交付模式漂移 → SCOPE_PROFILE_DRIFT
    T04  课时预算漂移 → SCOPE_MINUTES_DRIFT（self_study 生效口径=无上限）
    T05  未经确认的降级 → SCOPE_UNAUTHORIZED_DOWNGRADE
    T06  内容策略漂移 → SCOPE_POLICY_DRIFT
    T07  未批准范围变更 → SCOPE_CHANGE_UNAPPROVED（批准后放行）
    T08  未规划小节 → SCOPE_UNPLANNED_SECTION
    T09  核心预算超限 → SCOPE_CORE_BUDGET_OVERFLOW
    T10  must_cover 为空 → SCOPE_MUST_COVER_EMPTY 警告
    T11  干净工程全绿 + CLI 报告契约（schema_version 2 / input_digest）
    T12  T3 门禁批量接线（check_scope 在 GateRunner PURE_PYTHON_GATES 内）

运行: python -m unittest discover scripts -p "test_scope.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import check_scope  # noqa: E402
from workflow import contracts  # noqa: E402

import test_section_batch as tsb  # noqa: E402  (复用 write_config 治具)


COVERAGE_TEMPLATE = """# 覆盖规划（scope 门禁测试）

切片清单: {sections}

## 统计与规划指标

- 小节数量: {count}
- 核心推导总分钟数: {core} 分钟
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_project(d, *, profile="self_study", minutes=None, policy="faithful",
                 must_cover=None, sections=("sec1", "sec2"), core_minutes=137,
                 config_overrides=None, freeze=True):
    """构造带 Phase 0 契约的最小工程，返回 (self_dir, contract)。"""
    root = os.path.join(d, "proj")
    self_dir = os.path.join(root, "self")
    os.makedirs(self_dir)
    _write(os.path.join(root, "source.md"),
           "# 原材料\n\n第一段原文内容。\n\n第二段原文内容。\n\n第三段。\n")
    spec = {
        "main_title": "范围门禁测试",
        "delivery_profile": profile,
        "lesson_minutes": minutes,
        "content_policy": policy,
        "must_cover": must_cover if must_cover is not None
        else ["线性递推", "特征多项式"],
    }
    manifest = contracts.build_source_manifest(
        project_root=root, source_files=["source.md"], title=spec["main_title"])
    contract = contracts.build_intent_contract(user_spec=spec)
    if freeze:
        contracts.write_contracts(self_dir, manifest, contract)
    _write(os.path.join(self_dir, "main.tex"),
           "\\input{main-setup.tex}\n"
           + "".join("\\input{%s.tex}\n" % s for s in sections)
           + "\\end{document}\n")
    _write(os.path.join(self_dir, "coverage-map.md"),
           COVERAGE_TEMPLATE.format(
               sections=", ".join(sections), count=len(sections),
               core=core_minutes))
    cfg = {
        "output_format": "both",
        "delivery_profile": profile,
        "lesson_minutes": minutes if minutes is not None else "",
        "content_policy": policy,
        "run_scope": "full",
    }
    cfg.update(config_overrides or {})
    tsb.write_config(self_dir, **cfg)
    return self_dir, contract


def codes(result):
    return [e.get("code") for e in result.get("errors", [])]


class T01ContractPresence(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_missing_contract_is_fatal(self):
        self_dir, _ = make_project(self.d, freeze=False)
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_CONTRACT_MISSING", codes(result))
        self.assertTrue(result.get("fatal"))
        self.assertEqual(result["errors"][0]["code"], "SCOPE_CONTRACT_MISSING")

    def test_corrupt_contract_is_fatal(self):
        self_dir, _ = make_project(self.d)
        with open(os.path.join(self_dir, ".pd", "intent-contract.json"),
                  "w", encoding="utf-8") as f:
            f.write("{ not json")
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_CONTRACT_MISSING", codes(result))
        self.assertTrue(result.get("fatal"))


class T02ContractTamper(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir, self.contract = make_project(self.d)

    def test_tampered_contract_detected(self):
        path = os.path.join(self.self_dir, ".pd", "intent-contract.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["lesson_minutes"] = 999  # 改写但 digest 未重算
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        result = check_scope.check_scope_project(self.self_dir)
        self.assertIn("SCOPE_CONTRACT_TAMPERED", codes(result))

    def test_clean_contract_passes(self):
        result = check_scope.check_scope_project(self.self_dir)
        self.assertEqual(codes(result), [])


class T03ProfileDrift(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_profile_drift(self):
        self_dir, _ = make_project(self.d, profile="self_study")
        tsb.write_config(self_dir, delivery_profile="teaching",
                         lesson_minutes="", content_policy="faithful",
                         run_scope="full")
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_PROFILE_DRIFT", codes(result))

    def test_profile_match_passes(self):
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90)
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_PROFILE_DRIFT", codes(result))


class T04MinutesDrift(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_budgeted_profile_minutes_drift(self):
        """teaching 合同冻结 90 分钟，配置改成 137 → 漂移。"""
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90)
        tsb.write_config(self_dir, delivery_profile="teaching",
                         lesson_minutes=137, content_policy="faithful",
                         run_scope="full")
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_MINUTES_DRIFT", codes(result))

    def test_self_study_ignores_config_minutes(self):
        """self_study 的 config.yaml lesson_minutes 不生效（audit §4.2）。"""
        self_dir, _ = make_project(self.d, profile="self_study", minutes=None)
        tsb.write_config(self_dir, delivery_profile="self_study",
                         lesson_minutes=90, content_policy="faithful",
                         run_scope="full")
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_MINUTES_DRIFT", codes(result))
        self.assertIsNone(result["checked"]["lesson_minutes"])


class T05UnauthorizedDowngrade(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_confirmed_spec_downgrade_detected(self):
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90)
        _write(os.path.join(self_dir, ".pd", "confirmed-spec.json"),
               json.dumps({"delivery_profile": "self_study",
                           "lesson_minutes": 90}, ensure_ascii=False))
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_UNAUTHORIZED_DOWNGRADE", codes(result))

    def test_matching_confirmed_spec_passes(self):
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90)
        _write(os.path.join(self_dir, ".pd", "confirmed-spec.json"),
               json.dumps({"delivery_profile": "teaching",
                           "lesson_minutes": 90}, ensure_ascii=False))
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_UNAUTHORIZED_DOWNGRADE", codes(result))


class T06PolicyDrift(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_policy_drift(self):
        self_dir, _ = make_project(self.d, policy="faithful")
        tsb.write_config(self_dir, delivery_profile="self_study",
                         lesson_minutes="", content_policy="authoring",
                         run_scope="full")
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_POLICY_DRIFT", codes(result))


class T07ScopeChange(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir, _ = make_project(self.d)

    def _write_changes(self, changes):
        _write(os.path.join(self.self_dir, ".pd", "scope-change.json"),
               json.dumps({"schema_version": 1, "changes": changes},
                          ensure_ascii=False))

    def test_unapproved_change_blocks(self):
        self._write_changes([{"id": "sc-1", "scope": "sec4",
                              "reason": "补充代数几何视角"}])
        result = check_scope.check_scope_project(self.self_dir)
        self.assertIn("SCOPE_CHANGE_UNAPPROVED", codes(result))

    def test_approved_change_passes(self):
        self._write_changes([{"id": "sc-1", "scope": "sec4",
                              "reason": "补充代数几何视角",
                              "approved_by": "teacher"}])
        result = check_scope.check_scope_project(self.self_dir)
        self.assertNotIn("SCOPE_CHANGE_UNAPPROVED", codes(result))


class T08UnplannedSection(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_unplanned_section_detected(self):
        self_dir, _ = make_project(self.d, sections=("sec1", "sec2"))
        _write(os.path.join(self_dir, "main.tex"),
               "\\input{sec1.tex}\n\\input{sec2.tex}\n\\input{sec3.tex}\n")
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_UNPLANNED_SECTION", codes(result))

    def test_planned_sections_pass(self):
        self_dir, _ = make_project(self.d, sections=("sec1", "sec2"))
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_UNPLANNED_SECTION", codes(result))


class T09CoreBudgetOverflow(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_core_minutes_overflow(self):
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90,
                                   core_minutes=137)
        result = check_scope.check_scope_project(self_dir)
        self.assertIn("SCOPE_CORE_BUDGET_OVERFLOW", codes(result))

    def test_core_minutes_within_budget(self):
        self_dir, _ = make_project(self.d, profile="teaching", minutes=90,
                                   core_minutes=85)
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_CORE_BUDGET_OVERFLOW", codes(result))

    def test_self_study_has_no_core_cap(self):
        self_dir, _ = make_project(self.d, profile="self_study",
                                   core_minutes=137)
        result = check_scope.check_scope_project(self_dir)
        self.assertNotIn("SCOPE_CORE_BUDGET_OVERFLOW", codes(result))


class T10MustCoverWarning(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_empty_must_cover_warns(self):
        self_dir, _ = make_project(self.d, must_cover=[])
        result = check_scope.check_scope_project(self_dir)
        warn_codes = [w.get("code") for w in result.get("warnings", [])]
        self.assertIn("SCOPE_MUST_COVER_EMPTY", warn_codes)


class T11CliContract(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_scope_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_cli_report_and_exit_codes(self):
        self_dir, _ = make_project(self.d)
        report_path = os.path.join(self_dir, ".pd", "reports",
                                   "check_scope.json")
        rc = check_scope.main([self_dir, "--json", report_path])
        self.assertEqual(rc, 0)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["tool"], "check_scope.py")
        self.assertEqual(report["tool_version"], "1.0")
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["input_digest"])
        self.assertIn("check_scope.py", report["tool"])

        # 漂移后同一 CLI 应返回 1 并写 fail 报告
        tsb.write_config(self_dir, delivery_profile="teaching",
                         lesson_minutes="", content_policy="faithful",
                         run_scope="full")
        rc = check_scope.main([self_dir, "--json", report_path])
        self.assertEqual(rc, 1)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any(e["code"] == "SCOPE_PROFILE_DRIFT"
                            for e in report["errors"]))


class T12GateRunnerWiring(unittest.TestCase):

    def test_scope_gate_is_pure_python(self):
        from workflow.adapters.gate_runner import PURE_PYTHON_GATES
        self.assertIn("check_scope", PURE_PYTHON_GATES)
        self.assertEqual(PURE_PYTHON_GATES["check_scope"], "check_scope.py")

    def test_scope_gate_runs_in_process(self):
        """T3 批量门禁内 check_scope 以进程内方式执行。"""
        from workflow.adapters.gate_runner import GateRunner
        d = tempfile.mkdtemp(prefix="qct_scope_gr_")
        self.addCleanup(shutil.rmtree, d, True)
        self_dir, _ = make_project(d)
        runner = GateRunner(self_dir)
        reports = runner.run_gates(
            [{
                "gate_name": "check_scope",
                "script_name": "check_scope.py",
                "args": [self_dir],
                "report_rel_path": os.path.join(".pd", "reports",
                                                "check_scope.json"),
            }],
            run_id="run-gr")
        report = reports["check_scope"]
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report.get("execution_mode"), "in-process")
        self.assertTrue(report.get("batch_input_digest"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
