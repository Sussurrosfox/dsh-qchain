"""任务21（PAROBS-02/03/06）：并发观测口径 —— Python 侧不得把未知写成实测 0。

卡片 21 的核心不变量（C12.4）：
  1) manual / 恢复路径必须读「本批次」的真实批摘要（.pd/dispatch-runs/<batch_id>.json）；
  2) 读不到、读到的不是本批、或旧摘要没有峰值字段 → 必须是 None/unknown + 诊断来源，
     绝不允许回落到「磁盘重推导出来的 0」冒充实测；
  3) 缓存复用单列计数，不得抬高「真实起单」数；
  4) 峰值低于声明并行度不再触发正文返修（只给原因 + 警告），
     但并发越界（峰值 > cap）属正确性阻断，仍必须 HALT。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workflow.nodes import section_batch_nodes as sbn  # noqa: E402
from workflow.nodes.section_batch_nodes import _disk_metrics, node_t2_join_sections  # noqa: E402

import test_section_batch as tsb  # noqa: E402  （复用既有夹具，不复制一份断言口径）


# ---------------------------------------------------------------------------
# 夹具：批摘要（真实宿主 qchain_dispatch 写的 .pd/dispatch-runs/<batch_id>.json）
# ---------------------------------------------------------------------------

def runs_dir(d):
    return os.path.join(d, ".pd", "dispatch-runs")


def run_file(d, batch_id):
    return os.path.join(runs_dir(d), f"{batch_id}.json")


def write_run_record(d, batch_id, request_ids, record_batch_id=None, **fields):
    """写一份 schema_version=2 的批摘要（字段与 index.js 单协调者输出一致）。"""
    os.makedirs(runs_dir(d), exist_ok=True)
    record = {
        "schema_version": 2,
        "batch_id": batch_id,
        "request_count": len(request_ids),
        "request_ids": list(request_ids),
        "observed_scope": "host_call",
        "observed_host_in_flight": 3,
        "peak_observed_host_in_flight": 3,
        "peak_host_in_flight_source": "coordinator_host_call_counter",
        "ready_count": len(request_ids),
        "peak_ready": 3,
        "queue_wait_ms": {
            "source": "coordinator_lane_queue",
            "observed_count": 3,
            "max_ms": 12,
            "total_ms": 30,
            "mean_ms": 10,
        },
        "vendor_queue_ms": None,
        "vendor_first_token_ms": None,
        "inference_ms": None,
        "elapsed_ms": 1234,
        "elapsed_clock": "monotonic_ms",
        "real_start_count": 3,
        "cache_reuse_count": 0,
        "slots_held_unconfirmed": 0,
        "undispatched": 0,
        "cap_respected": True,
        "receipt_refs": [".pd/agent-calls/dsp-1.json"],
        "executor_kind": "host_subagent",
        "effective_concurrency": 3,
        "serial_reason_code": "",
        "serial_reason_source": "observed_parallel",
    }
    record.update(fields)
    if record_batch_id is not None:
        record["batch_id"] = record_batch_id
    with open(run_file(d, batch_id), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def fake_requests(n=3, batch_id="bat-t2-test"):
    return [
        {"dispatch_id": f"dsp-{i}", "scope": f"sec{i}", "batch_id": batch_id,
         "artifacts": [f"sec{i}.tex"]}
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# PAROBS-02：_disk_metrics 的批证据读取口径
# ---------------------------------------------------------------------------

class T21_DiskMetrics(unittest.TestCase):
    """_disk_metrics 只认本批真实摘要，未知一律 None + 来源。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.requests = fake_requests(3)
        self.ids = [r["dispatch_id"] for r in self.requests]

    def metrics(self, **kwargs):
        return _disk_metrics(
            self.requests, ["sec1", "sec2", "sec3"], {},
            self_dir=self.d, batch_id="bat-t2-test", **kwargs)

    def test_PAROBS_02a_valid_record_reproduces_peak_and_receipt(self):
        write_run_record(self.d, "bat-t2-test", self.ids, peak_observed_host_in_flight=3)
        m = self.metrics()
        self.assertEqual(m["max_in_flight"], 3)
        self.assertEqual(m["max_in_flight_source"], "dispatch_run")
        self.assertEqual(m["observed_scope"], "host_call")
        self.assertEqual(m["peak_ready"], 3)
        self.assertEqual(m["real_starts"], 3)
        self.assertEqual(m["cache_reuse"], 0)
        self.assertEqual(m["queue_wait_ms"]["max_ms"], 12)
        self.assertEqual(m["receipt_refs"], [".pd/agent-calls/dsp-1.json"])
        # 供应商侧时间观测不到 → 必须 null，绝不能写 0
        self.assertIsNone(m["vendor_queue_ms"])
        self.assertIsNone(m["inference_ms"])
        self.assertIsNone(m["diagnostics"])

    def test_PAROBS_02b_missing_record_is_unknown_not_zero(self):
        m = self.metrics()
        self.assertIsNone(m["max_in_flight"],
                          "无批摘要时峰值必须 unknown(None)，不得写实测 0")
        self.assertTrue(str(m["max_in_flight_source"]).startswith("unknown:"),
                        m["max_in_flight_source"])
        self.assertEqual(m["serial_reason_code"], "host_queue_or_unknown")
        self.assertIn("run_record_missing", m["diagnostics"])
        self.assertIsNone(m["real_starts"])
        self.assertIsNone(m["peak_ready"])
        # 磁盘重推导的固定 0 不得出现
        self.assertNotEqual(m["max_in_flight"], 0)

    def test_PAROBS_02c_stale_batch_record_not_applied(self):
        # 旧批次的摘要留在盘上（同目录），新批次绝不能套用旧峰值
        write_run_record(self.d, "bat-t2-old", self.ids, peak_observed_host_in_flight=4)
        m = self.metrics()
        self.assertIsNone(m["max_in_flight"])
        self.assertIn("run_record_missing", m["diagnostics"])

        # 同文件名但内部 batch_id 不是本批 → 拒绝
        write_run_record(self.d, "bat-t2-test", self.ids,
                         record_batch_id="bat-t2-old",
                         peak_observed_host_in_flight=4)
        m2 = self.metrics()
        self.assertIsNone(m2["max_in_flight"])
        self.assertIn("run_record_batch_mismatch", m2["diagnostics"])

    def test_PAROBS_02d_request_set_mismatch_not_applied(self):
        write_run_record(self.d, "bat-t2-test", ["dsp-9", "dsp-8", "dsp-7"])
        m = self.metrics()
        self.assertIsNone(m["max_in_flight"])
        self.assertIn("run_record_request_mismatch", m["diagnostics"])

    def test_PAROBS_02e_legacy_record_without_peak_is_unknown(self):
        os.makedirs(runs_dir(self.d), exist_ok=True)
        with open(run_file(self.d, "bat-t2-test"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "batch_id": "bat-t2-test",
                       "request_count": 3}, f)
        m = self.metrics()
        self.assertIsNone(m["max_in_flight"])
        self.assertIn("run_record_peak_unknown", m["diagnostics"])
        self.assertEqual(m["serial_reason_code"], "host_queue_or_unknown")

    def test_PAROBS_03e_cache_reuse_counted_separately(self):
        write_run_record(self.d, "bat-t2-test", self.ids,
                         peak_observed_host_in_flight=0,
                         observed_host_in_flight=0, peak_ready=3,
                         real_start_count=0, cache_reuse_count=3,
                         serial_reason_code="cache_reuse",
                         serial_reason_source="observed_claim_reuse",
                         receipt_refs=[".pd/agent-calls/dsp-1.json"])
        m = self.metrics()
        # 全缓存命中是「真的观测到 0 次宿主起单」，属于实测 0（有来源），
        # 但缓存复用必须单列，不得算成一次真实起单。
        self.assertEqual(m["max_in_flight"], 0)
        self.assertEqual(m["max_in_flight_source"], "dispatch_run")
        self.assertEqual(m["real_starts"], 0)
        self.assertEqual(m["cache_reuse"], 3)
        self.assertEqual(m["serial_reason_code"], "cache_reuse")
        self.assertEqual(m["serial_reason_source"], "observed_claim_reuse")

    def test_PAROBS_06a_unknown_reason_value_is_normalised(self):
        write_run_record(self.d, "bat-t2-test", self.ids,
                         serial_reason_code="weird_reason",
                         serial_reason_source="made_up")
        m = self.metrics()
        self.assertEqual(m["serial_reason_code"], "host_queue_or_unknown")
        self.assertTrue(str(m["serial_reason_source"]).startswith("unknown:"),
                        m["serial_reason_source"])


