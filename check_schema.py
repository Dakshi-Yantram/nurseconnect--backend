"""
Run this ON THE PRODUCTION SERVER (same env as the app) to see the REAL
traceback behind the 500, instead of guessing.
"""
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Patient, ConsumerProfile, User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        try:
            res = await session.execute(
                select(Patient, ConsumerProfile, User)
                .join(ConsumerProfile, ConsumerProfile.id == Patient.consumer_id)
                .join(User, User.id == ConsumerProfile.user_id)
            )
            print("PATIENTS query OK, rows:", len(res.all()))
        except Exception as e:
            print("PATIENTS query FAILED:")
            print(repr(e))

        try:
            res = await session.execute(
                select(ConsumerProfile, User).join(User, User.id == ConsumerProfile.user_id)
            )
            print("CONSUMERS query OK, rows:", len(res.all()))
        except Exception as e:
            print("CONSUMERS query FAILED:")
            print(repr(e))
    await engine.dispose()

asyncio.run(main())