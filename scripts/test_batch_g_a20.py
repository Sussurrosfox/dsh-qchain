r"""批次 G / A20：check_fidelity 资产守恒的显式豁免通路（fidelity-gap 锚点）。

bug.md A20：`check_fidelity.py` 资产守恒对账只做计数，`disposition ==
"excluded"`（T1 用户排除）是唯一豁免通路。而 prompts/subagent_prompt.md
第 15 条与 prompts/acceptance_prompt.md 第 8 条承诺"对确实无法转换或保留
的资产，必须在产物中显式给出 gap 清单（逐项列出源行号、环境名、缺失原因
与建议处置）"——门禁侧此前没有任何 gap 清单解析器，按契约写 gap 注释的
作者照样被判 FIDELITY_ASSET_LOST（生产实证：sec2.tex:75/101 的 gap 占位
注释触发 dsp-repair-sec2-2d5b028478 / dsp-repair-sec6-c18a915cda 返修）。

本测试钉住：
  * 合法申报 → 从"应保留"扣除，不再报 FIDELITY_ASSET_LOST；
  * 申报不存在的资产 / 空话理由 / 未登记单元 → 各自独立错误码拦下；
  * 无申报且资产缺失 → 仍然 FIDELITY_ASSET_LOST（门禁牙齿不减）；
  * excluded 单元的原有豁免不变。

用法：python -B -m pytest scripts/test_batch_g_a20.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_fidelity as cf  # noqa: E402


def _mk_units(*specs):
    """specs: (source_id, source_file, lo, hi, disposition)"""
    return [{"source_id": sid, "source_file": sf, "locator": "L%d-%d" % (lo, hi),
             "disposition": disp} for sid, sf, lo, hi, disp in specs]


UNITS = _mk_units(
    ("SRC-026", "src/1.3.tex", 10, 39, "preserved"),
    ("SRC-031", "src/1.3.tex", 41, 79, "preserved"),
    ("SRC-099", "src/1.3.tex", 80, 120, "excluded"),
)

SRC_TEXT = "\n".join(
    ["line %d" % (i + 1) for i in range(0, 12)] +
    ["\\includegraphics{fig_1.png}"] +          # L13，属 SRC-026
    ["line"] * 25 +                             # L14-38
    ["\\begin{tikzcd}"] +                       # L39，属 SRC-026
    ["line"] * 1 +                              # L40（区间缝）
    ["line"] * 1 +                              # L41
    ["\\includegraphics{fig_2.png}"] +           # L42，属 SRC-031
    ["line"] * 37 +                             # L43-79
    ["\\includegraphics{fig_3.png}"] +           # L80，属 SRC-099（excluded）
    ["line"] * 40) + "\n"                       # L81-120

MANIFEST = {"project_root": "", "sources": [
    {"path": "src/1.3.tex", "include": True}]}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.workdir = os.path.join(self.d, "self")
        os.makedirs(self.workdir)
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "1.3.tex"), "w", encoding="utf-8") as f:
            f.write(SRC_TEXT)
        self.manifest = dict(MANIFEST, project_root=self.d)

    def _sec(self, body, name="sec1.tex"):
        with open(os.path.join(self.workdir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def _run(self, rows=None):
        return cf.check_asset_conservation(
            self.workdir, self.manifest, UNITS, rows or [],
            cf.parse_asset_gaps(self.workdir))


class TestA20GapChannel(_Tmp):

    def test_source_side_sees_three_kept_and_one_excluded(self):
        """基线：SRC-099 的图被 excluded 扣除，应保留 = 3（L13/L39/L42）。"""
        problems, warnings, stats = self._run()
        self.assertEqual(stats["source_kept"]["figure"], 3)
        self.assertEqual(stats["output"]["figure"], 0)
        self.assertEqual([p["code"] for p in problems],
                         ["FIDELITY_ASSET_LOST"])
        # 缺失清单应逐条列出源行号与归属单元
        for token in ("L13", "L39", "L42", "SRC-026", "SRC-031"):
            self.assertIn(token, problems[0]["detail"])
        self.assertNotIn("L80", problems[0]["detail"])

    def test_declared_gap_removes_the_loss(self):
        """合法申报 L13 的图 → 应保留降为 2，仍缺 2（L39/L42 未搬也未申报）。"""
        self._sec("% fidelity-gap: SRC-026 figure L13 | "
                  "原图为手写扫描，栅格化后文字不可读，正文已文字描述替代\n")
        problems, warnings, stats = self._run()
        self.assertEqual(stats["declared_gaps"], 1)
        self.assertEqual(stats["source_kept"]["figure"], 2)
        self.assertEqual([p["code"] for p in problems],
                         ["FIDELITY_ASSET_LOST"])
        # 缺失清单只该剩 L39/L42，不该再列 L13
        self.assertIn("L39", problems[0]["detail"])
        self.assertNotIn("L13", problems[0]["detail"])

    def test_all_missing_assets_declared_passes(self):
        """三个缺失资产都合法申报 → 完全通过。"""
        self._sec(
            "% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，栅格化后文字不可读，"
            "正文已文字描述替代\n"
            "% fidelity-gap: SRC-026 figure L39 | 原 TikZ 依赖未安装的宏包，"
            "已改用文字推导并在 teacherNote 说明\n"
            "% fidelity-gap: SRC-031 figure L42 | 原图为外部数据库截图，版权不明，"
            "建议人工重绘\n")
        problems, warnings, stats = self._run()
        self.assertEqual([p["code"] for p in problems], [])
        self.assertEqual(stats["declared_gaps"], 3)
        self.assertEqual(stats["source_kept"]["figure"], 0)

    def test_real_asset_kept_still_passes(self):
        """把资产真的搬进来仍然通过（既有通路不变）。"""
        self._sec("\\includegraphics{fig_1.png}\n"
                  "\\begin{tikzcd}\\end{tikzcd}\n"
                  "\\includegraphics{fig_2.png}\n")
        problems, warnings, stats = self._run()
        self.assertEqual([p["code"] for p in problems], [])
        self.assertEqual(stats["source_kept"]["figure"], 3)
        self.assertEqual(stats["output"]["figure"], 3)

    def test_gap_for_nonexistent_asset_is_rejected(self):
        """申报源侧不存在的资产行号 → FIDELITY_GAP_NO_SUCH_ASSET。"""
        self._sec("% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n"
                  "% fidelity-gap: SRC-026 figure L999 | 杜撰的一行，"
                  "源侧并不存在这个资产\n")
        problems, warnings, stats = self._run()
        self.assertIn("FIDELITY_GAP_NO_SUCH_ASSET",
                      [p["code"] for p in problems])
        # 合法那条仍生效
        self.assertEqual(stats["declared_gaps"], 1)
        self.assertEqual(stats["source_kept"]["figure"], 2)

    def test_empty_reason_is_rejected(self):
        """空话理由 → FIDELITY_GAP_NO_REASON，且不扣除。"""
        self._sec("% fidelity-gap: SRC-026 figure L13 | 无\n")
        problems, warnings, stats = self._run()
        self.assertIn("FIDELITY_GAP_NO_REASON", [p["code"] for p in problems])
        self.assertEqual(stats["declared_gaps"], 0)
        self.assertIn("FIDELITY_ASSET_LOST", [p["code"] for p in problems])

    def test_unknown_unit_is_rejected(self):
        """申报未登记单元 → FIDELITY_GAP_UNKNOWN_SOURCE。"""
        self._sec("% fidelity-gap: SRC-999 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n")
        problems, warnings, stats = self._run()
        self.assertIn("FIDELITY_GAP_UNKNOWN_SOURCE",
                      [p["code"] for p in problems])
        self.assertEqual(stats["declared_gaps"], 0)

    def test_excluded_unit_declaration_is_redundant_warning(self):
        """对已 excluded 单元申报 → warning（不扣错，也不双扣）。"""
        self._sec("% fidelity-gap: SRC-099 figure L80 | 原图为装饰性页眉图，"
                  "与教学内容无关，建议删除\n")
        problems, warnings, stats = self._run()
        self.assertEqual([p["code"] for p in problems],
                         ["FIDELITY_ASSET_LOST"])
        self.assertIn("FIDELITY_GAP_REDUNDANT",
                      [w["code"] for w in warnings])
        self.assertEqual(stats["declared_gaps"], 0)

    def test_stale_declaration_warns(self):
        """申报豁免但切片里其实有图 → warning FIDELITY_GAP_STALE。"""
        self._sec("% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n"
                  "\\includegraphics{fig_1.png}\n")
        problems, warnings, stats = self._run()
        self.assertIn("FIDELITY_GAP_STALE", [w["code"] for w in warnings])
        self.assertEqual(stats["declared_gaps"], 0)

    def test_gap_in_any_section_counts(self):
        """豁免锚点写在任意切片内都生效（按源资产归属，不按切片）。"""
        self._sec("无关正文\n", name="sec1.tex")
        self._sec("% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n", name="sec2.tex")
        problems, warnings, stats = self._run()
        self.assertEqual(stats["declared_gaps"], 1)
        self.assertEqual(stats["source_kept"]["figure"], 2)

    def test_anchor_regex_shape(self):
        """锚点语法：大小写/无 L 前缀/竖线与空格分隔都认，非法形态不认。"""
        ok = [
            "% fidelity-gap: SRC-026 figure L13 | 理由文字足够长",
            "%fidelity-gap:SRC-026 figure 13 理由文字足够长",
            "  % Fidelity-Gap:  src-026  table  L7  |  表格过宽已拆分",
            "% fidelity-gap: SRC-031 figure L40|原图依赖缺失，正文已文字描述替代",
        ]
        for line in ok:
            self.assertIsNotNone(cf.RE_ASSET_GAP.match(line), line)
        bad = [
            "% fidelity-gap: SRC-026 L13 | 理由文字足够长",     # 缺 kind
            "% fidelity-gap: SRC-026 figure | 理由文字足够长",  # 缺行号
            "% fidelity-gap: SRC-026 figure L13",              # 缺理由
            "% source: SRC-026",                               # 别的锚点
            "\\includegraphics{fig_1.png}",                    # 正文
        ]
        for line in bad:
            self.assertIsNone(cf.RE_ASSET_GAP.match(line), line)

    def test_parse_asset_gaps_reads_all_sections(self):
        self._sec("% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n", name="sec1.tex")
        self._sec("正文\n", name="sec2.tex")
        gaps = cf.parse_asset_gaps(self.workdir)
        self.assertEqual(len(gaps), 1)
        sid, kind, ln, reason, sec, idx = gaps[0]
        self.assertEqual((sid, kind, ln), ("SRC-026", "figure", 13))
        self.assertEqual(sec, "sec1.tex")
        self.assertIn("手写扫描", reason)

    def test_parse_asset_gaps_ignores_sec_template(self):
        """sec_template.tex 里的示例锚点不算申报（否则新切片自带豁免）。"""
        self._sec("% fidelity-gap: SRC-026 figure L13 | 原图为手写扫描，"
                  "栅格化后文字不可读，正文已文字描述替代\n",
                  name="sec_template.tex")
        self._sec("正文\n", name="sec1.tex")
        self.assertEqual(cf.parse_asset_gaps(self.workdir), [])
        problems, warnings, stats = self._run()
        self.assertEqual(stats["declared_gaps"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
