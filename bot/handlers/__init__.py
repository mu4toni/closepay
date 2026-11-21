from aiogram import Dispatcher

from bot.handlers import admin_channel, subscriber


def register_routers(dp: Dispatcher) -> None:
    """Register all feature routers in deterministic priority order.

    ``subscriber.router`` is registered before ``admin_channel.router`` so that
    deep-linked ``/start ch_<id>`` commands are intercepted before plain ``/start``.
    """
    dp.include_router(subscriber.router)
    dp.include_router(admin_channel.router)
