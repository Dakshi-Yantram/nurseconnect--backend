"""
Generic dev/test script — moves bookings stuck at `pending_payment` to
`confirmed` (simulating a successful payment capture), so they become
visible to workers via /worker/new-requests.

USAGE (from backend/ folder):

    # Confirm ALL pending_payment bookings:
    python confirm_payments.py --all

    # Confirm only bookings for a specific patient name:
    python confirm_payments.py "shalini"

    # Preview only, don't change anything:
    python confirm_payments.py --all --dry-run

⚠️  DEV/TEST ONLY — this bypasses the real Razorpay payment capture flow.
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, Patient
from app.models.enums import BookingStatus, PaymentStatus


async def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    confirm_all = "--all" in args
    patient_name = next((a for a in args if not a.startswith("--")), None)

    if not confirm_all and not patient_name:
        print('Usage: python confirm_payments.py --all   (or)   python confirm_payments.py "<patient name>"')
        sys.exit(1)

    async with AsyncSessionLocal() as session:
        query = (
            select(Booking, Patient)
            .join(Patient, Booking.patient_id == Patient.id)
            .where(Booking.status == BookingStatus.pending_payment)
        )
        if patient_name:
            query = query.where(Patient.full_name.ilike(f"%{patient_name}%"))

        res = await session.execute(query)
        rows = res.all()

        if not rows:
            print("No matching pending_payment bookings found.")
            return

        print(f"Found {len(rows)} pending_payment booking(s):")
        for booking, patient in rows:
            print(f"  - {patient.full_name:<15} {booking.booking_ref}  ({booking.id})")

        if dry_run:
            print("\n--dry-run set: not changing anything.")
            return

        for booking, _ in rows:
            booking.payment_status = PaymentStatus.captured
            booking.status = BookingStatus.confirmed

        await session.commit()
        print(f"\nDone. {len(rows)} booking(s) moved to status=confirmed, payment_status=captured.")
        print("Refresh /partner/assignments — they should now show under New Requests.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())