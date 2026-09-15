"""TEMPORARY testing script — sets BOTH package_price and per_visit_price
to 1 for all active care packages, so every package card (website + app)
shows Rs 1 and every real booking only charges Rs 1.

Both consumer.nurseconnect.co.in (website) and the mobile app read prices
from this same `care_packages` table via the backend API, so one run of
this script updates the price everywhere.

Run fix_package_prices.py later to restore the real prices before going live.
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

    result = await conn.execute(
        "UPDATE care_packages SET package_price = 1, per_visit_price = 1 WHERE is_active = true"
    )
    print(f"care_packages.package_price & per_visit_price -> 1: {result}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())