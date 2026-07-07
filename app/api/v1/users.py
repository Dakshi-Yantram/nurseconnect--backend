"""Consumer profile, patient, family member endpoints."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_consumer_profile, get_current_user
from app.models.models import ConsumerProfile, FamilyMember, Patient
from app.schemas.schemas import (
    ConsumerProfileOut,
    ConsumerProfileUpdate,
    FamilyMemberCreate,
    FamilyMemberOut,
    PatientCreate,
    PatientOut,
)

router = APIRouter(tags=["consumer"])


@router.get("/consumers/me", response_model=ConsumerProfileOut)
async def my_consumer_profile(profile: ConsumerProfile = Depends(get_consumer_profile)):
    return ConsumerProfileOut.model_validate(profile)


@router.put("/consumers/me", response_model=ConsumerProfileOut)
async def update_my_consumer_profile(
    payload: ConsumerProfileUpdate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    await db.commit()
    await db.refresh(profile)
    return ConsumerProfileOut.model_validate(profile)


# ----- Patients -----
@router.post("/patients", response_model=PatientOut)
async def create_patient(
    payload: PatientCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    patient = Patient(consumer_id=profile.id, **payload.model_dump())
    db.add(patient)
    await db.commit()
    await db.refresh(patient)
    return PatientOut.model_validate(patient)


@router.get("/patients", response_model=List[PatientOut])
async def list_patients(profile: ConsumerProfile = Depends(get_consumer_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Patient).where(Patient.consumer_id == profile.id).order_by(Patient.created_at.desc()))
    return [PatientOut.model_validate(p) for p in res.scalars().all()]


@router.get("/patients/{patient_id}", response_model=PatientOut)
async def get_patient(patient_id: UUID, profile: ConsumerProfile = Depends(get_consumer_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Patient).where(Patient.id == patient_id, Patient.consumer_id == profile.id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    return PatientOut.model_validate(p)


@router.put("/patients/{patient_id}", response_model=PatientOut)
async def update_patient(
    patient_id: UUID,
    payload: PatientCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Patient).where(Patient.id == patient_id, Patient.consumer_id == profile.id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    return PatientOut.model_validate(p)


# ----- Family members -----
@router.post("/family-members", response_model=FamilyMemberOut)
async def add_family_member(
    payload: FamilyMemberCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    fm = FamilyMember(consumer_id=profile.id, **payload.model_dump())
    db.add(fm)
    await db.commit()
    await db.refresh(fm)
    return FamilyMemberOut.model_validate(fm)


@router.get("/family-members", response_model=List[FamilyMemberOut])
async def list_family_members(profile: ConsumerProfile = Depends(get_consumer_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(FamilyMember).where(FamilyMember.consumer_id == profile.id))
    return [FamilyMemberOut.model_validate(f) for f in res.scalars().all()]


@router.delete("/family-members/{member_id}")
async def remove_family_member(member_id: UUID, profile: ConsumerProfile = Depends(get_consumer_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(FamilyMember).where(FamilyMember.id == member_id, FamilyMember.consumer_id == profile.id))
    fm = res.scalar_one_or_none()
    if not fm:
        raise HTTPException(status_code=404, detail="Family member not found")
    await db.delete(fm)
    await db.commit()
    return {"deleted": True}
