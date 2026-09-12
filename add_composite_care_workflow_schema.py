"""Adds the Workflow 1 "Composite Care Package" schema to an existing
database: material_included bundling, the synchronized nurse/patient safety
checklist, mandatory photo-proof fields, and automated invoicing.

Only needed for a database that already existed before this change — a
brand-new database gets all of this automatically from create_tables.py
(which builds every table fresh from the current SQLAlchemy models).

Safe to re-run: every statement is idempotent (IF NOT EXISTS / ADD VALUE
IF NOT EXISTS).
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

    # New enum type: how the procedural kit reaches the nurse.
    await conn.execute("""
        DO $$ BEGIN
            CREATE TYPE fulfillment_route AS ENUM ('pouch_stock', 'partner_pickup');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("Enum type ready: fulfillment_route")

    # New booking_status values (additive — existing rows unaffected).
    # NOTE: ALTER TYPE ... ADD VALUE cannot run inside a DO/exception block or
    # an explicit multi-statement transaction on PG < 13, so these run as
    # plain top-level statements; IF NOT EXISTS alone makes them idempotent.
    for value in ("prescription_pending", "searching_nurse", "quality_discrepancy_alert"):
        await conn.execute(f"ALTER TYPE booking_status ADD VALUE IF NOT EXISTS '{value}';")
    print("booking_status extended: prescription_pending, searching_nurse, quality_discrepancy_alert")

    # care_packages — mark which packages bundle a procedural kit.
    await conn.execute("""
        ALTER TABLE care_packages
        ADD COLUMN IF NOT EXISTS material_included BOOLEAN NOT NULL DEFAULT false;
    """)

    # bookings — snapshot of material bundling + fulfillment + linked Rx.
    await conn.execute("""
        ALTER TABLE bookings
        ADD COLUMN IF NOT EXISTS material_included BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS prescription_id UUID REFERENCES prescriptions(id),
        ADD COLUMN IF NOT EXISTS fulfillment_route fulfillment_route;
    """)
    print("bookings + care_packages: material_included columns added")

    # visit_records — synchronized safety checklist + anti-cheat + photo proof.
    await conn.execute("""
        ALTER TABLE visit_records
        ADD COLUMN IF NOT EXISTS pre_procedure_checklist JSONB,
        ADD COLUMN IF NOT EXISTS pre_procedure_checklist_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS patient_safety_verification JSONB,
        ADD COLUMN IF NOT EXISTS patient_safety_verification_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS quality_discrepancy BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS quality_discrepancy_reviewed_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS quality_discrepancy_reviewed_by UUID REFERENCES users(id),
        ADD COLUMN IF NOT EXISTS pre_procedure_photo_url TEXT,
        ADD COLUMN IF NOT EXISTS pre_procedure_photo_meta JSONB,
        ADD COLUMN IF NOT EXISTS post_procedure_photo_url TEXT,
        ADD COLUMN IF NOT EXISTS post_procedure_photo_meta JSONB;
    """)
    print("visit_records: safety checklist + photo-proof columns added")

    # invoices table.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id UUID PRIMARY KEY,
            booking_id UUID NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE CASCADE,
            invoice_number VARCHAR(40) NOT NULL UNIQUE,
            invoice_type VARCHAR(50) NOT NULL DEFAULT 'professional_service',
            gst_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
            subtotal_amount NUMERIC(10,2) NOT NULL,
            tax_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
            total_amount NUMERIC(10,2) NOT NULL,
            line_items JSONB NOT NULL,
            pdf_url TEXT,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_invoices_booking_id ON invoices(booking_id);
    """)
    print("invoices table ready")

    await conn.close()
    print("Done.")


asyncio.run(main())
