"""List (dry-run, default) or deactivate test-only / template-less catalogue rows.

Rows like "High-Risk Service (template missing — test only)" must not exist as
sellable rows in production. The API now hides and refuses them
(app.services.catalog_guard), but leaving them ``is_active`` in the database is
still untidy and admin lists show them. Run WITHOUT flags first to review, then:

    python deactivate_test_catalog_items.py --apply

Never hard-deletes (existing bookings may reference these rows).
"""
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()
APPLY = "--apply" in sys.argv

TEST_RE = r"(test[ _-]*only|template[ _-]*missing|qa[ _-]*only|\[ *test *\]|\( *test *\)|dummy|sandbox)"


async def main():
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "?sslmode=require")
    conn = await asyncpg.connect(dsn)
    try:
        for table, code in (("service_catalogue", "service_code"), ("care_packages", "package_code")):
            rows = await conn.fetch(
                f"""SELECT id, {code} AS code, name, is_active FROM {table}
                     WHERE (name ~* $1 OR {code} ~* $1 OR {code} ~* '^(TEST|TMP|QA|ZZ)[_-]')""",
                TEST_RE,
            )
            print(f"[{table}] {len(rows)} test-like row(s)")
            for r in rows:
                print(f"   {'ACTIVE' if r['is_active'] else 'inactive'}  {r['code']}  {r['name']}")
            if APPLY and rows:
                await conn.execute(
                    f"UPDATE {table} SET is_active = false WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in rows],
                )
                print(f"   -> deactivated {len(rows)}")
        if not APPLY:
            print("dry run only; re-run with --apply to deactivate")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
