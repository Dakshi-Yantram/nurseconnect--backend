"""One-off migration for the Nurse Safety Check feature.

This repo has no Alembic — new tables get created by create_tables.py via
Base.metadata.create_all(), but that call never ALTERs an existing table or
adds values to an existing Postgres enum type, so the new columns on
worker_alertness_checks and the two new visit_status enum values need to be
applied by hand once. Safe to re-run — every statement is idempotent
(IF NOT EXISTS / catches the "already exists" error for enum values).

Run once, after pulling this change and before deploying the new API code:

    python migrate_safety_check_v2.py
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

STATEMENTS = [
    # New Postgres enum values. ADD VALUE cannot run inside the same
    # transaction as a statement that *uses* the type, but each of these
    # is executed as its own implicit transaction below, so that's fine.
    "ALTER TYPE visit_status ADD VALUE IF NOT EXISTS 'en_route'",
    "ALTER TYPE visit_status ADD VALUE IF NOT EXISTS 'arrived'",
    # New enum type backing WorkerAlertnessCheck.tier.
    """
    DO $$ BEGIN
        CREATE TYPE alertness_tier AS ENUM ('pass', 'warning', 'fail');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$;
    """,
    # New columns on worker_alertness_checks.
    "ALTER TABLE worker_alertness_checks ADD COLUMN IF NOT EXISTS false_starts_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE worker_alertness_checks ADD COLUMN IF NOT EXISTS lapses_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE worker_alertness_checks ADD COLUMN IF NOT EXISTS tier alertness_tier DEFAULT 'pass'",
    "ALTER TABLE worker_alertness_checks ADD COLUMN IF NOT EXISTS declaration_confirmed BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE worker_alertness_checks ADD COLUMN IF NOT EXISTS declaration_confirmed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS ix_worker_alertness_checks_booking ON worker_alertness_checks (booking_id)",
    # Backfill tier for any pre-existing rows from before this migration,
    # so historical attempts aren't silently NULL.
    "UPDATE worker_alertness_checks SET tier = CASE WHEN passed THEN 'pass' ELSE 'fail' END WHERE tier IS NULL",
    # Prescription renewal-consultation fee (expired-Rx gate).
    "ALTER TABLE prescriptions ADD COLUMN IF NOT EXISTS renewal_consultation_paid BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE prescriptions ADD COLUMN IF NOT EXISTS renewal_consultation_order_id VARCHAR(100)",
    "ALTER TABLE prescriptions ADD COLUMN IF NOT EXISTS renewal_consultation_paid_at TIMESTAMPTZ",
]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)
    try:
        for stmt in STATEMENTS:
            try:
                await conn.execute(stmt)
                print(f"OK   {stmt.strip().splitlines()[0][:80]}")
            except asyncpg.DuplicateObjectError:
                print(f"SKIP (already exists) {stmt.strip().splitlines()[0][:80]}")
    finally:
        await conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
