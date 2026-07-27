"""
One-off script to mark the admin user's email as verified, so the
"Verify your email before signing in" block no longer applies.

USAGE (from the backend/ folder, same place you run seed_admin.py):

    python fix_admin_email_verified.py
"""
import asyncio
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from sqlalchemy import select

ADMIN_EMAIL = "admin@nurseconnect.in"


async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.email == ADMIN_EMAIL))
        user = res.scalar_one_or_none()
        if not user:
            print(f"No user found with email {ADMIN_EMAIL}")
            return
        user.email_verified_at = datetime.now(timezone.utc)
        await session.commit()
        print(f"email_verified_at set for {ADMIN_EMAIL} — you can log in now.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())