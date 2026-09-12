"""Adds user_sessions.apns_voip_token — the iOS PushKit device token.

Kept separate from fcm_token on purpose: Apple issues a *different* token for
VoIP pushes than for ordinary notifications, and a VoIP push sent to the
standard APNs token is silently dropped. Storing them in one column would
make that failure invisible.

Only needed for a database that already existed before this change — a
brand-new database gets the column automatically from create_tables.py.

Safe to re-run: idempotent (IF NOT EXISTS).
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

    await conn.execute("""
        ALTER TABLE user_sessions
        ADD COLUMN IF NOT EXISTS apns_voip_token TEXT NULL
    """)
    print("user_sessions.apns_voip_token added")

    # Ringing looks up sessions by (user, revoked, token present) on every
    # incoming call, so give that lookup an index rather than scanning.
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_user_sessions_user_active
        ON user_sessions (user_id)
        WHERE revoked = false
    """)
    print("ix_user_sessions_user_active created")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
