"""Adds two new tables:

- worker_availability_slots — the nurse's declared weekly working hours
  (e.g. "Mon-Fri 9am-5pm"), separate from the existing live online/offline
  toggle on worker_profiles.availability.
- worker_alertness_checks — a log of every pre-navigation reaction-time
  "green button" check a nurse completes before opening Google Maps for an
  accepted booking, used to spot patterns of fatigue.

Only needed for a database that already existed before this change — a
brand-new database gets both tables automatically from create_tables.py.

Safe to re-run: every statement uses IF NOT EXISTS.
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dsn = (
        DATABASE_URL
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("?ssl=require", "?sslmode=require")
    )
    conn = await asyncpg.connect(dsn)

    try:
        # ============================================================
        # 1. worker_availability_slots
        # ============================================================
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_availability_slots (
                id UUID PRIMARY KEY,
                worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
                day_of_week SMALLINT NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT true,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_availability_slots_worker_day
            ON worker_availability_slots (worker_id, day_of_week)
        """)
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE worker_availability_slots
                ADD CONSTRAINT ck_availability_slot_day_of_week
                CHECK (day_of_week BETWEEN 0 AND 6);
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE worker_availability_slots
                ADD CONSTRAINT ck_availability_slot_end_after_start
                CHECK (end_time > start_time);
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """)
        print("✅ worker_availability_slots ready")

        # ============================================================
        # 2. worker_alertness_checks
        # ============================================================
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_alertness_checks (
                id UUID PRIMARY KEY,
                worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
                booking_id UUID NULL REFERENCES bookings(id) ON DELETE SET NULL,
                round_reaction_times_ms INTEGER[],
                average_reaction_time_ms INTEGER,
                missed_taps INTEGER NOT NULL DEFAULT 0,
                passed BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_alertness_checks_worker_created
            ON worker_alertness_checks (worker_id, created_at)
        """)
        print("✅ worker_alertness_checks ready")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
