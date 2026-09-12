import asyncio
from sqlalchemy import text
from app.core.database import engine, Base
from app.models import *  # saare models import karne ke liye

async def create_all():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Base.metadata.create_all only creates tables that don't exist yet —
    # it never diffs/alters an existing table's columns. worker_profiles
    # already exists on any DB from before signature_url was added to the
    # WorkerProfile model, so create_all silently leaves that column
    # missing and every worker-profile SELECT (e.g. worker login) 500s.
    # Same idempotent ALTER TABLE ... ADD COLUMN IF NOT EXISTS pattern
    # already used for bookings.dispatch_started_at in app/seed.py's
    # _run_pending_column_migrations — safe to re-run on every invocation.
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE worker_profiles ADD COLUMN IF NOT EXISTS signature_url TEXT NULL"
        ))

    print("Tables created successfully!")

asyncio.run(create_all())