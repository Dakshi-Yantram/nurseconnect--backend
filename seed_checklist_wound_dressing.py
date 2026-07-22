"""
One-off script to create a checklist template for the WOUND_DRESSING service
and link it via ServiceCatalogue.checklist_template_id.

Why this exists: without a linked ChecklistTemplate, GET /api/care/workflow/
{booking_id} always returns checklist_template: null, so the nurse's "Care
questionnaire" screen shows "No questionnaire configured for this service"
even after a successful OTP check-in. There was previously no seed data and
no admin/trainer UI to author these, so nothing could ever show up here.

This is a stopgap so the questionnaire step of the onboarding -> OTP ->
questionnaire workflow actually has content to display. Long-term, this
content should be authored by a clinical_trainer account and approved by a
reviewer (see UserRole.clinical_trainer / ContentStatus in app/models/enums.py)
once that CRUD surface is built — it doesn't exist yet.

USAGE (from the backend/ folder):

    python seed_checklist_wound_dressing.py
"""
import asyncio

from app.core.database import AsyncSessionLocal, engine
from app.models.enums import ChecklistPhase, ContentStatus
from app.models.models import ChecklistTemplate, ServiceCatalogue
from sqlalchemy import select

TEMPLATE_CODE = "WOUND_DRESSING_CHECKLIST"
SERVICE_CODE = "WOUND_DRESSING"

QUESTIONS = [
    {"id": "wound_size_cm", "type": "number", "text": "Wound size (cm)", "required": True},
    {"id": "signs_of_infection", "type": "boolean", "text": "Any signs of infection?", "required": True},
    {
        "id": "dressing_type",
        "type": "single_select",
        "text": "Dressing type used",
        "required": True,
        "options": ["Gauze", "Hydrocolloid", "Foam", "Other"],
    },
    {"id": "patient_pain_level", "type": "number", "text": "Patient pain level (0-10)", "required": False},
    {"id": "notes", "type": "textarea", "text": "Additional observations", "required": False},
]


async def main():
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(ChecklistTemplate).where(ChecklistTemplate.code == TEMPLATE_CODE))
        ).scalar_one_or_none()
        if existing:
            tpl = existing
            print(f"· checklist template {TEMPLATE_CODE} already exists (id={tpl.id})")
        else:
            tpl = ChecklistTemplate(
                code=TEMPLATE_CODE,
                name="Wound Dressing Visit Checklist",
                service_codes=[SERVICE_CODE],
                phase=ChecklistPhase.all,
                version=1,
                is_active=True,
                status=ContentStatus.published,
                questions=QUESTIONS,
            )
            session.add(tpl)
            await session.commit()
            await session.refresh(tpl)
            print(f"+ created checklist template {TEMPLATE_CODE} (id={tpl.id})")

        svc = (
            await session.execute(select(ServiceCatalogue).where(ServiceCatalogue.service_code == SERVICE_CODE))
        ).scalar_one_or_none()
        if not svc:
            print(f"! service {SERVICE_CODE} not found — run app/seed.py first")
            return
        if svc.checklist_template_id != tpl.id:
            svc.checklist_template_id = tpl.id
            await session.commit()
            print(f"+ linked {SERVICE_CODE} -> checklist template {tpl.id}")
        else:
            print(f"· {SERVICE_CODE} already linked to this template")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
