"""
One-off script to create a reviewer account and reviewer profile — so tickets
can actually auto-assign to someone and appear in the "Nurse Approval" /
My Review Queue UI.

USAGE (from the backend/ folder):

    python seed_reviewer.py

Creates ONE reviewer account:
    email:    reviewer@nurseconnect.in
    password: Reviewer@1234
    role:     reviewer
    + a ReviewerProfile row (active, can_review_nurse_documents=True)
"""
import asyncio
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models.models import User, ReviewerProfile
from app.models.enums import UserRole, UserStatus
from sqlalchemy import select

REVIEWER_EMAIL = "reviewer@nurseconnect.in"
REVIEWER_PASSWORD = "Reviewer@1234"
REVIEWER_PHONE = "+919999000009"
REVIEWER_FULL_NAME = "Test Reviewer"


async def main():
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(User).where(User.email == REVIEWER_EMAIL))
        user = existing.scalar_one_or_none()

        if not user:
            user = User(
                phone_e164=REVIEWER_PHONE,
                email=REVIEWER_EMAIL,
                full_name=REVIEWER_FULL_NAME,
                role=UserRole.reviewer,
                status=UserStatus.active,
                password_hash=hash_password(REVIEWER_PASSWORD),
                email_verified_at=datetime.now(timezone.utc),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            print(f"+ created reviewer user {REVIEWER_EMAIL} (id={user.id})")
        else:
            if user.role != UserRole.reviewer:
                user.role = UserRole.reviewer
            if not user.email_verified_at:
                user.email_verified_at = datetime.now(timezone.utc)
            await session.commit()
            print(f"· reviewer user {REVIEWER_EMAIL} already existed (id={user.id})")

        rp_res = await session.execute(
            select(ReviewerProfile).where(ReviewerProfile.user_id == user.id)
        )
        rp = rp_res.scalar_one_or_none()
        if not rp:
            rp = ReviewerProfile(
                user_id=user.id,
                is_active=True,
                can_review_nurse_documents=True,
                max_open_tickets=20,
                specialization="nursing",
            )
            session.add(rp)
            await session.commit()
            print("+ created ReviewerProfile")
        else:
            rp.is_active = True
            rp.can_review_nurse_documents = True
            await session.commit()
            print("· ReviewerProfile already existed — ensured active")

    print("\nDone. Log in via POST /api/auth/login with:")
    print(f"  email:    {REVIEWER_EMAIL}")
    print(f"  password: {REVIEWER_PASSWORD}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())