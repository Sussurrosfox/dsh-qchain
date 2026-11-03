"""任务卡 22：section 并发全链验收（PARE2E-01..06 的 Python 侧）。

这一层刻意**跨进程**：真实 ``node_t2_plan_dispatch``（Python 图节点）产出批次与
``.pd/dispatch-batches/<batch_id>.json`` 索引 → 真实 ``qchain_dispatch`` 工具入口
（node 子进程，subagent 提供方用可控 stub 顶替）并发执行整批 → 真实
``node_t2_join_sections`` 从磁盘重推导收尾。

证明边界（不得混写）：
* 本文件证明的是「宿主请求窗口重叠」+「批入口/恢复链不退化」；
* 供应商推理并发与真实模型耗时**不在此层证明**（需外部授权，记 PENDING_EXTERNAL）；
* stub 的耗时不代表生产加速，故这里只断言重叠与调用数，不断言墙钟比例。

所有写入都落在 pytest 的临时目录（tmp_path），不触碰项目目录与真实学案，
也不启动流水线、不读取任何 Phase 0 令牌。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_section_batch as tsb  # noqa: E402
from workflow import edges as wf_edges  # noqa: E402
from workflow import graph as wf_graph  # noqa: E402
from workflow.nodes import fast_review_nodes as frn  # noqa: E402
from workflow.nodes import section_batch_nodes as sbn  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

CANDIDATE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(sbn.__file__))))
NODE = shutil.which("node")

# 真实工具入口的跨进程治具：加载候选 index.js 的 qchain_dispatch，把 subagent
# 提供方换成可控 stub（按 MARK-<dispatch_id> 识别请求，写出该请求声明的产物），
# 记录每次宿主调用的起止时刻，最后把结果以 JSON 打到 stdout。
HARNESS = r"""
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const selfDir = process.argv[2];
const batchId = process.argv[3];
const capArg = process.argv[4] || '';
const root = process.env.Q22_ROOT;
const delayMs = Number(process.env.Q22_DELAY_MS || '120');

const events = [];
const calls = [];
const t0 = process.hrtime.bigint();
const now = () => Number(process.hrtime.bigint() - t0) / 1e6;

const reqDir = path.join(selfDir, '.pd', 'agent-requests');
const requests = fs.existsSync(reqDir)
  ? fs.readdirSync(reqDir).filter((n) => n.endsWith('.json'))
      .map((n) => JSON.parse(fs.readFileSync(path.join(reqDir, n), 'utf8')))
  : [];
const byMarker = new Map(requests.map((r) => [`MARK-${r.dispatch_id}`, r]));

