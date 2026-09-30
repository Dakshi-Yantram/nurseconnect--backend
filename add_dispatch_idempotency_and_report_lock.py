"""Booking->Nurse->Visit hardening migration.

Adds:
  * bookings.dispatch_cycle, bookings.no_worker_alerted_cycle
  * booking_dispatch_notifications  (UNIQUE booking_id, worker_id, cycle)
      -> a nurse can be pushed a given booking at most once per dispatch cycle
  * visit_records.report_finalized_at / report_finalized_by / report_content_hash
  * Backfill: every already-completed visit is stamped finalized (content hash
    left NULL for legacy rows: it was never computed at the time).
  * Postgres triggers that make a finalized report immutable AT THE DATABASE
    (defence in depth behind app.services.report_lock):
      - visit_records: care_notes, family_summary, checklist_responses,
        documentation_responses, documentation_complete and the finalization
        columns cannot change once report_finalized_at is set
        (rating_*, payout/insurance/etc. columns remain writable).
      - vital_sign_readings / visit_checklist_responses /
        visit_documentation_items: no INSERT/UPDATE/DELETE against a finalized
        visit.

Safe to re-run (IF NOT EXISTS / CREATE OR REPLACE / idempotent backfill).
A brand-new database gets the columns/table from create_tables.py; the
triggers and backfill are only created here, so run it on every environment.

    python add_dispatch_idempotency_and_report_lock.py
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

DDL = [
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dispatch_cycle INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_worker_alerted_cycle INTEGER NULL",
    """
    CREATE TABLE IF NOT EXISTS booking_dispatch_notifications (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
        cycle INTEGER NOT NULL DEFAULT 1,
        wave INTEGER NOT NULL DEFAULT 1,
        notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ux_dispatch_notif_booking_worker_cycle UNIQUE (booking_id, worker_id, cycle)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_dispatch_notif_booking ON booking_dispatch_notifications (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_dispatch_notif_worker ON booking_dispatch_notifications (worker_id)",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_at TIMESTAMPTZ NULL",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_by UUID NULL REFERENCES users(id)",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_content_hash VARCHAR(64) NULL",
    # One booking has at most one open dispatch claim; also speeds the open-booking scan.
    "CREATE INDEX IF NOT EXISTS ix_bookings_open_dispatch ON bookings (status, scheduled_date) WHERE worker_id IS NULL",
]

BACKFILL = """
UPDATE visit_records
   SET report_finalized_at = COALESCE(check_out_at, updated_at, now())
 WHERE report_finalized_at IS NULL
   AND (check_out_at IS NOT NULL OR status = 'completed')
"""

TRIGGERS = [
    """
    CREATE OR REPLACE FUNCTION nc_guard_finalized_visit_record() RETURNS trigger AS $$
    BEGIN
        IF OLD.report_finalized_at IS NOT NULL AND (
               NEW.care_notes IS DISTINCT FROM OLD.care_notes
            OR NEW.family_summary IS DISTINCT FROM OLD.family_summary
            OR NEW.checklist_responses IS DISTINCT FROM OLD.checklist_responses
            OR NEW.documentation_responses IS DISTINCT FROM OLD.documentation_responses
            OR NEW.documentation_complete IS DISTINCT FROM OLD.documentation_complete
            OR NEW.report_finalized_at IS DISTINCT FROM OLD.report_finalized_at
            OR NEW.report_finalized_by IS DISTINCT FROM OLD.report_finalized_by
            OR (OLD.report_content_hash IS NOT NULL
                AND NEW.report_content_hash IS DISTINCT FROM OLD.report_content_hash)
        ) THEN
            RAISE EXCEPTION 'REPORT_FINALIZED: visit_records % is finalized and immutable', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_guard_finalized_visit_record ON visit_records",
    """
    CREATE TRIGGER trg_guard_finalized_visit_record
        BEFORE UPDATE ON visit_records
        FOR EACH ROW EXECUTE FUNCTION nc_guard_finalized_visit_record()
    """,
    """
    CREATE OR REPLACE FUNCTION nc_guard_finalized_visit_child() RETURNS trigger AS $$
    DECLARE
        vid UUID;
        fin TIMESTAMPTZ;
    BEGIN
        IF TG_OP = 'DELETE' THEN vid := OLD.visit_record_id; ELSE vid := NEW.visit_record_id; END IF;
        IF vid IS NULL THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END IF;
        SELECT report_finalized_at INTO fin FROM visit_records WHERE id = vid;
        IF fin IS NOT NULL THEN
            RAISE EXCEPTION 'REPORT_FINALIZED: % on finalized visit % is not allowed', TG_TABLE_NAME, vid
                USING ERRCODE = 'check_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END;
    $$ LANGUAGE plpgsql
    """,
]
for _t in ("vital_sign_readings", "visit_checklist_responses", "visit_documentation_items"):
    TRIGGERS += [
        f"DROP TRIGGER IF EXISTS trg_guard_finalized_{_t} ON {_t}",
        f"""
        CREATE TRIGGER trg_guard_finalized_{_t}
            BEFORE INSERT OR UPDATE OR DELETE ON {_t}
            FOR EACH ROW EXECUTE FUNCTION nc_guard_finalized_visit_child()
        """,
    ]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            for stmt in DDL:
                await conn.execute(stmt)
            print("columns / table / indexes ready")
            res = await conn.execute(BACKFILL)
            print(f"backfilled finalized visits: {res}")
            for stmt in TRIGGERS:
                await conn.execute(stmt)
            print("immutability triggers installed")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
