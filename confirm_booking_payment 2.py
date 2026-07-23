import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking
from sqlalchemy import select

BOOKING_ID = "467ef2b3-55e8-4f74-86d6-bf909efa51c7"

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Booking).where(Booking.id == BOOKING_ID))
        booking = result.scalar_one_or_none()

        if not booking:
            print("Booking not found")
            return

        print(f"Current: status={booking.status}, payment_status={booking.payment_status}")
        booking.payment_status = "captured"
        booking.status = "confirmed"
        await session.commit()
        print(f"Updated: status={booking.status}, payment_status={booking.payment_status}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())