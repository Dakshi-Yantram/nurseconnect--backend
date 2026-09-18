"""Adds schema for three features, all additive/idempotent (safe to re-run):

1. E-prescription (doctor-generated, in-app):
   - worker_profiles: signature_url / signature_public_id / signature_uploaded_at
     (the doctor's saved signature PNG, reused to stamp every Rx PDF)
   - prescriptions: is_doctor_generated, issued_by_worker_id, diet_notes,
     patient_issues, signature_url, pdf_url, pdf_public_id,
     verification_hash, qr_code_url

2. Teledoctor consultation queue (waiting -> diet -> patient issues ->
   prescription), one new table: tele_consultations.

3. Payout admin-approval gate (two-step: approve, then process/pay), plus
   spread onboarding-fee tracking:
   - worker_payouts: approval_status, approved_by, approved_at,
     approval_rejection_reason
   - worker_agreements: onboarding_fee_collected

Only needed for a database that already existed before this change — a
brand-new database gets all of this from create_tables.py automatically.
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

    # ---- new enum types -----------------------------------------------
    await conn.execute("""
        DO $$ BEGIN
            CREATE TYPE tele_consultation_stage AS ENUM
                ('waiting', 'diet_review', 'patient_assessment', 'prescription', 'completed');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("tele_consultation_stage enum ok")

    await conn.execute("""
        DO $$ BEGIN
            CREATE TYPE payout_approval_status AS ENUM
                ('pending_approval', 'approved', 'rejected');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("payout_approval_status enum ok")

    # ---- worker_profiles: doctor signature -----------------------------
    await conn.execute("""
        ALTER TABLE worker_profiles
        ADD COLUMN IF NOT EXISTS signature_url TEXT NULL,
        ADD COLUMN IF NOT EXISTS signature_public_id TEXT NULL,
        ADD COLUMN IF NOT EXISTS signature_uploaded_at TIMESTAMPTZ NULL
    """)
    print("worker_profiles signature columns added")

    # ---- prescriptions: e-prescription fields --------------------------
    await conn.execute("""
        ALTER TABLE prescriptions
        ADD COLUMN IF NOT EXISTS is_doctor_generated BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS issued_by_worker_id UUID NULL REFERENCES worker_profiles(id),
        ADD COLUMN IF NOT EXISTS diet_notes TEXT NULL,
        ADD COLUMN IF NOT EXISTS patient_issues TEXT NULL,
        ADD COLUMN IF NOT EXISTS signature_url TEXT NULL,
        ADD COLUMN IF NOT EXISTS pdf_url TEXT NULL,
        ADD COLUMN IF NOT EXISTS pdf_public_id TEXT NULL,
        ADD COLUMN IF NOT EXISTS verification_hash VARCHAR(64) NULL,
        ADD COLUMN IF NOT EXISTS qr_code_url TEXT NULL
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_prescriptions_is_doctor_generated
        ON prescriptions (is_doctor_generated)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_prescriptions_verification_hash
        ON prescriptions (verification_hash)
    """)
    print("prescriptions e-prescription columns added")

    # ---- tele_consultations table --------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tele_consultations (
            id UUID PRIMARY KEY,
            booking_id UUID NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE CASCADE,
            doctor_worker_id UUID NOT NULL REFERENCES worker_profiles(id),
            patient_id UUID NOT NULL REFERENCES patients(id),
            stage tele_consultation_stage NOT NULL DEFAULT 'waiting',
            diet_notes TEXT NULL,
            patient_issues TEXT NULL,
            patient_all_okay BOOLEAN NULL,
            prescription_id UUID NULL REFERENCES prescriptions(id),
            started_at TIMESTAMPTZ NULL,
            diet_reviewed_at TIMESTAMPTZ NULL,
            patient_assessed_at TIMESTAMPTZ NULL,
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_tele_consultations_doctor_stage
        ON tele_consultations (doctor_worker_id, stage)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_tele_consultations_stage
        ON tele_consultations (stage)
    """)
    print("tele_consultations table ready")

    # ---- worker_payouts: approval gate ----------------------------------
    await conn.execute("""
        ALTER TABLE worker_payouts
        ADD COLUMN IF NOT EXISTS approval_status payout_approval_status NOT NULL DEFAULT 'pending_approval',
        ADD COLUMN IF NOT EXISTS approved_by UUID NULL REFERENCES users(id),
        ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS approval_rejection_reason TEXT NULL
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_worker_payouts_approval_status
        ON worker_payouts (approval_status)
    """)
    print("worker_payouts approval columns added")

    # Back-fill: payouts already paid clearly went through the old one-step
    # flow — mark them approved retroactively so historical rows don't show
    # as stuck pending approval in the new admin queue.
    result = await conn.execute("""
        UPDATE worker_payouts
        SET approval_status = 'approved', approved_at = COALESCE(paid_at, now())
        WHERE status = 'paid' AND approval_status = 'pending_approval'
    """)
    print(f"back-filled already-paid payouts as approved: {result}")

    # ---- worker_agreements: spread onboarding fee tracking --------------
    await conn.execute("""
        ALTER TABLE worker_agreements
        ADD COLUMN IF NOT EXISTS onboarding_fee_collected NUMERIC(6,2) NOT NULL DEFAULT 0
    """)
    # Back-fill: agreements already flagged fully-deducted -> collected = 200
    await conn.execute("""
        UPDATE worker_agreements
        SET onboarding_fee_collected = 200.00
        WHERE onboarding_fee_deducted = true AND onboarding_fee_collected = 0
    """)
    print("worker_agreements onboarding_fee_collected added + back-filled")

    await conn.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
