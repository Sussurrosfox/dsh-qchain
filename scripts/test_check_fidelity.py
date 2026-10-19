"""test_check_fidelity.py — 来源忠实性门禁回归（audit §18.6/§18.7）。

fixture 用真实 contracts + source_inventory 构造自足工程，覆盖 §18.7 的
八项回归判定： faithful 下未批准新增 FAIL / 排除项 PASS / 覆盖缺口 FAIL /
EXPLAIN PASS / 批准 bridge PASS / 未批准新例题 FAIL / authoring 白名单
PASS / 输入 digest 篡改 FAIL。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ENGINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

from workflow import contracts  # noqa: E402
from scripts import source_inventory as inv  # noqa: E402
import check_fidelity as cf  # noqa: E402

SAMPLE_MD = """# 08-矩阵函数

## 一、矩阵函数

1. $f(A)=Pf(D)P^{-1}$ 与幂级数定义。
2. $M'=AM$ 与谱方法。

## 二、计算

1. 三种计算路径。
"""

EXCLUDED_MD = """# 2023.0.8-2

1. 题目内容。
"""

SEC1_OK = """% -*- coding: utf-8 -*-
\\section*{I 矩阵函数}

% source: SRC-003
\\begin{microknowledge}[矩阵函数]
幂级数定义 $f(A)=Pf(D)P^{-1}$。
\\end{microknowledge}

