from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database import requests as db
from bot.database.models import Subscription
from bot.keyboards import renew_subscription_kb
from bot.utils import escape_html, format_datetime, utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SubscriptionCheckResult:
    """Summary returned by a scheduled subscription audit run."""

    checked_at_utc: str
    expired_found: int = 0
    expired_processed: int = 0
    reminders_found: int = 0
    reminders_sent: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


async def _kick_user_from_channel(
    bot: Bot,
    *,
    tg_channel_id: int,
    tg_user_id: int,
    subscription_id: int,
) -> None:
    """Remove a user from a channel via ban + immediate unban so they can rejoin later."""
    try:
        await bot.ban_chat_member(chat_id=tg_channel_id, user_id=tg_user_id)
    except TelegramForbiddenError as exc:
        logger.warning(
            "Cannot ban user=%s in channel=%s (sub=%s): bot lacks permissions (%s)",
            tg_user_id,
            tg_channel_id,
            subscription_id,
            exc,
        )
        return
    except TelegramBadRequest as exc:
        logger.warning(
            "Cannot ban user=%s in channel=%s (sub=%s): %s",
            tg_user_id,
            tg_channel_id,
            subscription_id,
            exc,
        )
        return
    except TelegramAPIError as exc:
        logger.error(
            "Telegram API error while banning user=%s in channel=%s (sub=%s): %s",
            tg_user_id,
            tg_channel_id,
            subscription_id,
            exc,
        )
        return

    try:
        await bot.unban_chat_member(
            chat_id=tg_channel_id,
            user_id=tg_user_id,
            only_if_banned=True,
        )
    except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError) as exc:
        logger.warning(
            "User=%s was banned in channel=%s (sub=%s), but unban failed: %s",
            tg_user_id,
            tg_channel_id,
            subscription_id,
            exc,
        )


async def _process_expired_subscription(
    bot: Bot,
    session: AsyncSession,
    subscription: Subscription,
) -> None:
    user = subscription.user
    channel = subscription.channel

    await db.mark_subscription_expired(session, subscription)

    if user is None or channel is None:
        logger.warning(
            "Marked subscription #%s expired, but user or channel relation is missing.",
            subscription.id,
        )
        return

    await _kick_user_from_channel(
        bot,
        tg_channel_id=channel.tg_channel_id,
        tg_user_id=user.telegram_id,
        subscription_id=subscription.id,
    )

    text = (
        f"🚫 <b>Срок вашей подписки на канал «{escape_html(channel.title)}» истёк.</b>\n\n"
        "Доступ к каналу закрыт. Вы можете оформить подписку повторно в любой момент, "
        "нажав кнопку ниже:"
    )
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=renew_subscription_kb(
                channel.id,
                button_text="🔄 Оформить подписку снова",
            ),
        )
    except TelegramForbiddenError:
        logger.info(
            "User %s blocked the bot; skipping expiration notice for sub #%s",
            user.telegram_id,
            subscription.id,
        )
    except (TelegramBadRequest, TelegramAPIError) as exc:
        logger.warning(
            "Failed to send expiration notice to user=%s for sub #%s: %s",
            user.telegram_id,
            subscription.id,
            exc,
        )


async def _process_expiring_reminder(
    bot: Bot,
    session: AsyncSession,
    subscription: Subscription,
) -> bool:
    """Send a 3-day reminder and return True if the reminder was delivered."""
    user = subscription.user
    channel = subscription.channel

    if user is None or channel is None:
        await db.mark_subscription_reminded(session, subscription)
        return False

    text = (
        f"⏳ <b>Напоминание о подписке</b>\n\n"
        f"Ваша подписка на канал <b>«{escape_html(channel.title)}»</b> "
        f"истекает <b>{format_datetime(subscription.expires_at)}</b>.\n\n"
        "Продлите подписку заранее, чтобы не потерять доступ — новый срок "
        "прибавится к текущему:"
    )

    delivered = False
    should_mark_reminded = True
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=renew_subscription_kb(
                channel.id,
                button_text="🔄 Продлить подписку",
            ),
        )
        delivered = True
    except TelegramForbiddenError:
        logger.info(
            "User %s blocked the bot; marking sub #%s as reminded to avoid retries",
            user.telegram_id,
            subscription.id,
        )
    except TelegramBadRequest as exc:
        logger.warning(
            "Bad request sending 3d reminder to user=%s (sub #%s): %s",
            user.telegram_id,
            subscription.id,
            exc,
        )
    except TelegramAPIError as exc:
        # Transient network/rate-limit error: retry on the next cron invocation.
        should_mark_reminded = False
        logger.error(
            "Transient Telegram API error sending 3d reminder to user=%s (sub #%s): %s",
            user.telegram_id,
            subscription.id,
            exc,
        )

    if should_mark_reminded:
        await db.mark_subscription_reminded(session, subscription)
    return delivered


async def check_subscriptions(
    bot: Bot,
    session_maker: async_sessionmaker[AsyncSession],
) -> SubscriptionCheckResult:
    """Audit active subscriptions: expire & kick overdue users, remind expiring ones.

    Intended to be invoked by Vercel Cron via ``GET /api/cron``.
    """
    now = utcnow()
    result = SubscriptionCheckResult(checked_at_utc=now.isoformat(timespec="seconds"))
    logger.info("Running cron subscription check at %s UTC", result.checked_at_utc)

    async with session_maker() as session:
        expired_subs = await db.get_expired_subscriptions(session, now=now)
        result.expired_found = len(expired_subs)
        logger.info("Found %d expired subscription(s)", result.expired_found)

        for subscription in expired_subs:
            try:
                await _process_expired_subscription(bot, session, subscription)
                result.expired_processed += 1
            except Exception:
                await session.rollback()
                result.errors += 1
                logger.exception(
                    "Unexpected error processing expired subscription #%s",
                    subscription.id,
                )

        reminding_subs = await db.get_subscriptions_to_remind(
            session,
            now=now,
            days_ahead=3,
        )
        result.reminders_found = len(reminding_subs)
        logger.info("Found %d subscription(s) requiring 3-day reminder", result.reminders_found)

        for subscription in reminding_subs:
            try:
                delivered = await _process_expiring_reminder(bot, session, subscription)
                if delivered:
                    result.reminders_sent += 1
            except Exception:
                await session.rollback()
                result.errors += 1
                logger.exception(
                    "Unexpected error sending 3-day reminder for subscription #%s",
                    subscription.id,
                )

    return result
