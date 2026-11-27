"""配线链路恢复的核心领域逻辑。

设计要点
--------
1. 两端只按 (设备资产号 asset_no, 端口名 port_name) 匹配现库端口记录，
   绝不使用备份 links.id —— 该记录号在现库可能已被复用。
2. 一个端口最多一条链路：在 links 的 A 端或 B 端出现都算占用。
3. 预览时对“所列现库端口记录”做快照并签名带到确认请求；确认时在
   ``BEGIN IMMEDIATE`` 事务内重新读取现库：
   - 端口记录必须与预览快照一致（检测删除/重建/改字段）；
   - 端口此刻必须空闲（检测预览后的并发占用）；
   - 同批内部不能重复使用同一端口；
   全部通过才一次性插入并提交，任一冲突则回滚，整批不写入。
4. 与所选端口无关的现库写入不会影响上述任何检查。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .db import connect
from . import history
# 常见命名的直接再导出，方便脚本 / 自动化从 app.core 引用历史查询能力
from .history import (  # noqa: F401
    ensure_history,
    list_revisions,
    query_port_history,
    record_revision,
)

# 进入签名快照的现库端口记录字段（即预览页面“所列记录”的全部字段）
PORT_FIELDS = [
    "port_id",
    "device_id",
    "asset_no",
    "device_name",
    "model",
    "location",
    "port_name",
    "port_type",
    "speed",
    "status",
    "remark",
]

FIELD_LABELS = {
    "port_id": "现库端口记录号",
    "device_id": "设备内部ID",
    "asset_no": "设备资产号",
    "device_name": "设备名称",
    "model": "设备型号",
    "location": "机房位置",
    "port_name": "端口名",
    "port_type": "端口类型",
    "speed": "速率",
    "status": "端口状态",
    "remark": "备注",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def coord_key(asset_no: str, port_name: str) -> str:
    return f"{asset_no}\x1f{port_name}"


def normalize_ids(raw: Iterable[Any]) -> List[int]:
    """把表单/JSON 传入的选择规范化为去重、保序的 int 列表。"""
    ids: List[int] = []
    for value in raw or []:
        try:
            bid = int(value)
        except (TypeError, ValueError):
            continue
        if bid > 0 and bid not in ids:
            ids.append(bid)
    return ids


# ---------------------------------------------------------------------------
# 读取查询
# ---------------------------------------------------------------------------
_BACKUP_LINK_SQL = """
SELECT l.id, l.link_no, l.cable_type, l.remark,
       pa.port_name AS a_port_name, da.asset_no AS a_asset_no,
       pb.port_name AS b_port_name, db.asset_no AS b_asset_no
FROM links l
JOIN ports pa ON pa.id = l.a_port_id
JOIN devices da ON da.id = pa.device_id
JOIN ports pb ON pb.id = l.b_port_id
JOIN devices db ON db.id = pb.device_id
WHERE l.id = ?
"""

_LIVE_PORT_SQL = """
SELECT p.id AS port_id, p.device_id AS device_id,
       d.asset_no AS asset_no, d.name AS device_name,
       d.model AS model, d.location AS location,
       p.port_name AS port_name, p.port_type AS port_type,
       p.speed AS speed, p.status AS status, p.remark AS remark
FROM ports p
JOIN devices d ON d.id = p.device_id
WHERE d.asset_no = ? AND p.port_name = ?
"""

_OCCUPANTS_SQL = """
SELECT l.id AS link_id, l.link_no AS link_no, l.cable_type AS cable_type,
       l.a_port_id AS a_port_id, l.b_port_id AS b_port_id,
       da.asset_no AS a_asset_no, pa.port_name AS a_port_name,
       db.asset_no AS b_asset_no, pb.port_name AS b_port_name,
       CASE WHEN l.a_port_id = ? THEN 'A' ELSE 'B' END AS found_at_end
FROM links l
JOIN ports pa ON pa.id = l.a_port_id
JOIN devices da ON da.id = pa.device_id
JOIN ports pb ON pb.id = l.b_port_id
JOIN devices db ON db.id = pb.device_id
WHERE l.a_port_id = ? OR l.b_port_id = ?
ORDER BY l.id
"""

_SAME_LINK_SQL = """
SELECT id, link_no FROM links
WHERE (a_port_id = ? AND b_port_id = ?)
   OR (a_port_id = ? AND b_port_id = ?)
