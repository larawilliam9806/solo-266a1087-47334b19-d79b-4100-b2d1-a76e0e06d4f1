"""SQLite 连接与库结构。

备份库与现库使用完全相同的配线库结构，各自对应一个独立的 SQLite 文件。
匹配链路两端时只使用 (设备资产号, 端口名)，绝不使用 links.id —— 备份中的
记录号在现库可能已经被新链路复用。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Union

PathLike = Union[str, os.PathLike]

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS devices (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_no TEXT    NOT NULL UNIQUE,          -- 设备资产号
    name     TEXT    NOT NULL,                 -- 设备名称
    model    TEXT,                             -- 设备型号
    location TEXT                              -- 机房/机柜位置
);

CREATE TABLE IF NOT EXISTS ports (
    id        INTEGER PRIMARY KEY AUTOINCREMENT, -- 现库端口记录号
    device_id INTEGER NOT NULL REFERENCES devices(id),
    port_name TEXT    NOT NULL,                  -- 端口名
    port_type TEXT,                              -- 端口类型（电口/光口）
    speed     TEXT,                              -- 速率
    status    TEXT    NOT NULL DEFAULT '在用',
    remark    TEXT,
    UNIQUE (device_id, port_name)
);

CREATE TABLE IF NOT EXISTS links (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT, -- 链路记录号
    link_no                 TEXT,                               -- 链路编号
    a_port_id               INTEGER NOT NULL REFERENCES ports(id),
    b_port_id               INTEGER NOT NULL REFERENCES ports(id),
    cable_type              TEXT,
    remark                  TEXT,
    source                  TEXT NOT NULL DEFAULT 'manual',
    restored_from_backup_id INTEGER,
    created_at              TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (a_port_id <> b_port_id)
);

CREATE INDEX IF NOT EXISTS idx_links_a_port ON links(a_port_id);
CREATE INDEX IF NOT EXISTS idx_links_b_port ON links(b_port_id);

-- 每个端口最多接一条链路：无论它出现在 A 端还是 B 端都算占用。
-- 应用层在事务内预先检查，触发器作为最终兜底，防止任何写入（含直接写库）
-- 造成一个端口出现在两条链路上。
CREATE TRIGGER IF NOT EXISTS trg_links_port_unique_insert
AFTER INSERT ON links
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM links x
        WHERE x.id <> NEW.id
          AND (x.a_port_id IN (NEW.a_port_id, NEW.b_port_id)
               OR x.b_port_id IN (NEW.a_port_id, NEW.b_port_id))
    )
    THEN RAISE(ABORT, 'PORT_ALREADY_OCCUPIED')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_links_port_unique_update
BEFORE UPDATE OF a_port_id, b_port_id ON links
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM links x
        WHERE x.id <> NEW.id
          AND (x.a_port_id IN (NEW.a_port_id, NEW.b_port_id)
               OR x.b_port_id IN (NEW.a_port_id, NEW.b_port_id))
    )
    THEN RAISE(ABORT, 'PORT_ALREADY_OCCUPIED')
    END;
END;
"""

# ---------------------------------------------------------------------------
# 接线历史
#
# 历史按“历史段（segment / epoch）”组织：首次启用为当前已有接线写一个起始
# 变更编号（baseline）；重置演示数据会重建库并开启一个新的历史段，新段编号
# 重新从 1 开始，旧段编号 + 旧段标识不会指向新段记录。
#
# change_revisions：每“一笔”变更（一次手工增 / 一次手工删 / 一整批补回）一行。
# link_changes：该笔变更实际改动的每条链路一行 —— 新增一笔历史写入量只随
# 本次改动的链路数增长，而不是复制整库接线快照。
# 端点以「资产号 + 端口名」坐标冗余落库，端口记录日后被删 / 重建仍可查历史。
# ---------------------------------------------------------------------------
HISTORY_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS history_segments (
        segment_id TEXT PRIMARY KEY,              -- 历史段标识（重置后变化，用于隔离旧编号）
        segment_no INTEGER NOT NULL,              -- 第几段（从 1 开始）
        reason     TEXT NOT NULL DEFAULT 'enable',-- enable / seed / reseed
        note       TEXT,
        started_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS change_revisions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        segment_id  TEXT NOT NULL,
        rev_no      INTEGER NOT NULL,             -- 段内变更编号（从 1 开始）
        kind        TEXT NOT NULL,                -- baseline/manual_add/manual_delete/restore_batch
        channel     TEXT,                         -- web/api/system
        title       TEXT,
        link_count  INTEGER NOT NULL DEFAULT 0,   -- 本笔改动的链路数
        created_at  TEXT NOT NULL,                -- 提交时间（与接线写入同一事务）
        UNIQUE (segment_id, rev_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS link_changes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        segment_id   TEXT NOT NULL,
        rev_no       INTEGER NOT NULL,
        revision_id  INTEGER,
        action       TEXT NOT NULL,               -- baseline / connect / disconnect
        link_id      INTEGER,
        link_no      TEXT,
        a_asset_no   TEXT,
        a_port_name  TEXT,
        b_asset_no   TEXT,
        b_port_name  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_change_revisions_segment ON change_revisions(segment_id, rev_no)",
    "CREATE INDEX IF NOT EXISTS idx_link_changes_rev ON link_changes(segment_id, rev_no)",
    "CREATE INDEX IF NOT EXISTS idx_link_changes_a ON link_changes(a_asset_no, a_port_name)",
    "CREATE INDEX IF NOT EXISTS idx_link_changes_b ON link_changes(b_asset_no, b_port_name)",
]

HISTORY_SQL = ";\n".join(s.strip().rstrip(";") for s in HISTORY_STATEMENTS) + ";\n"

# 新建库时一次性建好全部表
SCHEMA_SQL = SCHEMA_SQL + "\n" + HISTORY_SQL


def connect(path: PathLike) -> sqlite3.Connection:
    """打开一个配置好的 SQLite 连接。

    isolation_level=None 使用自动提交模式；需要事务时显式执行
    BEGIN IMMEDIATE，以便确认操作拿到写锁并读到最新已提交数据。
    """
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # WAL：查历史的读连接不会挡住现库提交接线变更；文件系统不支持时
    # SQLite 会自动退回普通回滚日志模式，busy_timeout 仍负责排队。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def is_initialized(path: PathLike) -> bool:
    if not os.path.exists(path):
        return False
    try:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT count(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name IN ('devices','ports','links')"
            ).fetchone()
            return bool(row and row["n"] == 3)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
