from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberAdministrator,
    ChatMemberOwner,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import requests as db
from bot.database.models import Channel
from bot.keyboards import (
    MenuCB,
    OwnerChannelCB,
    OwnerTariffCB,
    TariffDurationCB,
    cancel_fsm_kb,
    channel_card_kb,
    channel_connected_kb,
    channel_stats_kb,
    main_menu_kb,
    my_channels_kb,
    tariff_duration_kb,
)
from bot.states import ConnectChannelStates, CreateTariffStates, EditChannelStates
from bot.utils import (
    build_channel_deeplink,
    escape_html,
    format_duration,
    safe_answer_callback,
    safe_edit_text,
)

logger = logging.getLogger(__name__)

router = Router(name="admin_channel")

ALLOWED_TARIFF_DURATIONS = {0, 7, 30, 90}
MAX_TARIFF_NAME_LENGTH = 64
MIN_STARS_PRICE = 1
MAX_STARS_PRICE = 100_000
MAX_DESCRIPTION_LENGTH = 500


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _main_menu_text(full_name: str) -> str:
    safe_name = escape_html(full_name)
    return (
        f"👋 Привет, <b>{safe_name}</b>!\n\n"
        "Это SaaS-платформа для монетизации закрытых Telegram-каналов и чатов "
        "с оплатой через <b>Telegram Stars (⭐)</b>.\n\n"
        "Выберите действие:"
    )


async def _render_channel_card(
    session: AsyncSession,
    bot: Bot,
    channel: Channel,
) -> tuple[str, list]:
    tariffs = await db.get_channel_tariffs(session, channel.id, active_only=False)
    active_subscribers = await db.count_active_subscribers(session, channel.id)
    bot_info = await bot.me()
    bot_username = bot_info.username or ""
    deeplink = build_channel_deeplink(bot_username, channel.id)

    status_text = "🟢 Активен" if channel.is_active else "⏸ Приостановлен"
    description_text = (
        escape_html(channel.description) if channel.description else "<i>Не указано</i>"
    )

    if tariffs:
        tariff_lines = []
        for tariff in tariffs:
            state_badge = "✅" if tariff.is_active else "⏸"
            duration = format_duration(tariff.duration_days)
            tariff_lines.append(
                f"  {state_badge} <b>{escape_html(tariff.name)}</b> — "
                f"{tariff.price_stars} ⭐ ({duration})"
            )
        tariffs_block = "\n".join(tariff_lines)
    else:
        tariffs_block = "  <i>Тарифы ещё не добавлены</i>"

    text = (
        f"📢 <b>{escape_html(channel.title)}</b>\n"
        f"Статус: {status_text}\n"
        f"👥 Активных подписчиков: <b>{active_subscribers}</b>\n\n"
        f"📝 <b>Описание:</b>\n{description_text}\n\n"
        f"💳 <b>Тарифы</b> <i>(нажмите на тариф ниже, чтобы вкл/выкл)</i>:\n"
        f"{tariffs_block}\n\n"
        f"🔗 <b>Ссылка для подписчиков:</b>\n"
        f"<code>{escape_html(deeplink)}</code>"
    )
    return text, tariffs


