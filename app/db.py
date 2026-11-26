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
