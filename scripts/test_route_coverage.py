"""路由表覆盖门禁：任何门禁能吐出的错误码，都必须在 issues.CODE_ROUTES 里登记。

设计动机（本次 run 的教训）：``issues.py`` 的路由表是靠人工维护的，而门禁脚本
会不断新增消息。只要有一处新码漏登记，旧版本就会靠 ``FIDELITY_`` 前缀兜底把
单字符 typo 放大成 T1 全链重跑。本测试把「漏登记」变成当场红的红灯：

* 扫描所有生产脚本（非 test_*.py）里出现的门禁码字面量；
* 扫描三个分类器（``check_numbering._PROBLEM_CODES`` /
  ``check_dialogue._PROBLEM_CODES`` / ``build._BUILD_CODES``）会返回的码；
* 扫描 ``MESSAGE_CODE_ROUTES`` 指向的码；
* 全部必须命中 ``CODE_ROUTES``，否则报出缺口清单。

新增一种错误消息而忘了配路由时，这里会先红，而不是等到流水线里空转一轮。
"""
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_ROOT)

from workflow import issues as iss  # noqa: E402

SCRIPTS_DIR = os.path.join(PLUGIN_ROOT, "scripts")
CODE_LITERAL = re.compile(
    r"""["']((?:FIDELITY|LATEX|NUMBERING|DIALOGUE|SEMANTIC|PROVENANCE|SCOPE|BUDGET|CLASSROOM|RENDER)_[A-Z0-9_]+)["']""")

# 明确不参与路由的码：由门禁内部聚合用，或属于报告分类而非可修复缺陷
IGNORED = {
    "FIDELITY_COVERAGE_FORMAT",  # warning 级行格式提示，登记在 CODE_ROUTES 但只作告警
}


def _production_files():
    for sub in ("scripts", "workflow", os.path.join("workflow", "nodes")):
        d = os.path.join(PLUGIN_ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            yield os.path.join(d, name)


def _codes_in_sources():
    found = {}
    for path in _production_files():
        text = open(path, encoding="utf-8").read()
        for code in CODE_LITERAL.findall(text):
            found.setdefault(code, os.path.basename(path))
    return found


def _classifier_codes():
    import check_numbering
    import check_dialogue
    import build

    codes = set()
    for rx, code in check_numbering._PROBLEM_CODES:
        codes.add(code)
    for rx, code in check_dialogue._PROBLEM_CODES:
        codes.add(code)
    for rx, code in build._BUILD_CODES:
        codes.add(code)
    return codes


def test_every_code_literal_has_a_route():
    missing = {c: f for c, f in _codes_in_sources().items()
               if c not in iss.CODE_ROUTES and c not in IGNORED}
    assert not missing, (
        "以下门禁/脚本会吐出的错误码没有登记路由（issues.CODE_ROUTES），"
        "请补齐（否则会退回粗粒度 GATE_ERROR_ROUTES 或触发 unclassified HALT）：\n"
        + "\n".join("  %s（出自 %s）" % (c, f) for c, f in sorted(missing.items())))


def test_every_classifier_code_has_a_route():
    missing = sorted(c for c in _classifier_codes() if c not in iss.CODE_ROUTES)
    assert not missing, (
        "门禁分类器会返回未登记的错误码：%s" % ", ".join(missing))


def test_message_route_codes_exist_in_code_routes():
    bad = {code for _rx, code in iss.MESSAGE_CODE_ROUTES if code not in iss.CODE_ROUTES}
    assert not bad, "MESSAGE_CODE_ROUTES 指向了不存在的码：%s" % sorted(bad)


def test_route_table_coverage_reports_gap():
    cov = iss.route_table_coverage(["FIDELITY_SOURCE_NOT_COVERED", "NO_SUCH_CODE_XYZ"])
    assert "NO_SUCH_CODE_XYZ" in cov["missing"]
    assert "FIDELITY_SOURCE_NOT_COVERED" not in cov["missing"]
    assert cov["known_count"] >= 1


def test_no_prefix_fallback_masks_unknown_codes():
    """收敛式兜底：前缀兜底已全部删除，未知 SCOPE_/FIDELITY_ 码必须如实报缺口。"""
    assert iss.PREFIX_ROUTES == (), (
        "前缀兜底应保持为空——任何整族兜底都会把未登记码静默改道，"
        "让路由表缺口长期看不见")
    cov = iss.route_table_coverage(["SCOPE_ANYTHING", "FIDELITY_ANYTHING"])
    assert set(cov["missing"]) == {"SCOPE_ANYTHING", "FIDELITY_ANYTHING"}


def test_every_route_entry_uses_a_declared_repair_kind():
    tables = {
        "CODE_ROUTES": iss.CODE_ROUTES,
        "GATE_ERROR_ROUTES": iss.GATE_ERROR_ROUTES,
        "PREFIX_ROUTES": {p: r for p, r in iss.PREFIX_ROUTES},
        "RENDER_MESSAGE_ROUTES": {str(rx.pattern): r
                                  for rx, r in iss.RENDER_MESSAGE_ROUTES},
        "STRICT_WARNING_GATES": iss.STRICT_WARNING_GATES,
    }
    for name, table in tables.items():
        for key, route in table.items():
            assert route.repair_kind in iss.REPAIR_KINDS, (
                "%s[%s] 的 repair_kind=%r 不在 REPAIR_KINDS 内"
                % (name, key, route.repair_kind))


def test_resource_and_environment_codes_are_not_agent():
    """资源/环境缺口绝不能派内容返修专员——这是本次最贵的一类空转。"""
    for code, route in iss.CODE_ROUTES.items():
        if code in ("LATEX_ASSET_MISSING", "LATEX_ASSET_OPAQUE",
                    "FIDELITY_ASSET_SOURCE_UNREADABLE",
                    "FIDELITY_ASSET_LOCATOR_UNKNOWN"):
            assert route.repair_kind == "asset", code
        if code in ("LATEX_FONT_MISSING", "LATEX_ENV_MISSING", "LATEX_CONFIG_FAILED"):
            assert route.repair_kind == "environment", code
