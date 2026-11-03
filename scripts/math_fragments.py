#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""math_fragments.py — 全数学段提取 + 共享宏配置 + 严格 KaTeX 批量校验（任务卡02）

背景：旧 check_render 用精确正则 ``<span class="qc-math">...</span>`` 提取数学段，
于是 ``class="qc-math qc-math-display"``（展示公式）、属性换序、单引号、嵌套 span
全部漏检——坏宏在这些写法下红字交付。本模块用 HTMLParser 按 class token 收集所有
qc-math 元素，并把公式交给随包真实 KaTeX（scripts/validate_math.cjs，throwOnError）
做一次进程批量严格校验。

公共接口：
  extract_math_fragments(html, source="") -> [MathFragment]
  strip_math_delimiters(text, class_display=False) -> (body, display, error)
  load_math_config(path=None) -> dict               # templates/html/math-config.json
  math_config_script_json(config) -> str            # 可安全内嵌 <script> 的 JSON
  validate_fragments(fragments, ...) -> dict        # status ok/fail/env_error

不做的事：不执行输入材料中的 JS；不把 KaTeX 不可用静默当作通过。
"""
import json
import os
import shutil
import subprocess
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TEMPLATES_HTML = os.path.join(ROOT, "templates", "html")
DEFAULT_CONFIG_PATH = os.path.join(TEMPLATES_HTML, "math-config.json")
DEFAULT_KATEX_JS = os.path.join(TEMPLATES_HTML, "katex", "katex.min.js")
VALIDATOR_JS = os.path.join(HERE, "validate_math.cjs")

#: 环境变量：显式指定 node 可执行文件（测试 / 非常规安装）
NODE_ENV = "QCHAIN_NODE"

_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr"))


class MathFragment(object):
    """一个 qc-math 原始节点（文档序，1 起）。"""

    __slots__ = ("index", "math_id", "source", "card", "qid", "raw",
                 "display", "class_display", "text", "error")

    def __init__(self, index, source, card, qid, text, class_display):
        self.index = index
        self.math_id = "qcm-%d" % index
        self.source = source
        self.card = card
        self.qid = qid
        self.text = text                  # 解码实体后的节点全部文本（含定界符）
        self.class_display = class_display
        self.raw, self.display, self.error = strip_math_delimiters(
            text, class_display)

    def location(self):
        where = self.source or "html"
        if self.card:
            where += " 卡片 %s" % self.card
        return "%s %s" % (where, self.math_id)

    def as_dict(self):
        return {"index": self.index, "id": self.math_id,
                "source": self.source, "card": self.card, "qid": self.qid,
                "display": self.display, "error": self.error}


def strip_math_delimiters(text, class_display=False):
    r"""剥离最外层 TeX 定界符，返回 (公式正文, 是否展示公式, 错误或 None)。

    只剥掉成对的最外层 ``$$..$$`` 或 ``$..$``；不用 str.strip('$')，因此公式内
    合法字符（含转义 ``\$``）不被误删。无定界符/不成对/正文为空 → error 非空，
    正文原样保留以便后续报告。
    """
    s = text.strip()
    if len(s) >= 4 and s.startswith("$$") and s.endswith("$$") \
            and not _escaped_at(s, len(s) - 2):
        body, display, error = s[2:-2], True, None
    elif len(s) >= 2 and s.startswith("$") and s.endswith("$") \
            and not _escaped_at(s, len(s) - 1):
        body, display, error = s[1:-1], False, None
    else:
        return s, bool(class_display), "数学段缺少成对的最外层 $ 定界符"
    if not body.strip():
        error = "数学段为空"
    return body, bool(display or class_display), error


def _escaped_at(s, pos):
    """s[pos] 前面是否有奇数个反斜杠（即该字符被转义）。"""
    n = 0
    k = pos - 1
    while k >= 0 and s[k] == "\\":
        n += 1
        k -= 1
    return n % 2 == 1


class _MathCollector(HTMLParser):
    def __init__(self, source):
        # convert_charrefs=True：handle_data 收到的文本已解码实体
        HTMLParser.__init__(self, convert_charrefs=True)
        self.source = source
        self.fragments = []
        self._stack = []          # [(tag, card_id, qid)]
        self._math = None         # 当前最外层 qc-math 的采集状态
        self._index = 0

    # -- helpers
    @staticmethod
    def _classes(attrs):
        for k, v in attrs:
            if k.lower() == "class" and v:
                return v.split()
        return []

    def _current_card(self):
        for _tag, card, qid in reversed(self._stack):
            if card is not None:
                return card, qid
        return None, None

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        void = tag in _VOID_TAGS
        if self._math is not None:
            if not void:
                self._math["depth"] += 1
            return
        classes = self._classes(attrs)
        if "qc-math" in classes:
            card, qid = self._current_card()
            self._index += 1
            self._math = {"depth": 1, "text": [], "index": self._index,
                          "card": card, "qid": qid,
                          "display": "qc-math-display" in classes}
            if void:       # 异常写法 <qc-math/>：当作空段
                self._finish_math()
            return
        if void:
            return
        card_id = qid = None
        if "qc-card" in classes:
            amap = dict((k.lower(), v) for k, v in attrs)
            card_id = amap.get("id") or amap.get("data-qid") or "?"
            if card_id.startswith("qc-card-"):
                card_id = card_id[len("qc-card-"):]
            qid = amap.get("data-qid")
        self._stack.append((tag, card_id, qid))

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._math is not None:
            return                          # 自闭合：不影响深度
        if "qc-math" in self._classes(attrs):
            card, qid = self._current_card()
            self._index += 1
            self.fragments.append(MathFragment(
                self._index, self.source, card, qid, "",
                "qc-math-display" in self._classes(attrs)))
        # 其它自闭合标签无需入栈

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._math is not None:
            if tag in _VOID_TAGS:
                return
            self._math["depth"] -= 1
            if self._math["depth"] <= 0:
                self._finish_math()
            return
        for k in range(len(self._stack) - 1, -1, -1):
            if self._stack[k][0] == tag:
                del self._stack[k:]
                break

    def handle_data(self, data):
        if self._math is not None:
            self._math["text"].append(data)

    def _finish_math(self):
        m, self._math = self._math, None
        self.fragments.append(MathFragment(
            m["index"], self.source, m["card"], m["qid"],
            "".join(m["text"]), m["display"]))

    def close(self):
        HTMLParser.close(self)
        if self._math is not None:   # 未闭合的 qc-math：仍计入，由校验报错
            self._finish_math()


def extract_math_fragments(html, source=""):
    """按 class token 收集全部（最外层）qc-math 元素；顺序 = 文档序。"""
    parser = _MathCollector(source)
    parser.feed(html)
    parser.close()
    return parser.fragments


# ---------------------------------------------------------------------------
# 共享配置
# ---------------------------------------------------------------------------
def load_math_config(path=None):
    """读取共享数学配置；trust 恒强制为 False，maxExpand 必须有上限。"""
    p = path or DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("macros"), dict):
        raise ValueError("math-config.json 缺少 macros 对象: %s" % p)
    cfg["trust"] = False
    me = cfg.get("maxExpand")
    if not isinstance(me, int) or isinstance(me, bool) or me <= 0:
        raise ValueError("math-config.json maxExpand 必须是正整数上限: %s" % p)
    return cfg


def math_config_script_json(config):
    r"""序列化为可安全放入 <script type="application/json"> 的文本。

    < > & 与 U+2028/2029 全部转为 \uXXXX，防止 ``</script>`` 提前闭合；
    浏览器 JSON.parse 对这些转义还原为原字符。
    """
    text = json.dumps(config, ensure_ascii=False, sort_keys=True)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ---------------------------------------------------------------------------
# 严格校验
# ---------------------------------------------------------------------------
def find_node():
    env = os.environ.get(NODE_ENV)
    if env:
        return env if os.path.isfile(env) else None
    return shutil.which("node")


def _env_result(message, total):
    return {"status": "env_error", "engine": None, "total": total,
            "checked": 0, "failures": [], "env_error": message}


def validate_fragments(fragments, config=None, node=None, katex_js=None,
                       timeout=120):
    """把全部公式一次交给 validate_math.cjs。

    返回 dict：status ∈ {ok, fail, env_error}；engine.version；total/checked；
    failures 每项含 math_id/card/source/message/position。
    定界符/空段等 fragment.error 先于 KaTeX 记为失败（不送入引擎）。
    任一环境问题（缺 node、缺 KaTeX、输出不可解析、计数不符）均为 env_error，
    绝不静默当作通过。
    """
    total = len(fragments)
    config = config if config is not None else load_math_config()
    delim_failures = []
    items = []
    by_index = {}
    for seq, fr in enumerate(fragments, 1):
        # 批量可含多个 HTML（student/teacher 的 math_id 各自从 1 起），
        # 送入引擎用全局序号 seq 做键，回填时再映射回 fragment。
        by_index[seq] = fr
        if fr.error:
            delim_failures.append(_failure(fr, fr.error, None))
            continue
        items.append({"index": seq, "id": fr.math_id, "card": fr.card,
                      "tex": fr.raw, "display": fr.display})

    node = node or find_node()
    if not node:
        return _env_result(
            "未找到 node 可执行文件（可设置环境变量 %s）；无法进行严格 KaTeX"
            " 校验" % NODE_ENV, total)
    katex_js = katex_js or DEFAULT_KATEX_JS
    if not os.path.isfile(katex_js):
        return _env_result("随包 KaTeX 缺失: %s" % katex_js, total)
    if not os.path.isfile(VALIDATOR_JS):
        return _env_result("严格校验器缺失: %s" % VALIDATOR_JS, total)

    payload = json.dumps({"config": config, "items": items},
                         ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.run(
            [node, VALIDATOR_JS, "--katex", katex_js],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return _env_result("无法运行 node 校验器: %s" % exc, total)

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        res = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        res = None
    if not isinstance(res, dict):
        return _env_result(
            "校验器输出不可解析（exit=%s）: %s"
            % (proc.returncode,
               (proc.stderr.decode("utf-8", errors="replace") or out)[:300]),
            total)
    if res.get("status") == "env_error" or proc.returncode == 2:
        return _env_result(res.get("env_error") or "校验器环境错误", total)
    if res.get("total") != len(items) or res.get("checked") != len(items):
        return _env_result(
            "校验器计数不符：送入 %d 段，引擎 total=%s checked=%s"
            % (len(items), res.get("total"), res.get("checked")), total)

    failures = list(delim_failures)
    for f in res.get("failures", []):
        fr = by_index.get(f.get("index"))
        failures.append(_failure(fr, f.get("message", ""), f.get("position"),
                                 fallback_id=f.get("id")))
    failures.sort(key=lambda x: (x.get("source") or "", x.get("index") or 0))
    return {"status": "fail" if failures else "ok",
            "engine": res.get("engine"), "total": total, "checked": total,
            "failures": failures, "env_error": None}


def _failure(fr, message, position, fallback_id=None):
    return {"index": fr.index if fr else None,
            "math_id": fr.math_id if fr else fallback_id,
            "card": fr.card if fr else None,
            "qid": fr.qid if fr else None,
            "source": fr.source if fr else None,
            "location": fr.location() if fr else fallback_id,
            "formula": (fr.raw[:120] if fr else None),
            "message": message, "position": position}
