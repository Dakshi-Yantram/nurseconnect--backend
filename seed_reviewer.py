"""
One-off script to create a support-staff account — so someone can actually
log in to the Support portal (Support Queue, Ticket Queue, All Escalations)
without needing an admin to create a support account by hand first.

USAGE (from the backend/ folder, same place as seed_reviewer.py):

    python seed_support.py

Creates ONE support account:
    email:    support@nurseconnect.in
    password: Test@123
    role:     support

Support role has no separate profile table (unlike reviewer ->
ReviewerProfile / worker -> WorkerProfile) — the "support" role on the User
row is all that's needed; it's what /api/escalations/*, /api/support/tickets
etc. check via require_roles(UserRole.admin, UserRole.support).
"""
import asyncio
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models.models import User
from app.models.enums import UserRole, UserStatus
from sqlalchemy import select

SUPPORT_EMAIL = "support@nurseconnect.in"
SUPPORT_PASSWORD = "Test@123"
SUPPORT_PHONE = "+919999000010"
SUPPORT_FULL_NAME = "Support Staff"


async def main():
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(User).where(User.email == SUPPORT_EMAIL))
        user = existing.scalar_one_or_none()

        if not user:
            user = User(
                phone_e164=SUPPORT_PHONE,
                email=SUPPORT_EMAIL,
                full_name=SUPPORT_FULL_NAME,
                role=UserRole.support,
                status=UserStatus.active,
                password_hash=hash_password(SUPPORT_PASSWORD),
                email_verified_at=datetime.now(timezone.utc),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            print(f"+ created support user {SUPPORT_EMAIL} (id={user.id})")
        else:
            changed = False
            if user.role != UserRole.support:
                user.role = UserRole.support
                changed = True
            if user.status != UserStatus.active:
                user.status = UserStatus.active
                changed = True
            if not user.email_verified_at:
                user.email_verified_at = datetime.now(timezone.utc)
                changed = True
            # Always reset the password to the known value below, in case it
            # was previously created with a different / unknown password.
            user.password_hash = hash_password(SUPPORT_PASSWORD)
            await session.commit()
            print(f"· support user {SUPPORT_EMAIL} already existed (id={user.id}) — role/status/password ensured" + (" (updated)" if changed else ""))

    print("\nDone. Log in via POST /api/auth/login (or the web login page) with:")
    print(f"  email:    {SUPPORT_EMAIL}")
    print(f"  password: {SUPPORT_PASSWORD}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())