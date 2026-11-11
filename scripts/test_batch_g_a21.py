"""A21 回归：手工编辑/repurpose 场景下的血缘修复通道。

bug.md A21：check_provenance.py 要求每节 secN.tex 当前 digest 能回溯到该节
最新写作/返修 attempt 的认证证据。手工 edit、或派发单被 repurpose（改
artifacts 派发他节）都会让这一节失去认证 → PROV_WRITER_DIGEST_MISSING，
而当时的唯一解法是"给每节一个真实内容改动并走正规派发"（= 一整轮返修）。

本批新增正式登记通道 .pd/manual-edits.json：
  * 登记摘要 == 当前字节 → writer 链放行（替代硬造派发）；
  * 登记摘要 != 当前字节 → 不放行（登记后又改过，需重新登记）；
  * 登记项本身不合法（节不存在/摘要非 sha256/无理由/重复）→ 报错；
  * checker 链（语义审读）仍按 digest 绑定，手工编辑不豁免它。

安全方向逐条有反向测试兜底：登记不是永久免检金牌。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import check_provenance as cp  # noqa: E402


MAIN_TEX = "\\documentclass{article}\n\\begin{document}\n" \
           "\\input{sec1.tex}\n\\input{sec2.tex}\n\\end{document}\n"
SEC1 = "\\section{一}\n% qid: sec1-q1\n题干一\n\\fillin[甲]{2cm}\n"
SEC2 = "\\section{二}\n% qid: sec2-q1\n题干二\n\\fillin[乙]{2cm}\n"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-a21-")
        self.workdir = os.path.join(self.tmp, "self")
        os.makedirs(os.path.join(self.workdir, ".pd"), exist_ok=True)
        self._write("main.tex", MAIN_TEX)
        self._write("sec1.tex", SEC1)
        self._write("sec2.tex", SEC2)

    def _write(self, rel, text):
        path = os.path.join(self.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # newline="\n"：Windows 默认会把 \n 翻成 \r\n，digest 就对不上了
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _write_request(self, dispatch_id, artifacts, scope="sec1"):
        os.makedirs(os.path.join(self.workdir, ".pd", "agent-requests"),
                    exist_ok=True)
        req = {"dispatch_id": dispatch_id, "artifacts": list(artifacts),
               "scope": scope}
        with open(os.path.join(self.workdir, ".pd", "agent-requests",
                               dispatch_id + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False)

    def _write_ledger(self, dispatch_id, digests, status="ok"):
        os.makedirs(os.path.join(self.workdir, ".pd", "agent-calls"),
                    exist_ok=True)
        rec = {"dispatch_id": dispatch_id, "status": status,
               "worker": "subagent:author-test",
               "output_digests": dict(digests)}
        with open(os.path.join(self.workdir, ".pd", "agent-calls",
                               dispatch_id + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)

    def _write_manual(self, entries):
        data = {"schema_version": cp.MANUAL_EDITS_SCHEMA_VERSION,
                "edits": entries}
        self._write(os.path.join(".pd", "manual-edits.json"),
                    json.dumps(data, ensure_ascii=False, indent=2))

    def _check(self):
        return cp.check_provenance_project(self.workdir, mode=cp.MODE_WRITER_ONLY)


class TestBaseline(_Base):
    """派发链正常时手工登记不参与；血缘断时旧行为不变。"""

    def test_certified_by_dispatch_chain(self):
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec2.tex": _digest(SEC2)})
        res = self._check()
        self.assertEqual([e["code"] for e in res["errors"]], [])
        self.assertEqual(res["checked"]["certified"], ["sec1", "sec2"])
        self.assertEqual(res["checked"]["manual"], [])

    def test_manual_edit_breaks_provenance_without_registration(self):
        """手工改动 sec1 后不登记 → 仍然 PROV_WRITER_DIGEST_MISSING。"""
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec2.tex": _digest(SEC2)})
        self._write("sec1.tex", SEC1 + "% 人工补充一句说明\n")
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes)
        self.assertIn("sec1.tex", " ".join(e["detail"] for e in res["errors"]))


class TestA21ManualChannel(_Base):

    def _broken_lineage(self):
        """sec1 被手工改过、派发链只认证旧内容。"""
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec2.tex": _digest(SEC2)})
        edited = SEC1 + "% 人工补充一句说明\n"
        self._write("sec1.tex", edited)
        return edited

    def test_registration_repairs_writer_chain(self):
        edited = self._broken_lineage()
        self._write_manual([{"section": "sec1", "digest": _digest(edited),
                             "reason": "补充一处笔误说明",
                             "edited_by": "教研"}])
        res = self._check()
        self.assertEqual([e["code"] for e in res["errors"]], [])
        self.assertEqual(res["checked"]["certified"], ["sec1", "sec2"])
        self.assertEqual(res["checked"]["manual"], ["sec1"])

    def test_registration_must_match_current_bytes(self):
        """登记后又被改动 → 登记过期，血缘重新断裂（不是永久免检）。"""
        self._broken_lineage()
        self._write_manual([{"section": "sec1",
                             "digest": _digest(SEC1),
                             "reason": "补充一处笔误说明"}])
        # 磁盘内容已被上面改成 edited，登记写的是旧 SEC1 → 不匹配
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes)
        detail = " ".join(e["detail"] for e in res["errors"]
                          if e["code"] == "PROV_WRITER_DIGEST_MISSING")
        self.assertIn("手工编辑登记", detail)
        self.assertIn("--register-manual-edit", detail)

    def test_registration_for_unknown_section(self):
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec2.tex": _digest(SEC2)})
        self._write_manual([{"section": "sec9", "digest": "0" * 64,
                             "reason": "不存在的切片"}])
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_MANUAL_EDIT_UNKNOWN_SECTION", codes)
        # 合法节不受影响
        self.assertEqual(res["checked"]["certified"], ["sec1", "sec2"])

    def test_registration_requires_digest(self):
        self._broken_lineage()
        self._write_manual([{"section": "sec1", "digest": "不是摘要",
                             "reason": "补充一处笔误说明"}])
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_MANUAL_EDIT_BAD_DIGEST", codes)
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes)

    def test_registration_requires_reason(self):
        edited = self._broken_lineage()
        self._write_manual([{"section": "sec1", "digest": _digest(edited),
                             "reason": "无"}])
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_MANUAL_EDIT_NO_REASON", codes)

    def test_registration_cannot_bypass_repurpose(self):
        """repurpose 派发单改写他节：登记是显式通道，不是默认放行。"""
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        # dsp-2 被 repurpose：只写了 sec1（合批副作用），sec2 无人认证
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec1.tex": _digest(SEC1)})
        res = self._check()
        codes = [e["code"] for e in res["errors"]]
        self.assertIn("PROV_WRITER_DIGEST_MISSING", codes)
        # sec2 仍可通过登记恢复，但必须显式登记
        self._write_manual([{"section": "sec2", "digest": _digest(SEC2),
                             "reason": "repurpose 派发单改写了别节"}])
        res2 = self._check()
        self.assertEqual([e["code"] for e in res2["errors"]], [])
        self.assertEqual(res2["checked"]["manual"], ["sec2"])


class TestRegisterCli(_Base):

    def _cli(self, *argv):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.pop("PYTHONIOENCODING", None)
        return subprocess.run(
            [sys.executable, "-B", os.path.join(HERE, "check_provenance.py")]
            + list(argv),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)

    def test_register_then_check(self):
        self._write_request("dsp-1", ["sec1.tex"], scope="sec1")
        self._write_request("dsp-2", ["sec2.tex"], scope="sec2")
        self._write_ledger("dsp-1", {"sec1.tex": _digest(SEC1)})
        self._write_ledger("dsp-2", {"sec2.tex": _digest(SEC2)})
        self._write("sec1.tex", SEC1 + "% 人工补充一句说明\n")

        r = self._cli(self.workdir, "--register-manual-edit", "sec1",
                      "--reason", "补充一处笔误说明",
                      "--attested-by", "教研")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已登记", r.stdout)

        path = os.path.join(self.workdir, ".pd", "manual-edits.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["edits"]), 1)
        entry = data["edits"][0]
        self.assertEqual(entry["section"], "sec1")
        self.assertEqual(entry["digest"], _digest(SEC1 + "% 人工补充一句说明\n"))
        self.assertEqual(entry["edited_by"], "教研")

        r2 = self._cli(self.workdir, "--mode", "writer-only")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

        r3 = self._cli(self.workdir, "--list-manual-edits")
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        self.assertIn("sec1.tex", r3.stdout)
        self.assertIn("有效", r3.stdout)

    def test_register_requires_reason(self):
        r = self._cli(self.workdir, "--register-manual-edit", "sec1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--reason", r.stderr)

    def test_register_rejects_missing_section(self):
        r = self._cli(self.workdir, "--register-manual-edit", "sec9",
                      "--reason", "不存在的切片")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("不存在", r.stdout)

    def test_register_is_idempotent_per_section(self):
        self._write("sec1.tex", SEC1 + "% 第一次\n")
        ok, _ = cp.register_manual_edit(self.workdir, "sec1", "第一次改动")
        self.assertTrue(ok)
        self._write("sec1.tex", SEC1 + "% 第二次\n")
        ok, _ = cp.register_manual_edit(self.workdir, "sec1", "第二次改动")
        self.assertTrue(ok)
        edits = cp.load_manual_edits(self.workdir)
        self.assertEqual(list(edits), ["sec1"])
        self.assertEqual(edits["sec1"]["reason"], "第二次改动")
        self.assertEqual(edits["sec1"]["digest"],
                         _digest(SEC1 + "% 第二次\n"))

    def test_list_reports_stale_entry(self):
        self._write_manual([{"section": "sec1", "digest": "0" * 64,
                             "reason": "随便写的一个摘要"}])
        r = self._cli(self.workdir, "--list-manual-edits")
        self.assertEqual(r.returncode, 0)
        self.assertIn("已过期", r.stdout)


class TestLoadRobustness(_Base):

    def test_missing_file_is_empty(self):
        self.assertEqual(cp.load_manual_edits(self.workdir), {})

    def test_broken_json_is_empty(self):
        self._write(os.path.join(".pd", "manual-edits.json"), "{ 坏掉的 json")
        self.assertEqual(cp.load_manual_edits(self.workdir), {})

    def test_wrong_root_type_is_empty(self):
        self._write(os.path.join(".pd", "manual-edits.json"), "[]")
        self.assertEqual(cp.load_manual_edits(self.workdir), {})

    def test_malformed_entries_are_skipped(self):
        self._write_manual(["不是对象", {"section": ""}, {"no_section": 1}])
        self.assertEqual(cp.load_manual_edits(self.workdir), {})


if __name__ == "__main__":
    unittest.main()