# ---------------------------------------------------------------------------
# PAROBS-06：T2 manual / 恢复路径的 join 语义
# ---------------------------------------------------------------------------

class T21_JoinManual(unittest.TestCase):
    """manual（磁盘重推导）路径：读批证据、低峰值只告警、越界才阻断。"""

    def setUp(self):
        self.addCleanup(sbn._reset_pool)
        self.addCleanup(tsb.register_capable_worker(4))
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        tsb.write_config(self.d, agent_dispatch="fast")
        self.state = tsb.make_state(self.d, sections=("sec1", "sec2", "sec3"))
        self.state.update(tsb.run_plan(self.state))
        self.batch_id = self.state["batch_id"]
        self.requests = self.state["dispatch_plan"]["requests_full"]
        self.plan = self.state["dispatch_plan"]
        sbn._reset_pool()
        self.captured = []
        original = sbn.interrupt
        sbn.interrupt = lambda payload: self.captured.append(payload) or {}
        self.addCleanup(lambda: setattr(sbn, "interrupt", original))
        # 第一次进入：把整批中断一次（manual 路径的真实入口）
        first = node_t2_join_sections(self.state)
        self.assertEqual(len(self.captured), 1)
        self.state.update(first)

    def _ids(self):
        return [r["dispatch_id"] for r in self.requests]

    def _write_all_products(self):
        # C5 逐节：一个作者请求可覆盖 1–2 节，三节两请求是常态 —— 必须逐节
        # 写产物，否则逐节验证会把漏写的那节判成 missing_artifacts（夹具缺陷）。
        for req in self.requests:
            for sec in (req.get("scopes") or [req["scope"]]):
                path = os.path.join(self.d, f"{sec}.tex")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"% {sec}\n")
        tsb.seed_agent_calls(self.d, self.requests)

    def test_PAROBS_06b_manual_restore_reproduces_peak_from_batch_record(self):
        self._write_all_products()
        write_run_record(self.d, self.batch_id, self._ids(),
                         peak_observed_host_in_flight=2,
                         real_start_count=3, cache_reuse_count=0,
                         serial_reason_code="", serial_reason_source="observed_parallel")
        patch = node_t2_join_sections(dict(self.state))
        m = patch["dispatch_metrics"]
        self.assertEqual(m["max_in_flight"], 2, m)
        self.assertEqual(m["max_in_flight_source"], "dispatch_run")
        self.assertEqual(m["observed_scope"], "host_call")
        self.assertEqual(m["receipt_refs"], [".pd/agent-calls/dsp-1.json"])
        self.assertEqual(patch["routing_action"], "PROCEED",
                         patch.get("error_message", ""))
        self.assertIsNone(m.get("parallelism_warning"))
        self.assertEqual(len(self.captured), 1)  # 不因观测口径再中断一次

    def test_PAROBS_06c_missing_evidence_is_unknown_and_only_warns(self):
        patch = node_t2_join_sections(dict(self.state))
        m = patch["dispatch_metrics"]
        self.assertIsNone(m["max_in_flight"],
                          "无批摘要的 manual 恢复必须是 unknown(None)，不是 0")
        self.assertTrue(str(m["max_in_flight_source"]).startswith("unknown:"))
        self.assertEqual(m["serial_reason_code"], "host_queue_or_unknown")
        self.assertNotEqual(patch.get("stage"), "HALTED",
                            "观测不到峰值不得判「伪装完成」而阻断")
        self.assertIn("parallelism_warning", m)
        self.assertEqual(m["parallelism_warning"]["code"], "PARALLELISM_UNVERIFIED")
        self.assertEqual(len(self.captured), 1)

    def test_PAROBS_06d_low_peak_warns_with_reason_without_content_rework(self):
        self._write_all_products()
        write_run_record(self.d, self.batch_id, self._ids(),
                         peak_observed_host_in_flight=1,
                         real_start_count=3, cache_reuse_count=0,
                         serial_reason_code="explicit_safe",
                         serial_reason_source="config:agent_max_concurrency")
        patch = node_t2_join_sections(dict(self.state))
        m = patch["dispatch_metrics"]
        # 正文不返修：三节全部 ok → PROCEED（低峰值只给原因 + 警告）
        self.assertEqual(patch["routing_action"], "PROCEED",
                         patch.get("error_message", ""))
        self.assertEqual(m["ok"], 3)
        self.assertEqual(m["max_in_flight"], 1)
        warning = m["parallelism_warning"]
        self.assertEqual(warning["code"], "PARALLELISM_BELOW_DECLARED")
        self.assertEqual(warning["observed"], 1)
        self.assertEqual(warning["declared"], int(self.plan["min_parallelism"]))
        self.assertEqual(warning["serial_reason_code"], "explicit_safe")
        self.assertEqual(warning["serial_reason_source"],
                         "config:agent_max_concurrency")

    def test_PAROBS_06e_cap_overflow_still_halts(self):
        self._write_all_products()
        cap = int(self.plan["max_concurrency"])
        write_run_record(self.d, self.batch_id, self._ids(),
                         peak_observed_host_in_flight=cap + 1,
                         effective_concurrency=cap)
        patch = node_t2_join_sections(dict(self.state))
        self.assertEqual(patch["stage"], "HALTED")
        self.assertEqual(patch["routing_action"], "HALT")
        self.assertIn("并发越界", patch["error_message"])


