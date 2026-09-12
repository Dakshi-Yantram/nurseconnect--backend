"""Adds the Contracts system to an existing database:
  - New table: worker_agreements (Stage 1 clickwrap + Stage 2 e-stamp agreement)
  - New columns on worker_documents: ocr_extracted_name, ocr_extracted_registration_no,
    ocr_confidence, ocr_raw_text

Safe to re-run: every statement is idempotent (IF NOT EXISTS). A brand-new
database gets all of this automatically from create_tables.py.
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
        CREATE TABLE IF NOT EXISTS worker_agreements (
            id UUID PRIMARY KEY,
            worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
            stage INTEGER NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            provider_type_snapshot VARCHAR(50) NOT NULL,
            rendered_text TEXT NOT NULL,
            template_version VARCHAR(20) NOT NULL DEFAULT 'v1',
            accepted_at TIMESTAMPTZ,
            otp_verified BOOLEAN NOT NULL DEFAULT false,
            ip_address VARCHAR(64),
            esign_provider VARCHAR(50),
            esign_reference_id VARCHAR(255),
            esign_document_url TEXT,
            onboarding_fee_deducted BOOLEAN NOT NULL DEFAULT false,
            voided_at TIMESTAMPTZ,
            void_reason TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_worker_agreements_worker_stage ON worker_agreements(worker_id, stage);")
    print("worker_agreements table ready")

    await conn.execute("ALTER TABLE worker_documents ADD COLUMN IF NOT EXISTS ocr_extracted_name VARCHAR(255);")
    await conn.execute("ALTER TABLE worker_documents ADD COLUMN IF NOT EXISTS ocr_extracted_registration_no VARCHAR(100);")
    await conn.execute("ALTER TABLE worker_documents ADD COLUMN IF NOT EXISTS ocr_confidence NUMERIC(4,3);")
    await conn.execute("ALTER TABLE worker_documents ADD COLUMN IF NOT EXISTS ocr_raw_text TEXT;")
    print("OCR columns ready on worker_documents")

    await conn.close()
    print("\nDone. No existing rows were modified.")


if __name__ == "__main__":
    asyncio.run(main())
