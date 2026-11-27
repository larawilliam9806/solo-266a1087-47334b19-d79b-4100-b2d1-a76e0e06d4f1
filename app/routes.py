"""路由：恢复页面的 HTML 工作流 + 便于自动化的 JSON API。"""
from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import core, history
from .tokens import dump_token, load_token, make_serializer

bp = Blueprint("web", __name__)


def _serializer():
    return make_serializer(current_app.config["SECRET_KEY"])


def _paths():
    return current_app.config["LIVE_DB"], current_app.config["BACKUP_DB"]


# ===========================================================================
# 恢复工作流（HTML）
# ===========================================================================
@bp.get("/")
@bp.get("/recovery")
@bp.get("/restore")
def recovery_index():
    backup, live = _dbs()
    rows = backup.execute(
        "SELECT id FROM links ORDER BY id"
    ).fetchall()
    all_ids = [r["id"] for r in rows]
    evaluation = core.evaluate_selection(backup, live, all_ids)

    show = request.args.get("show", "deleted")
    items = evaluation["items"]
    if show == "all":
        visible = items
    else:
        # 默认只看“现库已无相同两端链路”的疑似误删记录
        visible = [it for it in items if it["found"] and it["same_link"] is None]

    stats = {
        "backup_total": len(items),
        "deleted": sum(1 for it in items if it["found"] and it["same_link"] is None),
        "present": sum(1 for it in items if it.get("same_link")),
        "restorable": sum(
            1
            for it in items
            if it["found"] and it["same_link"] is None and it["restorable"]
        ),
        "blocked": sum(
            1
            for it in items
            if it["found"] and it["same_link"] is None and not it["restorable"]
        ),
    }
    return render_template(
        "recovery.html", items=visible, show=show, stats=stats
    )


@bp.post("/preview")
def preview_form():
    ids = core.normalize_ids(request.form.getlist("link_ids"))
    if not ids:
        flash("请先勾选至少一条备份链路，再进行预览。", "warn")
        return redirect(url_for("web.recovery_index"))

    backup, live = _dbs()
    evaluation = core.evaluate_selection(backup, live, ids)
    payload = core.build_token_payload(evaluation)
    token = dump_token(_serializer(), payload)
    return render_template(
        "preview.html",
        preview=evaluation,
        items=evaluation["items"],
        token=token,
        confirm_error=None,
    )


@bp.post("/confirm")
def confirm_form():
    token = request.form.get("token", "")
    form_ids = core.normalize_ids(request.form.getlist("link_ids"))

    enforce_snapshot = True
    if token:
        payload, err = load_token(_serializer(), token)
        if err:
            flash(err, "error")
            return redirect(url_for("web.recovery_index"))
        if form_ids and form_ids != payload.get("sel"):
            flash("提交的链路选择与预览凭证不一致，请重新预览。", "error")
            return redirect(url_for("web.recovery_index"))
    else:
        # 允许跳过签名凭证、直接按当前现库做原子校验与写入
        if not form_ids:
            flash("缺少预览凭证且未选择链路，请先勾选链路并预览。", "error")
            return redirect(url_for("web.recovery_index"))
        payload = {"sel": form_ids}
        enforce_snapshot = False

    live_path, backup_path = _paths()
    result = core.confirm_restore(
        live_path, backup_path, payload,
        enforce_snapshot=enforce_snapshot, channel="web",
    )

    if result["ok"]:
        return render_template(
            "result.html", restored=result["restored"], count=result["count"]
        )

    if "evaluation" not in result:
        flash(result.get("fatal", "确认失败，整批未写入。"), "error")
        return redirect(url_for("web.recovery_index"))

    # 确认失败的具体原因（用于顶部冲突清单）
    conflict_items = result["evaluation"]["items"]
    confirm_errors = [
        {"backup_id": it["backup_id"], "reasons": it["reasons"]}
        for it in conflict_items
        if it.get("reasons")
    ]

    # 用当前现库重新生成一份干净预览与新凭证，供值班员核对后再次确认
    backup, live = _dbs()
    fresh = core.evaluate_selection(backup, live, payload.get("sel", []))
    fresh_payload = core.build_token_payload(fresh)
    fresh_token = dump_token(_serializer(), fresh_payload)
    # HTML 表单场景返回 200 + 页面内冲突清单，便于值班员/自动化直接读取
    return render_template(
        "preview.html",
        preview=fresh,
        items=fresh["items"],
        token=fresh_token,
        confirm_error=result.get("error", "确认时发现冲突，整批未写入。"),
        confirm_errors=confirm_errors,
    )


