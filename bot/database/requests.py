from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from bot.database.models import Channel, Subscription, SubscriptionStatus, Tariff, User
from bot.utils.formatting import utcnow


@dataclass(slots=True)
class TariffStat:
    tariff_id: int
    name: str
    price_stars: int
    duration_days: int
    is_active: bool
    active_subscribers: int
    total_subscriptions: int


@dataclass(slots=True)
class ChannelStats:
    active_subscribers: int
    lifetime_subscribers: int
    expiring_soon_subscribers: int
    expired_subscriptions: int
    total_subscriptions: int
    tariffs: list[TariffStat]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    stmt = select(User).where(User.telegram_id == telegram_id)
    return await session.scalar(stmt)


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    full_name: str,
) -> User:
    """Create or update a Telegram user safely under concurrent requests."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is not None:
        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        if changed:
            await session.commit()
        return user

    user = User(
        telegram_id=telegram_id,
        username=username,
        full_name=full_name,
    )
    try:
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user
    except IntegrityError:
        await session.rollback()
        existing = await get_user_by_telegram_id(session, telegram_id)
        if existing is None:
            raise
        existing.username = username
        existing.full_name = full_name
        await session.commit()
        return existing


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


async def get_channel_by_tg_id(session: AsyncSession, tg_channel_id: int) -> Channel | None:
    stmt = select(Channel).where(Channel.tg_channel_id == tg_channel_id)
    return await session.scalar(stmt)


async def get_channel_by_id(
    session: AsyncSession,
    channel_id: int,
    *,
    include_tariffs: bool = False,
) -> Channel | None:
    stmt = select(Channel).where(Channel.id == channel_id)
    if include_tariffs:
        stmt = stmt.options(selectinload(Channel.tariffs))
    return await session.scalar(stmt)


async def get_owner_channel(
    session: AsyncSession,
    channel_id: int,
    owner_id: int,
    *,
    include_tariffs: bool = False,
) -> Channel | None:
    stmt = select(Channel).where(
        Channel.id == channel_id,
        Channel.owner_id == owner_id,
    )
    if include_tariffs:
        stmt = stmt.options(selectinload(Channel.tariffs))
    return await session.scalar(stmt)


async def get_owner_channels(session: AsyncSession, owner_id: int) -> list[Channel]:
    stmt = (
        select(Channel)
        .where(Channel.owner_id == owner_id)
        .order_by(Channel.is_active.desc(), Channel.id.desc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def create_channel(
    session: AsyncSession,
    *,
    owner_id: int,
    tg_channel_id: int,
    title: str,
    description: str | None = None,
) -> Channel:
    channel = Channel(
        owner_id=owner_id,
        tg_channel_id=tg_channel_id,
        title=title,
        description=description,
        is_active=True,
    )
    session.add(channel)
    await session.commit()
    await session.refresh(channel)
    return channel


async def update_channel(
    session: AsyncSession,
    channel: Channel,
    *,
    title: str | None = None,
    description: str | None = None,
    set_description: bool = False,
    is_active: bool | None = None,
) -> Channel:
    if title is not None:
        channel.title = title
    if set_description:
        channel.description = description
    if is_active is not None:
        channel.is_active = is_active
    await session.commit()
    await session.refresh(channel)
    return channel


# ---------------------------------------------------------------------------
# Tariffs
# ---------------------------------------------------------------------------


async def create_tariff(
    session: AsyncSession,
    *,
    channel_id: int,
    name: str,
    price_stars: int,
    duration_days: int,
) -> Tariff:
    tariff = Tariff(
        channel_id=channel_id,
        name=name,
        price_stars=price_stars,
        duration_days=duration_days,
        is_active=True,
    )
    session.add(tariff)
    await session.commit()
    await session.refresh(tariff)
    return tariff


async def get_tariff_by_id(
    session: AsyncSession,
    tariff_id: int,
    *,
    channel_id: int | None = None,
) -> Tariff | None:
    stmt = select(Tariff).where(Tariff.id == tariff_id)
    if channel_id is not None:
        stmt = stmt.where(Tariff.channel_id == channel_id)
    return await session.scalar(stmt)


async def get_channel_tariffs(
    session: AsyncSession,
    channel_id: int,
    *,
    active_only: bool = False,
) -> list[Tariff]:
    stmt = select(Tariff).where(Tariff.channel_id == channel_id)
    if active_only:
        stmt = stmt.where(Tariff.is_active.is_(True))
    stmt = stmt.order_by(Tariff.price_stars.asc(), Tariff.id.asc())
    result = await session.scalars(stmt)
    return list(result.all())


async def toggle_tariff_active(session: AsyncSession, tariff: Tariff) -> Tariff:
    tariff.is_active = not tariff.is_active
    await session.commit()
    await session.refresh(tariff)
    return tariff


# ---------------------------------------------------------------------------
# Subscriptions & Statistics
# ---------------------------------------------------------------------------


async def get_active_subscription(
    session: AsyncSession,
    *,
    user_id: int,
    channel_id: int,
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.channel_id == channel_id,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def create_or_extend_subscription(
    session: AsyncSession,
    *,
    user_id: int,
    channel_id: int,
    tariff: Tariff,
    now: datetime | None = None,
) -> tuple[Subscription, bool]:
    """Create a new subscription or extend an existing active subscription.

    Returns a tuple ``(subscription, is_extended)``.
    """
    current_time = now or utcnow()
    existing = await get_active_subscription(
        session,
        user_id=user_id,
        channel_id=channel_id,
    )

    if existing is not None:
        existing.tariff_id = tariff.id
        if tariff.duration_days <= 0 or existing.expires_at is None:
            # Lifetime tariff or already lifetime subscription
            existing.expires_at = None
        else:
            base_time = existing.expires_at if existing.expires_at > current_time else current_time
            existing.expires_at = base_time + timedelta(days=tariff.duration_days)
        existing.reminded_3d = False
        await session.commit()
        await session.refresh(existing)
        return existing, True

    expires_at = (
        None if tariff.duration_days <= 0 else current_time + timedelta(days=tariff.duration_days)
    )
    subscription = Subscription(
        user_id=user_id,
        channel_id=channel_id,
        tariff_id=tariff.id,
        expires_at=expires_at,
        status=SubscriptionStatus.ACTIVE.value,
        reminded_3d=False,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription, False


async def get_user_subscriptions(session: AsyncSession, user_id: int) -> list[Subscription]:
    stmt = (
        select(Subscription)
        .options(
            joinedload(Subscription.channel),
            joinedload(Subscription.tariff),
        )
        .where(Subscription.user_id == user_id)
        .order_by(
            case((Subscription.status == SubscriptionStatus.ACTIVE.value, 0), else_=1),
            Subscription.id.desc(),
        )
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def count_active_subscribers(session: AsyncSession, channel_id: int) -> int:
    now = utcnow()
    stmt = select(func.count(Subscription.id)).where(
        Subscription.channel_id == channel_id,
        Subscription.status == SubscriptionStatus.ACTIVE.value,
        or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
    )
    count = await session.scalar(stmt)
    return int(count or 0)


async def get_channel_stats(session: AsyncSession, channel_id: int) -> ChannelStats:
    now = utcnow()
    soon_threshold = now + timedelta(days=3)

    active_cond = and_(
        Subscription.status == SubscriptionStatus.ACTIVE.value,
        or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
    )

    summary_stmt = select(
        func.count(Subscription.id).label("total"),
        func.coalesce(func.sum(case((active_cond, 1), else_=0)), 0).label("active"),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            Subscription.status == SubscriptionStatus.ACTIVE.value,
                            Subscription.expires_at.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("lifetime"),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            Subscription.status == SubscriptionStatus.ACTIVE.value,
                            Subscription.expires_at.is_not(None),
                            Subscription.expires_at > now,
                            Subscription.expires_at <= soon_threshold,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("expiring_soon"),
        func.coalesce(
            func.sum(
                case(
                    (Subscription.status == SubscriptionStatus.EXPIRED.value, 1),
                    else_=0,
                )
            ),
            0,
        ).label("expired"),
    ).where(Subscription.channel_id == channel_id)

    summary_row = (await session.execute(summary_stmt)).one()

    tariffs_stmt = (
        select(
            Tariff.id,
            Tariff.name,
            Tariff.price_stars,
            Tariff.duration_days,
            Tariff.is_active,
            func.count(Subscription.id).label("total_subs"),
            func.coalesce(func.sum(case((active_cond, 1), else_=0)), 0).label("active_subs"),
        )
        .outerjoin(
            Subscription,
            and_(
                Subscription.tariff_id == Tariff.id,
                Subscription.channel_id == channel_id,
            ),
        )
        .where(Tariff.channel_id == channel_id)
        .group_by(
            Tariff.id,
            Tariff.name,
            Tariff.price_stars,
            Tariff.duration_days,
            Tariff.is_active,
        )
        .order_by(Tariff.price_stars.asc(), Tariff.id.asc())
    )

    tariff_rows = (await session.execute(tariffs_stmt)).all()
    tariff_stats = [
        TariffStat(
            tariff_id=row.id,
            name=row.name,
            price_stars=row.price_stars,
            duration_days=row.duration_days,
            is_active=row.is_active,
            active_subscribers=int(row.active_subs or 0),
            total_subscriptions=int(row.total_subs or 0),
        )
        for row in tariff_rows
    ]

    return ChannelStats(
        active_subscribers=int(summary_row.active or 0),
        lifetime_subscribers=int(summary_row.lifetime or 0),
        expiring_soon_subscribers=int(summary_row.expiring_soon or 0),
        expired_subscriptions=int(summary_row.expired or 0),
        total_subscriptions=int(summary_row.total or 0),
        tariffs=tariff_stats,
    )


# ---------------------------------------------------------------------------
# Scheduler Queries
# ---------------------------------------------------------------------------


async def get_expired_subscriptions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[Subscription]:
    """Fetch active subscriptions whose expiration timestamp has already passed."""
    current_time = now or utcnow()
    stmt = (
        select(Subscription)
        .options(
            joinedload(Subscription.user),
            joinedload(Subscription.channel),
            joinedload(Subscription.tariff),
        )
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at < current_time,
        )
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def get_subscriptions_to_remind(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    days_ahead: int = 3,
) -> list[Subscription]:
    """Fetch active subscriptions expiring within ``days_ahead`` days not yet reminded."""
    current_time = now or utcnow()
    threshold = current_time + timedelta(days=days_ahead)
    stmt = (
        select(Subscription)
        .options(
            joinedload(Subscription.user),
            joinedload(Subscription.channel),
            joinedload(Subscription.tariff),
        )
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at > current_time,
            Subscription.expires_at <= threshold,
            Subscription.reminded_3d.is_(False),
        )
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def mark_subscription_reminded(
    session: AsyncSession,
    subscription: Subscription,
) -> None:
    subscription.reminded_3d = True
    await session.commit()


async def mark_subscription_expired(
    session: AsyncSession,
    subscription: Subscription,
) -> None:
    subscription.status = SubscriptionStatus.EXPIRED.value
    await session.commit()
