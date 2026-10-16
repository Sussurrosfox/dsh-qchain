"""sqlite_saver.py — Persistent (cross-process) LangGraph checkpointer.

`MemorySaver` 只活在单个进程里：CLI 先跑 `--run`、再用新 Python 进程跑
`--resume` 时，新进程的 MemorySaver 是空的，既找不到 interrupt 也找不到
checkpoint，跨进程恢复必然失败。本模块用标准库 sqlite3 实现同一接口，把
checkpoint / metadata / pending writes / channel blobs 落盘，使 CLI 与 UI 在
进程退出甚至重启后仍能按 `thread_id` 原地恢复。

不引入 `langgraph-checkpoint-sqlite` 依赖：接口面很小，自己实现可以避免新增
第三方依赖；序列化直接复用 langgraph 的 serde（`dumps_typed`/`loads_typed`），
口径与内存版一致。
"""

import json
import os
import sqlite3
import threading
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    RunnableConfig,
    WRITES_IDX_MAP,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

# 空通道值的哨兵类型（对应 MemorySaver 的 ("empty", b"")）
EMPTY_TYPE = "empty"


class _JsonOnlySerde(JsonPlusSerializer):
    """只允许 JSON/msgpack 可序列化类型的 serde（安全检查点）。

    检查点数据库落盘于工作区（.langgraph/*.sqlite），属于不可信输入面：
    即便上游 serde 版本变更或显式开启 pickle_fallback，本类也拒绝
    反序列化任何 pickle 类型标签，杜绝"投毒检查点 → resume/status 即 RCE"。
    本项目 ShrinkCoursewareState 全为 JSON 原生类型，收紧不改变功能。
    """

    def __init__(self) -> None:
        # 显式关闭 pickle 回退（langgraph>=1.x 存在该参数且缺省 False；
        # 显式声明以锁定安全语义，不随上游默认值漂移）
        try:
            super().__init__(pickle_fallback=False)
        except TypeError:
            super().__init__()

    def loads_typed(self, data):
        if isinstance(data, (tuple, list)) and data and data[0] == "pickle":
            raise ValueError(
                "检查点包含 pickle 序列化数据，已拒绝反序列化（安全策略）"
            )
        return super().loads_typed(data)

    def dumps_typed(self, obj):
        type_, blob = super().dumps_typed(obj)
        if type_ == "pickle":
            raise TypeError(
                "状态包含非 JSON 可序列化对象，已拒绝 pickle 序列化（安全策略）"
            )
        return type_, blob

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    checkpoint_type TEXT NOT NULL,
    checkpoint BLOB NOT NULL,
    metadata_type TEXT NOT NULL,
    metadata BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value BLOB NOT NULL,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE TABLE IF NOT EXISTS blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""


