#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_batch_g_a13.py — A13：tex2cards 裸跑必须刷新 cards.json。

A13 原文（bug.md）::

    :1492-1496 仅带 --json PATH 才写盘 → 磁盘 cards.json 陈旧 → check_render
    H7 对账报 220≠223 / 660≠669。
    规避：每次重渲染后必须 python tex2cards.py . --json cards.json。

旧行为下 `python tex2cards.py .` 把新牌组打到 stdout、磁盘 cards.json 却停在
旧版本：人看 stdout 全是新数字，门禁看旧文件报不守恒，方向完全相反。本测试锁定：

  A13-1 裸跑刷新 ``<project>/cards.json``（内容 = 本次解析的牌组）；
  A13-2 ``--json PATH`` 仍只写 PATH、不碰规范位置（显式优先，向后兼容）；
  A13-3 ``--no-persist`` 一个字节都不写（只读查看场景）；
  A13-4 退出码语义不变：有 validate_deck 问题仍返回 1，但文件已落盘。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
TEX2CARDS = os.path.join(PLUGIN_ROOT, "scripts", "tex2cards.py")

SEC = r"""\section*{第一节}
\begin{probchain}
% qid: sec1-q1
\item \qtype{填空题}
题干。\fillin[答案]{2.0cm}[答案/别的/第三个]\anspace{0.5cm}
\begin{solution}
解答：答案是答案。
\end{solution}
\begin{teacherNote}[提示]
讲清要点。
\end{teacherNote}
\end{probchain}
"""


class A13Persist(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qchain-a13-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        with open(os.path.join(self.tmp, "main.tex"), "w", encoding="utf-8") as f:
            f.write("\\begin{document}\n\\input{sec1.tex}\n\\end{document}\n")
        with open(os.path.join(self.tmp, "sec1.tex"), "w", encoding="utf-8") as f:
            f.write(SEC)

    def _run(self, *extra):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONPATH"] = PLUGIN_ROOT
        # Windows 管道下子进程 stdout 默认按 locale(GBK) 编码；显式锁 UTF-8，
        # 否则父进程按 utf-8 解码得到乱码（同一坑即 TEV_GateCli）。
        env["PYTHONUTF8"] = "1"
        return subprocess.run([sys.executable, "-B", TEX2CARDS, self.tmp, *extra],
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", timeout=120)

    def _cards_path(self):
        return os.path.join(self.tmp, "cards.json")

    def test_bare_run_refreshes_canonical_cards_json(self):
        stale = '{"schema":"qchain-html-deck/1","cards":[],"counts":{}}'
        with open(self._cards_path(), "w", encoding="utf-8") as f:
            f.write(stale)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        with open(self._cards_path(), encoding="utf-8") as f:
            deck = json.load(f)
        kinds = [c.get("kind") for c in deck.get("cards") or []]
        self.assertIn("question", kinds, "裸跑必须刷新 cards.json（A13）")
        self.assertIn("牌组已写入", r.stdout)
        self.assertIn("cards.json", r.stdout)

    def test_bare_run_prints_summary_not_the_whole_deck(self):
        """裸跑写文件 + 打摘要行：stdout 不再倾倒整副牌组（那是 --no-persist 的活）。"""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn("牌组已写入", r.stdout)
        self.assertNotIn('"cards"', r.stdout)
        self.assertIn("通过", r.stdout)

    def test_explicit_json_path_wins_and_canonical_untouched(self):
        stale = '{"schema":"qchain-html-deck/1","cards":[],"counts":{}}'
        with open(self._cards_path(), "w", encoding="utf-8") as f:
            f.write(stale)
        other = os.path.join(self.tmp, "out", "deck.json")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        r = self._run("--json", other)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        with open(other, encoding="utf-8") as f:
            self.assertTrue(json.load(f).get("cards"))
        with open(self._cards_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), stale,
                             "--json PATH 显式优先，不写规范位置")

    def test_no_persist_writes_nothing(self):
        r = self._run("--no-persist")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertFalse(os.path.exists(self._cards_path()),
                         "--no-persist 必须一个字节都不写")
        self.assertIn('"cards"', r.stdout)

    def test_bad_deck_still_exit_1_but_file_persisted(self):
        # 缺 solution/teacherNote → validate_deck 报问题；文件仍应落盘便于排查
        with open(os.path.join(self.tmp, "sec1.tex"), "w", encoding="utf-8") as f:
            f.write(r"""\section*{第一节}
\begin{probchain}
% qid: sec1-q1
\item \qtype{填空题}
题干。\fillin[答案]{2.0cm}[答案/别的/第三个]\anspace{0.5cm}
\end{probchain}
""")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("[问题]", r.stdout)
        self.assertTrue(os.path.exists(self._cards_path()),
                        "校验失败也要落盘，便于排查真实产物")
        self.assertIn("牌组已写入", r.stdout,
                      "落盘提示与校验失败提示并存，不互相吞")

    def test_missing_output_dir_reports_error(self):
        r = self._run("--json", os.path.join(self.tmp, "nope", "deck.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("输出目录不存在", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
