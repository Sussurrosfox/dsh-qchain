#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_check_dispatch.py — T2 fan-out 派发计划硬门禁回归测试（unittest + 临时目录）

覆盖 T17–T28：只在临时目录合成 `.pd/dispatch-plan.json`，不触碰真实 self/
工程，也不依赖 xelatex 与网络。

  T17 3 小节 + 3 个有效请求 → pass（退出码 0）
  T18 3 小节 + 1 个请求 → fail UNDER_DISPATCH（审计硬规则）
  T19 required_scope_count=1 / 1 小节 + 1 请求 → pass（单派发合法）
  T20 坏 digest → BAD_DIGEST
  T21 请求 batch_id 与计划不一致 → BATCH_MISMATCH
  T22 重复 scope → DUPLICATE_SCOPE
  T23 请求数超过 max_concurrency → OVER_CONCURRENCY
  T24 其余字段规则（缺失键 / 重复 dispatch_id / 坏 scope / 坏 role /
      空 artifacts / 坏并发值 / 低并行度仅警告）
  T25 计划文件缺失 → PLAN_MISSING
  T26 计划 JSON 畸形 → PLAN_BAD_JSON
  T27 --json 报告在 pass/fail 两种情形下都落盘且 status 正确
  T28 子进程冒烟：真实退出码与 stdout 摘要

运行: python -m unittest discover scripts -p "test_check_dispatch.py" -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_dispatch  # noqa: E402

DIGEST = "a" * 64
DIGEST_OTHER = "0123456789abcdef" * 4


def make_plan(**overrides):
    """一份 3 小节 / 3 请求的合法派发计划；overrides 逐键覆盖。"""
    plan = {
        "batch_id": "bat-t2-0001",
        "stage": "T2",
        "created_at": "2024-01-01T00:00:00+00:00",
        "max_concurrency": 4,
        "min_parallelism": 1,
        "sections": ["sec0", "sec1", "sec2"],
        "required_scope_count": 3,
        "requests": [
            {"dispatch_id": "dsp-t2-sec0-0000000001", "scope": "sec0",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec0.tex"], "status": "pending"},
            {"dispatch_id": "dsp-t2-sec1-0000000002", "scope": "sec1",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec1.tex"], "status": "pending"},
            {"dispatch_id": "dsp-t2-sec2-0000000003", "scope": "sec2",
             "role": "writer", "input_digest": DIGEST,
             "artifacts": ["sec2.tex"], "status": "pending"},
        ],
    }
    plan.update(overrides)
    return plan


def write_plan(d, plan):
    """把计划写到 <d>/.pd/dispatch-plan.json，返回该路径。"""
    pd_dir = os.path.join(d, ".pd")
    os.makedirs(pd_dir, exist_ok=True)
    path = os.path.join(pd_dir, "dispatch-plan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


def run_gate(d, *extra):
    """import 路径调用 main(argv)：兼容返回值与 SystemExit.code 两种约定。"""
    try:
        rc = check_dispatch.main([d] + list(extra))
    except SystemExit as exc:  # pragma: no cover - 当前实现走返回值
        rc = exc.code
    return 0 if rc is None else int(rc)


def run_cli(d, *extra):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "check_dispatch.py"), d]
        + list(extra),
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def load_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class T17_T19_Parallelism(unittest.TestCase):
    """并行度硬规则（审计核心：小节数 vs 派发请求数）。"""

    def test_t17_three_sections_three_requests_pass(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            rc = run_gate(d)
            self.assertEqual(rc, 0)
            res = check_dispatch.check_dispatch_project(d)
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["checked"], {"sections": 3, "requests": 3})

    def test_t18_three_sections_one_request_fails_under_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(
                requests=[make_plan()["requests"][0]]))
            rc = run_gate(d)
            self.assertEqual(rc, 1)
            res = check_dispatch.check_dispatch_project(d)
            codes = [e["code"] for e in res["errors"]]
            self.assertIn("UNDER_DISPATCH", codes)

    def test_t19_single_scope_single_request_pass(self):
        with tempfile.TemporaryDirectory() as d:
            plan = make_plan(
                sections=["sec0"],
                required_scope_count=1,
                min_parallelism=1,
                max_concurrency=1,
                requests=[{"dispatch_id": "dsp-t2-sec0-1", "scope": "sec0",
                           "role": "writer", "input_digest": DIGEST_OTHER,
                           "artifacts": ["sec0.tex"], "status": "pending"}])
            write_plan(d, plan)
            rc = run_gate(d)
            self.assertEqual(rc, 0)
            res = check_dispatch.check_dispatch_project(d)
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["warnings"], [])

    def test_t19b_empty_requests_fails_under_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(requests=[]))
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            codes = [e["code"] for e in res["errors"]]
            self.assertIn("UNDER_DISPATCH", codes)

    def test_t19c_required_scope_two_with_one_request_fails(self):
        with tempfile.TemporaryDirectory() as d:
            plan = make_plan(
                sections=["sec0", "sec1"],
                required_scope_count=2,
                requests=[make_plan()["requests"][0]])
            write_plan(d, plan)
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            codes = [e["code"] for e in res["errors"]]
            self.assertIn("UNDER_DISPATCH", codes)


