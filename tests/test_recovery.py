"""恢复流程的端到端测试：匹配规则、预览原因、并发一致性、整批原子性。"""
import re

import pytest

from app import core, create_app
from app.db import connect
from app.seed import seed_demo


@pytest.fixture()
def dbs(tmp_path):
    live = str(tmp_path / "live.db")
    backup = str(tmp_path / "backup.db")
    seed_demo(live, backup)
    return live, backup


@pytest.fixture()
def client(dbs):
    live, backup = dbs
    app = create_app(
        {
            "TESTING": True,
            "AUTO_SEED": False,
            "LIVE_DB": live,
            "BACKUP_DB": backup,
            "SECRET_KEY": "unit-test-key",
        }
    )
    with app.test_client() as c:
        yield c


def preview(client, ids):
    resp = client.post("/api/preview", json={"link_ids": ids})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def confirm(client, token, ids=None):
    body = {"token": token}
    if ids is not None:
        body["link_ids"] = ids
    return client.post("/api/confirm", json=body)


def link_count(live_path):
    conn = connect(live_path)
    try:
        return conn.execute("SELECT count(*) c FROM links").fetchone()["c"]
    finally:
        conn.close()


def port_id(live_path, asset, name):
    conn = connect(live_path)
    try:
        row = core.find_live_port(conn, asset, name)
        return row["port_id"] if row else None
    finally:
        conn.close()


