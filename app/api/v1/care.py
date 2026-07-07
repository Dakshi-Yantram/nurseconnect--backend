"""Care: ABHA records, consent, prescriptions.

Patch 5A — RBAC hardening on consent endpoints.

A consent record is visible / mutable only to:
  * The consumer who owns the patient
  * The worker currently assigned to that patient's booking
  * admin_clinical / admin_super / admin_ops
"""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, is_admin
from app.integrations import abha_client
from app.models.enums import ConsentStatus, UserRole
from app.models.models import (
    AbhaRecord,
    Booking,
    ConsentRecord,
    ConsumerProfile,
    Patient,
    WorkerProfile,
)
from app.schemas.schemas import (
    AbhaRecordCreate,
    AbhaRecordOut,
    ConsentCreate,
)

router = APIRouter(tags=["care"])


async def _patient_ownership_check(
    db: AsyncSession, current: CurrentUser, patient_id: UUID
) -> Patient:
    """Patch 5A — RBAC ownership check used by consent + ABHA endpoints.

    Admin roles bypass. Workers only have access if they're the assigned
    worker on at least one of the patient's bookings. Consumers must own
    the patient.
    """
    pres = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if is_admin(current.role):
        return patient
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one_or_none()
        if not cp or patient.consumer_id != cp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return patient
    if current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one_or_none()
        if not wp:
            raise HTTPException(status_code=403, detail="Forbidden")
        b_res = await db.execute(
            select(Booking).where(Booking.patient_id == patient.id, Booking.worker_id == wp.id).limit(1)
        )
        if b_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=403, detail="Forbidden")
        return patient
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/abha-records", response_model=AbhaRecordOut)
async def upload_abha_record(payload: AbhaRecordCreate, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    patient = await _patient_ownership_check(db, current, payload.patient_id)
    rec = AbhaRecord(
        patient_id=patient.id,
        record_type=payload.record_type,
        title=payload.title,
        cloudinary_url=payload.cloudinary_url,
        cloudinary_public_id=payload.cloudinary_public_id,
        abha_health_id=payload.abha_health_id,
        issued_on=payload.issued_on,
        issued_by=payload.issued_by,
        metadata_json=payload.metadata_json,
        uploaded_by=current.id,
    )
    if payload.abha_health_id:
        link = await abha_client.link_health_id(payload.abha_health_id, {"patient_id": str(patient.id)})
        rec.is_synced_with_abha = bool(link.get("linked"))
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return AbhaRecordOut.model_validate(rec)


@router.get("/abha-records/patient/{patient_id}", response_model=List[AbhaRecordOut])
async def list_abha_records(patient_id: UUID, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    await _patient_ownership_check(db, current, patient_id)
    res = await db.execute(select(AbhaRecord).where(AbhaRecord.patient_id == patient_id).order_by(AbhaRecord.created_at.desc()))
    return [AbhaRecordOut.model_validate(r) for r in res.scalars().all()]


@router.post("/consents")
async def record_consent(payload: ConsentCreate, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _patient_ownership_check(db, current, payload.patient_id)
    rec = ConsentRecord(
        patient_id=payload.patient_id,
        booking_id=payload.booking_id,
        consent_type=payload.consent_type,
        consented_by_user_id=current.id,
        consented_by_name=payload.consented_by_name,
        relationship_to_patient=payload.relationship_to_patient,
        capture_method=payload.capture_method,
        consent_text_version=payload.consent_text_version,
        consent_text_hash=payload.consent_text_hash,
        status=ConsentStatus.given,
        given_at=datetime.now(timezone.utc),
        expires_at=payload.expires_at,
        is_offline_captured=payload.is_offline_captured,
        synced_at=None if payload.is_offline_captured else datetime.now(timezone.utc),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return {"id": str(rec.id), "status": rec.status.value, "given_at": rec.given_at.isoformat()}


@router.get("/consents/patient/{patient_id}")
async def list_consents(patient_id: UUID, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    await _patient_ownership_check(db, current, patient_id)
    res = await db.execute(select(ConsentRecord).where(ConsentRecord.patient_id == patient_id).order_by(ConsentRecord.given_at.desc()))
    items = res.scalars().all()
    return [
        {
            "id": str(r.id),
            "consent_type": r.consent_type.value,
            "status": r.status.value,
            "given_at": r.given_at.isoformat(),
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "consented_by_name": r.consented_by_name,
            "relationship_to_patient": r.relationship_to_patient,
            "booking_id": str(r.booking_id) if r.booking_id else None,
        }
        for r in items
    ]


@router.post("/consents/{consent_id}/revoke")
async def revoke_consent(consent_id: UUID, reason: str, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ConsentRecord).where(ConsentRecord.id == consent_id))
    rec = res.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Consent not found")
    # Only the consumer/guardian who owns this patient (or admin) may revoke.
    await _patient_ownership_check(db, current, rec.patient_id)
    if current.role == UserRole.worker:
        # Workers must never revoke a consent — even on their own assigned bookings.
        raise HTTPException(status_code=403, detail="Workers cannot revoke consents")
    rec.status = ConsentStatus.revoked
    rec.revoked_at = datetime.now(timezone.utc)
    rec.revoked_by = current.id
    rec.revocation_reason = reason
    await db.commit()
    return {"revoked": True}
