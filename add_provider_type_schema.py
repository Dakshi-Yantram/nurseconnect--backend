"""Adds the Provider Type system to an existing database:
  - New WorkerType values: doctor, dentist, physiotherapist, mother_baby_caregiver
  - New WorkerOnboardingStatus values: training_pending, assessment_pending, expired
  - New enum: provider_status_change_reason
  - allowed_provider_types column on service_catalogue + care_packages
  - qualification_name column on worker_profiles
  - worker_references table
  - provider_status_history table

Only needed for a database that already existed before this change — a
brand-new database gets all of this automatically from create_tables.py
(which builds every table fresh from the current SQLAlchemy models).

Safe to re-run: every statement is idempotent (IF NOT EXISTS / ADD VALUE
IF NOT EXISTS). Does NOT modify any existing row's data — this is schema
only. See backfill_provider_types.py for the data backfill of existing
nurse/caregiver rows (nothing to do there: existing worker_type values are
already valid members of the extended enum, no row needs to change).
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

    # --- Extend worker_type_enum with the four new provider types ---
    # Postgres requires each ADD VALUE to run outside a transaction block
    # and cannot be combined with other DDL in the same statement.
    for value in ("doctor", "dentist", "physiotherapist", "mother_baby_caregiver"):
        await conn.execute(f"ALTER TYPE worker_type_enum ADD VALUE IF NOT EXISTS '{value}';")
    print("worker_type_enum extended: doctor, dentist, physiotherapist, mother_baby_caregiver")

    # --- Extend worker_onboarding_status ---
    for value in ("training_pending", "assessment_pending", "expired"):
        await conn.execute(f"ALTER TYPE worker_onboarding_status ADD VALUE IF NOT EXISTS '{value}';")
    print("worker_onboarding_status extended: training_pending, assessment_pending, expired")

    # --- New enum: provider_status_change_reason ---
    await conn.execute("""
        DO $$ BEGIN
            CREATE TYPE provider_status_change_reason AS ENUM (
                'applied', 'documents_submitted', 'document_rejected',
                'training_completed', 'training_required',
                'assessment_passed', 'assessment_failed',
                'practical_signoff_passed', 'practical_signoff_failed',
                'background_check_cleared', 'background_check_failed',
                'admin_approved', 'admin_rejected', 'admin_suspended', 'admin_reinstated',
                'license_expired', 'qualification_expired',
                'opted_in', 'opted_out'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("Enum type ready: provider_status_change_reason")

    # --- allowed_provider_types on service_catalogue / care_packages ---
    await conn.execute("""
        ALTER TABLE service_catalogue
            ADD COLUMN IF NOT EXISTS allowed_provider_types VARCHAR[];
    """)
    await conn.execute("""
        ALTER TABLE care_packages
            ADD COLUMN IF NOT EXISTS allowed_provider_types VARCHAR[];
    """)
    print("allowed_provider_types columns ready on service_catalogue, care_packages")

    # --- qualification_name on worker_profiles ---
    await conn.execute("""
        ALTER TABLE worker_profiles
            ADD COLUMN IF NOT EXISTS qualification_name VARCHAR(255);
    """)
    print("qualification_name column ready on worker_profiles")

    # --- worker_references table ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_references (
            id UUID PRIMARY KEY,
            worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
            reference_name VARCHAR(255) NOT NULL,
            relationship_to_worker VARCHAR(100),
            phone VARCHAR(20),
            previous_employer_name VARCHAR(255),
            verification_status VARCHAR(50) DEFAULT 'pending',
            verified_by UUID REFERENCES users(id),
            verified_at TIMESTAMPTZ,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_worker_references_worker_id ON worker_references(worker_id);")
    print("worker_references table ready")

    # --- provider_status_history table ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_status_history (
            id UUID PRIMARY KEY,
            worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
            from_status VARCHAR(50),
            to_status VARCHAR(50) NOT NULL,
            reason provider_status_change_reason,
            related_service_id UUID REFERENCES service_catalogue(id),
            related_package_id UUID REFERENCES care_packages(id),
            changed_by UUID REFERENCES users(id),
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_provider_status_history_worker_created ON provider_status_history(worker_id, created_at);")
    print("provider_status_history table ready")

    await conn.close()
    print("\nDone. Existing worker_profiles rows are untouched — worker_type stays "
          "'nurse' or 'caregiver' for every existing provider, allowed_provider_types "
          "is NULL (unrestricted) on every existing service/package, so nothing that "
          "qualifies today stops qualifying.")


if __name__ == "__main__":
    asyncio.run(main())