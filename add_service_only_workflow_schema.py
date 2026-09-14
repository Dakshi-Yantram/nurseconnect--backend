"""Workflow 2 — Customer Books WITHOUT Material (Service-Only).

Adds the columns the service-only flow needs on top of the Workflow 1
(composite care) schema:

  bookings.patient_supply_confirmation  — the booking-time supply guardrail
                                          the patient ticked (also the flag
                                          that marks a booking as Workflow 2)
  bookings.patient_supply_photo_url     — photo of the supplies next to the Rx
  visit_records.supply_issue_reported   — nurse found a problem on arrival
  visit_records.supply_issue_details    — {issue_type, notes, reported_at}

Only needed for a database that already existed before this change — a
brand-new database gets these automatically from create_tables.py.

Safe to re-run: idempotent (ADD COLUMN IF NOT EXISTS).
"""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

STATEMENTS = [
    """
    ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS patient_supply_confirmation JSONB NULL
    """,
    """
    ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS patient_supply_photo_url TEXT NULL
    """,
    """
    ALTER TABLE visit_records
    ADD COLUMN IF NOT EXISTS supply_issue_reported BOOLEAN NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE visit_records
    ADD COLUMN IF NOT EXISTS supply_issue_details JSONB NULL
    """,
]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)

    for stmt in STATEMENTS:
        await conn.execute(stmt)
        print(f"ok: {' '.join(stmt.split())[:80]}")

    await conn.close()
    print("Workflow 2 schema ready")


if __name__ == "__main__":
    asyncio.run(main())
