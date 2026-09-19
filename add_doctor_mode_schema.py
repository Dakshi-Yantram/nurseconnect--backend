"""Adds the Tele-Doctor / Physical Doctor split to an existing database.

  - New WorkerType values: tele_doctor, physical_doctor

That is the entire schema change. The split is modelled as two additional
Provider Types rather than a new "mode" column, because every mechanism that
needs to tell the two apart is already keyed on Provider Type:

  - required/optional documents      (app/core/provider_types.py)
  - package + service eligibility    (allowed_provider_types, already present)
  - the qualification gate           (app/services/qualification.py)
  - training module targeting        (allowed_provider_types on modules)
  - onboarding form composition      (driven by required_docs())

So no new tables, no new columns, and no parallel branching to keep in sync.

Only needed for a database that already existed before this change — a
brand-new database gets this automatically from create_tables.py, which
builds every table fresh from the current SQLAlchemy models.

Safe to re-run: ADD VALUE IF NOT EXISTS is idempotent.

NO EXISTING ROW IS MODIFIED. Every current doctor keeps worker_type =
'doctor', and 'doctor' remains both tele-capable and physical-capable (see
TELE_CAPABLE_PROVIDER_TYPES / PHYSICAL_CAPABLE_PROVIDER_TYPES), so no
provider who can run a tele-consultation today loses that ability. Moving
an existing doctor onto a specific mode is a deliberate admin action, not
something this migration does behind your back.

Usage:
    python add_doctor_mode_schema.py
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

NEW_WORKER_TYPES = ("tele_doctor", "physical_doctor")


async def main():
    dsn = (
        DATABASE_URL
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("?ssl=require", "?sslmode=require")
    )
    conn = await asyncpg.connect(dsn)

    try:
        # Postgres requires each ADD VALUE to run outside a transaction block
        # and refuses to combine it with other DDL in the same statement.
        for value in NEW_WORKER_TYPES:
            await conn.execute(
                f"ALTER TYPE worker_type_enum ADD VALUE IF NOT EXISTS '{value}';"
            )
        print("worker_type_enum extended: " + ", ".join(NEW_WORKER_TYPES))

        # Report what is actually on the type now, so a re-run is verifiable
        # rather than just silent.
        rows = await conn.fetch(
            """
            SELECT e.enumlabel
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = 'worker_type_enum'
            ORDER BY e.enumsortorder;
            """
        )
        print("worker_type_enum now contains: " + ", ".join(r["enumlabel"] for r in rows))

        counts = await conn.fetch(
            "SELECT worker_type, COUNT(*) AS n FROM worker_profiles GROUP BY worker_type ORDER BY worker_type;"
        )
        print("\nExisting provider counts (unchanged by this migration):")
        for r in counts:
            print(f"  {r['worker_type']}: {r['n']}")
    finally:
        await conn.close()

    print(
        "\nDone. No rows were modified. Existing doctors remain worker_type "
        "'doctor' and stay capable of BOTH tele and physical work, so nothing "
        "that functions today stops functioning.\n"
        "\nNext steps (deliberate admin actions, not automatic):\n"
        "  1. Set allowed_provider_types on the Tele-Doctor and Physical "
        "Doctor packages so each mode surfaces its own packages.\n"
        "  2. Re-type individual doctors to 'tele_doctor' or 'physical_doctor' "
        "as you decide which mode each one works in."
    )


if __name__ == "__main__":
    asyncio.run(main())
