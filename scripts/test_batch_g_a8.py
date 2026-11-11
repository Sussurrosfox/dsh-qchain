"""A8 复现：probchain 内第二个顶层 enumerate 的内容被丢弃。

机制（tex2cards.py）：
  1. 第一个 `\\begin{enumerate}` 在 probchain 内 → 父帧 probchain + 题目未开
     parts → `meta["parts"]=True`（二级子题，收进 cur_part）。
  2. 第二个顶层 `\\begin{enumerate}` 时 `q["parts_opened"]` 已为 True → 落到
     `meta["list"]=True`（:1104）。
  3. `\\end{enumerate}` → close_cur_item + `_flush_list(fr, stack)`；父帧是
     probchain，`cur_part`/`cur_item` 均为 None → 内容塞进 **probchain 帧的
     裸缓冲 fr[2]**（_flush_list :1360）。
  4. `\\end{probchain}` 分支（:1197-1203）只消费 _stem / between_buf /
     pending_lead，**从不读 fr[2]** → 整段连同其中的 \\fillin 槽位静默消失。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tex2cards  # noqa: E402

TEX = r"""\section*{第一节}
\begin{probchain}
% qid: sec1-q1
\item \qtype{填空题}
题干第一段。
\begin{enumerate}
\item 子问一：系数是\fillin[a_{k2}]{2.0cm}[a_{k2}/x/y]\ansspace{0.5cm}
\end{enumerate}
题干续文。
\begin{enumerate}
\item 第二组一问：答案是\fillin[second]{1.0cm}[first/second/third]\ansspace{0.5cm}
\end{enumerate}
\end{probchain}
"""


class TestA8ProbchainSecondEnumerate(unittest.TestCase):
    def _deck(self, text=TEX):
        tmp = tempfile.mkdtemp(prefix="qchain-a8-")
        with open(os.path.join(tmp, "main.tex"), "w", encoding="utf-8") as fh:
            fh.write("\\begin{document}\n\\input{sec1.tex}\n\\end{document}\n")
        with open(os.path.join(tmp, "sec1.tex"), "w", encoding="utf-8") as fh:
            fh.write(text)
        deck, _issues = tex2cards.parse_project(tmp)
        return deck

    def test_second_enumerate_slots_survive(self):
        deck = self._deck()
        q = [c for c in deck["cards"] if c.get("kind") == "question"]
        self.assertEqual(len(q), 1, "应有 1 张题卡")
        answers = [s.get("answer") for s in q[0].get("slots", [])]
        self.assertIn("a_{k2}", answers,
                      "第一个 enumerate 的槽位丢失（既有回归面）")
        self.assertIn("second", answers,
                      "probchain 内第二个 enumerate 的槽位被丢弃（A8）")

    def test_second_enumerate_text_survive_in_stem(self):
        deck = self._deck()
        q = [c for c in deck["cards"] if c.get("kind") == "question"][0]
        blob = json.dumps(q, ensure_ascii=False)
        self.assertIn("第二组一问", blob,
                      "第二个 enumerate 的题干文本被丢弃（A8）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