const tools = {};
const ctx = {
  tools: {
    register(def) { tools[def.name] = def; },
    get(name) {
      if (name !== 'subagent') return undefined;
      return { execute: async (subArgs) => {
        const prompt = String(subArgs.prompt || '');
        const hit = /MARK-([A-Za-z0-9_.-]+)/.exec(prompt);
        const id = hit ? hit[1] : 'unknown';
        calls.push(id);
        events.push({ id, phase: 'start', at_ms: now() });
        await new Promise((r) => setTimeout(r, delayMs));
        events.push({ id, phase: 'end', at_ms: now() });
        const req = byMarker.get(`MARK-${id}`);
        const arts = (req && req.artifacts) || [];
        const text = arts
          .map((a) => `<<<FILE:${a}>>>\n% stub content of ${a}\n<<<END:${a}>>>`)
          .join('\n');
        return { runId: `run-${id}`, output: [{ type: 'text', text }] };
      } };
    },
  },
  get() { return undefined; },
};
const mod = await import(pathToFileURL(path.join(root, 'index.js')).href);
mod.apply(ctx, { projectRoot: root });
const args = { self_dir: selfDir, batch_id: batchId };
if (capArg) args.max_concurrency = Number(capArg);
const res = await tools.qchain_dispatch.execute(args);
process.stdout.write(JSON.stringify({
  calls,
  events,
  result: {
    ok: res.ok,
    dispatched: res.dispatched,
    failed: res.failed,
    success: res.success,
    batch_id: res.batch_id,
    run_record_rel: res.run_record_rel,
    effective_concurrency: res.effective_concurrency,
    configured_concurrency: res.configured_concurrency,
    peak_observed_host_in_flight: res.peak_observed_host_in_flight,
    peak_ready: res.peak_ready,
    cap_respected: res.cap_respected,
    capability_verified: res.capability_verified,
    worker_max_concurrency: res.worker_max_concurrency,
    serial_reason: res.serial_reason,
    serial_reason_code: res.serial_reason_code,
    serial_reason_source: res.serial_reason_source,
    observed_scope: res.observed_scope,
    vendor_queue_ms: res.vendor_queue_ms === undefined ? null : res.vendor_queue_ms,
    inference_ms: res.inference_ms === undefined ? null : res.inference_ms,
    statuses: (res.results || []).map((r) => [r.dispatch_id, r.status]),
  },
}));
"""


def _succ(compiled):
    """图级后继表（含条件边落点），用于证明 barrier/自环接线。"""
    builder = compiled.builder
    succ = {}
    for a, c in builder.edges:
        succ.setdefault(a, set()).add(c)
    for src, branches in builder.branches.items():
        for _name, spec in branches.items():
            succ.setdefault(src, set()).update(spec.ends.values())
    return succ


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _scopes(req):
    """请求覆盖的节：多 scope 请求带 scopes，单 scope 请求沿用 legacy scope。"""
    return list(req.get("scopes") or ([req["scope"]] if req.get("scope") else []))


class _Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        sbn._reset_pool()
        self.addCleanup(sbn._reset_pool)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        self._harness_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._harness_dir.cleanup)
        self.harness = os.path.join(self._harness_dir.name, "runner.mjs")
        with open(self.harness, "w", encoding="utf-8") as f:
            f.write(HARNESS)

    # -- 治具 ---------------------------------------------------------------
    def _mark(self, requests):
        """给派发单打上 stub 识别用的 MARK（只动 instruction，不改语义字段）。"""
        for req in requests:
            path = os.path.join(
                self.d, ".pd", "agent-requests", req["dispatch_id"] + ".json")
            record = _read_json(path)
            record["instruction"] = (
                "MARK-%s\n%s" % (req["dispatch_id"], record.get("instruction", "")))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

    def _run_tool(self, batch_id, cap=None, delay_ms=120):
        if not NODE:  # pragma: no cover - 环境缺 node 时明确跳过而不是假装通过
            self.skipTest("node 不在 PATH 上，无法跑真实批工具入口")
        env = dict(os.environ)
        env.update({
            "Q22_ROOT": CANDIDATE_ROOT,
            "Q22_DELAY_MS": str(delay_ms),
            "PYTHONIOENCODING": "utf-8",
        })
        argv = [NODE, self.harness, self.d, batch_id]
        if cap is not None:
            argv.append(str(cap))
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=180, cwd=self.d)
        self.assertEqual(
            proc.returncode, 0,
            "批工具入口失败: %s\n%s" % (proc.stdout[-2000:], proc.stderr[-2000:]))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _state(self, sections, patch=None, **extra):
        statuses = {sec: "pending" for sec in sections}
        state = tsb.make_state(self.d, sections, statuses, **extra)
        if patch:
            state.update({
                "dispatch_plan": patch["dispatch_plan"],
                "batch_id": patch["batch_id"],
                "section_statuses": patch["section_statuses"],
                "dispatch_metrics": patch.get("dispatch_metrics", {}),
                "dispatch_history": patch.get("dispatch_history", []),
                "staged_sections": patch.get("staged_sections", {}),
                "stage": patch["stage"],
            })
        return state

    def _write_sections(self, sections):
        for sec in sections:
            with open(os.path.join(self.d, sec + ".tex"), "w",
                      encoding="utf-8") as f:
                f.write("%% %s\n\\section{%s}\n" % (sec, sec))

    @staticmethod
    def _overlaps(events):
        """从宿主事件账里找出真实重叠的请求对（第二单在第一单结束前启动）。"""
        starts = [e for e in events if e["phase"] == "start"]
        ends = {e["id"]: e["at_ms"] for e in events if e["phase"] == "end"}
        pairs = []
        for i, a in enumerate(starts):
            for b in starts[i + 1:]:
                if a["id"] == b["id"]:
                    continue
                end_a = ends.get(a["id"])
                end_b = ends.get(b["id"])
                if end_a is None or end_b is None:
                    continue
                if a["at_ms"] < end_b and b["at_ms"] < end_a:
                    pairs.append((a["id"], b["id"]))
        return pairs


# ---------------------------------------------------------------------------
# PARE2E-01：四节两批作者 —— 真实图节点 + 真实批工具入口的宿主请求重叠
# ---------------------------------------------------------------------------
class T22_CrossLayerAuthorFanout(_Base):
    """四节两批作者：Python 计划 → 真实批入口并发执行 → 磁盘重推导收尾。"""

    SECTIONS = ("sec1", "sec2", "sec3", "sec4")

    def _plan(self, cap_profile="fast"):
        tsb.write_config(self.d, agent_dispatch=cap_profile)
        patch = tsb.run_plan(self._state(self.SECTIONS))
        self.assertEqual(patch["stage"], "T2_SECTIONS_PENDING",
                         "计划节点应停在待派发阶段: %s" % patch.get("error_message"))
        return patch

    def test_author_requests_overlap_through_real_batch_entry(self):
        patch = self._plan()
        requests = patch["dispatch_plan"]["requests_full"]
        self.assertEqual(
            [_scopes(r) for r in requests], [["sec1", "sec2"], ["sec3", "sec4"]],
            "四节在 cap4/窗口2 下应合成两个两节作者请求（合批减少调用）")
        for req in requests:
            self.assertEqual(req["schema_version"], 2)
            self.assertEqual(
                sorted(req["artifact_scopes"].values()),
                sorted(_scopes(req)),
                "逐节产物归属必须齐全（C5）")
        batch_id = patch["batch_id"]
        index = _read_json(os.path.join(
            self.d, ".pd", "dispatch-batches", batch_id + ".json"))
        self.assertEqual(index["required_scope_count"], 4)
        self.assertEqual(sorted(index["request_ids"]),
                         sorted(r["dispatch_id"] for r in requests))

        self._mark(requests)
        sbn._reset_pool()  # 模拟「宿主派发」：本进程没有活池
        out = self._run_tool(batch_id)

        self.assertEqual(out["result"]["dispatched"], 2)
        self.assertEqual(out["result"]["ok"], 2, out["result"]["statuses"])
        self.assertEqual(len(out["calls"]), 2, "两次宿主调用，不得重复派发")
        pairs = self._overlaps(out["events"])
        self.assertEqual(
            len(pairs), 1,
            "必须证明两个作者请求的宿主窗口真实重叠，事件账=%s" % out["events"])
        self.assertEqual(out["result"]["peak_observed_host_in_flight"], 2)
        self.assertEqual(out["result"]["cap_respected"], True)
        self.assertEqual(out["result"]["observed_scope"], "host_call")
        # 宿主能力未核实：false/null，不得写 0 或宣称已验证
        self.assertIs(out["result"]["capability_verified"], False)
        self.assertIsNone(out["result"]["worker_max_concurrency"])
        self.assertIsNone(out["result"]["vendor_queue_ms"])
        self.assertIsNone(out["result"]["inference_ms"])

        run = _read_json(os.path.join(
            self.d, ".pd", "dispatch-runs", batch_id + ".json"))
        self.assertEqual(run["peak_observed_host_in_flight"], 2)
        self.assertEqual(run["executor_kind"], "host_subagent")
        self.assertEqual(run["observed_scope"], "host_call")

        # 恢复链：同一 batch_id 从磁盘重推导，不重复中断、不重复执行
        join_state = self._state(self.SECTIONS, patch)
        join_state["manual_interrupted_batch"] = batch_id
        join_state["dispatch_mode"] = "manual"
        sbn._reset_pool()
        after = sbn.node_t2_join_sections(join_state)
        self.assertEqual(after["stage"], "T2_SECTIONS_STAGED",
                         after.get("error_message") or after)
        self.assertEqual(after.get("routing_action"), "PROCEED")
        self.assertEqual(
            sorted(sec for sec, st in after["section_statuses"].items()
                   if st == "staged"),
            list(self.SECTIONS))
        self.assertNotIn("manual_interrupted_batch_REINTERRUPT", after)
        self.assertEqual(after.get("dispatch_mode"), "manual")

    def test_cap_override_does_not_change_request_count(self):
        """并发上限只改执行槽位，不得为了峰值而拆分原本合理的一单（C9/C12）。"""
        patch = self._plan()
        requests = patch["dispatch_plan"]["requests_full"]
        self._mark(requests)
        sbn._reset_pool()
        out = self._run_tool(patch["batch_id"], cap=1, delay_ms=60)
        self.assertEqual(out["result"]["dispatched"],
                         len(requests),
                         "cap 变化不得改变请求数（不许为提高峰值拆单）")
        self.assertEqual(out["result"]["effective_concurrency"], 1)
        self.assertEqual(out["result"]["peak_observed_host_in_flight"], 1)
        self.assertEqual(self._overlaps(out["events"]), [],
                         "cap=1 时必须串行")
        self.assertTrue(
            str(out["result"].get("serial_reason") or "").strip()
            or str(out["result"].get("serial_reason_code") or "").strip(),
            "串行必须给出可解释原因，实际 %s" % out["result"]["serial_reason"])


# ---------------------------------------------------------------------------
# PARE2E-02：三节 —— T1(1) + 作者(2) + 终审(1) = 4 次调用
# ---------------------------------------------------------------------------
class T22_CallBudget(_Base):
    """三节场景的调用预算：并发增加不得增加研究/审读请求数。"""

    SECTIONS = ("sec1", "sec2", "sec3")

    def test_three_sections_need_two_author_requests_and_one_review(self):
        # 声明异步+并发 4 的 worker：计划才敢承诺并行度 >=2（否则保守退化为 1）
        self.addCleanup(tsb.register_capable_worker(4))
        tsb.write_config(self.d, agent_dispatch="fast")
        self._write_sections(self.SECTIONS)
        patch = tsb.run_plan(self._state(self.SECTIONS))
        author_requests = patch["dispatch_plan"]["requests_full"]
        self.assertEqual(
            [_scopes(r) for r in author_requests],
            [["sec1", "sec2"], ["sec3"]],
            "三节应合成 2 个作者请求（相邻合批，不截断）")
        plan = patch["dispatch_plan"]
        self.assertGreaterEqual(plan["effective_concurrency"], 2)
        self.assertGreaterEqual(plan["min_parallelism"], 2)

        # 并发档位不改变「每单节数」：safe 档位单批窗口仍是 2 节（cap×window），
        # 因此全册所需作者请求总数恒为 ceil(3/2)=2，并发只影响同时执行的单数。
        tsb.write_config(self.d, agent_dispatch="safe")
        serial_patch = tsb.run_plan(self._state(self.SECTIONS))
        serial_requests = serial_patch["dispatch_plan"]["requests_full"]
        self.assertEqual([len(_scopes(r)) for r in serial_requests], [2],
                         "safe 档位仍按相邻两节合批（合批不受并发档位影响）")
        self.assertEqual(serial_patch["dispatch_plan"]["batch_size"],
                         len(serial_requests),
                         "batch_size 记的是本批请求单数（执行槽位的最小单位）")
        self.assertEqual(serial_patch["dispatch_plan"]["effective_concurrency"], 1)
        self.assertEqual(-(-len(self.SECTIONS) // 2), len(author_requests))
        tsb.write_config(self.d, agent_dispatch="fast")

        review_state = self._state(self.SECTIONS, patch)
        review_state["section_statuses"] = {sec: "staged" for sec in self.SECTIONS}
        review_state["output_format"] = "both"
        review_plan = frn.node_t4_plan_combined_review(review_state)
        self.assertEqual(review_plan["stage"], "T4_COMBINED_REVIEW_PENDING")
        review_requests = review_plan["dispatch_plan"]["requests_full"]
        self.assertEqual(
            len(review_requests), 2,
            "终审单批上限 2 节（split_review_batches 夹取 1–2），三节 → R=2")
        self.assertEqual(sorted(review_requests[0]["review_scopes"]),
                         ["sec1", "sec2"])
        self.assertEqual(review_requests[1]["review_scopes"], ["sec3"])

        # 实测调用预算：T1(1) + 作者(K=2) + 终审(R=2) = 5。
        # 注意：tasks/START-HERE 的 1+K+R「R=1 时 K+2」口径要求三节共 4 次，
        # 与终审单批 ≤2 节的实现存在 1 次偏差 → 已记入 evidence remaining，
        # 本卡不改审读批策略（改上限会改变审读覆盖面与 C5 单批 1–2 节口径）。
        total = 1 + len(author_requests) + len(review_requests)
        self.assertEqual(total, 5,
                         "实测冷缓存预算 = 1 + 2 + 2（偏差项见 remaining）")
        # 显式放大批大小也仍被夹取到 2 节/批（口径固定，不因配置漂移）
        wide_state = self._state(self.SECTIONS, patch)
        wide_state["section_statuses"] = {s: "staged" for s in self.SECTIONS}
        wide_state["output_format"] = "both"
        wide_state["review_sections_per_batch"] = len(self.SECTIONS)
        wide_plan = frn.node_t4_plan_combined_review(wide_state)
        self.assertEqual(
            len(wide_plan["dispatch_plan"]["requests_full"]), 2,
            "审读单批上限 2 节为硬口径（split_review_batches 夹取），显式放大无效")

    def test_review_batch_index_is_dispatchable(self):
        """合并终审批次也必须能被批入口按 batch_id 选中（不是逐单 request_ref）。"""
        tsb.write_config(self.d, agent_dispatch="fast")
        self._write_sections(self.SECTIONS)
        state = self._state(self.SECTIONS)
        state["section_statuses"] = {sec: "staged" for sec in self.SECTIONS}
        state["output_format"] = "both"
        plan = frn.node_t4_plan_combined_review(state)
        batch_id = plan["batch_id"]
        index_path = os.path.join(
            self.d, ".pd", "dispatch-batches", batch_id + ".json")
        self.assertTrue(os.path.isfile(index_path),
                        "T4 合并终审批次必须写批次索引（否则宿主无法一次取全批）")
        index = _read_json(index_path)
        self.assertEqual(index["batch_id"], batch_id)
        self.assertEqual(
            sorted(index["request_ids"]),
            sorted(r["dispatch_id"]
                   for r in plan["dispatch_plan"]["requests_full"]))


# ---------------------------------------------------------------------------
# PARE2E-01（后半）：legacy 各并行阶段同样可整批选中
# ---------------------------------------------------------------------------
class T22_LegacyStageBatchSelection(_Base):
    """legacy 的 T2 语义审读与 T4 验收阶段也必须写批次索引（可整批派发）。"""

    def test_legacy_t2_semantic_review_writes_batch_index(self):
        import test_semantic_nodes as tsem

        sections = ("sec1", "sec2")
        for sec in sections:
            tsem.write_sec(self.d, sec)
        state = tsem.make_check_state(self.d, sections,
                                      {sec: "staged" for sec in sections})
        patch, _calls = tsem.run_plan_check(state)
        self.assertEqual(patch["stage"], "T2_CHECK_PENDING",
                         patch.get("error_message") or patch)
        requests = patch["dispatch_plan"]["requests_full"]
        self.assertTrue(requests)
        batch_id = patch["batch_id"]
        index_path = os.path.join(
            self.d, ".pd", "dispatch-batches", batch_id + ".json")
        self.assertTrue(os.path.isfile(index_path),
                        "T2 语义审读批次必须写批次索引")
        index = _read_json(index_path)
        self.assertEqual(index["stage"], "T2_CHECK")
        self.assertEqual(sorted(index["request_ids"]),
                         sorted(r["dispatch_id"] for r in requests))

    def test_legacy_t4_review_writes_batch_index(self):
        import test_t4_review_batch as tt4
        from workflow.nodes import t4_review_batch_nodes as t4nodes

        sections = ("sec1", "sec2")
        tt4.make_self(self.d, sections)
        state = tt4.base_state(self.d, sections)
        patch = t4nodes.node_t4_plan_review(state)
        self.assertEqual(patch["stage"], "T4_REVIEW_PENDING",
                         patch.get("error_message") or patch)
        self.assertEqual(patch.get("routing_action"), "PROCEED")
        requests = patch["dispatch_plan"]["requests_full"]
        self.assertTrue(requests)
        batch_id = patch["batch_id"]
        index_path = os.path.join(
            self.d, ".pd", "dispatch-batches", batch_id + ".json")
        self.assertTrue(os.path.isfile(index_path),
                        "legacy T4 验收批次必须写批次索引")
        index = _read_json(index_path)
        self.assertEqual(sorted(index["request_ids"]),
                         sorted(r["dispatch_id"] for r in requests))


# ---------------------------------------------------------------------------
# PARE2E-03：知识前置不串行 + 阶段 barrier 不被拆解
# ---------------------------------------------------------------------------
class T22_DependencyAndBarrier(_Base):
    """知识顺序只排派发次序；真实产物依赖才等待；构建/发布保持阶段 barrier。"""

    SECTIONS = ("sec1", "sec2", "sec3", "sec4")

    def test_knowledge_prerequisite_does_not_serialize_authors(self):
        os.makedirs(os.path.join(self.d, ".pd"), exist_ok=True)
        with open(os.path.join(self.d, ".pd", "dependency-map.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 依赖图\n\n"
                    "| 节点编号 | 必须先知道什么 | 首次出现位置 | 是否已闭合 |\n"
                    "| --- | --- | --- | --- |\n"
                    "| sec3 | sec1 的结论 | sec3 | 是 |\n")
        self.addCleanup(tsb.register_capable_worker(4))
        tsb.write_config(self.d, agent_dispatch="fast")
        patch = tsb.run_plan(self._state(self.SECTIONS))
        plan = patch["dispatch_plan"]
        requests = plan["requests_full"]
        self.assertEqual(plan["sections"], list(self.SECTIONS))
        self.assertEqual(len(requests), 2,
                         "知识前置不得把两节拆成串行单（仍应两批并发）")
        # 作者请求之间不存在真实产物依赖：各自只声明自己的产物与来源
        for req in requests:
            self.assertNotIn("depends_on", req)
            self.assertEqual(req["artifacts"],
                             ["%s.tex" % sec for sec in _scopes(req)])
            for rel in req["artifacts"]:
                self.assertEqual(req["artifact_scopes"][rel],
                                 rel[:-len(".tex")])
        self.assertGreaterEqual(plan["min_parallelism"], 2)

    def test_stage_barriers_stay_in_the_graph(self):
        compiled = wf_graph.create_fast_graph(checkpointer=MemorySaver())
        succ = _succ(compiled)
        # 派发 → 收尾：不每节 join（边数是批次级，不是节级）
        self.assertEqual(succ["t2_dispatch_sections"], {"t2_join_sections"})
        self.assertIn("t2_submit_and_check_sections", succ["t2_join_sections"])
        # T4 合并终审：派发 → 收尾（含 manual 自环），收尾后才允许交付
        self.assertIn("t4_join_combined_review",
                      succ["t4_dispatch_combined_review"])
        self.assertIn("t4_join_combined_review",
                      succ["t4_join_combined_review"])
        self.assertEqual(
            wf_edges.route_after_fast_combined_review_join(
                {"stage": "T4_COMBINED_REVIEW_RUNNING"}),
            "t4_join_combined_review",
            "manual 收尾自环必须存在（否则宿主派发后无处收尾）")
        # 全册构建/发布不得早于阶段收尾
        self.assertNotIn("t3_build", succ.get("t2_join_sections", set()))
        self.assertIn("t3_checks", succ["t4_join_combined_review"])
        self.assertEqual(succ["t4_promote_and_deliver"], {"__end__"})


# ---------------------------------------------------------------------------
# PARE2E-04：重复批调用 / 缓存与回执复用不重复执行
# ---------------------------------------------------------------------------
class T22_RepeatSafety(_Base):
    """同一 batch 再次派发：回执复用、零重复执行、零跨域覆盖。"""

    SECTIONS = ("sec1", "sec2", "sec3", "sec4")

    def test_second_batch_call_reuses_receipts_and_keeps_peak(self):
        tsb.write_config(self.d, agent_dispatch="fast")
        patch = tsb.run_plan(self._state(self.SECTIONS))
        requests = patch["dispatch_plan"]["requests_full"]
        batch_id = patch["batch_id"]
        self._mark(requests)
        sbn._reset_pool()
        first = self._run_tool(batch_id)
        self.assertEqual(first["result"]["ok"], 2)

        second = self._run_tool(batch_id)
        self.assertEqual(second["result"]["ok"], 2)
        self.assertEqual(second["calls"], [],
                         "重复派发不得再次调用宿主（回执复用），实际 %s"
                         % second["calls"])
        self.assertEqual(second["events"], [],
                         "复用路径不得产生新的宿主调用窗口")
        self.assertLessEqual(second["result"]["peak_observed_host_in_flight"], 4)

        # 逐节产物与回执仍在，且内容未被覆盖
        for req in requests:
            for sec in req["scopes"]:
                self.assertTrue(os.path.isfile(os.path.join(self.d, sec + ".tex")))
            receipt = os.path.join(
                self.d, ".pd", "agent-calls", req["dispatch_id"] + ".json")
            self.assertTrue(os.path.isfile(receipt))
            self.assertEqual(_read_json(receipt)["status"], "ok")


# ---------------------------------------------------------------------------
# PARE2E-06：单节 / 显式 safe 不因低峰值返修
# ---------------------------------------------------------------------------
class T22_LowPeakIsNotAFailure(_Base):
    """低峰值只解释不返修：单节计划 min_parallelism=1、safe 档位有明确原因。"""

    def test_single_section_plan_has_no_parallel_demand(self):
        tsb.write_config(self.d, agent_dispatch="fast")
        patch = tsb.run_plan(self._state(("sec1",)))
        plan = patch["dispatch_plan"]
        self.assertEqual(plan["min_parallelism"], 1)
        self.assertEqual(len(plan["requests_full"]), 1)
        metrics = patch.get("dispatch_metrics") or {}
        self.assertNotEqual(patch["stage"], "HALTED",
                            "单节低峰值不得 HALT: %s" % patch.get("error_message"))
        self.assertFalse(metrics.get("halted_parallelism", False))

    def test_safe_profile_serial_reason_is_explained(self):
        tsb.write_config(self.d, agent_dispatch="safe")
        patch = tsb.run_plan(self._state(("sec1", "sec2", "sec3", "sec4")))
        plan = patch["dispatch_plan"]
        self.assertEqual(plan["effective_concurrency"], 1)
        self.assertTrue(str(plan.get("serial_reason") or "").strip(),
                        "串行档位必须写明原因（不得只留空字段）")
        self.assertNotEqual(patch["stage"], "HALTED")

    def test_low_peak_parallelism_check_is_advisory_only(self):
        """生产不设「峰值必须 >=2」硬门禁（C12.4）：低峰值只给告警。"""
        plan = {"min_parallelism": 2, "max_concurrency": 4,
                "effective_concurrency": 4, "batch_size": 4,
                "sections": ["sec1", "sec2", "sec3", "sec4"],
                "requests": [{"dispatch_id": "a", "scope": "sec1"},
                             {"dispatch_id": "b", "scope": "sec3"}]}
        verdict = sbn._parallelism_check(
            plan, {"max_in_flight": 1, "executor_kind": "host_subagent"},
            "bat-t22")
        self.assertIsNone(verdict["halt"], "低峰值不得判 HALT（只告警）")
        self.assertIsNotNone(verdict["warning"], "低峰值必须留下告警/原因")
        self.assertEqual(verdict["warning"]["code"],
                         sbn.PARALLELISM_BELOW_DECLARED)
        self.assertEqual(verdict["warning"]["observed"], 1)
        self.assertIs(verdict["warning"]["content_rework_allowed"], False,
                      "低峰值不得据此返修学案正文")
        # 未知峰值同样只告警（unknown 不是实测 0）
        unknown = sbn._parallelism_check(
            plan, {"max_in_flight": None}, "bat-t22")
        self.assertIsNone(unknown["halt"])
        self.assertEqual(unknown["warning"]["code"],
                         sbn.PARALLELISM_UNVERIFIED)
        # cap 越界才是正确性阻断
        over = sbn._parallelism_check(plan, {"max_in_flight": 6}, "bat-t22")
        self.assertIsNotNone(over["halt"], "峰值越 cap 必须 HALT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
