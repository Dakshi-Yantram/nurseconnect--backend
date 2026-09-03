"""Teledoctor consultation queue.

Admin dashboard flow: user role = worker/worker_type=doctor ("teledoctor")
picks up assigned bookings from a waiting queue and moves each one, in
order, through: waiting -> diet_review -> patient_assessment -> prescription
-> completed. Stage only ever advances forward.

  POST /teleconsult/start                 create/return the queue item for a
                                           booking (called when the doctor's
                                           Dyte call for that booking starts)
  GET  /teleconsult/queue                 doctor's own queue, optionally
                                           filtered by stage
  GET  /teleconsult/admin/queue           admin/operations view across all
                                           teledoctors, grouped by stage
  PATCH /teleconsult/{id}/diet            record diet notes, advance to
                                           patient_assessment
  PATCH /teleconsult/{id}/patient-issues  record patient issues (or "all
                                           okay"), advance to prescription
  PATCH /teleconsult/{id}/complete        mark completed (after e-Rx issued)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_worker_profile, require_operations
from app.models.enums import TeleConsultationStage, WorkerType
from app.models.models import Booking, Patient, TeleConsultation, User, WorkerProfile

router = APIRouter(prefix="/teleconsult", tags=["teleconsult"])


def _require_doctor(worker: WorkerProfile = Depends(get_worker_profile)) -> WorkerProfile:
    if worker.worker_type != WorkerType.doctor:
        raise HTTPException(status_code=403, detail="Doctor account required")
    return worker


class TeleConsultOut(BaseModel):
    id: UUID
    booking_id: UUID
    doctor_worker_id: UUID
    patient_id: UUID
    patient_name: Optional[str] = None
    stage: str
    diet_notes: Optional[str]
    patient_issues: Optional[str]
    patient_all_okay: Optional[bool]
    prescription_id: Optional[UUID]
    created_at: datetime

    class Config:
        from_attributes = True


class StartRequest(BaseModel):
    booking_id: UUID


@router.post("/start", response_model=TeleConsultOut)
async def start_consultation(
    body: StartRequest,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == body.booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.worker_id != doctor.id:
        raise HTTPException(status_code=403, detail="This booking is not assigned to you")

    existing = await db.execute(select(TeleConsultation).where(TeleConsultation.booking_id == booking.id))
    consult = existing.scalar_one_or_none()
    if consult:
        return _with_patient_name(consult, None)

    consult = TeleConsultation(
        booking_id=booking.id,
        doctor_worker_id=doctor.id,
        patient_id=booking.patient_id,
        stage=TeleConsultationStage.waiting,
        started_at=datetime.now(timezone.utc),
    )
    db.add(consult)
    await db.commit()
    await db.refresh(consult)
    return _with_patient_name(consult, None)


@router.get("/queue", response_model=list[TeleConsultOut])
async def my_queue(
    stage: Optional[str] = None,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(TeleConsultation, Patient.full_name)
        .join(Patient, Patient.id == TeleConsultation.patient_id)
        .where(TeleConsultation.doctor_worker_id == doctor.id)
        .order_by(TeleConsultation.created_at.asc())
    )
    if stage:
        stmt = stmt.where(TeleConsultation.stage == stage)
    rows = (await db.execute(stmt)).all()
    return [_with_patient_name(c, name) for c, name in rows]


@router.get("/admin/queue")
async def admin_queue(
    stage: Optional[str] = None,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    """Cross-doctor view for the admin dashboard's teledoctor tab, grouped
    by stage so ops can see how many consultations are stuck waiting vs.
    mid-diet vs. mid-assessment vs. ready for/awaiting prescription."""
    stmt = (
        select(TeleConsultation, Patient.full_name, User.full_name, Booking.booking_ref)
        .join(Patient, Patient.id == TeleConsultation.patient_id)
        .join(WorkerProfile, WorkerProfile.id == TeleConsultation.doctor_worker_id)
        .join(User, User.id == WorkerProfile.user_id)
        .join(Booking, Booking.id == TeleConsultation.booking_id)
        .order_by(TeleConsultation.created_at.asc())
    )
    if stage:
        stmt = stmt.where(TeleConsultation.stage == stage)
    rows = (await db.execute(stmt)).all()

    grouped: dict = {s.value: [] for s in TeleConsultationStage}
    for consult, patient_name, doctor_name, booking_ref in rows:
        grouped[consult.stage.value].append({
            "id": str(consult.id),
            "booking_id": str(consult.booking_id),
            "booking_ref": booking_ref,
            "doctor_name": doctor_name,
            "patient_name": patient_name,
            "stage": consult.stage.value,
            "diet_notes": consult.diet_notes,
            "patient_issues": consult.patient_issues,
            "patient_all_okay": consult.patient_all_okay,
            "prescription_id": str(consult.prescription_id) if consult.prescription_id else None,
            "created_at": consult.created_at.isoformat(),
        })
    return grouped


class DietRequest(BaseModel):
    diet_notes: str


@router.patch("/{consult_id}/diet", response_model=TeleConsultOut)
async def record_diet(
    consult_id: UUID,
    body: DietRequest,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    consult = await _get_owned(db, consult_id, doctor.id)
    if consult.stage not in (TeleConsultationStage.waiting, TeleConsultationStage.diet_review):
        raise HTTPException(status_code=409, detail=f"Cannot record diet notes at stage {consult.stage.value}")
    consult.diet_notes = body.diet_notes
    consult.diet_reviewed_at = datetime.now(timezone.utc)
    consult.stage = TeleConsultationStage.patient_assessment
    await db.commit()
    await db.refresh(consult)
    return _with_patient_name(consult, None)


class PatientIssuesRequest(BaseModel):
    all_okay: bool
    issues: Optional[str] = None  # required when all_okay is False


@router.patch("/{consult_id}/patient-issues", response_model=TeleConsultOut)
async def record_patient_issues(
    consult_id: UUID,
    body: PatientIssuesRequest,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    consult = await _get_owned(db, consult_id, doctor.id)
    if consult.stage not in (TeleConsultationStage.patient_assessment,):
        raise HTTPException(status_code=409, detail=f"Cannot record patient issues at stage {consult.stage.value}")
    if not body.all_okay and not body.issues:
        raise HTTPException(status_code=422, detail="issues is required when all_okay is false")

    consult.patient_all_okay = body.all_okay
    consult.patient_issues = "All okay" if body.all_okay else body.issues
    consult.patient_assessed_at = datetime.now(timezone.utc)
    consult.stage = TeleConsultationStage.prescription
    await db.commit()
    await db.refresh(consult)
    return _with_patient_name(consult, None)


@router.patch("/{consult_id}/complete", response_model=TeleConsultOut)
async def complete_consultation(
    consult_id: UUID,
    doctor: WorkerProfile = Depends(_require_doctor),
    db: AsyncSession = Depends(get_db),
):
    consult = await _get_owned(db, consult_id, doctor.id)
    if consult.stage != TeleConsultationStage.prescription:
        raise HTTPException(status_code=409, detail="Issue the e-prescription before completing")
    if not consult.prescription_id:
        raise HTTPException(status_code=409, detail="No e-prescription linked yet — issue one first")
    consult.stage = TeleConsultationStage.completed
    consult.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(consult)
    return _with_patient_name(consult, None)


async def _get_owned(db: AsyncSession, consult_id: UUID, doctor_id: UUID) -> TeleConsultation:
    res = await db.execute(select(TeleConsultation).where(TeleConsultation.id == consult_id))
    consult = res.scalar_one_or_none()
    if not consult:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if consult.doctor_worker_id != doctor_id:
        raise HTTPException(status_code=403, detail="Not your consultation")
    return consult


def _with_patient_name(consult: TeleConsultation, patient_name: Optional[str]) -> TeleConsultOut:
    out = TeleConsultOut.model_validate(consult)
    out.patient_name = patient_name
    out.stage = consult.stage.value
    return out
