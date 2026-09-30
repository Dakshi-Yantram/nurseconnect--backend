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

    # Additive columns for dispatch idempotency + report finalization (see
    # add_dispatch_idempotency_and_report_lock.py). Same reasoning as above:
    # create_all never alters existing tables.
    async with engine.begin() as conn:
        for stmt in (
            "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dispatch_cycle INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_worker_alerted_cycle INTEGER NULL",
            "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_at TIMESTAMPTZ NULL",
            "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_by UUID NULL REFERENCES users(id)",
            "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_content_hash VARCHAR(64) NULL",
        ):
            await conn.execute(text(stmt))

    print("Tables created successfully!")

asyncio.run(create_all())