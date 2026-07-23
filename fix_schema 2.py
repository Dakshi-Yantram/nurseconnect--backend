import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

async def main():
    # asyncpg doesn't understand SQLAlchemy-style "+asyncpg" driver suffixes
    # or the "ssl=require" query param spelling — normalize both.
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)
    await conn.execute("""
        ALTER TABLE escalations
        ADD COLUMN IF NOT EXISTS assigned_to UUID,
        ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS internal_notes VARCHAR,
        ADD COLUMN IF NOT EXISTS priority VARCHAR DEFAULT 'normal';
    """)
    print("Columns added successfully.")
    await conn.close()

asyncio.run(main())