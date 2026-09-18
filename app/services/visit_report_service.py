"""Generates the visit care-summary PDF and stores it, on demand.

Mirrors the shape of `billing_service.py`'s document handling: this module
never decides *who* may see which view -- the caller (the visits API) has
already established that via `get_worker_profile` / `get_consumer_profile`
and passes in `include_clinical_notes` accordingly. This module only turns
already-authorized data into a PDF and a URL.

Generated on demand rather than cached on the visit row: unlike an invoice
(a legal document that must not be reissued differently), a care summary
can be regenerated freely as the nurse edits her report, so there is no
correctness reason to persist a URL that could go stale.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company import get_company
from app.integrations.providers import ExternalProviderError, cloudinary_client
from app.models.models import Patient, User, VisitRecord, VitalSignReading, WorkerProfile
from app.services.visit_report_pdf import render_visit_report_pdf

logger = logging.getLogger(__name__)


async def _latest_vitals(db: AsyncSession, booking_id: UUID) -> Optional[dict]:
    res = await db.execute(
        select(VitalSignReading)
        .where(VitalSignReading.booking_id == booking_id)
        .order_by(VitalSignReading.recorded_at.desc())
        .limit(1)
    )
    v = res.scalar_one_or_none()
    if v is None:
        return None
    return {
        "bp_systolic": v.bp_systolic,
        "bp_diastolic": v.bp_diastolic,
        "pulse": v.pulse,
        "spo2": v.spo2,
        "temperature_f": float(v.temperature_f) if v.temperature_f is not None else None,
    }


async def _upload_pdf(pdf_bytes: bytes, filename: str) -> Optional[str]:
    try:
        payload = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
        result = await cloudinary_client.upload_base64(
            payload, folder="visit-reports", resource_type="auto"
        )
        return result.get("secure_url")
    except (ExternalProviderError, Exception) as exc:  # noqa: BLE001
        logger.warning("Visit report PDF upload failed for %s: %s", filename, exc)
        return None


async def generate_visit_report_pdf_url(
    db: AsyncSession,
    visit: VisitRecord,
    booking_ref: str,
    *,
    include_clinical_notes: bool,
) -> Optional[str]:
    """Render the visit's care summary and return a hosted PDF URL.

    `include_clinical_notes` gates only the clinical-notes section of the
    PDF; the summary text shown is always the family summary, since that is
    the one field meant to read as a finished write-up on both views (the
    nurse's clinical notes are her separate working record, shown as its
    own section only on her copy).
    """
    pres = await db.execute(select(Patient).where(Patient.id == visit.patient_id))
    patient = pres.scalar_one_or_none()

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == visit.worker_id))
    worker = wres.scalar_one_or_none()
    nurse_name = "\u2014"
    if worker is not None:
        ures = await db.execute(select(User).where(User.id == worker.user_id))
        user = ures.scalar_one_or_none()
        nurse_name = (user.full_name if user and user.full_name else None) or "\u2014"

    vitals = await _latest_vitals(db, visit.booking_id)

    pdf_bytes = render_visit_report_pdf(
        company=get_company(),
        booking_ref=booking_ref,
        patient_name=(patient.full_name if patient else "\u2014"),
        nurse_name=nurse_name,
        nurse_council_no=(worker.registration_no if worker else None),
        check_in_at=visit.check_in_at,
        check_out_at=visit.check_out_at,
        duration_minutes=visit.actual_duration_minutes,
        vitals=vitals,
        summary_text=visit.family_summary,
        summary_label="Summary for the family",
        include_clinical_notes=include_clinical_notes,
        care_notes=visit.care_notes if include_clinical_notes else None,
    )
    return await _upload_pdf(pdf_bytes, f"visit-report-{visit.id}")
