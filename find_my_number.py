"""Find which phone number(s) you've logged in with — sorted by most recent
login first, so if you logged out, the one at the top is almost certainly
the account you were just using.

Run from the backend project root:
    python find_my_number.py
"""
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User).order_by(User.last_login_at.desc().nullslast())
        )
        users = res.scalars().all()
        print(f"Found {len(users)} users (most recently logged-in first):\n")
        for u in users:
            print(
                f"phone={u.phone_e164} | role={u.role.value if u.role else None} "
                f"| name={u.full_name} | last_login={u.last_login_at} "
                f"| status={u.status.value if u.status else None}"
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())