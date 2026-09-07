"""Adds soft-delete columns to care_packages for an existing database.

Only needed for a database that already existed before this change — a
brand-new database gets all of this automatically from create_tables.py
(which builds every table fresh from the current SQLAlchemy models).

Safe to re-run: every statement is idempotent (ADD COLUMN IF NOT EXISTS).

Distinction this enables:
  - is_active=False  -> package still exists, shown greyed-out / read-only
  - is_deleted=True  -> package hidden from every list entirely (never
    hard-deleted, since existing CarePackageBooking rows reference it)
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
        ALTER TABLE care_packages
            ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_care_packages_is_deleted ON care_packages(is_deleted);
    """)
    print("care_packages soft-delete columns ready: is_deleted, deleted_at, deleted_by")

    await conn.close()
    print("Done.")


asyncio.run(main())
