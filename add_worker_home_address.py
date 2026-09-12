"""Adds worker_profiles.home_address — the nurse's full street address,
captured on the profile page alongside home city, GPS pin and travel radius.

Only needed for a database that already existed before this change — a
brand-new database gets the column automatically from create_tables.py.

Safe to re-run: idempotent (ADD COLUMN IF NOT EXISTS).
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
        ALTER TABLE worker_profiles
        ADD COLUMN IF NOT EXISTS home_address VARCHAR(500) NULL
    """)
    print("worker_profiles.home_address added")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
