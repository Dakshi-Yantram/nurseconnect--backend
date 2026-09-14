"""Adds Cash-on-delivery payment support to an existing database.

  - New PaymentStatus value: cash_due
  - New enum: payment_method (razorpay, cash)
  - bookings.payment_method              NOT NULL DEFAULT 'razorpay'
  - bookings.cash_collected_at/_by/_amount
  - bookings.cash_remitted_at

Only needed for a database that already existed before this change — a
brand-new database gets all of it from create_tables.py.

Safe to re-run: every statement is idempotent.

Existing rows are not semantically changed. payment_method defaults to
'razorpay', which is what every booking taken so far actually was, so
reporting over historical data stays correct without a backfill.

Why `cash_due` is its own status rather than reusing `pending`:
a cash booking IS confirmed and dispatchable, it simply has not been paid
yet. Reusing `pending` would make "awaiting payment choice" and "arranged,
money due at the visit" indistinguishable, and reusing `captured` would
book revenue that has not been received.

Usage:
    python add_cash_payment_schema.py
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
        # ADD VALUE must run outside a transaction block and alone.
        await conn.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'cash_due';")
        print("payment_status extended: cash_due")

        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE payment_method AS ENUM ('razorpay', 'cash');
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """)
        print("Enum type ready: payment_method")

        await conn.execute("""
            ALTER TABLE bookings
                ADD COLUMN IF NOT EXISTS payment_method payment_method
                    NOT NULL DEFAULT 'razorpay';
        """)
        await conn.execute("""
            ALTER TABLE bookings
                ADD COLUMN IF NOT EXISTS cash_collected_at   TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS cash_collected_by   UUID REFERENCES worker_profiles(id),
                ADD COLUMN IF NOT EXISTS cash_collected_amount NUMERIC(10, 2),
                ADD COLUMN IF NOT EXISTS cash_remitted_at    TIMESTAMPTZ;
        """)
        print("bookings: payment_method + cash collection/remittance columns ready")

        # Supports the provider's "cash I still owe the company" query and
        # the payout deduction that nets it off.
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_bookings_cash_outstanding
                ON bookings (cash_collected_by)
                WHERE cash_collected_at IS NOT NULL AND cash_remitted_at IS NULL;
        """)
        print("Index ready: ix_bookings_cash_outstanding")

        # Cash recovered by netting off a payout (see
        # payout_service.apply_cash_recovery).
        await conn.execute("""
            ALTER TABLE worker_payouts
                ADD COLUMN IF NOT EXISTS cash_recovered NUMERIC(10, 2) NOT NULL DEFAULT 0;
        """)
        print("worker_payouts.cash_recovered ready")

        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM bookings WHERE payment_method = 'razorpay';"
        )
        print(f"\nExisting bookings tagged payment_method='razorpay': {row['n']}")
    finally:
        await conn.close()

    print(
        "\nDone. No existing booking changed meaning — all historical bookings "
        "were online payments and are now labelled as such."
    )


if __name__ == "__main__":
    asyncio.run(main())
