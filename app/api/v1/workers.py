"""Worker endpoints: profile, search, public, availability, bank, kit, documents."""
from datetime import date, datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_worker_profile,
    require_roles,
)
from app.integrations import cloudinary_client
from app.models.enums import (
    UserRole,
    WorkerAvailability,
    WorkerOnboardingStatus,
    WorkerPreferenceStatus,
    WorkerQualificationSource,
    WorkerQualificationStatus,
)
from app.models.models import (
    CarePackage,
    ServiceCatalogue,
    User,
    WorkerCertificate,
    WorkerDocument,
    WorkerKitItem,
    WorkerProfile,
    WorkerServicePreference,
    WorkerServiceQualification,
)
from app.schemas.schemas import (
    AvailabilityToggleRequest,
    BankDetailsUpdate,
    WorkerLocationUpdateRequest,
    WorkerProfileOut,
    WorkerProfileUpdate,
    WorkerPublicOut,
    WorkerSearchQuery,
)
from app.services.common_services import audit
from app.services.qualification import (
    is_worker_opted_in_for_service,
    is_worker_qualified_for_service,
)

router = APIRouter(prefix="/workers", tags=["workers"])

from app.models.enums import WorkerType

# Required documents differ by worker type. Nurses are clinically qualified;
# caregivers are non-clinical helpers, so no nursing license is demanded of them.
REQUIRED_DOCUMENTS_BY_TYPE = {
    WorkerType.nurse: {"aadhaar", "nursing_license", "degree_certificate", "police_verification"},
    WorkerType.caregiver: {"aadhaar", "police_verification"},
}
# Optional / supporting documents (uploaded to strengthen the profile / unlock
# more services), never block submission.
OPTIONAL_DOCUMENTS_BY_TYPE = {
    WorkerType.nurse: {"experience_certificate", "specialization_certificate"},
    WorkerType.caregiver: {"caregiver_training_certificate", "degree_certificate", "experience_certificate"},
}
# Human-readable labels for the app.
DOCUMENT_LABELS = {
    "aadhaar": "Aadhaar Card",
    "nursing_license": "Nursing Registration / License",
    "degree_certificate": "Degree / Education Certificate",
    "police_verification": "Police Verification",
    "experience_certificate": "Experience Certificate",
    "specialization_certificate": "Specialization Certificate",
    "caregiver_training_certificate": "Caregiver Training Certificate",
}


def _required_docs(profile) -> set:
    return REQUIRED_DOCUMENTS_BY_TYPE.get(getattr(profile, "worker_type", WorkerType.nurse), REQUIRED_DOCUMENTS_BY_TYPE[WorkerType.nurse])


def _optional_docs(profile) -> set:
    return OPTIONAL_DOCUMENTS_BY_TYPE.get(getattr(profile, "worker_type", WorkerType.nurse), set())


def _all_allowed_docs(profile) -> set:
    return _required_docs(profile) | _optional_docs(profile)


def _doc_catalogue(profile) -> list:
    req, opt = _required_docs(profile), _optional_docs(profile)
    out = [{"type": t, "label": DOCUMENT_LABELS.get(t, t), "required": True} for t in sorted(req)]
    out += [{"type": t, "label": DOCUMENT_LABELS.get(t, t), "required": False} for t in sorted(opt)]
    return out


# Back-compat alias (old code referenced this flat set).
REQUIRED_WORKER_DOCUMENTS = REQUIRED_DOCUMENTS_BY_TYPE[WorkerType.nurse]


class WorkerDocumentUploadRequest(BaseModel):
    document_type: str
    data_base64: str
    document_number: Optional[str] = None
    valid_until: Optional[date] = None


