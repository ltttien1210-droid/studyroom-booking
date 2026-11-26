from datetime import timedelta
from secrets import token_urlsafe

import click
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from .booking import local_now, reserve, slot_taken
from .models import Base, Booking, Room, User, db_session

ROOMS = [
    ("A-201", "Phòng Thảo luận A", "Thư viện · Tầng 2", 4, "Bảng trắng · Wi-Fi"),
    ("A-204", "Phòng Thảo luận B", "Thư viện · Tầng 2", 6, "Màn hình HDMI · Bảng trắng"),
    ("B-302", "Phòng Làm việc nhóm", "Nhà B · Tầng 3", 8, "TV · Wi-Fi · Ổ cắm"),
    ("C-105", "Phòng Thuyết trình", "Nhà C · Tầng 1", 12, "Máy chiếu · Bảng trắng · Wi-Fi"),
]


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables and initialize the room catalogue."""
        Base.metadata.create_all(app.extensions["studyroom_engine"])
        with db_session() as db:
            codes = set(db.scalars(select(Room.code)))
            db.add_all(
                Room(code=code, name=name, location=location, capacity=capacity, equipment=equipment)
                for code, name, location, capacity, equipment in ROOMS if code not in codes
            )
            db.commit()
        click.echo("Đã sẵn sàng cơ sở dữ liệu và danh sách phòng.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create local demo credentials and example bookings for tomorrow."""
        with db_session() as db:
            accounts = {}
            for username, name, password in (
                ("demo", "Sinh viên Demo", "StudyRoom-Demo26!"),
                ("sample-team", "Nhóm minh họa", token_urlsafe(24)),
            ):
                user = db.scalar(select(User).where(User.username == username))
                if user is None:
                    user = User(username=username, name=name, password_hash=generate_password_hash(password))
                    db.add(user)
                    db.flush()
                accounts[username] = user.id
            day = local_now().date() + timedelta(days=1)
            for code, start, end, username, purpose in (
                ("A-201", 9 * 60, 10 * 60 + 30, "sample-team", "Ôn thi cơ sở dữ liệu"),
                ("A-204", 13 * 60, 14 * 60 + 30, "demo", "Họp nhóm đồ án phần mềm"),
                ("B-302", 15 * 60 + 30, 17 * 60, "sample-team", "Chuẩn bị bài thuyết trình"),
            ):
                room = db.scalar(select(Room).where(Room.code == code))
                if room is None:
                    raise click.ClickException("Hãy chạy init-db trước.")
                if not slot_taken(db, room.id, day, start, end) and not db.scalar(select(Booking.id).where(
                    Booking.room_id == room.id, Booking.day == day, Booking.start_minute == start,
                    Booking.user_id == accounts[username], Booking.purpose == purpose,
                ).limit(1)):
                    reserve(db, Booking(
                        user_id=accounts[username], room_id=room.id, day=day, start_minute=start,
                        end_minute=end, group_size=3, purpose=purpose,
                    ))
            db.commit()
        click.echo(f"Đã nạp dữ liệu mẫu cho ngày {day:%d/%m/%Y}. Tài khoản local: demo.")
