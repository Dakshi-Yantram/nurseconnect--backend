import asyncio
from app.core.database import engine, Base
from app.models import *  # saare models import karne ke liye

async def create_all():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created successfully!")

asyncio.run(create_all())