async def _onboarding_snapshot(
    profile: WorkerProfile,
    db: AsyncSession,
) -> dict:
    user_res = await db.execute(select(User).where(User.id == profile.user_id))
    user = user_res.scalar_one()
    docs_res = await db.execute(
        select(WorkerDocument).where(WorkerDocument.worker_id == profile.id)
    )
    docs = list(docs_res.scalars().all())
    uploaded_types = {d.document_type for d in docs}

    missing_profile_fields = []
    profile_values = {
        "full_name": user.full_name,
        "date_of_birth": profile.date_of_birth,
        "registration_no": profile.registration_no,
        "registration_authority": profile.registration_authority,
        "registration_valid_until": profile.registration_valid_until,
        "base_city": profile.base_city,
    }
    for field, value in profile_values.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            missing_profile_fields.append(field)
    if profile.registration_valid_until and profile.registration_valid_until < date.today():
        missing_profile_fields.append("registration_valid_until_not_expired")

    missing_documents = sorted(_required_docs(profile) - uploaded_types)
    rejected_documents = sorted(
        d.document_type for d in docs if d.verification_status == "rejected"
    )
    return {
        "onboarding_status": profile.onboarding_status.value,
        "worker_type": getattr(profile, "worker_type", WorkerType.nurse).value,
        "background_check_status": profile.background_check_status,
        "documents": _doc_catalogue(profile),
        "missing_profile_fields": missing_profile_fields,
        "missing_documents": missing_documents,
        "rejected_documents": rejected_documents,
        "can_submit_for_review": not missing_profile_fields and not missing_documents and not rejected_documents,
        "submitted_at": profile.onboarding_submitted_at,
        "reviewed_at": profile.onboarding_reviewed_at,
        "rejection_reason": profile.onboarding_rejection_reason,
    }


@router.get("/me", response_model=WorkerProfileOut)
async def my_worker_profile(profile: WorkerProfile = Depends(get_worker_profile)):
    return WorkerProfileOut.model_validate(profile)


