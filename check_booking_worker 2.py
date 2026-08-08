import asyncio
from uuid import UUID
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, WorkerProfile
from sqlalchemy import select

BOOKING_ID = UUID("14d38a97-8f6c-4c8f-8bef-f91fff020f8a")

async def main():
    async with AsyncSessionLocal() as session:
        bres = await session.execute(select(Booking).where(Booking.id == BOOKING_ID))
        booking = bres.scalar_one_or_none()
        if not booking:
            print("Booking not found")
            return
        print(f"Booking worker_id: {booking.worker_id}")

        if booking.worker_id:
            wres = await session.execute(select(WorkerProfile).where(WorkerProfile.id == booking.worker_id))
            worker = wres.scalar_one_or_none()
            if worker:
                print(f"Assigned worker profile id: {worker.id}, user_id: {worker.user_id}")
            else:
                print("No matching WorkerProfile found for that worker_id")
        else:
            print("Booking has no worker assigned at all")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())