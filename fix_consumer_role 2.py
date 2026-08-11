# fix_consumer_role.py
import asyncio
from app.core.database import AsyncSessionLocal
from app.models.models import User
from app.models.enums import UserRole
from sqlalchemy import select

async def main():
    user_id = "d7e3698e-e162-4f36-af3b-4dbe8cb4cd54"
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.id == user_id))
        user = res.scalar_one_or_none()
        print("Current role:", user.role)
        user.role = UserRole.consumer
        await db.commit()
        print("Updated role to consumer")

asyncio.run(main())