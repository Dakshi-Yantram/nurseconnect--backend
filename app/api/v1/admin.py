"""Admin endpoints (catalog mgmt, worker approval, ledger, dashboards)."""
from typing import List, Optional
from uuid import UUID

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, is_admin, require_admin, require_reviewer, require_roles
from app.models.enums import (
    BookingStatus,
    EscalationStatus,
    GenderRestriction,
    UserRole,
    UserStatus,
    VisitFrequency,
    WorkerOnboardingStatus,
    WorkerTier,
)
from app.models.models import (
    Booking,
    CarePackage,
    ConsumerProfile,
    Escalation,
    FinancialLedger,
    NurseReviewTicket,
    Patient,
    User,
    WorkerDocument,
    WorkerProfile,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# Ticket statuses that are still "open" in the reviewer queue — used to find
# the live ticket for a worker when syncing status after a review action.
_OPEN_TICKET_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION", "UNASSIGNED")


async def _sync_ticket_status(db: AsyncSession, worker_id: UUID, new_status: str) -> None:
    """Keep the reviewer-queue ticket (NurseReviewTicket) in sync with worker
    onboarding / document review actions taken from the admin endpoints below.

    Without this, WorkerDocument/WorkerProfile get updated but the ticket that
    actually drives the reviewer's queue UI (/api/review/my-queue) never
    changes, so cards stay stuck on "PENDING REVIEW" forever.
    """
    res = await db.execute(
        select(NurseReviewTicket).where(
            NurseReviewTicket.nurse_id == worker_id,
            NurseReviewTicket.status.in_(_OPEN_TICKET_STATUSES),
        )
    )
    ticket = res.scalar_one_or_none()
    if ticket:
        ticket.status = new_status


class DocumentReviewRequest(BaseModel):
    status: str
    reason: Optional[str] = None


class BackgroundCheckRequest(BaseModel):
    status: str
    reason: Optional[str] = None


class WorkerRejectionRequest(BaseModel):
    reason: str


class WorkerTierUpdateRequest(BaseModel):
    tier: str  # tier1 .. tier5


@router.get("/dashboard")
async def admin_dashboard(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Admin only")
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    total_consumers = (await db.execute(select(func.count(User.id)).where(User.role == UserRole.consumer))).scalar() or 0
    total_workers = (await db.execute(select(func.count(User.id)).where(User.role == UserRole.worker))).scalar() or 0
    pending_workers = (await db.execute(select(func.count(WorkerProfile.id)).where(WorkerProfile.onboarding_status == WorkerOnboardingStatus.pending_review))).scalar() or 0
    bookings_today = (await db.execute(select(func.count(Booking.id)).where(Booking.scheduled_date == func.current_date()))).scalar() or 0
    open_escalations = (await db.execute(select(func.count(Escalation.id)).where(Escalation.status != EscalationStatus.resolved))).scalar() or 0
    return {
        "total_users": total_users,
        "total_consumers": total_consumers,
        "total_workers": total_workers,
        "pending_worker_approvals": pending_workers,
        "bookings_today": bookings_today,
        "open_escalations": open_escalations,
    }


@router.get("/workers/pending")
async def pending_workers(
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(WorkerProfile, User).join(User, User.id == WorkerProfile.user_id).where(WorkerProfile.onboarding_status == WorkerOnboardingStatus.pending_review)
    )
    items = []
    for wp, u in res.all():
        docs_res = await db.execute(
            select(WorkerDocument).where(WorkerDocument.worker_id == wp.id)
        )
        documents = [
            {
                "id": str(doc.id),
                "document_type": doc.document_type,
                "verification_status": doc.verification_status,
                "document_url": doc.cloudinary_url,
                "rejection_reason": doc.rejection_reason,
            }
            for doc in docs_res.scalars().all()
        ]
        items.append({
            "worker_id": str(wp.id),
            "user_id": str(u.id),
            "full_name": u.full_name,
            "phone": u.phone_e164,
            "email": u.email,
            "tier": wp.tier.value,
            "background_check_status": wp.background_check_status,
            "documents": documents,
            "created_at": wp.created_at.isoformat(),
        })
    return items



@router.get("/workers/all")
async def all_workers(
    onboarding_status: str | None = None,
    limit: int = 200,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """All workers regardless of onboarding status — used by the admin nurses list."""
    stmt = (
        select(WorkerProfile, User)
        .join(User, User.id == WorkerProfile.user_id)
        .order_by(WorkerProfile.created_at.desc())
        .limit(limit)
    )
    if onboarding_status:
        from app.models.enums import WorkerOnboardingStatus
        try:
            stmt = stmt.where(WorkerProfile.onboarding_status == WorkerOnboardingStatus(onboarding_status))
        except ValueError:
            pass
    res = await db.execute(stmt)
    items = []
    for wp, u in res.all():
        docs_res = await db.execute(select(WorkerDocument).where(WorkerDocument.worker_id == wp.id))
        documents = [
            {"document_type": doc.document_type, "verification_status": doc.verification_status}
            for doc in docs_res.scalars().all()
        ]
        items.append({
            "worker_id": str(wp.id),
            "user_id": str(u.id),
            "full_name": u.full_name,
            "phone": u.phone_e164,
            "email": u.email,
            "tier": wp.tier.value,
            "worker_type": getattr(wp, "worker_type", None) and wp.worker_type.value,
            "onboarding_status": wp.onboarding_status.value,
            "background_check_status": wp.background_check_status,
            "availability": wp.availability.value if wp.availability else None,
            "documents": documents,
            "created_at": wp.created_at.isoformat(),
        })
    return items

@router.post("/workers/{worker_id}/approve")
async def approve_worker(
    worker_id: UUID,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    if wp.onboarding_status != WorkerOnboardingStatus.pending_review:
        raise HTTPException(status_code=409, detail="Worker has not submitted onboarding for review")
    docs_res = await db.execute(select(WorkerDocument).where(WorkerDocument.worker_id == wp.id))
    docs = list(docs_res.scalars().all())
    required = {"aadhaar", "nursing_license", "degree_certificate", "police_verification"}
    verified_types = {
        d.document_type
        for d in docs
        if d.verification_status == "verified"
        and (d.valid_until is None or d.valid_until >= date.today())
    }
    missing_verified = sorted(required - verified_types)
    if missing_verified:
        raise HTTPException(
            status_code=409,
            detail={"message": "Required documents are not verified", "documents": missing_verified},
        )
    if wp.background_check_status != "passed":
        raise HTTPException(status_code=409, detail="Background check has not passed")
    wp.onboarding_status = WorkerOnboardingStatus.approved
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = None
    # ACTIVATE the account: the worker was held in `onboarding` until now.
    ures = await db.execute(select(User).where(User.id == wp.user_id))
    wuser = ures.scalar_one_or_none()
    if wuser and wuser.status == UserStatus.onboarding:
        wuser.status = UserStatus.active
    # Award the worker's tier badge on first approval (skill-based badge).
    from app.services.badges import award_tier_badge
    await award_tier_badge(db, wp)
    # BUGFIX: approval used to never create WorkerServiceQualification rows,
    # so any service/package gated only by tier (no training/cert/assessment)
    # stayed permanently locked with locked_reason=QUALIFICATION_RECORD_MISSING
    # and no way for the worker to opt in or request access.
    from app.services.qualification import sync_tier_qualifications
    await sync_tier_qualifications(db, wp)
    # Keep the reviewer-queue ticket in sync so the card leaves "PENDING REVIEW".
    await _sync_ticket_status(db, wp.id, "APPROVED")
    await db.commit()
    return {"approved": True}


@router.patch("/workers/{worker_id}/documents/{document_id}")
async def review_worker_document(
    worker_id: UUID,
    document_id: UUID,
    payload: DocumentReviewRequest,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    if payload.status not in ("verified", "rejected"):
        raise HTTPException(status_code=400, detail="Document status must be verified or rejected")
    res = await db.execute(
        select(WorkerDocument).where(
            WorkerDocument.id == document_id,
            WorkerDocument.worker_id == worker_id,
        )
    )
    doc = res.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    doc.verification_status = payload.status
    doc.verified_by = current.id
    doc.verified_at = datetime.now(timezone.utc)
    doc.rejection_reason = payload.reason if payload.status == "rejected" else None
    # Keep the reviewer-queue ticket in sync: a rejected document needs the
    # nurse to clarify/resubmit; a verified document moves the ticket into
    # active review (out of the raw "just submitted" PENDING_REVIEW state).
    await _sync_ticket_status(
        db, worker_id, "NEEDS_CLARIFICATION" if payload.status == "rejected" else "IN_REVIEW"
    )
    await db.commit()
    return {"reviewed": True, "verification_status": doc.verification_status}


@router.post("/workers/{worker_id}/background-check")
async def record_background_check(
    worker_id: UUID,
    payload: BackgroundCheckRequest,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    if payload.status not in ("in_progress", "passed", "failed"):
        raise HTTPException(status_code=400, detail="Invalid background check status")
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp.background_check_status = payload.status
    if payload.status == "failed":
        wp.onboarding_status = WorkerOnboardingStatus.rejected
        wp.onboarding_rejection_reason = payload.reason or "Background check failed"
        wp.onboarding_reviewed_at = datetime.now(timezone.utc)
        await _sync_ticket_status(db, wp.id, "REJECTED")
    await db.commit()
    return {"background_check_status": wp.background_check_status}


@router.post("/workers/{worker_id}/reject")
async def reject_worker(
    worker_id: UUID,
    payload: WorkerRejectionRequest,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp.onboarding_status = WorkerOnboardingStatus.rejected
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = payload.reason.strip()
    wp.availability = "offline"
    # Keep the reviewer-queue ticket in sync so the card leaves the queue.
    await _sync_ticket_status(db, wp.id, "REJECTED")
    await db.commit()
    return {"rejected": True}


@router.patch("/workers/{worker_id}/tier")
async def set_worker_tier(
    worker_id: UUID,
    payload: WorkerTierUpdateRequest,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Reviewer sets the worker's skill tier before approving them.
    The tier drives skill-gating (which services they can serve) and mints the
    tier badge at approval."""
    tier = _validate_enum_field(payload.tier, WorkerTier, "tier")
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp.tier = tier
    # If already approved, refresh the tier badge to the new level.
    if wp.onboarding_status == WorkerOnboardingStatus.approved:
        from app.services.badges import award_tier_badge
        await award_tier_badge(db, wp)
        # BUGFIX: re-sync qualification rows so a tier upgrade (e.g. to the
        # top tier / "Clinical Lead") immediately unlocks any tier-only
        # gated services instead of leaving them stuck as locked.
        from app.services.qualification import sync_tier_qualifications
        await sync_tier_qualifications(db, wp)
    await db.commit()
    return {"worker_id": str(wp.id), "tier": wp.tier.value}


@router.post("/workers/{worker_id}/suspend")
async def suspend_worker(
    worker_id: UUID,
    reason: str,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(WorkerProfile, User).join(User, User.id == WorkerProfile.user_id).where(WorkerProfile.id == worker_id))
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp, user = row
    wp.onboarding_status = WorkerOnboardingStatus.suspended
    user.status = UserStatus.suspended
    await db.commit()
    return {"suspended": True}


@router.get("/financial/ledger")
async def ledger(
    booking_id: Optional[UUID] = None,
    worker_id: Optional[UUID] = None,
    consumer_id: Optional[UUID] = None,
    limit: int = 100,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(FinancialLedger).order_by(FinancialLedger.created_at.desc()).limit(limit)
    if booking_id:
        stmt = stmt.where(FinancialLedger.booking_id == booking_id)
    if worker_id:
        stmt = stmt.where(FinancialLedger.worker_id == worker_id)
    if consumer_id:
        stmt = stmt.where(FinancialLedger.consumer_id == consumer_id)
    res = await db.execute(stmt)
    return [
        {
            "id": str(e.id),
            "entry_type": e.entry_type.value,
            "amount": float(e.amount),
            "currency": e.currency,
            "debit_account": e.debit_account,
            "credit_account": e.credit_account,
            "booking_id": str(e.booking_id) if e.booking_id else None,
            "worker_id": str(e.worker_id) if e.worker_id else None,
            "consumer_id": str(e.consumer_id) if e.consumer_id else None,
            "description": e.description,
            "created_at": e.created_at.isoformat(),
        }
        for e in res.scalars().all()
    ]


@router.post("/rematch/{booking_id}")
async def rematch_booking(
    booking_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    b.worker_id = None
    b.status = BookingStatus.rematch_pending
    b.rematch_count += 1
    await db.commit()
    return {"rematch_initiated": True, "attempt": b.rematch_count}


@router.get("/patients")
async def admin_list_patients(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Admin only")

    stmt = (
        select(Patient, ConsumerProfile, User)
        .join(ConsumerProfile, ConsumerProfile.id == Patient.consumer_id)
        .join(User, User.id == ConsumerProfile.user_id)
        .order_by(Patient.created_at.desc())
    )
    res = await db.execute(stmt)

    items = []
    for patient, profile, user in res.all():
        age = None
        if patient.date_of_birth:
            today = date.today()
            age = today.year - patient.date_of_birth.year - (
                (today.month, today.day) < (patient.date_of_birth.month, patient.date_of_birth.day)
            )
        items.append({
            "id": str(patient.id),
            "full_name": patient.full_name,
            "age": age,
            "gender": patient.gender.value if patient.gender else None,
            "phone_e164": user.phone_e164,
            "city": profile.city,
            "care_plan": None,
            "is_bpl": False,
        })
    return items

@router.get("/patients/{patient_id}")
async def admin_get_patient(
    patient_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Admin only")

    stmt = (
        select(Patient, ConsumerProfile, User)
        .join(ConsumerProfile, ConsumerProfile.id == Patient.consumer_id)
        .join(User, User.id == ConsumerProfile.user_id)
        .where(Patient.id == patient_id)
    )
    res = await db.execute(stmt)
    row = res.first()

    if not row:
        raise HTTPException(status_code=404, detail="Patient not found")

    patient, profile, user = row
    age = None
    if patient.date_of_birth:
        today = date.today()
        age = today.year - patient.date_of_birth.year - (
            (today.month, today.day) < (patient.date_of_birth.month, patient.date_of_birth.day)
        )

    return {
        "id": str(patient.id),
        "full_name": patient.full_name,
        "age": age,
        "gender": patient.gender.value if patient.gender else None,
        "phone_e164": user.phone_e164,
        "city": profile.city,
        "care_plan": None,
        "is_bpl": False,
    }
@router.get("/consumers")
async def admin_list_consumers(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Admin only")

    stmt = (
        select(ConsumerProfile, User)
        .join(User, User.id == ConsumerProfile.user_id)
        .order_by(User.full_name)
    )
    res = await db.execute(stmt)

    return [
        {
            "id": str(profile.id),
            "full_name": user.full_name,
            "phone": user.phone_e164,
        }
        for profile, user in res.all()
    ]


# ============================================================================
# PATCH 3 — Admin Care Package endpoints
# Backs the admin Care Packages page (_app.care-packages.tsx):
#   POST   /api/admin/care-packages              create
#   PUT    /api/admin/care-packages/{id}          update
#   PATCH  /api/admin/care-packages/{id}/toggle   activate/deactivate
# ============================================================================
class CarePackageCreateRequest(BaseModel):
    name: str
    package_code: str
    tagline: Optional[str] = None
    description: Optional[str] = None
    target_condition: Optional[str] = None
    min_tier: str = "tier1"
    gender_restriction: str = "any"
    visit_frequency: Optional[str] = None
    visits_per_cycle: Optional[int] = None
    cycle_duration_days: Optional[int] = None
    package_price: Optional[float] = None
    per_visit_price: Optional[float] = None
    commission_pct: Optional[float] = None
    subsidy_eligible: bool = False
    requires_prescription: bool = False
    insurance_covered: bool = True
    available_cities: Optional[List[str]] = None


class CarePackageUpdateRequest(CarePackageCreateRequest):
    pass


def _serialize_care_package(pkg: CarePackage) -> dict:
    return {
        "id": str(pkg.id),
        "package_code": pkg.package_code,
        "name": pkg.name,
        "tagline": pkg.tagline,
        "description": pkg.description,
        "target_condition": pkg.target_condition,
        "min_tier": pkg.min_tier.value if pkg.min_tier else None,
        "gender_restriction": pkg.gender_restriction.value if pkg.gender_restriction else None,
        "visit_frequency": pkg.visit_frequency.value if pkg.visit_frequency else None,
        "visits_per_cycle": pkg.visits_per_cycle,
        "cycle_duration_days": pkg.cycle_duration_days,
        "shift_hours": pkg.shift_hours,
        "package_price": float(pkg.package_price) if pkg.package_price is not None else None,
        "per_visit_price": float(pkg.per_visit_price) if pkg.per_visit_price is not None else None,
        "subsidy_eligible": pkg.subsidy_eligible,
        "commission_pct": float(pkg.commission_pct) if pkg.commission_pct is not None else None,
        "requires_prescription": pkg.requires_prescription,
        "insurance_covered": pkg.insurance_covered,
        "is_active": pkg.is_active,
        "version": pkg.version,
        "available_cities": pkg.available_cities,
        "created_at": pkg.created_at.isoformat() if pkg.created_at else None,
    }


def _validate_enum_field(value: Optional[str], enum_cls, field_name: str):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(e.value for e in enum_cls)
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name} '{value}'. Must be one of: {valid}",
        )


@router.post("/care-packages", status_code=201)
async def create_care_package(
    payload: CarePackageCreateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(CarePackage).where(CarePackage.package_code == payload.package_code.strip().upper())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Package code '{payload.package_code}' already exists")

    min_tier = _validate_enum_field(payload.min_tier, WorkerTier, "min_tier")
    gender_restriction = _validate_enum_field(payload.gender_restriction, GenderRestriction, "gender_restriction")
    visit_frequency = _validate_enum_field(payload.visit_frequency, VisitFrequency, "visit_frequency")

    pkg = CarePackage(
        package_code=payload.package_code.strip().upper(),
        name=payload.name.strip(),
        tagline=payload.tagline,
        description=payload.description,
        target_condition=payload.target_condition,
        min_tier=min_tier or WorkerTier.tier1,
        gender_restriction=gender_restriction or GenderRestriction.any,
        visit_frequency=visit_frequency,
        visits_per_cycle=payload.visits_per_cycle,
        cycle_duration_days=payload.cycle_duration_days,
        package_price=Decimal(str(payload.package_price)) if payload.package_price is not None else None,
        per_visit_price=Decimal(str(payload.per_visit_price)) if payload.per_visit_price is not None else None,
        subsidy_eligible=payload.subsidy_eligible,
        commission_pct=Decimal(str(payload.commission_pct)) if payload.commission_pct is not None else None,
        requires_prescription=payload.requires_prescription,
        insurance_covered=payload.insurance_covered,
        available_cities=payload.available_cities,
        is_active=True,
        version=1,
        created_by=current.id,
    )
    db.add(pkg)
    await db.commit()
    await db.refresh(pkg)
    return _serialize_care_package(pkg)


@router.put("/care-packages/{package_id}")
async def update_care_package(
    package_id: UUID,
    payload: CarePackageUpdateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(CarePackage).where(CarePackage.id == package_id))
    pkg = res.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Care package not found")

    new_code = payload.package_code.strip().upper()
    if new_code != pkg.package_code:
        dupe = await db.execute(
            select(CarePackage).where(CarePackage.package_code == new_code, CarePackage.id != package_id)
        )
        if dupe.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Package code '{new_code}' already exists")

    min_tier = _validate_enum_field(payload.min_tier, WorkerTier, "min_tier")
    gender_restriction = _validate_enum_field(payload.gender_restriction, GenderRestriction, "gender_restriction")
    visit_frequency = _validate_enum_field(payload.visit_frequency, VisitFrequency, "visit_frequency")

    pkg.package_code = new_code
    pkg.name = payload.name.strip()
    pkg.tagline = payload.tagline
    pkg.description = payload.description
    pkg.target_condition = payload.target_condition
    pkg.min_tier = min_tier or pkg.min_tier
    pkg.gender_restriction = gender_restriction or pkg.gender_restriction
    pkg.visit_frequency = visit_frequency
    pkg.visits_per_cycle = payload.visits_per_cycle
    pkg.cycle_duration_days = payload.cycle_duration_days
    pkg.package_price = Decimal(str(payload.package_price)) if payload.package_price is not None else None
    pkg.per_visit_price = Decimal(str(payload.per_visit_price)) if payload.per_visit_price is not None else None
    pkg.subsidy_eligible = payload.subsidy_eligible
    pkg.commission_pct = Decimal(str(payload.commission_pct)) if payload.commission_pct is not None else None
    pkg.requires_prescription = payload.requires_prescription
    pkg.insurance_covered = payload.insurance_covered
    pkg.available_cities = payload.available_cities
    pkg.version = (pkg.version or 1) + 1

    await db.commit()
    await db.refresh(pkg)
    return _serialize_care_package(pkg)


@router.patch("/care-packages/{package_id}/toggle")
async def toggle_care_package(
    package_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(CarePackage).where(CarePackage.id == package_id))
    pkg = res.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Care package not found")
    pkg.is_active = not pkg.is_active
    await db.commit()
    return {"id": str(pkg.id), "is_active": pkg.is_active}