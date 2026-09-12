"""Fixes care_packages.package_price (currently a dummy ₹1 for all packages)
and corrects DIABETES_CARE_14D.per_visit_price (DB has 733, seed.py says 714).
Safe to re-run: always sets to the same target values.
"""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

FIXES = [
    ("POST_OP_7D", 8999, None),
    ("ELDERLY_MONTHLY", 17999, None),
    ("DIABETES_CARE_14D", 4999, 714),
    ("MATERNITY_POSTNATAL_30D", 21999, None),
]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)

    for code, package_price, per_visit_price in FIXES:
        if per_visit_price is not None:
            result = await conn.execute(
                """
                UPDATE care_packages
                SET package_price = $1, per_visit_price = $2
                WHERE package_code = $3
                """,
                package_price, per_visit_price, code,
            )
        else:
            result = await conn.execute(
                """
                UPDATE care_packages
                SET package_price = $1
                WHERE package_code = $2
                """,
                package_price, code,
            )
        print(f"{code}: {result}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())