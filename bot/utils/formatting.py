from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime compatible with SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def escape_html(text: str) -> str:
    """Escape text for safe usage with Telegram HTML parse mode."""
    return html.escape(text, quote=True)


def format_duration(days: int) -> str:
    """Format tariff duration in days into a human-readable Russian label."""
    if days <= 0:
        return "Навсегда"
    mod100 = days % 100
    mod10 = days % 10
    if 11 <= mod100 <= 14:
        suffix = "дней"
    elif mod10 == 1:
        suffix = "день"
    elif 2 <= mod10 <= 4:
        suffix = "дня"
    else:
        suffix = "дней"
    return f"{days} {suffix}"


def format_datetime(dt: datetime | None) -> str:
    """Format UTC datetime for user-facing display."""
    if dt is None:
        return "Навсегда"
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def build_channel_deeplink(bot_username: str, channel_id: int) -> str:
    """Build subscriber paywall deep link for a channel."""
    return f"https://t.me/{bot_username}?start=ch_{channel_id}"


async def safe_answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    """Answer callback query safely, ignoring stale query errors."""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        logger.debug("Ignoring callback answer error: %s", exc)


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> Message | None:
    """Edit message text safely; fallback to sending a new message if not editable."""
    if message.text is None:
        try:
            return await message.answer(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as send_exc:
            logger.warning("Failed to send message replacing non-text message: %s", send_exc)
            return None

    try:
        result = await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        return result if isinstance(result, Message) else None
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if "message is not modified" in error_text:
            return message
        logger.warning("Failed to edit message %s, sending new one: %s", message.message_id, exc)
        try:
            return await message.answer(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as send_exc:
            logger.warning("Failed to send fallback message: %s", send_exc)
            return None
    except TelegramForbiddenError as exc:
        logger.warning("Bot forbidden to edit message in chat %s: %s", message.chat.id, exc)
        return None