# ===========================================================================
# 现库管理（演示“等待确认期间现库照常写入”）
# ===========================================================================
@bp.get("/live")
def live_page():
    backup, live = _dbs()
    links = live.execute(
        """
        SELECT l.id, l.link_no, l.cable_type, l.remark, l.source,
               l.restored_from_backup_id, l.created_at,
               da.asset_no AS a_asset, pa.port_name AS a_port,
               db.asset_no AS b_asset, pb.port_name AS b_port
        FROM links l
        JOIN ports pa ON pa.id=l.a_port_id
        JOIN devices da ON da.id=pa.device_id
        JOIN ports pb ON pb.id=l.b_port_id
        JOIN devices db ON db.id=pb.device_id
        ORDER BY l.id
        """
    ).fetchall()

    ports = live.execute(
        """
        SELECT p.id, p.port_name, p.port_type, p.speed, p.status, p.remark,
               d.asset_no, d.name AS device_name,
               (SELECT l2.id FROM links l2
                  WHERE l2.a_port_id=p.id OR l2.b_port_id=p.id LIMIT 1) AS occupied_link_id
        FROM ports p JOIN devices d ON d.id=p.device_id
        ORDER BY d.asset_no, p.id
        """
    ).fetchall()

    port_options = live.execute(
        """
        SELECT p.id, d.asset_no, p.port_name,
               EXISTS(SELECT 1 FROM links l
                      WHERE l.a_port_id=p.id OR l.b_port_id=p.id) AS occupied
        FROM ports p JOIN devices d ON d.id=p.device_id
        ORDER BY d.asset_no, p.port_name
        """
    ).fetchall()

    return render_template(
        "live.html",
        links=[dict(r) for r in links],
        ports=[dict(r) for r in ports],
        port_options=[dict(r) for r in port_options],
    )


@bp.post("/live/links/add")
def live_link_add():
    form = request.form
    try:
        a_id = int(form.get("a_port_id") or 0) or None
        b_id = int(form.get("b_port_id") or 0) or None
    except ValueError:
        a_id = b_id = None
    result = core.add_live_link(
        current_app.config["LIVE_DB"],
        a_port_id=a_id,
        b_port_id=b_id,
        link_no=(form.get("link_no") or "").strip() or None,
        cable_type=(form.get("cable_type") or "").strip() or None,
        remark=(form.get("remark") or "").strip() or None,
        channel="web",
    )
    if result["ok"]:
        lk = result["link"]
        flash(f"现库已写入新链路 #{lk['id']}：{lk['a_coord']} ↔ {lk['b_coord']}", "ok")
    else:
        for e in result.get("errors", ["写入失败"]):
            flash(e, "error")
    return redirect(url_for("web.live_page"))


@bp.post("/live/links/<int:link_id>/delete")
def live_link_delete(link_id: int):
    result = core.delete_live_link(
        current_app.config["LIVE_DB"], link_id, channel="web"
    )
    if result["ok"]:
        flash(f"已从现库删除链路 #{link_id}，相关端口已释放。", "ok")
    else:
        for e in result.get("errors", ["删除失败"]):
            flash(e, "error")
    return redirect(url_for("web.live_page"))


@bp.post("/live/ports/<int:port_id>/remark")
def live_port_remark(port_id: int):
    remark = (request.form.get("remark") or "").strip()
    result = core.update_port_remark(current_app.config["LIVE_DB"], port_id, remark)
    if result["ok"]:
        flash(f"端口记录 #{port_id} 的备注已更新（若它在预览中，确认时会提示记录不一致）。", "ok")
    else:
        for e in result.get("errors", ["更新失败"]):
            flash(e, "error")
    return redirect(url_for("web.live_page"))


