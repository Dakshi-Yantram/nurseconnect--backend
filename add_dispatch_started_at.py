"""Adds bookings.dispatch_started_at — the moment a booking became visible
to workers (payment captured → status confirmed). The radius-wave dispatch
clock now runs from this timestamp instead of created_at, so time spent on
the payment screen no longer burns the wave window.

Backfills existing confirmed/dispatched rows with created_at so their wave
math stays unchanged (they're historical anyway).

Only needed for a database that already existed before this change — a
brand-new database gets the column automatically from create_tables.py.

Safe to re-run: idempotent (IF NOT EXISTS + backfill only touches NULLs).
"""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)

    await conn.execute("""
        ALTER TABLE bookings
        ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ NULL
    """)
    print("bookings.dispatch_started_at added")

    updated = await conn.execute("""
        UPDATE bookings
        SET dispatch_started_at = created_at
        WHERE dispatch_started_at IS NULL
          AND status NOT IN ('draft', 'pending_payment')
    """)
    print(f"backfilled: {updated}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
