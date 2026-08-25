"""Shared business logic for the two guarded visit workflows.

Workflow 1 — Composite Care Package (`material_included=True`): the platform
supplies the procedural kit.
Workflow 2 — Service-Only (`material_included=False`): the patient supplies
their own materials, so the nurse additionally inspects and expiry-checks
them before starting.

Two pieces of logic live here that don't belong in the API layer:

1. `diff_safety_checklists` — the anti-cheat comparison between what the
   nurse self-reported on their pre-procedure questionnaire and what the
   patient/family independently confirmed on their mirrored Safety
   Verification Card. A mismatch (nurse says YES, patient says NO) is a
   `QUALITY_DISCREPANCY_ALERT`, never silently resolved. The two workflows
   ask different questions, so the compared item set is workflow-specific.

2. `generate_invoice` — Step 7, automated invoicing at checkout. Both
   workflows are 0% GST per spec but carry different invoice types;
   anything else falls back to a standard taxable professional-service
   invoice so this stays reusable platform-wide.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Booking, Invoice, VisitRecord

# Workflow 1 — Pre-Procedure Clinical & Intake Questionnaire.
COMPOSITE_SAFETY_CHECKLIST_ITEMS = (
    "hand_hygiene",
    "sterile_gloves",
    "identity_and_wellbeing_check",
    "allergy_and_complaint_history",
    "prescription_and_expiry_check",
)

# Workflow 2 — Pre-Procedure & Patient Supply Inspection. Hand hygiene and
# gloves are common to both; the last three cover the patient's own supplies.
SERVICE_ONLY_SAFETY_CHECKLIST_ITEMS = (
    "hand_hygiene",
    "sterile_gloves",
    "health_condition_check",
    "supply_packaging_intact",
    "supply_expiry_check",
)

# Back-compat alias — Workflow 1 was the original and some callers/tests
# still import this name.
NURSE_SAFETY_CHECKLIST_ITEMS = COMPOSITE_SAFETY_CHECKLIST_ITEMS


def checklist_items_for(material_included: bool) -> tuple:
    """The five yes/no items this booking's workflow asks both sides."""
    return (
        COMPOSITE_SAFETY_CHECKLIST_ITEMS
        if material_included
        else SERVICE_ONLY_SAFETY_CHECKLIST_ITEMS
    )


def diff_safety_checklists(
    nurse_checklist: Dict[str, Any],
    patient_verification: Dict[str, Any],
    material_included: bool = True,
) -> List[str]:
    """Return the list of item keys where the nurse said YES but the patient
    said NO. Per spec, this specific direction of mismatch is the one that
    matters (a nurse under-claiming isn't a safety risk; a nurse
    over-claiming while the patient disagrees is)."""
    mismatched: List[str] = []
    for item in checklist_items_for(material_included):
        nurse_said_yes = bool(nurse_checklist.get(item))
        patient_said_no = patient_verification.get(item) is False
        if nurse_said_yes and patient_said_no:
            mismatched.append(item)
    return mismatched


def is_service_only_workflow(booking: Booking) -> bool:
    """True when this booking went through the Workflow 2 supply guardrail.

    Deliberately narrower than `not booking.material_included`: plain service
    bookings created through the ordinary bookings flow never collected a
    supply confirmation, and must keep their existing taxable invoice
    treatment and straight-to-dispatch routing rather than silently becoming
    GST-exempt and Rx-gated.
    """
    return booking.patient_supply_confirmation is not None


def is_guarded_workflow(booking: Booking) -> bool:
    """True for bookings running either guarded workflow.

    Both gate dispatch behind pharmacist prescription review and both run the
    synchronized nurse/patient safety checklist, so this single predicate is
    what payment routing, dispatch and the Step 4–7 endpoints all branch on.
    """
    return bool(booking.material_included) or is_service_only_workflow(booking)


async def _next_invoice_number(db: AsyncSession) -> str:
    count = (await db.execute(select(func.count()).select_from(Invoice))).scalar_one()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"INV-{today}-{count + 1:06d}"


async def generate_invoice(db: AsyncSession, booking: Booking, visit: VisitRecord) -> Invoice:
    """Idempotently generate (or return the existing) invoice for a
    completed booking.

    Workflow 1 (Composite Care Package) bills 0% GST as a single bundled
    Composite Healthcare Service line. Workflow 2 (Service-Only) bills 0%
    GST as a GST-exempt Paramedical Nursing Service — no materials are sold,
    so nothing on the invoice is taxable."""
    existing = await db.execute(select(Invoice).where(Invoice.booking_id == booking.id))
    invoice = existing.scalar_one_or_none()
    if invoice:
        return invoice

    subtotal = Decimal(booking.total_amount)

    if booking.material_included:
        invoice_type = "composite_healthcare_service"
        gst_percent = Decimal("0")
        tax_amount = Decimal("0")
        total_amount = subtotal
        line_items = [
            {
                "description": "Composite Healthcare Service (Nursing Visit + Procedural Kit)",
                "amount": str(subtotal),
            }
        ]
    elif is_service_only_workflow(booking):
        invoice_type = "paramedical_nursing_service"
        gst_percent = Decimal("0")
        tax_amount = Decimal("0")
        total_amount = subtotal
        line_items = [
            {
                "description": "Paramedical Nursing Service (Procedure Technique Only — GST Exempt)",
                "amount": str(subtotal),
            }
        ]
    else:
        invoice_type = "professional_service"
        gst_percent = Decimal(booking.tax_amount and (booking.tax_amount / subtotal * 100) or 0)
        tax_amount = Decimal(booking.tax_amount or 0)
        total_amount = Decimal(booking.total_amount)
        line_items = [
            {"description": "Professional Nursing Service", "amount": str(booking.base_amount)},
        ]
        if booking.surge_amount:
            line_items.append({"description": "Surge Charge", "amount": str(booking.surge_amount)})
        if tax_amount:
            line_items.append({"description": "GST", "amount": str(tax_amount)})

    invoice = Invoice(
        booking_id=booking.id,
        invoice_number=await _next_invoice_number(db),
        invoice_type=invoice_type,
        gst_percent=gst_percent,
        subtotal_amount=subtotal,
        tax_amount=tax_amount,
        total_amount=total_amount,
        line_items=line_items,
    )
    db.add(invoice)
    await db.flush()
    return invoice
