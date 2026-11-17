#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_recovery.py — RC-3：HALT 的官方恢复原语。

REC-01  拒绝无真人批准（approved_by 空）
REC-02  拒绝未知动作 / 闭集外的 reroute 目标（不动任何状态）
REC-03  dry-run 只报告不写，checkpoint 不变、无备份
REC-04  retry_gate 真恢复：checkpoint 前移、备份落盘、台账落 .pd/recoveries/
REC-05  abandon_dispatch 清掉 pending_dispatch 并回到门禁决策点
REC-06  relax_budget 只在抬高时生效（不降低已冻结上限）
REC-07  非 HALTED 状态拒绝恢复（避免动运行中的图）
REC-08  build_halt_payload 给出的 how 命令可被 pdstate recover 认识

用真实 SqliteSaver + 临时 sqlite；不启动完整流水线、不派 Agent。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from workflow import recovery as rec  # noqa: E402
from workflow.checkpoints import SqliteSaver  # noqa: E402
from workflow.graph import create_legacy_graph  # noqa: E402

THREAD = "recover-test"


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rec-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.self_dir = os.path.join(self.tmp, "proj", "self")
        os.makedirs(os.path.join(self.self_dir, ".langgraph"), exist_ok=True)
        self.db = os.path.join(self.self_dir, ".langgraph",
                               "courseware_checkpoints.sqlite")
        self.saver = SqliteSaver(self.db)
        self.app = create_legacy_graph(checkpointer=self.saver)
        self.cfg = {"configurable": {"thread_id": THREAD}}
        self._seed({})

    def _seed(self, extra):
        st = {"stage": "HALTED", "routing_action": "HALT",
              "error_message": "测试停机",
              "pending_dispatch": ".pd/agent-requests/dsp-x.json",
              "execution_config": {"repair_max_per_scope": 2,
                                   "repair_max_total": 6}}
        st.update(extra)
        self.app.update_state(self.cfg, st, as_node="repair_await")

    def _state(self):
        return dict(self.app.get_state(self.cfg).values)

    def _recover(self, **kw):
        kw.setdefault("approved_by", "用户")
        return rec.recover(self.self_dir, db_path=self.db, thread_id=THREAD, **kw)

    # -- REC-01 ----------------------------------------------------------
    def test_requires_human_approver(self):
        with self.assertRaises(rec.RecoveryError) as ctx:
            self._recover(action="retry_gate", approved_by="  ")
        self.assertIn("真人批准", str(ctx.exception))

    # -- REC-02 ----------------------------------------------------------
    def test_unknown_action_and_target_are_refused(self):
        with self.assertRaises(rec.RecoveryError):
            self._recover(action="rm_rf")
        with self.assertRaises(rec.RecoveryError) as ctx:
            self._recover(action="reroute_to", target="t9_nonexistent")
        self.assertIn("闭集", str(ctx.exception))
        # 状态与备份目录都没被动
        self.assertEqual(self._state()["stage"], "HALTED")
        self.assertFalse(os.path.isdir(
            os.path.join(self.self_dir, ".pd", "recoveries")))

    # -- REC-03 ----------------------------------------------------------
    def test_dry_run_reports_without_writing(self):
        before = self.app.get_state(self.cfg).config["configurable"]["checkpoint_id"]
        led = self._recover(action="retry_gate", dry_run=True)
        self.assertTrue(led["dry_run"])
        self.assertFalse(led["applied"])
        self.assertEqual(led["as_node"], "repair_await")
        after = self.app.get_state(self.cfg).config["configurable"]["checkpoint_id"]
        self.assertEqual(before, after)
        self.assertEqual(self._state()["stage"], "HALTED")
        self.assertFalse([p for p in os.listdir(
            os.path.dirname(self.db)) if ".bak-recover-" in p])

    # -- REC-04 ----------------------------------------------------------
    def test_retry_gate_recovers_and_ledgers(self):
        before = self.app.get_state(self.cfg).config["configurable"]["checkpoint_id"]
        led = self._recover(action="retry_gate", reason="补了图片资产，重跑门禁")
        self.assertTrue(led["applied"])
        self.assertNotEqual(led["after_checkpoint_id"], before)
        self.assertTrue(os.path.isfile(led["backup"]))
        st = self._state()
        self.assertEqual(st.get("routing_action"), "PROCEED")
        self.assertIsNone(st.get("error_message"))
        self.assertIsNone(st.get("pending_dispatch"))
        self.assertEqual(st.get("stage"), "T3_CHECKS_PENDING")
        # 台账可审计
        path = led["ledger"]
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["approved_by"], "用户")
        self.assertEqual(on_disk["action"], "retry_gate")
        self.assertEqual(on_disk["before_checkpoint_id"], before)

    # -- REC-05 ----------------------------------------------------------
    def test_abandon_dispatch_clears_pending(self):
        led = self._recover(action="abandon_dispatch",
                            dispatch_id="dsp-x",
                            reason="误派单：契约问题被当成内容问题")
        self.assertTrue(led["applied"])
        st = self._state()
        self.assertIsNone(st.get("pending_dispatch"))
        self.assertEqual(st.get("stage"), "T3_CHECKS_PENDING")

    def test_abandon_dispatch_refuses_other_dispatch(self):
        with self.assertRaises(rec.RecoveryError):
            self._recover(action="abandon_dispatch", dispatch_id="dsp-OTHER")

    # -- REC-06 ----------------------------------------------------------
    def test_relax_budget_only_raises_limits(self):
        led = self._recover(action="relax_budget", budgets={"per_scope": 8,
                                                            "total": 20},
                            reason="返修额度被非内容问题吃光")
        cfg = self._state()["execution_config"]
        self.assertEqual(cfg["repair_max_per_scope"], 8)
        self.assertEqual(cfg["repair_max_total"], 20)
        self.assertTrue(led["applied"])
        # 再给更小的值 → 不降低
        self._seed({})
        self._recover(action="relax_budget", budgets={"per_scope": 1,
                                                      "total": 1})
        cfg = self._state()["execution_config"]
        self.assertEqual(cfg["repair_max_per_scope"], 2)
        self.assertEqual(cfg["repair_max_total"], 6)

    def test_relax_budget_requires_positive_limits(self):
        with self.assertRaises(rec.RecoveryError):
            self._recover(action="relax_budget", budgets={"per_scope": 0,
                                                          "total": 0})

    # -- REC-07 ----------------------------------------------------------
    def test_refuses_when_not_halted(self):
        self._seed({"stage": "T3_BUILDING", "routing_action": "PROCEED"})
        with self.assertRaises(rec.RecoveryError) as ctx:
            self._recover(action="retry_gate")
        self.assertIn("不是 HALTED", str(ctx.exception))

    # -- REC-08 ----------------------------------------------------------
    def test_halt_payload_documents_the_supported_command(self):
        payload = rec.build_halt_payload(
            cause="no_change", reason="产物未变化", gate="repair",
            target="sec2", dispatch_id="dsp-a1b2",
            budget={"per_scope": {"sec2": 2}})
        self.assertIn("pdstate.py", payload["how"])
        self.assertIn("recover", payload["how"])
        self.assertIn("--approved-by", payload["how"])
        kinds = {a["action"] for a in payload["actions"]}
        self.assertTrue(kinds <= set(rec.HALT_ACTIONS))
        self.assertIn("abandon_dispatch", kinds)
        self.assertIn("retry_gate", kinds)
        self.assertIn("relax_budget", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
