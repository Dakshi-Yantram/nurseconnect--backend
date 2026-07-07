import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from app.core.security import hash_password
from sqlalchemy import select

USER_EMAIL = "testworker@yantram.com"
NEW_PASSWORD = "Test@1234"

async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.email == USER_EMAIL))
        user = res.scalar_one_or_none()
        if not user:
            print("User not found")
            return
        user.password_hash = hash_password(NEW_PASSWORD)
        await session.commit()
        print(f"Password for {USER_EMAIL} reset to: {NEW_PASSWORD}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())