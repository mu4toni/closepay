from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    """Top-level navigation and FSM cancellation callbacks."""

    action: str


class OwnerChannelCB(CallbackData, prefix="och"):
    """Channel management actions for channel owners."""

    action: str
    channel_id: int


class OwnerTariffCB(CallbackData, prefix="otf"):
    """Tariff management actions for channel owners."""

    action: str
    channel_id: int
    tariff_id: int


class TariffDurationCB(CallbackData, prefix="tdur"):
    """Tariff duration selection during tariff creation FSM."""

    days: int


class SubscriberChannelCB(CallbackData, prefix="sch"):
    """Open subscriber paywall/tariff list for a specific channel."""

    channel_id: int


class BuyTariffCB(CallbackData, prefix="buy"):
    """Trigger Telegram Stars invoice for a specific tariff."""

    channel_id: int
    tariff_id: int
