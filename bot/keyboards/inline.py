from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database.models import Channel, Subscription, Tariff
from bot.keyboards.callbacks import (
    BuyTariffCB,
    MenuCB,
    OwnerChannelCB,
    OwnerTariffCB,
    SubscriberChannelCB,
    TariffDurationCB,
)
from bot.utils.formatting import format_duration


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Подключить канал",
        callback_data=MenuCB(action="connect_channel"),
    )
    builder.button(
        text="📢 Мои каналы",
        callback_data=MenuCB(action="my_channels"),
    )
    builder.button(
        text="🎫 Мои подписки",
        callback_data=MenuCB(action="my_subscriptions"),
    )
    builder.adjust(1)
    return builder.as_markup()


def back_to_main_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 Главное меню", callback_data=MenuCB(action="main"))
    return builder.as_markup()


def cancel_fsm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=MenuCB(action="cancel"))
    return builder.as_markup()


def channel_connected_kb(channel_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Создать первый тариф",
        callback_data=OwnerChannelCB(action="add_tariff", channel_id=channel_id),
    )
    builder.button(
        text="📢 Открыть карточку канала",
        callback_data=OwnerChannelCB(action="view", channel_id=channel_id),
    )
    builder.button(text="🏠 Главное меню", callback_data=MenuCB(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def my_channels_kb(channels: list[Channel]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        status_icon = "🟢" if channel.is_active else "⏸"
        builder.button(
            text=f"{status_icon} {channel.title[:48]}",
            callback_data=OwnerChannelCB(action="view", channel_id=channel.id),
        )
    builder.button(
        text="➕ Подключить канал",
        callback_data=MenuCB(action="connect_channel"),
    )
    builder.button(text="🏠 Главное меню", callback_data=MenuCB(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def channel_card_kb(channel: Channel, tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Добавить тариф",
                callback_data=OwnerChannelCB(
                    action="add_tariff",
                    channel_id=channel.id,
                ).pack(),
            )
        ]
    ]

    for tariff in tariffs:
        status_icon = "✅" if tariff.is_active else "⏸"
        duration = format_duration(tariff.duration_days)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon} {tariff.name[:24]} ({duration}) — {tariff.price_stars} ⭐",
                    callback_data=OwnerTariffCB(
                        action="toggle",
                        channel_id=channel.id,
                        tariff_id=tariff.id,
                    ).pack(),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data=OwnerChannelCB(
                    action="stats",
                    channel_id=channel.id,
                ).pack(),
            ),
            InlineKeyboardButton(
                text="✏️ Описание",
                callback_data=OwnerChannelCB(
                    action="edit_desc",
                    channel_id=channel.id,
                ).pack(),
            ),
        ]
    )

    toggle_label = "⏸ Приостановить продажи" if channel.is_active else "▶️ Возобновить продажи"
    rows.append(
        [
            InlineKeyboardButton(
                text=toggle_label,
                callback_data=OwnerChannelCB(
                    action="toggle_active",
                    channel_id=channel.id,
                ).pack(),
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Мои каналы",
                callback_data=MenuCB(action="my_channels").pack(),
            ),
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data=MenuCB(action="main").pack(),
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_stats_kb(channel_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔄 Обновить",
        callback_data=OwnerChannelCB(action="stats", channel_id=channel_id),
    )
    builder.button(
        text="⬅️ Назад к каналу",
        callback_data=OwnerChannelCB(action="view", channel_id=channel_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def tariff_duration_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="7 дней", callback_data=TariffDurationCB(days=7))
    builder.button(text="30 дней", callback_data=TariffDurationCB(days=30))
    builder.button(text="90 дней", callback_data=TariffDurationCB(days=90))
    builder.button(text="♾ Навсегда", callback_data=TariffDurationCB(days=0))
    builder.button(text="❌ Отмена", callback_data=MenuCB(action="cancel"))
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def subscriber_tariffs_kb(channel_id: int, tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tariff in tariffs:
        duration = format_duration(tariff.duration_days)
        builder.button(
            text=f"⭐ {tariff.name[:26]} ({duration}) — {tariff.price_stars} XTR",
            callback_data=BuyTariffCB(channel_id=channel_id, tariff_id=tariff.id),
        )
    builder.adjust(1)
    return builder.as_markup()


def invoice_pay_kb(price_stars: int, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ Оплатить {price_stars} XTR",
                    pay=True,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К выбору тарифов",
                    callback_data=SubscriberChannelCB(channel_id=channel_id).pack(),
                )
            ],
        ]
    )


def invite_link_kb(invite_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Перейти в канал", url=invite_url)],
            [
                InlineKeyboardButton(
                    text="🎫 Мои подписки",
                    callback_data=MenuCB(action="my_subscriptions").pack(),
                )
            ],
        ]
    )


def renew_subscription_kb(
    channel_id: int,
    *,
    button_text: str = "🔄 Продлить подписку",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=button_text,
        callback_data=SubscriberChannelCB(channel_id=channel_id),
    )
    return builder.as_markup()


def my_subscriptions_kb(subscriptions: list[Subscription]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    seen_channels: set[int] = set()
    for sub in subscriptions:
        if sub.channel is None or sub.channel_id in seen_channels or not sub.channel.is_active:
            continue
        seen_channels.add(sub.channel_id)
        builder.button(
            text=f"🔄 {sub.channel.title[:42]}",
            callback_data=SubscriberChannelCB(channel_id=sub.channel_id),
        )
    builder.button(text="🏠 Главное меню", callback_data=MenuCB(action="main"))
    builder.adjust(1)
    return builder.as_markup()
