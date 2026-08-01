# create_test_escalation.py
import asyncio
from uuid import uuid4
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, Escalation, WorkerProfile
from app.models.enums import EscalationLevel, EscalationStatus
from sqlalchemy import select

BOOKING_ID = "9b59090f-873a-4912-807f-13402856c3ca"

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Booking).where(Booking.id == BOOKING_ID))
        booking = result.scalar_one_or_none()

        if not booking:
            print("Booking not found")
            return

        # Escalation.worker_id is a required FK — booking has no assigned
        # worker yet, so just grab any existing worker profile for this
        # test row. In real usage this would be the worker who ran the visit.
        wres = await session.execute(select(WorkerProfile).limit(1))
        worker = wres.scalar_one_or_none()

        if not worker:
            print("No worker_profiles exist in the DB at all — need to seed one first.")
            return

        escalation = Escalation(
            id=uuid4(),
            booking_id=booking.id,
            worker_id=worker.id,
            patient_id=booking.patient_id,
            level=EscalationLevel.watch,
            status=EscalationStatus.open,
            trigger_type="manual_test",
            notes="Test escalation created for notifications page verification",
            created_at=datetime.now(timezone.utc),
        )
        session.add(escalation)
        await session.commit()
        print(f"Created escalation {escalation.id} for booking {booking.id} using worker {worker.id}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())