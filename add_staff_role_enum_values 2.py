"""Adds the new staff UserRole values (operations, support,
clinical_training_lead, clinical_trainer) to the existing Postgres
`user_role` enum type.

Only needed for a database that already existed before this change —
`create_tables.py` on a brand-new database picks these up automatically
since it creates the enum type fresh from the current Python UserRole enum.

`ALTER TYPE ... ADD VALUE` cannot be used in the same transaction as a
statement that references the new value, but running the four ADD VALUE
statements themselves back-to-back is safe on Postgres 12+.
"""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

NEW_VALUES = ["operations", "support", "clinical_training_lead", "clinical_trainer"]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)
    for value in NEW_VALUES:
        await conn.execute(f"ALTER TYPE user_role ADD VALUE IF NOT EXISTS '{value}'")
        print(f"Added enum value: {value}")
    await conn.close()
    print("Done.")


asyncio.run(main())
