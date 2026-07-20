from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import get_settings
from app.db.models import Base
settings=get_settings()
engine=create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal=async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
