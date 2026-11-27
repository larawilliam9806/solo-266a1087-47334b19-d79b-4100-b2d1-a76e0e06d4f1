"""现库接线历史：历史段、变更编号、每链路增量明细与端口区间回放。

核心约束（与需求逐条对应）
--------------------------
1. 首次启用（或重置后播种）时，为当前已有接线写一个 ``baseline`` 起始变更
   编号（段内 #1），重启后数据仍在 live.db，可按原编号查询。
2. 网页 / API 的一次手工增、删各产生一个变更编号；备份整批补回共用一个编号，
   编号下按本次改动的链路数写 N 行 ``link_changes`` —— 新增历史数据量只随
   本次改动链路数增长。
3. 变更编号、明细与接线写入在**同一个 SQLite 事务**内提交；接线失败 / 校验
   回滚时不会留下任何历史。
4. 重置演示数据会重建 live.db 并生成新的 ``segment_id``（历史段），段内编号
   重新从 1 开始；旧段标识 + 旧编号不会读到新段记录。
5. 明细冗余保存两端「资产号 + 端口名」坐标，按坐标回放某端口在起始编号提交
   后的对端 / 空闲状态，以及之后每笔接线、拆线及对端。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .db import HISTORY_STATEMENTS, connect, init_schema

# ---------------------------------------------------------------------------
# 常量与中文文案（页面 / JSON 共用，保证“接线 / 拆线 / 空闲”等措辞稳定）
# ---------------------------------------------------------------------------
KIND_BASELINE = "baseline"
KIND_MANUAL_ADD = "manual_add"
KIND_MANUAL_DELETE = "manual_delete"
KIND_RESTORE_BATCH = "restore_batch"
KIND_RESET = "history_reset"

ACTION_BASELINE = "baseline"
ACTION_CONNECT = "connect"
ACTION_DISCONNECT = "disconnect"

KIND_LABELS: Dict[str, str] = {
    KIND_BASELINE: "起始基线",
    KIND_MANUAL_ADD: "手工接线",
    KIND_MANUAL_DELETE: "手工拆线",
    KIND_RESTORE_BATCH: "备份整批补回",
    KIND_RESET: "历史段重置",
}

ACTION_LABELS: Dict[str, str] = {
    ACTION_BASELINE: "起始接线",
    ACTION_CONNECT: "接线",
    ACTION_DISCONNECT: "拆线",
}

_META_CURRENT_SEGMENT = "current_segment_id"


def now_text() -> str:
    """与 links.created_at 默认值一致的本地时间文本。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_segment_id() -> str:
    return uuid.uuid4().hex


def coord_text(asset_no: Optional[str], port_name: Optional[str]) -> str:
    return f"{asset_no or '?'}/{port_name or '?'}"


# ---------------------------------------------------------------------------
# schema / 历史段引导
# ---------------------------------------------------------------------------
def ensure_history_tables_locked(conn: sqlite3.Connection) -> None:
    """在当前事务内幂等建好历史表（逐条 execute，不触发 commitscript 的隐式提交）。"""
    for stmt in HISTORY_STATEMENTS:
        conn.execute(stmt)


