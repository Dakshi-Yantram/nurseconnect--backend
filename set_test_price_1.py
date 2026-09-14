"""TEMPORARY testing script — sets per_visit_price = 1 for all care packages,
so real bookings only charge ₹1 during testing. package_price is left
untouched (still shows the real total on the card).

Run fix_package_prices.py again later to restore real per_visit_price values
before going live.
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
        "UPDATE care_packages SET per_visit_price = 1 WHERE is_active = true"
    )
    print(f"care_packages.per_visit_price -> 1: {result}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())