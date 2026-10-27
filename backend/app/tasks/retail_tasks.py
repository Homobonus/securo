import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.app_clock import get_workspace_timezone, use_resolved_timezone
from app.core.config import get_settings
from app.models.receipt import RetailConnection
from app.services import retail_connection_service
from app.worker import celery_app

logger = logging.getLogger(__name__)

# Receipts are not urgent, and the stores are private APIs that notice a
# chatty client. A connection still importing its history is the
# exception: it gets a batch every hourly run until it has caught up.
STALE_THRESHOLD = timedelta(hours=6)
BACKFILL_BATCH = 300


async def _sync_all() -> int:
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    synced = 0
    try:
        cutoff = datetime.now(timezone.utc) - STALE_THRESHOLD
        async with session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        RetailConnection.id,
                        RetailConnection.last_sync_at,
                        RetailConnection.settings,
                    ).where(RetailConnection.status.in_(["active", "error"]))
                )
            ).all()
        ids = [
            connection_id
            for connection_id, last_sync_at, settings in rows
            if last_sync_at is None
            or last_sync_at < cutoff
            or not (settings or {}).get("backfill_complete")
        ]
        for connection_id in ids:
            try:
                async with session_maker() as session:
                    connection = await session.get(RetailConnection, connection_id)
                    if connection is None:
                        continue
                    tz = await get_workspace_timezone(session, connection.workspace_id)
                    with use_resolved_timezone(tz):
                        result = await retail_connection_service.sync_connection(
                            session, connection, max_receipts=BACKFILL_BATCH, time_budget=None
                        )
                    logger.info(
                        "Retail sync %s: %d new, %d matched",
                        connection_id,
                        result.created,
                        result.matched,
                    )
                    synced += 1
            except Exception:
                logger.exception("Retail sync failed for connection %s", connection_id)
        return synced
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.retail_tasks.sync_all_retail_connections")
def sync_all_retail_connections() -> dict:
    return {"synced": asyncio.run(_sync_all())}
