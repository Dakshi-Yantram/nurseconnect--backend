# check_consumer_profile.py
import asyncio
from app.core.database import AsyncSessionLocal
from app.models.models import ConsumerProfile
from sqlalchemy import select

async def main():
    user_id = "cf588c0a-1af5-4882-b442-14fdd625f432"
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == user_id))
        profile = res.scalar_one_or_none()
        if profile:
            print("ConsumerProfile found:", profile.id)
        else:
            print("No ConsumerProfile row for this user")

asyncio.run(main())