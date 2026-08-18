"""Production DB schema sync for NurseConnect.

Adds:
- Three-gate qualification model
- Anti-cheat assessment configuration
- Worker assessment sessions
- Practical sign-offs
- Service provider-type filtering
- Service soft-delete columns

Safe to re-run because schema changes use IF NOT EXISTS.
"""

import asyncio
import os

import asyncpg
from dotenv import load_dotenv


load_dotenv(".env.prod")

DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dsn = (
        DATABASE_URL
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("?ssl=require", "?sslmode=require")
    )

    conn = await asyncpg.connect(dsn)

    try:
        # ============================================================
        # 1. ENUM TYPES
        # ============================================================

        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE qualification_gate AS ENUM (
                    'credential_only',
                    'theory_verified',
                    'practical_verified'
                );
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """)

        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE assessment_session_status AS ENUM (
                    'in_progress',
                    'completed',
                    'expired'
                );
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """)

        print("✅ Enum types ready")

        # ============================================================
        # 2. SERVICE CATALOGUE
        # ============================================================

        await conn.execute("""
            ALTER TABLE service_catalogue
            ADD COLUMN IF NOT EXISTS
                gate qualification_gate
                NOT NULL DEFAULT 'credential_only',

            ADD COLUMN IF NOT EXISTS
                practical_checklist_items VARCHAR[],

            ADD COLUMN IF NOT EXISTS
                allowed_provider_types VARCHAR[],

            ADD COLUMN IF NOT EXISTS
                is_deleted BOOLEAN
                NOT NULL DEFAULT false,

            ADD COLUMN IF NOT EXISTS
                deleted_at TIMESTAMPTZ,

            ADD COLUMN IF NOT EXISTS
                deleted_by UUID;
        """)

        print("✅ service_catalogue schema updated")

        # ============================================================
        # 3. CARE PACKAGES
        # ============================================================

        await conn.execute("""
            ALTER TABLE care_packages
            ADD COLUMN IF NOT EXISTS
                gate qualification_gate
                NOT NULL DEFAULT 'credential_only',

            ADD COLUMN IF NOT EXISTS
                practical_checklist_items VARCHAR[];
        """)

        print("✅ care_packages schema updated")

        # ============================================================
        # 4. ASSESSMENT MODULES - ANTI CHEAT
        # ============================================================

        await conn.execute("""
            ALTER TABLE assessment_modules
            ADD COLUMN IF NOT EXISTS
                randomize_options BOOLEAN
                NOT NULL DEFAULT true,

            ADD COLUMN IF NOT EXISTS
                questions_per_attempt INTEGER,

            ADD COLUMN IF NOT EXISTS
                time_limit_minutes INTEGER,

            ADD COLUMN IF NOT EXISTS
                max_attempts INTEGER,

            ADD COLUMN IF NOT EXISTS
                cooldown_hours INTEGER
                NOT NULL DEFAULT 0;
        """)

        print("✅ assessment_modules anti-cheat schema updated")

        # ============================================================
        # 5. WORKER ASSESSMENT SESSIONS
        # ============================================================

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_assessment_sessions (
                id UUID PRIMARY KEY,

                worker_id UUID NOT NULL
                    REFERENCES worker_profiles(id)
                    ON DELETE CASCADE,

                assessment_id UUID NOT NULL
                    REFERENCES assessment_modules(id)
                    ON DELETE CASCADE,

                assessment_version INTEGER NOT NULL,

                status assessment_session_status NOT NULL
                    DEFAULT 'in_progress',

                question_order JSONB NOT NULL,

                current_index INTEGER NOT NULL
                    DEFAULT 0,

                answers JSONB NOT NULL
                    DEFAULT '[]',

                score INTEGER,

                passed BOOLEAN,

                started_at TIMESTAMPTZ NOT NULL
                    DEFAULT now(),

                expires_at TIMESTAMPTZ,

                completed_at TIMESTAMPTZ
            );
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_worker_assessment_sessions_worker_id
            ON worker_assessment_sessions(worker_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_worker_assessment_sessions_assessment_id
            ON worker_assessment_sessions(assessment_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_worker_assessment_sessions_status
            ON worker_assessment_sessions(status);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_worker_assessment_sessions_worker_ass
            ON worker_assessment_sessions(worker_id, assessment_id);
        """)

        print("✅ worker_assessment_sessions ready")

        # ============================================================
        # 6. PRACTICAL SIGN-OFFS
        # ============================================================

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS practical_sign_offs (
                id UUID PRIMARY KEY,

                worker_id UUID NOT NULL
                    REFERENCES worker_profiles(id)
                    ON DELETE CASCADE,

                service_id UUID
                    REFERENCES service_catalogue(id),

                package_id UUID
                    REFERENCES care_packages(id),

                checklist_responses JSONB NOT NULL,

                passed BOOLEAN NOT NULL,

                notes TEXT,

                signed_by UUID NOT NULL
                    REFERENCES users(id),

                signed_at TIMESTAMPTZ NOT NULL
                    DEFAULT now()
            );
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_practical_sign_offs_worker_id
            ON practical_sign_offs(worker_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_practical_sign_offs_service_id
            ON practical_sign_offs(service_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_practical_sign_offs_package_id
            ON practical_sign_offs(package_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_practical_sign_offs_worker_service
            ON practical_sign_offs(worker_id, service_id);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
                ix_practical_sign_offs_worker_package
            ON practical_sign_offs(worker_id, package_id);
        """)

        print("✅ practical_sign_offs ready")

        print("")
        print("🎉 DATABASE SCHEMA SYNC COMPLETE")
        print("🎉 All migration changes completed successfully")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())