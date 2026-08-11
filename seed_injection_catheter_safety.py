"""
One-off script that wires up the safety documentation for invasive
micro-visit procedures: Injection (IM/IV) and Urinary Catheter Change.

For each of these two services this creates/links a DocumentationTemplate
with three fields, all rendered by the generic care-workflow engine
(app/services/care_workflow_engine.py) — no per-service hardcoding in the
API layer:

  1. `procedure_site_photo` (type=photo)
     The nurse must take and upload a photo of the injection/catheter site
     before completing the visit. Required, blocks checkout.

  2. `packaging_integrity_check` (type=packaging_integrity_check)
     The nurse must confirm — per consumable — that the patient-provided
     item is sealed and within its expiry date, and capture one photo of
     the laid-out packaging as evidence. Required, blocks checkout.

  3. `material_availability` (type=material_check)
     The nurse confirms whether the required consumables are on hand. If
     not, the frontend auto-generates a shopping list from this field's
     `required_materials` metadata and prompts the nurse to capture a photo
     of the doctor's prescription. Required, blocks checkout (so the visit
     can't be silently checked out without either the materials being
     available or a prescription on file for follow-up).

A companion ChecklistTemplate is also seeded with a couple of pre-procedure
safety questions, mirroring the existing WOUND_DRESSING_CHECKLIST pattern.

USAGE (from the backend/ folder):

    python seed_injection_catheter_safety.py
"""
import asyncio
from decimal import Decimal

from app.core.database import AsyncSessionLocal, engine
from app.models.enums import (
    BillingTrigger,
    ChecklistPhase,
    ContentStatus,
    ServiceCategory,
    ServiceRiskLevel,
    WorkerTier,
)
from app.models.models import ChecklistTemplate, DocumentationTemplate, ServiceCatalogue
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Service-specific consumable lists. Kept as plain data so a clinical trainer
# can later edit these via the template-authoring UI without touching code.
# ---------------------------------------------------------------------------
INJECTION_CONSUMABLES = ["Sterile syringe", "Alcohol swab", "Prescribed injection vial/ampoule", "Cotton/gauze swab"]
INJECTION_MATERIALS = [
    {"name": "Sterile syringe (appropriate size)", "qty": "1-2 pcs"},
    {"name": "Alcohol swabs", "qty": "1 pack"},
    {"name": "Prescribed injection vial/ampoule", "qty": "as prescribed"},
    {"name": "Cotton/gauze swabs", "qty": "small pack"},
    {"name": "Sharps disposal container", "qty": "1"},
]

CATHETER_CONSUMABLES = ["Sterile catheter kit", "Sterile gloves", "Antiseptic solution", "Catheter drainage bag", "Lubricating gel"]
CATHETER_MATERIALS = [
    {"name": "Sterile urinary catheter (correct size)", "qty": "1"},
    {"name": "Sterile gloves", "qty": "1 pair"},
    {"name": "Antiseptic solution (e.g. povidone-iodine)", "qty": "1 bottle"},
    {"name": "Catheter drainage bag", "qty": "1"},
    {"name": "Sterile lubricating gel", "qty": "1 sachet"},
    {"name": "Sterile drape", "qty": "1"},
]


def _doc_fields(procedure_label: str, consumables: list[str], materials: list[dict]) -> list[dict]:
    return [
        {
            "field_id": "procedure_site_photo",
            "type": "photo",
            "label": f"Photo of the {procedure_label} site (before and/or after)",
            "required": True,
            "blocks_checkout": True,
        },
        {
            "field_id": "packaging_integrity_check",
            "type": "packaging_integrity_check",
            "label": "Verify each consumable is sealed and within its expiry date, then photograph the packaging",
            "required": True,
            "blocks_checkout": True,
            "consumables": consumables,
        },
        {
            "field_id": "material_availability",
            "type": "material_check",
            "label": "Are all required consumables available with the patient/family?",
            "required": True,
            "blocks_checkout": True,
            "required_materials": materials,
        },
        {
            "field_id": "family_summary",
            "type": "textarea",
            "label": "Summary for the family",
            "required": False,
            "blocks_checkout": False,
        },
    ]


CHECKLIST_QUESTIONS = [
    {"id": "patient_consent", "type": "consent_confirmation", "text": "Patient/family consent obtained for the procedure", "required": True},
    {"id": "hand_hygiene_done", "type": "boolean", "text": "Hand hygiene / aseptic precautions completed before starting", "required": True},
    {"id": "notes", "type": "textarea", "text": "Additional clinical observations", "required": False},
]

