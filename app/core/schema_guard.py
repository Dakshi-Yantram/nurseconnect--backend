"""Additive-schema safety net, run at every app start.

WHY: the API is deployed by Elastic Beanstalk (Procfile + buildspec), which
never runs deploy.sh or any add_*_schema.py script, and in production the seed
(which used to carry these ALTERs) does not run at startup. New ORM columns
therefore reach a database that does not have them, and every query touching
that table then fails with a 500 - the same failure the repo's own
create_tables.py comment describes for worker login.

This module makes those columns exist BEFORE traffic is served. Design rules:
  * strictly additive and idempotent (IF NOT EXISTS) - never drops or rewrites;
  * NEVER raises: a failure is logged and the app still boots, so this can not
    take login or anything else down;
  * bounded: lock_timeout / statement_timeout so a busy table cannot stall
    startup, and a Postgres advisory lock so two workers starting together
    do not deadlock on the same ALTER;
  * the report-lock TRIGGERS are NOT installed here (they are defence in depth,
    installed by add_dispatch_idempotency_and_report_lock.py); the application
    enforces the lock on its own.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

_LOCK_KEY = "nurseconnect:schema_guard:v1"

STATEMENTS = (
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dispatch_cycle INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_worker_alerted_cycle INTEGER NULL",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_at TIMESTAMPTZ NULL",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_finalized_by UUID NULL REFERENCES users(id)",
    "ALTER TABLE visit_records ADD COLUMN IF NOT EXISTS report_content_hash VARCHAR(64) NULL",
    """CREATE TABLE IF NOT EXISTS booking_dispatch_notifications (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
        cycle INTEGER NOT NULL DEFAULT 1,
        wave INTEGER NOT NULL DEFAULT 1,
        notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ux_dispatch_notif_booking_worker_cycle UNIQUE (booking_id, worker_id, cycle)
    )""",
)


_EXPECTED_COLUMNS = {
    ("bookings", "dispatch_cycle"),
    ("bookings", "no_worker_alerted_cycle"),
    ("visit_records", "report_finalized_at"),
    ("visit_records", "report_finalized_by"),
    ("visit_records", "report_content_hash"),
}

_CHECK_COLUMNS_SQL = """
    SELECT table_name, column_name FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND ((table_name = 'bookings' AND column_name IN ('dispatch_cycle', 'no_worker_alerted_cycle'))
         OR (table_name = 'visit_records' AND column_name IN
             ('report_finalized_at', 'report_finalized_by', 'report_content_hash')))
"""
_CHECK_TABLE_SQL = "SELECT to_regclass('booking_dispatch_notifications')"


async def _schema_is_complete(engine) -> bool:
    """Read-only check (no DDL, so it takes no table locks)."""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(_CHECK_COLUMNS_SQL))).fetchall()
        present = {(r[0], r[1]) for r in rows}
        table = (await conn.execute(text(_CHECK_TABLE_SQL))).scalar()
    return present >= _EXPECTED_COLUMNS and table is not None


async def ensure_additive_schema(engine) -> bool:
    """Make sure the additive schema exists. Returns True when it does (or was
    just created), False if anything failed. Never raises.

    The normal case (everything already there) runs only the read-only check -
    no ALTER TABLE, so no lock on bookings/visit_records at every restart.
    """
    try:
        if await _schema_is_complete(engine):
            logger.info("schema_guard: schema already complete")
            return True
        async with engine.begin() as conn:
            # SET LOCAL: scoped to this transaction, nothing leaks to the pool.
            await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            await conn.execute(text("SET LOCAL statement_timeout = '20s'"))
            # Only one worker migrates at a time; the others wait, then no-op.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": _LOCK_KEY},
            )
            for stmt in STATEMENTS:
                await conn.execute(text(stmt))
        logger.info("schema_guard: additive columns/table created")
        return True
    except Exception:  # noqa: BLE001 - must never block startup
        logger.exception(
            "schema_guard: could not ensure additive schema; continuing without it. "
            "Run add_dispatch_idempotency_and_report_lock.py manually."
        )
        return False
