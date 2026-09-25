"""
One-off script to UPDATE existing care package rows with the customer-facing
expanded-card content (whats_included / service_details_text /
important_information), plus any tagline/description/price changes in
WORKBOOK_PACKAGE_CUSTOMER_COPY.

Matches on package_code and overwrites those fields if they've changed.
Safe to re-run — it's a no-op once everything matches. Run
add_care_package_content_schema.py first so the columns exist.

Usage (on the server, from the backend repo root):
    python3 update_care_package_content.py
"""
import asyncio
import sys
from decimal import Decimal

from app.core.database import AsyncSessionLocal, engine
from app.models.models import CarePackage
from app.seed import WORKBOOK_PACKAGE_CUSTOMER_COPY
from sqlalchemy import select

FIELDS = (
    "tagline",
    "description",
    "whats_included",
    "service_details_text",
    "important_information",
)
PRICE_FIELDS = ("package_price", "per_visit_price")


async def update_packages(session) -> int:
    updated = 0
    for package_code, copy in WORKBOOK_PACKAGE_CUSTOMER_COPY.items():
        res = await session.execute(
            select(CarePackage).where(CarePackage.package_code == package_code)
        )
        row = res.scalar_one_or_none()
        if row is None:
            # This script only refreshes copy on packages that already exist
            # (created by seed_workbook_package_requirements). If it's
            # missing, run the full seed first.
            print(f"  ! skipped {package_code}: package does not exist yet")
            continue

        changed = False
        for field in FIELDS:
            if field in copy and getattr(row, field) != copy[field]:
                setattr(row, field, copy[field])
                changed = True
        for field in PRICE_FIELDS:
            if field in copy:
                new_value = Decimal(str(copy[field]))
                if getattr(row, field) != new_value:
                    setattr(row, field, new_value)
                    changed = True

        if changed:
            updated += 1
            print(f"  ~ updated {package_code}: {row.name!r}")
        else:
            print(f"  · up to date: {package_code}: {row.name!r}")
    return updated


async def main():
    print("Care package content update runner")
    print("=" * 50)
    async with AsyncSessionLocal() as session:
        updated = await update_packages(session)
        await session.commit()
    print("=" * 50)
    print(f"Done. {updated} care package row(s) updated.")
    if updated == 0:
        print("(Everything already matched — nothing to do.)")
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nUpdate run FAILED: {e}", file=sys.stderr)
        sys.exit(1)
