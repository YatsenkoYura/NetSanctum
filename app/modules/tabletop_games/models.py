from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TabletopRoom(Base):
    __tablename__ = "tabletop_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    game_id: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="lobby", index=True)
    operator_share_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    game_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    participants: Mapped[list["TabletopParticipant"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", order_by="TabletopParticipant.seat"
    )
    messages: Mapped[list["TabletopMessage"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class TabletopParticipant(Base):
    __tablename__ = "tabletop_participants"
    __table_args__ = (UniqueConstraint("room_id", "nickname", name="uq_tabletop_room_nickname"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("tabletop_rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nickname: Mapped[str] = mapped_column(String(40), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    seat: Mapped[int] = mapped_column(Integer, nullable=False)
    role_id: Mapped[str | None] = mapped_column(String(63), nullable=True)
    role_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reminders: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    gm_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_alive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    room: Mapped[TabletopRoom] = relationship(back_populates="participants")


class TabletopMessage(Base):
    __tablename__ = "tabletop_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("tabletop_rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_participant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tabletop_participants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recipient_participant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tabletop_participants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    room: Mapped[TabletopRoom] = relationship(back_populates="messages")
    sender: Mapped[TabletopParticipant | None] = relationship(foreign_keys=[sender_participant_id])
    recipient: Mapped[TabletopParticipant | None] = relationship(foreign_keys=[recipient_participant_id])
