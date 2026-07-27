"""
Generic dev/test script — sets the SAME base_city + home location for
EVERY worker, so the proximity/city-mismatch filter in
/worker/new-requests never hides a booking for anyone, regardless of
which worker ID logs in.

Uses Bangalore coordinates since most of the test bookings in this
project were created with Bangalore as the city.

USAGE (from backend/ folder):

    python set_common_worker_location.py
"""
import asyncio
from decimal import Decimal

from app.core.database import AsyncSessionLocal, engine
from app.models.models import WorkerProfile
from sqlalchemy import select

COMMON_CITY = "Bangalore"
COMMON_LAT = Decimal("12.9716")
COMMON_LNG = Decimal("77.5946")


async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(WorkerProfile))
        workers = res.scalars().all()

        if not workers:
            print("No workers found.")
            return

        for w in workers:
            w.base_city = COMMON_CITY
            w.home_latitude = COMMON_LAT
            w.home_longitude = COMMON_LNG
            w.current_latitude = COMMON_LAT
            # current_longitude field name may vary; set defensively
            if hasattr(w, "current_longitude"):
                w.current_longitude = COMMON_LNG
            print(f"Worker {w.id}: base_city={COMMON_CITY}, location=({COMMON_LAT}, {COMMON_LNG})")

        await session.commit()

    print(f"\nDone. All {len(workers)} worker(s) now share the same location.")
    print("Refresh /partner/assignments — city/distance filtering should no longer hide any booking.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())