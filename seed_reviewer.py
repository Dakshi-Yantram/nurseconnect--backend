"""
One-off script to create a single reviewer user directly in the database,
bypassing the API (which blocks self-registration of admin/reviewer roles).

Place this file in the backend/ folder (next to seed_admin.py) and run:

    python seed_reviewer.py

This creates ONE reviewer account:
    email:    reviewer@nurseconnect.in
    password: Review@1234
    role:     reviewer
    status:   active (no email verification needed — created pre-verified)

A reviewer can:
  - review / verify worker onboarding documents
  - approve or reject nurses (onboarding)
  - author training modules and assessments
  - check assessments / tests taken by nurses and caregivers

Safe to re-run: if a user with this email already exists, it will skip
creation instead of erroring or creating a duplicate.
"""
import asyncio
import sys

from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models.models import User
from app.models.enums import UserRole, UserStatus
from sqlalchemy import select


REVIEWER_EMAIL = "reviewer@nurseconnect.in"
REVIEWER_PASSWORD = "Review@1234"
REVIEWER_PHONE = "+919999000004"
REVIEWER_FULL_NAME = "Test Reviewer"
REVIEWER_ROLE = UserRole.reviewer


async def main():
    print("NurseConnect reviewer seed runner")
    print("=" * 50)

    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(User).where(User.email == REVIEWER_EMAIL)
        )
        user = existing.scalar_one_or_none()

        if user:
            # Keep the role correct even if the row predates this script.
            if user.role != REVIEWER_ROLE:
                user.role = REVIEWER_ROLE
                await session.commit()
                print(f"  · reviewer user {REVIEWER_EMAIL} already existed — role corrected to '{REVIEWER_ROLE.value}'")
            else:
                print(f"  · reviewer user {REVIEWER_EMAIL} already exists (id={user.id}), skipping creation")
        else:
            user = User(
                phone_e164=REVIEWER_PHONE,
                email=REVIEWER_EMAIL,
                full_name=REVIEWER_FULL_NAME,
                role=REVIEWER_ROLE,
                status=UserStatus.active,
                password_hash=hash_password(REVIEWER_PASSWORD),
            )
            session.add(user)
            await session.commit()
            print(f"  + created reviewer user {REVIEWER_EMAIL} (role={REVIEWER_ROLE.value})")

    print("\n" + "=" * 50)
    print("Done. You can now log in via POST /api/auth/login with:")
    print(f"  email:    {REVIEWER_EMAIL}")
    print(f"  password: {REVIEWER_PASSWORD}")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nReviewer seed run FAILED: {e}", file=sys.stderr)
        sys.exit(1)