@bp.post("/demo/reseed")
@bp.get("/demo/reseed")
def demo_reseed():
    from .seed import seed_demo

    seed_demo(current_app.config["LIVE_DB"], current_app.config["BACKUP_DB"])
    flash("已重新生成演示数据库：备份库与现库恢复到初始场景。", "ok")
    return redirect(request.referrer or url_for("web.recovery_index"))


# ===========================================================================
# JSON API（自动化 / 脚本验收使用，语义与页面完全一致）
# ===========================================================================
@bp.get("/api/backup/links")
def api_backup_links():
    backup, live = _dbs()
    ids = [r["id"] for r in backup.execute("SELECT id FROM links ORDER BY id")]
    evaluation = core.evaluate_selection(backup, live, ids)
    return jsonify({"ok": True, "total": len(ids), "items": evaluation["items"]})


@bp.post("/api/preview")
def api_preview():
    data = request.get_json(silent=True) or {}
    raw = (
        data.get("link_ids")
        or data.get("backup_ids")
        or data.get("ids")
        or data.get("selected")
        or []
    )
    ids = core.normalize_ids(raw)
    if not ids:
        return jsonify({"ok": False, "error": "link_ids 不能为空"}), 400
    backup, live = _dbs()
    evaluation = core.evaluate_selection(backup, live, ids)
    payload = core.build_token_payload(evaluation)
    token = dump_token(_serializer(), payload)
    return jsonify(
        {
            "ok": True,
            "token": token,
            "all_restorable": evaluation["all_restorable"],
            "items": evaluation["items"],
        }
    )


@bp.post("/api/confirm")
def api_confirm():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    enforce_snapshot = True

    if token:
        payload, err = load_token(_serializer(), token)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        link_ids = core.normalize_ids(data.get("link_ids", []))
        if link_ids and link_ids != payload.get("sel"):
            return jsonify({"ok": False, "error": "link_ids 与预览凭证不一致"}), 400
    else:
        # 未使用预览凭证时，直接按确认瞬间的现库状态做整批原子校验
        raw = (
            data.get("link_ids")
            or data.get("backup_ids")
            or data.get("ids")
            or data.get("selected")
            or []
        )
        ids = core.normalize_ids(raw)
        if not ids:
            return jsonify({
                "ok": False,
                "error": "必须提供预览 token，或在 link_ids 中给出至少一条链路。",
            }), 400
        payload = {"sel": ids}
        enforce_snapshot = False

    result = core.confirm_restore(
        current_app.config["LIVE_DB"],
        current_app.config["BACKUP_DB"],
        payload,
        enforce_snapshot=enforce_snapshot,
        channel="api",
    )
    if result["ok"]:
        return jsonify(
            {
                "ok": True,
                "count": result["count"],
                "restored": result["restored"],
                "revision": result.get("revision"),
            }
        )

    status = result.get("status", 409)
    body = {"ok": False, "error": result.get("error") or result.get("fatal")}
    if "evaluation" in result:
        evaluation = result["evaluation"]
        fresh_payload = core.build_token_payload(evaluation)
        body["items"] = evaluation["items"]
        body["all_restorable"] = evaluation["all_restorable"]
        body["fresh_token"] = dump_token(_serializer(), fresh_payload)
    return jsonify(body), status