% qid: sec1-q1 | 题型: 进入题
% source: SRC-004
\\item 计算题。
\\begin{solution}
解答。
\\end{solution}
"""


def _codes(result):
    return sorted({e["code"] for e in result["errors"]})


class FidelityFixture(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="qct_fid_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "8.md"), "w",
                  encoding="utf-8") as f:
            f.write(SAMPLE_MD)
        with open(os.path.join(self.root, "2023.0.8-2.md"), "w",
                  encoding="utf-8") as f:
            f.write(EXCLUDED_MD)
        self.self_dir = os.path.join(self.root, "self")
        os.makedirs(os.path.join(self.self_dir, ".pd"))

    def _build_contracts(self, policy="faithful", allowed=()):
        manifest = contracts.build_source_manifest(
            project_root=self.root, source_files=["8.md"],
            excluded_sources=["2023.0.8-2.md :: 用户明确排除"])
        contract = contracts.build_intent_contract(
            user_spec={"content_policy": policy,
                       "excluded_sources": "2023.0.8-2.md",
                       "allowed_extensions": list(allowed)})
        contracts.write_contracts(self.self_dir, manifest, contract)
        inventory = inv.build_inventory(manifest, self.root)
        inv.write_inventory(self.self_dir, inventory)
        return manifest, contract, inventory

    def _write(self, rel, text):
        path = os.path.join(self.self_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _write_approvals(self, entries):
        self._write(os.path.join(".pd", "approvals.json"),
                    json.dumps({"schema_version": 1,
                                "approvals": entries}, ensure_ascii=False))

    # ---- T01: faithful + EXPLAIN 全保留 → PASS ----
    def test_t01_faithful_preserved_pass(self):
        _, _, inv_data = self._build_contracts()
        preserve_ids = [u["source_id"] for u in inv_data["units"]
                        if u["disposition"] == "preserve"]
        excluded_ids = [u["source_id"] for u in inv_data["units"]
                        if u["disposition"] == "excluded"]
        lines = ["# 来源映射", ""]
        for i, sid in enumerate(preserve_ids):
            lines.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 改写讲解"
                         % sid)
        for sid in excluded_ids:
            lines.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> "
                         "用户排除" % sid)
        self._write("source-map.md", "\n".join(lines) + "\n")
        self._write("coverage-map.md",
                    "# 覆盖表\n\n| source_ids | scope | action | "
                    "new_objects |\n|---|---|---|---|\n"
                    "| %s | sec1 | EXPLAIN |  |\n"
                    % ", ".join(preserve_ids))
        self._write("sec1.tex", SEC1_OK)
        res = cf.check_fidelity_project(self.self_dir)
        self.assertEqual(_codes(res), [], msg=str(res["errors"]))

    # ---- T02: 单元无处置记录 → SOURCE_NOT_COVERED ----
    def test_t02_missing_unit_fails(self):
        _, _, inv_data = self._build_contracts()
        ids = [u["source_id"] for u in inv_data["units"]
               if u["disposition"] == "preserve"]
        rows = ["%s -> sec1 -> EXPLAIN -> EXPLAINED -> 只映射第一个"
                % ids[0]]
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_SOURCE_NOT_COVERED", _codes(res))

    # ---- T03: faithful + EXTEND 未隔离 → FAIL ----
    def test_t03_extend_unapproved_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            act = "EXTEND" if u["disposition"] == "preserve" else "PRESERVE"
            st = "PRESERVED" if u["disposition"] == "preserve" \
                else "EXCLUDED_BY_USER"
            rows.append("%s -> sec1 -> %s -> %s -> 行"
                        % (u["source_id"], act, st))
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_EXTEND_UNAPPROVED", _codes(res))

    # ---- T04: EXTEND 停在 PENDING_APPROVAL → 只警告不 FAIL ----
    def test_t04_extend_pending_warns_only(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXTEND -> PENDING_APPROVAL -> "
                            "待批准" % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        codes = _codes(res)
        # PENDING 单元未进正文 → 覆盖缺口 + EXTEND 警告，但不得 EXTEND_UNAPPROVED
        self.assertNotIn("FIDELITY_EXTEND_UNAPPROVED", codes)
        self.assertIn("FIDELITY_SOURCE_NOT_COVERED", codes)
        warn_codes = {w["code"] for w in res["warnings"]}
        self.assertIn("FIDELITY_EXTEND_PENDING", warn_codes)

    # ---- T05: 批准 bridge → PASS 且 bridge 入账 ----
    def test_t05_approved_bridge_pass(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> BRIDGE -> BRIDGED -> 最小前置"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        self._write_approvals([{
            "id": "BR-001", "kind": "bridge", "scope": "sec1",
            "reason": "最小前置：矩阵范数", "approver": "user"}])
        sec = SEC1_OK.replace("% source: SRC-004",
                              "% source: SRC-004\n% bridge: BR-001")
        self._write("sec1.tex", sec)
        res = cf.check_fidelity_project(self.self_dir)
        self.assertEqual(_codes(res), [], msg=str(res["errors"]))
        self.assertEqual(res["checked"]["bridges"], ["BR-001"])

    # ---- T06: 未批准 bridge 锚点 → FAIL ----
    def test_t06_unapproved_bridge_anchor_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 讲解"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        sec = SEC1_OK.replace("% source: SRC-004",
                              "% source: SRC-004\n% bridge: BR-009")
        self._write("sec1.tex", sec)
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_BRIDGE_UNAPPROVED", _codes(res))

    # ---- T07: 正文引用排除单元 → FAIL ----
    def test_t07_excluded_referenced_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 讲解"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        excluded_id = [u["source_id"] for u in inv_data["units"]
                       if u["disposition"] == "excluded"][0]
        sec = SEC1_OK.replace("% source: SRC-004",
                              "%% source: %s\n%% source: SRC-004"
                              % excluded_id)
        self._write("sec1.tex", sec)
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_EXCLUDED_REFERENCED", _codes(res))

    # ---- T08: qid/知识卡缺锚点 → FAIL ----
    def test_t08_ungrounded_output_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 讲解"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        sec = SEC1_OK.replace("% source: SRC-003", "% 无锚点")
        sec = sec.replace("% source: SRC-004", "% 无锚点")
        self._write("sec1.tex", sec)
        res = cf.check_fidelity_project(self.self_dir)
        codes = _codes(res)
        self.assertIn("FIDELITY_UNGROUNDED_QID", codes)
        self.assertIn("FIDELITY_UNGROUNDED_BLOCK", codes)

    # ---- T09: faithful 逆序 → FAIL ----
    def test_t09_order_violation_fails(self):
        _, _, inv_data = self._build_contracts()
        ids = [u["source_id"] for u in inv_data["units"]
               if u["disposition"] == "preserve"]
        if len(ids) < 2:
            self.skipTest("样本单元不足")
        rows = ["%s -> sec1 -> EXPLAIN -> EXPLAINED -> 倒序一" % ids[1],
                "%s -> sec1 -> EXPLAIN -> EXPLAINED -> 倒序二" % ids[0]]
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_ORDER_VIOLATION", _codes(res))

    # ---- T10: authoring + allowed_extensions → PASS ----
    def test_t10_authoring_allowed_extension_pass(self):
        _, _, inv_data = self._build_contracts(
            policy="authoring",
            allowed=[{"id": "EXT-1", "scope": "sec1",
                      "reason": "补充 Schur 三角分解", "approver": "user"}])
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXTEND -> EXPLAINED -> 白名单扩展"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertNotIn("FIDELITY_EXTEND_NOT_ALLOWED", _codes(res))

    # ---- T11: authoring 白名单外扩展 → FAIL ----
    def test_t11_authoring_disallowed_extension_fails(self):
        self._build_contracts(policy="authoring")
        _, _, inv_data = None, None, None
        # 重新取 inventory
        inv_data = inv.load_inventory(self.self_dir)
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec9 -> EXTEND -> EXPLAINED -> 白名单外"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_EXTEND_NOT_ALLOWED", _codes(res))

    # ---- T12: 输入 digest 篡改 → FAIL ----
    def test_t12_source_digest_tamper_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 讲解"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        with open(os.path.join(self.root, "8.md"), "a",
                  encoding="utf-8") as f:
            f.write("\n篡改内容。\n")
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_SOURCE_DIGEST_MISMATCH", _codes(res))

    # ---- T13: 缺 manifest → 硬失败 ----
    def test_t13_manifest_missing(self):
        res = cf.check_fidelity_project(self.self_dir)
        self.assertEqual(_codes(res), ["FIDELITY_MANIFEST_MISSING"])

    # ---- T14: coverage-map 新对象无登记 → FAIL ----
    def test_t14_novelty_unapproved_fails(self):
        _, _, inv_data = self._build_contracts()
        rows = []
        for u in inv_data["units"]:
            if u["disposition"] == "excluded":
                rows.append("%s -> - -> PRESERVE -> EXCLUDED_BY_USER -> 排除"
                            % u["source_id"])
            else:
                rows.append("%s -> sec1 -> EXPLAIN -> EXPLAINED -> 讲解"
                            % u["source_id"])
        self._write("source-map.md", "\n".join(rows) + "\n")
        self._write("coverage-map.md",
                    "# 覆盖表\n\n| source_ids | scope | action | "
                    "new_objects |\n|---|---|---|---|\n"
                    "| %s | sec1 | PRESERVE | Schur 定理 |\n"
                    % inv_data["units"][0]["source_id"])
        res = cf.check_fidelity_project(self.self_dir)
        self.assertIn("FIDELITY_NOVELTY_UNAPPROVED", _codes(res))

    # ---- T15: CLI 退出码与 JSON 报告 ----
    def test_t15_cli_exit_codes(self):
        self._build_contracts()
        out = os.path.join(self.d, "fid.json")
        rc_fail = cf.main([self.self_dir, "--json", out])
        self.assertEqual(rc_fail, 1)
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(rep["tool"], "check_fidelity.py")
        self.assertEqual(rep["status"], "fail")
        self.assertTrue(rep["errors"])


if __name__ == "__main__":
    unittest.main()
