import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User, WorkerProfile
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User))
        users = res.scalars().all()
        print(f"Found {len(users)} users:\n")
        for u in users:
            print(f"id={u.id} | email={u.email} | role={u.role}")

        print("\nWorker profiles:")
        wres = await session.execute(select(WorkerProfile))
        for w in wres.scalars():
            print(f"worker_id={w.id} | user_id={w.user_id}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())