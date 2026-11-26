from datetime import date, datetime, timezone

from flask import current_app
from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Unicode,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def db_session():
    return current_app.extensions["studyroom_db"]()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Unicode(80))
    password_hash: Mapped[str] = mapped_column(String(255))


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(Unicode(80))
    location: Mapped[str] = mapped_column(Unicode(120))
    capacity: Mapped[int]
    equipment: Mapped[str] = mapped_column(Unicode(180))


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("start_minute >= 420 AND end_minute <= 1260 AND end_minute > start_minute"),
        CheckConstraint("group_size >= 1"),
        Index("ix_booking_owner_day", "user_id", "day"),
        Index("ix_booking_room_day", "room_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    day: Mapped[date] = mapped_column(Date)
    start_minute: Mapped[int]
    end_minute: Mapped[int]
    group_size: Mapped[int]
    purpose: Mapped[str] = mapped_column(Unicode(140))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    cancelled_at: Mapped[datetime | None]
    room: Mapped[Room] = relationship()


class OccupiedSlot(Base):
    __tablename__ = "occupied_slots"
    __table_args__ = (PrimaryKeyConstraint(name="pk_occupied_slot"),)

    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), primary_key=True, autoincrement=False)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    minute: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