@router.put("/me", response_model=WorkerProfileOut)
async def update_my_worker_profile(
    payload: WorkerProfileUpdate,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    changes = payload.model_dump(exclude_unset=True)
    credential_fields = {
        "date_of_birth",
        "registration_no",
        "registration_authority",
        "registration_valid_until",
    }
    for field, value in changes.items():
        setattr(profile, field, value)
    if credential_fields.intersection(changes) and profile.onboarding_status in (
        WorkerOnboardingStatus.pending_review,
        WorkerOnboardingStatus.approved,
        WorkerOnboardingStatus.rejected,
    ):
        profile.onboarding_status = WorkerOnboardingStatus.documents_pending
        profile.onboarding_rejection_reason = None
        profile.availability = WorkerAvailability.offline
    await db.commit()
    await db.refresh(profile)
    return WorkerProfileOut.model_validate(profile)


@router.put("/me/availability", response_model=WorkerProfileOut)
async def toggle_availability(
    payload: AvailabilityToggleRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    if (
        payload.availability == WorkerAvailability.online
        and profile.onboarding_status != WorkerOnboardingStatus.approved
    ):
        raise HTTPException(
            status_code=403,
            detail="Caregiver verification must be approved before going online",
        )
    profile.availability = payload.availability
    await db.commit()
    await db.refresh(profile)
    return WorkerProfileOut.model_validate(profile)


# Patch 3 — Worker current-location ping for Haversine proximity dispatch.
# Reuses existing Patch 2 worker JWT auth via ``get_worker_profile``.
@router.post("/me/location")
async def update_my_location(
    payload: WorkerLocationUpdateRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Worker pushes current GPS coordinates.

    Stored on the worker profile (used inline by the request-visibility filter)
    and also appended to ``worker_location_log`` so we keep an audit trail.
    Authenticated worker only — workers can update only their own location.
    """
    from app.models.models import WorkerLocationLog
    now = datetime.now(timezone.utc)
    profile.current_latitude = payload.latitude
    profile.current_longitude = payload.longitude
    profile.current_location_updated_at = payload.captured_at or now
    profile.current_location_accuracy = payload.accuracy
    db.add(
        WorkerLocationLog(
            worker_id=profile.id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            accuracy_metres=payload.accuracy,
            is_offline=False,
            synced_at=now,
        )
    )
    await db.commit()
    return {
        "ok": True,
        "current_latitude": float(profile.current_latitude),
        "current_longitude": float(profile.current_longitude),
        "current_location_updated_at": profile.current_location_updated_at.isoformat(),
        "current_location_accuracy": profile.current_location_accuracy,
    }


@router.put("/me/bank-details", response_model=WorkerProfileOut)
async def update_bank_details(
    payload: BankDetailsUpdate,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    profile.bank_account_holder = payload.bank_account_holder
    profile.bank_account_number = payload.bank_account_number
    profile.bank_ifsc = payload.bank_ifsc
    await db.commit()
    await db.refresh(profile)
    return WorkerProfileOut.model_validate(profile)


@router.get("/search", response_model=List[WorkerPublicOut])
async def search_workers(
    city: Optional[str] = None,
    min_tier: Optional[str] = None,
    gender: Optional[str] = None,
    available_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search workers — accessible by consumers and admins."""
    if current.role not in (UserRole.consumer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Not authorised")

    conds = [WorkerProfile.onboarding_status == WorkerOnboardingStatus.approved]
    if city:
        conds.append(WorkerProfile.base_city == city)
    if gender:
        conds.append(WorkerProfile.gender == gender)
    if available_only:
        conds.append(WorkerProfile.availability == WorkerAvailability.online)
    if min_tier:
        conds.append(WorkerProfile.tier == min_tier)

    stmt = (
        select(WorkerProfile, User)
        .join(User, User.id == WorkerProfile.user_id)
        .where(and_(*conds))
        .order_by(WorkerProfile.rating_average.desc(), WorkerProfile.completed_visits_count.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    res = await db.execute(stmt)
    items = []
    for wp, user in res.all():
        items.append(
            WorkerPublicOut(
                id=wp.id,
                full_name=user.full_name,
                avatar_url=user.avatar_url,
                tier=wp.tier,
                gender=wp.gender,
                bio=wp.bio,
                years_of_experience=wp.years_of_experience,
                languages_spoken=wp.languages_spoken,
                specialisations=wp.specialisations,
                rating_average=wp.rating_average,
                rating_count=wp.rating_count,
                completed_visits_count=wp.completed_visits_count,
                availability=wp.availability,
                base_city=wp.base_city,
            )
        )
    return items


@router.get("/{worker_id}/public", response_model=WorkerPublicOut)
async def public_worker_profile(worker_id: UUID, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    res = await db.execute(
        select(WorkerProfile, User).join(User, User.id == WorkerProfile.user_id).where(WorkerProfile.id == worker_id)
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp, user = row
    return WorkerPublicOut(
        id=wp.id,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        tier=wp.tier,
        gender=wp.gender,
        bio=wp.bio,
        years_of_experience=wp.years_of_experience,
        languages_spoken=wp.languages_spoken,
        specialisations=wp.specialisations,
        rating_average=wp.rating_average,
        rating_count=wp.rating_count,
        completed_visits_count=wp.completed_visits_count,
        availability=wp.availability,
        base_city=wp.base_city,
    )


# ----- Documents -----
@router.get("/me/onboarding")
async def my_onboarding_status(
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    return await _onboarding_snapshot(profile, db)


@router.post("/me/onboarding/submit")
async def submit_onboarding_for_review(
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    if profile.onboarding_status == WorkerOnboardingStatus.approved:
        return await _onboarding_snapshot(profile, db)
    snapshot = await _onboarding_snapshot(profile, db)
    if not snapshot["can_submit_for_review"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Complete the caregiver profile and required documents first",
                "missing_profile_fields": snapshot["missing_profile_fields"],
                "missing_documents": snapshot["missing_documents"],
                "rejected_documents": snapshot["rejected_documents"],
            },
        )
    profile.onboarding_status = WorkerOnboardingStatus.pending_review
    profile.onboarding_submitted_at = datetime.now(timezone.utc)
    profile.onboarding_rejection_reason = None
    if profile.background_check_status in ("pending", "failed"):
        # This queues the check for a provider/admin integration. It is never
        # treated as passed until an explicit result is recorded.
        profile.background_check_status = "queued"
    await audit(
        db,
        profile.user_id,
        "worker",
        "worker.onboarding_submitted",
        "worker",
        profile.id,
    )
    # Create and auto-assign a review ticket to the best available reviewer.
    from app.services.reviewer_assignment import get_or_create_ticket
    await get_or_create_ticket(db, nurse_id=profile.id, priority="NORMAL")
    await db.commit()
    return await _onboarding_snapshot(profile, db)


@router.post("/me/documents")
async def upload_document(
    document_type: str,
    cloudinary_url: str,
    cloudinary_public_id: str,
    document_number: Optional[str] = None,
    valid_until: Optional[date] = None,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    if document_type not in _all_allowed_docs(profile):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported document type. Allowed: {sorted(_all_allowed_docs(profile))}",
        )
    doc = WorkerDocument(
        worker_id=profile.id,
        document_type=document_type,
        document_number=document_number,
        cloudinary_url=cloudinary_url,
        cloudinary_public_id=cloudinary_public_id,
        valid_until=valid_until,
    )
    db.add(doc)
    if profile.onboarding_status in (
        WorkerOnboardingStatus.pending_review,
        WorkerOnboardingStatus.approved,
        WorkerOnboardingStatus.rejected,
    ):
        profile.onboarding_status = WorkerOnboardingStatus.documents_pending
        profile.onboarding_rejection_reason = None
        profile.availability = WorkerAvailability.offline
    await db.commit()
    await db.refresh(doc)
    return {"id": str(doc.id), "verification_status": doc.verification_status}


@router.post("/me/documents/upload")
async def upload_document_file(
    payload: WorkerDocumentUploadRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    if payload.document_type not in _all_allowed_docs(profile):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported document type. Allowed: {sorted(_all_allowed_docs(profile))}",
        )
    upload = await cloudinary_client.upload_base64(
        payload.data_base64,
        folder=f"nurseconnect/workers/{profile.id}",
        resource_type="auto",
    )
    doc = WorkerDocument(
        worker_id=profile.id,
        document_type=payload.document_type,
        document_number=payload.document_number,
        cloudinary_url=upload["secure_url"],
        cloudinary_public_id=upload["public_id"],
        valid_until=payload.valid_until,
    )
    db.add(doc)
    if profile.onboarding_status in (
        WorkerOnboardingStatus.pending_review,
        WorkerOnboardingStatus.approved,
        WorkerOnboardingStatus.rejected,
    ):
        profile.onboarding_status = WorkerOnboardingStatus.documents_pending
        profile.onboarding_rejection_reason = None
        profile.availability = WorkerAvailability.offline
    await db.commit()
    await db.refresh(doc)
    return {"id": str(doc.id), "verification_status": doc.verification_status}


@router.get("/me/documents")
async def list_documents(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WorkerDocument).where(WorkerDocument.worker_id == profile.id))
    docs = res.scalars().all()
    return [
        {
            "id": str(d.id),
            "document_type": d.document_type,
            "document_number": d.document_number,
            "cloudinary_url": d.cloudinary_url,
            "verification_status": d.verification_status,
            "valid_until": d.valid_until.isoformat() if d.valid_until else None,
            "created_at": d.created_at.isoformat(),
        }
        for d in docs
    ]


# ----- Certificates -----
@router.get("/me/certificates")
async def list_certificates(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WorkerCertificate).where(WorkerCertificate.worker_id == profile.id))
    items = res.scalars().all()
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "issued_by": c.issued_by,
            "issued_on": c.issued_on.isoformat() if c.issued_on else None,
            "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            "cloudinary_url": c.cloudinary_url,
        }
        for c in items
    ]


# ----- Kit -----
@router.get("/me/kit")
async def list_kit(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WorkerKitItem).where(WorkerKitItem.worker_id == profile.id))
    return [
        {
            "id": str(k.id),
            "item_code": k.item_code,
            "item_name": k.item_name,
            "is_present": k.is_present,
            "last_checked_at": k.last_checked_at.isoformat() if k.last_checked_at else None,
            "notes": k.notes,
        }
        for k in res.scalars().all()
    ]


@router.put("/me/kit/{kit_id}")
async def update_kit_item(
    kit_id: UUID,
    is_present: bool,
    notes: Optional[str] = None,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(WorkerKitItem).where(WorkerKitItem.id == kit_id, WorkerKitItem.worker_id == profile.id))
    item = res.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Kit item not found")
    item.is_present = is_present
    item.notes = notes
    item.last_checked_at = datetime.now(timezone.utc)
    # Update kit_complete flag on profile
    all_items_res = await db.execute(select(WorkerKitItem).where(WorkerKitItem.worker_id == profile.id))
    profile.kit_complete = all(i.is_present for i in all_items_res.scalars().all())
    await db.commit()
    return {"ok": True, "kit_complete": profile.kit_complete}


# ----- Earnings -----
@router.get("/me/earnings")
async def my_earnings(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    from app.models.models import WorkerPayout
    res = await db.execute(select(WorkerPayout).where(WorkerPayout.worker_id == profile.id).order_by(WorkerPayout.created_at.desc()))
    payouts = res.scalars().all()
    total_paid = sum((p.net_amount for p in payouts if p.status.value == "paid"), 0)
    total_pending = sum((p.net_amount for p in payouts if p.status.value in ("pending", "on_hold", "processing")), 0)
    return {
        "total_paid": float(total_paid),
        "total_pending": float(total_pending),
        "payouts": [
            {
                "id": str(p.id),
                "booking_id": str(p.booking_id),
                "gross_amount": float(p.gross_amount),
                "tds_deducted": float(p.tds_deducted),
                "net_amount": float(p.net_amount),
                "status": p.status.value,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                "created_at": p.created_at.isoformat(),
            }
            for p in payouts
        ],
    }


# ============================================================================
# Patch 2 — Service eligibility + preference management
# ============================================================================
class ServiceEligibilityItem(BaseModel):
    target_type: str  # "service" | "package"
    id: UUID
    code: str
    name: str
    category: Optional[str] = None
    min_tier: Optional[str] = None
    risk_level: Optional[str] = None
    qualification_status: str
    qualification_source: Optional[str] = None
    preference_status: str
    willing_to_accept: bool
    can_opt_in: bool
    locked_reason: Optional[str] = None
    requires_admin_skill_approval: bool = False


class ServicePreferenceUpdate(BaseModel):
    target_type: str  # "service" | "package"
    target_id: UUID
    preference_status: WorkerPreferenceStatus
    notes: Optional[str] = None
    preferred_radius_km: Optional[int] = None


class ServiceQualificationRequest(BaseModel):
    target_type: str  # "service" | "package"
    target_id: UUID


@router.get("/me/service-eligibility", response_model=List[ServiceEligibilityItem])
async def my_service_eligibility(
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """List every active care package (admin-managed, no standalone
    services) with the worker's current qualification + preference status
    and whether they may opt in. No price is ever included here — nurses
    see packages purely as opt-in offerings gated by training/assessments."""
    items: List[ServiceEligibilityItem] = []

    pres = await db.execute(select(CarePackage).where(CarePackage.is_active.is_(True)))
    packages = list(pres.scalars().all())

    # Pre-fetch qualifications & preferences for this worker (single-pass).
    qres = await db.execute(
        select(WorkerServiceQualification).where(
            WorkerServiceQualification.worker_id == profile.id
        )
    )
    qmap_pkg = {q.package_id: q for q in qres.scalars().all() if q.package_id is not None}

    prres = await db.execute(
        select(WorkerServicePreference).where(
            WorkerServicePreference.worker_id == profile.id
        )
    )
    pmap_pkg = {p.package_id: p for p in prres.scalars().all() if p.package_id is not None}

    for pkg in packages:
        q = qmap_pkg.get(pkg.id)
        p = pmap_pkg.get(pkg.id)
        q_status = q.qualification_status.value if q else WorkerQualificationStatus.NOT_QUALIFIED.value
        q_source = q.qualification_source.value if (q and q.qualification_source) else None
        p_status = p.preference_status.value if p else WorkerPreferenceStatus.OPTED_OUT.value
        willing = bool(p.willing_to_accept) if p else False

        qualified, locked_reason = await is_worker_qualified_for_service(profile, pkg, db)

        items.append(ServiceEligibilityItem(
            target_type="package",
            id=pkg.id,
            code=pkg.package_code,
            name=pkg.name,
            category=None,
            min_tier=pkg.min_tier.value if pkg.min_tier else None,
            risk_level=pkg.risk_level.value if pkg.risk_level else None,
            qualification_status=q_status,
            qualification_source=q_source,
            preference_status=p_status,
            willing_to_accept=willing,
            can_opt_in=qualified,
            locked_reason=None if qualified else locked_reason,
            requires_admin_skill_approval=bool(pkg.requires_admin_skill_approval),
        ))

    return items


@router.put("/me/service-preferences", response_model=ServiceEligibilityItem)
async def update_service_preference(
    payload: ServicePreferenceUpdate,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Opt the worker in/out of a specific service or package.

    Rules:
      - Worker can OPT_IN only when qualification_status == APPROVED.
      - Worker can always OPT_OUT or PAUSE.
    """
    if payload.target_type not in ("service", "package"):
        raise HTTPException(status_code=400, detail="target_type must be 'service' or 'package'")

    target = None
    if payload.target_type == "service":
        res = await db.execute(
            select(ServiceCatalogue).where(
                ServiceCatalogue.id == payload.target_id,
                ServiceCatalogue.is_active.is_(True),
            )
        )
        target = res.scalar_one_or_none()
    else:
        res = await db.execute(
            select(CarePackage).where(
                CarePackage.id == payload.target_id, CarePackage.is_active.is_(True)
            )
        )
        target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail=f"{payload.target_type} not found or inactive")

    # OPT_IN is gated by qualification. OPT_OUT / PAUSED always allowed.
    if payload.preference_status == WorkerPreferenceStatus.OPTED_IN:
        qualified, locked_reason = await is_worker_qualified_for_service(profile, target, db)
        if not qualified:
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "code": "WORKER_NOT_QUALIFIED_FOR_SERVICE",
                    "message": "You are not yet qualified for this service.",
                    "locked_reason": locked_reason,
                },
            )

    # Upsert preference row
    cond = (
        WorkerServicePreference.service_id == target.id
        if payload.target_type == "service"
        else WorkerServicePreference.package_id == target.id
    )
    pres = await db.execute(
        select(WorkerServicePreference).where(
            and_(WorkerServicePreference.worker_id == profile.id, cond)
        )
    )
    pref = pres.scalar_one_or_none()
    if not pref:
        pref = WorkerServicePreference(
            worker_id=profile.id,
            service_id=target.id if payload.target_type == "service" else None,
            package_id=target.id if payload.target_type == "package" else None,
        )
        db.add(pref)
    pref.preference_status = payload.preference_status
    pref.willing_to_accept = payload.preference_status == WorkerPreferenceStatus.OPTED_IN
    if payload.notes is not None:
        pref.notes = payload.notes
    if payload.preferred_radius_km is not None:
        pref.preferred_radius_km = payload.preferred_radius_km

    await audit(
        db,
        profile.user_id,
        "worker",
        "worker.service_preference.update",
        payload.target_type,
        target.id,
        {"preference_status": payload.preference_status.value},
    )
    await db.commit()
    await db.refresh(pref)

    # Build eligibility response for this single item
    qres = await db.execute(
        select(WorkerServiceQualification).where(
            and_(
                WorkerServiceQualification.worker_id == profile.id,
                (WorkerServiceQualification.service_id == target.id)
                if payload.target_type == "service"
                else (WorkerServiceQualification.package_id == target.id),
            )
        )
    )
    q = qres.scalar_one_or_none()
    qualified, locked_reason = await is_worker_qualified_for_service(profile, target, db)
    return ServiceEligibilityItem(
        target_type=payload.target_type,
        id=target.id,
        code=getattr(target, "service_code", None) or getattr(target, "package_code", ""),
        name=target.name,
        category=getattr(target.category, "value", None) if hasattr(target, "category") else None,
        min_tier=target.min_tier.value if target.min_tier else None,
        risk_level=getattr(target, "risk_level", None).value if getattr(target, "risk_level", None) else None,
        qualification_status=(q.qualification_status.value if q else WorkerQualificationStatus.NOT_QUALIFIED.value),
        qualification_source=(q.qualification_source.value if (q and q.qualification_source) else None),
        preference_status=pref.preference_status.value,
        willing_to_accept=bool(pref.willing_to_accept),
        can_opt_in=qualified,
        locked_reason=None if qualified else locked_reason,
        requires_admin_skill_approval=bool(getattr(target, "requires_admin_skill_approval", False)),
    )

# ---------------------------------------------------------------------------
# Service area — where the worker is based + how far they'll travel. Captured
# during onboarding; drives geo-dispatch when live location isn't fresh.
# PUT /api/workers/me/service-area
# ---------------------------------------------------------------------------
@router.post("/me/service-qualification-requests", response_model=ServiceEligibilityItem)
async def request_service_qualification(
    payload: ServiceQualificationRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Let an approved care professional request reviewer unlock for a locked
    service/package instead of leaving the Services page as a dead end."""
    if payload.target_type not in ("service", "package"):
        raise HTTPException(status_code=400, detail="target_type must be 'service' or 'package'")
    if profile.onboarding_status != WorkerOnboardingStatus.approved:
        raise HTTPException(status_code=403, detail="Care professional must be approved before requesting service unlocks")

    if payload.target_type == "service":
        res = await db.execute(
            select(ServiceCatalogue).where(
                ServiceCatalogue.id == payload.target_id,
                ServiceCatalogue.is_active.is_(True),
            )
        )
        target = res.scalar_one_or_none()
    else:
        res = await db.execute(
            select(CarePackage).where(
                CarePackage.id == payload.target_id,
                CarePackage.is_active.is_(True),
            )
        )
        target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail=f"{payload.target_type} not found or inactive")

    qualified, _ = await is_worker_qualified_for_service(profile, target, db)
    q_status = WorkerQualificationStatus.APPROVED if qualified else WorkerQualificationStatus.QUALIFIED_PENDING_APPROVAL

    cond = (
        WorkerServiceQualification.service_id == target.id
        if payload.target_type == "service"
        else WorkerServiceQualification.package_id == target.id
    )
    qres = await db.execute(
        select(WorkerServiceQualification).where(
            and_(WorkerServiceQualification.worker_id == profile.id, cond)
        )
    )
    qual = qres.scalar_one_or_none()
    if not qual:
        qual = WorkerServiceQualification(
            worker_id=profile.id,
            service_id=target.id if payload.target_type == "service" else None,
            package_id=target.id if payload.target_type == "package" else None,
            qualification_source=WorkerQualificationSource.ADMIN_APPROVAL,
        )
        db.add(qual)
    qual.qualification_status = q_status
    qual.qualification_source = qual.qualification_source or WorkerQualificationSource.ADMIN_APPROVAL

    await audit(
        db,
        profile.user_id,
        "worker",
        "worker.service_qualification.request",
        payload.target_type,
        target.id,
        {"qualification_status": q_status.value},
    )
    await db.commit()
    await db.refresh(qual)

    qualified, locked_reason = await is_worker_qualified_for_service(profile, target, db)
    return ServiceEligibilityItem(
        target_type=payload.target_type,
        id=target.id,
        code=getattr(target, "service_code", None) or getattr(target, "package_code", ""),
        name=target.name,
        category=getattr(target.category, "value", None) if hasattr(target, "category") else None,
        min_tier=target.min_tier.value if target.min_tier else None,
        risk_level=getattr(target, "risk_level", None).value if getattr(target, "risk_level", None) else None,
        qualification_status=qual.qualification_status.value,
        qualification_source=(qual.qualification_source.value if qual.qualification_source else None),
        preference_status=WorkerPreferenceStatus.OPTED_OUT.value,
        willing_to_accept=False,
        can_opt_in=qualified,
        locked_reason=None if qualified else locked_reason,
        requires_admin_skill_approval=bool(getattr(target, "requires_admin_skill_approval", False)),
    )


class ServiceAreaRequest(BaseModel):
    base_city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    service_radius_km: Optional[int] = None


@router.put("/me/service-area")
async def set_service_area(
    payload: ServiceAreaRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    if payload.base_city is not None:
        profile.base_city = payload.base_city.strip() or None
    if payload.latitude is not None and payload.longitude is not None:
        profile.home_latitude = payload.latitude
        profile.home_longitude = payload.longitude
    if payload.service_radius_km is not None:
        profile.service_radius_km = max(1, min(int(payload.service_radius_km), 100))
    await db.commit()
    return {
        "base_city": profile.base_city,
        "home_latitude": float(profile.home_latitude) if profile.home_latitude is not None else None,
        "home_longitude": float(profile.home_longitude) if profile.home_longitude is not None else None,
        "service_radius_km": profile.service_radius_km,
    }
