"""接线历史特性的端到端测试。

覆盖需求：
- 首次启用为已有接线记起始编号；重启后按原编号可查；
- 网页 / API 手工增删各一笔；备份整批补回共用一个编号；
- 接线与历史同事务，失败不留历史；
- 每笔历史增量只随本次改动链路数增长；
- 起始状态 + 区间内每次接线/拆线及对端；拆了接回原对端分开显示；
- 查历史不阻塞现库写入；
- 重置演示数据另开历史段，旧编号不指向新记录。
"""
import threading

import pytest

from app import core, create_app, history
from app.db import connect
from app.seed import seed_demo


@pytest.fixture()
def dbs(tmp_path):
    live = str(tmp_path / "live.db")
    backup = str(tmp_path / "backup.db")
    seed_demo(live, backup)
    return live, backup


@pytest.fixture()
def app(dbs):
    live, backup = dbs
    app = create_app(
        {
            "TESTING": True,
            "AUTO_SEED": False,
            "LIVE_DB": live,
            "BACKUP_DB": backup,
            "SECRET_KEY": "history-unit-key",
        }
    )
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def rev_rows(live_path, rev_no):
    conn = connect(live_path)
    try:
        return conn.execute(
            "SELECT * FROM link_changes WHERE rev_no=? ORDER BY id", (rev_no,)
        ).fetchall()
    finally:
        conn.close()


def revision_count(live_path):
    conn = connect(live_path)
    try:
        return conn.execute("SELECT count(*) c FROM change_revisions").fetchone()["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 起始编号 / 基线状态
# ---------------------------------------------------------------------------
def test_seed_creates_single_baseline_revision(dbs):
    live, _ = dbs
    ov = history.list_revisions(live)
    assert len(ov["segments"]) == 1
    assert [r["rev_no"] for r in ov["revisions"]] == [1]
    base = ov["revisions"][0]
    assert base["kind"] == history.KIND_BASELINE
    assert base["link_count"] == 5
    # 已有 5 条现库链路 -> 起始编号下 5 行基线增量
    assert len(rev_rows(live, 1)) == 5
    assert base["committed_at"]


def test_baseline_state_peer_and_idle(dbs):
    live, _ = dbs
    # P01 初始接核心1 GE1/0/1
    r = history.query_port_history(live, asset_no="ZC-PP-11", port_name="P01", start=1, end=1)
    assert r["ok"]
    st = r["state_at_start"]
    assert st["occupied"] is True and st["idle"] is False
    assert st["peer_coord"] == "ZC-HX-01/GE1/0/1"
    assert "对端" in st["label"]

    # P07 初始空闲
    r2 = history.query_port_history(live, asset_no="ZC-PP-11", port_name="P07", start=1, end=1)
    assert r2["state_at_start"]["state"] == "idle"
    assert r2["state_at_start"]["peer"] is None
    assert r2["state_at_start"]["label"] == "空闲"


def test_restart_keeps_numbers(dbs):
    live, backup = dbs
    app1 = create_app({"TESTING": True, "AUTO_SEED": False,
                       "LIVE_DB": live, "BACKUP_DB": backup, "SECRET_KEY": "k"})
    c1 = app1.test_client()
    resp = c1.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    assert resp.status_code == 200
    assert resp.get_json()["revision"]["rev_no"] == 2

    # 模拟重启：全新 app / 连接，同一个 db 文件，编号照旧且不重复基线
    app2 = create_app({"TESTING": True, "AUTO_SEED": False,
                       "LIVE_DB": live, "BACKUP_DB": backup, "SECRET_KEY": "k"})
    c2 = app2.test_client()
    ov = c2.get("/api/history/revisions").get_json()
    assert [r["rev_no"] for r in ov["revisions"]] == [1, 2]
    q = c2.get("/api/history?asset_no=ZC-PP-11&port_name=P07&start=1&end=2")
    body = q.get_json()
    assert body["ok"]
    assert body["state_at_start"]["state"] == "idle"
    assert [(e["action"], e["peer_coord"]) for e in body["events"]] == [
        ("connect", "ZC-PP-11/P08")
    ]


# ---------------------------------------------------------------------------
# 手工增删：各一笔；拆了接回原对端分开显示
# ---------------------------------------------------------------------------
def test_api_manual_add_delete_reconnect_same_peer(client, dbs):
    live, _ = dbs

    r = client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08", "link_no": "H1",
    })
    assert r.status_code == 200
    assert r.get_json()["revision"]["rev_no"] == 2
    link_id = r.get_json()["link"]["id"]

    r = client.delete(f"/api/live/links/{link_id}")
    assert r.status_code == 200
    assert r.get_json()["revision"]["rev_no"] == 3

    r = client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08", "link_no": "H1-again",
    })
    assert r.status_code == 200
    assert r.get_json()["revision"]["rev_no"] == 4

    # 每笔只写 1 行增量
    for n in (2, 3, 4):
        assert len(rev_rows(live, n)) == 1

    body = client.get(
        "/api/history?asset_no=ZC-PP-11&port_name=P07&start=1&end=4"
    ).get_json()
    assert body["state_at_start"]["state"] == "idle"
    events = body["events"]
    assert [e["action"] for e in events] == ["connect", "disconnect", "connect"]
    # 拆掉又接回“同一个对端 P08”，三次都带对端且分成三条
    assert [e["peer_coord"] for e in events] == [
        "ZC-PP-11/P08", "ZC-PP-11/P08", "ZC-PP-11/P08"
    ]
    assert len({e["rev_no"] for e in events}) == 3
    assert all(e["action_label"] in ("接线", "拆线") for e in events)

    # 从 #2 之后开始看：只剩拆、接两条；#2 提交后的状态为已接 P08
    body2 = client.get(
        "/api/history?asset_no=ZC-PP-11&port_name=P07&start=2&end=4"
    ).get_json()
    assert body2["state_at_start"]["occupied"] is True
    assert body2["state_at_start"]["peer_coord"] == "ZC-PP-11/P08"
    assert [e["action"] for e in body2["events"]] == ["disconnect", "connect"]


