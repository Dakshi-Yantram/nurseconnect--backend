"""
Diagnostic script — prints the REAL backend status, worker_id, and patient
for every booking, so we can see exactly why "shalini" / "anil" bookings
aren't showing up in /worker/new-requests.

USAGE (from backend/ folder):

    python check_booking_status.py
"""
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, Patient
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Booking, Patient)
            .join(Patient, Booking.patient_id == Patient.id)
            .order_by(Booking.created_at.desc())
        )
        rows = res.all()

        if not rows:
            print("No bookings found at all.")
            return

        print(f"{'patient':<15} {'status':<18} {'worker_id':<38} {'booking_ref':<15} id")
        print("-" * 110)
        for booking, patient in rows:
            worker_id = str(booking.worker_id) if booking.worker_id else "— (unassigned)"
            print(f"{patient.full_name:<15} {booking.status.value:<18} {worker_id:<38} {booking.booking_ref:<15} {booking.id}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())