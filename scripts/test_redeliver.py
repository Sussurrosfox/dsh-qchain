#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_redeliver.py — redeliver.py / render_smoke.py 单元测试

覆盖：
  1. redeliver --dry-run：最小夹具上打印计划且不写盘（逐字节快照对比）；
  2. digest 对齐纯函数：review-notes / acceptance / agent-calls 旧 digest
     被重算 sha256 替换、其余字段逐字节不动；
  3. render_smoke 页搜索纯函数（mock 文本页）+ fitz 可用时的两页临时
     PDF 实测（不依赖真实 XeLaTeX 编译）；
  4. render_smoke --pdfs 选择与 run_smoke 复用入口（纯文件系统，不依赖
     真实 pdftoppm 渲染：缺 PDF 不再记 problem）；
  5. make_review_pack 冒烟联动：子进程模拟成功/失败/无 PDF 三种盘面，
     以及 content_digest 不因 smoke 区漂移；
  6. deliver 冒烟硬门槛纯函数 smoke_gate_status（报告缺失/status 非 pass/
     pass+png_count/--skip-smoke/html-only）。

运行: python scripts/test_redeliver.py   （或 python -m unittest）
"""
import copy
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

import evidence  # noqa: E402
import deliver  # noqa: E402
import make_review_pack  # noqa: E402
import redeliver  # noqa: E402
import render_smoke  # noqa: E402

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

STALE = "0" * 64


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as f:
        f.write(content)


def snapshot_tree(root):
    """目录全部文件的 {rel: bytes} 快照（dry-run 不落盘断言用）。"""
    snap = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as f:
                snap[os.path.relpath(p, root).replace(os.sep, "/")] = f.read()
    return snap


class FixtureMixin(unittest.TestCase):
    """最小 self 工作区夹具：main.tex + sec1.tex + 双模假产物 + 三类旧 digest 证据。"""

    STUDENT_PDF = b"%PDF-1.4 fake student artifact\n"
    TEACHER_PDF = b"%PDF-1.4 fake teacher artifact\n"
    STUDENT_HTML = b"<html>student</html>\n"
    TEACHER_HTML = b"<html>teacher</html>\n"

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="redeliver-test-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.self_dir = os.path.join(self.d, "self")
        os.makedirs(self.self_dir)
        write_file(os.path.join(self.self_dir, "main.tex"),
                   "\\input{sec1}\n")
        write_file(os.path.join(self.self_dir, "sec1.tex"),
                   "第一节内容 \\fillin[甲]{2cm}[甲/乙]\n")
        write_file(os.path.join(self.self_dir, "config.yaml"),
                   "output_format: both\n")
        write_file(os.path.join(self.self_dir, "student.pdf"), self.STUDENT_PDF)
        write_file(os.path.join(self.self_dir, "teacher.pdf"), self.TEACHER_PDF)
        write_file(os.path.join(self.self_dir, "student.html"), self.STUDENT_HTML)
        write_file(os.path.join(self.self_dir, "teacher.html"), self.TEACHER_HTML)
        write_file(os.path.join(self.self_dir, ".pd", "reports",
                                "compare.json"), {"status": "review"})

        # 三类旧 digest 证据
        write_file(os.path.join(self.self_dir, ".pd", "review-notes.json"), {
            "schema_version": 2,
            "compare_report_sha256": STALE,
            "input_digest": STALE,
            "pdf_sha256": {"student": STALE, "teacher": STALE},
            "html_sha256": {"student": STALE, "teacher": STALE},
            "decisions": [{"issue_id": "CMP-x", "judgment": "confirmed_benign",
                           "evidence": "人工复核记录原文不得被改动"}],
        })
        write_file(os.path.join(self.self_dir, ".pd", "acceptance",
                                "sec1.json"), {
            "schema_version": 2,
            "scope": "sec1",
            "reviewed_qids": ["sec1-q1"],
            "content_digest": STALE,
            "student_pdf_sha256": STALE,
            "student_html_sha256": STALE,
            "conclusion": "PASS",
            "reviewer_role": "独立验收专员",
            "stage1_record": "阶段一记录原文",
            "stage2_record": "阶段二记录原文",
            "issues": [],
        })
        write_file(os.path.join(self.self_dir, ".pd", "agent-calls",
                                "dsp-t2-sec1.json"), {
            "schema_version": 1,
            "dispatch_id": "dsp-t2-sec1",
            "batch_id": "bat-t2-x",
            "worker": "subagent:author-dsp-t2-sec1",
            "artifacts_written": ["sec1.tex"],
            "output_digests": {"sec1.tex": STALE},
            "status": "ok",
        })


class TestDryRun(FixtureMixin):
    def test_dry_run_prints_plan_and_writes_nothing(self):
        before = snapshot_tree(self.self_dir)
        rc = redeliver.main(["--self-dir", self.self_dir, "--dry-run"])
        self.assertEqual(rc, 0)
        after = snapshot_tree(self.self_dir)
        self.assertEqual(before, after,
                         "dry-run 不得落盘：快照不一致 %s"
                         % sorted(set(before) ^ set(after)
                                  or [k for k in before
                                      if before[k] != after.get(k)]))

    def test_dry_run_skip_build(self):
        before = snapshot_tree(self.self_dir)
        rc = redeliver.main(["--self-dir", self.self_dir,
                             "--dry-run", "--skip-build"])
        self.assertEqual(rc, 0)
        self.assertEqual(before, snapshot_tree(self.self_dir))

    def test_missing_self_dir(self):
        rc = redeliver.main(["--self-dir",
                             os.path.join(self.d, "no-such-dir"), "--dry-run"])
        self.assertEqual(rc, 2)


class TestRealign(FixtureMixin):
    def _digs(self):
        return redeliver.current_digests(self.self_dir)

    def test_review_notes_realign(self):
        path = os.path.join(self.self_dir, ".pd", "review-notes.json")
        before = redeliver.load_json(path)
        original = copy.deepcopy(before)
        cmp_sha = evidence.sha256_file(
            os.path.join(self.self_dir, ".pd", "reports", "compare.json"))
        digs = self._digs()
        changes = redeliver.realign_review_notes(
            before, cmp_sha, digs["content_digest"],
            digs["pdf_sha256"], digs["html_sha256"])

        # 六处 digest 全部更新为真值
        self.assertEqual(before["compare_report_sha256"], cmp_sha)
        self.assertEqual(before["input_digest"], digs["content_digest"])
        self.assertEqual(before["pdf_sha256"]["student"],
                         sha256_bytes(self.STUDENT_PDF))
        self.assertEqual(before["pdf_sha256"]["teacher"],
                         sha256_bytes(self.TEACHER_PDF))
        self.assertEqual(before["html_sha256"]["student"],
                         sha256_bytes(self.STUDENT_HTML))
        self.assertEqual(before["html_sha256"]["teacher"],
                         sha256_bytes(self.TEACHER_HTML))
        self.assertEqual(len(changes), 6)
        # 其余字段（含 decisions 复核原文）逐字节不动
        self.assertEqual(before["decisions"], original["decisions"])
        self.assertEqual(before["schema_version"], original["schema_version"])

    def test_review_notes_noop_when_fresh(self):
        path = os.path.join(self.self_dir, ".pd", "review-notes.json")
        data = redeliver.load_json(path)
        cmp_sha = evidence.sha256_file(
            os.path.join(self.self_dir, ".pd", "reports", "compare.json"))
        digs = self._digs()
        redeliver.realign_review_notes(data, cmp_sha, digs["content_digest"],
                                       digs["pdf_sha256"], digs["html_sha256"])
        again = redeliver.realign_review_notes(
            data, cmp_sha, digs["content_digest"],
            digs["pdf_sha256"], digs["html_sha256"])
        self.assertEqual(again, [], "已新鲜的记录二次对齐应为零变更（幂等）")

    def test_acceptance_realign(self):
        path = os.path.join(self.self_dir, ".pd", "acceptance", "sec1.json")
        rec = redeliver.load_json(path)
        original = copy.deepcopy(rec)
        digs = self._digs()
        changes = redeliver.realign_acceptance(
            rec, digs["content_digest"], digs["pdf_sha256"], digs["html_sha256"])

        self.assertEqual(rec["content_digest"], digs["content_digest"])
        self.assertEqual(rec["student_pdf_sha256"],
                         sha256_bytes(self.STUDENT_PDF))
        self.assertEqual(rec["student_html_sha256"],
                         sha256_bytes(self.STUDENT_HTML))
        self.assertEqual(len(changes), 3)
        # 审读记录原文与其他字段不动
        for k in ("scope", "reviewed_qids", "conclusion", "reviewer_role",
                  "stage1_record", "stage2_record", "issues", "schema_version"):
            self.assertEqual(rec[k], original[k])

    def test_agent_call_realign(self):
        path = os.path.join(self.self_dir, ".pd", "agent-calls",
                            "dsp-t2-sec1.json")
        ev = redeliver.load_json(path)
        original = copy.deepcopy(ev)
        sec1_sha = evidence.sha256_file(os.path.join(self.self_dir, "sec1.tex"))
        changes, missing = redeliver.realign_agent_call(
            ev, lambda rel: evidence.sha256_file(
                os.path.join(self.self_dir, rel)))
        self.assertEqual(missing, [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(ev["output_digests"]["sec1.tex"], sec1_sha)
        # 证据身份字段不动（worker/dispatch_id 等）
        for k in ("schema_version", "dispatch_id", "batch_id", "worker",
                  "artifacts_written", "status"):
            self.assertEqual(ev[k], original[k])

    def test_agent_call_missing_artifact_not_rewritten(self):
        ev = {"output_digests": {"ghost.tex": STALE}}
        changes, missing = redeliver.realign_agent_call(ev, lambda rel: None)
        self.assertEqual(changes, [])
        self.assertEqual(missing, ["ghost.tex"])
        self.assertEqual(ev["output_digests"]["ghost.tex"], STALE,
                         "缺失产物的旧 digest 不得被改写（留作诚实失败信号）")

    def test_collect_and_apply_plan_end_to_end(self):
        """夹具全量：collect_plan + apply + agent-calls 二遍，断言幂等。"""
        digs = self._digs()
        cmp_sha = evidence.sha256_file(
            os.path.join(self.self_dir, ".pd", "reports", "compare.json"))
        entries, problems = redeliver.collect_plan(self.self_dir, digs, cmp_sha)
        self.assertEqual(problems, [])
        self.assertEqual(len(entries), 2)  # review-notes + acceptance/sec1
        for e in entries:
            redeliver.atomic_write_json(e["path"], e["data"])
        ac_entries, ac_problems = redeliver.collect_agent_call_plan(self.self_dir)
        self.assertEqual(ac_problems, [])
        self.assertEqual(len(ac_entries), 1)
        for e in ac_entries:
            redeliver.atomic_write_json(e["path"], e["data"])
        # 落盘后盘面应全新：evidence.validate_* 口径的 digest 全部一致
        notes = redeliver.load_json(
            os.path.join(self.self_dir, ".pd", "review-notes.json"))
        self.assertEqual(notes["input_digest"], digs["content_digest"])
        rec = redeliver.load_json(
            os.path.join(self.self_dir, ".pd", "acceptance", "sec1.json"))
        self.assertEqual(rec["content_digest"], digs["content_digest"])
        # 幂等：第二遍 collect 应为空
        entries2, _ = redeliver.collect_plan(self.self_dir, digs, cmp_sha)
        ac2, _ = redeliver.collect_agent_call_plan(self.self_dir)
        self.assertEqual(entries2, [])
        self.assertEqual(ac2, [])


class TestRenderSmokeSearch(unittest.TestCase):
    """页搜索纯函数（mock 文本页，不依赖真实 PDF 编译）。"""

    def test_find_option_page_hit(self):
        pages = ["封面与目录",
                 "填空：请选择（甲／乙／丙）",
                 "（另一种／写法）"]
        self.assertEqual(render_smoke.find_option_page(pages), 2)

    def test_find_option_page_requires_both_chars(self):
        pages = ["只有括号（没有斜杠", "只有斜杠／没有括号"]
        self.assertIsNone(render_smoke.find_option_page(pages))

    def test_find_option_page_empty(self):
        self.assertIsNone(render_smoke.find_option_page([]))
        self.assertIsNone(render_smoke.find_option_page(["", None]))

    def test_page_has_option_group(self):
        self.assertTrue(render_smoke.page_has_option_group("（a／b）"))
        # 实测 XeLaTeX 抽取常用半角斜杠：全角括号 + 半角 "/" 也算命中
        self.assertTrue(render_smoke.page_has_option_group("（H−1K/KH/HK）"))
        self.assertFalse(render_smoke.page_has_option_group("(a/b)"))  # 半角括号不算
        self.assertFalse(render_smoke.page_has_option_group("（只有括号没有斜杠）"))
        self.assertFalse(render_smoke.page_has_option_group("商群 G/N 的指数"))  # 括号外斜杠不算
        self.assertFalse(render_smoke.page_has_option_group(""))


@unittest.skipUnless(render_smoke.fitz is not None, "PyMuPDF 不可用")
class TestRenderSmokeFitz(unittest.TestCase):
    """fitz 实测：临时构造两页 PDF，验证页文本抽取与选项页定位。"""

    def test_extract_and_find_on_temp_pdf(self):
        import fitz
        d = tempfile.mkdtemp(prefix="smoke-test-")
        self.addCleanup(shutil.rmtree, d, True)
        pdf = os.path.join(d, "t.pdf")
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_text((72, 72), "page one: intro, no options")
        p2 = doc.new_page()
        # 内置简中字体（helv 不支持 CJK，抽取会得到空串）
        p2.insert_text((72, 72), "填空（甲／乙／丙）", fontname="china-s")
        doc.save(pdf)
        doc.close()
        texts = render_smoke.extract_page_texts(pdf)
        self.assertEqual(len(texts), 2)
        self.assertEqual(render_smoke.find_option_page(texts), 2)


class TestRenderSmokePdfsArg(unittest.TestCase):
    """--pdfs 解析纯函数。"""

    def test_default_and_dedupe(self):
        self.assertEqual(render_smoke.parse_pdfs_arg(None),
                         ["student", "teacher"])
        self.assertEqual(render_smoke.parse_pdfs_arg("teacher,student,teacher"),
                         ["teacher", "student"])
        self.assertEqual(render_smoke.parse_pdfs_arg("student"), ["student"])

    def test_unknown_and_empty_names_ignored(self):
        self.assertEqual(render_smoke.parse_pdfs_arg(""), [])
        self.assertEqual(render_smoke.parse_pdfs_arg(" ghost ,"), [])
        self.assertEqual(render_smoke.parse_pdfs_arg("student,students"),
                         ["student"])


class TestRenderSmokeRunSmoke(unittest.TestCase):
    """run_smoke 复用入口（mock 渲染层，只验文件系统与选择语义）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="smoke-run-")
        self.addCleanup(shutil.rmtree, self.d, True)
        write_file(os.path.join(self.d, "main.tex"), "\\input{sec1}\n")
        write_file(os.path.join(self.d, "sec1.tex"), "第一节内容\n")
        write_file(os.path.join(self.d, "config.yaml"), "output_format: both\n")

    def _fake_smoke_one_pdf(self, pdftoppm, self_dir, name, smoke_dir, problems):
        return [{"pdf": name + ".pdf",
                 "png": ".pd/review/smoke/%s-p1.png" % name,
                 "page": 1, "kind": "first_page", "pattern": None}]

    def test_missing_pdf_is_not_a_problem(self):
        """缺 PDF 不再记 problem：由调用方决定（deliver 自行核对产物）。"""
        with mock.patch.object(render_smoke, "smoke_one_pdf",
                               self._fake_smoke_one_pdf):
            report, code = render_smoke.run_smoke(self.d)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["evidence"]["png_count"], 0)
        self.assertEqual(report["evidence"]["pdfs"], [])
        self.assertFalse(os.path.exists(os.path.join(
            self.d, ".pd", "review", "smoke", "smoke-manifest.json")))

    def test_only_existing_pdf_participates(self):
        write_file(os.path.join(self.d, "student.pdf"), b"%PDF fake\n")
        with mock.patch.object(render_smoke, "smoke_one_pdf",
                               side_effect=self._fake_smoke_one_pdf) as m:
            report, code = render_smoke.run_smoke(self.d)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["evidence"]["pdfs"], ["student"])
        self.assertEqual(report["evidence"]["png_count"], 1)
        self.assertEqual([c.args[2] for c in m.call_args_list], ["student"])
        man_path = os.path.join(self.d, ".pd", "review", "smoke",
                                "smoke-manifest.json")
        self.assertTrue(os.path.isfile(man_path))
        with open(man_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["png_count"], 1)

    def test_explicit_pdfs_selection(self):
        write_file(os.path.join(self.d, "student.pdf"), b"%PDF fake\n")
        write_file(os.path.join(self.d, "teacher.pdf"), b"%PDF fake\n")
        with mock.patch.object(render_smoke, "smoke_one_pdf",
                               side_effect=self._fake_smoke_one_pdf) as m:
            report, code = render_smoke.run_smoke(self.d, pdfs="teacher")
        self.assertEqual(code, 0)
        self.assertEqual(report["evidence"]["requested_pdfs"], ["teacher"])
        self.assertEqual([c.args[2] for c in m.call_args_list], ["teacher"])

    def test_cli_help_lists_pdfs(self):
        with self.assertRaises(SystemExit) as cm:
            render_smoke.main(["--help"])
        self.assertEqual(cm.exception.code, 0)