class T20_T23_FieldRules(unittest.TestCase):
    """字段级硬规则：digest / batch 对齐 / 重复 scope / 并发上限。"""

    def test_t20_bad_digest_fails(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = make_plan()["requests"]
            bad = dict(reqs[0])
            bad["input_digest"] = "NOT-A-SHA256"
            write_plan(d, make_plan(requests=[bad] + reqs[1:]))
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertIn("BAD_DIGEST", [e["code"] for e in res["errors"]])

    def test_t21_request_batch_id_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = make_plan()["requests"]
            bad = dict(reqs[1])
            bad["batch_id"] = "bat-t2-9999"
            write_plan(d, make_plan(requests=[reqs[0], bad, reqs[2]]))
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertIn("BATCH_MISMATCH", [e["code"] for e in res["errors"]])

    def test_t22_duplicate_scope_fails(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = make_plan()["requests"]
            dup = dict(reqs[2])
            dup["dispatch_id"] = "dsp-t2-sec2-0000000004"
            dup["scope"] = "sec0"  # 与 requests[0] 抢同一切片
            write_plan(d, make_plan(requests=[reqs[0], reqs[1], dup]))
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertIn("DUPLICATE_SCOPE", [e["code"] for e in res["errors"]])

    def test_t23_over_concurrency_fails(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(max_concurrency=2))
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertIn("OVER_CONCURRENCY", [e["code"] for e in res["errors"]])


class T24_OtherRules(unittest.TestCase):
    """其余硬规则与唯一一条警告。"""

    def _codes(self, d):
        return [e["code"] for e in check_dispatch.check_dispatch_project(d)["errors"]]

    def test_t24a_missing_top_level_keys_one_entry_each(self):
        with tempfile.TemporaryDirectory() as d:
            plan = make_plan()
            for key in ("stage", "max_concurrency"):
                plan.pop(key)
            write_plan(d, plan)
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            missing = [e for e in res["errors"] if e["code"] == "MISSING_KEY"]
            self.assertEqual(len(missing), 2)
            self.assertTrue(any("stage" in e["detail"] for e in missing))
            self.assertTrue(any("max_concurrency" in e["detail"] for e in missing))

    def test_t24b_duplicate_dispatch_id_and_bad_scope_role_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = make_plan()["requests"]
            r0 = dict(reqs[0])
            r0["dispatch_id"] = reqs[1]["dispatch_id"]  # 重复派发身份
            r1 = dict(reqs[1])
            r1["scope"] = "secX"            # 不在 sections
            r1["role"] = ""                 # 空角色
            r1["artifacts"] = []            # 空产物清单
            write_plan(d, make_plan(requests=[r0, r1, reqs[2]]))
            self.assertEqual(run_gate(d), 1)
            codes = self._codes(d)
            self.assertIn("DUP_DISPATCH_ID", codes)
            self.assertIn("BAD_SCOPE", codes)
            self.assertIn("BAD_ROLE", codes)
            self.assertIn("BAD_ARTIFACTS", codes)

    def test_t24c_bad_concurrency_value(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(max_concurrency=0))
            self.assertEqual(run_gate(d), 1)
            self.assertIn("BAD_CONCURRENCY", self._codes(d))

    def test_t24d_low_parallelism_is_warning_only(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(min_parallelism=8))
            self.assertEqual(run_gate(d), 0)  # 仅警告，不失败
            res = check_dispatch.check_dispatch_project(d)
            self.assertEqual(res["errors"], [])
            self.assertEqual([w["code"] for w in res["warnings"]],
                             ["LOW_PARALLELISM"])

    def test_t24e_request_batch_id_absent_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            res = check_dispatch.check_dispatch_project(d)
            self.assertNotIn("BATCH_MISMATCH",
                             [e["code"] for e in res["errors"]])

    def test_t24f_empty_plan_batch_id_fails(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan(batch_id=""))
            self.assertEqual(run_gate(d), 1)
            self.assertIn("BATCH_MISMATCH", self._codes(d))


class T25_T26_PlanIO(unittest.TestCase):
    """计划文件不存在 / 不可解析。"""

    def test_t25_missing_plan_fails(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertEqual([e["code"] for e in res["errors"]],
                             ["PLAN_MISSING"])
            self.assertEqual(res["checked"], {"sections": 0, "requests": 0})

    def test_t26_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as d:
            pd_dir = os.path.join(d, ".pd")
            os.makedirs(pd_dir, exist_ok=True)
            with open(os.path.join(pd_dir, "dispatch-plan.json"), "w",
                      encoding="utf-8") as f:
                f.write("{ this is not json ")
            self.assertEqual(run_gate(d), 1)
            res = check_dispatch.check_dispatch_project(d)
            self.assertEqual([e["code"] for e in res["errors"]],
                             ["PLAN_BAD_JSON"])

    def test_t26b_explicit_plan_path_is_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            alt = os.path.join(d, "alt-plan.json")
            with open(alt, "w", encoding="utf-8") as f:
                json.dump(make_plan(), f)
            self.assertEqual(run_gate(d, "--plan", alt), 0)
            res = check_dispatch.check_dispatch_project(d, alt)
            self.assertEqual(res["plan"], os.path.abspath(alt))

    def test_t26c_bom_prefixed_plan_is_tolerated(self):
        # Windows 侧工具/编辑器常写出带 BOM 的 UTF-8；内容校验仍严格。
        with tempfile.TemporaryDirectory() as d:
            pd_dir = os.path.join(d, ".pd")
            os.makedirs(pd_dir, exist_ok=True)
            with open(os.path.join(pd_dir, "dispatch-plan.json"), "w",
                      encoding="utf-8-sig") as f:
                json.dump(make_plan(), f, ensure_ascii=False)
            self.assertEqual(run_gate(d), 0)


class T27_Report(unittest.TestCase):
    """--json 报告：pass/fail 两种情形都必须落盘且 status 正确。"""

    def test_t27_json_report_status_both_ways(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            ok_json = os.path.join(d, "reports", "ok", "check_dispatch.json")
            self.assertEqual(run_gate(d, "--json", ok_json), 0)
            rep_ok = load_report(ok_json)
            self.assertEqual(rep_ok["status"], "pass")
            self.assertEqual(rep_ok["errors"], [])
            self.assertEqual(rep_ok["checked"], {"sections": 3, "requests": 3})
            self.assertEqual(rep_ok["plan"],
                             os.path.join(os.path.abspath(d), ".pd",
                                          "dispatch-plan.json"))
            self.assertIn("warnings", rep_ok)

            write_plan(d, make_plan(requests=[make_plan()["requests"][0]]))
            bad_json = os.path.join(d, "reports", "bad", "check_dispatch.json")
            self.assertEqual(run_gate(d, "--json", bad_json), 1)
            rep_bad = load_report(bad_json)
            self.assertEqual(rep_bad["status"], "fail")
            self.assertIn("UNDER_DISPATCH",
                          [e["code"] for e in rep_bad["errors"]])

    def test_t27b_report_written_even_when_plan_missing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "reports", "check_dispatch.json")
            self.assertEqual(run_gate(d, "--json", out), 1)
            rep = load_report(out)
            self.assertEqual(rep["status"], "fail")
            self.assertEqual(rep["errors"][0]["code"], "PLAN_MISSING")


class T28_Smoke(unittest.TestCase):
    """子进程冒烟：真实退出码 + stdout 人读摘要。"""

    def test_t28_subprocess_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            write_plan(d, make_plan())
            r = run_cli(d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("[check_dispatch] 通过", r.stdout)

            write_plan(d, make_plan(
                requests=[make_plan()["requests"][0]], max_concurrency=0))
            r = run_cli(d)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("[FAIL] UNDER_DISPATCH", r.stdout)
            self.assertIn("[FAIL] BAD_CONCURRENCY", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
