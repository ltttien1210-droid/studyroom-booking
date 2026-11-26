from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .auth import signed_in
from .models import Booking, OccupiedSlot, Room, db_session, utc_now

booking = Blueprint("booking", __name__)
OPEN, CLOSE, STEP = 7 * 60, 21 * 60, 30
MAX_DURATION, ADVANCE_DAYS = 120, 14
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
STARTS = range(OPEN, CLOSE, STEP)


def local_now():
    return datetime.now(TIMEZONE)


def format_minute(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def valid_time(day, minute, now):
    return day > now.date() or (day == now.date() and minute > now.hour * 60 + now.minute)


def slot_taken(db, room_id, day, start, end):
    return db.scalar(
        select(OccupiedSlot.minute).where(
            OccupiedSlot.room_id == room_id, OccupiedSlot.day == day,
            OccupiedSlot.minute >= start, OccupiedSlot.minute < end,
        ).limit(1)
    ) is not None


def slot_conflict(error):
    statement = (error.statement or "").lower()
    original = error.orig
    if "insert into occupied_slots" not in statement:
        return False
    return (
        getattr(original, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_PRIMARYKEY"
        or "pk_occupied_slot" in str(original).lower()
    )


def reserve(db, reservation):
    """The slot key arbitrates competing reservations across processes."""
    db.add(reservation)
    db.flush()
    db.add_all(
        OccupiedSlot(
            room_id=reservation.room_id, day=reservation.day, minute=minute, booking_id=reservation.id,
        )
        for minute in range(reservation.start_minute, reservation.end_minute, STEP)
    )
    db.flush()


def read_day(value, default):
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


@booking.get("/")
@signed_in
def index():
    now = local_now()
    today = now.date()
    last_day = today + timedelta(days=ADVANCE_DAYS)
    day = min(last_day, max(today, read_day(request.args.get("day", ""), today)))
    seats = min(30, max(1, request.args.get("seats", 1, type=int)))
    with db_session() as db:
        rooms = db.scalars(select(Room).where(Room.capacity >= seats).order_by(Room.capacity, Room.code)).all()
        busy = defaultdict(set)
        for room_id, minute in db.execute(
            select(OccupiedSlot.room_id, OccupiedSlot.minute).where(OccupiedSlot.day == day)
        ):
            busy[room_id].add(minute)

    return render_template(
        "index.html", rooms=rooms, day=day, today=today, last_day=last_day, seats=seats,
        previous=day - timedelta(days=1), next_day=day + timedelta(days=1),
        starts=STARTS, past={slot for slot in STARTS if not valid_time(day, slot, now)}, busy=busy,
    )


@booking.route("/phong/<int:room_id>/dat", methods=["GET", "POST"])
@signed_in
def reserve_room(room_id):
    now = local_now()
    today = now.date()
    last_day = today + timedelta(days=ADVANCE_DAYS)
    errors = {}
    status = 200
    with db_session() as db:
        room = db.get(Room, room_id)
        if room is None:
            abort(404, "Không tìm thấy phòng.")
        if request.method == "GET":
            day = min(last_day, max(today, read_day(request.args.get("day", ""), today)))
            start = request.args.get("start", type=int)
            size = min(room.capacity, max(1, request.args.get("seats", 1, type=int)))
            values = {"day": day.isoformat(), "start": start, "end": start + STEP if start in STARTS else None,
                      "group_size": str(size), "purpose": ""}
        else:
            values = {key: request.form.get(key, "").strip() for key in ("day", "group_size", "purpose")}
            values["start"] = request.form.get("start", type=int)
            values["end"] = request.form.get("end", type=int)
            day = read_day(values["day"], None)
            start, end = values["start"], values["end"]
            size = request.form.get("group_size", type=int)
            if day is None or not today <= day <= last_day:
                errors["day"] = "Chọn ngày từ hôm nay đến 14 ngày tiếp theo."
            if start not in STARTS or end not in range(OPEN + STEP, CLOSE + 1, STEP):
                errors["time"] = "Chọn giờ theo mốc 30 phút trong khoảng 07:00–21:00."
            elif not STEP <= end - start <= MAX_DURATION:
                errors["time"] = "Một lượt đặt cần từ 30 phút đến 2 giờ."
            elif day and not valid_time(day, start, now):
                errors["time"] = "Giờ bắt đầu phải ở tương lai."
            if size is None or not 1 <= size <= room.capacity:
                errors["group_size"] = f"Nhóm cần từ 1 đến {room.capacity} người."
            purpose = values["purpose"]
            if len(purpose) < 3 or len(purpose.encode("utf-16-le")) > 280 or "\x00" in purpose:
                errors["purpose"] = "Nhập mục đích sử dụng từ 3 đến 140 ký tự."
            if not errors:
                if slot_taken(db, room_id, day, start, end):
                    errors["time"] = "Phòng đã có lịch trong khung giờ này. Hãy chọn giờ khác."
                    status = 409
                else:
                    reservation = Booking(
                        room_id=room_id, user_id=g.user.id, day=day, start_minute=start,
                        end_minute=end, group_size=size, purpose=purpose,
                    )
                    try:
                        reserve(db, reservation)
                        reservation_id = reservation.id
                        db.commit()
                    except IntegrityError as error:
                        db.rollback()
                        if not slot_conflict(error) and not slot_taken(db, room_id, day, start, end):
                            raise
                        errors["time"] = "Khung giờ này vừa được đặt. Hãy chọn giờ khác."
                        status = 409
                    else:
                        flash(f"Đặt phòng thành công · Mã SR-{reservation_id:05d}")
                        return redirect(url_for("booking.mine"), code=303)
            if errors and status == 200:
                status = 422

        selected_day = read_day(values["day"], today)
        busy_rows = db.scalars(
            select(Booking).where(
                Booking.room_id == room_id, Booking.day == selected_day, Booking.cancelled_at.is_(None),
            ).order_by(Booking.start_minute)
        ).all() if today <= selected_day <= last_day else []
        return render_template(
            "reserve.html", room=room, values=values, today=today, last_day=last_day,
            starts=STARTS, ends=range(OPEN + STEP, CLOSE + 1, STEP), errors=errors, busy_rows=busy_rows,
        ), status


@booking.get("/lich-cua-toi")
@signed_in
def mine():
    now = local_now()
    today = now.date()
    minute = now.hour * 60 + now.minute
    view = request.args.get("view", "upcoming")
    if view not in ("upcoming", "history"):
        abort(400, "Bộ lọc lịch không hợp lệ.")
    future = (Booking.day > today) | ((Booking.day == today) & (Booking.end_minute > minute))
    conditions = [Booking.user_id == g.user.id]
    if view == "upcoming":
        conditions.extend((Booking.cancelled_at.is_(None), future))
        order = (Booking.day, Booking.start_minute, Booking.id)
    else:
        conditions.append(Booking.cancelled_at.is_not(None) | ~future)
        order = (Booking.day.desc(), Booking.start_minute.desc(), Booking.id.desc())
    with db_session() as db:
        total = db.scalar(select(func.count()).select_from(Booking).where(*conditions))
        pages = max(1, (total + 11) // 12)
        page = min(pages, max(1, request.args.get("page", 1, type=int)))
        rows = db.scalars(
            select(Booking).options(joinedload(Booking.room)).where(*conditions)
            .order_by(*order).offset((page - 1) * 12).limit(12)
        ).all()
    return render_template("mine.html", rows=rows, now=now, page=page, pages=pages, view=view)


@booking.post("/lich/<int:booking_id>/huy")
@signed_in
def cancel(booking_id):
    with db_session() as db:
        reservation = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == g.user.id))
        if reservation is None:
            abort(404, "Không tìm thấy lịch đặt của bạn.")
        if reservation.cancelled_at:
            flash("Lịch này đã được hủy trước đó.")
        elif not valid_time(reservation.day, reservation.start_minute, local_now()):
            abort(409, "Chỉ hủy được trước giờ bắt đầu.")
        else:
            released = db.execute(
                update(Booking).where(Booking.id == booking_id, Booking.cancelled_at.is_(None))
                .values(cancelled_at=utc_now())
            )
            if released.rowcount == 1:
                db.execute(delete(OccupiedSlot).where(OccupiedSlot.booking_id == booking_id))
                flash("Đã hủy lịch. Khung giờ đã được mở lại.")
            else:
                flash("Lịch này đã được hủy trước đó.")
            db.commit()
    return redirect(url_for("booking.mine", view="history"), code=303)
