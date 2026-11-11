#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""ledger.py — 人工批准台账（approvals）与 advisory 延期台账（deferrals）的**唯一写入通路**

为什么存在（N1）：``.pd/approvals.json`` 原先只有读取方（``check_fidelity`` 的
BRIDGE / EXTENSION 判定），全仓**没有任何写入通路**。于是 faithful 模式下
BRIDGE 锚点永远拿不到批准 → 返修子 Agent 修好 A 问题后新增的任何桥接都被判
``FIDELITY_BRIDGE_UNAPPROVED``，下一轮再改再判，形成"修复-复发"死循环。
本模块补上写入口，并把人（批准人）作为必填字段固化进台账。

为什么还有 deferrals（N2）：审读 advisory 下一轮会被机械升级成 blocking，且
返修提示词要求"一轮全清"。真实教学里存在「源材料口径差」这类**无解但有理由**
的 advisory，只能由人拍板延期。deferrals 台账把这种决定落在盘面上，让投影器
把该条保持 advisory，并在返修提示里显式标注，不再逐轮重提。

两种台账都用同一套约束：
  * 必须写明 ``reason``（为什么批准/为什么延期）；
  * 必须写明人（``approved_by`` / ``deferred_by``，Agent 名字不算）；
  * 原子写（tmp + os.replace），不产生半截文件；
  * 记录当时的产物摘要（source-map / 小节切片），便于事后追责与漂移发现。

CLI::

    python scripts/ledger.py approve --self DIR --kind bridge --scope sec1 \
        --reason "原文以备注给出等价说明" --approved-by "张三"
    python scripts/ledger.py defer --self DIR --scope sec2 --code \
        FIDELITY_SOURCE_NOT_COVERED --reason "源材料未给该口径" --deferred-by "张三"
    python scripts/ledger.py list --self DIR
    python scripts/ledger.py revoke --self DIR --id BR-sec1-01 --by "张三"
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import path_safety  # noqa: E402

TOOL_NAME = "ledger.py"
TOOL_VERSION = "1.0.0"

APPROVALS_REL = ".pd/approvals.json"
DEFERRALS_REL = ".pd/deferrals.json"

APPROVAL_KINDS = {"bridge": "BR", "extension": "EXT", "extend": "EXT"}
DEFERRAL_STATUSES = ("active", "revoked", "expired")


# ---------------------------------------------------------------------------
# 读写底座
# ---------------------------------------------------------------------------

def _ledger_path(self_dir, rel):
    """台账路径：越界即报错（台账绝不允许写到 self 之外）。"""
    abs_path, _norm = path_safety.safe_join_under(self_dir, rel, label="台账文件")
    return abs_path


