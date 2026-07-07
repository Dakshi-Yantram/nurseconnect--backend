import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from sqlalchemy import select

PHONE = "+919999000003"

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.phone_e164 == PHONE))
        user = result.scalar_one_or_none()

        if not user:
            print(f"No user found with phone {PHONE}")
        else:
            print(f"Found user: id={user.id}, email={user.email}, full_name={user.full_name}, role={user.role}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())