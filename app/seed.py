"""生成演示用的备份库与现库。

刻意构造的场景：
- 备份 #1/#2：两端与现库链路一致，仍在现库中（未被误删）。
- 备份 #3/#7/#8/#9：误删后两端口在现库都空闲，可整批补回。
- 备份 #4：A 端端口 PP11/P04 已被现库 #4 链路占用。
- 备份 #5：B 端端口 CK03/GE0/0/1 已被现库 #5 链路占用。
- 备份 #6：对端设备 ZC-OLD-99 已在现库下架/不存在，无法匹配。
- 备份 #7 的记录号 7 在现库被一条完全不同的新链路 #7 复用 —— 只能按
  资产号 + 端口名匹配。
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

from .db import connect, init_schema

Coord = Tuple[str, str]  # (asset_no, port_name)

# ---------------------------------------------------------------------------
# 设备：id, 资产号, 名称, 型号, 位置
# ---------------------------------------------------------------------------
DEVICES = [
    (1, "ZC-HX-01", "一号核心交换机", "Huawei CE6857", "A栋3F 主机房 A-01"),
    (2, "ZC-HX-02", "二号核心交换机", "Huawei CE6857", "A栋3F 主机房 A-02"),
    (3, "ZC-PP-11", "11号机柜配线架", "AMP 24口 CAT6", "A栋3F 网络区 11柜"),
    (4, "ZC-PP-12", "12号机柜配线架", "AMP 24口 CAT6", "A栋3F 网络区 12柜"),
    (5, "ZC-FWQ-21", "21号应用服务器", "Dell R750", "A栋2F 服务器区 21柜"),
    (6, "ZC-CK-03", "出口路由器03", "H3C MSR3640", "A栋3F 主机房 B-07"),
    (7, "ZC-OLD-99", "已下架旧接入交换机", "Cisco 2960", "原B栋1F 配线间（已下架）"),
]

# ---------------------------------------------------------------------------
# 端口：(device_id, port_name, 类型, 速率, 状态, 备份备注, 现库备注)
# 同一端口在备份/现库的备注刻意有差异时，预览以现库记录为准。
# ---------------------------------------------------------------------------
PORT_ROWS = [
    # 核心交换机 1
    (1, "GE1/0/1", "光口", "10G", "在用", "至11柜配线架01", "至11柜配线架01"),
    (1, "GE1/0/2", "光口", "10G", "在用", "至11柜配线架03", "端口已重新打标，待恢复"),
    (1, "GE1/0/3", "电口", "1G", "在用", "至11柜配线架04", "至11柜配线架04"),
    (1, "GE1/0/24", "光口", "10G", "在用", "至旧交换机F0/1", "预留"),
    # 核心交换机 2
    (2, "GE1/0/1", "光口", "10G", "在用", "至12柜配线架01", "至12柜配线架01"),
    (2, "GE1/0/2", "光口", "10G", "在用", "至12柜配线架02", "改跳至11柜04口"),
    (2, "GE1/0/3", "光口", "10G", "在用", "至12柜配线架04", "至12柜配线架04"),
    (2, "GE1/0/4", "光口", "10G", "空闲", None, "新增预留端口"),
    # 11 柜配线架 P01-P08
    (3, "P01", "电口", "1G", "在用", "至核心1 GE1/0/1", "至核心1 GE1/0/1"),
    (3, "P02", "电口", "1G", "在用", "至服务器21 eth0", "至服务器21 eth0"),
    (3, "P03", "电口", "1G", "在用", "办公区VLAN10", "办公区点位-已重新打标"),
    (3, "P04", "电口", "1G", "在用", "监控区VLAN20", "现跳至核心2 GE1/0/2"),
    (3, "P05", "电口", "1G", "在用", "至服务器21 eth1", "备用跳接"),
    (3, "P06", "电口", "1G", "在用", "至核心2 GE1/0/4", "至核心2 GE1/0/4"),
    (3, "P07", "电口", "1G", "空闲", None, "空闲模块"),
    (3, "P08", "电口", "1G", "空闲", None, "空闲模块"),
    # 12 柜配线架
    (4, "P01", "电口", "1G", "在用", "至核心2 GE1/0/1", "现跳至出口路由03 GE0/0/0"),
    (4, "P02", "电口", "1G", "在用", "至出口路由03 GE0/0/1", "至出口路由03 GE0/0/1"),
    (4, "P03", "电口", "1G", "在用", "财务部专线跳接", "财务部专线跳接"),
    (4, "P04", "电口", "1G", "在用", "至核心2 GE1/0/3", "至核心2 GE1/0/3"),
    (4, "P05", "电口", "1G", "空闲", None, "空闲模块"),
    # 服务器
    (5, "eth0", "电口", "1G", "在用", "业务口 bond0", "bond0"),
    (5, "eth1", "电口", "1G", "在用", "业务口 bond1", "bond1 待恢复"),
    # 出口路由器
    (6, "GE0/0/0", "电口", "1G", "在用", "外网主线路", "至12柜P01"),
    (6, "GE0/0/1", "电口", "1G", "在用", "外网备线路", "至12柜P02"),
    # 已下架旧设备（仅备份库保留）
    (7, "F0/1", "电口", "100M", "在用", "上行核心1 GE1/0/24", None),
    (7, "F0/2", "电口", "100M", "在用", "旧办公接入", None),
]

# 资产号索引
ASSET_BY_DEVICE_ID = {d[0]: d[1] for d in DEVICES}

# ---------------------------------------------------------------------------
# 备份库中的链路（备份记录号固定为 1..9）
# (id, link_no, a_coord, b_coord, cable_type, remark)
# ---------------------------------------------------------------------------
BACKUP_LINKS = [
    (1, "LK-2025-0001", ("ZC-PP-11", "P01"),        ("ZC-HX-01", "GE1/0/1"),  "六类网线",   "主干跳接"),
    (2, "LK-2025-0002", ("ZC-PP-11", "P02"),        ("ZC-FWQ-21", "eth0"),     "六类网线",   "服务器业务口"),
    (3, "LK-2025-0003", ("ZC-PP-11", "P03"),        ("ZC-HX-01", "GE1/0/2"),  "LC-LC万兆光纤", "办公区上行（误删）"),
    (4, "LK-2025-0004", ("ZC-PP-11", "P04"),        ("ZC-HX-01", "GE1/0/3"),  "六类网线",   "监控区上行（误删）"),
    (5, "LK-2025-0005", ("ZC-PP-12", "P03"),        ("ZC-CK-03", "GE0/0/1"),  "六类网线",   "财务专线（误删）"),
    (6, "LK-2025-0006", ("ZC-OLD-99", "F0/1"),      ("ZC-HX-01", "GE1/0/24"), "LC-LC千兆光纤", "旧交换机上行（误删）"),
    (7, "LK-2025-0007", ("ZC-PP-11", "P05"),        ("ZC-FWQ-21", "eth1"),     "六类网线",   "服务器冗余口（误删）"),
    (8, "LK-2025-0008", ("ZC-PP-12", "P04"),        ("ZC-HX-02", "GE1/0/3"),  "LC-LC万兆光纤", "12柜上行（误删）"),
    (9, "LK-2025-0009", ("ZC-PP-11", "P06"),        ("ZC-HX-02", "GE1/0/4"),  "六类网线",   "11柜新增上行（误删）"),
]

# ---------------------------------------------------------------------------
# 现库中的链路。刻意保留 id=1、2，制造 id=4、5，以及被复用的 id=7。
# (id, link_no, a_coord, b_coord, cable_type, remark, source)
# ---------------------------------------------------------------------------
LIVE_LINKS = [
    (1, "LK-LIVE-0001", ("ZC-PP-11", "P01"), ("ZC-HX-01", "GE1/0/1"),  "六类网线", "主干跳接", "manual"),
    (2, "LK-LIVE-0002", ("ZC-PP-11", "P02"), ("ZC-FWQ-21", "eth0"),     "六类网线", "服务器业务口", "manual"),
    # 备份 #4 删除后，P04 被新跳接占用：
    (4, "LK-LIVE-0004", ("ZC-PP-11", "P04"), ("ZC-HX-02", "GE1/0/2"),  "六类网线", "监控区改跳核心2", "manual"),
    # 备份 #5 删除后，GE0/0/1 被新链路占用（该端口出现在 B 端）：
    (5, "LK-LIVE-0005", ("ZC-PP-12", "P02"), ("ZC-CK-03", "GE0/0/1"),  "六类网线", "外网备线改接12柜02", "manual"),
    # 记录号 7 已被一条与备份 #7 完全无关的新链路复用：
    (7, "LK-LIVE-0007", ("ZC-PP-12", "P01"), ("ZC-CK-03", "GE0/0/0"),  "六类网线", "记录号7已被复用的新链路", "manual"),
]


def _port_id_index(conn) -> Dict[Coord, int]:
    rows = conn.execute(
        "SELECT p.id AS id, d.asset_no AS asset_no, p.port_name AS port_name "
        "FROM ports p JOIN devices d ON d.id = p.device_id"
    ).fetchall()
    return {(r["asset_no"], r["port_name"]): r["id"] for r in rows}


def _build_db(path: str, *, backup: bool) -> None:
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if os.path.exists(side):
            os.remove(side)

    conn = connect(path)
    try:
        init_schema(conn)
        conn.execute("BEGIN")

        devices = DEVICES if backup else [d for d in DEVICES if d[0] != 7]
        conn.executemany(
            "INSERT INTO devices (id, asset_no, name, model, location) "
            "VALUES (?, ?, ?, ?, ?)",
            devices,
        )

        for pid, prow in enumerate(PORT_ROWS, start=1):
            device_id, port_name, ptype, speed, status, bk_remark, live_remark = prow
            if not backup and device_id == 7:
                continue
            remark = bk_remark if backup else live_remark
            conn.execute(
                "INSERT INTO ports (id, device_id, port_name, port_type, speed, status, remark) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, device_id, port_name, ptype, speed, status, remark),
            )

        idx = _port_id_index(conn)
        links = BACKUP_LINKS if backup else LIVE_LINKS
        for row in links:
            if backup:
                lid, no, a, b, cable, remark = row
                source, restored_id = "backup", None
            else:
                lid, no, a, b, cable, remark, source = row
                restored_id = None
            conn.execute(
                "INSERT INTO links (id, link_no, a_port_id, b_port_id, cable_type, "
                "remark, source, restored_from_backup_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (lid, no, idx[a], idx[b], cable, remark, source, restored_id),
            )

        max_link_id = max(r[0] for r in links)
        conn.execute("DELETE FROM sqlite_sequence WHERE name='links'")
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('links', ?)",
            (max_link_id,),
        )

        # 为现库播种出的已有接线写新历史段的 #1 起始编号（与播种同一事务）。
        # 每次 seed_demo 都重建库文件，因此重置演示数据后必然是一个全新历史段，
        # 旧段编号不会指向新库记录。备份库不使用接线历史，只保留空表结构。
        if not backup:
            from . import history

            history.ensure_active_segment(conn, reason="seed")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def seed_demo(live_path: str, backup_path: str) -> None:
    """强制重建两份演示数据库。"""
    _build_db(backup_path, backup=True)
    _build_db(live_path, backup=False)


def ensure_seed(live_path: str, backup_path: str) -> bool:
    """数据库缺失或未初始化时写入演示数据。返回是否执行了播种。"""
    from .db import is_initialized

    did = False
    if not (is_initialized(live_path) and is_initialized(backup_path)):
        os.makedirs(os.path.dirname(os.path.abspath(live_path)), exist_ok=True)
        seed_demo(live_path, backup_path)
        did = True

    # 旧版本已有的 live.db（无历史表 / 无起始编号）在此无损升级：
    # 首次为已有接线补一个起始变更编号，重启后幂等不重复。
    from . import history

    history.ensure_history(live_path)
    return did


if __name__ == "__main__":
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    live = os.environ.get("LIVE_DB", os.path.join(data_dir, "live.db"))
    backup = os.environ.get("BACKUP_DB", os.path.join(data_dir, "backup.db"))
    forced = os.environ.get("SEED_FORCE", "0") == "1"
    if forced:
        seed_demo(live, backup)
        print(f"[seed] 已强制重建演示数据库：\n  backup={backup}\n  live={live}")
    else:
        did = ensure_seed(live, backup)
        if did:
            print(f"[seed] 已初始化演示数据库：\n  backup={backup}\n  live={live}")
        else:
            print("[seed] 两份数据库均已存在，跳过播种（设置 SEED_FORCE=1 可重建）。")
