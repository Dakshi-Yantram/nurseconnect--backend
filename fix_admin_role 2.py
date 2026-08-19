# fix_admin_role.py
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from app.models.enums import UserRole
from sqlalchemy import select

USER_ID = "d7e3698e-e162-4f36-af3b-4dbe8cb4cd54"  # Dakshi Gupta

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == USER_ID))
        user = result.scalar_one_or_none()

        if not user:
            print("User not found")
            return

        print(f"Current role: {user.role}")
        user.role = UserRole.admin_super
        await session.commit()
        print(f"Updated role: {user.role}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())