def occupy(live_path, asset, name, other_asset="ZC-PP-11", other_name="P07"):
    """模拟外部现库写入：占用指定端口（另一端用空闲端口）。"""
    conn = connect(live_path)
    try:
        p = core.find_live_port(conn, asset, name)
        q = core.find_live_port(conn, other_asset, other_name)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO links (link_no,a_port_id,b_port_id,source) "
            "VALUES ('X',?,?, 'manual')",
            (p["port_id"], q["port_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 播种场景 / 匹配规则
# ---------------------------------------------------------------------------
def test_seed_scenarios(dbs):
    live_path, backup_path = dbs
    bk = connect(backup_path)
    lv = connect(live_path)
    ids = [r["id"] for r in bk.execute("SELECT id FROM links ORDER BY id")]
    ev = core.evaluate_selection(bk, lv, ids)
    by_id = {it["backup_id"]: it for it in ev["items"]}

    assert {i for i, it in by_id.items() if it["restorable"]} == {3, 7, 8, 9}
    assert by_id[1]["same_link"] and by_id[2]["same_link"]

    r4 = " ".join(by_id[4]["reasons"])
    assert "ZC-PP-11/P04" in r4 and "#4" in r4 and "A 端" in r4

    r5 = " ".join(by_id[5]["reasons"])
    assert "ZC-CK-03/GE0/0/1" in r5 and "#5" in r5 and "B 端" in r5

    r6 = " ".join(by_id[6]["reasons"])
    assert "ZC-OLD-99" in r6 and "资产号" in r6

    # 备份 #7 的记录号被现库 #7 不同链路复用，但仍可按两端坐标匹配并恢复
    assert by_id[7]["id_hint"]["kind"] == "reused"
    hint = by_id[7]["id_hint"]["live_link"]
    assert (hint["a_asset_no"], hint["a_port_name"]) == ("ZC-PP-12", "P01")
    assert by_id[7]["a"]["port"]["asset_no"] == "ZC-PP-11"
    assert by_id[7]["a"]["port"]["port_name"] == "P05"
    assert by_id[7]["restorable"] is True


def test_preview_lists_full_port_records(client):
    data = preview(client, [3])
    item = data["items"][0]
    assert data["all_restorable"] is True
    assert item["a"]["port"]["port_id"] > 0
    assert item["a"]["port"]["device_name"]
    assert item["a"]["port"]["location"]
    assert "token" in data and data["token"]


# ---------------------------------------------------------------------------
# 正常补回
# ---------------------------------------------------------------------------
def test_happy_confirm_batch(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3, 7, 8])
    resp = confirm(client, data["token"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and body["count"] == 3
    # 新现库记录号由自增产生（8,9,10），并不沿用备份旧号 3,7,8
    assert [(x["backup_id"], x["new_id"]) for x in body["restored"]] == [
        (3, 8), (7, 9), (8, 10)
    ]
    assert link_count(live_path) == 8  # 初始 5 + 3

    conn = connect(live_path)
    try:
        rows = conn.execute(
            "SELECT * FROM links WHERE source='restored' ORDER BY id"
        ).fetchall()
        assert [r["restored_from_backup_id"] for r in rows] == [3, 7, 8]
    finally:
        conn.close()


def test_double_confirm_is_rejected(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3])
    token = data["token"]
    assert confirm(client, token).status_code == 200
    resp = confirm(client, token)
    assert resp.status_code == 409
    assert link_count(live_path) == 6


# ---------------------------------------------------------------------------
# 并发：无关写入不影响；相关冲突整批拒绝
# ---------------------------------------------------------------------------
def test_unrelated_live_writes_do_not_block(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3])

    # 与 BK#3 两端无关的常规现库写入
    resp = client.post(
        "/api/live/links",
        json={
            "a_asset": "ZC-PP-11", "a_port": "P07",
            "b_asset": "ZC-PP-11", "b_port": "P08",
            "link_no": "UNRELATED-1",
        },
    )
    assert resp.status_code == 200

    # 另改一个无关端口的备注
    other = port_id(live_path, "ZC-PP-12", "P05")
    assert client.patch(f"/api/live/ports/{other}", json={"remark": "无关改动"}).status_code == 200

    resp = confirm(client, data["token"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert link_count(live_path) == 7  # 5 + 1 无关 + 1 补回


def test_conflicting_occupy_blocks_entire_batch(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3, 7])
    assert data["all_restorable"] is True

    # 预览后、确认前，外部直接写库占用 BK#3 的 A 端 PP11/P03
    occupy(live_path, "ZC-PP-11", "P03")

    resp = confirm(client, data["token"], ids=[3, 7])
    assert resp.status_code == 409
    body = resp.get_json()
    joined = " ".join(
        r for it in body["items"] for r in it["reasons"]
    )
    assert "ZC-PP-11/P03" in joined and "占用" in joined

    # 外部占用链路 1 条；两条备份均未补回（BK#7 被连坐，整批不写入）
    assert link_count(live_path) == 6
    conn = connect(live_path)
    try:
        assert conn.execute(
            "SELECT count(*) c FROM links WHERE source='restored'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_snapshot_port_edit_blocks(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3])
    pid = port_id(live_path, "ZC-PP-11", "P03")
    r = client.patch(f"/api/live/ports/{pid}", json={"remark": "预览后被修改的备注"})
    assert r.status_code == 200

    resp = confirm(client, data["token"])
    assert resp.status_code == 409
    joined = " ".join(x for it in resp.get_json()["items"] for x in it["reasons"])
    assert "变化" in joined or "修改" in joined
    assert link_count(live_path) == 5


def test_snapshot_port_deletion_blocks(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3])
    conn = connect(live_path)
    try:
        pid = port_id(live_path, "ZC-PP-11", "P03")
        conn.execute("DELETE FROM ports WHERE id=?", (pid,))
    finally:
        conn.close()

    resp = confirm(client, data["token"])
    assert resp.status_code == 409
    joined = " ".join(x for it in resp.get_json()["items"] for x in it["reasons"])
    assert "删除" in joined
    assert link_count(live_path) == 5


def test_new_port_appearing_after_missing_preview_asks_repreview(client, dbs):
    live_path, _ = dbs
    # BK#6 预览时缺 ZC-OLD-99/F0/1
    data = preview(client, [6])
    assert data["all_restorable"] is False

    # 预览后现库补建该设备与端口
    conn = connect(live_path)
    try:
        conn.execute("INSERT INTO devices (id,asset_no,name) VALUES (99,'ZC-OLD-99','补建设备')")
        conn.execute("INSERT INTO ports (device_id,port_name) VALUES (99,'F0/1')")
    finally:
        conn.close()

    resp = confirm(client, data["token"])
    assert resp.status_code == 409
    joined = " ".join(x for it in resp.get_json()["items"] for x in it["reasons"])
    assert "新增" in joined or "重新预览" in joined
    assert link_count(live_path) == 5


# ---------------------------------------------------------------------------
# 预览自带冲突 / 同批重复端口
# ---------------------------------------------------------------------------
def test_preview_reasons_for_seed_blockers(client):
    data = preview(client, [4, 5, 6])
    assert data["all_restorable"] is False
    resp = confirm(client, data["token"])
    assert resp.status_code == 409


def test_intra_batch_duplicate_port(dbs, client):
    live_path, backup_path = dbs
    # 在备份库插入 #10，A 端与 BK#3 相同。备份触发器会阻止这种不一致数据，
    # 这里先移除触发器，模拟来自旧版本、无触发器的备份文件。
    bk = connect(backup_path)
    try:
        bk.execute("DROP TRIGGER trg_links_port_unique_insert")
        row = bk.execute(
            "SELECT p.id FROM ports p JOIN devices d ON d.id=p.device_id "
            "WHERE d.asset_no='ZC-PP-11' AND p.port_name='P03'"
        ).fetchone()
        other = bk.execute(
            "SELECT p.id FROM ports p JOIN devices d ON d.id=p.device_id "
            "WHERE d.asset_no='ZC-HX-02' AND p.port_name='GE1/0/1'"
        ).fetchone()
        bk.execute(
            "INSERT INTO links (id,link_no,a_port_id,b_port_id) VALUES (10,'BK10',?,?)",
            (row["id"], other["id"]),
        )
    finally:
        bk.close()

    data = preview(client, [3, 10])
    joined = " ".join(
        r for it in data["items"] for r in it["reasons"]
    )
    assert "同时被本次勾选" in joined
    resp = confirm(client, data["token"])
    assert resp.status_code == 409
    assert link_count(live_path) == 5


def test_atomic_batch_one_late_conflict_blocks_all(client, dbs):
    live_path, _ = dbs
    data = preview(client, [3, 7, 8, 9])
    assert data["all_restorable"] is True

    # 仅在确认前抢占 BK#8 的 PP12/P04
    occupy(live_path, "ZC-PP-12", "P04", "ZC-PP-12", "P05")

    resp = confirm(client, data["token"])
    assert resp.status_code == 409
    assert link_count(live_path) == 6  # 只有外部占用那一条
    conn = connect(live_path)
    try:
        assert conn.execute(
            "SELECT count(*) c FROM links WHERE source='restored'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Token / 表单页面
# ---------------------------------------------------------------------------
def test_tampered_token_rejected(client):
    data = preview(client, [3])
    bad = data["token"][:-2] + "xx"
    resp = client.post("/api/confirm", json={"token": bad})
    assert resp.status_code == 400


def test_html_preview_and_confirm_flow(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "误删链路恢复".encode() in r.data

    r = client.post("/preview", data={"link_ids": ["3", "8"]})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    m = re.search(r'name="token" value="([^"]+)"', html)
    assert m
    assert "确认整批补回（2 条）" in html
    assert "ZC-PP-11" in html

    r = client.post(
        "/confirm",
        data={"token": m.group(1), "link_ids": ["3", "8"]},
    )
    assert r.status_code == 200
    assert "已整批补回 2 条误删链路" in r.get_data(as_text=True)


def test_html_confirm_conflict_renders_reasons(client, dbs):
    live_path, _ = dbs
    r = client.post("/preview", data={"link_ids": ["3"]})
    token = re.search(r'name="token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    occupy(live_path, "ZC-PP-11", "P03")

    r = client.post("/confirm", data={"token": token, "link_ids": ["3"]})
    assert r.status_code == 200  # HTML 表单返回页内冲突清单
    html = r.get_data(as_text=True)
    assert "整批未写入" in html
    assert "ZC-PP-11/P03" in html


def test_html_confirm_without_token_direct_ids(client):
    # 不经过签名凭证，直接提交勾选：服务端按当前现库原子校验并写入
    r = client.post("/confirm", data={"link_ids": ["3"]})
    assert r.status_code == 200
    assert "已整批补回 1 条误删链路" in r.get_data(as_text=True)


def test_live_add_rejects_occupied_port(client):
    resp = client.post(
        "/api/live/links",
        json={
            "a_asset": "ZC-PP-11", "a_port": "P01",   # 已被 #1 占用
            "b_asset": "ZC-PP-11", "b_port": "P08",
        },
    )
    assert resp.status_code == 409
    assert "占用" in resp.get_json()["errors"][0]


def test_index_lists_reuse_warning(client):
    html = client.get("/?show=all").get_data(as_text=True)
    assert "该记录号现库已被复用" in html
    assert "ZC-OLD-99" in html
    assert "疑似误删" in html


def test_concurrent_overlapping_batches_only_one_wins(dbs):
    """两个包含同一端口的批次并发确认：写事务串行化，恰好一个整批成功。"""
    import threading

    live_path, backup_path = dbs
    bk = connect(backup_path)
    lv = connect(live_path)

    def token_for(ids):
        ev = core.evaluate_selection(bk, lv, ids)
        assert ev["all_restorable"] is True
        return core.build_token_payload(ev)

    payload_a = token_for([3, 7])   # 端口集合与 B 在 #3 重叠
    payload_b = token_for([3, 8])
    bk.close()
    lv.close()

    results = []

    def worker(payload):
        results.append(core.confirm_restore(live_path, backup_path, payload))

    t1 = threading.Thread(target=worker, args=(payload_a,))
    t2 = threading.Thread(target=worker, args=(payload_b,))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [r["ok"] for r in results]
    assert oks.count(True) == 1, results
    winner = next(r for r in results if r["ok"])
    assert winner["count"] == 2
    # 初始 5 条 + 获胜批次 2 条；失败批次零写入
    assert link_count(live_path) == 7
    conn = connect(live_path)
    try:
        restored = [
            r["restored_from_backup_id"]
            for r in conn.execute(
                "SELECT restored_from_backup_id FROM links WHERE source='restored'"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert restored == [x["backup_id"] for x in winner["restored"]]
    assert set(restored) == {3, 7} or set(restored) == {3, 8}
