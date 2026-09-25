"""Sets package_price AND per_visit_price to ₹1 for every row in care_packages.

Safe to re-run (idempotent). Run this from the backend project root, with your
DATABASE_URL available (same as fix_package_prices.py already in this repo).

Usage:
    python set_all_prices_to_1.py            # apply the change
    python set_all_prices_to_1.py --dry-run  # just show what would change
"""

# --- SAFETY GUARD (added): destructive/test-only script -------------------
# Refuses to run against a production environment unless explicitly forced.
import os as _os, sys as _sys
try:
    from dotenv import load_dotenv as _ld
    _ld()
except Exception:  # noqa: BLE001
    pass
if (_os.environ.get("APP_ENV", "").strip().lower() in {"production", "prod", "staging", "stage", "uat"}
        and "--i-know-this-is-production" not in _sys.argv):
    _sys.exit("Refusing to run: APP_ENV is production. Re-run with --i-know-this-is-production if you are certain.")
if "--i-know-this-is-production" in _sys.argv:
    _sys.argv.remove("--i-know-this-is-production")
# ---------------------------------------------------------------------------

import asyncio
import os
import sys
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dry_run = "--dry-run" in sys.argv

    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)

    rows = await conn.fetch(
        "SELECT package_code, package_price, per_visit_price FROM care_packages ORDER BY package_code"
    )

    print(f"{'DRY RUN — ' if dry_run else ''}{len(rows)} package(s) found:")
    for r in rows:
        print(f"  {r['package_code']:<28} package_price={r['package_price']} per_visit_price={r['per_visit_price']}")

    if dry_run:
        await conn.close()
        return

    result = await conn.execute(
        """
        UPDATE care_packages
        SET package_price = 1,
            per_visit_price = 1
        """
    )
    print(f"\nDone: {result}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())