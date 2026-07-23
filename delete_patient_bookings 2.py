"""
Cleanup script — permanently deletes ALL bookings (and everything linked
to them: visit records, vitals, escalations, consent, care notes, etc.)
for a given patient name. Use this to wipe out old test data like the
"anandi" bookings that keep showing up in /partner/visits.

⚠️  THIS IS A HARD, IRREVERSIBLE DELETE. Run this only against your
    dev/staging database, never production, unless you are 100% sure.

USAGE (on the server, inside the backend virtualenv, same folder as main.py):

    python delete_patient_bookings.py "anandi"

    # To just preview what WOULD be deleted, without deleting anything:
    python delete_patient_bookings.py "anandi" --dry-run
"""
import asyncio
import sys

from sqlalchemy import select, delete

from app.core.database import AsyncSessionLocal, engine
from app.models.models import (
    Booking,
    Patient,
    VitalSignReading,
    Prescription,
    MedicationAdministration,
    ConsentRecord,
    Escalation,
    FinancialLedger,
    WorkerPayout,
    Dispute,
    Complaint,
    OfflineSyncQueue,
    WorkerLocationLog,
    InsuranceCoverageAssessment,
    CareNote,
)

# Tables that reference bookings.id WITHOUT a DB-level cascade — these must
# be deleted manually before the booking itself.
# (visit_records / visit_checklist_responses / visit_documentation_items
#  DO have ondelete="CASCADE" in the schema, so deleting the booking
#  automatically wipes those — no manual step needed for them.)
DEPENDENT_MODELS = [
    VitalSignReading,
    Prescription,
    MedicationAdministration,
    ConsentRecord,
    Escalation,
    FinancialLedger,
    WorkerPayout,
    Dispute,
    Complaint,
    OfflineSyncQueue,
    WorkerLocationLog,
    InsuranceCoverageAssessment,
    CareNote,
]


async def main():
    if len(sys.argv) < 2:
        print("Usage: python delete_patient_bookings.py <patient_name> [--dry-run]")
        sys.exit(1)

    patient_name = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    async with AsyncSessionLocal() as session:
        # 1. Find matching patients (case-insensitive contains match)
        pres = await session.execute(
            select(Patient).where(Patient.full_name.ilike(f"%{patient_name}%"))
        )
        patients = pres.scalars().all()
        if not patients:
            print(f"No patients found matching '{patient_name}'.")
            return

        print(f"Found {len(patients)} matching patient(s):")
        for p in patients:
            print(f"  - {p.full_name} ({p.id})")

        patient_ids = [p.id for p in patients]

        # 2. Find all bookings for these patients
        bres = await session.execute(
            select(Booking).where(Booking.patient_id.in_(patient_ids))
        )
        bookings = bres.scalars().all()

        if not bookings:
            print("No bookings found for these patients. Nothing to delete.")
            return

        booking_ids = [b.id for b in bookings]
        print(f"\nFound {len(bookings)} booking(s) to delete:")
        for b in bookings:
            print(f"  - {b.booking_ref} | status={b.status.value} | id={b.id}")

        if dry_run:
            print("\n--dry-run set: not deleting anything.")
            return

        confirm = input(f"\nType 'yes' to permanently delete these {len(bookings)} booking(s) and all linked records: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

        # 3. Delete dependent rows first (no DB-level cascade on these)
        for model in DEPENDENT_MODELS:
            result = await session.execute(
                delete(model).where(model.booking_id.in_(booking_ids))
            )
            if result.rowcount:
                print(f"  deleted {result.rowcount} row(s) from {model.__tablename__}")

        # 4. Delete the bookings themselves
        # (visit_records / visit_checklist_responses / visit_documentation_items
        #  cascade automatically at the DB level)
        result = await session.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
        print(f"  deleted {result.rowcount} row(s) from bookings")

        await session.commit()
        print("\nDone. Refresh /partner/visits — those entries should be gone.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())