async def _show_channel_card_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    channel: Channel,
    *,
    edit: bool = True,
    prefix_notice: str | None = None,
) -> None:
    text, tariffs = await _render_channel_card(session, bot, channel)
    if prefix_notice:
        text = f"{prefix_notice}\n\n{text}"
    markup = channel_card_kb(channel, tariffs)
    if edit:
        await safe_edit_text(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def _extract_chat_target(message: Message) -> tuple[int | str | None, str | None]:
    """Extract target chat ID or @username from forwarded message or text.

    Returns ``(target, error_hint)``.
    """
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, None
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.id, None
    if isinstance(origin, (MessageOriginUser, MessageOriginHiddenUser)):
        return None, (
            "⚠️ Это сообщение переслано от обычного пользователя, поэтому Telegram "
            "скрывает ID канала/чата.\n\n"
            "• Для <b>канала</b> перешлите любой пост прямо из канала.\n"
            "• Для <b>закрытого чата (группы)</b> отправьте его числовой ID "
            "(например, <code>-1001234567890</code>)."
        )

    # Legacy Bot API field fallback
    legacy_chat = getattr(message, "forward_from_chat", None)
    if legacy_chat is not None:
        return legacy_chat.id, None

    if message.text:
        raw = message.text.strip()
        if raw.startswith("@") and len(raw) >= 5 and " " not in raw:
            return raw, None
        try:
            chat_id = int(raw)
        except ValueError:
            chat_id = None
        if chat_id is not None and chat_id < 0:
            return chat_id, None

    return None, (
        "⚠️ Не удалось определить канал или чат.\n\n"
        "Перешлите любое сообщение из нужного канала или отправьте числовой ID "
        "чата (например, <code>-1001234567890</code>)."
    )


# ---------------------------------------------------------------------------
# Main menu & navigation
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return

    await state.clear()
    user = await db.upsert_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await message.answer(
        _main_menu_text(user.full_name),
        reply_markup=main_menu_kb(),
    )


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main_menu(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    if isinstance(callback.message, Message):
        await safe_edit_text(
            callback.message,
            _main_menu_text(user.full_name),
            reply_markup=main_menu_kb(),
        )
    await safe_answer_callback(callback)


@router.callback_query(StateFilter("*"), MenuCB.filter(F.action == "cancel"))
async def cb_cancel_fsm(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    channel_id = data.get("channel_id")
    await state.clear()

    if isinstance(callback.message, Message):
        user = await db.upsert_user(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        if isinstance(channel_id, int):
            channel = await db.get_owner_channel(session, channel_id, user.id)
            if channel is not None:
                await _show_channel_card_message(
                    callback.message,
                    session,
                    bot,
                    channel,
                    edit=True,
                )
                await safe_answer_callback(callback, "Действие отменено.")
                return

        await safe_edit_text(
            callback.message,
            _main_menu_text(user.full_name),
            reply_markup=main_menu_kb(),
        )
    await safe_answer_callback(callback, "Действие отменено.")


# ---------------------------------------------------------------------------
# Scenario A.2: Connect channel (FSM)
# ---------------------------------------------------------------------------


@router.callback_query(MenuCB.filter(F.action == "connect_channel"))
async def cb_start_connect_channel(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    bot_info = await bot.me()
    bot_username = f"@{bot_info.username}" if bot_info.username else "этого бота"

    await state.set_state(ConnectChannelStates.waiting_for_channel)
    text = (
        "➕ <b>Подключение закрытого канала или чата</b>\n\n"
        f"1. Добавьте <b>{escape_html(bot_username)}</b> в ваш канал/чат как администратора.\n"
        "2. Выдайте боту права:\n"
        "   • <b>Приглашение пользователей</b> (<code>can_invite_users</code>)\n"
        "   • <b>Удаление / блокировка пользователей</b> (<code>can_restrict_members</code>)\n"
        "3. Перешлите сюда любое сообщение из канала\n"
        "   <i>(для закрытого чата можно отправить его ID, например <code>-1001234567890</code>)</i>."
    )
    if isinstance(callback.message, Message):
        await safe_edit_text(callback.message, text, reply_markup=cancel_fsm_kb())
    await safe_answer_callback(callback)


@router.message(ConnectChannelStates.waiting_for_channel)
async def process_connect_channel(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return

    target, error_hint = _extract_chat_target(message)
    if target is None:
        await message.answer(
            error_hint or "⚠️ Перешлите сообщение из канала или укажите ID чата.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    try:
        chat = await bot.get_chat(target)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("get_chat failed for target=%s: %s", target, exc)
        await message.answer(
            "❌ Не удалось получить доступ к каналу/чату.\n"
            "Убедитесь, что бот добавлен в него как администратор, и попробуйте снова.",
            reply_markup=cancel_fsm_kb(),
        )
        return
    except TelegramAPIError as exc:
        logger.error("Unexpected Telegram API error during get_chat(%s): %s", target, exc)
        await message.answer(
            "❌ Временная ошибка Telegram API. Попробуйте ещё раз чуть позже.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        await message.answer(
            "❌ Можно подключать только каналы, группы и супергруппы.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    # Verify bot is administrator with required rights
    try:
        bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("get_chat_member(bot) failed for chat=%s: %s", chat.id, exc)
        await message.answer(
            "❌ Бот не состоит в этом канале/чате или не имеет прав администратора.\n"
            "Добавьте бота в администраторы и отправьте сообщение снова.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    if not isinstance(bot_member, ChatMemberAdministrator):
        await message.answer(
            "❌ Бот не является администратором в этом канале/чате.\n"
            "Назначьте бота администратором с правами приглашения и удаления "
            "пользователей, затем попробуйте снова.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    missing_rights: list[str] = []
    if not bot_member.can_invite_users:
        missing_rights.append("• Приглашение пользователей (<code>can_invite_users</code>)")
    if not bot_member.can_restrict_members:
        missing_rights.append(
            "• Блокировка / удаление пользователей (<code>can_restrict_members</code>)"
        )

    if missing_rights:
        rights_text = "\n".join(missing_rights)
        await message.answer(
            "❌ У бота недостаточно прав в канале "
            f"<b>{escape_html(chat.title or str(chat.id))}</b>.\n\n"
            "Выдайте недостающие права администратора:\n"
            f"{rights_text}\n\n"
            "После выдачи прав снова перешлите сообщение из канала.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    # Verify that the requesting user is an owner/admin of the channel
    try:
        user_member = await bot.get_chat_member(
            chat_id=chat.id,
            user_id=message.from_user.id,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "get_chat_member(user=%s) failed for chat=%s: %s",
            message.from_user.id,
            chat.id,
            exc,
        )
        await message.answer(
            "❌ Не удалось проверить ваши права в этом канале/чате.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    if not isinstance(user_member, (ChatMemberOwner, ChatMemberAdministrator)):
        await message.answer(
            "❌ Подключить канал может только его владелец или администратор.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    user = await db.upsert_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )

    chat_title = chat.title or f"Канал {chat.id}"
    existing = await db.get_channel_by_tg_id(session, chat.id)

    if existing is not None:
        if existing.owner_id != user.id:
            await message.answer(
                "❌ Этот канал/чат уже подключён другим владельцем.",
                reply_markup=cancel_fsm_kb(),
            )
            return

        updated = await db.update_channel(
            session,
            existing,
            title=chat_title,
            is_active=True,
        )
        await state.clear()
        await _show_channel_card_message(
            message,
            session,
            bot,
            updated,
            edit=False,
            prefix_notice="ℹ️ Этот канал уже был подключён. Данные и права успешно обновлены!",
        )
        return

    channel = await db.create_channel(
        session,
        owner_id=user.id,
        tg_channel_id=chat.id,
        title=chat_title,
        description=chat.description,
    )
    await state.clear()

    await message.answer(
        f"✅ Канал <b>{escape_html(channel.title)}</b> успешно подключён!\n\n"
        "Теперь создайте первый тариф, чтобы подписчики могли оплачивать доступ "
        "через Telegram Stars.",
        reply_markup=channel_connected_kb(channel.id),
    )


# ---------------------------------------------------------------------------
# Scenario A.3: My channels, Channel card, Stats, Description, Tariffs
# ---------------------------------------------------------------------------


@router.callback_query(MenuCB.filter(F.action == "my_channels"))
async def cb_my_channels(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channels = await db.get_owner_channels(session, user.id)

    if channels:
        text = (
            "📢 <b>Мои каналы</b>\n\n"
            f"Подключено каналов: <b>{len(channels)}</b>\n"
            "Выберите канал для управления тарифами и просмотра статистики:"
        )
    else:
        text = (
            "📢 <b>Мои каналы</b>\n\n"
            "У вас пока нет подключённых каналов.\n"
            "Нажмите «➕ Подключить канал», чтобы добавить свой первый закрытый канал или чат."
        )

    if isinstance(callback.message, Message):
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=my_channels_kb(channels),
        )
    await safe_answer_callback(callback)


@router.callback_query(OwnerChannelCB.filter(F.action == "view"))
async def cb_view_channel(
    callback: CallbackQuery,
    callback_data: OwnerChannelCB,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден или нет доступа.", show_alert=True)
        return

    if isinstance(callback.message, Message):
        await _show_channel_card_message(
            callback.message,
            session,
            bot,
            channel,
            edit=True,
        )
    await safe_answer_callback(callback)


@router.callback_query(OwnerChannelCB.filter(F.action == "toggle_active"))
async def cb_toggle_channel_active(
    callback: CallbackQuery,
    callback_data: OwnerChannelCB,
    bot: Bot,
    session: AsyncSession,
) -> None:
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    updated = await db.update_channel(session, channel, is_active=not channel.is_active)
    status_notice = "Продажи возобновлены." if updated.is_active else "Продажи приостановлены."

    if isinstance(callback.message, Message):
        await _show_channel_card_message(
            callback.message,
            session,
            bot,
            updated,
            edit=True,
        )
    await safe_answer_callback(callback, status_notice)


@router.callback_query(OwnerTariffCB.filter(F.action == "toggle"))
async def cb_toggle_tariff(
    callback: CallbackQuery,
    callback_data: OwnerTariffCB,
    bot: Bot,
    session: AsyncSession,
) -> None:
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    tariff = await db.get_tariff_by_id(
        session,
        callback_data.tariff_id,
        channel_id=channel.id,
    )
    if tariff is None:
        await safe_answer_callback(callback, "Тариф не найден.", show_alert=True)
        return

    updated = await db.toggle_tariff_active(session, tariff)
    notice = f"Тариф «{updated.name}» {'включён' if updated.is_active else 'скрыт'}."

    if isinstance(callback.message, Message):
        await _show_channel_card_message(
            callback.message,
            session,
            bot,
            channel,
            edit=True,
        )
    await safe_answer_callback(callback, notice)


@router.callback_query(OwnerChannelCB.filter(F.action == "stats"))
async def cb_channel_stats(
    callback: CallbackQuery,
    callback_data: OwnerChannelCB,
    session: AsyncSession,
) -> None:
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    stats = await db.get_channel_stats(session, channel.id)

    if stats.tariffs:
        tariff_lines = []
        for item in stats.tariffs:
            badge = "✅" if item.is_active else "⏸"
            tariff_lines.append(
                f"  {badge} <b>{escape_html(item.name)}</b> "
                f"({item.price_stars} ⭐ / {format_duration(item.duration_days)}): "
                f"активных <b>{item.active_subscribers}</b>, всего <b>{item.total_subscriptions}</b>"
            )
        tariffs_section = "\n".join(tariff_lines)
    else:
        tariffs_section = "  <i>Тарифы ещё не созданы</i>"

    text = (
        f"📊 <b>Статистика канала «{escape_html(channel.title)}»</b>\n\n"
        f"👥 Активных подписчиков: <b>{stats.active_subscribers}</b>\n"
        f"♾ Из них бессрочных: <b>{stats.lifetime_subscribers}</b>\n"
        f"⏳ Истекают в ближайшие 3 дня: <b>{stats.expiring_soon_subscribers}</b>\n"
        f"🚫 Истёкших подписок: <b>{stats.expired_subscriptions}</b>\n"
        f"📈 Всего записей о подписках: <b>{stats.total_subscriptions}</b>\n\n"
        f"💳 <b>По тарифам:</b>\n{tariffs_section}"
    )

    if isinstance(callback.message, Message):
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=channel_stats_kb(channel.id),
        )
    await safe_answer_callback(callback)


# ---------------------------------------------------------------------------
# Edit channel description (FSM)
# ---------------------------------------------------------------------------


@router.callback_query(OwnerChannelCB.filter(F.action == "edit_desc"))
async def cb_start_edit_description(
    callback: CallbackQuery,
    callback_data: OwnerChannelCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    await state.set_state(EditChannelStates.waiting_for_description)
    await state.update_data(channel_id=channel.id)

    text = (
        f"✏️ <b>Описание канала «{escape_html(channel.title)}»</b>\n\n"
        "Отправьте новое описание, которое увидят подписчики перед оплатой "
        f"(до {MAX_DESCRIPTION_LENGTH} символов).\n"
        "Отправьте <code>-</code>, чтобы очистить описание."
    )
    if isinstance(callback.message, Message):
        await safe_edit_text(callback.message, text, reply_markup=cancel_fsm_kb())
    await safe_answer_callback(callback)


@router.message(EditChannelStates.waiting_for_description, F.text)
async def process_edit_description(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None or message.text is None:
        return

    raw_text = message.text.strip()
    if len(raw_text) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"❌ Описание слишком длинное (максимум {MAX_DESCRIPTION_LENGTH} символов). "
            "Сократите текст и отправьте снова.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    data = await state.get_data()
    channel_id = data.get("channel_id")
    user = await db.upsert_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    channel = (
        await db.get_owner_channel(session, channel_id, user.id)
        if isinstance(channel_id, int)
        else None
    )
    await state.clear()

    if channel is None:
        await message.answer("❌ Канал не найден.", reply_markup=main_menu_kb())
        return

    new_description = None if raw_text == "-" else raw_text
    updated = await db.update_channel(
        session,
        channel,
        description=new_description,
        set_description=True,
    )
    await _show_channel_card_message(
        message,
        session,
        bot,
        updated,
        edit=False,
        prefix_notice="✅ Описание канала обновлено.",
    )


@router.message(EditChannelStates.waiting_for_description)
async def process_edit_description_invalid(message: Message) -> None:
    await message.answer(
        "⚠️ Пожалуйста, отправьте текстовое описание или <code>-</code> для очистки.",
        reply_markup=cancel_fsm_kb(),
    )


# ---------------------------------------------------------------------------
# Create tariff (FSM): Name -> Price in Stars -> Duration
# ---------------------------------------------------------------------------


@router.callback_query(OwnerChannelCB.filter(F.action == "add_tariff"))
async def cb_start_add_tariff(
    callback: CallbackQuery,
    callback_data: OwnerChannelCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, callback_data.channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    await state.set_state(CreateTariffStates.waiting_for_name)
    await state.update_data(channel_id=channel.id)

    text = (
        f"➕ <b>Новый тариф для «{escape_html(channel.title)}»</b>\n\n"
        "Шаг 1 из 3. Введите <b>название тарифа</b>\n"
        "<i>Например: «1 месяц», «Квартал VIP», «Доступ навсегда»</i>"
    )
    if isinstance(callback.message, Message):
        await safe_edit_text(callback.message, text, reply_markup=cancel_fsm_kb())
    await safe_answer_callback(callback)


@router.message(CreateTariffStates.waiting_for_name, F.text)
async def process_tariff_name(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return

    name = message.text.strip()
    if not name or len(name) > MAX_TARIFF_NAME_LENGTH:
        await message.answer(
            f"❌ Название тарифа должно содержать от 1 до {MAX_TARIFF_NAME_LENGTH} символов. "
            "Попробуйте снова.",
            reply_markup=cancel_fsm_kb(),
        )
        return

    await state.update_data(tariff_name=name)
    await state.set_state(CreateTariffStates.waiting_for_price)
    await message.answer(
        f"Тариф: <b>{escape_html(name)}</b>\n\n"
        "Шаг 2 из 3. Введите <b>стоимость в Telegram Stars (⭐)</b> — целое число "
        f"от {MIN_STARS_PRICE} до {MAX_STARS_PRICE}:",
        reply_markup=cancel_fsm_kb(),
    )


@router.message(CreateTariffStates.waiting_for_name)
async def process_tariff_name_invalid(message: Message) -> None:
    await message.answer(
        "⚠️ Введите текстовое название тарифа.",
        reply_markup=cancel_fsm_kb(),
    )


@router.message(CreateTariffStates.waiting_for_price, F.text)
async def process_tariff_price(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return

    raw_price = message.text.strip()
    try:
        price_stars = int(raw_price)
    except ValueError:
        price_stars = -1

    if not (MIN_STARS_PRICE <= price_stars <= MAX_STARS_PRICE):
        await message.answer(
            "❌ Цена должна быть целым числом от "
            f"<b>{MIN_STARS_PRICE}</b> до <b>{MAX_STARS_PRICE}</b> Telegram Stars.\n"
            "Введите корректное число:",
            reply_markup=cancel_fsm_kb(),
        )
        return

    data = await state.get_data()
    tariff_name = str(data.get("tariff_name", ""))
    await state.update_data(price_stars=price_stars)
    await state.set_state(CreateTariffStates.waiting_for_duration)

    await message.answer(
        f"Тариф: <b>{escape_html(tariff_name)}</b>\n"
        f"Цена: <b>{price_stars} ⭐</b>\n\n"
        "Шаг 3 из 3. Выберите <b>срок действия доступа</b>:",
        reply_markup=tariff_duration_kb(),
    )


@router.message(CreateTariffStates.waiting_for_price)
async def process_tariff_price_invalid(message: Message) -> None:
    await message.answer(
        "⚠️ Введите цену целым числом в Telegram Stars (например, <code>250</code>).",
        reply_markup=cancel_fsm_kb(),
    )


@router.callback_query(
    CreateTariffStates.waiting_for_duration,
    TariffDurationCB.filter(),
)
async def process_tariff_duration(
    callback: CallbackQuery,
    callback_data: TariffDurationCB,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback_data.days not in ALLOWED_TARIFF_DURATIONS:
        await safe_answer_callback(callback, "Недопустимый срок действия.", show_alert=True)
        return

    data = await state.get_data()
    channel_id = data.get("channel_id")
    tariff_name = data.get("tariff_name")
    price_stars = data.get("price_stars")
    await state.clear()

    if not (
        isinstance(channel_id, int)
        and isinstance(tariff_name, str)
        and isinstance(price_stars, int)
    ):
        await safe_answer_callback(
            callback,
            "Сессия создания тарифа устарела. Начните заново.",
            show_alert=True,
        )
        return

    user = await db.upsert_user(
        session,
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    channel = await db.get_owner_channel(session, channel_id, user.id)
    if channel is None:
        await safe_answer_callback(callback, "Канал не найден.", show_alert=True)
        return

    tariff = await db.create_tariff(
        session,
        channel_id=channel.id,
        name=tariff_name,
        price_stars=price_stars,
        duration_days=callback_data.days,
    )

    notice = (
        f"✅ Тариф <b>{escape_html(tariff.name)}</b> "
        f"({tariff.price_stars} ⭐ / {format_duration(tariff.duration_days)}) успешно добавлен!"
    )
    if isinstance(callback.message, Message):
        await _show_channel_card_message(
            callback.message,
            session,
            bot,
            channel,
            edit=True,
            prefix_notice=notice,
        )
    await safe_answer_callback(callback, "Тариф создан!")


@router.message(CreateTariffStates.waiting_for_duration)
async def process_tariff_duration_text_fallback(message: Message) -> None:
    await message.answer(
        "⚠️ Выберите срок действия с помощью кнопок ниже:",
        reply_markup=tariff_duration_kb(),
    )
