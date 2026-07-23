"""
One-off script to create a single admin user directly in the database,
bypassing the API (which deliberately blocks self-registration of admin roles).

USAGE (from the backend/ folder, same place you run run_seed.py):

    python seed_admin.py

This creates ONE admin account:
    email:    admin@nurseconnect.in
    password: Admin@1234
    role:     admin
    status:   active (no email verification needed — created pre-verified)

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


ADMIN_EMAIL = "admin@nurseconnect.in"
ADMIN_PASSWORD = "Admin@1234"
ADMIN_PHONE = "+919999000008"
ADMIN_FULL_NAME = "Test Admin"
ADMIN_ROLE = UserRole.admin


async def main():
    print("NurseConnect admin seed runner")
    print("=" * 50)

    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(User).where(User.email == ADMIN_EMAIL)
        )
        user = existing.scalar_one_or_none()

        if user:
            if user.role != ADMIN_ROLE:
                user.role = ADMIN_ROLE
                await session.commit()
                print(f"  · admin user {ADMIN_EMAIL} already existed — role corrected to '{ADMIN_ROLE.value}'")
            else:
                print(f"  · admin user {ADMIN_EMAIL} already exists (id={user.id}), skipping creation")
        else:
            user = User(
                phone_e164=ADMIN_PHONE,
                email=ADMIN_EMAIL,
                full_name=ADMIN_FULL_NAME,
                role=ADMIN_ROLE,
                status=UserStatus.active,
                password_hash=hash_password(ADMIN_PASSWORD),
            )
            session.add(user)
            await session.commit()
            print(f"  + created admin user {ADMIN_EMAIL} (role={ADMIN_ROLE.value})")

    print("\n" + "=" * 50)
    print("Done. You can now log in via POST /api/auth/login with:")
    print(f"  email:    {ADMIN_EMAIL}")
    print(f"  password: {ADMIN_PASSWORD}")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nAdmin seed run FAILED: {e}", file=sys.stderr)
        sys.exit(1)