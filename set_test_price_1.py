"""TEMPORARY testing script — sets BOTH package_price and per_visit_price
to 1 for all active care packages, so every package card (website + app)
shows Rs 1 and every real booking only charges Rs 1.

Both consumer.nurseconnect.co.in (website) and the mobile app read prices
from this same `care_packages` table via the backend API, so one run of
this script updates the price everywhere.

Run fix_package_prices.py later to restore the real prices before going live.
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