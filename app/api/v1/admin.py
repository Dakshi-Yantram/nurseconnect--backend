"""Admin endpoints (catalog mgmt, worker approval, ledger, dashboards)."""
import re
from typing import List, Optional
from uuid import UUID

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, is_admin, require_admin, require_operations, require_reviewer, require_roles
from app.core.security import hash_password
from app.models.enums import (
    BookingStatus,
    ComplaintStatus,
    DisputeStatus,
    EscalationLevel,
    EscalationStatus,
    GenderRestriction,
    PaymentStatus,
    PayoutBatchStatus,
    QualificationGate,
    UserRole,
    UserStatus,
    VisitFrequency,
    VisitStatus,
    WorkerOnboardingStatus,
    WorkerTier,
    WorkerType,
)
from app.models.models import (
    AuditLog,
    Booking,
    CarePackage,
    ClinicalRuleSet,
    Complaint,
    ConsentRecord,
    ConsumerProfile,
    DataRetentionSchedule,
    Dispute,
    Escalation,
    FinancialLedger,
    NurseReviewTicket,
    Patient,
    PayoutBatch,
    RoleDefinition,
    ServiceCatalogue,
    SubsidyEligibility,
    User,
    VisitRecord,
    WorkerDocument,
    WorkerPayout,
    WorkerProfile,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# Ticket statuses that are still "open" in the reviewer queue — used to find
# the live ticket for a worker when syncing status after a review action.
_OPEN_TICKET_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION", "UNASSIGNED")

REQUIRED_DOCUMENTS_BY_WORKER_TYPE = {
    WorkerType.nurse: {"aadhaar", "nursing_license", "degree_certificate", "police_verification"},
    WorkerType.caregiver: {"aadhaar", "police_verification"},
}


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
    required = REQUIRED_DOCUMENTS_BY_WORKER_TYPE.get(
        getattr(wp, "worker_type", WorkerType.nurse),
        REQUIRED_DOCUMENTS_BY_WORKER_TYPE[WorkerType.nurse],
    )
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


@router.post("/workers/{worker_id}/resync-qualifications")
async def resync_worker_qualifications(
    worker_id: UUID,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Re-run the tier→qualification bridge for a single worker.

    `approve_worker` and `set_worker_tier` already call
    `sync_tier_qualifications` going forward, but any worker who was
    approved/tier-changed *before* that fix shipped still has no
    WorkerServiceQualification rows at all, so their "My Services" page
    shows everything Locked / QUALIFICATION_RECORD_MISSING with nothing to
    opt in to or request. This endpoint lets a reviewer fix an existing
    worker without having to re-approve or bounce their tier.
    """
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    if wp.onboarding_status != WorkerOnboardingStatus.approved:
        raise HTTPException(status_code=409, detail="Worker must be approved before syncing qualifications")
    from app.services.qualification import sync_tier_qualifications
    updated = await sync_tier_qualifications(db, wp)
    await db.commit()
    return {"worker_id": str(wp.id), "tier": wp.tier.value, "qualifications_synced": len(updated)}


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
    # Three-gate qualification model
    gate: str = "credential_only"  # credential_only | theory_verified | practical_verified
    required_assessment_codes: Optional[List[str]] = None
    practical_checklist_items: Optional[List[str]] = None


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
        "gate": pkg.gate.value if pkg.gate else None,
        "required_assessment_codes": pkg.required_assessment_codes,
        "practical_checklist_items": pkg.practical_checklist_items,
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
    gate = _validate_enum_field(payload.gate, QualificationGate, "gate")

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
        gate=gate or QualificationGate.credential_only,
        required_assessment_codes=payload.required_assessment_codes,
        practical_checklist_items=payload.practical_checklist_items,
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
    gate = _validate_enum_field(payload.gate, QualificationGate, "gate")

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
    pkg.gate = gate or pkg.gate
    pkg.required_assessment_codes = payload.required_assessment_codes
    pkg.practical_checklist_items = payload.practical_checklist_items
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


# ============================================================================
# Dashboard analytics — real aggregates, replaces frontend mock-data.ts KPIS /
# BOOKING_TREND / SERVICE_DISTRIBUTION / ALERTS / ACTIVITY.
# ============================================================================
@router.get("/dashboard/kpis")
async def dashboard_kpis(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    total_patients = (await db.execute(select(func.count(Patient.id)))).scalar() or 0
    active_nurses = (await db.execute(
        select(func.count(WorkerProfile.id)).where(WorkerProfile.onboarding_status == WorkerOnboardingStatus.approved)
    )).scalar() or 0
    active_visits = (await db.execute(
        select(func.count(VisitRecord.id)).where(VisitRecord.status == VisitStatus.in_progress)
    )).scalar() or 0
    revenue = (await db.execute(
        select(func.coalesce(func.sum(Booking.total_amount), 0)).where(Booking.payment_status == PaymentStatus.captured)
    )).scalar() or 0
    avg_rating = (await db.execute(select(func.avg(WorkerProfile.rating_average)))).scalar() or 0
    total_bookings = (await db.execute(select(func.count(Booking.id)))).scalar() or 0
    completed_bookings = (await db.execute(select(func.count(Booking.id)).where(Booking.status == BookingStatus.completed))).scalar() or 0
    completion_rate = round((completed_bookings / total_bookings) * 100, 1) if total_bookings else 0.0
    return {
        "total_patients": total_patients,
        "active_nurses": active_nurses,
        "active_visits": active_visits,
        "revenue": float(revenue),
        "avg_rating": round(float(avg_rating), 2),
        "completion_rate": completion_rate,
    }


@router.get("/dashboard/booking-trend")
async def dashboard_booking_trend(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Last 7 calendar days: booking count + completed count per day."""
    rows = await db.execute(
        select(Booking.scheduled_date, Booking.status, func.count(Booking.id))
        .where(Booking.scheduled_date >= func.current_date() - 6)
        .group_by(Booking.scheduled_date, Booking.status)
    )
    by_day: dict = {}
    for day, status, count in rows.all():
        entry = by_day.setdefault(day.isoformat(), {"date": day.isoformat(), "bookings": 0, "completed": 0})
        entry["bookings"] += count
        if status == BookingStatus.completed:
            entry["completed"] += count
    return sorted(by_day.values(), key=lambda r: r["date"])


@router.get("/dashboard/service-distribution")
async def dashboard_service_distribution(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(ServiceCatalogue.name, func.count(Booking.id))
        .join(Booking, Booking.service_id == ServiceCatalogue.id)
        .group_by(ServiceCatalogue.name)
        .order_by(func.count(Booking.id).desc())
        .limit(8)
    )
    data = rows.all()
    total = sum(c for _, c in data) or 1
    return [{"name": name, "value": count, "pct": round((count / total) * 100)} for name, count in data]


@router.get("/dashboard/alerts")
async def dashboard_alerts(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Live operational alert counts, derived from real pending-work queues."""
    pending_approvals = (await db.execute(
        select(func.count(WorkerProfile.id)).where(WorkerProfile.onboarding_status == WorkerOnboardingStatus.pending_review)
    )).scalar() or 0
    open_incidents = (await db.execute(
        select(func.count(Escalation.id)).where(Escalation.status != EscalationStatus.resolved)
    )).scalar() or 0
    _resolved_complaint_statuses = (ComplaintStatus.resolved_action_taken, ComplaintStatus.resolved_no_action, ComplaintStatus.closed)
    open_complaints = (await db.execute(
        select(func.count(Complaint.id)).where(Complaint.status.not_in(_resolved_complaint_statuses))
    )).scalar() or 0

    alerts = []
    if pending_approvals:
        alerts.append({"id": "pending-approvals", "label": f"{pending_approvals} Pending Nurse Approvals", "priority": "high", "action": "Review Now", "to": "/nurse-approval"})
    if open_incidents:
        alerts.append({"id": "open-incidents", "label": f"{open_incidents} Escalations Unresolved", "priority": "high", "action": "Investigate", "to": "/incidents"})
    if open_complaints:
        alerts.append({"id": "open-complaints", "label": f"{open_complaints} Complaints Open", "priority": "medium", "action": "View Details", "to": "/complaints"})
    if not alerts:
        alerts.append({"id": "all-clear", "label": "System Health: All Services Operational", "priority": "low", "action": "View Status", "to": "/audit-logs"})
    return alerts


@router.get("/dashboard/activity")
async def dashboard_activity(
    limit: int = 10,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    logs = rows.scalars().all()
    actor_ids = {l.actor_id for l in logs if l.actor_id}
    actors: dict = {}
    if actor_ids:
        ures = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: (u.full_name or u.email or str(u.id)) for u in ures.scalars().all()}
    return [
        {
            "who": actors.get(l.actor_id, l.actor_type or "System"),
            "what": l.action,
            "target": l.entity_id or l.entity_type or "",
            "when": l.created_at.isoformat(),
        }
        for l in logs
    ]


@router.get("/regions")
async def admin_regions(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """City-wise rollup: nurses, patients (via consumer city), visits, revenue."""
    nurse_rows = await db.execute(
        select(WorkerProfile.base_city, func.count(WorkerProfile.id))
        .where(WorkerProfile.base_city.is_not(None))
        .group_by(WorkerProfile.base_city)
    )
    nurses_by_city = dict(nurse_rows.all())

    patient_rows = await db.execute(
        select(ConsumerProfile.city, func.count(Patient.id))
        .join(Patient, Patient.consumer_id == ConsumerProfile.id)
        .where(ConsumerProfile.city.is_not(None))
        .group_by(ConsumerProfile.city)
    )
    patients_by_city = dict(patient_rows.all())

    visit_rows = await db.execute(
        select(ConsumerProfile.city, func.count(Booking.id), func.coalesce(func.sum(Booking.total_amount), 0))
        .join(ConsumerProfile, ConsumerProfile.id == Booking.consumer_id)
        .where(ConsumerProfile.city.is_not(None))
        .group_by(ConsumerProfile.city)
    )
    visits_by_city = {city: (count, float(rev)) for city, count, rev in visit_rows.all()}

    cities = set(nurses_by_city) | set(patients_by_city) | set(visits_by_city)
    return [
        {
            "city": city,
            "nurses": nurses_by_city.get(city, 0),
            "patients": patients_by_city.get(city, 0),
            "visits": visits_by_city.get(city, (0, 0.0))[0],
            "revenue": visits_by_city.get(city, (0, 0.0))[1],
        }
        for city in sorted(cities)
    ]


@router.get("/live-visits")
async def admin_live_visits(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Ops-dashboard feed: bookings that are currently pending/claimable or
    have an in-progress visit."""
    rows = await db.execute(
        select(Booking, VisitRecord, Patient, WorkerProfile, User)
        .join(Patient, Patient.id == Booking.patient_id)
        .outerjoin(VisitRecord, VisitRecord.booking_id == Booking.id)
        .outerjoin(WorkerProfile, WorkerProfile.id == Booking.worker_id)
        .outerjoin(User, User.id == WorkerProfile.user_id)
        .where(Booking.status.in_([BookingStatus.confirmed, BookingStatus.rematch_pending, BookingStatus.in_progress]))
        .order_by(Booking.scheduled_date.desc(), Booking.scheduled_start_time.desc())
        .limit(50)
    )
    out = []
    for booking, visit, patient, worker, worker_user in rows.all():
        out.append({
            "id": booking.booking_ref,
            "patient": patient.full_name,
            "nurse": worker_user.full_name if worker_user else None,
            "status": visit.status.value if visit else booking.status.value,
            "area": (booking.address_snapshot or {}).get("city", ""),
            "scheduled_date": booking.scheduled_date.isoformat(),
            "scheduled_time": booking.scheduled_start_time.isoformat(),
            "amount": float(booking.total_amount),
            "is_urgent": booking.is_urgent,
        })
    return out


@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = 100,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    logs = rows.scalars().all()
    actor_ids = {l.actor_id for l in logs if l.actor_id}
    actors: dict = {}
    if actor_ids:
        ures = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: (u.email or str(u.id)) for u in ures.scalars().all()}
    return [
        {
            "id": str(l.id),
            "ts": l.created_at.isoformat(),
            "actor": actors.get(l.actor_id, l.actor_type or "system"),
            "action": l.action,
            "entity": f"{l.entity_type or ''}:{l.entity_id or ''}".strip(":"),
            "changes": l.changes,
        }
        for l in logs
    ]


# ============================================================================
# Complaints
# ============================================================================
class ComplaintStatusUpdateRequest(BaseModel):
    status: str
    resolution_notes: Optional[str] = None


@router.get("/complaints")
async def list_complaints(
    status: Optional[str] = None,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(Complaint).order_by(Complaint.created_at.desc())
    if status:
        q = q.where(Complaint.status == status)
    rows = await db.execute(q)
    complaints = rows.scalars().all()
    raiser_ids = {c.raised_by for c in complaints}
    names: dict = {}
    if raiser_ids:
        ures = await db.execute(select(User).where(User.id.in_(raiser_ids)))
        names = {u.id: (u.full_name or u.email) for u in ures.scalars().all()}
    return [
        {
            "id": str(c.id),
            "subject": c.description[:80],
            "category": c.category,
            "status": c.status.value,
            "raisedBy": names.get(c.raised_by, "Unknown"),
            "created": c.created_at.isoformat(),
            "resolution_notes": c.resolution_notes,
        }
        for c in complaints
    ]


@router.post("/complaints/{complaint_id}/status")
async def update_complaint_status(
    complaint_id: UUID,
    payload: ComplaintStatusUpdateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Complaint).where(Complaint.id == complaint_id))
    complaint = res.scalar_one_or_none()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    complaint.status = payload.status
    complaint.assigned_to = current.id
    if payload.resolution_notes:
        complaint.resolution_notes = payload.resolution_notes
    if payload.status in (ComplaintStatus.resolved_action_taken.value, ComplaintStatus.resolved_no_action.value, ComplaintStatus.closed.value):
        complaint.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": str(complaint.id), "status": complaint.status}


# ============================================================================
# Disputes
# ============================================================================
class DisputeResolveRequest(BaseModel):
    status: str
    resolution_notes: Optional[str] = None
    consumer_refund_amount: Optional[float] = None
    worker_penalty_amount: Optional[float] = None


@router.get("/disputes")
async def list_disputes(
    status: Optional[str] = None,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(Dispute, Booking).join(Booking, Booking.id == Dispute.booking_id).order_by(Dispute.created_at.desc())
    if status:
        q = q.where(Dispute.status == status)
    rows = await db.execute(q)
    _open_dispute_statuses = (DisputeStatus.open, DisputeStatus.investigating)
    return [
        {
            "id": str(d.id),
            "booking": b.booking_ref,
            "amount": float(d.hold_amount) if d.hold_amount else None,
            "reason": d.description,
            "dispute_type": d.dispute_type.value,
            "status": d.status.value,
            "hold": d.status in _open_dispute_statuses,
            "opened": d.created_at.isoformat(),
        }
        for d, b in rows.all()
    ]


@router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: UUID,
    payload: DisputeResolveRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    dispute = res.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    dispute.status = payload.status
    dispute.assigned_to = current.id
    if payload.resolution_notes:
        dispute.resolution_notes = payload.resolution_notes
    if payload.consumer_refund_amount is not None:
        dispute.consumer_refund_amount = Decimal(str(payload.consumer_refund_amount))
    if payload.worker_penalty_amount is not None:
        dispute.worker_penalty_amount = Decimal(str(payload.worker_penalty_amount))
    dispute.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": str(dispute.id), "status": dispute.status}


# ============================================================================
# Payouts
# ============================================================================
@router.get("/payouts")
async def list_payout_batches(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(PayoutBatch).order_by(PayoutBatch.created_at.desc()))
    return [
        {
            "id": str(p.id),
            "batch": p.batch_reference,
            "nurses": p.total_payouts,
            "gross": float(p.total_amount),
            "status": p.status.value,
            "date": p.created_at.isoformat(),
        }
        for p in rows.scalars().all()
    ]


@router.get("/payouts/{batch_id}")
async def get_payout_batch(
    batch_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    batch = res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail="Payout batch not found")
    prows = await db.execute(select(WorkerPayout).where(WorkerPayout.payout_batch_id == batch_id))
    payouts = prows.scalars().all()
    return {
        "id": str(batch.id),
        "batch": batch.batch_reference,
        "status": batch.status.value,
        "total_amount": float(batch.total_amount),
        "created_at": batch.created_at.isoformat(),
        "payouts": [
            {
                "id": str(p.id),
                "worker_id": str(p.worker_id),
                "gross_amount": float(p.gross_amount),
                "net_amount": float(p.net_amount),
                "status": p.status.value,
            }
            for p in payouts
        ],
    }


# ============================================================================
# Clinical rule sets
# ============================================================================
class RuleSetCreateRequest(BaseModel):
    rule_set_code: str
    name: str
    vital_thresholds: dict
    red_flag_symptoms: list
    escalation_levels: dict
    allergy_check_required: bool = True
    refusal_of_care_protocol: Optional[dict] = None
    insurance_coverage_rules: Optional[dict] = None


@router.get("/clinical-rule-sets")
async def list_rule_sets(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(ClinicalRuleSet).order_by(ClinicalRuleSet.created_at.desc()))
    return [
        {
            "id": str(r.id),
            "rule_set_code": r.rule_set_code,
            "name": r.name,
            "version": r.version,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows.scalars().all()
    ]


@router.get("/clinical-rule-sets/{rule_set_id}")
async def get_rule_set(
    rule_set_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(ClinicalRuleSet).where(ClinicalRuleSet.id == rule_set_id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Rule set not found")
    return {
        "id": str(r.id),
        "rule_set_code": r.rule_set_code,
        "name": r.name,
        "version": r.version,
        "is_active": r.is_active,
        "vital_thresholds": r.vital_thresholds,
        "red_flag_symptoms": r.red_flag_symptoms,
        "escalation_levels": r.escalation_levels,
        "allergy_check_required": r.allergy_check_required,
        "refusal_of_care_protocol": r.refusal_of_care_protocol,
        "insurance_coverage_rules": r.insurance_coverage_rules,
    }


@router.post("/clinical-rule-sets")
async def create_rule_set(
    payload: RuleSetCreateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    r = ClinicalRuleSet(
        rule_set_code=payload.rule_set_code,
        name=payload.name,
        vital_thresholds=payload.vital_thresholds,
        red_flag_symptoms=payload.red_flag_symptoms,
        escalation_levels=payload.escalation_levels,
        allergy_check_required=payload.allergy_check_required,
        refusal_of_care_protocol=payload.refusal_of_care_protocol,
        insurance_coverage_rules=payload.insurance_coverage_rules,
        created_by=current.id,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return {"id": str(r.id), "rule_set_code": r.rule_set_code}


# ============================================================================
# Consents (global admin view)
# ============================================================================
@router.get("/consents")
async def list_consents(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(ConsentRecord, Patient).join(Patient, Patient.id == ConsentRecord.patient_id).order_by(ConsentRecord.given_at.desc()).limit(200)
    )
    return [
        {
            "id": str(c.id),
            "patient": p.full_name,
            "type": c.consent_type.value,
            "version": c.consent_text_version,
            "status": c.status.value,
            "signedAt": c.given_at.isoformat() if c.given_at else None,
        }
        for c, p in rows.all()
    ]


# ============================================================================
# Data retention schedules
# ============================================================================
@router.get("/retention-schedules")
async def list_retention_schedules(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(DataRetentionSchedule))
    return [
        {
            "id": str(s.id),
            "entity": s.data_type,
            "policy": f"{s.retention_days} days",
            "lastRun": s.last_run_at.isoformat() if s.last_run_at else None,
            "processed": s.records_processed,
            "active": s.is_active,
        }
        for s in rows.scalars().all()
    ]


# ============================================================================
# Subsidy recipients
# ============================================================================
@router.get("/subsidy-recipients")
async def list_subsidy_recipients(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(SubsidyEligibility, ConsumerProfile, User)
        .join(ConsumerProfile, ConsumerProfile.id == SubsidyEligibility.consumer_id)
        .join(User, User.id == ConsumerProfile.user_id)
        .where(SubsidyEligibility.subsidy_type != "none")
    )
    return [
        {
            "id": str(s.id),
            "patient": u.full_name,
            "scheme": s.scheme_name or s.subsidy_type.value,
            "verified": s.verified,
            "subsidy_percent": float(s.subsidy_percent),
            "expires": s.valid_until.isoformat() if s.valid_until else None,
        }
        for s, cp, u in rows.all()
    ]


# ============================================================================
# Operations command-center snapshot — real backend aggregate for
# ops-dashboard.tsx. Replaces the client-side orchestration-engine
# derivation (useAdminOperationsSnapshot) with live DB-backed counts,
# a cross-workflow priority feed, and a recent-activity feed sourced from
# the audit log.
# ============================================================================
_UNCLAIMED_BOOKING_STATUSES = (BookingStatus.confirmed, BookingStatus.rematch_pending)
_CLAIMED_INFLIGHT_STATUSES = (BookingStatus.assigned, BookingStatus.worker_en_route, BookingStatus.worker_arrived, BookingStatus.in_progress)
_ACTIVE_ESCALATION_STATUSES = (EscalationStatus.open, EscalationStatus.acknowledged, EscalationStatus.investigating)
_URGENT_ESCALATION_LEVELS = (EscalationLevel.emergency, EscalationLevel.contact_doctor)
_OPEN_DISPUTE_STATUSES = (DisputeStatus.open, DisputeStatus.investigating)


@router.get("/ops-snapshot")
async def ops_snapshot(current: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    unclaimed = (await db.execute(
        select(func.count(Booking.id)).where(Booking.status.in_(_UNCLAIMED_BOOKING_STATUSES))
    )).scalar() or 0
    claimed = (await db.execute(
        select(func.count(Booking.id)).where(Booking.status.in_(_CLAIMED_INFLIGHT_STATUSES))
    )).scalar() or 0

    active_escalations = (await db.execute(
        select(func.count(Escalation.id)).where(Escalation.status.in_(_ACTIVE_ESCALATION_STATUSES))
    )).scalar() or 0
    opened_24h = (await db.execute(
        select(func.count(Escalation.id)).where(Escalation.created_at >= day_ago)
    )).scalar() or 0
    recovered_24h = (await db.execute(
        select(func.count(Escalation.id)).where(Escalation.status == EscalationStatus.resolved, Escalation.resolved_at >= day_ago)
    )).scalar() or 0
    sla_breached = (await db.execute(
        select(func.count(Escalation.id)).where(
            Escalation.status.in_(_ACTIVE_ESCALATION_STATUSES),
            Escalation.sla_breach_at.is_not(None),
            Escalation.sla_breach_at < now,
        )
    )).scalar() or 0

    open_disputes = (await db.execute(
        select(func.count(Dispute.id)).where(Dispute.status.in_(_OPEN_DISPUTE_STATUSES))
    )).scalar() or 0

    # Priority feed — top escalations, urgent unclaimed bookings, open disputes.
    esc_rows = (await db.execute(
        select(Escalation, Patient)
        .join(Patient, Patient.id == Escalation.patient_id)
        .where(Escalation.status.in_(_ACTIVE_ESCALATION_STATUSES))
        .order_by(Escalation.created_at.desc())
        .limit(8)
    )).all()
    priority_feed = []
    for e, p in esc_rows:
        priority_feed.append({
            "workflow": "escalation",
            "id": str(e.id),
            "title": p.full_name,
            "subtitle": e.trigger_type or "escalation",
            "priority": "urgent" if e.level in _URGENT_ESCALATION_LEVELS else "high",
            "state": e.status.value,
            "entered_at": e.created_at.isoformat(),
        })

    booking_rows = (await db.execute(
        select(Booking, Patient)
        .join(Patient, Patient.id == Booking.patient_id)
        .where(Booking.status.in_(_UNCLAIMED_BOOKING_STATUSES), Booking.is_urgent.is_(True))
        .order_by(Booking.scheduled_date.asc())
        .limit(6)
    )).all()
    for b, p in booking_rows:
        priority_feed.append({
            "workflow": "booking",
            "id": b.booking_ref,
            "title": p.full_name,
            "subtitle": "unclaimed urgent booking",
            "priority": "urgent",
            "state": b.status.value,
            "entered_at": b.created_at.isoformat(),
        })

    dispute_rows = (await db.execute(
        select(Dispute, Booking)
        .join(Booking, Booking.id == Dispute.booking_id)
        .where(Dispute.status.in_(_OPEN_DISPUTE_STATUSES))
        .order_by(Dispute.created_at.desc())
        .limit(6)
    )).all()
    for d, b in dispute_rows:
        priority_feed.append({
            "workflow": "dispute",
            "id": str(d.id),
            "title": f"Booking {b.booking_ref}",
            "subtitle": d.dispute_type.value,
            "priority": "high",
            "state": d.status.value,
            "entered_at": d.created_at.isoformat(),
        })

    _priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    priority_feed.sort(key=lambda it: (_priority_rank.get(it["priority"], 9), it["entered_at"]), reverse=False)

    # Recent intervention feed — from the real audit log.
    audit_rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(15)
    )).scalars().all()
    actor_ids = {a.actor_id for a in audit_rows if a.actor_id}
    actors: dict = {}
    if actor_ids:
        ures = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: (u.full_name or u.email or str(u.id)) for u in ures.scalars().all()}
    intervention_feed = [
        {
            "id": str(a.id),
            "workflow": a.entity_type or "system",
            "entity_id": a.entity_id,
            "action": a.action,
            "actor": actors.get(a.actor_id, a.actor_type or "system"),
            "at": a.created_at.isoformat(),
        }
        for a in audit_rows
    ]
    last_intervention_at = audit_rows[0].created_at.isoformat() if audit_rows else None

    urgent_count = sum(1 for it in priority_feed if it["priority"] == "urgent")

    return {
        "dispatch": {"unclaimed": unclaimed, "claimed": claimed},
        "escalations": {"active": active_escalations, "opened_24h": opened_24h, "recovered_24h": recovered_24h},
        "sla_breached": sla_breached,
        "disputes_open": open_disputes,
        "urgent": urgent_count,
        "priority_feed": priority_feed[:15],
        "intervention_feed": intervention_feed,
        "last_intervention_at": last_intervention_at,
    }


# ============================================================================
# Role definitions — admin manages display name / description / permission
# list for the staff roles that exist as UserRole enum values (operations,
# support, clinical_training_lead, clinical_trainer). Note: role_key is
# fixed to those 4 enum values — adding a genuinely NEW role name requires a
# code change (see add_staff_role_enum_values.py), since UserRole is a
# native Postgres enum. The "permissions" list here is descriptive metadata
# shown in the admin UI; the actual access control enforcement lives in the
# require_operations / require_support / require_clinical_* dependencies in
# app/core/deps.py, not in this table.
# ============================================================================
STAFF_ROLE_KEYS = (
    UserRole.operations.value,
    UserRole.support.value,
    UserRole.clinical_training_lead.value,
    UserRole.clinical_trainer.value,
)

DEFAULT_ROLE_DEFINITIONS = {
    UserRole.operations.value: {
        "display_name": "Operations",
        "description": "Creates and manages support, clinical training lead, and clinical trainer accounts. Manages FAQs.",
        "permissions": ["staff.create_support", "staff.create_clinical_training_lead", "staff.create_clinical_trainer", "faq.manage"],
    },
    UserRole.support.value: {
        "display_name": "Support",
        "description": "Receives and resolves customer/nurse support tickets.",
        "permissions": ["tickets.view", "tickets.resolve"],
    },
    UserRole.clinical_training_lead.value: {
        "display_name": "Clinical Training Lead",
        "description": "Reviews and approves training content submitted by clinical trainers before it publishes to nurses.",
        "permissions": ["training.review", "training.approve", "training.publish"],
    },
    UserRole.clinical_trainer.value: {
        "display_name": "Clinical Trainer",
        "description": "Authors training modules and MCQ assessments for review.",
        "permissions": ["training.create", "training.submit_for_review"],
    },
}


class RoleDefinitionUpsertRequest(BaseModel):
    role_key: str
    display_name: str
    description: Optional[str] = None
    permissions: list[str] = []
    is_active: bool = True


def _serialize_role_def(r: RoleDefinition) -> dict:
    return {
        "id": str(r.id),
        "role_key": r.role_key.value,
        "display_name": r.display_name,
        "description": r.description,
        "permissions": r.permissions or [],
        "is_active": r.is_active,
        "updated_at": r.updated_at.isoformat(),
    }


@router.get("/roles")
async def list_role_definitions(current: CurrentUser = Depends(require_operations), db: AsyncSession = Depends(get_db)):
    """Staff role catalog — operations reads this to populate the role
    dropdown when creating new staff accounts. Any staff role without a
    RoleDefinition row yet falls back to its shipped default."""
    rows = (await db.execute(select(RoleDefinition))).scalars().all()
    by_key = {r.role_key.value: r for r in rows}
    out = []
    for key in STAFF_ROLE_KEYS:
        if key in by_key:
            out.append(_serialize_role_def(by_key[key]))
        else:
            d = DEFAULT_ROLE_DEFINITIONS[key]
            out.append({
                "id": None,
                "role_key": key,
                "display_name": d["display_name"],
                "description": d["description"],
                "permissions": d["permissions"],
                "is_active": True,
                "updated_at": None,
            })
    return out


@router.put("/roles/{role_key}")
async def upsert_role_definition(
    role_key: str,
    payload: RoleDefinitionUpsertRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if role_key not in STAFF_ROLE_KEYS:
        raise HTTPException(status_code=400, detail=f"role_key must be one of {STAFF_ROLE_KEYS}")
    res = await db.execute(select(RoleDefinition).where(RoleDefinition.role_key == role_key))
    r = res.scalar_one_or_none()
    if not r:
        r = RoleDefinition(role_key=UserRole(role_key), created_by=current.id)
        db.add(r)
    r.display_name = payload.display_name
    r.description = payload.description
    r.permissions = payload.permissions
    r.is_active = payload.is_active
    await db.commit()
    await db.refresh(r)
    return _serialize_role_def(r)


# ============================================================================
# Staff account creation.
#   - Admin creates operations accounts (only admin can).
#   - Operations (or admin) creates support / clinical_training_lead /
#     clinical_trainer accounts.
# Staff accounts are provisioned directly active — no self-registration or
# email-verification loop, since a trusted admin/ops user is vouching for them.
# ============================================================================
def _staff_normalize_email(email: str) -> str:
    return email.strip().lower()


def _staff_normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "")
    if not p.startswith("+"):
        p = f"+91{p}" if len(p) == 10 and p.isdigit() else f"+{p}"
    return p


def _staff_validate_password(password: str) -> None:
    if (
        len(password) < 8
        or len(password.encode("utf-8")) > 72
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"\d", password)
    ):
        raise HTTPException(status_code=400, detail="Password must be 8-72 chars and include uppercase, lowercase, and a number")


class StaffCreateRequest(BaseModel):
    full_name: str
    email: str
    phone_e164: str
    password: str
    role: str


async def _create_staff_user(payload: StaffCreateRequest, db: AsyncSession) -> User:
    _staff_validate_password(payload.password)
    email = _staff_normalize_email(payload.email)
    phone = _staff_normalize_phone(payload.phone_e164)

    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        full_name=payload.full_name,
        email=email,
        phone_e164=phone,
        password_hash=hash_password(payload.password),
        role=UserRole(payload.role),
        status=UserStatus.active,
        email_verified_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _serialize_staff_user(u: User) -> dict:
    return {"id": str(u.id), "full_name": u.full_name, "email": u.email, "phone_e164": u.phone_e164, "role": u.role.value}


@router.post("/staff/operations")
async def create_operations_account(
    payload: StaffCreateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Only admin can create operations accounts."""
    if payload.role != UserRole.operations.value:
        raise HTTPException(status_code=400, detail="This endpoint only creates operations accounts")
    user = await _create_staff_user(payload, db)
    return _serialize_staff_user(user)


@router.post("/staff/reviewer")
async def create_reviewer_account(
    payload: StaffCreateRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Only admin can create reviewer accounts. Reviewers review nurse
    onboarding documents and background checks — separate from the
    support/clinical_training_lead/clinical_trainer roles operations
    manages via /staff, so this gets its own admin-only endpoint (same
    shape as /staff/operations)."""
    if payload.role != UserRole.reviewer.value:
        raise HTTPException(status_code=400, detail="This endpoint only creates reviewer accounts")
    user = await _create_staff_user(payload, db)
    return _serialize_staff_user(user)


@router.post("/staff")
async def create_staff_account(
    payload: StaffCreateRequest,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    """Operations (or admin) creates support / clinical_training_lead /
    clinical_trainer accounts. Cannot create operations or admin accounts
    from here — operations accounts are admin-only (see /staff/operations)."""
    allowed = (UserRole.support.value, UserRole.clinical_training_lead.value, UserRole.clinical_trainer.value)
    if payload.role not in allowed:
        raise HTTPException(status_code=400, detail=f"role must be one of {allowed}")
    user = await _create_staff_user(payload, db)
    return _serialize_staff_user(user)


@router.get("/staff")
async def list_staff_accounts(current: CurrentUser = Depends(require_operations), db: AsyncSession = Depends(get_db)):
    """All internal staff accounts (operations, support, clinical training roles)."""
    staff_roles = (UserRole.operations, UserRole.support, UserRole.clinical_training_lead, UserRole.clinical_trainer)
    rows = (await db.execute(select(User).where(User.role.in_(staff_roles)).order_by(User.created_at.desc()))).scalars().all()
    return [_serialize_staff_user(u) for u in rows]