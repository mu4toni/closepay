from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.config import Settings, load_settings
from bot.database import (
    PostgresFSMStorage,
    close_db,
    create_db_engine,
    create_session_maker,
    init_db,
)
from bot.handlers import register_routers
from bot.middlewares import DbSessionMiddleware

logger = logging.getLogger(__name__)


class BotRuntime:
    """Serverless-friendly container holding Bot, Dispatcher, and async DB resources."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_db_engine(settings.DATABASE_URL)
        self.session_maker: async_sessionmaker[AsyncSession] = create_session_maker(
            self.engine
        )
        self.bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher(storage=PostgresFSMStorage(self.session_maker))
        self.dp.update.middleware(DbSessionMiddleware(self.session_maker))
        register_routers(self.dp)

        self._db_initialized = False
        self._init_lock = asyncio.Lock()

    async def ensure_db_initialized(self) -> None:
        """Create tables once per warm Serverless instance (idempotent)."""
        if self._db_initialized:
            return
        async with self._init_lock:
            if self._db_initialized:
                return
            await init_db(self.engine)
            self._db_initialized = True
            logger.info("Database schema verified/initialized.")

    async def close(self) -> None:
        """Release HTTP and database resources on worker shutdown."""
        await self.bot.session.close()
        await close_db(self.engine)
        self._db_initialized = False


_runtime: BotRuntime | None = None


def get_runtime() -> BotRuntime:
    """Return process-wide singleton BotRuntime (reused across warm Vercel invocations)."""
    global _runtime
    if _runtime is None:
        settings = load_settings()
        _runtime = BotRuntime(settings)
    return _runtime


async def shutdown_runtime() -> None:
    """Close and reset the singleton BotRuntime if initialized."""
    global _runtime
    if _runtime is not None:
        await _runtime.close()
        _runtime = None
