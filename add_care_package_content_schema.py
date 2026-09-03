"""Production DB schema sync for NurseConnect.

Adds the customer-facing "expanded card" content columns to care_packages:

- whats_included          VARCHAR[]  — bullet list shown under "What's included"
- service_details_text    TEXT       — 1-2 sentence "Service details" line
- important_information   TEXT       — shown only when applicable, else the
                                        card omits the section entirely

These back the collapsed/expanded package-card pattern (WHAT CARE DO YOU
NEED?) — the collapsed card already had name/price/description/metadata;
this is what "View details" reveals inline, without a screen change.

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
        await conn.execute("""
            ALTER TABLE care_packages
            ADD COLUMN IF NOT EXISTS
                whats_included VARCHAR[],

            ADD COLUMN IF NOT EXISTS
                service_details_text TEXT,

            ADD COLUMN IF NOT EXISTS
                important_information TEXT;
        """)

        print("✅ care_packages expanded-card content columns ready")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