def _load(self_dir, rel, key):
    path = _ledger_path(self_dir, rel)
    if not os.path.isfile(path):
        return {"schema_version": 1, key: []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        # 损坏的台账不当成"空台账"静默覆盖——那会抹掉人工签发的批准。
        raise LedgerError("LEDGER_CORRUPT: %s 不是合法 JSON，拒绝改写" % rel)
    if not isinstance(data, dict):
        raise LedgerError("LEDGER_CORRUPT: %s 顶层必须是对象" % rel)
    if not isinstance(data.get(key), list):
        data[key] = []
    data.setdefault("schema_version", 1)
    return data


def _write_atomic(self_dir, rel, data):
    path = _ledger_path(self_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


class LedgerError(RuntimeError):
    """台账写入被拒（结构化错误码在消息前缀）。"""


# ---------------------------------------------------------------------------
# 读取侧（门禁 / 投影器 / 返修提示词共用）
# ---------------------------------------------------------------------------

def load_approvals(self_dir):
    """返回批准条目列表（缺文件 → 空列表）。"""
    return list(_load(self_dir, APPROVALS_REL, "approvals").get("approvals") or [])


def load_deferrals(self_dir):
    """返回延期条目列表（缺文件 → 空列表）。"""
    return list(_load(self_dir, DEFERRALS_REL, "deferrals").get("deferrals") or [])


def _today():
    return datetime.date.today().isoformat()


def _entry_active_1(entry, today=None):
    if str(entry.get("status") or "active") != "active":
        return False
    expires = str(entry.get("expires_at") or "").strip()
    if not expires:
        return True
    return expires >= (today or _today())


def active_deferrals(self_dir, today=None):
    """未撤销、未到期的延期条目。"""
    return [e for e in load_deferrals(self_dir) if _entry_active_1(e, today)]


def _norm_key(value):
    return str(value or "").strip().lower()


def deferral_matches(entry, *, scope=None, code=None, qid=None):
    """一条延期是否覆盖给定 issue。

    匹配口径刻意"宽进严出"：scope 必须相符（可缺省 = 全项目级延期），
    code / qid 至少命中一个；两者都没写就是全节级延期（由人显式承担）。
    """
    e_scope = _norm_key(entry.get("scope"))
    if e_scope and _norm_key(scope) != e_scope:
        return False
    e_code, e_qid = _norm_key(entry.get("code")), _norm_key(entry.get("qid"))
    if not e_code and not e_qid:
        return True
    if e_code and _norm_key(code) == e_code:
        return True
    if e_qid and _norm_key(qid) == e_qid:
        return True
    return False


def find_deferral(self_dir, *, scope=None, code=None, qid=None, today=None):
    """命中第一条覆盖该 issue 的生效延期（无命中 → None）。"""
    for entry in active_deferrals(self_dir, today=today):
        if deferral_matches(entry, scope=scope, code=code, qid=qid):
            return entry
    return None


# ---------------------------------------------------------------------------
# 写入侧
# ---------------------------------------------------------------------------

def _sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _require_human(value, what):
    text = str(value or "").strip()
    if not text:
        raise LedgerError(
            "LEDGER_%s_REQUIRED: %s 必填——批准/延期必须由真人拍板，"
            "Agent 不能自己给自己签发" % (what.upper(), what))
    low = text.lower()
    if low.startswith(("subagent", "agent:", "qchain", "dsh", "bot", "auto")):
        raise LedgerError(
            "LEDGER_%s_NOT_HUMAN: %r 看着是 Agent 标识；请填入真人的姓名或"
            "称呼（由主 Agent 用 ask_user_question 取得）" % (what.upper(), text))
    return text


def approve(self_dir, kind, scope, reason, approved_by, approval_id=None,
            qid=None, extra=None):
    """登记一条人工批准（bridge / extension），返回新条目。"""
    kind_norm = _norm_key(kind)
    if kind_norm not in APPROVAL_KINDS:
        raise LedgerError("LEDGER_KIND_INVALID: kind=%r，合法值 %s"
                          % (kind, sorted(APPROVAL_KINDS)))
    prefix = APPROVAL_KINDS[kind_norm]
    reason = str(reason or "").strip()
    if not reason:
        raise LedgerError("LEDGER_REASON_REQUIRED: 批准必须写明理由"
                          "（check_fidelity 要求 kind=%s 的记录带 reason）"
                          % kind_norm)
    approved_by = _require_human(approved_by, "approved_by")
    scope = str(scope or "").strip()
    if not scope:
        raise LedgerError("LEDGER_SCOPE_REQUIRED: 批准必须写明 scope"
                          "（source-map 的 BRIDGE 行按 scope 匹配）")

    data = _load(self_dir, APPROVALS_REL, "approvals")
    entries = data.setdefault("approvals", [])
    ident = str(approval_id or "").strip()
    if ident:
        ident = path_safety.sanitize_id(ident, label="批准 id")
    else:
        n = 1 + sum(1 for e in entries
                    if _norm_key(e.get("scope")) == _norm_key(scope)
                    and str(e.get("kind", "")).lower() == kind_norm)
        ident = "%s-%s-%02d" % (prefix, scope, n)
    for e in entries:
        if str(e.get("id") or "").upper() == ident.upper():
            raise LedgerError("LEDGER_ID_DUPLICATE: 批准 id %r 已存在" % ident)

    entry = {
        "id": ident,
        "kind": kind_norm,
        "scope": scope,
        "qid": str(qid or ""),
        "reason": reason,
        "approved_by": approved_by,
        "approved_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "status": "active",
        "source_map_digest": _sha256_file(os.path.join(self_dir, "source-map.md")),
        "tool": "%s/%s" % (TOOL_NAME, TOOL_VERSION),
    }
    if extra:
        entry.update(extra)
    entries.append(entry)
    _write_atomic(self_dir, APPROVALS_REL, data)
    return entry


def defer(self_dir, scope, reason, deferred_by, code=None, qid=None,
          expires_at=None, extra=None):
    """登记一条 advisory 延期，返回新条目。"""
    reason = str(reason or "").strip()
    if not reason:
        raise LedgerError("LEDGER_REASON_REQUIRED: 延期必须写明理由"
                          "（否则下一轮无从判断该不该重提）")
    deferred_by = _require_human(deferred_by, "deferred_by")
    code, qid = str(code or "").strip(), str(qid or "").strip()
    if not code and not qid:
        raise LedgerError("LEDGER_TARGET_REQUIRED: 延期必须指定 --code 或 --qid"
                          "（否则会误伤同一小节的全部 advisory）")
    scope = str(scope or "").strip()
    expires = str(expires_at or "").strip()
    if expires:
        try:
            datetime.date.fromisoformat(expires)
        except ValueError:
            raise LedgerError("LEDGER_EXPIRES_INVALID: expires_at=%r 不是 "
                              "YYYY-MM-DD" % expires)

    data = _load(self_dir, DEFERRALS_REL, "deferrals")
    entries = data.setdefault("deferrals", [])
    ident = "DF-%s-%02d" % (scope or "all", 1 + sum(
        1 for e in entries if _norm_key(e.get("scope")) == _norm_key(scope)))
    entry = {
        "id": ident,
        "scope": scope,
        "code": code,
        "qid": qid,
        "reason": reason,
        "deferred_by": deferred_by,
        "deferred_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "expires_at": expires,
        "status": "active",
        "tool": "%s/%s" % (TOOL_NAME, TOOL_VERSION),
    }
    if extra:
        entry.update(extra)
    entries.append(entry)
    _write_atomic(self_dir, DEFERRALS_REL, data)
    return entry


def revoke(self_dir, entry_id, by):
    """撤销一条批准或延期（保留原条目，只改 status + 审计字段）。"""
    by = _require_human(by, "revoked_by")
    wanted = str(entry_id or "").strip()
    if not wanted:
        raise LedgerError("LEDGER_ID_REQUIRED: 撤销必须指定 --id")
    for rel, key in ((APPROVALS_REL, "approvals"), (DEFERRALS_REL, "deferrals")):
        data = _load(self_dir, rel, key)
        for entry in data.get(key) or []:
            if str(entry.get("id") or "").upper() != wanted.upper():
                continue
            entry["status"] = "revoked"
            entry["revoked_by"] = by
            entry["revoked_at"] = datetime.datetime.now().replace(
                microsecond=0).isoformat()
            _write_atomic(self_dir, rel, data)
            return entry
    raise LedgerError("LEDGER_ID_UNKNOWN: 台账里没有 id=%r" % wanted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(payload, as_json, human_lines):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in human_lines:
        print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description="人工批准 / advisory 延期台账")
    # --json 放在子命令这一层：`ledger.py approve ... --json` 与
    # `ledger.py --json approve ...` 都要能用（Agent 两种写法都会写出来）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="结构化输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("approve", parents=[common],
                       help="登记 bridge/extension 批准")
    a.add_argument("--self", dest="self_dir", required=True)
    a.add_argument("--kind", required=True, help="bridge | extension")
    a.add_argument("--scope", required=True)
    a.add_argument("--id", dest="approval_id", default="")
    a.add_argument("--qid", default="")
    a.add_argument("--reason", required=True)
    a.add_argument("--approved-by", required=True,
                   help="真人批准人（必填；Agent 标识会被拒）")

    d = sub.add_parser("defer", parents=[common], help="登记 advisory 延期")
    d.add_argument("--self", dest="self_dir", required=True)
    d.add_argument("--scope", default="")
    d.add_argument("--code", default="")
    d.add_argument("--qid", default="")
    d.add_argument("--reason", required=True)
    d.add_argument("--deferred-by", required=True)
    d.add_argument("--expires-at", default="")

    r = sub.add_parser("revoke", parents=[common], help="撤销批准/延期")
    r.add_argument("--self", dest="self_dir", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--by", required=True)

    l = sub.add_parser("list", parents=[common], help="列出两种台账")
    l.add_argument("--self", dest="self_dir", required=True)

    args = ap.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    try:
        if args.cmd == "approve":
            entry = approve(args.self_dir, args.kind, args.scope, args.reason,
                            args.approved_by, approval_id=args.approval_id,
                            qid=args.qid)
            _emit({"success": True, "approval": entry}, as_json,
                  ["[ledger] 已登记批准 %s（kind=%s scope=%s by %s）"
                   % (entry["id"], entry["kind"], entry["scope"],
                      entry["approved_by"])])
        elif args.cmd == "defer":
            entry = defer(args.self_dir, args.scope, args.reason,
                          args.deferred_by, code=args.code, qid=args.qid,
                          expires_at=args.expires_at)
            _emit({"success": True, "deferral": entry}, as_json,
                  ["[ledger] 已登记延期 %s（scope=%s code=%s qid=%s by %s）"
                   % (entry["id"], entry["scope"] or "-", entry["code"] or "-",
                      entry["qid"] or "-", entry["deferred_by"])])
        elif args.cmd == "revoke":
            entry = revoke(args.self_dir, args.id, args.by)
            _emit({"success": True, "revoked": entry}, as_json,
                  ["[ledger] 已撤销 %s（by %s）" % (entry["id"], args.by)])
        else:
            payload = {"approvals": load_approvals(args.self_dir),
                       "deferrals": load_deferrals(args.self_dir)}
            _emit(payload, as_json,
                  ["[ledger] approvals: %d 条" % len(payload["approvals"])]
                  + ["  - %s %s scope=%s reason=%s"
                     % (e.get("id"), e.get("kind"), e.get("scope"),
                        e.get("reason")) for e in payload["approvals"]]
                  + ["[ledger] deferrals: %d 条" % len(payload["deferrals"])]
                  + ["  - %s scope=%s code=%s qid=%s reason=%s"
                     % (e.get("id"), e.get("scope"), e.get("code"), e.get("qid"),
                        e.get("reason")) for e in payload["deferrals"]])
    except (LedgerError, path_safety.PathEscape) as exc:
        _emit({"success": False, "error": str(exc)}, as_json, ["[ledger] %s" % exc])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
