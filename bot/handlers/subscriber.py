from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import requests as db
from bot.database.models import SubscriptionStatus
from bot.keyboards import (
    BuyTariffCB,
    MenuCB,
    SubscriberChannelCB,
    back_to_main_kb,
    invite_link_kb,
    invoice_pay_kb,
    my_subscriptions_kb,
    subscriber_tariffs_kb,
)
from bot.utils import (
    escape_html,
    format_datetime,
    format_duration,
    safe_answer_callback,
    safe_edit_text,
)

logger = logging.getLogger(__name__)

router = Router(name="subscriber")

INVOICE_PAYLOAD_PREFIX = "sub"


def _build_invoice_payload(channel_id: int, tariff_id: int) -> str:
    return f"{INVOICE_PAYLOAD_PREFIX}:{channel_id}:{tariff_id}"


def _parse_invoice_payload(payload: str) -> tuple[int, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != INVOICE_PAYLOAD_PREFIX:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_channel_deeplink(args: str | None) -> int | None:
    if not args or not args.startswith("ch_"):
        return None
    raw_id = args.removeprefix("ch_")
    if not raw_id.isdigit():
        return None
    return int(raw_id)


async def _build_paywall_view(
    session: AsyncSession,
    *,
    channel_id: int,
    user_id: int,
) -> tuple[str, list] | tuple[str, None]:
    channel = await db.get_channel_by_id(session, channel_id)
    if channel is None or not channel.is_active:
        return (
            "❌ Канал не найден или временно приостановил приём новых подписчиков.",
            None,
        )

    tariffs = await db.get_channel_tariffs(session, channel.id, active_only=True)
    if not tariffs:
        return (
            f"📢 <b>{escape_html(channel.title)}</b>\n\n"
            "К сожалению, для этого канала пока нет активных тарифов.",
            None,
        )

    active_sub = await db.get_active_subscription(
        session,
        user_id=user_id,
        channel_id=channel.id,
    )

    parts = [f"📢 <b>{escape_html(channel.title)}</b>"]
    if channel.description:
        parts.append(escape_html(channel.description))

    if active_sub is not None:
        if active_sub.expires_at is None:
            parts.append("✨ <i>У вас уже есть бессрочная подписка на этот канал.</i>")
        else:
            parts.append(
                "⏳ <i>Ваша текущая подписка активна до "
                f"<b>{format_datetime(active_sub.expires_at)}</b>. "
                "При оплате новый срок прибавится к текущему.</i>"
            )

    parts.append("Выберите тариф для оплаты через <b>Telegram Stars (⭐)</b>:")
    return "\n\n".join(parts), tariffs


# ---------------------------------------------------------------------------
# Scenario B.1–B.2: Deep link /start ch_<id> & renewal callback
# ---------------------------------------------------------------------------


@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(
    message: Message,
    command: CommandObject,
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

    channel_id = _parse_channel_deeplink(command.args)
    if channel_id is None:
        await message.answer(
            "⚠️ Некорректная ссылка приглашения. Перейдите в главное меню.",
            reply_markup=back_to_main_kb(),
        )
        return

    text, tariffs = await _build_paywall_view(
        session,
        channel_id=channel_id,
        user_id=user.id,
    )
    if tariffs is None:
        await message.answer(text, reply_markup=back_to_main_kb())
        return

    await message.answer(
        text,
        reply_markup=subscriber_tariffs_kb(channel_id, tariffs),
    )


@router.callback_query(SubscriberChannelCB.filter())
async def cb_open_channel_paywall(
    callback: CallbackQuery,
    callback_data: SubscriberChannelCB,
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

    text, tariffs = await _build_paywall_view(
        session,
        channel_id=callback_data.channel_id,
        user_id=user.id,
    )
    markup = (
        subscriber_tariffs_kb(callback_data.channel_id, tariffs)
        if tariffs is not None
        else back_to_main_kb()
    )

    if isinstance(callback.message, Message):
        # Invoice messages cannot be edited into text; safe_edit_text handles fallback cleanly.
        await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_answer_callback(callback)


@router.callback_query(MenuCB.filter(F.action == "my_subscriptions"))
async def cb_my_subscriptions(
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
    subscriptions = await db.get_user_subscriptions(session, user.id)

    if not subscriptions:
        text = (
            "🎫 <b>Мои подписки</b>\n\n"
            "У вас пока нет оформленных подписок.\n"
            "Чтобы оформить доступ к закрытому каналу, перейдите по специальной "
            "ссылке от его владельца."
        )
    else:
        lines = ["🎫 <b>Мои подписки</b>\n"]
        for sub in subscriptions[:15]:
            channel_title = (
                escape_html(sub.channel.title)
                if sub.channel is not None
                else f"Канал #{sub.channel_id}"
            )
            tariff_name = (
                escape_html(sub.tariff.name)
                if sub.tariff is not None
                else "Тариф"
            )
            if sub.status == SubscriptionStatus.ACTIVE.value:
                badge = "🟢 Активна"
                expiry_info = f"до {format_datetime(sub.expires_at)}"
            else:
                badge = "🔴 Истекла"
                expiry_info = format_datetime(sub.expires_at)

            lines.append(
                f"• <b>{channel_title}</b>\n"
                f"  {badge} · {tariff_name} ({expiry_info})"
            )
        lines.append("\nНажмите на канал ниже, чтобы продлить или оформить подписку снова:")
        text = "\n".join(lines)

    if isinstance(callback.message, Message):
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=my_subscriptions_kb(subscriptions),
        )
    await safe_answer_callback(callback)


# ---------------------------------------------------------------------------
# Scenario B.3: Telegram Stars (XTR) payment flow
# ---------------------------------------------------------------------------


@router.callback_query(BuyTariffCB.filter())
async def cb_buy_tariff(
    callback: CallbackQuery,
    callback_data: BuyTariffCB,
    bot: Bot,
    session: AsyncSession,
) -> None:
    channel = await db.get_channel_by_id(session, callback_data.channel_id)
    if channel is None or not channel.is_active:
        await safe_answer_callback(
            callback,
            "Этот канал больше не доступен для подписки.",
            show_alert=True,
        )
        return

    tariff = await db.get_tariff_by_id(
        session,
        callback_data.tariff_id,
        channel_id=channel.id,
    )
    if tariff is None or not tariff.is_active:
        await safe_answer_callback(
            callback,
            "Выбранный тариф больше не активен.",
            show_alert=True,
        )
        return

    duration_label = format_duration(tariff.duration_days)
    invoice_title = f"Доступ: {channel.title}"[:32]
    invoice_description = (
        f"Тариф «{tariff.name}» ({duration_label}) для доступа к закрытому каналу «{channel.title}»."
    )[:255]

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=invoice_title,
            description=invoice_description,
            payload=_build_invoice_payload(channel.id, tariff.id),
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=tariff.name[:32],
                    amount=tariff.price_stars,
                )
            ],
            reply_markup=invoice_pay_kb(tariff.price_stars, channel.id),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.error(
            "Failed to send Stars invoice (channel=%s, tariff=%s, user=%s): %s",
            channel.id,
            tariff.id,
            callback.from_user.id,
            exc,
        )
        await safe_answer_callback(
            callback,
            "Не удалось выставить счёт. Попробуйте позже.",
            show_alert=True,
        )
        return

    await safe_answer_callback(callback)


@router.pre_checkout_query()
async def process_pre_checkout_query(
    pre_checkout_query: PreCheckoutQuery,
    session: AsyncSession,
) -> None:
    parsed = _parse_invoice_payload(pre_checkout_query.invoice_payload)
    if parsed is None:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Некорректные данные платежа. Попробуйте выбрать тариф заново.",
        )
        return

    channel_id, tariff_id = parsed
    channel = await db.get_channel_by_id(session, channel_id)
    tariff = await db.get_tariff_by_id(session, tariff_id, channel_id=channel_id)

    if channel is None or not channel.is_active:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Канал временно недоступен для оплаты.",
        )
        return

    if tariff is None or not tariff.is_active:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Этот тариф больше не доступен. Выберите другой тариф.",
        )
        return

    if pre_checkout_query.currency != "XTR" or pre_checkout_query.total_amount != tariff.price_stars:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Стоимость тарифа изменилась. Запросите новый счёт.",
        )
        return

    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(
    message: Message,
    bot: Bot,
    session: AsyncSession,
) -> None:
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    parsed = _parse_invoice_payload(payment.invoice_payload)
    if parsed is None:
        logger.error(
            "Received successful_payment with unknown payload=%r from user=%s (charge=%s)",
            payment.invoice_payload,
            message.from_user.id,
            payment.telegram_payment_charge_id,
        )
        await message.answer(
            "✅ Оплата получена, но возникла ошибка при определении тарифа. "
            "Свяжитесь с поддержкой и укажите ID платежа: "
            f"<code>{escape_html(payment.telegram_payment_charge_id)}</code>."
        )
        return

    channel_id, tariff_id = parsed
    user = await db.upsert_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    channel = await db.get_channel_by_id(session, channel_id)
    tariff = await db.get_tariff_by_id(session, tariff_id, channel_id=channel_id)

    if channel is None or tariff is None:
        logger.error(
            "Channel (%s) or tariff (%s) missing after payment %s",
            channel_id,
            tariff_id,
            payment.telegram_payment_charge_id,
        )
        await message.answer(
            "✅ Оплата получена, но канал или тариф не найден в системе. "
            "Свяжитесь с поддержкой и передайте ID платежа: "
            f"<code>{escape_html(payment.telegram_payment_charge_id)}</code>."
        )
        return

    subscription, is_extended = await db.create_or_extend_subscription(
        session,
        user_id=user.id,
        channel_id=channel.id,
        tariff=tariff,
    )

    action_word = "продлена" if is_extended else "оформлена"
    expiry_line = (
        "♾ <b>Срок доступа:</b> Навсегда"
        if subscription.expires_at is None
        else f"📅 <b>Доступ активен до:</b> {format_datetime(subscription.expires_at)}"
    )

    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=channel.tg_channel_id,
            member_limit=1,
            name=f"Sub #{subscription.id}"[:32],
        )
    except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError) as exc:
        logger.error(
            "Failed to create invite link for channel=%s (tg_id=%s) after payment %s: %s",
            channel.id,
            channel.tg_channel_id,
            payment.telegram_payment_charge_id,
            exc,
        )
        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b> Подписка на канал "
            f"<b>{escape_html(channel.title)}</b> {action_word}.\n"
            f"{expiry_line}\n\n"
            "⚠️ Однако бот не смог создать пригласительную ссылку (возможно, у него "
            "отозвали права администратора в канале). Пожалуйста, свяжитесь с владельцем "
            "канала и сообщите ID платежа:\n"
            f"<code>{escape_html(payment.telegram_payment_charge_id)}</code>",
            reply_markup=back_to_main_kb(),
        )
        return

    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"📢 Канал: <b>{escape_html(channel.title)}</b>\n"
        f"💳 Тариф: <b>{escape_html(tariff.name)}</b> ({payment.total_amount} ⭐)\n"
        f"📌 Статус: подписка {action_word}\n"
        f"{expiry_line}\n\n"
        "👇 Нажмите кнопку ниже, чтобы перейти в канал. "
        "<i>Ссылка одноразовая и действует только для одного входа.</i>",
        reply_markup=invite_link_kb(invite_link.invite_link),
    )
