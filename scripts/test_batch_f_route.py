#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_f_route.py — 创审分离的「模型层」回归（N4）

审计发现：INV-2 只比对 `worker` 字符串。worker 不同只证明「会话是另起的」。
宿主 `resolveChildAgentOptions` 在请求未给路由时**继承父会话当前路由**，
而 config.yaml 的 `agent_dispatch.model_map` 默认整块被注释掉——于是
「不同 worker、同一个模型」是默认形态：审读方与写作方共享同样的偏差，
独立审读在模型层根本不存在。config.yaml:75 的注释却承诺「为创审分离配置
不同路由」，与实际行为相反。

本测试锁三条口径：
  1. 两侧生效路由相同 → REVIEWER_ROUTE_NOT_SEPARATED（error，阻断）；
  2. 生效路由不同 → 不报；
  3. 旧台账没记生效路由（全 null）→ 只 WARN（REVIEWER_ROUTE_UNRECORDED），
     不把「不知道」当成「违规」。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
GATE = os.path.join(HERE, "check_agent_audit.py")

sys.path.insert(0, HERE)
sys.path.insert(0, PLUGIN_ROOT)


def _sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestReviewerRouteSeparation(unittest.TestCase):
    """check_agent_audit 的 REVIEWER_ROUTE_NOT_SEPARATED / REVIEWER_ROUTE_UNRECORDED。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="batch-f-route-")
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, ".pd", "agent-requests"), exist_ok=True)
        os.makedirs(os.path.join(self.d, ".pd", "agent-calls"), exist_ok=True)
        with open(os.path.join(self.d, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("agent_dispatch:\n  profile: safe\n  allow_unattested: false\n")

    # -- 夹具 ---------------------------------------------------------------
    def _artifact(self, rel, text="% 切片内容\n"):
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path) or self.d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return _sha(path)

    def _request(self, scope, stage, artifact):
        dispatch_id = "dsp-%s" % uuid.uuid4().hex[:12]
        req = {
            "dispatch_id": dispatch_id,
            "batch_id": "bat-%s-%s" % (stage.lower().replace("_", ""), scope),
            "run_id": "route-run",
            "stage": stage,
            "role": "%s 专员" % stage,
            "scope": scope,
            "prompt_file": "subagent_prompt.md",
            "artifacts": [artifact],
            "context": {},
            "instruction": "route separation fixture",
            "max_retries": 0,
        }
        with open(os.path.join(self.d, ".pd", "agent-requests", dispatch_id + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)
        return req

    def _evidence(self, req, worker, digest, provider=None, model=None,
                  effective=None):
        ev = {
            "schema_version": 2,
            "dispatch_id": req["dispatch_id"],
            "batch_id": req["batch_id"],
            "worker": worker,
            "provider": provider,
            "model": model,
            "started_at": "2026-11-11T00:00:00+00:00",
            "finished_at": "2026-11-11T00:00:01+00:00",
            "duration_ms": 1000,
            "artifacts_written": list(req["artifacts"]),
            "output_digests": {req["artifacts"][0]: digest},
            "status": "ok",
        }
        if effective is not None:
            ev["effective_provider"] = effective[0]
            ev["effective_model"] = effective[1]
            ev["route_source"] = "host-session"
        path = os.path.join(self.d, ".pd", "agent-calls",
                            req["dispatch_id"] + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        return path

    def _gate(self):
        report_path = os.path.join(self.d, ".pd", "reports", "check_agent_audit.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        proc = subprocess.run([sys.executable, GATE, self.d, "--json", report_path],
                              capture_output=True, text=True, encoding="utf-8")
        with open(report_path, "r", encoding="utf-8") as f:
            return proc.returncode, json.load(f)

    # -- 用例 ---------------------------------------------------------------
    def test_same_effective_route_blocks(self):
        """审读与写作落在同一模型路由上 → 阻断（worker 不同不算分离）。"""
        writer = self._request("sec1", "T2_WRITE", "sec1.tex")
        d1 = self._artifact("sec1.tex")
        self._evidence(writer, "subagent:agent-a", d1,
                       effective=("scnet", "SCNet-Max"))
        reviewer = self._request("sec1", "T4_REVIEW_SEC1", ".pd/acceptance/sec1.json")
        d2 = self._artifact(".pd/acceptance/sec1.json", '{"ok": true}\n')
        self._evidence(reviewer, "subagent:agent-b", d2,
                       effective=("scnet", "SCNet-Max"))
        code, report = self._gate()
        self.assertEqual(code, 1)
        errs = [i for i in report["issues"] if i["severity"] == "error"]
        codes = [i["code"] for i in errs]
        self.assertIn("REVIEWER_ROUTE_NOT_SEPARATED", codes)
        detail = [i["message"] for i in errs
                  if i["code"] == "REVIEWER_ROUTE_NOT_SEPARATED"][0]
        self.assertIn("SCNet-Max", detail)
        self.assertIn("model_map", detail)

    def test_different_effective_route_passes(self):
        """生效路由不同 → 不报（这才是真正的模型层创审分离）。"""
        writer = self._request("sec1", "T2_WRITE", "sec1.tex")
        d1 = self._artifact("sec1.tex")
        self._evidence(writer, "subagent:agent-a", d1,
                       effective=("scnet", "SCNet-Max"))
        reviewer = self._request("sec1", "T4_REVIEW_SEC1", ".pd/acceptance/sec1.json")
        d2 = self._artifact(".pd/acceptance/sec1.json", '{"ok": true}\n')
        self._evidence(reviewer, "subagent:agent-b", d2,
                       effective=("intl", "Atria-Dawn"))
        code, report = self._gate()
        self.assertEqual(code, 0, [i for i in report["issues"]
                                   if i["severity"] == "error"])
        codes = [i["code"] for i in report["issues"]]
        self.assertNotIn("REVIEWER_ROUTE_NOT_SEPARATED", codes)

    def test_unrecorded_route_is_warning_not_error(self):
        """旧台账没记生效路由 → 只 WARN，不把「不知道」当「违规」。"""
        writer = self._request("sec1", "T2_WRITE", "sec1.tex")
        d1 = self._artifact("sec1.tex")
        self._evidence(writer, "subagent:agent-a", d1)  # 全 null
        reviewer = self._request("sec1", "T4_REVIEW_SEC1", ".pd/acceptance/sec1.json")
        d2 = self._artifact(".pd/acceptance/sec1.json", '{"ok": true}\n')
        self._evidence(reviewer, "subagent:agent-b", d2)  # 全 null
        code, report = self._gate()
        self.assertEqual(code, 0, [i for i in report["issues"]
                                   if i["severity"] == "error"])
        warns = [i for i in report["issues"] if i["severity"] == "warning"]
        self.assertIn("REVIEWER_ROUTE_UNRECORDED", [i["code"] for i in warns])

    def test_requested_route_does_not_mask_effective(self):
        """台账里 provider/model 是请求值；判定必须用 effective_*。"""
        writer = self._request("sec1", "T2_WRITE", "sec1.tex")
        d1 = self._artifact("sec1.tex")
        self._evidence(writer, "subagent:agent-a", d1,
                       provider="scnet", model="SCNet-Max",
                       effective=("scnet", "SCNet-Max"))
        reviewer = self._request("sec1", "T4_REVIEW_SEC1", ".pd/acceptance/sec1.json")
        d2 = self._artifact(".pd/acceptance/sec1.json", '{"ok": true}\n')
        # 请求值不同（intl/Atria-Dawn），但生效值撞车（scnet/SCNet-Max）
        self._evidence(reviewer, "subagent:agent-b", d2,
                       provider="intl", model="Atria-Dawn",
                       effective=("scnet", "SCNet-Max"))
        code, report = self._gate()
        self.assertEqual(code, 1)
        self.assertIn("REVIEWER_ROUTE_NOT_SEPARATED",
                      [i["code"] for i in report["issues"]
                       if i["severity"] == "error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
