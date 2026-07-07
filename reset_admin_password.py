import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models.models import User
from sqlalchemy import select

ADMIN_EMAIL = "ops@example.com"
NEW_PASSWORD = "Admin@1234"

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == ADMIN_EMAIL))
        user = result.scalar_one_or_none()

        if not user:
            print(f"No user found with email {ADMIN_EMAIL}")
            return

        user.password_hash = hash_password(NEW_PASSWORD)
        await session.commit()
        print(f"Password updated for {ADMIN_EMAIL} -> {NEW_PASSWORD}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())