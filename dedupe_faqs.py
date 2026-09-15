"""Delete duplicate FAQ rows, keeping the oldest (lowest created_at) row per
(question, audience) group. Companion to check_dupes.py, which only detects.

Run check_dupes.py first to see what will be affected; this script is the
fix. Safe to re-run — once duplicates are gone, it's a no-op.

    python dedupe_faqs.py
"""
import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal


async def dedupe():
    async with AsyncSessionLocal() as session:
        # Keep the row with the smallest id (ties broken by created_at) in
        # each (question, audience) group among active rows; deactivate
        # rather than hard-delete so nothing is unrecoverable.
        result = await session.execute(text("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY question, audience
                           ORDER BY created_at ASC, id ASC
                       ) AS rn
                FROM faqs
                WHERE is_active = true
            )
            UPDATE faqs
            SET is_active = false
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            RETURNING id
        """))
        deactivated = result.fetchall()
        await session.commit()
        if not deactivated:
            print("No duplicates found — nothing to do.")
        else:
            print(f"Deactivated {len(deactivated)} duplicate FAQ row(s): {[r.id for r in deactivated]}")


if __name__ == "__main__":
    asyncio.run(dedupe())