# ---------------------------------------------------------------------------
# PAROBS-06f：其余 join 节点同样适配（T2_CHECK / T4）
# ---------------------------------------------------------------------------

class T21_OtherJoins(unittest.TestCase):
    """结构核验：语义审读与 T4 验收 join 也必须读批证据，不得写死 0。"""

    def _source(self, rel):
        with open(os.path.join(ROOT, "workflow", "nodes", rel), encoding="utf-8") as f:
            return f.read()

    def test_PAROBS_06f_t2_check_and_t4_join_use_batch_evidence(self):
        semantic = self._source("semantic_nodes.py")
        t4 = self._source("t4_review_batch_nodes.py")
        for name, src in (("semantic_nodes.py", semantic),
                          ("t4_review_batch_nodes.py", t4)):
            # 低峰值/未知峰值统一走 _parallelism_check（同一份原因+警告决策），
            # 而不是各自再用 int(..., 0) 写死 0 直接 HALT。
            self.assertIn("_parallelism_check", src,
                          f"{name} 的并发门禁必须复用 _parallelism_check")
            self.assertIn("observed_scope", src,
                          f"{name} 必须落地观测口径")
            self.assertNotIn("max_in_flight\", 0)", src,
                             f"{name} 不得把未知写成 0")
            self.assertNotIn("禁止伪装完成", src,
                             f"{name} 不得再以低峰值 HALT 判定伪装完成")
        # 两个 join 真把 self_dir/batch_id 传给磁盘度量函数
        for name, src in (("semantic_nodes.py", semantic),
                          ("t4_review_batch_nodes.py", t4)):
            self.assertRegex(
                src, r"_disk_metrics\([\s\S]{0,600}?self_dir\s*=",
                f"{name} 的 _disk_metrics 必须传 self_dir（快照读批摘要）")
        # T2 语义审读的并行分支也要带观测口径
        self.assertIn("_pool_metrics_attribution", semantic)
        self.assertIn("_pool_metrics_attribution", t4)


if __name__ == "__main__":
    unittest.main()
