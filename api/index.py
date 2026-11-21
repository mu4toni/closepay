from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, Update
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from pydantic import ValidationError

from bot.runtime import get_runtime, shutdown_runtime
from bot.services import check_subscriptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _secrets_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        runtime = get_runtime()
        await runtime.ensure_db_initialized()
    except Exception as exc:
        # Log and allow worker startup; endpoints will retry initialization on request.
        logger.warning("Deferred startup initialization due to error: %s", exc)
    yield
    await shutdown_runtime()


app = FastAPI(
    title="Paywall Telegram Bot API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
@app.get("/api/health")
async def healthcheck() -> dict[str, str]:
    """Lightweight liveness check endpoint."""
    return {"status": "ok", "service": "paywall-bot"}


@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    secret_header: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> dict[str, bool]:
    """Receive Telegram webhook updates and dispatch them into aiogram."""
    runtime = get_runtime()

    if not _secrets_match(secret_header, runtime.settings.WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Telegram webhook secret token.",
        )

    try:
        payload: Any = await request.json()
        update = Update.model_validate(payload, context={"bot": runtime.bot})
    except (ValueError, ValidationError) as exc:
        logger.warning("Rejected invalid Telegram update payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Telegram update payload.",
        ) from exc

    await runtime.ensure_db_initialized()

    try:
        await runtime.dp.feed_update(bot=runtime.bot, update=update)
    except Exception:
        logger.exception("Unhandled error while processing update_id=%s", update.update_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process Telegram update.",
        )

    return {"ok": True}


@app.get("/api/cron")
async def cron_check_subscriptions(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Vercel Cron endpoint: send 3-day reminders and remove expired subscribers."""
    runtime = get_runtime()
    bearer_token = _extract_bearer_token(authorization)

    if not _secrets_match(bearer_token, runtime.settings.CRON_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized cron invocation.",
        )

    await runtime.ensure_db_initialized()

    result = await check_subscriptions(
        bot=runtime.bot,
        session_maker=runtime.session_maker,
    )
    return {"ok": True, **result.to_dict()}


@app.get("/api/set_webhook")
async def set_telegram_webhook(
    secret: str | None = Query(default=None),
    authorization: str | None = Header(default=None, alias="Authorization"),
    secret_header: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> dict[str, Any]:
    """One-time setup endpoint to register Telegram webhook and initialize DB schema."""
    runtime = get_runtime()
    provided_secret = secret_header or _extract_bearer_token(authorization) or secret

    if not _secrets_match(provided_secret, runtime.settings.WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid setup secret.",
        )

    await runtime.ensure_db_initialized()

    allowed_updates = runtime.dp.resolve_used_update_types()
    webhook_url = runtime.settings.webhook_endpoint

    try:
        await runtime.bot.set_my_commands(
            [BotCommand(command="start", description="Главное меню")]
        )
        await runtime.bot.set_webhook(
            url=webhook_url,
            secret_token=runtime.settings.WEBHOOK_SECRET,
            allowed_updates=allowed_updates,
            drop_pending_updates=False,
        )
        webhook_info = await runtime.bot.get_webhook_info()
    except TelegramAPIError as exc:
        logger.error("Failed to configure Telegram webhook (%s): %s", webhook_url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram API error: {exc}",
        ) from exc

    logger.info("Registered Telegram webhook at %s", webhook_info.url)
    return {
        "ok": True,
        "webhook_url": webhook_info.url,
        "pending_update_count": webhook_info.pending_update_count,
        "allowed_updates": allowed_updates,
    }
