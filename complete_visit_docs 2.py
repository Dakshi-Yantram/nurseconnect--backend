import asyncio
from uuid import UUID
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Booking, VisitRecord, ServiceCatalogue, ChecklistTemplate, DocumentationTemplate
from app.services.care_workflow_engine import (
    resolve_workflow_for_booking,
    validate_checklist_response,
    validate_documentation_payload,
    upsert_checklist_response,
    upsert_documentation_item,
)
from sqlalchemy import select

BOOKING_ID = UUID("467ef2b3-55e8-4f74-86d6-bf909efa51c7")

CHECKLIST_ANSWERS = {
    "patient_consent": True,
    "sterile_setup": True,
    "wound_assessment": "Wound size 3cm, minimal exudate, no foul odour",
    "infection_signs": ["none"],
    "dressing_type": "Hydrocolloid dressing",
}

DOC_ANSWERS = {
    "wound_photo": "https://via.placeholder.com/400x300.png?text=Wound+Photo",
    "family_summary": "Visit completed for Mr. Test Patient. BP 130/85, Pulse 78, SpO2 98%. Patient comfortable.",
}

async def main():
    async with AsyncSessionLocal() as session:
        bres = await session.execute(select(Booking).where(Booking.id == BOOKING_ID))
        booking = bres.scalar_one_or_none()
        if not booking:
            print("Booking not found")
            return

        vres = await session.execute(select(VisitRecord).where(VisitRecord.booking_id == BOOKING_ID))
        visit = vres.scalar_one_or_none()

        wf = await resolve_workflow_for_booking(BOOKING_ID, session)
        print(f"Workflow source: {wf.source}, checklist_template: {wf.checklist_template}, doc_template: {wf.documentation_template}")

        if wf.checklist_template:
            for qid, answer in CHECKLIST_ANSWERS.items():
                validated, _ = validate_checklist_response({"question_id": qid, "answer": answer}, wf.checklist_template)
                await upsert_checklist_response(
                    session, booking=booking, visit=visit, worker_id=booking.worker_id,
                    template=wf.checklist_template, validated=validated,
                )
                print(f"  checklist '{qid}' -> completed={validated['is_completed']}")

        if wf.documentation_template:
            for fid, answer in DOC_ANSWERS.items():
                if fid == "wound_photo":
                    payload = {"field_id": fid, "file_url": answer}
                else:
                    payload = {"field_id": fid, "value": answer}
                validated = validate_documentation_payload(payload, wf.documentation_template)
                await upsert_documentation_item(
                    session, booking=booking, visit=visit, worker_id=booking.worker_id,
                    template=wf.documentation_template, validated=validated,
                )
                print(f"  doc '{fid}' -> completed={validated['is_completed']}")

        await session.commit()
        print("Done.")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())