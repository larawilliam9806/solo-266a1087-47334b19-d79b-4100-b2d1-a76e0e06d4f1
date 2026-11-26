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

from . import core
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
        live_path, backup_path, payload, enforce_snapshot=enforce_snapshot
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
    result = core.delete_live_link(current_app.config["LIVE_DB"], link_id)
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
    )
    if result["ok"]:
        return jsonify(
            {"ok": True, "count": result["count"], "restored": result["restored"]}
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
    )
    return jsonify(result), (200 if result["ok"] else result.get("status", 400))


@bp.delete("/api/live/links/<int:link_id>")
@bp.post("/api/live/links/<int:link_id>/delete")
def api_live_link_delete(link_id: int):
    result = core.delete_live_link(current_app.config["LIVE_DB"], link_id)
    return jsonify(result), (200 if result["ok"] else result.get("status", 404))


@bp.patch("/api/live/ports/<int:port_id>")
def api_live_port_update(port_id: int):
    data = request.get_json(silent=True) or {}
    result = core.update_port_remark(
        current_app.config["LIVE_DB"], port_id, data.get("remark", "")
    )
    return jsonify(result), (200 if result["ok"] else result.get("status", 404))


@bp.post("/api/demo/reseed")
def api_demo_reseed():
    from .seed import seed_demo

    seed_demo(current_app.config["LIVE_DB"], current_app.config["BACKUP_DB"])
    return jsonify({"ok": True})


def _dbs():
    # 延迟导入避免循环
    from . import get_backup_db, get_live_db

    return get_backup_db(), get_live_db()
