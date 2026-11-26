import os
import secrets
from datetime import timedelta
from hmac import compare_digest
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, render_template, request, session
from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException

from .auth import auth
from .booking import booking, format_minute
from .commands import register_commands


def create_app(test_config=None):
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", interpolate=False)
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        DATABASE_BACKEND=os.getenv("DATABASE_BACKEND", "sqlite"),
        MSSQL_ODBC_CONNECTION=os.getenv("MSSQL_ODBC_CONNECTION"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"],
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    app.config.update(test_config or {})
    if len(app.config["SECRET_KEY"] or "") < 32:
        raise RuntimeError("Hãy tạo SECRET_KEY ngẫu nhiên dài ít nhất 32 ký tự trong .env.")

    backend = app.config["DATABASE_BACKEND"]
    if backend == "sqlite":
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        path = app.config.get("SQLITE_PATH", Path(app.instance_path) / "studyroom.sqlite")
        url = URL.create("sqlite", database=str(path))
    elif backend == "mssql" and app.config["MSSQL_ODBC_CONNECTION"]:
        url = URL.create("mssql+pyodbc", query={"odbc_connect": app.config["MSSQL_ODBC_CONNECTION"]})
    else:
        raise RuntimeError("Chọn sqlite hoặc cấu hình MSSQL_ODBC_CONNECTION cho mssql.")
    engine = create_engine(url, pool_pre_ping=True)
    if backend == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    app.extensions["studyroom_engine"] = engine
    app.extensions["studyroom_db"] = sessionmaker(engine)
    app.jinja_env.filters["clock"] = format_minute

    @app.template_global()
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            expected = session.get("csrf", "").encode()
            supplied = request.form.get("csrf", "").encode()
            if not expected or not compare_digest(expected, supplied):
                abort(400, "Biểu mẫu đã hết hạn. Hãy tải lại trang rồi thử lại.")

    @app.after_request
    def set_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def show_error(error):
        return render_template("error.html", error=error), error.code

    app.register_blueprint(auth)
    app.register_blueprint(booking)
    register_commands(app)
    return app
