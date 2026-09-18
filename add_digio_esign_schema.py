"""Adds Digio e-Sign session tracking to an existing database.

  - New table: worker_esign_sessions

Only needed for a database that already existed before this change — a
brand-new database gets this automatically from create_tables.py, which
builds every table fresh from the current SQLAlchemy models.

Why this is a separate table rather than new columns on worker_agreements:
worker_agreements.esign_reference_id/esign_document_url used to be filled
in directly from whatever the CLIENT claimed after a signing session —
there was no server-side record of a signing session ever actually having
been started with Digio, let alone completed. Stage 2 could be marked
"accepted" from a client-supplied string with nothing behind it.

worker_esign_sessions is the server's own record: created the moment we ask
Digio to start a session, and its `status` column is written ONLY by
Digio's webhook or a verified status poll (see app/api/v1/contracts.py and
app/integrations/providers.py::DigioClient) — never by a request body.
accept_stage2() now refuses to create the executed WorkerAgreement row
unless the caller's own session here has status='signed'.

Safe to re-run: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are
idempotent. No existing row in any table is modified.

Usage:
    python add_digio_esign_schema.py
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_esign_sessions (
                id UUID PRIMARY KEY,
                worker_id UUID NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
                stage INTEGER NOT NULL DEFAULT 2,
                provider VARCHAR(50) NOT NULL DEFAULT 'digio',
                status VARCHAR(20) NOT NULL DEFAULT 'created',
                digio_document_id VARCHAR(255),
                sign_url TEXT,
                rendered_text TEXT NOT NULL,
                template_version VARCHAR(20) NOT NULL,
                last_provider_payload JSONB,
                failure_reason TEXT,
                signed_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        print("Table ready: worker_esign_sessions")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_esign_sessions_worker_stage
                ON worker_esign_sessions (worker_id, stage);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_esign_sessions_worker_id
                ON worker_esign_sessions (worker_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_esign_sessions_digio_document_id
                ON worker_esign_sessions (digio_document_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_worker_esign_sessions_status
                ON worker_esign_sessions (status);
        """)
        print("Indexes ready: worker_id+stage, worker_id, digio_document_id, status")

        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM worker_esign_sessions;")
        print(f"\nCurrent row count in worker_esign_sessions: {row['n']}")
    finally:
        await conn.close()

    print(
        "\nDone. No existing table was modified. Set DIGIO_CLIENT_ID, "
        "DIGIO_CLIENT_SECRET and DIGIO_WEBHOOK_SECRET in the environment "
        "before disabling MOCK_EXTERNAL_PROVIDERS, or Stage 2 e-Sign requests "
        "will fail closed (see Settings.startup_warnings())."
    )


if __name__ == "__main__":
    asyncio.run(main())
