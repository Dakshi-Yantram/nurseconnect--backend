import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal


async def check():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT question, audience, COUNT(*) AS cnt
            FROM faqs
            WHERE is_active = true
            GROUP BY question, audience
            HAVING COUNT(*) > 1
        """))
        rows = result.all()
        if not rows:
            print("No active duplicates found — clean!")
        else:
            print(f"Found {len(rows)} duplicate group(s):")
            for row in rows:
                print(f"  question={row.question!r} audience={row.audience!r} count={row.cnt}")


if __name__ == "__main__":
    asyncio.run(check())