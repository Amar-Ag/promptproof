"""Request-scoped dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from promptproof_core.db import get_sessionmaker
from promptproof_core.settings import Settings, get_settings
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request, committed on success, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
