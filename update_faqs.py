"""
One-off script to UPDATE existing FAQ rows with the latest answer text.

Unlike seed_faqs() in seed.py (which skips a question if it already
exists), this script matches on (question, audience) and overwrites the
`answer` field if it has changed. Safe to re-run — it's a no-op once
everything matches.

Usage (on the server, from the backend repo root):
    python3 update_faqs.py
"""
import asyncio
import sys

from app.core.database import AsyncSessionLocal, engine
from app.models.models import Faq
from app.seed import FAQS
from sqlalchemy import select


async def update_faqs(session) -> int:
    updated = 0
    for data in FAQS:
        res = await session.execute(
            select(Faq).where(
                Faq.question == data["question"],
                Faq.audience == data["audience"],
            )
        )
        rows = res.scalars().all()
        if len(rows) > 1:
            # Duplicate rows from an earlier run — keep the first (oldest),
            # deactivate the rest so they stop showing up twice in the app.
            print(
                f"  ! found {len(rows)} duplicate rows for {data['question']!r} "
                f"({data['audience']}) — keeping one, deactivating the others"
            )
            for dup in rows[1:]:
                dup.is_active = False
        row = rows[0] if rows else None
        if row is None:
            # Doesn't exist yet — create it so this script also covers
            # brand-new FAQ entries added to the list.
            session.add(Faq(**data, is_active=True))
            updated += 1
            print(f"  + created FAQ: {data['question']!r} ({data['audience']})")
            continue

        changed = False
        for field in ("answer", "category", "display_order"):
            if getattr(row, field) != data.get(field):
                setattr(row, field, data.get(field))
                changed = True
        if changed:
            updated += 1
            print(f"  ~ updated FAQ: {data['question']!r} ({data['audience']})")
        else:
            print(f"  · up to date: {data['question']!r} ({data['audience']})")
    return updated


async def main():
    print("FAQ update runner")
    print("=" * 50)
    async with AsyncSessionLocal() as session:
        updated = await update_faqs(session)
        await session.commit()
    print("=" * 50)
    print(f"Done. {updated} FAQ row(s) created or updated.")
    if updated == 0:
        print("(Everything already matched — nothing to do.)")
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nUpdate run FAILED: {e}", file=sys.stderr)
        sys.exit(1)