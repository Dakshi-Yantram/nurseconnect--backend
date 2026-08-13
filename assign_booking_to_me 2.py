import asyncio
from uuid import UUID
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, WorkerProfile, User
from sqlalchemy import select

BOOKING_ID = UUID("14d38a97-8f6c-4c8f-8bef-f91fff020f8a")

# Change this to match whichever account you're logging in as in the browser
USER_EMAIL = "testworker@yantram.com" # <-- update this to your actual login email

async def main():
    async with AsyncSessionLocal() as session:
        # Find the user
        ures = await session.execute(select(User).where(User.email == USER_EMAIL))
        user = ures.scalar_one_or_none()
        if not user:
            print(f"No user found with email {USER_EMAIL}")
            return
        print(f"Found user: id={user.id}, role={user.role}")

        # Find their worker profile
        wres = await session.execute(select(WorkerProfile).where(WorkerProfile.user_id == user.id))
        worker = wres.scalar_one_or_none()
        if not worker:
            print("This user has no WorkerProfile — can't assign bookings to them.")
            return
        print(f"Found worker profile: id={worker.id}")

        # Assign booking to this worker
        bres = await session.execute(select(Booking).where(Booking.id == BOOKING_ID))
        booking = bres.scalar_one_or_none()
        if not booking:
            print("Booking not found")
            return

        booking.worker_id = worker.id
        await session.commit()
        print(f"Booking {BOOKING_ID} now assigned to worker {worker.id}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())