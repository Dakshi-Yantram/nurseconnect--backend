"""Adds call_sessions (Dyte call logs) and push_subscriptions (Web Push /
VAPID subscriptions used for the best-effort background call ping) tables.

Only needed for a database that already existed before this change — a
brand-new database gets these tables automatically from create_tables.py.

Safe to re-run: idempotent (CREATE TABLE IF NOT EXISTS).
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
        CREATE TABLE IF NOT EXISTS call_sessions (
            id UUID PRIMARY KEY,
            booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
            dyte_meeting_id VARCHAR(120) NOT NULL,
            initiated_by_user_id UUID NOT NULL REFERENCES users(id),
            initiated_by_role VARCHAR(20) NOT NULL,
            callee_user_id UUID NOT NULL REFERENCES users(id),
            status VARCHAR(20) NOT NULL DEFAULT 'ringing',
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            callee_joined_at TIMESTAMPTZ NULL,
            ended_at TIMESTAMPTZ NULL,
            duration_seconds INTEGER NULL,
            end_reason VARCHAR(30) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_call_sessions_booking_id ON call_sessions(booking_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_call_sessions_dyte_meeting_id ON call_sessions(dyte_meeting_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_call_sessions_status ON call_sessions(status)")
    print("call_sessions ready")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh_key TEXT NOT NULL,
            auth_key TEXT NOT NULL,
            user_agent TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user_id ON push_subscriptions(user_id)")
    print("push_subscriptions ready")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())