SERVICES = [
    dict(
        service_code="INJECTION_IM_IV",
        checklist_code="INJECTION_SAFETY_CHECKLIST",
        checklist_name="Injection (IM/IV) Safety Checklist",
        doc_code="INJECTION_SAFETY_DOC",
        doc_name="Injection (IM/IV) Safety Documentation",
        procedure_label="injection",
        consumables=INJECTION_CONSUMABLES,
        materials=INJECTION_MATERIALS,
        create_service=False,  # already seeded in app/seed.py
    ),
    dict(
        service_code="CATHETER_CHANGE",
        checklist_code="CATHETER_CHANGE_SAFETY_CHECKLIST",
        checklist_name="Urinary Catheter Change Safety Checklist",
        doc_code="CATHETER_CHANGE_SAFETY_DOC",
        doc_name="Urinary Catheter Change Safety Documentation",
        procedure_label="catheter change",
        consumables=CATHETER_CONSUMABLES,
        materials=CATHETER_MATERIALS,
        create_service=True,  # new service — didn't exist before
        service_kwargs=dict(
            name="Urinary Catheter Change",
            description="Replacement of an indwelling urinary catheter by a qualified nurse, with sterile technique.",
            category=ServiceCategory.micro_visit,
            min_tier=WorkerTier.tier2,
            duration_minutes=30,
            base_price=Decimal("449"),
            commission_pct=Decimal("20"),
            requires_prescription=True,
            billing_trigger=BillingTrigger.on_completion,
            insurance_covered=True,
            icon="droplet",
        ),
    ),
]


async def main():
    async with AsyncSessionLocal() as session:
        for cfg in SERVICES:
            svc = (
                await session.execute(select(ServiceCatalogue).where(ServiceCatalogue.service_code == cfg["service_code"]))
            ).scalar_one_or_none()

            if not svc and cfg.get("create_service"):
                svc = ServiceCatalogue(service_code=cfg["service_code"], risk_level=ServiceRiskLevel.MEDIUM, **cfg["service_kwargs"])
                session.add(svc)
                await session.commit()
                await session.refresh(svc)
                print(f"+ created service {cfg['service_code']} (id={svc.id})")
            elif not svc:
                print(f"! service {cfg['service_code']} not found — run app/seed.py first, skipping")
                continue
            else:
                if svc.risk_level != ServiceRiskLevel.MEDIUM and svc.risk_level == ServiceRiskLevel.LOW:
                    svc.risk_level = ServiceRiskLevel.MEDIUM
                    print(f"  · bumped {cfg['service_code']} risk_level -> MEDIUM (invasive procedure)")
                print(f"· service {cfg['service_code']} already exists (id={svc.id})")

            # --- checklist template ---
            ctpl = (
                await session.execute(select(ChecklistTemplate).where(ChecklistTemplate.code == cfg["checklist_code"]))
            ).scalar_one_or_none()
            if not ctpl:
                ctpl = ChecklistTemplate(
                    code=cfg["checklist_code"],
                    name=cfg["checklist_name"],
                    service_codes=[cfg["service_code"]],
                    phase=ChecklistPhase.all,
                    version=1,
                    is_active=True,
                    status=ContentStatus.published,
                    questions=CHECKLIST_QUESTIONS,
                )
                session.add(ctpl)
                await session.commit()
                await session.refresh(ctpl)
                print(f"+ created checklist template {cfg['checklist_code']} (id={ctpl.id})")
            else:
                print(f"· checklist template {cfg['checklist_code']} already exists (id={ctpl.id})")

            # --- documentation template ---
            dtpl = (
                await session.execute(select(DocumentationTemplate).where(DocumentationTemplate.template_code == cfg["doc_code"]))
            ).scalar_one_or_none()
            fields = _doc_fields(cfg["procedure_label"], cfg["consumables"], cfg["materials"])
            if not dtpl:
                dtpl = DocumentationTemplate(
                    template_code=cfg["doc_code"],
                    name=cfg["doc_name"],
                    version=1,
                    is_active=True,
                    mandatory_fields=fields,
                    photo_consent_required=True,
                    wound_image_mandatory=False,
                    status=ContentStatus.published,
                )
                session.add(dtpl)
                await session.commit()
                await session.refresh(dtpl)
                print(f"+ created documentation template {cfg['doc_code']} (id={dtpl.id})")
            else:
                print(f"· documentation template {cfg['doc_code']} already exists (id={dtpl.id})")

            # --- link ---
            changed = False
            if svc.checklist_template_id != ctpl.id:
                svc.checklist_template_id = ctpl.id
                changed = True
            if svc.documentation_template_id != dtpl.id:
                svc.documentation_template_id = dtpl.id
                changed = True
            if changed:
                await session.commit()
                print(f"+ linked {cfg['service_code']} -> checklist {ctpl.id}, documentation {dtpl.id}")
            else:
                print(f"· {cfg['service_code']} already linked")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
