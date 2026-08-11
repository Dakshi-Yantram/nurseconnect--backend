"""Workflow 1 — Composite Care Package (material_included bookings).

Two pieces of business logic live here that don't belong in the API layer:

1. `diff_safety_checklists` — the anti-cheat comparison between what the
   nurse self-reported on the Pre-Procedure Clinical & Intake Questionnaire
   and what the patient/family independently confirmed on their mirrored
   Safety Verification Card. A mismatch (nurse says YES, patient says NO)
   is a `QUALITY_DISCREPANCY_ALERT`, never silently resolved.

2. `generate_invoice` — Step 7, automated invoicing at checkout. Composite
   (material_included) bookings get a single "Composite Healthcare Service"
   line at 0% GST, per spec; other bookings get a standard taxable
   professional-service invoice so this can be reused platform-wide later.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Booking, Invoice, VisitRecord

NURSE_SAFETY_CHECKLIST_ITEMS = (
    "hand_hygiene",
    "sterile_gloves",
    "identity_and_wellbeing_check",
    "allergy_and_complaint_history",
    "prescription_and_expiry_check",
)


def diff_safety_checklists(nurse_checklist: Dict[str, Any], patient_verification: Dict[str, Any]) -> List[str]:
    """Return the list of item keys where the nurse said YES but the patient
    said NO. Per spec, this specific direction of mismatch is the one that
    matters (a nurse under-claiming isn't a safety risk; a nurse
    over-claiming while the patient disagrees is)."""
    mismatched: List[str] = []
    for item in NURSE_SAFETY_CHECKLIST_ITEMS:
        nurse_said_yes = bool(nurse_checklist.get(item))
        patient_said_no = patient_verification.get(item) is False
        if nurse_said_yes and patient_said_no:
            mismatched.append(item)
    return mismatched


async def _next_invoice_number(db: AsyncSession) -> str:
    count = (await db.execute(select(func.count()).select_from(Invoice))).scalar_one()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"INV-{today}-{count + 1:06d}"


async def generate_invoice(db: AsyncSession, booking: Booking, visit: VisitRecord) -> Invoice:
    """Idempotently generate (or return the existing) invoice for a
    completed booking. Composite Care Packages are billed 0% GST as a
    single bundled Composite Healthcare Service line."""
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
