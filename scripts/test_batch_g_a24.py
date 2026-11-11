"""A24：教学验收绑定 student.html 字节哈希 → 每次重渲染整体过期。

复现的根因（scripts/render_html.py:356）：

    "GENERATED_AT": time.strftime("%Y-%m-%d %H:%M:%S")

每次构建 student.html 都会写入当前时间戳，于是字节 sha256 **必然**变化，
即便内容一字未改。验收记录只认字节哈希时，任何一次重渲染都让
``validate_acceptance_json`` 报「绑定的 student.html 哈希与当前构建不一致」，
操作者被迫删 ``.pd/acceptance/*`` + claims + 回滚 checkpoint 重跑 T4。

修复口径：区分「字节身份」与「内容身份」。字节不同但学生可见内容
（卡片 ID 序列 + 每张卡全部可见文本，空白归一化）一致时，只是渲染层变动
——验收维持有效，只记一条 warning；内容身份不一致、或记录没登记内容
身份（无法区分）时，保持硬失败。

安全属性不变：
  - 内容身份里含题干、答案、选项、隐藏揭示区的可见文字——任何面向学生
    的改动都会改变它；
  - ``content_digest``（源内容摘要）校验照旧，不受本修复影响；
  - 没登记内容身份的旧记录仍然硬失败（fail-close），不会被自动放行。
"""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PLUGIN_ROOT = os.path.dirname(_HERE)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import evidence  # noqa: E402

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _card(cid, stem, options=("线性性", "乘法性质")):
    """一张最小 qc-card（结构对齐 render_html.render_card）。"""
    opts = "".join(
        '<button class="qc-option" data-correct="%s">%s</button>'
        % ("true" if i == 0 else "false", o)
        for i, o in enumerate(options))
    return (
        '<section class="qc-card" id="%s">\n'
        '  <div class="qc-card-head"><span class="qc-kind">问答题</span>'
        '<span class="qc-qid">%s</span></div>\n'
        '  <div class="qc-card-body"><div class="qc-stem">%s</div>\n'
        '    <div class="qc-options">%s</div></div>\n'
        '  <div class="qc-card-foot">第 %d / 3 页</div>\n'
        '</section>\n'
    ) % (cid, cid, stem, opts, 1)


def _wrap(cards, generated_at="2026-11-11 09:00:00"):
    body = "".join(cards)
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>自学案（学生版 · qchain-html 题链 HTML）</title>"
        "<style>.qc-card{color:#000}</style></head>\n<body>\n"
        "<h1>自学案</h1>\n"
        "<footer>生成时间 %s</footer>\n"
        "%s"
        "</body></html>\n"
    ) % (generated_at, body)


