from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.formatting import utcnow


class SubscriptionStatus(StrEnum):
    """Allowed subscription lifecycle statuses."""

    ACTIVE = "active"
    EXPIRED = "expired"


class Base(AsyncAttrs, DeclarativeBase):
    """Base SQLAlchemy 2.0 declarative class."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    channels: Mapped[list[Channel]] = relationship(
        "Channel",
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    tg_channel_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    owner: Mapped[User] = relationship("User", back_populates="channels")
    tariffs: Mapped[list[Tariff]] = relationship(
        "Tariff",
        back_populates="channel",
        cascade="all, delete-orphan",
        order_by=lambda: [Tariff.price_stars.asc(), Tariff.id.asc()],
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription",
        back_populates="channel",
        cascade="all, delete-orphan",
    )


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("channels.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    channel: Mapped[Channel] = relationship("Channel", back_populates="tariffs")
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription",
        back_populates="tariff",
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_status_expires_at", "status", "expires_at"),
        Index("ix_subscriptions_user_channel_status", "user_id", "channel_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    channel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("channels.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    tariff_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tariffs.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        default=SubscriptionStatus.ACTIVE.value,
        index=True,
        nullable=False,
    )
    reminded_3d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    channel: Mapped[Channel] = relationship("Channel", back_populates="subscriptions")
    tariff: Mapped[Tariff] = relationship("Tariff", back_populates="subscriptions")


class FSMState(Base):
    """Persistent FSM state storage for stateless Serverless environments."""

    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
