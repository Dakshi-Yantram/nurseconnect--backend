"""Phase 3 follow-up migration — run AFTER add_provider_type_schema.py.

Adds:
  - allowed_provider_types column on training_modules (was accepted by the
    API schema but silently dropped — no column existed to persist it)
  - allowed_provider_types column on assessment_modules (same gap, found
    when extending the same fix to standalone assessments)
  - is_deleted / deleted_at / deleted_by soft-delete columns on
    service_catalogue (mirrors the existing care_packages soft-delete
    pattern), needed by the new /admin/services CRUD endpoints

Safe to re-run: every statement is idempotent (ADD COLUMN IF NOT EXISTS).
Does not modify any existing row's data.
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

    # --- allowed_provider_types on training_modules ---
    await conn.execute("""
        ALTER TABLE training_modules
            ADD COLUMN IF NOT EXISTS allowed_provider_types VARCHAR[];
    """)
    print("allowed_provider_types column ready on training_modules")

    # --- allowed_provider_types on assessment_modules ---
    await conn.execute("""
        ALTER TABLE assessment_modules
            ADD COLUMN IF NOT EXISTS allowed_provider_types VARCHAR[];
    """)
    print("allowed_provider_types column ready on assessment_modules")

    # --- soft-delete columns on service_catalogue ---
    await conn.execute("""
        ALTER TABLE service_catalogue
            ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
    """)
    await conn.execute("""
        ALTER TABLE service_catalogue
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
    """)
    await conn.execute("""
        ALTER TABLE service_catalogue
            ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_service_catalogue_is_deleted ON service_catalogue(is_deleted);
    """)
    print("is_deleted / deleted_at / deleted_by columns ready on service_catalogue")

    await conn.close()
    print("\nDone. Every existing training_modules and assessment_modules row has "
          "allowed_provider_types = NULL (visible to every provider type, unchanged "
          "behavior). Every existing service_catalogue row has is_deleted = false "
          "(unchanged, still active).")


if __name__ == "__main__":
    asyncio.run(main())
