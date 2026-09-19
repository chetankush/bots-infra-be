from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=False,
    # Hosted Postgres usually sits behind PgBouncer (Supabase, Neon, RDS Proxy). In
    # transaction-pooling mode a server-side prepared statement can be routed to a
    # different backend on the next query and fail with "prepared statement does not
    # exist". Disabling asyncpg's statement cache makes the app safe behind any pooler
    # at a small per-query cost; a direct or session-mode connection is unaffected.
    connect_args={"statement_cache_size": 0},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