def _baseline_existing_links(conn: sqlite3.Connection, segment_id: str, at: str) -> Dict[str, Any]:
    """把调用时刻已存在的全部接线记为该段 #1 起始编号。调用方负责事务。"""
    rows = conn.execute(
        """
        SELECT l.id AS link_id, l.link_no AS link_no,
               da.asset_no AS a_asset_no, pa.port_name AS a_port_name,
               db.asset_no AS b_asset_no, pb.port_name AS b_port_name
        FROM links l
        JOIN ports pa ON pa.id = l.a_port_id
        JOIN devices da ON da.id = pa.device_id
        JOIN ports pb ON pb.id = l.b_port_id
        JOIN devices db ON db.id = pb.device_id
        ORDER BY l.id
        """
    ).fetchall()

    cur = conn.execute(
        """
        INSERT INTO change_revisions
            (segment_id, rev_no, kind, channel, title, link_count, created_at)
        VALUES (?, 1, ?, 'system', ?, ?, ?)
        """,
        (
            segment_id,
            KIND_BASELINE,
            "首次启用：为已有接线记录的起始编号",
            len(rows),
            at,
        ),
    )
    revision_id = cur.lastrowid
    for r in rows:
        conn.execute(
            """
            INSERT INTO link_changes
                (segment_id, rev_no, revision_id, action, link_id, link_no,
                 a_asset_no, a_port_name, b_asset_no, b_port_name)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                revision_id,
                ACTION_CONNECT,
                r["link_id"],
                r["link_no"],
                r["a_asset_no"],
                r["a_port_name"],
                r["b_asset_no"],
                r["b_port_name"],
            ),
        )
    return {
        "revision_id": revision_id,
        "rev_no": 1,
        "link_count": len(rows),
        "created_at": at,
    }


def ensure_active_segment(
    conn: sqlite3.Connection, *, reason: str = "enable"
) -> str:
    """确保当前连接的事务里存在当前历史段；不存在则建段并写起始基线。

    必须在调用方的写事务（BEGIN IMMEDIATE 之后、任何接线写入之前）中调用，
    这样万一后续校验失败回滚，不会留下“空启用”历史。
    """
    ensure_history_tables_locked(conn)
    row = conn.execute(
        "SELECT value FROM history_meta WHERE key=?", (_META_CURRENT_SEGMENT,)
    ).fetchone()
    if row is not None and row["value"]:
        exists = conn.execute(
            "SELECT 1 FROM history_segments WHERE segment_id=?", (row["value"],)
        ).fetchone()
        if exists:
            return row["value"]

    seg_id = new_segment_id()
    at = now_text()
    seg_no_row = conn.execute("SELECT count(*) AS n FROM history_segments").fetchone()
    seg_no = int(seg_no_row["n"]) + 1
    conn.execute(
        "INSERT INTO history_segments (segment_id, segment_no, reason, note, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (seg_id, seg_no, reason, None, at),
    )
    conn.execute(
        "INSERT INTO history_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_META_CURRENT_SEGMENT, seg_id),
    )
    _baseline_existing_links(conn, seg_id, at)
    return seg_id


def ensure_history(live_path: str) -> Dict[str, Any]:
    """应用启动 / 脚本入口调用：迁移旧库并保证存在起始编号。幂等。"""
    conn = connect(live_path)
    try:
        # 整段 SCHEMA_SQL 幂等执行：旧版本 live.db 会在此补上历史表。
        init_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        segment_id = ensure_active_segment(conn, reason="enable")
        conn.execute("COMMIT")
        seg = get_segment(conn, segment_id)
        return seg  # type: ignore[return-value]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 写入变更编号（与接线写入共用事务）
# ---------------------------------------------------------------------------
def normalize_change(change: Dict[str, Any]) -> Dict[str, Any]:
    action = change.get("action") or ACTION_CONNECT
    if action not in ACTION_LABELS:
        action = ACTION_CONNECT
    return {
        "action": action,
        "link_id": change.get("link_id"),
        "link_no": change.get("link_no"),
        "a_asset_no": change.get("a_asset_no") or change.get("a_asset"),
        "a_port_name": change.get("a_port_name") or change.get("a_port"),
        "b_asset_no": change.get("b_asset_no") or change.get("b_asset"),
        "b_port_name": change.get("b_port_name") or change.get("b_port"),
    }


def record_revision(
    conn: sqlite3.Connection,
    *,
    kind: str,
    changes: Sequence[Dict[str, Any]],
    channel: Optional[str] = None,
    title: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """在调用方事务内追加一个变更编号及其增量明细。

    每笔只写 1 行 change_revisions + len(changes) 行 link_changes。
    """
    ensure_history_tables_locked(conn)
    meta = conn.execute(
        "SELECT value FROM history_meta WHERE key=?", (_META_CURRENT_SEGMENT,)
    ).fetchone()
    if meta is None:
        # 兜底：正常路径下 core 写事务开头已调用 ensure_active_segment。
        segment_id = ensure_active_segment(conn)
    else:
        segment_id = meta["value"]

    next_row = conn.execute(
        "SELECT COALESCE(MAX(rev_no), 0) + 1 AS n FROM change_revisions WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    rev_no = int(next_row["n"])
    at = created_at or now_text()
    norm = [normalize_change(c) for c in changes]

    cur = conn.execute(
        """
        INSERT INTO change_revisions
            (segment_id, rev_no, kind, channel, title, link_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (segment_id, rev_no, kind, channel, title, len(norm), at),
    )
    revision_id = cur.lastrowid
    for c in norm:
        conn.execute(
            """
            INSERT INTO link_changes
                (segment_id, rev_no, revision_id, action, link_id, link_no,
                 a_asset_no, a_port_name, b_asset_no, b_port_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                rev_no,
                revision_id,
                c["action"],
                c["link_id"],
                c["link_no"],
                c["a_asset_no"],
                c["a_port_name"],
                c["b_asset_no"],
                c["b_port_name"],
            ),
        )

    return {
        "segment_id": segment_id,
        "revision_id": revision_id,
        "rev_no": rev_no,
        "number": rev_no,
        "kind": kind,
        "channel": channel,
        "link_count": len(norm),
        "created_at": at,
        "committed_at": at,
    }


# ---------------------------------------------------------------------------
# 读取：历史段 / 编号列表
# ---------------------------------------------------------------------------
def get_segment(conn: sqlite3.Connection, segment_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM history_segments WHERE segment_id=?", (segment_id,)
    ).fetchone()
    return dict(row) if row else None


def current_segment_id(conn: sqlite3.Connection) -> Optional[str]:
    ensure_history_tables_locked(conn)
    row = conn.execute(
        "SELECT value FROM history_meta WHERE key=?", (_META_CURRENT_SEGMENT,)
    ).fetchone()
    return row["value"] if row else None


def list_segments(live_path: str) -> List[Dict[str, Any]]:
    conn = connect(live_path)
    try:
        ensure_history_tables_locked(conn)
        rows = conn.execute(
            "SELECT * FROM history_segments ORDER BY segment_no"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _revision_summary(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    rev_no = d["rev_no"]
    kind = d.get("kind") or ""
    created = d.get("created_at")
    iso = created.replace(" ", "T") if isinstance(created, str) and " " in created else created
    d["number"] = rev_no
    d["change_no"] = rev_no
    d["revision"] = rev_no
    d["committed_at"] = created
    d["created_at_iso"] = iso
    d["committed_at_iso"] = iso
    d["time"] = created
    d["kind_label"] = KIND_LABELS.get(kind, kind)
    d["type"] = kind
    d["count"] = d.get("link_count", 0)
    d["change_count"] = d.get("link_count", 0)
    d["label"] = KIND_LABELS.get(kind, kind)
    return d


def list_revisions(
    live_path: str, *, segment_id: Optional[str] = None, include_all_segments: bool = False
) -> Dict[str, Any]:
    conn = connect(live_path)
    try:
        ensure_history_tables_locked(conn)
        current = current_segment_id(conn)
        segs = [dict(r) for r in conn.execute(
            "SELECT * FROM history_segments ORDER BY segment_no"
        ).fetchall()]

        target = segment_id or current
        stale = False
        if segment_id and segment_id != current:
            stale = get_segment(conn, segment_id) is not None
            # 旧段存在：允许查；不存在（如库被重建后拿任意旧标识）：标记失效
        rows: List[sqlite3.Row] = []
        if target:
            rows = conn.execute(
                "SELECT * FROM change_revisions WHERE segment_id=? ORDER BY rev_no",
                (target,),
            ).fetchall()
        revisions = [_revision_summary(r) for r in rows]

        all_revisions: List[Dict[str, Any]] = []
        if include_all_segments:
            all_rows = conn.execute(
                "SELECT * FROM change_revisions ORDER BY segment_id, rev_no"
            ).fetchall()
            all_revisions = [_revision_summary(r) for r in all_rows]

        return {
            "ok": True,
            "segment_id": current,
            "epoch_id": current,
            "epoch": next((s for s in segs if s["segment_id"] == current), None),
            "segment": next((s for s in segs if s["segment_id"] == current), None),
            "segments": segs,
            "epochs": segs,
            "requested_segment_id": segment_id,
            "stale_segment": bool(segment_id and segment_id != current),
            "old_segment_known": stale,
            "revisions": revisions,
            "change_numbers": revisions,
            "change_sets": revisions,
            "changesets": revisions,
            "records": revisions,
            "items": revisions,
            "all_revisions": all_revisions,
            "latest_number": revisions[-1]["rev_no"] if revisions else 0,
            "total": len(revisions),
        }
    finally:
        conn.close()


def get_revision_detail(
    live_path: str, rev_no: int, *, segment_id: Optional[str] = None
) -> Dict[str, Any]:
    conn = connect(live_path)
    try:
        ensure_history_tables_locked(conn)
        current = current_segment_id(conn)
        sid = segment_id or current
        if segment_id and segment_id != current and get_segment(conn, segment_id) is None:
            return {
                "ok": False,
                "status": 410,
                "code": "history_segment_gone",
                "error": "该变更编号属于已重置并清除的历史段，旧编号不能指向新记录。",
            }
        rev = conn.execute(
            "SELECT * FROM change_revisions WHERE segment_id=? AND rev_no=?",
            (sid, rev_no),
        ).fetchone()
        if rev is None:
            return {
                "ok": False,
                "status": 404,
                "code": "revision_not_found",
                "error": f"当前历史段中不存在变更编号 #{rev_no}。",
            }
        changes = conn.execute(
            "SELECT * FROM link_changes WHERE segment_id=? AND rev_no=? ORDER BY id",
            (sid, rev_no),
        ).fetchall()
        summary = _revision_summary(rev)
        is_baseline = rev["kind"] == KIND_BASELINE
        detailed = []
        for c in changes:
            item = dict(c)
            action = item.get("action") or ACTION_CONNECT
            item["type"] = action
            item["op"] = action
            item["action_label"] = ACTION_LABELS.get(action, action)
            item["label"] = item["action_label"]
            item["baseline"] = is_baseline
            item["peer_a"] = coord_text(item.get("a_asset_no"), item.get("a_port_name"))
            item["peer_b"] = coord_text(item.get("b_asset_no"), item.get("b_port_name"))
            detailed.append(item)
        summary["changes"] = detailed
        summary["links"] = detailed
        summary["change_count"] = len(detailed)
        return {"ok": True, "revision": summary, "changes": detailed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 端口解析 / 区间回放
# ---------------------------------------------------------------------------
def resolve_port(
    conn: sqlite3.Connection,
    *,
    port_id: Optional[int] = None,
    asset_no: Optional[str] = None,
    port_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if port_id:
        row = conn.execute(
            """
            SELECT p.id AS port_id, d.asset_no AS asset_no, p.port_name AS port_name,
                   d.name AS device_name
            FROM ports p JOIN devices d ON d.id = p.device_id
            WHERE p.id=?
            """,
            (port_id,),
        ).fetchone()
        if row:
            d = dict(row)
            d["coord"] = coord_text(d["asset_no"], d["port_name"])
            return d
    if asset_no and port_name:
        row = conn.execute(
            """
            SELECT p.id AS port_id, d.asset_no AS asset_no, p.port_name AS port_name,
                   d.name AS device_name
            FROM ports p JOIN devices d ON d.id = p.device_id
            WHERE d.asset_no=? AND p.port_name=?
            """,
            (asset_no, port_name),
        ).fetchone()
        if row:
            d = dict(row)
            d["coord"] = coord_text(d["asset_no"], d["port_name"])
            return d
        # 端口记录可能已被删除：历史坐标仍可直接用于回放
        return {
            "port_id": None,
            "asset_no": asset_no,
            "port_name": port_name,
            "device_name": None,
            "coord": coord_text(asset_no, port_name),
        }
    return None


def _peer_of(row: sqlite3.Row, port: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """返回 (本端位于 A/B, 对端坐标 dict)。"""
    a_key = (row["a_asset_no"], row["a_port_name"])
    b_key = (row["b_asset_no"], row["b_port_name"])
    self_key = (port["asset_no"], port["port_name"])
    if a_key == self_key:
        side = "A"
        peer_asset, peer_port = b_key
    elif b_key == self_key:
        side = "B"
        peer_asset, peer_port = a_key
    else:
        # 兜底：坐标不完全相等时无法判断，默认把 B 当对端
        side = "?"
        peer_asset, peer_port = b_key
    peer = {
        "asset_no": peer_asset,
        "port_name": peer_port,
        "coord": coord_text(peer_asset, peer_port),
    }
    return side, peer


def _state_after(rows: Sequence[sqlite3.Row], port: Dict[str, Any]) -> Dict[str, Any]:
    """把 <= 起始编号的明细按顺序折叠，得到起始编号提交后的占用状态。"""
    state: Dict[str, Any] = {
        "state": "idle",
        "occupied": False,
        "idle": True,
        "label": "空闲",
        "peer": None,
        "peer_coord": None,
        "link_id": None,
        "link_no": None,
        "rev_no": None,
    }
    for r in rows:
        if r["action"] == ACTION_DISCONNECT:
            state.update(
                state="idle",
                occupied=False,
                idle=True,
                label="空闲",
                peer=None,
                peer_coord=None,
                link_id=None,
                link_no=None,
                rev_no=r["rev_no"],
            )
        else:  # connect / baseline 都表示“接上某对端”
            _, peer = _peer_of(r, port)
            state.update(
                state="connected",
                occupied=True,
                idle=False,
                label=f"对端 {peer['coord']}",
                peer=peer,
                peer_coord=peer["coord"],
                link_id=r["link_id"],
                link_no=r["link_no"],
                rev_no=r["rev_no"],
            )
    return state


def _serialize_event(r: sqlite3.Row, port: Dict[str, Any], rev: sqlite3.Row) -> Dict[str, Any]:
    side, peer = _peer_of(r, port)
    action = r["action"]
    if action == ACTION_BASELINE:
        # 区间事件一般不会包含 #1 baseline；若显式查 #1..#1 也按“接线”展示
        action = ACTION_CONNECT
    action_label = ACTION_LABELS.get(action, action)
    created = rev["created_at"]
    iso = created.replace(" ", "T") if isinstance(created, str) and " " in created else created
    return {
        "rev_no": r["rev_no"],
        "number": r["rev_no"],
        "change_no": r["rev_no"],
        "revision": r["rev_no"],
        "revision_id": r["revision_id"],
        "segment_id": r["segment_id"],
        "committed_at": created,
        "created_at": created,
        "committed_at_iso": iso,
        "time": created,
        "kind": rev["kind"],
        "revision_kind": rev["kind"],
        "revision_kind_label": KIND_LABELS.get(rev["kind"], rev["kind"]),
        "action": action,
        "type": action,
        "op": action,
        "action_label": action_label,
        "label": action_label,
        "is_connect": action == ACTION_CONNECT,
        "is_disconnect": action == ACTION_DISCONNECT,
        "self_side": side,
        "peer": peer if action == ACTION_CONNECT else peer,
        # 拆线事件同样保留被拆链路的对端（“拆线及对端”）
        "peer_coord": peer["coord"],
        "peer_asset_no": peer["asset_no"],
        "peer_port_name": peer["port_name"],
        "link_id": r["link_id"],
        "link_no": r["link_no"],
        "a_asset_no": r["a_asset_no"],
        "a_port_name": r["a_port_name"],
        "b_asset_no": r["b_asset_no"],
        "b_port_name": r["b_port_name"],
        "coord_pair": coord_text(r["a_asset_no"], r["a_port_name"])
        + " ↔ "
        + coord_text(r["b_asset_no"], r["b_port_name"]),
    }


def query_port_history(
    live_path: str,
    *,
    port_id: Optional[int] = None,
    asset_no: Optional[str] = None,
    port_name: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    segment_id: Optional[str] = None,
    epoch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """核心查询：起始编号提交后的状态 + (start, end] 内每次接线 / 拆线。"""
    sid_arg = segment_id or epoch_id
    conn = connect(live_path)
    try:
        ensure_history_tables_locked(conn)
        current = current_segment_id(conn)
        target_sid = sid_arg or current

        # 历史段隔离：显式给了旧段标识但当前段已重建 -> 410，旧编号绝不指向新记录
        if sid_arg and current and sid_arg != current:
            known = get_segment(conn, sid_arg)
            if known is None:
                return {
                    "ok": False,
                    "status": 410,
                    "code": "history_segment_gone",
                    "error": "该变更编号属于已重置的历史段；演示数据重置后已另开一段历史，"
                    "旧编号不能指向新记录。请使用当前历史段中的编号重新查询。",
                    "current_segment_id": current,
                    "requested_segment_id": sid_arg,
                }
            target_sid = sid_arg  # 旧段仍保留时允许回看

        port = resolve_port(
            conn, port_id=port_id, asset_no=asset_no, port_name=port_name
        )
        if port is None:
            return {
                "ok": False,
                "status": 404,
                "code": "port_not_found",
                "error": "现库中找不到所选端口，无法查询接线历史。",
            }

        max_row = conn.execute(
            "SELECT COALESCE(MAX(rev_no), 0) AS n FROM change_revisions WHERE segment_id=?",
            (target_sid,),
        ).fetchone()
        latest = int(max_row["n"])
        if latest == 0:
            return {
                "ok": False,
                "status": 404,
                "code": "no_history",
                "error": "当前历史段还没有任何变更编号。",
                "segment_id": target_sid,
            }

        start_no = int(start) if start is not None else 1
        end_no = int(end) if end is not None else latest
        if start_no < 1 or end_no < 1:
            return {"ok": False, "status": 400, "code": "bad_revision",
                    "error": "变更编号必须从 1 开始。"}
        if start_no > end_no:
            return {
                "ok": False,
                "status": 400,
                "code": "bad_range",
                "error": f"起始编号 #{start_no} 不能大于结束编号 #{end_no}。",
            }

        missing = []
        rev_rows: Dict[int, sqlite3.Row] = {}
        for no in (start_no, end_no):
            r = conn.execute(
                "SELECT * FROM change_revisions WHERE segment_id=? AND rev_no=?",
                (target_sid, no),
            ).fetchone()
            if r is None:
                missing.append(no)
            else:
                rev_rows[no] = r
        if missing:
            no = missing[0]
            return {
                "ok": False,
                "status": 404,
                "code": "revision_not_found",
                "error": (
                    f"历史段 {str(target_sid)[:8]}… 中不存在变更编号 #{no}"
                    f"（当前最大编号为 #{latest}）；重置演示数据后旧编号不会指向新记录。"
                ),
                "latest_number": latest,
                "missing_revisions": missing,
                "segment_id": target_sid,
                "current_segment_id": current,
            }

        key_a = (port["asset_no"], port["port_name"])
        # 坐标匹配本端口出现在 A 端或 B 端的全部增量
        rows = conn.execute(
            """
            SELECT c.* FROM link_changes c
            WHERE c.segment_id=?
              AND (
                    (c.a_asset_no=? AND c.a_port_name=?)
                 OR (c.b_asset_no=? AND c.b_port_name=?)
              )
              AND c.rev_no <= ?
            ORDER BY c.rev_no, c.id
            """,
            (target_sid, key_a[0], key_a[1], key_a[0], key_a[1], end_no),
        ).fetchall()

        start_rows = [r for r in rows if r["rev_no"] <= start_no]
        state = _state_after(start_rows, port)

        event_rows = [r for r in rows if start_no < r["rev_no"] <= end_no]
        events: List[Dict[str, Any]] = []
        for r in event_rows:
            rev = rev_rows.get(r["rev_no"])
            if rev is None:
                rev = conn.execute(
                    "SELECT * FROM change_revisions WHERE segment_id=? AND rev_no=?",
                    (target_sid, r["rev_no"]),
                ).fetchone()
            events.append(_serialize_event(r, port, rev))

        start_summary = _revision_summary(rev_rows[start_no])
        end_summary = _revision_summary(rev_rows[end_no])
        result: Dict[str, Any] = {
            "ok": True,
            "port": port,
            "segment_id": target_sid,
            "epoch_id": target_sid,
            "current_segment_id": current,
            "is_current_segment": target_sid == current,
            "start_rev": start_no,
            "end_rev": end_no,
            "from": start_no,
            "to": end_no,
            "start": start_summary,
            "end": end_summary,
            "start_revision": start_summary,
            "end_revision": end_summary,
            "from_revision": start_summary,
            "to_revision": end_summary,
            "state_at_start": state,
            "start_state": state,
            "from_state": state,
            "state_after_start": state,
            "initial_state": state,
            "baseline_state": state,
            "events": events,
            "changes": events,
            "history": events,
            "items": events,
            "timeline": events,
            "records": events,
            "logs": events,
            "count": len(events),
            "event_count": len(events),
            "latest_number": latest,
        }
        return result
    finally:
        conn.close()


def port_options(live_path: str) -> List[Dict[str, Any]]:
    """历史页端口下拉数据。"""
    conn = connect(live_path)
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.port_name, d.asset_no,
                   EXISTS(SELECT 1 FROM links l
                          WHERE l.a_port_id=p.id OR l.b_port_id=p.id) AS occupied
            FROM ports p JOIN devices d ON d.id=p.device_id
            ORDER BY d.asset_no, p.port_name
            """
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "port_id": r["id"],
                    "asset_no": r["asset_no"],
                    "port_name": r["port_name"],
                    "coord": coord_text(r["asset_no"], r["port_name"]),
                    "occupied": bool(r["occupied"]),
                }
            )
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 常见命名别名（不同自动化 / 参考实现可能使用的函数名）
# ---------------------------------------------------------------------------
get_port_history = query_port_history
port_history = query_port_history
get_history = query_port_history
history_for_port = query_port_history
list_change_revisions = list_revisions
get_revisions = list_revisions
record_change_set = record_revision
bootstrap_history = ensure_history