@bp.get("/api/live/links")
def api_live_links():
    _, live = _dbs()
    rows = live.execute(
        """
        SELECT l.*, da.asset_no AS a_asset, pa.port_name AS a_port,
               db.asset_no AS b_asset, pb.port_name AS b_port
        FROM links l
        JOIN ports pa ON pa.id=l.a_port_id
        JOIN devices da ON da.id=pa.device_id
        JOIN ports pb ON pb.id=l.b_port_id
        JOIN devices db ON db.id=pb.device_id
        ORDER BY l.id
        """
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@bp.post("/api/live/links")
@bp.post("/api/live/links/add")
def api_live_link_add():
    data = request.get_json(silent=True) or {}

    def coord(prefix):
        asset = data.get(f"{prefix}_asset") or data.get(f"{prefix}_asset_no")
        port = data.get(f"{prefix}_port") or data.get(f"{prefix}_port_name")
        return (asset, port) if asset and port else None

    result = core.add_live_link(
        current_app.config["LIVE_DB"],
        a_port_id=data.get("a_port_id"),
        b_port_id=data.get("b_port_id"),
        a_coord=coord("a"),
        b_coord=coord("b"),
        link_no=data.get("link_no"),
        cable_type=data.get("cable_type"),
        remark=data.get("remark"),
        channel="api",
    )
    return jsonify(result), (200 if result["ok"] else result.get("status", 400))


@bp.delete("/api/live/links/<int:link_id>")
@bp.post("/api/live/links/<int:link_id>/delete")
def api_live_link_delete(link_id: int):
    result = core.delete_live_link(
        current_app.config["LIVE_DB"], link_id, channel="api"
    )
    return jsonify(result), (200 if result["ok"] else result.get("status", 404))


@bp.patch("/api/live/ports/<int:port_id>")
def api_live_port_update(port_id: int):
    data = request.get_json(silent=True) or {}
    result = core.update_port_remark(
        current_app.config["LIVE_DB"], port_id, data.get("remark", "")
    )
    return jsonify(result), (200 if result["ok"] else result.get("status", 404))


@bp.post("/api/demo/reseed")
@bp.get("/api/demo/reseed")
def api_demo_reseed():
    from .seed import seed_demo

    seed_demo(current_app.config["LIVE_DB"], current_app.config["BACKUP_DB"])
    return jsonify({"ok": True})


# ===========================================================================
# 接线历史（HTML 页面 + JSON API；同一套 core/history 查询语义）
# ===========================================================================
_REV_START_KEYS = (
    "start", "from", "begin", "since",
    "start_rev", "from_rev", "start_revision", "from_revision",
    "start_no", "from_no", "start_number", "from_number",
    "start_change", "from_change", "start_change_no", "from_change_no",
    "start_change_id", "from_change_id", "start_id", "from_id",
    "rev1", "r1", "n1",
)
_REV_END_KEYS = (
    "end", "to", "until", "stop",
    "end_rev", "to_rev", "end_revision", "to_revision",
    "end_no", "to_no", "end_number", "to_number",
    "end_change", "to_change", "end_change_no", "to_change_no",
    "end_change_id", "to_change_id", "end_id", "to_id",
    "rev2", "r2", "n2",
)
_SEGMENT_KEYS = (
    "segment_id", "segment", "epoch_id", "epoch",
    "generation", "history_id", "sid",
)


def _hist_params() -> dict:
    """合并 query string、表单与 JSON body，方便自动化用任意常见传参方式。"""
    params: dict = {}
    params.update(request.args.to_dict(flat=True))
    if request.form:
        params.update(request.form.to_dict(flat=True))
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            for k, v in body.items():
                params[k] = v
    return params


def _first_param(params: dict, *names: str):
    for name in names:
        value = params.get(name)
        if value is not None and value != "":
            return value
    return None


def _parse_rev_number(params: dict, keys) -> tuple:
    raw = _first_param(params, *keys)
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, "变更编号必须是整数。"
    try:
        return int(str(raw).strip()), None
    except (TypeError, ValueError):
        return None, f"变更编号无法识别：{raw!r}（应为整数编号）。"


def _parse_history_request(params: dict) -> dict:
    """把各种常见参数名归一为 query_port_history 的关键字。"""
    kwargs: dict = {}
    errors = []

    seg = _first_param(params, *_SEGMENT_KEYS)
    if seg is not None:
        kwargs["segment_id"] = str(seg)

    port_id_raw = _first_param(params, "port_id", "pid")
    asset = _first_param(
        params, "asset_no", "asset", "a_asset", "a_asset_no", "self_asset"
    )
    port_name = _first_param(
        params, "port_name", "port", "a_port", "port_name_",
        "self_port", "p", "name",
    )
    port_coord = _first_param(
        params, "port", "coord", "port_coord", "q", "selected_port", "target"
    )

    if port_id_raw is not None:
        try:
            kwargs["port_id"] = int(str(port_id_raw).strip())
        except (TypeError, ValueError):
            errors.append("port_id 必须是整数端口记录号。")
    elif asset is not None and (port_name is not None or port_coord is not None):
        kwargs["asset_no"] = str(asset).strip()
        kwargs["port_name"] = str(port_name or port_coord).strip()
    elif port_coord is not None:
        text = str(port_coord).strip()
        if text.isdigit():
            kwargs["port_id"] = int(text)
        else:
            asset_part = port_part = None
            if "/" in text:
                # 资产号不含 "/"，端口名自身可含 "/"（如 GE1/0/2），只切第一刀
                asset_part, port_part = text.split("/", 1)
            else:
                for sep in ("／", "|", "｜", ",", "，"):
                    if sep in text:
                        asset_part, port_part = text.split(sep, 1)
                        break
            if asset_part is not None and port_part is not None:
                kwargs["asset_no"] = asset_part.strip()
                kwargs["port_name"] = port_part.strip()
            else:
                errors.append(
                    "端口坐标格式应为「资产号/端口名」，例如 ZC-PP-11/P03。"
                )

    start, err = _parse_rev_number(params, _REV_START_KEYS)
    if err:
        errors.append(err)
    elif start is not None:
        kwargs["start"] = start

    end, err = _parse_rev_number(params, _REV_END_KEYS)
    if err:
        errors.append(err)
    elif end is not None:
        kwargs["end"] = end

    if errors:
        return {"errors": errors}
    return {"kwargs": kwargs}


def _history_overview(live_path: str) -> dict:
    overview = history.list_revisions(live_path)
    overview["ports"] = history.port_options(live_path)
    overview["ok"] = True
    return overview


@bp.route("/history", methods=["GET", "POST"])
@bp.route("/wiring-history", methods=["GET", "POST"])
@bp.route("/live/history", methods=["GET", "POST"])
def history_page():
    live_path = current_app.config["LIVE_DB"]
    params = _hist_params()

    overview = _history_overview(live_path)
    revisions = overview["revisions"]
    latest = overview["latest_number"] or 1

    result = None
    query_error = None
    selected_port_id = params.get("port_id", "")
    selected_start = params.get("start", "1") or "1"
    selected_end = params.get("end", str(latest)) or str(latest)

    has_query = any(
        params.get(k) for k in ("port_id", "port", "asset_no", "port_name", "q")
    )
    if has_query:
        parsed = _parse_history_request(params)
        if "errors" in parsed:
            query_error = "；".join(parsed["errors"])
            flash(query_error, "error")
        else:
            result = history.query_port_history(live_path, **parsed["kwargs"])
            if not result.get("ok"):
                query_error = result.get("error", "查询失败")
                flash(query_error, "error")
                result = None
            else:
                p = result["port"]
                selected_port_id = p.get("port_id") or selected_port_id
                selected_start = result["start_rev"]
                selected_end = result["end_rev"]

    return render_template(
        "history.html",
        ports=overview["ports"],
        revisions=revisions,
        segments=overview["segments"],
        current_segment=overview.get("segment"),
        latest_number=latest,
        result=result,
        query_error=query_error,
        selected_port_id=selected_port_id,
        selected_start=selected_start,
        selected_end=selected_end,
    )


@bp.get("/api/history")
@bp.get("/api/wiring-history")
@bp.get("/api/live/history")
@bp.get("/api/history/changes")
def api_history_get():
    live_path = current_app.config["LIVE_DB"]
    params = _hist_params()
    parsed = _parse_history_request(params)
    if "errors" in parsed:
        return jsonify({"ok": False, "errors": parsed["errors"]}), 400

    kwargs = parsed["kwargs"]
    wants_query = any(
        kwargs.get(k) is not None for k in ("port_id", "asset_no")
    )
    if not wants_query:
        return jsonify(_history_overview(live_path))

    result = history.query_port_history(live_path, **kwargs)
    if not result.get("ok"):
        return jsonify(result), result.get("status", 400)
    # 顺带返回编号列表与端口列表，方便自动化一次拿全
    result["revisions"] = history.list_revisions(
        live_path, segment_id=result.get("segment_id")
    )["revisions"]
    return jsonify(result)


@bp.post("/api/history/query")
@bp.post("/api/history")
def api_history_query_post():
    live_path = current_app.config["LIVE_DB"]
    params = _hist_params()
    parsed = _parse_history_request(params)
    if "errors" in parsed:
        return jsonify({"ok": False, "errors": parsed["errors"]}), 400
    result = history.query_port_history(live_path, **parsed["kwargs"])
    if not result.get("ok"):
        return jsonify(result), result.get("status", 400)
    return jsonify(result)


@bp.get("/api/history/port/<int:port_id>")
@bp.get("/api/history/port/<int:port_id>/<int:start>/<int:end>")
@bp.get("/api/history/<int:port_id>/<int:start>/<int:end>")
@bp.get("/api/live/history/port/<int:port_id>")
@bp.get("/api/live/history/port/<int:port_id>/<int:start>/<int:end>")
@bp.get("/api/live/ports/<int:port_id>/history")
@bp.get("/api/ports/<int:port_id>/history")
@bp.get("/api/wiring-history/port/<int:port_id>")
@bp.get("/api/wiring-history/port/<int:port_id>/<int:start>/<int:end>")
def api_history_path_query(port_id: int, start: int = 1, end: int | None = None):
    live_path = current_app.config["LIVE_DB"]
    params = _hist_params()
    segment = _first_param(params, *_SEGMENT_KEYS)
    # 嵌套路由常用 start_at/end_at、from_id/to_id 等，_hist_params 已合并；
    # 若路径未给区间而 query 给了，从参数解析补齐。
    if end is None:
        parsed = _parse_history_request(params)
        kw = parsed.get("kwargs", {})
        start = kw.get("start", start)
        end = kw.get("end")
        if segment is None:
            segment = kw.get("segment_id")
    result = history.query_port_history(
        live_path,
        port_id=port_id,
        start=start,
        end=end,
        segment_id=segment,
    )
    if not result.get("ok"):
        return jsonify(result), result.get("status", 400)
    return jsonify(result)


@bp.get("/api/history/revisions")
@bp.get("/api/live/history/revisions")
@bp.get("/api/wiring-history/revisions")
@bp.get("/api/revisions")
@bp.get("/api/changes")
def api_history_revisions():
    live_path = current_app.config["LIVE_DB"]
    segment = _first_param(request.args, *_SEGMENT_KEYS)
    all_segments = request.args.get("all") in ("1", "true", "yes")
    overview = history.list_revisions(
        live_path, segment_id=segment, include_all_segments=all_segments
    )
    return jsonify(overview)


@bp.get("/api/history/revisions/<int:rev_no>")
@bp.get("/api/revisions/<int:rev_no>")
def api_history_revision_detail(rev_no: int):
    segment = _first_param(request.args, *_SEGMENT_KEYS)
    result = history.get_revision_detail(
        current_app.config["LIVE_DB"], rev_no, segment_id=segment
    )
    return jsonify(result), (200 if result.get("ok") else result.get("status", 404))


@bp.get("/api/history/segments")
@bp.get("/api/history/epochs")
def api_history_segments():
    segments = history.list_segments(current_app.config["LIVE_DB"])
    current = None
    if segments:
        # meta 中取当前段
        from .db import connect as _connect

        conn = _connect(current_app.config["LIVE_DB"])
        try:
            current = history.current_segment_id(conn)
        finally:
            conn.close()
    return jsonify(
        {"ok": True, "segments": segments, "epochs": segments,
         "current_segment_id": current, "epoch_id": current}
    )


@bp.get("/api/history/ports")
def api_history_ports():
    return jsonify(
        {"ok": True, "ports": history.port_options(current_app.config["LIVE_DB"])}
    )


def _dbs():
    # 延迟导入避免循环
    from . import get_backup_db, get_live_db

    return get_backup_db(), get_live_db()