class TestA24ContentDigest(unittest.TestCase):
    """html_student_content_digest：渲染层无关、内容敏感。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a24-")
        self.path = os.path.join(self.tmp, "student.html")

    def _write(self, html):
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
        return self.path

    def test_same_content_different_timestamp_same_digest(self):
        """GENERATED_AT 变、内容不变 → 内容身份必须相同。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "若 $A$ 可逆，")],
                              "2026-11-11 09:00:00"))
        first = evidence.html_student_content_digest(a)
        sha_a = evidence.sha256_file(a)
        b = self._write(_wrap([_card("qc-card-CARD-01", "若 $A$ 可逆，")],
                              "2099-01-01 23:59:59"))
        second = evidence.html_student_content_digest(b)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        # 而字节哈希确实不同（否则本测试没有意义）
        self.assertNotEqual(sha_a, evidence.sha256_file(b))

    def test_css_and_asset_change_does_not_change_digest(self):
        """CSS / 资产路径 / 语言标记变动不影响内容身份。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "题干一")]))
        first = evidence.html_student_content_digest(a)
        b = self._write(
            _wrap([_card("qc-card-CARD-01", "题干一")])
            .replace("<style>.qc-card{color:#000}</style>",
                     '<link rel="stylesheet" href="assets/katex.min.css">')
            .replace('lang="zh-CN"', 'lang="en"'))
        self.assertEqual(first, evidence.html_student_content_digest(b))

    def test_stem_change_changes_digest(self):
        """题干改动 → 内容身份必须变。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "原题干")]))
        first = evidence.html_student_content_digest(a)
        b = self._write(_wrap([_card("qc-card-CARD-01", "改后题干")]))
        self.assertNotEqual(first, evidence.html_student_content_digest(b))

    def test_option_change_changes_digest(self):
        """选项改动 → 内容身份必须变（选项是学生可见内容）。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "题干")]))
        first = evidence.html_student_content_digest(a)
        b = self._write(_wrap([_card("qc-card-CARD-01", "题干",
                                     options=("线性性", "乘法性质", "分配律"))]))
        self.assertNotEqual(first, evidence.html_student_content_digest(b))

    def test_hidden_reveal_change_changes_digest(self):
        """隐藏揭示区文字改动 → 内容身份必须变（不跳过任何区域）。"""
        base = (
            '<section class="qc-card" id="qc-card-CARD-01">\n'
            '  <div class="qc-card-body"><div class="qc-stem">题干</div>\n'
            '    <div class="qc-reveal" hidden><div class="qc-solution">'
            '参考解答甲</div></div></div>\n'
            '</section>\n'
        )
        a = self._write(_wrap([base]))
        first = evidence.html_student_content_digest(a)
        b = self._write(_wrap([base.replace("参考解答甲", "参考解答乙")]))
        self.assertNotEqual(first, evidence.html_student_content_digest(b))

    def test_card_added_changes_digest(self):
        """新增卡片 → 内容身份必须变。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "题干一")]))
        first = evidence.html_student_content_digest(a)
        b = self._write(_wrap([_card("qc-card-CARD-01", "题干一"),
                               _card("qc-card-CARD-02", "题干二")]))
        self.assertNotEqual(first, evidence.html_student_content_digest(b))

    def test_whitespace_only_change_is_same_digest(self):
        """纯空白排版差异 → 内容身份不变（归一化）。"""
        a = self._write(_wrap([_card("qc-card-CARD-01", "题干一")]))
        first = evidence.html_student_content_digest(a)
        b = self._write(_wrap([_card("qc-card-CARD-01", "题干一")])
                        .replace("<h1>自学案</h1>",
                                 "<h1>自学案</h1>\n\n\n   \n"))
        self.assertEqual(first, evidence.html_student_content_digest(b))

    def test_missing_file_returns_none(self):
        """文件缺失 → None（fail-close，调用方不得当作通过）。"""
        self.assertIsNone(
            evidence.html_student_content_digest(
                os.path.join(self.tmp, "nope.html")))

    def test_no_cards_returns_none(self):
        """解析不出卡片页 → None（fail-close）。"""
        a = self._write("<html><body><p>没有卡片</p></body></html>")
        self.assertIsNone(evidence.html_student_content_digest(a))

    def test_digest_is_hex_sha256(self):
        a = self._write(_wrap([_card("qc-card-CARD-01", "题干")]))
        d = evidence.html_student_content_digest(a)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", d))


# ---------------------------------------------------------------------------
# v1 记录路径
# ---------------------------------------------------------------------------

def _v1_record(**over):
    rec = {
        "schema_version": 2,
        "scope": "sec1",
        "reviewed_qids": ["sec1-q1"],
        "content_digest": "c" * 64,
        "student_pdf_sha256": "d" * 64,
        "student_html_sha256": "e" * 64,
        "conclusion": "PASS",
        "reviewer_role": "reviewer",
        "stage1_record": "语义审读原文",
        "stage2_record": "教学审读原文",
        "issues": [],
    }
    rec.update(over)
    return rec