class TestMakeReviewPackSmoke(FixtureMixin):
    """make_review_pack 冒烟联动（monkeypatch 子进程，不依赖真实渲染）。"""

    MANIFEST = os.path.join(".pd", "review", "manifest.json")

    def _run_pack(self, smoke_rc=0, smoke_items=2, smoke_exc=None,
                  smoke_stdout=None, no_pdfs=False):
        """以模拟的 pdftotext / render_smoke 子进程跑一次 make_review_pack。

        返回 (rc, manifest dict, 捕获到的 render_smoke 命令列表)。
        """
        if smoke_stdout is None:
            smoke_stdout = json.dumps({
                "status": "pass" if smoke_rc == 0 else "fail",
                "problems": [] if smoke_rc == 0 else ["未找到 pdftoppm"],
                "items": [{"png": ".pd/review/smoke/student-p1.png"}] * smoke_items,
                "manifest": ".pd/review/smoke/smoke-manifest.json",
            }, ensure_ascii=False)
        smoke_cmds = []

        def fake_run(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            if cmd and cmd[0].endswith("pdftotext"):
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="第一节内容\n\x0c===== 第 2 页 =====\n第二节\n"
                    .encode("utf-8"))
            if any("render_smoke.py" in c for c in cmd):
                smoke_cmds.append(cmd)
                if smoke_exc is not None:
                    raise smoke_exc
                return subprocess.CompletedProcess(cmd, smoke_rc,
                                                   stdout=smoke_stdout,
                                                   stderr="")
            raise AssertionError("未预期的子进程调用: %s" % cmd)

        ctx = mock.patch.object(make_review_pack.subprocess, "run",
                                side_effect=fake_run)
        if no_pdfs:
            ctx2 = mock.patch.object(make_review_pack, "_smoke_pdfs",
                                     return_value=[])
            with ctx, ctx2:
                rc = make_review_pack.main([self.self_dir])
        else:
            with ctx:
                rc = make_review_pack.main([self.self_dir])
        with open(os.path.join(self.self_dir, self.MANIFEST),
                  encoding="utf-8") as f:
            manifest = json.load(f)
        return rc, manifest, smoke_cmds

    def test_smoke_pass_records_status_and_png_count(self):
        rc, man, cmds = self._run_pack(smoke_rc=0, smoke_items=2)
        self.assertEqual(rc, 0)
        self.assertEqual(man["smoke"]["status"], "pass")
        self.assertEqual(man["smoke"]["png_count"], 2)
        self.assertEqual(man["smoke"]["smoke_dir"], ".pd/review/smoke")
        self.assertEqual(man["smoke"]["report"], ".pd/reports/render_smoke.json")
        self.assertNotIn("note", man["smoke"])
        # 冒烟经子进程调用，且 --pdfs 传入实际存在的 PDF
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][1].replace(os.sep, "/").endswith(
            "scripts/render_smoke.py"), True)
        self.assertIn("--pdfs", cmds[0])
        self.assertEqual(cmds[0][cmds[0].index("--pdfs") + 1], "student,teacher")
        self.assertIn(self.self_dir, cmds[0])

    def test_smoke_fail_does_not_fail_pack(self):
        rc, man, cmds = self._run_pack(smoke_rc=1, smoke_items=0)
        self.assertEqual(rc, 0, "冒烟失败不得让审读包失败")
        self.assertEqual(man["smoke"]["status"], "fail")
        self.assertEqual(man["smoke"]["png_count"], 0)
        self.assertIn("未找到 pdftoppm", man["smoke"]["note"])
        self.assertEqual(len(cmds), 1)

    def test_smoke_subprocess_crash_does_not_fail_pack(self):
        rc, man, _ = self._run_pack(
            smoke_exc=OSError("无可执行文件"), smoke_items=0)
        self.assertEqual(rc, 0)
        self.assertEqual(man["smoke"]["status"], "fail")
        self.assertEqual(man["smoke"]["png_count"], 0)
        self.assertIn("子进程启动失败", man["smoke"]["note"])

    def test_smoke_unparseable_output_does_not_fail_pack(self):
        rc, man, _ = self._run_pack(smoke_stdout="not a json at all",
                                    smoke_items=0)
        self.assertEqual(rc, 0)
        self.assertEqual(man["smoke"]["status"], "fail")
        self.assertIn("不可解析", man["smoke"]["note"])

    def test_smoke_skipped_when_no_pdf_at_pack_time(self):
        rc, man, cmds = self._run_pack(no_pdfs=True)
        self.assertEqual(rc, 0)
        self.assertEqual(man["smoke"]["status"], "skipped")
        self.assertEqual(man["smoke"]["png_count"], 0)
        self.assertEqual(cmds, [], "无 PDF 时不应调用 render_smoke 子进程")

    def test_run_smoke_best_effort_skipped_without_pdfs(self):
        d = tempfile.mkdtemp(prefix="pack-nopdf-")
        self.addCleanup(shutil.rmtree, d, True)
        smoke = make_review_pack.run_smoke_best_effort(d)
        self.assertEqual(smoke["status"], "skipped")
        self.assertEqual(smoke["png_count"], 0)
        self.assertEqual(smoke["smoke_dir"], ".pd/review/smoke")

    def test_content_digest_not_drifted_by_smoke_section(self):
        """smoke 区只覆盖 .pd/ 派生产物，不得进入 content_digest 输入集。"""
        in_manifest = evidence.collect_inputs(self.self_dir)
        paths = {f["path"] for f in in_manifest["files"]}
        self.assertNotIn(".pd/review/manifest.json", paths)
        self.assertFalse(any(p.startswith(".pd/") for p in paths),
                         ".pd/ 派生产物不得参与 content_digest: %s"
                         % sorted(p for p in paths if p.startswith(".pd/")))
        digest_before = evidence.content_digest(in_manifest)

        rc, man, _ = self._run_pack(smoke_rc=0, smoke_items=2)
        self.assertEqual(rc, 0)
        self.assertIn("smoke", man)
        # manifest 里的摘要仍是原有输入集的摘要（验收记录引用值不变）
        self.assertEqual(man["content_digest"], digest_before)
        self.assertEqual(man["input_digest"], digest_before)
        # 冒烟截图/报告落盘后重算，摘要依旧不变
        self.assertEqual(
            evidence.content_digest(evidence.collect_inputs(self.self_dir)),
            digest_before)


