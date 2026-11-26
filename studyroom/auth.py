import re
import secrets
from functools import wraps
from threading import Lock
from time import monotonic

from flask import (
    Blueprint,
    current_app,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .models import User, db_session

auth = Blueprint("auth", __name__)
DUMMY_PASSWORD = generate_password_hash(secrets.token_urlsafe(24))
RATE_LOCK = Lock()


@auth.before_app_request
def current_user():
    g.user = None
    if session.get("uid") and request.endpoint != "static":
        with db_session() as db:
            g.user = db.get(User, session["uid"])


def signed_in(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def within_limit(bucket, maximum):
    now = monotonic()
    key = (bucket, request.remote_addr)
    with RATE_LOCK:
        attempts = current_app.extensions.setdefault("auth_attempts", {})
        for old in [k for k, (start, _) in attempts.items() if now - start >= 60]:
            del attempts[old]
        start, count = attempts.get(key, (now, 0))
        if count >= maximum:
            return False
        attempts[key] = (start, count + 1)
    return True


def start_session(user_id):
    session.clear()
    session.permanent = True
    session["uid"] = user_id
    return redirect(url_for("booking.index"), code=303)


@auth.route("/dang-nhap", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("booking.index"))
    error = None
    status = 200
    if request.method == "POST":
        if not within_limit("login", 10):
            return render_template("auth.html", error="Hãy đợi một phút trước khi thử lại."), 429
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if len(username) <= 32 and 1 <= len(password) <= 128:
            with db_session() as db:
                user = db.scalar(select(User).where(User.username == username))
                verified = check_password_hash(user.password_hash if user else DUMMY_PASSWORD, password)
                if verified and user:
                    return start_session(user.id)
        error, status = "Tên đăng nhập hoặc mật khẩu chưa đúng.", 401
    return render_template("auth.html", error=error), status


@auth.route("/dang-ky", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("booking.index"))
    name, username = "", ""
    error, status = None, 200
    if request.method == "POST":
        if not within_limit("register", 5):
            return render_template("auth.html", register=True, error="Hãy đợi một phút trước khi thử lại."), 429
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if len(name) < 2 or len(name.encode("utf-16-le")) > 160 or "\x00" in name:
            error = "Họ tên cần từ 2 đến 80 ký tự."
        elif not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
            error = "Tên đăng nhập cần 3–32 ký tự a-z, 0-9, dấu chấm, gạch dưới hoặc gạch nối."
        elif not 12 <= len(password) <= 128 or password != request.form.get("confirm"):
            error = "Mật khẩu cần 12–128 ký tự và khớp với ô xác nhận."
        else:
            with db_session() as db:
                user = User(name=name, username=username, password_hash=generate_password_hash(password))
                db.add(user)
                try:
                    db.flush()
                    user_id = user.id
                    db.commit()
                    return start_session(user_id)
                except IntegrityError:
                    db.rollback()
                    error, status = "Tên đăng nhập đã được sử dụng.", 409
        if status == 200:
            status = 422
    return render_template("auth.html", register=True, name=name, username=username, error=error), status


@auth.post("/dang-xuat")
@signed_in
def logout():
    session.clear()
    return redirect(url_for("auth.login"), code=303)
