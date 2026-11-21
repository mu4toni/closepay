from __future__ import annotations

import json
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database.models import FSMState


class PostgresFSMStorage(BaseStorage):
    """PostgreSQL-backed aiogram FSM storage for stateless Serverless deployments."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @staticmethod
    def _build_key(key: StorageKey) -> str:
        business_id = getattr(key, "business_connection_id", None) or ""
        return (
            f"{key.bot_id}:{key.chat_id}:{key.user_id}:"
            f"{key.thread_id or 0}:{business_id}:{key.destiny}"
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        storage_key = self._build_key(key)
        state_value = state.state if isinstance(state, State) else state

        stmt = (
            pg_insert(FSMState)
            .values(key=storage_key, state=state_value, data="{}")
            .on_conflict_do_update(
                index_elements=[FSMState.key],
                set_={"state": state_value},
            )
        )
        async with self._session_maker() as session:
            await session.execute(stmt)
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        storage_key = self._build_key(key)
        stmt = select(FSMState.state).where(FSMState.key == storage_key)
        async with self._session_maker() as session:
            return await session.scalar(stmt)

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        storage_key = self._build_key(key)
        serialized = json.dumps(dict(data), ensure_ascii=False)

        stmt = (
            pg_insert(FSMState)
            .values(key=storage_key, state=None, data=serialized)
            .on_conflict_do_update(
                index_elements=[FSMState.key],
                set_={"data": serialized},
            )
        )
        async with self._session_maker() as session:
            await session.execute(stmt)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        storage_key = self._build_key(key)
        stmt = select(FSMState.data).where(FSMState.key == storage_key)
        async with self._session_maker() as session:
            raw = await session.scalar(stmt)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def close(self) -> None:
        # Engine lifecycle is managed by BotRuntime.
        return None