class TestDeliverSmokeGate(unittest.TestCase):
    """deliver 渲染冒烟硬门槛纯函数 smoke_gate_status（复盘 §4.4.1）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="deliver-smoke-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _write(self, rel, data):
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return path

    def _write_png(self, rel, inked=True, side=300):
        """写一张真实 PNG（N8 起门槛要解析像素，夹具不能再是占位字符串）。

        inked=True 时画一条黑带（墨迹占比约 25%），False 时整张纯白。
        """
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = []
        for y in range(side):
            row = bytearray([0])  # filter type 0
            for x in range(side):
                dark = inked and (side // 4 <= y < side // 2)
                val = 0 if dark else 255
                row.extend([val, val, val])
            rows.append(bytes(row))
        raw = b"".join(rows)

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _report(self, status="pass", png_count=2):
        return {"schema_version": 2, "tool": "render_smoke.py",
                "tool_version": "1.1", "status": status,
                "issues": ([] if status == "pass" else
                           [{"severity": "error", "message": "未找到 pdftoppm"}]),
                "evidence": {"png_count": png_count,
                             "smoke_dir": ".pd/review/smoke",
                             "manifest": ".pd/review/smoke/smoke-manifest.json",
                             "items": [{"png": "x"}] * png_count}}

    def _smoke_manifest(self, png_count=2, inked=True):
        """生成清单，并按条目数量真实写出 PNG（N8 起逐张核验）。"""
        items = []
        for i in range(png_count):
            rel = ".pd/review/smoke/student-p%s.png" % (i + 1)
            self._write_png(rel, inked=inked)
            items.append({"png": rel, "pdf": "student.pdf", "page": i + 1})
        return {"schema_version": 2, "tool": "render_smoke.py",
                "tool_version": "1.1", "png_count": png_count,
                "items": items, "advisory": True}

    def test_missing_report_fails(self):
        ok, reason = deliver.smoke_gate_status(self.d, "both")
        self.assertFalse(ok)
        self.assertIn("render_smoke.json", reason)

    def test_status_not_pass_fails(self):
        self._write(os.path.join(".pd", "reports", "render_smoke.json"),
                    self._report(status="fail", png_count=0))
        self._write(os.path.join(".pd", "review", "smoke",
                                 "smoke-manifest.json"),
                    self._smoke_manifest(0))
        ok, reason = deliver.smoke_gate_status(self.d, "both")
        self.assertFalse(ok)
        self.assertIn("fail", reason)

    def test_pass_with_manifest_png_count_passes(self):
        self._write(os.path.join(".pd", "reports", "render_smoke.json"),
                    self._report("pass", 3))
        self._write(os.path.join(".pd", "review", "smoke",
                                 "smoke-manifest.json"),
                    self._smoke_manifest(3))
        ok, reason = deliver.smoke_gate_status(self.d, "pdf")
        self.assertTrue(ok)
        self.assertIn("PNG 3 张", reason)

    def test_manifest_missing_fails(self):
        self._write(os.path.join(".pd", "reports", "render_smoke.json"),
                    self._report("pass", 2))
        ok, reason = deliver.smoke_gate_status(self.d, "both")
        self.assertFalse(ok)
        self.assertIn("smoke-manifest.json", reason)

    def test_zero_png_count_fails(self):
        self._write(os.path.join(".pd", "reports", "render_smoke.json"),
                    self._report("pass", 0))
        self._write(os.path.join(".pd", "review", "smoke",
                                 "smoke-manifest.json"),
                    self._smoke_manifest(0))
        ok, reason = deliver.smoke_gate_status(self.d, "both")
        self.assertFalse(ok)
        self.assertIn("png_count=0", reason)

    def test_skip_smoke_passes_even_with_nothing_on_disk(self):
        ok, reason = deliver.smoke_gate_status(self.d, "both", skip=True)
        self.assertTrue(ok)
        self.assertIn("--skip-smoke", reason)

    def test_html_only_skips_gate(self):
        ok, reason = deliver.smoke_gate_status(self.d, "html")
        self.assertTrue(ok)
        self.assertIn("跳过", reason)

    def test_png_count_falls_back_to_items(self):
        man = self._smoke_manifest(2)
        man.pop("png_count")
        self._write(os.path.join(".pd", "reports", "render_smoke.json"),
                    self._report("pass", 2))
        self._write(os.path.join(".pd", "review", "smoke",
                                 "smoke-manifest.json"), man)
        ok, reason = deliver.smoke_gate_status(self.d, "both")
        self.assertTrue(ok)
        self.assertIn("PNG 2 张", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
