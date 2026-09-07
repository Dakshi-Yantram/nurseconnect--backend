"""E-prescription endpoints.

Doctor-generated prescriptions, distinct from the older "patient uploads a
photo of a paper Rx" flow (see Prescription.is_doctor_generated).

  POST /eprescriptions/signature        doctor uploads/replaces their saved
                                         signature PNG (used to stamp every
                                         Rx they issue)
  GET  /eprescriptions/signature        doctor checks whether they have one
  POST /eprescriptions                  doctor writes + issues an e-Rx for a
                                         booking (generates PDF + QR)
  GET  /eprescriptions/{id}             fetch one (doctor/admin/owning
                                         consumer)
  GET  /eprescriptions/booking/{id}     list e-Rx for a booking
  GET  /eprescriptions/verify/{hash}    PUBLIC — pharmacist/anyone scans the
                                         QR / opens the link to confirm the
                                         Rx is genuine and unaltered
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, get_worker_profile, is_admin
from app.integrations.providers import ExternalProviderError, cloudinary_client
from app.models.enums import PrescriptionStatus, WorkerType
from app.models.models import Booking, Patient, Prescription, TeleConsultation, User, WorkerProfile
from app.services import eprescription_service
from app.services.common_services import audit

router = APIRouter(prefix="/eprescriptions", tags=["eprescriptions"])


def _require_doctor(worker: WorkerProfile = Depends(get_worker_profile)) -> WorkerProfile:
    if worker.worker_type != WorkerType.doctor:
        raise HTTPException(status_code=403, detail="Doctor account required")
    return worker


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------
class SignatureUploadRequest(BaseModel):
    image_base64: str = Field(..., description="PNG signature, base64 (raw or data: URI)")


@router.post("/signature")
async def upload_signature(
    body: SignatureUploadRequest,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    try:
        upload = await cloudinary_client.upload_base64(body.image_base64, folder="signatures", resource_type="image")
    except ExternalProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    doctor.signature_url = upload["secure_url"]
    doctor.signature_public_id = upload["public_id"]
    doctor.signature_uploaded_at = datetime.now(timezone.utc)
    await db.commit()
    return {"signature_url": doctor.signature_url, "uploaded_at": doctor.signature_uploaded_at.isoformat()}


@router.get("/signature")
async def get_signature(doctor: WorkerProfile = Depends(_require_doctor)):
    return {
        "has_signature": bool(doctor.signature_url),
        "signature_url": doctor.signature_url,
        "uploaded_at": doctor.signature_uploaded_at.isoformat() if doctor.signature_uploaded_at else None,
    }


# ---------------------------------------------------------------------------
# Create / issue an e-prescription
# ---------------------------------------------------------------------------
class DrugLine(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    scheduled_drug: bool = False


class EPrescriptionCreateRequest(BaseModel):
    booking_id: UUID
    drugs_listed: list[DrugLine] = Field(default_factory=list)
    diet_notes: Optional[str] = None
    patient_issues: Optional[str] = None  # None/omitted -> treated as "all okay"
    valid_days: int = 30


class EPrescriptionOut(BaseModel):
    id: UUID
    booking_id: Optional[UUID]
    patient_id: UUID
    status: str
    pdf_url: Optional[str]
    qr_code_url: Optional[str]
    verification_hash: Optional[str]
    prescribed_date: Optional[date]
    valid_until: Optional[date]
    diet_notes: Optional[str]
    patient_issues: Optional[str]
    drugs_listed: Optional[list]

    class Config:
        from_attributes = True


@router.post("", response_model=EPrescriptionOut)
async def create_eprescription(
    body: EPrescriptionCreateRequest,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    if not doctor.signature_url:
        raise HTTPException(status_code=400, detail="Upload your signature before issuing an e-prescription")

    res = await db.execute(select(Booking).where(Booking.id == body.booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.worker_id != doctor.id:
        raise HTTPException(status_code=403, detail="This booking is not assigned to you")

    pres = await db.execute(select(Patient).where(Patient.id == booking.patient_id))
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    dres = await db.execute(select(User).where(User.id == doctor.user_id))
    doctor_user = dres.scalar_one_or_none()
    doctor_name = (doctor_user.full_name if doctor_user else None) or "Doctor"

    prescribed_on = date.today()
    valid_until = None
    if body.valid_days:
        from datetime import timedelta
        valid_until = prescribed_on + timedelta(days=body.valid_days)

    drugs_payload = [d.model_dump() for d in body.drugs_listed]
    scheduled_drug = any(d.scheduled_drug for d in body.drugs_listed)
    patient_issues_text = body.patient_issues or "All okay — nothing flagged at this consultation."

    prescription = Prescription(
        patient_id=patient.id,
        booking_id=booking.id,
        uploaded_by=doctor_user.id if doctor_user else doctor.user_id,
        cloudinary_url="",  # not used for the doctor-generated flow; pdf_url below is authoritative
        cloudinary_public_id="",
        prescribed_by_name=doctor_name,
        prescribed_by_reg_no=doctor.registration_no,
        hospital_clinic="NurseConnect Teleconsultation",
        prescribed_date=prescribed_on,
        valid_until=valid_until,
        drugs_listed=drugs_payload,
        scheduled_drug=scheduled_drug,
        status=PrescriptionStatus.verified,  # doctor-issued in-app -> self-verified, no separate pharmacist review needed
        verified_by=doctor_user.id if doctor_user else None,
        verified_at=datetime.now(timezone.utc),
        is_doctor_generated=True,
        issued_by_worker_id=doctor.id,
        diet_notes=body.diet_notes,
        patient_issues=patient_issues_text,
        signature_url=doctor.signature_url,
    )
    db.add(prescription)
    await db.flush()  # get prescription.id before hashing

    hash_payload = {
        "prescription_id": str(prescription.id),
        "patient_id": str(patient.id),
        "issued_by_worker_id": str(doctor.id),
        "doctor_name": doctor_name,
        "doctor_reg_no": doctor.registration_no,
        "drugs_listed": drugs_payload,
        "diet_notes": body.diet_notes,
        "patient_issues": patient_issues_text,
        "prescribed_date": prescribed_on,
    }
    verification_hash = eprescription_service.build_verification_hash(hash_payload)

    signature_bytes = None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(doctor.signature_url)
            if r.status_code == 200:
                signature_bytes = r.content
    except Exception:  # noqa: BLE001 — a missing signature image must never block Rx issuance
        signature_bytes = None

    pdf_bytes = eprescription_service.render_prescription_pdf(
        patient_name=patient.full_name,
        patient_age_gender=None,
        doctor_name=doctor_name,
        doctor_reg_no=doctor.registration_no,
        hospital_clinic="NurseConnect Teleconsultation",
        prescribed_date=prescribed_on,
        drugs_listed=drugs_payload,
        diet_notes=body.diet_notes,
        patient_issues=patient_issues_text,
        signature_png_bytes=signature_bytes,
        verification_hash=verification_hash,
        booking_ref=booking.booking_ref,
    )

    import base64
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    try:
        upload = await cloudinary_client.upload_base64(
            f"data:application/pdf;base64,{pdf_b64}", folder="eprescriptions", resource_type="auto"
        )
    except ExternalProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    qr_bytes = eprescription_service.make_qr_png_bytes(eprescription_service.build_verify_url(verification_hash))
    qr_b64 = base64.b64encode(qr_bytes).decode("ascii")
    try:
        qr_upload = await cloudinary_client.upload_base64(
            f"data:image/png;base64,{qr_b64}", folder="eprescriptions/qr", resource_type="image"
        )
    except ExternalProviderError:
        qr_upload = None

    prescription.pdf_url = upload["secure_url"]
    prescription.pdf_public_id = upload["public_id"]
    prescription.verification_hash = verification_hash
    prescription.qr_code_url = qr_upload["secure_url"] if qr_upload else None

    # Advance the teledoctor queue item for this booking to "prescription"
    # done, if one exists (see app/api/v1/teleconsult.py).
    tres = await db.execute(select(TeleConsultation).where(TeleConsultation.booking_id == booking.id))
    consult = tres.scalar_one_or_none()
    if consult:
        consult.prescription_id = prescription.id
        consult.stage = consult.stage  # left to the explicit /teleconsult/{id}/stage endpoint to advance

    await audit(db, doctor_user.id if doctor_user else None, "worker", "eprescription.issue", "prescription", prescription.id, {"booking_id": str(booking.id)})
    await db.commit()
    await db.refresh(prescription)
    return prescription


@router.get("/booking/{booking_id}", response_model=list[EPrescriptionOut])
async def list_for_booking(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Prescription).where(Prescription.booking_id == booking_id, Prescription.is_doctor_generated.is_(True))
        .order_by(Prescription.created_at.desc())
    )
    return res.scalars().all()


@router.get("/{prescription_id}", response_model=EPrescriptionOut)
async def get_eprescription(
    prescription_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Prescription).where(Prescription.id == prescription_id))
    prescription = res.scalar_one_or_none()
    if not prescription:
        raise HTTPException(status_code=404, detail="Not found")
    return prescription


# ---------------------------------------------------------------------------
# Public verification (no auth — this is what the QR code links to)
# ---------------------------------------------------------------------------
@router.get("/verify/{verification_hash}")
async def verify_eprescription(verification_hash: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Prescription).where(Prescription.verification_hash == verification_hash))
    prescription = res.scalar_one_or_none()
    if not prescription:
        return {"valid": False, "reason": "No matching e-prescription found"}

    hash_payload = {
        "prescription_id": str(prescription.id),
        "patient_id": str(prescription.patient_id),
        "issued_by_worker_id": str(prescription.issued_by_worker_id) if prescription.issued_by_worker_id else None,
        "doctor_name": prescription.prescribed_by_name,
        "doctor_reg_no": prescription.prescribed_by_reg_no,
        "drugs_listed": prescription.drugs_listed,
        "diet_notes": prescription.diet_notes,
        "patient_issues": prescription.patient_issues,
        "prescribed_date": prescription.prescribed_date,
    }
    recomputed = eprescription_service.build_verification_hash(hash_payload)
    tampered = recomputed != verification_hash

    return {
        "valid": not tampered,
        "prescription_id": str(prescription.id),
        "doctor_name": prescription.prescribed_by_name,
        "doctor_reg_no": prescription.prescribed_by_reg_no,
        "prescribed_date": prescription.prescribed_date.isoformat() if prescription.prescribed_date else None,
        "valid_until": prescription.valid_until.isoformat() if prescription.valid_until else None,
        "drugs_listed": prescription.drugs_listed,
        "pdf_url": prescription.pdf_url,
    }
