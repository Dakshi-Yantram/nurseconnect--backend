"""Fixes production error:

    asyncpg.exceptions.UndefinedColumnError:
    column worker_payouts.approval_status does not exist

Adds ONLY the payout admin-approval gate to `worker_payouts`:
  - new enum: payout_approval_status (pending_approval, approved, rejected)
  - worker_payouts.approval_status (NOT NULL, default pending_approval)
  - worker_payouts.approved_by (UUID, nullable)
  - worker_payouts.approved_at (TIMESTAMPTZ, nullable)
  - worker_payouts.approval_rejection_reason (TEXT, nullable)
  - index on worker_payouts.approval_status

This is a deliberately narrow subset of add_eprescription_and_payout_approval_schema.py
(which also touches worker_profiles, prescriptions, tele_consultations, and
worker_agreements). Do NOT run that file to fix this issue — it contains
unrelated changes. This script only creates the payout_approval_status enum
and the four worker_payouts columns/index above.

Only needed for a database that already existed before this change — a
brand-new database gets this automatically from create_tables.py (which
builds every table fresh from the current SQLAlchemy models).

Safe to re-run: every statement is idempotent (IF NOT EXISTS / duplicate_object
guard). Does not modify any existing row's data beyond populating the new
approval_status column with its default for pre-existing rows.
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)

    # --- 1. Enum: payout_approval_status ---------------------------------
    await conn.execute("""
        DO $$ BEGIN
            CREATE TYPE payout_approval_status AS ENUM
                ('pending_approval', 'approved', 'rejected');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("payout_approval_status enum ready")

    # --- 2. worker_payouts columns ----------------------------------------
    await conn.execute("""
        ALTER TABLE worker_payouts
        ADD COLUMN IF NOT EXISTS approval_status payout_approval_status NOT NULL DEFAULT 'pending_approval',
        ADD COLUMN IF NOT EXISTS approved_by UUID NULL REFERENCES users(id),
        ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS approval_rejection_reason TEXT NULL;
    """)
    print("worker_payouts approval columns ready")

    # --- 3. Index on approval_status ---------------------------------------
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_worker_payouts_approval_status
        ON worker_payouts (approval_status);
    """)
    print("ix_worker_payouts_approval_status index ready")

    await conn.close()
    print("\nDone. worker_payouts.approval_status now exists with default "
          "'pending_approval' for all existing rows; approved_by/approved_at/"
          "approval_rejection_reason are NULL until an admin acts on a payout.")


if __name__ == "__main__":
    asyncio.run(main())
