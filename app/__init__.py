"""机房配线库误删链路恢复系统 —— Flask 应用工厂。"""
from __future__ import annotations

import os

from flask import Flask, current_app, g

from .db import connect


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)

    data_dir = os.environ.get("DATA_DIR", os.path.join(project_root(), "data"))
    default_live = os.environ.get("LIVE_DB", os.path.join(data_dir, "live.db"))
    default_backup = os.environ.get("BACKUP_DB", os.path.join(data_dir, "backup.db"))

    app.config.update(
        SECRET_KEY=os.environ.get(
            "SECRET_KEY", "idc-recovery-dev-secret-key-change-me"
        ),
        LIVE_DB=default_live,
        BACKUP_DB=default_backup,
        AUTO_SEED=os.environ.get("AUTO_SEED", "1") != "0",
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)

    # JSON 直接输出 UTF-8 中文，避免响应体里只有 \uXXXX 转义
    try:
        app.json.ensure_ascii = False
    except AttributeError:
        app.config["JSON_AS_ASCII"] = False

    # 启动即保证两份 SQLite 演示库可用（测试可传 AUTO_SEED=False 自行播种）
    if app.config.get("AUTO_SEED"):
        from .seed import ensure_seed

        ensure_seed(app.config["LIVE_DB"], app.config["BACKUP_DB"])

    from .routes import bp

    app.register_blueprint(bp)

    @app.teardown_appcontext
    def _close_dbs(exc):
        for key in ("live_db", "backup_db"):
            conn = g.pop(key, None)
            if conn is not None:
                conn.close()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.errorhandler(404)
    def _not_found(e):
        from flask import request

        if request.path.startswith("/api/"):
            return {"ok": False, "error": "接口不存在"}, 404
        from flask import render_template

        return render_template("error.html", code=404, message="页面不存在"), 404

    @app.errorhandler(500)
    def _server_error(e):
        from flask import request

        if request.path.startswith("/api/"):
            return {"ok": False, "error": "服务器内部错误"}, 500
        from flask import render_template

        return render_template("error.html", code=500, message="服务器内部错误"), 500

    return app


def get_live_db():
    if "live_db" not in g:
        g.live_db = connect(current_app.config["LIVE_DB"])
    return g.live_db


def get_backup_db():
    if "backup_db" not in g:
        g.backup_db = connect(current_app.config["BACKUP_DB"])
    return g.backup_db