def test_web_form_add_and_delete_each_record_one(client):
    # 网页表单新增
    r = client.post("/live/links/add", data={
        "a_port_id": "15", "b_port_id": "16", "link_no": "WEB1",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    ov = client.get("/api/history/revisions").get_json()
    assert ov["latest_number"] == 2
    assert ov["revisions"][-1]["channel"] == "web"

    # 找到新链路 id 后走网页删除
    links = client.get("/api/live/links").get_json()["items"]
    lid = next(l["id"] for l in links if l.get("link_no") == "WEB1")
    r = client.post(f"/live/links/{lid}/delete", follow_redirects=False)
    assert r.status_code in (302, 303)
    ov = client.get("/api/history/revisions").get_json()
    assert [x["rev_no"] for x in ov["revisions"]] == [1, 2, 3]
    assert ov["revisions"][-1]["kind"] == history.KIND_MANUAL_DELETE
    assert ov["revisions"][-1]["channel"] == "web"


# ---------------------------------------------------------------------------
# 备份整批补回：共用一个编号，增量行数 = 链路数
# ---------------------------------------------------------------------------
def test_restore_batch_shares_one_revision(client, dbs):
    live, _ = dbs
    data = client.post("/api/preview", json={"link_ids": [3, 7, 8]}).get_json()
    resp = client.post("/api/confirm", json={"token": data["token"]})
    assert resp.status_code == 200
    body = resp.get_json()
    rev = body["revision"]
    assert rev["link_count"] == 3
    assert rev["rev_no"] == 2
    # 整批只有一个新编号，编号下恰好 3 行增量
    assert revision_count(live) == 2
    rows = rev_rows(live, 2)
    assert len(rows) == 3
    assert all(r["action"] == "connect" for r in rows)

    # 涉及端口 P03 能在该编号看到一次接线事件
    q = client.get("/api/history?port_id=11&start=1&end=2").get_json()  # PP11/P03 id=11
    assert q["ok"]
    assert [e["rev_no"] for e in q["events"]] == [2]
    assert q["events"][0]["peer_coord"] == "ZC-HX-01/GE1/0/2"


def test_each_new_revision_grows_only_with_changed_links(client, dbs):
    live, _ = dbs
    conn = connect(live)
    try:
        total_before = conn.execute("SELECT count(*) c FROM link_changes").fetchone()["c"]
    finally:
        conn.close()
    assert total_before == 5  # baseline

    client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    conn = connect(live)
    try:
        total_after_one = conn.execute("SELECT count(*) c FROM link_changes").fetchone()["c"]
    finally:
        conn.close()
    assert total_after_one - total_before == 1

    data = client.post("/api/preview", json={"link_ids": [3, 7, 8]}).get_json()
    client.post("/api/confirm", json={"token": data["token"]})
    conn = connect(live)
    try:
        # 批次 3 条 -> 只增加 3 行，而不是按全库链路数膨胀
        delta_batch = conn.execute(
            "SELECT count(*) c FROM link_changes WHERE rev_no=3"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert delta_batch == 3


# ---------------------------------------------------------------------------
# 原子性：失败不留历史
# ---------------------------------------------------------------------------
def test_failed_manual_write_leaves_no_history(client, dbs):
    live, _ = dbs
    before = revision_count(live)
    # P01 已被占用 -> 409
    r = client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P01",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    assert r.status_code == 409
    assert revision_count(live) == before
    # 删除不存在的链路 -> 404，也不记一笔
    r = client.delete("/api/live/links/9999")
    assert r.status_code == 404
    assert revision_count(live) == before


def test_failed_confirm_leaves_no_history(client, dbs):
    live, _ = dbs
    before = revision_count(live)
    data = client.post("/api/preview", json={"link_ids": [3, 7]}).get_json()

    # 外部抢占 BK#3 的端口
    conn = connect(live)
    try:
        p = core.find_live_port(conn, "ZC-PP-11", "P03")
        q = core.find_live_port(conn, "ZC-PP-11", "P07")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO links (link_no,a_port_id,b_port_id,source) "
            "VALUES ('X',?,?, 'manual')",
            (p["port_id"], q["port_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    resp = client.post("/api/confirm", json={"token": data["token"], "link_ids": [3, 7]})
    assert resp.status_code == 409
    assert revision_count(live) == before
    conn = connect(live)
    try:
        assert conn.execute(
            "SELECT count(*) c FROM change_revisions WHERE kind=?",
            (history.KIND_RESTORE_BATCH,),
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_history_insert_failure_rolls_back_links(dbs, monkeypatch):
    """历史写入抛错时，接线插入必须一起回滚（同事务）。"""
    live, _ = dbs

    def boom(*a, **k):
        raise RuntimeError("history disk full simulation")

    monkeypatch.setattr(history, "record_revision", boom)
    with pytest.raises(RuntimeError):
        core.add_live_link(
            live,
            a_coord=("ZC-PP-11", "P07"),
            b_coord=("ZC-PP-11", "P08"),
        )
    conn = connect(live)
    try:
        assert conn.execute("SELECT count(*) c FROM links").fetchone()["c"] == 5
        assert conn.execute("SELECT count(*) c FROM change_revisions").fetchone()["c"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 并发：查历史时现库仍可写入
# ---------------------------------------------------------------------------
def test_history_read_does_not_block_write(dbs, app):
    live, _ = dbs
    reader = connect(live)
    try:
        reader.execute("BEGIN")
        # 模拟一个正在进行的历史查询读事务
        reader.execute("SELECT count(*) FROM link_changes").fetchall()

        results = {}

        def do_write():
            with app.test_client() as c:
                resp = c.post("/api/live/links", json={
                    "a_asset": "ZC-PP-11", "a_port": "P07",
                    "b_asset": "ZC-PP-11", "b_port": "P08",
                })
                results["status"] = resp.status_code

        t = threading.Thread(target=do_write)
        t.start()
        t.join(timeout=25)
        assert not t.is_alive(), "历史读事务阻塞了现库写入"
        assert results.get("status") == 200
    finally:
        try:
            reader.execute("ROLLBACK")
        except Exception:
            pass
        reader.close()


# ---------------------------------------------------------------------------
# 重置演示数据：另开历史段，旧编号不指向新记录
# ---------------------------------------------------------------------------
def test_reseed_opens_new_segment_and_fences_old_numbers(client, dbs):
    live, _ = dbs
    old = client.get("/api/history/revisions").get_json()
    old_seg = old["segment_id"]

    client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    assert client.get("/api/history/revisions").get_json()["latest_number"] == 2

    r = client.post("/api/demo/reseed")
    assert r.status_code in (200, 302)

    new = client.get("/api/history/revisions").get_json()
    assert new["segment_id"] != old_seg
    # 新段重新从 #1 基线开始，列表里没有旧段的 #2
    assert [x["rev_no"] for x in new["revisions"]] == [1]
    assert new["revisions"][0]["link_count"] == 5

    # 旧的最大编号 #2 在新段不存在 -> 明确 404，而不是返回新记录
    r = client.get("/api/history?port_id=15&start=2&end=2")
    assert r.status_code == 404
    assert r.get_json()["code"] == "revision_not_found"

    # 携带旧段标识 -> 410 历史段失效
    r = client.get(f"/api/history?port_id=15&start=1&end=1&segment_id={old_seg}")
    assert r.status_code == 410
    body = r.get_json()
    assert body["code"] == "history_segment_gone"
    assert "旧编号" in body["error"] or "历史段" in body["error"]

    # 新段 #1 正常可查，且提交时间是重置后的新时间
    r = client.get("/api/history?port_id=9&start=1&end=1")
    assert r.status_code == 200
    assert r.get_json()["state_at_start"]["peer_coord"] == "ZC-HX-01/GE1/0/1"


def test_reseed_then_reuse_same_number_does_not_leak_old_events(client):
    """新段里再次走到 #2 时，#2 必须是新段自己的记录，不含旧段事件。"""
    old = client.get("/api/history/revisions").get_json()
    old_seg = old["segment_id"]

    # 旧段：P07-P08 接线 = #2
    client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    client.post("/api/demo/reseed")

    # 新段也制造 #2，但接的是另一个对端 P05-P06? 用 P07-P08 也行，
    # 关键是不带 segment 时只看到新段，带旧 segment 被拒绝。
    r = client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    assert r.get_json()["revision"]["rev_no"] == 2
    body = client.get("/api/history?asset_no=ZC-PP-11&port_name=P07&start=1&end=2").get_json()
    assert body["segment_id"] != old_seg
    assert len(body["events"]) == 1
    assert body["events"][0]["committed_at"] == body["end"]["created_at"]


# ---------------------------------------------------------------------------
# HTML 页面
# ---------------------------------------------------------------------------
def test_history_html_page_lists_numbers_times_and_result(client):
    html = client.get("/history").get_data(as_text=True)
    assert "接线历史" in html
    assert "变更编号" in html
    assert "提交时间" in html
    # 下拉里有 #1 与时间
    assert "#1" in html

    client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })
    client.delete("/api/live/links/8")
    client.post("/api/live/links", json={
        "a_asset": "ZC-PP-11", "a_port": "P07",
        "b_asset": "ZC-PP-11", "b_port": "P08",
    })

    html = client.get("/history?port_id=15&start=1&end=4").get_data(as_text=True)
    # 起始状态：P07 初始空闲
    assert "起始编号" in html
    assert "空闲" in html
    assert "接线" in html and "拆线" in html
    assert "ZC-PP-11/P08" in html
    # 三行事件（拆接分开）
    assert html.count('data-role="event-row"') == 3


def test_history_aliases_and_overview(client):
    for path in ("/wiring-history", "/live/history"):
        assert client.get(path).status_code == 200

    ov = client.get("/api/history").get_json()
    assert ov["ok"] and "revisions" in ov and "ports" in ov

    for path in ("/api/wiring-history", "/api/live/history",
                 "/api/history/revisions", "/api/revisions"):
        assert client.get(path).status_code == 200, path

    # 常见别名参数名 from_rev/to_rev
    r = client.get(
        "/api/history?port_id=15&from_rev=1&to_rev=1"
    )
    assert r.status_code == 200
    assert r.get_json()["state_at_start"]["state"] == "idle"

    # 路径风格
    r = client.get("/api/history/port/15/1/1")
    assert r.status_code == 200

    # 坐标风格（端口名自身带 /）
    r = client.get("/api/history?port=ZC-HX-01/GE1/0/1&start=1&end=1")
    assert r.status_code == 200
    assert r.get_json()["state_at_start"]["peer_coord"] == "ZC-PP-11/P01"

    # 其它分隔符 / 数字坐标
    assert client.get("/api/history?port=ZC-PP-11|P07&start=1&end=1").status_code == 200
    assert client.get("/api/history?port=15&start=1&end=1").status_code == 200
    bad = client.get("/api/history?port=not-a-coord&start=1&end=1")
    assert bad.status_code == 400
