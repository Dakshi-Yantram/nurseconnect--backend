"""Restores original package_price / per_visit_price values captured on
2026-09-23 from production (before the set_all_prices_to_1.py run).

Usage:
    python restore_prices.py            # apply the restore
    python restore_prices.py --dry-run  # just show what would change
"""

import os as _os, sys as _sys
try:
    from dotenv import load_dotenv as _ld
    _ld()
except Exception:
    pass
if (_os.environ.get("APP_ENV", "").strip().lower() in {"production", "prod", "staging", "stage", "uat"}
        and "--i-know-this-is-production" not in _sys.argv):
    _sys.exit("Refusing to run: APP_ENV is production. Re-run with --i-know-this-is-production if you are certain.")
if "--i-know-this-is-production" in _sys.argv:
    _sys.argv.remove("--i-know-this-is-production")

import asyncio
import os
import sys
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

ORIGINAL_PRICES = {
    "DIABETES_CARE_14D":       (4999.00, 714.00),
    "ELDERLY_MONTHLY":         (17999.00, 600.00),
    "MATERNITY_POSTNATAL_30D": (21999.00, 733.00),
    "PKG-A1-01":               (1.00, 1.00),
    "PKG-A1-02":               (299.00, 299.00),
    "PKG-A1-03":               (329.00, 329.00),
    "PKG-A1-04":               (449.00, 449.00),
    "PKG-A1-05":               (499.00, 499.00),
    "PKG-A1-06":               (599.00, 599.00),
    "PKG-A1-07":               (449.00, 449.00),
    "PKG-A1-08":               (599.00, 599.00),
    "PKG-A1-09":               (999.00, 999.00),
    "PKG-A1-10":               (1699.00, 1699.00),
    "PKG-A1-11":               (349.00, 349.00),
    "PKG-A1-12":               (399.00, 399.00),
    "PKG-A2-01":               (449.00, 449.00),
    "PKG-A2-02":               (499.00, 499.00),
    "PKG-A2-03":               (599.00, 599.00),
    "PKG-A2-04":               (799.00, 799.00),
    "PKG-A2-05":               (699.00, 699.00),
    "PKG-A2-06":               (499.00, 499.00),
    "PKG-A2-07":               (449.00, 449.00),
    "PKG-A2-08":               (799.00, 799.00),
    "PKG-A2-09":               (549.00, 549.00),
    "PKG-A2-10":               (899.00, 899.00),
    "PKG-A2-11":               (649.00, 649.00),
    "PKG-A3-01":               (999.00, 999.00),
    "PKG-A3-02":               (1099.00, 1099.00),
    "PKG-A3-03":               (1499.00, 1499.00),
    "PKG-A3-04":               (8499.00, 1214.00),
    "PKG-A3-05":               (1899.00, 1899.00),
    "PKG-A3-06":               (1699.00, 1699.00),
    "PKG-A3-07":               (1599.00, 1599.00),
    "PKG-A4-01":               (899.00, 899.00),
    "PKG-A4-02":               (1599.00, 1599.00),
    "PKG-A4-03":               (2199.00, 2199.00),
    "PKG-A4-04":               (2399.00, 2399.00),
    "PKG-A4-05":               (4299.00, 4299.00),
    "PKG-A4-06":               (13999.00, 2000.00),
    "PKG-A4-07":               (54999.00, 1833.00),
    "PKG-A4-08":               (3999.00, 3999.00),
    "POST_OP_7D":              (8999.00, 1285.00),
    "POST_OP_CARE_7D":         (10999.00, 1571.00),
}


async def main():
    dry_run = "--dry-run" in sys.argv

    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)

    print(f"{'DRY RUN — ' if dry_run else ''}restoring {len(ORIGINAL_PRICES)} package(s):")
    for code, (pkg_price, visit_price) in ORIGINAL_PRICES.items():
        print(f"  {code:<28} package_price={pkg_price} per_visit_price={visit_price}")
        if not dry_run:
            await conn.execute(
                "UPDATE care_packages SET package_price = $1, per_visit_price = $2 WHERE package_code = $3",
                pkg_price, visit_price, code,
            )

    if not dry_run:
        print("\nDone.")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