class SqliteSaver(BaseCheckpointSaver[str]):
    """SQLite-backed checkpointer.

    用法::

        saver = SqliteSaver("courseware_checkpoints.sqlite")
        graph = builder.compile(checkpointer=saver)

    同一路径可被多个进程安全打开（WAL 模式 + 每次操作独立事务）。
    """

    def __init__(self, path: str, *, serde=None):
        # serde 缺省为 _JsonOnlySerde（拒绝 pickle）；外部注入自定义 serde
        # 时仍被允许，但生产路径一律走安全默认。
        super().__init__(serde=serde if serde is not None else _JsonOnlySerde())
        self.path = os.path.abspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                # 初始化用的连接必须真正关闭并清掉线程缓存，
                # 否则后续 _connect() 会拿到一个已关闭的连接
                conn.close()
                self._local.conn = None

    # -- connection handling -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "SqliteSaver":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _ns(config: RunnableConfig) -> str:
        return config.get("configurable", {}).get("checkpoint_ns", "") or ""

    @staticmethod
    def _thread(config: RunnableConfig) -> str:
        return config["configurable"]["thread_id"]

    def _load_blobs(
        self, thread_id: str, checkpoint_ns: str, versions: ChannelVersions
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        conn = self._connect()
        for k, ver in versions.items():
            row = conn.execute(
                "SELECT value_type, value FROM blobs WHERE thread_id=? "
                "AND checkpoint_ns=? AND channel=? AND version=?",
                (thread_id, checkpoint_ns, k, str(ver)),
            ).fetchone()
            if row is None or row[0] == EMPTY_TYPE:
                continue
            result[k] = self.serde.loads_typed((row[0], row[1]))
        return result

    def _tuple(self, thread_id: str, checkpoint_ns: str,
               checkpoint_id: str) -> Optional[CheckpointTuple]:
        conn = self._connect()
        row = conn.execute(
            "SELECT checkpoint_type, checkpoint, metadata_type, metadata, "
            "parent_checkpoint_id FROM checkpoints "
            "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchone()
        if row is None:
            return None
        cp_type, cp_blob, md_type, md_blob, parent_id = row
        checkpoint_: Checkpoint = self.serde.loads_typed((cp_type, cp_blob))
        writes = conn.execute(
            "SELECT task_id, channel, value_type, value FROM writes "
            "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? "
            "ORDER BY task_id, idx",
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchall()
        pending = [
            (tid, ch, self.serde.loads_typed((vt, v)))
            for tid, ch, vt, v in writes
        ]
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint={
                **checkpoint_,
                "channel_values": self._load_blobs(
                    thread_id, checkpoint_ns, checkpoint_["channel_versions"]
                ),
            },
            metadata=self.serde.loads_typed((md_type, md_blob)),
            pending_writes=pending,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    # -- BaseCheckpointSaver API --------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = self._thread(config)
        checkpoint_ns = self._ns(config)
        with self._lock:
            if checkpoint_id := get_checkpoint_id(config):
                return self._tuple(thread_id, checkpoint_ns, checkpoint_id)
            conn = self._connect()
            row = conn.execute(
                "SELECT MAX(checkpoint_id) FROM checkpoints "
                "WHERE thread_id=? AND checkpoint_ns=?",
                (thread_id, checkpoint_ns),
            ).fetchone()
            if not row or not row[0]:
                return None
            return self._tuple(thread_id, checkpoint_ns, row[0])

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        with self._lock:
            conn = self._connect()
            if config is not None:
                thread_id = self._thread(config)
                config_ns: Optional[str] = self._ns(config)
                config_cid = get_checkpoint_id(config)
                rows = conn.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, "
                    "metadata_type, metadata FROM checkpoints "
                    "WHERE thread_id=? ORDER BY checkpoint_id DESC",
                    (thread_id,),
                ).fetchall()
            else:
                config_ns = None
                config_cid = None
                rows = conn.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, "
                    "metadata_type, metadata FROM checkpoints "
                    "ORDER BY thread_id, checkpoint_id DESC"
                ).fetchall()

            before_cid = get_checkpoint_id(before) if before else None
            emitted = 0
            for tid, ns, cid, md_type, md_blob in rows:
                if config_ns is not None and ns != config_ns:
                    continue
                if config_cid and cid != config_cid:
                    continue
                if before_cid and cid >= before_cid:
                    continue
                metadata = self.serde.loads_typed((md_type, md_blob))
                if filter and not all(
                    v == metadata.get(k) for k, v in filter.items()
                ):
                    continue
                if limit is not None and emitted >= limit:
                    break
                emitted += 1
                tup = self._tuple(tid, ns, cid)
                if tup is not None:
                    yield tup

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        c = checkpoint.copy()
        thread_id = self._thread(config)
        checkpoint_ns = config.get("configurable", {}).get("checkpoint_ns", "")
        values: Dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        with self._lock:
            conn = self._connect()
            for k, ver in new_versions.items():
                if k in values:
                    vtype, vblob = self.serde.dumps_typed(values[k])
                else:
                    vtype, vblob = EMPTY_TYPE, b""
                conn.execute(
                    "INSERT OR REPLACE INTO blobs "
                    "(thread_id, checkpoint_ns, channel, version, "
                    "value_type, value) VALUES (?,?,?,?,?,?)",
                    (thread_id, checkpoint_ns, k, str(ver), vtype, vblob),
                )
            cp_type, cp_blob = self.serde.dumps_typed(c)
            md_type, md_blob = self.serde.dumps_typed(
                get_checkpoint_metadata(config, metadata)
            )
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "checkpoint_type, checkpoint, metadata_type, metadata) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    config.get("configurable", {}).get("checkpoint_id"),
                    cp_type,
                    cp_blob,
                    md_type,
                    md_blob,
                ),
            )
            conn.commit()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = self._thread(config)
        checkpoint_ns = config.get("configurable", {}).get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        with self._lock:
            conn = self._connect()
            for idx, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, idx)
                if write_idx >= 0:
                    existing = conn.execute(
                        "SELECT 1 FROM writes WHERE thread_id=? AND checkpoint_ns=? "
                        "AND checkpoint_id=? AND task_id=? AND idx=?",
                        (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx),
                    ).fetchone()
                    if existing:
                        continue
                vtype, vblob = self.serde.dumps_typed(value)
                conn.execute(
                    "INSERT OR REPLACE INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                    "channel, value_type, value, task_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx,
                        channel, vtype, vblob, task_path,
                    ),
                )
            conn.commit()

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM blobs WHERE thread_id=?", (thread_id,))
            conn.commit()

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        # run_id 不由本 checkpointer 索引；保留接口兼容性
        return None

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        with self._lock:
            conn = self._connect()
            for table in ("checkpoints", "writes", "blobs"):
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(%s)" % table).fetchall()]
                rows = conn.execute(
                    "SELECT %s FROM %s WHERE thread_id=?"
                    % (",".join(cols), table), (source_thread_id,)
                ).fetchall()
                for row in rows:
                    values = tuple(
                        target_thread_id if c == "thread_id" else v
                        for c, v in zip(cols, row)
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
                        % (table, ",".join(cols), ",".join("?" * len(cols))),
                        values,
                    )
            conn.commit()


# 供外部查询/清理用的小工具 ------------------------------------------------

def thread_ids(db_path: str) -> List[str]:
    """列出某个 checkpoint 数据库中保存过的所有 thread_id。"""
    if not os.path.isfile(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def dump_json(db_path: str) -> str:
    """人类可读的 checkpoint 概览（调试用）。"""
    if not os.path.isfile(db_path):
        return "{}"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id "
            "FROM checkpoints ORDER BY thread_id, checkpoint_id"
        ).fetchall()
    except sqlite3.Error:
        return "{}"
    finally:
        conn.close()
    return json.dumps(
        [
            {
                "thread_id": t,
                "checkpoint_ns": ns,
                "checkpoint_id": cid,
                "parent_checkpoint_id": pid,
            }
            for t, ns, cid, pid in rows
        ],
        ensure_ascii=False,
        indent=2,
    )