LIMIT 1
"""

_LIVE_LINK_BY_ID_SQL = """
SELECT l.id, l.link_no,
       da.asset_no AS a_asset_no, pa.port_name AS a_port_name,
       db.asset_no AS b_asset_no, pb.port_name AS b_port_name
FROM links l
JOIN ports pa ON pa.id = l.a_port_id
JOIN devices da ON da.id = pa.device_id
JOIN ports pb ON pb.id = l.b_port_id
JOIN devices db ON db.id = pb.device_id
WHERE l.id = ?
"""


def fetch_backup_link(conn: sqlite3.Connection, bid: int) -> Optional[sqlite3.Row]:
    return conn.execute(_BACKUP_LINK_SQL, (bid,)).fetchone()


def find_live_port(conn: sqlite3.Connection, asset_no: str, port_name: str) -> Optional[sqlite3.Row]:
    return conn.execute(_LIVE_PORT_SQL, (asset_no, port_name)).fetchone()


def port_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    return {field: row[field] for field in PORT_FIELDS}


def find_occupants(conn: sqlite3.Connection, port_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(_OCCUPANTS_SQL, (port_id, port_id, port_id)).fetchall()
    occupants: List[Dict[str, Any]] = []
    for r in rows:
        o = dict(r)
        if r["found_at_end"] == "A":
            o["self_coord"] = f"{r['a_asset_no']}/{r['a_port_name']}"
            o["peer_coord"] = f"{r['b_asset_no']}/{r['b_port_name']}"
            o["peer_side"] = "B"
        else:
            o["self_coord"] = f"{r['b_asset_no']}/{r['b_port_name']}"
            o["peer_coord"] = f"{r['a_asset_no']}/{r['a_port_name']}"
            o["peer_side"] = "A"
        occupants.append(o)
    return occupants


def format_occupancy_reason(occ: Dict[str, Any], asset_no: str, port_name: str) -> str:
    return (
        f"端口 {asset_no}/{port_name} 已被现库链路 #{occ['link_id']}"
        f"（{occ['link_no'] or '未编号'}：{occ['a_asset_no']}/{occ['a_port_name']} ↔ "
        f"{occ['b_asset_no']}/{occ['b_port_name']}）占用，该端口出现在这条链路的"
        f"{occ['found_at_end']} 端。每个端口最多接一条链路，必须先拆除/移走该占用链路。"
    )


def find_same_link(conn: sqlite3.Connection, a_port_id: Optional[int], b_port_id: Optional[int]):
    if not a_port_id or not b_port_id:
        return None
    return conn.execute(
        _SAME_LINK_SQL, (a_port_id, b_port_id, b_port_id, a_port_id)
    ).fetchone()


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def evaluate_endpoint(
    live: sqlite3.Connection, end: str, asset_no: str, port_name: str
) -> Dict[str, Any]:
    ep: Dict[str, Any] = {
        "end": end,
        "asset_no": asset_no,
        "port_name": port_name,
        "coord": f"{asset_no}/{port_name}",
        "key": coord_key(asset_no, port_name),
        "port": None,
        "snapshot": None,
        "occupants": [],
        "reasons": [],
    }
    row = find_live_port(live, asset_no, port_name)
    if row is None:
        device = live.execute(
            "SELECT asset_no, name FROM devices WHERE asset_no = ?", (asset_no,)
        ).fetchone()
        if device is None:
            ep["reasons"].append(
                f"现库中不存在资产号为「{asset_no}」的设备，"
                f"备份端端口「{port_name}」无法按资产号+端口名匹配到现库记录。"
            )
        else:
            ep["reasons"].append(
                f"现库设备「{asset_no} {device['name']}」存在，"
                f"但找不到端口「{port_name}」（可能已被删除或改名），无法补回。"
            )
        return ep

    snap = port_snapshot(row)
    ep["port"] = snap
    ep["snapshot"] = snap
    ep["occupants"] = find_occupants(live, snap["port_id"])
    for occ in ep["occupants"]:
        ep["reasons"].append(format_occupancy_reason(occ, asset_no, port_name))
    return ep


def evaluate_selection(
    backup: sqlite3.Connection, live: sqlite3.Connection, raw_ids: Iterable[Any]
) -> Dict[str, Any]:
    ids = normalize_ids(raw_ids)
    items: List[Dict[str, Any]] = []

    for bid in ids:
        row = fetch_backup_link(backup, bid)
        item: Dict[str, Any] = {
            "backup_id": bid,
            "found": row is not None,
            "link_reasons": [],
            "reasons": [],
            "a": None,
            "b": None,
            "same_link": None,
            "id_hint": None,
            "restorable": False,
        }
        if row is None:
            item["link_reasons"].append(f"备份库中不存在记录号为 #{bid} 的链路。")
            items.append(item)
            continue

        item.update(
            {
                "link_no": row["link_no"],
                "cable_type": row["cable_type"],
                "remark": row["remark"],
            }
        )
        item["a"] = evaluate_endpoint(live, "A", row["a_asset_no"], row["a_port_name"])
        item["b"] = evaluate_endpoint(live, "B", row["b_asset_no"], row["b_port_name"])

        if item["a"]["key"] == item["b"]["key"]:
            item["link_reasons"].append(
                f"链路两端在备份中指向同一个端口 {item['a']['coord']}，不是有效的双端链路。"
            )

        # 备份记录号在现库是否已被复用（仅作提示，不参与匹配）
        a_pid = item["a"]["port"]["port_id"] if item["a"]["port"] else None
        b_pid = item["b"]["port"]["port_id"] if item["b"]["port"] else None
        same = find_same_link(live, a_pid, b_pid)
        item["same_link"] = dict(same) if same is not None else None

        hint_row = live.execute(_LIVE_LINK_BY_ID_SQL, (bid,)).fetchone()
        if hint_row is None:
            item["id_hint"] = {"kind": "free", "live_link": None}
        else:
            is_same_record = bool(same is not None and same["id"] == hint_row["id"])
            item["id_hint"] = {
                "kind": "same" if is_same_record else "reused",
                "live_link": dict(hint_row),
            }

        items.append(item)

    # 同一次勾选内部不得重复占用同一端口
    owners: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        if not item["found"]:
            continue
        for side in ("a", "b"):
            ep = item[side]
            owners.setdefault(ep["key"], []).append(item)
    for key, group in owners.items():
        if len(group) <= 1:
            continue
        coord_text = key.replace("\x1f", "/")
        ids_str = "、".join(f"#{it['backup_id']}" for it in group)
        for item in group:
            others = "、".join(
                f"#{it['backup_id']}" for it in group if it is not item
            )
            item["link_reasons"].append(
                f"端口 {coord_text} 同时被本次勾选的备份链路 {ids_str} 使用；"
                f"与 {others} 冲突，同一端口不能补回两条链路。"
            )

    finalize_items(items)
    all_restorable = bool(items) and all(it["restorable"] for it in items)
    return {"ids": ids, "items": items, "all_restorable": all_restorable}


def finalize_items(items: List[Dict[str, Any]]) -> None:
    """汇总链路级原因 + 两端原因，得出每条链路是否可补回。"""
    for item in items:
        reasons = list(item.get("link_reasons", []))
        for side in ("a", "b"):
            ep = item.get(side)
            if ep:
                reasons.extend(ep.get("reasons", []))
        # 去重保序
        seen = set()
        uniq = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        item["reasons"] = uniq
        item["restorable"] = item["found"] and not uniq


# ---------------------------------------------------------------------------
# 预览快照 / 签名凭证
# ---------------------------------------------------------------------------
def build_token_payload(evaluation: Dict[str, Any]) -> Dict[str, Any]:
    snapshots: Dict[str, Optional[Dict[str, Any]]] = {}
    fingerprints: Dict[str, Any] = {}
    for item in evaluation["items"]:
        bid = str(item["backup_id"])
        if item["found"]:
            fingerprints[bid] = {
                "link_no": item.get("link_no"),
                "cable_type": item.get("cable_type"),
                "remark": item.get("remark"),
                "a": [item["a"]["asset_no"], item["a"]["port_name"]],
                "b": [item["b"]["asset_no"], item["b"]["port_name"]],
            }
            for side in ("a", "b"):
                ep = item[side]
                snapshots[ep["key"]] = ep["snapshot"]
    return {
        "v": 1,
        "at": now_iso(),
        "sel": evaluation["ids"],
        "fp": fingerprints,
        "snap": snapshots,
    }


def apply_fingerprint_check(items: List[Dict[str, Any]], fingerprints: Dict[str, Any]) -> None:
    for item in items:
        if not item["found"]:
            continue
        fp = fingerprints.get(str(item["backup_id"]))
        if not fp:
            item["link_reasons"].append("预览凭证中缺少该链路的备份快照，请重新预览。")
            continue
        current_a = [item["a"]["asset_no"], item["a"]["port_name"]]
        current_b = [item["b"]["asset_no"], item["b"]["port_name"]]
        if current_a != fp.get("a") or current_b != fp.get("b"):
            item["link_reasons"].append(
                "备份链路的两端坐标与预览时不一致（备份文件可能已被替换），请重新预览。"
            )
        for field in ("link_no", "cable_type", "remark"):
            if item.get(field) != fp.get(field):
                item["link_reasons"].append(
                    f"备份链路的{FIELD_LABELS.get(field, field)}在预览后发生变化，请重新预览。"
                )
                break


def apply_snapshot_check(items: List[Dict[str, Any]], snapshots: Dict[str, Any]) -> None:
    """把确认时新读到的端口记录与预览所列记录逐一比较。"""
    for item in items:
        if not item["found"]:
            continue
        for side in ("a", "b"):
            ep = item[side]
            if ep["key"] not in snapshots:
                ep["reasons"].append("预览凭证中缺少该端口的记录快照，请重新预览。")
                continue
            old = snapshots[ep["key"]]
            cur = ep["snapshot"]

            if old is None and cur is None:
                continue
            if old is None and cur is not None:
                ep["reasons"].append(
                    "预览时现库还没有匹配的端口记录；等待确认期间现库新增了该记录，"
                    "为保证与预览一致，请重新预览后再确认。"
                )
                continue
            if old is not None and cur is None:
                ep["reasons"].append(
                    f"预览时列出的现库端口记录 #{old.get('port_id')}"
                    f"（{old.get('asset_no')}/{old.get('port_name')}）"
                    "在确认前已被删除或改名，现库记录与预览不一致，请重新预览。"
                )
                continue

            # 两边都有：先比记录身份，再比展示字段
            if old["port_id"] != cur["port_id"]:
                ep["reasons"].append(
                    f"按资产号+端口名虽仍匹配到端口，但现库记录号已从预览时的 "
                    f"#{old['port_id']} 变为 #{cur['port_id']}"
                    "（旧记录疑似删除后重建，记录号可能已被复用），"
                    "与预览所列记录不一致，请重新预览。"
                )
            changes = []
            for field in PORT_FIELDS:
                if field == "port_id":
                    continue
                if old.get(field) != cur.get(field):
                    label = FIELD_LABELS.get(field, field)
                    changes.append(
                        f"{label}由「{old.get(field) if old.get(field) is not None else '空'}」"
                        f"变为「{cur.get(field) if cur.get(field) is not None else '空'}」"
                    )
            if changes:
                ep["reasons"].append(
                    "现库端口记录在预览后被修改：" + "；".join(changes)
                    + "。请核对后重新预览。"
                )


# ---------------------------------------------------------------------------
# 确认补回（整批原子）
# ---------------------------------------------------------------------------
def confirm_restore(
    live_path: str,
    backup_path: str,
    payload: Dict[str, Any],
    *,
    enforce_snapshot: bool = True,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    raw_ids = payload.get("sel") if isinstance(payload, dict) else None
    ids = normalize_ids(raw_ids)
    if not ids:
        return {"ok": False, "status": 400, "fatal": "没有选择任何链路。"}

    live = connect(live_path)
    backup = connect(backup_path)
    try:
        # 立即拿写锁：之后的读都在同一写事务内，确认期间的并发写入要么在
        # 拿锁前提交（能被读到并据此判定冲突），要么等待我们提交。
        live.execute("BEGIN IMMEDIATE")
        # 先确保历史段存在（仅在缺失时写起始基线）；与后续接线写入同事务，
        # 本批若回滚，不会留下任何历史编号。
        history.ensure_active_segment(live)

        evaluation = evaluate_selection(backup, live, ids)
        items = evaluation["items"]

        if enforce_snapshot:
            apply_fingerprint_check(items, payload.get("fp", {}) or {})
            apply_snapshot_check(items, payload.get("snap", {}) or {})
        finalize_items(items)

        if not all(it["restorable"] for it in items):
            live.execute("ROLLBACK")
            return {
                "ok": False,
                "status": 409,
                "error": "确认时校验未通过，发现冲突；整批未写入任何链路。",
                "evaluation": evaluation,
            }

        restored: List[Dict[str, Any]] = []
        history_changes: List[Dict[str, Any]] = []
        for item in items:
            a_id = item["a"]["port"]["port_id"]
            b_id = item["b"]["port"]["port_id"]
            cur = live.execute(
                """
                INSERT INTO links
                    (link_no, a_port_id, b_port_id, cable_type, remark,
                     source, restored_from_backup_id)
                VALUES (?, ?, ?, ?, ?, 'restored', ?)
                """,
                (
                    item.get("link_no"),
                    a_id,
                    b_id,
                    item.get("cable_type"),
                    item.get("remark"),
                    item["backup_id"],
                ),
            )
            new_id = cur.lastrowid
            restored.append(
                {
                    "new_id": new_id,
                    "backup_id": item["backup_id"],
                    "link_no": item.get("link_no"),
                    "cable_type": item.get("cable_type"),
                    "remark": item.get("remark"),
                    "a_coord": item["a"]["coord"],
                    "b_coord": item["b"]["coord"],
                }
            )
            history_changes.append(
                {
                    "action": history.ACTION_CONNECT,
                    "link_id": new_id,
                    "link_no": item.get("link_no"),
                    "a_asset_no": item["a"]["asset_no"],
                    "a_port_name": item["a"]["port_name"],
                    "b_asset_no": item["b"]["asset_no"],
                    "b_port_name": item["b"]["port_name"],
                }
            )

        # 整批补回共用一个变更编号，编号下只写本批 N 条链路增量。
        revision = history.record_revision(
            live,
            kind=history.KIND_RESTORE_BATCH,
            changes=history_changes,
            channel=channel,
            title=f"备份整批补回 {len(history_changes)} 条链路",
        )

        live.execute("COMMIT")
        return {
            "ok": True,
            "restored": restored,
            "count": len(restored),
            "revision": revision,
        }

    except sqlite3.IntegrityError as exc:
        try:
            live.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        message = str(exc)
        if "PORT_ALREADY_OCCUPIED" in message or "UNIQUE" in message.upper():
            message = (
                "写入被数据库拒绝：某个端口已经占用（每个端口最多接一条链路）。"
                "整批已回滚，未写入任何链路，请重新预览。"
            )
        return {"ok": False, "status": 409, "fatal": message}
    except Exception:
        # 历史写入等任何意外都必须连带接线写入一起回滚，失败不留历史。
        try:
            live.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        live.close()
        backup.close()


# ---------------------------------------------------------------------------
# 现库常规写入（供页面 / JSON API 演示“现库照常写入”）
# ---------------------------------------------------------------------------
def add_live_link(
    live_path: str,
    *,
    a_port_id: Optional[int] = None,
    b_port_id: Optional[int] = None,
    a_coord: Optional[tuple] = None,
    b_coord: Optional[tuple] = None,
    link_no: Optional[str] = None,
    cable_type: Optional[str] = None,
    remark: Optional[str] = None,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    live = connect(live_path)
    try:
        live.execute("BEGIN IMMEDIATE")
        history.ensure_active_segment(live)

        def resolve(port_id, coord):
            if port_id:
                row = live.execute(
                    "SELECT p.id AS port_id, d.asset_no, p.port_name "
                    "FROM ports p JOIN devices d ON d.id=p.device_id WHERE p.id=?",
                    (port_id,),
                ).fetchone()
            elif coord:
                row = find_live_port(live, coord[0], coord[1])
            else:
                row = None
            return row

        a_row = resolve(a_port_id, a_coord)
        b_row = resolve(b_port_id, b_coord)
        problems = []
        if a_row is None:
            problems.append("A 端端口在现库中不存在。")
        if b_row is None:
            problems.append("B 端端口在现库中不存在。")
        if problems:
            live.execute("ROLLBACK")
            return {"ok": False, "status": 400, "errors": problems}

        if a_row["port_id"] == b_row["port_id"]:
            problems.append("链路两端不能是同一个端口。")

        for row, label in ((a_row, "A"), (b_row, "B")):
            occ = find_occupants(live, row["port_id"])
            for o in occ:
                problems.append(format_occupancy_reason(o, row["asset_no"], row["port_name"]))
        if problems:
            live.execute("ROLLBACK")
            return {"ok": False, "status": 409, "errors": problems}

        cur = live.execute(
            "INSERT INTO links (link_no, a_port_id, b_port_id, cable_type, remark, source) "
            "VALUES (?, ?, ?, ?, ?, 'manual')",
            (link_no, a_row["port_id"], b_row["port_id"], cable_type, remark),
        )
        new_id = cur.lastrowid
        revision = history.record_revision(
            live,
            kind=history.KIND_MANUAL_ADD,
            channel=channel,
            changes=[
                {
                    "action": history.ACTION_CONNECT,
                    "link_id": new_id,
                    "link_no": link_no,
                    "a_asset_no": a_row["asset_no"],
                    "a_port_name": a_row["port_name"],
                    "b_asset_no": b_row["asset_no"],
                    "b_port_name": b_row["port_name"],
                }
            ],
            title=f"手工接线 {a_row['asset_no']}/{a_row['port_name']} ↔ "
            f"{b_row['asset_no']}/{b_row['port_name']}",
        )
        live.execute("COMMIT")
        return {
            "ok": True,
            "link": {
                "id": new_id,
                "link_no": link_no,
                "a_coord": f"{a_row['asset_no']}/{a_row['port_name']}",
                "b_coord": f"{b_row['asset_no']}/{b_row['port_name']}",
            },
            "revision": revision,
        }
    except sqlite3.IntegrityError as exc:
        try:
            live.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return {"ok": False, "status": 409, "errors": [f"数据库约束拒绝写入：{exc}"]}
    except Exception:
        try:
            live.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        live.close()


def delete_live_link(
    live_path: str, link_id: int, *, channel: Optional[str] = None
) -> Dict[str, Any]:
    live = connect(live_path)
    try:
        live.execute("BEGIN IMMEDIATE")
        history.ensure_active_segment(live)
        row = live.execute(
            """
            SELECT l.id, l.link_no,
                   da.asset_no AS a_asset_no, pa.port_name AS a_port_name,
                   db.asset_no AS b_asset_no, pb.port_name AS b_port_name
            FROM links l
            JOIN ports pa ON pa.id=l.a_port_id
            JOIN devices da ON da.id=pa.device_id
            JOIN ports pb ON pb.id=l.b_port_id
            JOIN devices db ON db.id=pb.device_id
            WHERE l.id=?
            """,
            (link_id,),
        ).fetchone()
        if row is None:
            live.execute("ROLLBACK")
            return {"ok": False, "status": 404, "errors": [f"现库不存在链路 #{link_id}。"]}
        live.execute("DELETE FROM links WHERE id=?", (link_id,))
        revision = history.record_revision(
            live,
            kind=history.KIND_MANUAL_DELETE,
            channel=channel,
            changes=[
                {
                    "action": history.ACTION_DISCONNECT,
                    "link_id": row["id"],
                    "link_no": row["link_no"],
                    "a_asset_no": row["a_asset_no"],
                    "a_port_name": row["a_port_name"],
                    "b_asset_no": row["b_asset_no"],
                    "b_port_name": row["b_port_name"],
                }
            ],
            title=f"手工拆线 #{link_id}：{row['a_asset_no']}/{row['a_port_name']} ↔ "
            f"{row['b_asset_no']}/{row['b_port_name']}",
        )
        live.execute("COMMIT")
        return {"ok": True, "deleted": dict(row), "revision": revision}
    except Exception:
        try:
            live.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        live.close()


def update_port_remark(live_path: str, port_id: int, remark: str) -> Dict[str, Any]:
    live = connect(live_path)
    try:
        live.execute("BEGIN IMMEDIATE")
        row = live.execute("SELECT id FROM ports WHERE id=?", (port_id,)).fetchone()
        if row is None:
            live.execute("ROLLBACK")
            return {"ok": False, "status": 404, "errors": [f"现库不存在端口记录 #{port_id}。"]}
        live.execute("UPDATE ports SET remark=? WHERE id=?", (remark, port_id))
        live.execute("COMMIT")
        return {"ok": True}
    finally:
        live.close()


# 常见命名别名（便于不同调用习惯 / 自动化脚本）
add_link = add_live_link
create_link = add_live_link
delete_link = delete_live_link
remove_link = delete_live_link