class TestA24V1Acceptance(unittest.TestCase):
    """v1（盲审记录）路径：字节变 + 内容一致 → 记账放行。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a24v1-")
        self.path = os.path.join(self.tmp, "student.html")
        self.content = "a" * 64
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_wrap([_card("qc-card-CARD-01", "题干一")]))
        self.cur_sha = evidence.sha256_file(self.path)
        self.assertTrue(TIMESTAMP.search(open(self.path, encoding="utf-8").read()))

    def _run(self, rec, **kw):
        warns = []
        problems = evidence.validate_acceptance_json(
            rec, current_digest="c" * 64, student_pdf_sha="d" * 64,
            student_html_sha=self.cur_sha,
            student_html_content_sha=self.content, warnings=warns)
        return problems, warns

    def test_render_only_change_passes_with_warning(self):
        """核心场景：字节哈希不同、内容身份一致 → 无 problem + 一条 warning。"""
        rec = _v1_record(**{evidence.HTML_CONTENT_FIELD: self.content})
        problems, warns = self._run(rec)
        self.assertEqual(problems, [])
        self.assertEqual(len(warns), 1)
        self.assertIn("渲染层变动", warns[0])
        self.assertIn("sec1", warns[0])

    def test_content_changed_still_fails(self):
        """内容身份不一致 → 仍然硬失败（安全属性不放松）。"""
        rec = _v1_record(**{evidence.HTML_CONTENT_FIELD: "b" * 64})
        problems, _ = self._run(rec)
        self.assertEqual(len(problems), 1)
        self.assertIn("内容身份 已改变", problems[0])

    def test_no_recorded_content_identity_still_fails(self):
        """旧记录没登记内容身份 → 无法区分，保持硬失败（fail-close）。"""
        problems, _ = self._run(_v1_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("内容身份 也未登记", problems[0])

    def test_matching_bytes_no_warning(self):
        """字节本来就一致 → 无 warning。"""
        rec = _v1_record(student_html_sha256=self.cur_sha,
                         **{evidence.HTML_CONTENT_FIELD: self.content})
        problems, warns = self._run(rec)
        self.assertEqual(problems, [])
        self.assertEqual(warns, [])

    def test_without_content_sha_argument_still_fails(self):
        """调用方没给内容身份（老调用面）→ 行为与修复前一致。"""
        rec = _v1_record(**{evidence.HTML_CONTENT_FIELD: self.content})
        problems = evidence.validate_acceptance_json(
            rec, current_digest="c" * 64, student_pdf_sha="d" * 64,
            student_html_sha=self.cur_sha)
        self.assertEqual(len(problems), 1)

    def test_warnings_none_is_tolerated(self):
        """warnings 不传（None）时不得炸。"""
        rec = _v1_record(**{evidence.HTML_CONTENT_FIELD: self.content})
        problems = evidence.validate_acceptance_json(
            rec, current_digest="c" * 64, student_pdf_sha="d" * 64,
            student_html_sha=self.cur_sha,
            student_html_content_sha=self.content, warnings=None)
        self.assertEqual(problems, [])

    def test_review_ref_content_identity_is_honoured(self):
        """内容身份登记在 content_review_refs 里（审读当时）也认。"""
        rec = _v1_record(content_review_refs={
            "review_artifact": ".pd/reviews/sec1.md",
            "review_artifact_sha256": "f" * 64,
            "dispatch_id": "dsp-t4-sec1",
            "section_digest": "1" * 64,
            "artifact_digests": {"student.html": "e" * 64,
                                 evidence.HTML_CONTENT_FIELD: self.content},
        })
        problems, warns = self._run(rec)
        self.assertEqual(problems, [])
        self.assertEqual(len(warns), 1)

    def test_delivery_ref_content_identity_is_not_trusted(self):
        """防呆：只登记在 delivery_check_ref（本轮磁盘实测）里不算数。

        那是当前值，拿来与当前值比恒等——等于把这道校验关掉。必须要求
        审读当时的内容身份。
        """
        rec = _v1_record(delivery_check_ref={
            "artifact_digests": {"student.html": "e" * 64,
                                 evidence.HTML_CONTENT_FIELD: self.content},
            "source": "本轮磁盘实测",
        })
        problems, _ = self._run(rec)
        self.assertEqual(len(problems), 1)
        self.assertIn("内容身份 也未登记", problems[0])


# ---------------------------------------------------------------------------
# v2 记录路径（合并终审投影）
# ---------------------------------------------------------------------------

def _v2_record(**over):
    rec = {
        "schema_version": 2,
        "review_kind": "combined_source_fidelity",
        "scope": "sec1",
        "reviewed_qids": ["sec1-q1"],
        "content_digest": "c" * 64,
        "output_format": "both",
        "student_pdf_sha256": "d" * 64,
        "student_html_sha256": "e" * 64,
        "conclusion": "PASS",
        "reviewer_role": "reviewer",
        "issues": [],
        "source_comparison": "逐条比对源材料与产物，未发现无依据新增。",
        "scope_content_digest": "2" * 64,
        "content_review_refs": {
            "review_artifact": ".pd/reviews/sec1.md",
            "review_artifact_sha256": "f" * 64,
            "dispatch_id": "dsp-t4-sec1",
            "batch_id": "bat-t4c-1",
            "scope": "sec1",
            "section_digest": "2" * 64,
            "artifact_digests": {"student.pdf": "d" * 64,
                                 "student.html": "e" * 64},
            "reviewed_at": "2026-11-11T09:00:00",
        },
        "delivery_check_ref": {
            "artifact_digests": {"student.pdf": "d" * 64,
                                 "student.html": "e" * 64},
            "output_format": "both",
            "source": "review_projection.py 投影时磁盘实测",
        },
        "content_review_reused": False,
        "derived_from": {
            "review_artifact": ".pd/reviews/sec1.md",
            "review_artifact_sha256": "f" * 64,
            "dispatch_id": "dsp-t4-sec1",
            "projection_schema_version": 1,
            "scope": "sec1",
        },
    }
    rec.update(over)
    return rec


class TestA24V2Acceptance(unittest.TestCase):
    """v2（合并终审投影）路径同口径。"""

    def setUp(self):
        self.content = "a" * 64
        self.cur_sha = "9" * 64

    def _run(self, rec):
        warns = []
        problems = evidence.validate_acceptance_json(
            rec, current_digest="c" * 64, student_pdf_sha="d" * 64,
            student_html_sha=self.cur_sha,
            student_html_content_sha=self.content, warnings=warns)
        return problems, warns

    def test_render_only_change_passes_with_warning(self):
        rec = _v2_record()
        rec["content_review_refs"]["artifact_digests"][
            evidence.HTML_CONTENT_FIELD] = self.content
        problems, warns = self._run(rec)
        self.assertEqual(problems, [])
        self.assertEqual(len(warns), 1)

    def test_top_level_content_identity_is_honoured(self):
        rec = _v2_record(**{evidence.HTML_CONTENT_FIELD: self.content})
        problems, warns = self._run(rec)
        self.assertEqual(problems, [])
        self.assertEqual(len(warns), 1)

    def test_content_changed_still_fails(self):
        rec = _v2_record()
        rec["content_review_refs"]["artifact_digests"][
            evidence.HTML_CONTENT_FIELD] = "b" * 64
        problems, _ = self._run(rec)
        self.assertTrue(any("student.html" in p for p in problems), problems)

    def test_no_content_identity_still_fails(self):
        problems, _ = self._run(_v2_record())
        self.assertTrue(any("student.html" in p for p in problems), problems)

    def test_delivery_ref_only_is_not_trusted(self):
        """同 v1：delivery_check_ref 里的内容身份不得作为放行依据。"""
        rec = _v2_record()
        rec["delivery_check_ref"]["artifact_digests"][
            evidence.HTML_CONTENT_FIELD] = self.content
        problems, _ = self._run(rec)
        self.assertTrue(any("student.html" in p for p in problems), problems)


# ---------------------------------------------------------------------------
# 投影侧（review_projection）
# ---------------------------------------------------------------------------

class TestA24Projection(unittest.TestCase):
    """投影时登记内容身份：只在「审读员看到的就是本轮产物」时登记。"""

    def setUp(self):
        import review_projection as rp
        self.rp = rp
        self.tmp = tempfile.mkdtemp(prefix="a24proj-")
        with open(os.path.join(self.tmp, "student.html"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(_wrap([_card("qc-card-CARD-01", "题干一")]))
        self.cur = evidence.sha256_file(
            os.path.join(self.tmp, "student.html"))
        self.record = {
            "output_format": "both",
            "input_digest": "c" * 64,
            "reviewed_at": "2026-11-11T09:00:00",
            "reviewer_role": "reviewer",
            "artifact_digests": {"student.pdf": "d" * 64,
                                 "student.html": self.cur},
        }
        self.section = {"scope": "sec1", "section_digest": "2" * 64,
                        "reviewed_qids": ["sec1-q1"],
                        "acceptance_conclusion": "PASS"}

    def _project(self, **kw):
        return self.rp.project_acceptance(
            self.record, ".pd/reviews/sec1.md", "f" * 64, self.section,
            disk_digests=kw.pop("disk", {"student.pdf": "d" * 64,
                                         "student.html": self.cur}),
            self_dir=kw.pop("self_dir", self.tmp), **kw)

    def test_same_round_registers_content_identity(self):
        rec = self._project()
        self.assertEqual(rec[evidence.HTML_CONTENT_FIELD],
                         evidence.html_student_content_digest(
                             os.path.join(self.tmp, "student.html")))

    def test_cross_round_leaves_it_empty(self):
        """审读与交付错轮 → 不登记（fail-close）。"""
        rec = self._project(disk={"student.pdf": "d" * 64,
                                  "student.html": "8" * 64})
        self.assertIsNone(rec[evidence.HTML_CONTENT_FIELD])

    def test_html_only_without_self_dir_is_empty(self):
        rec = self._project(self_dir=None)
        self.assertIsNone(rec[evidence.HTML_CONTENT_FIELD])


if __name__ == "__main__":
    unittest.main(verbosity=2)
