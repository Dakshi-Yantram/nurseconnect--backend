"""
Diagnostic — lists every User with role=worker, and shows whether they
have a matching WorkerProfile row. This finds accounts (like your "hunny"
login) that are stuck at 404 on /worker/new-requests because they never
got a WorkerProfile created.

USAGE (from backend/ folder):

    python check_worker_profiles.py
"""
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User, WorkerProfile
from app.models.enums import UserRole
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as session:
        ures = await session.execute(select(User).where(User.role == UserRole.worker))
        users = ures.scalars().all()

        if not users:
            print("No users with role=worker found at all.")
            return

        print(f"{'name/email/phone':<35} {'user_id':<38} has_worker_profile")
        print("-" * 100)
        for u in users:
            wres = await session.execute(
                select(WorkerProfile).where(WorkerProfile.user_id == u.id)
            )
            profile = wres.scalar_one_or_none()
            label = u.email or getattr(u, "phone", None) or getattr(u, "full_name", None) or "?"
            print(f"{str(label):<35} {str(u.id):<38} {'YES (' + str(profile.id) + ')' if profile else 'NO — MISSING'}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())