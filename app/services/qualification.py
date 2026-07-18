"""Patch 2 — Worker package/service qualification + opt-in helpers.

Centralised business logic for determining whether a worker may receive
booking requests for a given service or care package. Booking visibility
and accept endpoints rely on `can_worker_receive_service`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Union
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    QualificationGate,
    UserStatus,
    WorkerOnboardingStatus,
    WorkerPreferenceStatus,
    WorkerQualificationSource,
    WorkerQualificationStatus,
    WorkerTier,
)
from app.models.models import (
    AssessmentModule,
    CarePackage,
    PracticalSignOff,
    ServiceCatalogue,
    TrainingCompletion,
    TrainingModule,
    User,
    WorkerAssessmentAttempt,
    WorkerCertificate,
    WorkerProfile,
    WorkerServicePreference,
    WorkerServiceQualification,
)

ServiceLike = Union[ServiceCatalogue, CarePackage]


_TIER_ORDER = {
    WorkerTier.tier1: 1,
    WorkerTier.tier2: 2,
    WorkerTier.tier3: 3,
    WorkerTier.tier4: 4,
    WorkerTier.tier5: 5,
}


def _tier_value(t: Optional[WorkerTier]) -> int:
    return _TIER_ORDER.get(t, 0) if t is not None else 0


def _is_service(obj: ServiceLike) -> bool:
    return isinstance(obj, ServiceCatalogue)


async def _get_user(db: AsyncSession, worker: WorkerProfile) -> Optional[User]:
    res = await db.execute(select(User).where(User.id == worker.user_id))
    return res.scalar_one_or_none()


async def _has_passed_required_trainings(
    db: AsyncSession, worker: WorkerProfile, codes: list[str]
) -> bool:
    if not codes:
        return True
    res = await db.execute(select(TrainingModule).where(TrainingModule.code.in_(codes)))
    modules = res.scalars().all()
    if len(modules) != len(set(codes)):
        # Some required modules don't exist — treat as not satisfied so we
        # don't accidentally qualify the worker.
        return False
    module_ids = [m.id for m in modules]
    cres = await db.execute(
        select(TrainingCompletion).where(
            TrainingCompletion.worker_id == worker.id,
            TrainingCompletion.module_id.in_(module_ids),
            TrainingCompletion.assessment_passed.is_(True),
        )
    )
    completed_ids = {c.module_id for c in cres.scalars().all()}
    return all(mid in completed_ids for mid in module_ids)


async def _has_valid_certificates(
    db: AsyncSession, worker: WorkerProfile, codes: list[str]
) -> bool:
    if not codes:
        return True
    res = await db.execute(
        select(WorkerCertificate).where(WorkerCertificate.worker_id == worker.id)
    )
    certs = res.scalars().all()
    today = date.today()
    present_names = {
        (c.name or "").strip().upper()
        for c in certs
        if (c.valid_until is None or c.valid_until >= today)
    }
    for code in codes:
        if code.strip().upper() not in present_names:
            return False
    return True


# Patch 4B — assessment linkage helpers
async def _has_passed_required_assessments(
    db: AsyncSession,
    worker: WorkerProfile,
    codes: list[str],
    minimum_pass_score: Optional[int] = None,
) -> bool:
    """All listed assessment codes must have at least one passing attempt by
    this worker. If ``minimum_pass_score`` is given it overrides the
    assessment's own pass score for the purpose of qualification gating.
    """
    if not codes:
        return True
    res = await db.execute(
        select(AssessmentModule).where(AssessmentModule.code.in_(codes))
    )
    asms = res.scalars().all()
    if len(asms) != len(set(codes)):
        return False
    by_id = {a.id: a for a in asms}
    ares = await db.execute(
        select(WorkerAssessmentAttempt).where(
            WorkerAssessmentAttempt.worker_id == worker.id,
            WorkerAssessmentAttempt.assessment_id.in_(list(by_id.keys())),
        )
    )
    attempts = list(ares.scalars().all())
    # Group best score per assessment_id
    best: dict[UUID, int] = {}
    for at in attempts:
        prev = best.get(at.assessment_id, -1)
        if at.passed and at.score > prev:
            best[at.assessment_id] = at.score
    for aid, a in by_id.items():
        score = best.get(aid)
        if score is None:
            return False
        threshold = int(minimum_pass_score) if minimum_pass_score is not None else int(a.pass_score or 0)
        if score < threshold:
            return False
    return True


# Gate 3 — practical sign-off helper
async def _has_passed_practical_signoff(
    db: AsyncSession, worker: WorkerProfile, service: ServiceLike
) -> bool:
    """True if the worker's most recent PracticalSignOff for this
    service/package is a pass. Only the latest sign-off counts — a new
    failed one after an old pass re-locks the service until a new pass."""
    cond = (
        PracticalSignOff.service_id == service.id
        if _is_service(service)
        else PracticalSignOff.package_id == service.id
    )
    res = await db.execute(
        select(PracticalSignOff)
        .where(PracticalSignOff.worker_id == worker.id, cond)
        .order_by(PracticalSignOff.signed_at.desc())
        .limit(1)
    )
    latest = res.scalar_one_or_none()
    return bool(latest and latest.passed)


async def evaluate_and_upsert_qualification_for_practical_signoff(
    db: AsyncSession, worker: WorkerProfile, target: ServiceLike
) -> Optional[WorkerServiceQualification]:
    """Re-evaluate qualification for one service/package after a passing
    practical sign-off. Does NOT auto opt-in."""
    if getattr(target, "gate", None) != QualificationGate.practical_verified:
        return None

    qual = await _get_qualification_row(db, worker.id, target)
    training_codes = list(getattr(target, "required_training_module_codes", None) or [])
    cert_codes = list(getattr(target, "required_certificate_codes", None) or [])
    assessment_codes = list(getattr(target, "required_assessment_codes", None) or [])
    min_pass = getattr(target, "minimum_pass_score", None)
    requires_admin = bool(getattr(target, "requires_admin_skill_approval", False))

    training_ok = await _has_passed_required_trainings(db, worker, training_codes)
    cert_ok = await _has_valid_certificates(db, worker, cert_codes)
    assess_ok = await _has_passed_required_assessments(db, worker, assessment_codes, min_pass)
    practical_ok = await _has_passed_practical_signoff(db, worker, target)
    tier_ok = _tier_value(worker.tier) >= _tier_value(target.min_tier)

    if not qual:
        qual = WorkerServiceQualification(
            worker_id=worker.id,
            service_id=target.id if _is_service(target) else None,
            package_id=None if _is_service(target) else target.id,
            qualification_source=WorkerQualificationSource.TRAINING,
        )
        db.add(qual)

    if not (training_ok and cert_ok and assess_ok and practical_ok and tier_ok):
        qual.qualification_status = WorkerQualificationStatus.TRAINING_REQUIRED
    elif requires_admin and not qual.admin_approved_at:
        qual.qualification_status = WorkerQualificationStatus.QUALIFIED_PENDING_APPROVAL
    else:
        qual.qualification_status = WorkerQualificationStatus.APPROVED
        qual.valid_from = qual.valid_from or datetime.now(timezone.utc)

    await db.flush()
    return qual


async def _get_qualification_row(
    db: AsyncSession, worker_id: UUID, service: ServiceLike
) -> Optional[WorkerServiceQualification]:
    if _is_service(service):
        cond = WorkerServiceQualification.service_id == service.id
    else:
        cond = WorkerServiceQualification.package_id == service.id
    res = await db.execute(
        select(WorkerServiceQualification).where(
            WorkerServiceQualification.worker_id == worker_id,
            cond,
        )
    )
    return res.scalar_one_or_none()


async def _get_preference_row(
    db: AsyncSession, worker_id: UUID, service: ServiceLike
) -> Optional[WorkerServicePreference]:
    if _is_service(service):
        cond = WorkerServicePreference.service_id == service.id
    else:
        cond = WorkerServicePreference.package_id == service.id
    res = await db.execute(
        select(WorkerServicePreference).where(
            WorkerServicePreference.worker_id == worker_id,
            cond,
        )
    )
    return res.scalar_one_or_none()


async def is_worker_qualified_for_service(
    worker: WorkerProfile, service: ServiceLike, db: AsyncSession
) -> tuple[bool, Optional[str]]:
    """Return (qualified, locked_reason). qualified=True only when all gates pass."""
    user = await _get_user(db, worker)
    if not user:
        return False, "WORKER_INACTIVE"
    if user.status != UserStatus.active:
        return False, "WORKER_INACTIVE"
    if worker.onboarding_status != WorkerOnboardingStatus.approved:
        return False, "WORKER_NOT_VERIFIED"

    qual = await _get_qualification_row(db, worker.id, service)

    # Tier check (unless explicit override row exists)
    min_tier = service.min_tier
    tier_ok = _tier_value(worker.tier) >= _tier_value(min_tier)
    override = bool(getattr(service, "lower_tier_override_allowed", False))
    if not tier_ok and not (qual and override):
        return False, "TIER_TOO_LOW"

    # Training requirement
    required_codes = list(getattr(service, "required_training_module_codes", None) or [])
    if required_codes:
        passed = await _has_passed_required_trainings(db, worker, required_codes)
        if not passed:
            return False, "TRAINING_REQUIRED"

    # Certificate requirement
    cert_codes = list(getattr(service, "required_certificate_codes", None) or [])
    if cert_codes:
        ok = await _has_valid_certificates(db, worker, cert_codes)
        if not ok:
            return False, "CERTIFICATE_REQUIRED"

    # Patch 4B — Assessment requirement
    assessment_codes = list(getattr(service, "required_assessment_codes", None) or [])
    if assessment_codes:
        min_pass = getattr(service, "minimum_pass_score", None)
        passed = await _has_passed_required_assessments(db, worker, assessment_codes, min_pass)
        if not passed:
            return False, "ASSESSMENT_REQUIRED"

    # Gate 3 — practical sign-off requirement. Theory (the assessment check
    # above) must already have passed; a trainer must additionally have
    # observed and signed the practical checklist.
    if getattr(service, "gate", None) == QualificationGate.practical_verified:
        practical_ok = await _has_passed_practical_signoff(db, worker, service)
        if not practical_ok:
            return False, "PRACTICAL_SIGNOFF_REQUIRED"

    # Admin approval requirement
    requires_admin = bool(getattr(service, "requires_admin_skill_approval", False))
    if requires_admin:
        if not qual or not qual.admin_approved_at:
            return False, "ADMIN_APPROVAL_REQUIRED"

    # Qualification record must be APPROVED and not expired
    if not qual:
        return False, "QUALIFICATION_RECORD_MISSING"
    if qual.qualification_status != WorkerQualificationStatus.APPROVED:
        return False, f"QUALIFICATION_STATUS_{qual.qualification_status.value}"
    now = datetime.now(timezone.utc)
    if qual.valid_until and qual.valid_until < now:
        return False, "QUALIFICATION_EXPIRED"
    return True, None


async def is_worker_opted_in_for_service(
    worker: WorkerProfile, service: ServiceLike, db: AsyncSession
) -> bool:
    pref = await _get_preference_row(db, worker.id, service)
    if not pref:
        return False
    return (
        pref.preference_status == WorkerPreferenceStatus.OPTED_IN
        and bool(pref.willing_to_accept)
    )


async def can_worker_receive_service(
    worker: WorkerProfile, service: ServiceLike, db: AsyncSession
) -> tuple[bool, Optional[str]]:
    """Returns (allowed, reason). reason is set when allowed=False."""
    qualified, reason = await is_worker_qualified_for_service(worker, service, db)
    if not qualified:
        return False, reason or "NOT_QUALIFIED"
    if not await is_worker_opted_in_for_service(worker, service, db):
        return False, "NOT_OPTED_IN"
    return True, None


# ---------------------------------------------------------------------------
# Training → qualification bridge
# ---------------------------------------------------------------------------
async def evaluate_and_upsert_qualifications_for_module(
    db: AsyncSession,
    worker: WorkerProfile,
    module: TrainingModule,
    completion: TrainingCompletion,
) -> list[WorkerServiceQualification]:
    """When a worker passes a training assessment, sync qualifications for any
    service/package whose `required_training_module_codes` contains this module
    code AND for which all other requirements pass.

    Does NOT auto opt-in.
    """
    if not module.code or not completion.assessment_passed:
        return []

    updated: list[WorkerServiceQualification] = []

    # Find services that require this module
    sres = await db.execute(
        select(ServiceCatalogue).where(
            ServiceCatalogue.required_training_module_codes.any(module.code),
            ServiceCatalogue.is_active.is_(True),
        )
    )
    services = list(sres.scalars().all())

    pres = await db.execute(
        select(CarePackage).where(
            CarePackage.required_training_module_codes.any(module.code),
            CarePackage.is_active.is_(True),
        )
    )
    packages = list(pres.scalars().all())

    for target in services + packages:
        qual = await _get_qualification_row(db, worker.id, target)
        # Re-check all requirements except the qualification row itself.
        all_codes = list(getattr(target, "required_training_module_codes", None) or [])
        cert_codes = list(getattr(target, "required_certificate_codes", None) or [])
        assessment_codes = list(getattr(target, "required_assessment_codes", None) or [])
        min_pass = getattr(target, "minimum_pass_score", None)
        requires_admin = bool(getattr(target, "requires_admin_skill_approval", False))

        training_ok = await _has_passed_required_trainings(db, worker, all_codes)
        cert_ok = await _has_valid_certificates(db, worker, cert_codes)
        assess_ok = await _has_passed_required_assessments(db, worker, assessment_codes, min_pass)
        practical_ok = (
            await _has_passed_practical_signoff(db, worker, target)
            if getattr(target, "gate", None) == QualificationGate.practical_verified
            else True
        )
        tier_ok = _tier_value(worker.tier) >= _tier_value(target.min_tier)

        if not qual:
            qual = WorkerServiceQualification(
                worker_id=worker.id,
                service_id=target.id if _is_service(target) else None,
                package_id=None if _is_service(target) else target.id,
                qualification_source=WorkerQualificationSource.TRAINING,
            )
            db.add(qual)

        qual.training_module_id = module.id
        qual.training_completion_id = completion.id
        qual.assessment_score = completion.assessment_score
        qual.qualification_source = WorkerQualificationSource.TRAINING

        if not (training_ok and cert_ok and assess_ok and practical_ok and tier_ok):
            qual.qualification_status = WorkerQualificationStatus.TRAINING_REQUIRED
        elif requires_admin and not qual.admin_approved_at:
            qual.qualification_status = WorkerQualificationStatus.QUALIFIED_PENDING_APPROVAL
        else:
            qual.qualification_status = WorkerQualificationStatus.APPROVED
            qual.valid_from = qual.valid_from or datetime.now(timezone.utc)

        await db.flush()
        updated.append(qual)

    return updated


# ---------------------------------------------------------------------------
# Tier assignment → qualification bridge
#
# BUGFIX: approving a worker / setting their tier (admin.py: approve_worker,
# set_worker_tier) used to only set `worker.tier` and mint the tier badge —
# it never touched WorkerServiceQualification. Any service/package with no
# training, certificate or assessment requirement (i.e. gated by tier alone)
# therefore stayed stuck at QUALIFICATION_RECORD_MISSING forever, because
# nothing else in the codebase ever creates that row for a tier-only service.
# This mirrors evaluate_and_upsert_qualifications_for_module/_assessment but
# is triggered from tier assignment / approval instead of training.
# ---------------------------------------------------------------------------
async def sync_tier_qualifications(
    db: AsyncSession, worker: WorkerProfile
) -> list[WorkerServiceQualification]:
    """Re-evaluate every active service/package against the worker's current
    tier and upsert a WorkerServiceQualification row wherever the worker now
    meets (or no longer meets) the requirements that don't depend on training
    or assessment completion.

    - Tier not met -> leaves/creates the row as NOT_QUALIFIED (locked, tier).
    - Tier met + no training/cert/assessment required + no admin-approval
      required -> APPROVED, source=TIER.
    - Tier met + no training/cert/assessment required + admin-approval
      required -> QUALIFIED_PENDING_APPROVAL (unless already admin-approved).
    - Tier met but training/cert/assessment still outstanding -> leaves the
      status for the training/assessment bridge to manage; only touches the
      row here if it doesn't exist yet (so `can_opt_in` shows the right
      locked_reason instead of QUALIFICATION_RECORD_MISSING).
    """
    updated: list[WorkerServiceQualification] = []

    sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.is_active.is_(True)))
    services = list(sres.scalars().all())
    pres = await db.execute(select(CarePackage).where(CarePackage.is_active.is_(True)))
    packages = list(pres.scalars().all())

    for target in services + packages:
        tier_ok = _tier_value(worker.tier) >= _tier_value(target.min_tier)

        training_codes = list(getattr(target, "required_training_module_codes", None) or [])
        cert_codes = list(getattr(target, "required_certificate_codes", None) or [])
        assessment_codes = list(getattr(target, "required_assessment_codes", None) or [])
        requires_admin = bool(getattr(target, "requires_admin_skill_approval", False))
        min_pass = getattr(target, "minimum_pass_score", None)

        training_ok = await _has_passed_required_trainings(db, worker, training_codes)
        cert_ok = await _has_valid_certificates(db, worker, cert_codes)
        assess_ok = await _has_passed_required_assessments(db, worker, assessment_codes, min_pass)
        practical_ok = (
            await _has_passed_practical_signoff(db, worker, target)
            if getattr(target, "gate", None) == QualificationGate.practical_verified
            else True
        )

        qual = await _get_qualification_row(db, worker.id, target)

        if not qual:
            qual = WorkerServiceQualification(
                worker_id=worker.id,
                service_id=target.id if _is_service(target) else None,
                package_id=None if _is_service(target) else target.id,
                qualification_source=WorkerQualificationSource.TIER,
            )
            db.add(qual)

        # Never downgrade a qualification that was already earned through
        # training/assessment/admin approval — this bridge only manages the
        # tier-only case.
        if qual.qualification_status == WorkerQualificationStatus.APPROVED:
            continue

        if not tier_ok:
            qual.qualification_status = WorkerQualificationStatus.NOT_QUALIFIED
        elif not (training_ok and cert_ok and assess_ok and practical_ok):
            # Tier is fine but something else is still outstanding — leave
            # status as-is (defaults to NOT_QUALIFIED for a brand new row) so
            # the eligibility endpoint reports the *real* locked_reason
            # (TRAINING_REQUIRED / CERTIFICATE_REQUIRED / ASSESSMENT_REQUIRED)
            # rather than QUALIFICATION_RECORD_MISSING.
            if qual.qualification_status is None:
                qual.qualification_status = WorkerQualificationStatus.NOT_QUALIFIED
        elif requires_admin and not qual.admin_approved_at:
            qual.qualification_status = WorkerQualificationStatus.QUALIFIED_PENDING_APPROVAL
            qual.qualification_source = WorkerQualificationSource.TIER
        else:
            qual.qualification_status = WorkerQualificationStatus.APPROVED
            qual.qualification_source = WorkerQualificationSource.TIER
            qual.valid_from = qual.valid_from or datetime.now(timezone.utc)

        await db.flush()
        updated.append(qual)

    return updated


# Backwards-compat shim removed


# ---------------------------------------------------------------------------
# Patch 4B — Assessment → qualification bridge
# ---------------------------------------------------------------------------
async def evaluate_and_upsert_qualifications_for_assessment(
    db: AsyncSession,
    worker: WorkerProfile,
    assessment: AssessmentModule,
    attempt: WorkerAssessmentAttempt,
) -> list[str]:
    """When a worker passes a standalone assessment, sync qualifications for
    any service/package whose ``required_assessment_codes`` contains this
    assessment code AND for which all OTHER requirements pass.

    Returns the list of service_code / package_code values that transitioned
    to APPROVED on this call (used by the UI to surface unlocked services).

    Does NOT auto opt-in — worker still has to opt-in separately.
    """
    if not assessment.code or not attempt.passed:
        return []

    unlocked: list[str] = []

    sres = await db.execute(
        select(ServiceCatalogue).where(
            ServiceCatalogue.required_assessment_codes.any(assessment.code),
            ServiceCatalogue.is_active.is_(True),
        )
    )
    services = list(sres.scalars().all())

    pres = await db.execute(
        select(CarePackage).where(
            CarePackage.required_assessment_codes.any(assessment.code),
            CarePackage.is_active.is_(True),
        )
    )
    packages = list(pres.scalars().all())

    for target in services + packages:
        qual = await _get_qualification_row(db, worker.id, target)
        training_codes = list(getattr(target, "required_training_module_codes", None) or [])
        cert_codes = list(getattr(target, "required_certificate_codes", None) or [])
        assessment_codes = list(getattr(target, "required_assessment_codes", None) or [])
        min_pass = getattr(target, "minimum_pass_score", None)
        requires_admin = bool(getattr(target, "requires_admin_skill_approval", False))

        training_ok = await _has_passed_required_trainings(db, worker, training_codes)
        cert_ok = await _has_valid_certificates(db, worker, cert_codes)
        assess_ok = await _has_passed_required_assessments(db, worker, assessment_codes, min_pass)
        practical_ok = (
            await _has_passed_practical_signoff(db, worker, target)
            if getattr(target, "gate", None) == QualificationGate.practical_verified
            else True
        )
        tier_ok = _tier_value(worker.tier) >= _tier_value(target.min_tier)

        if not qual:
            qual = WorkerServiceQualification(
                worker_id=worker.id,
                service_id=target.id if _is_service(target) else None,
                package_id=None if _is_service(target) else target.id,
                qualification_source=WorkerQualificationSource.TRAINING,
            )
            db.add(qual)

        qual.assessment_score = attempt.score
        qual.qualification_source = WorkerQualificationSource.TRAINING
        prev_status = qual.qualification_status

        if not (training_ok and cert_ok and assess_ok and practical_ok and tier_ok):
            qual.qualification_status = WorkerQualificationStatus.TRAINING_REQUIRED
        elif requires_admin and not qual.admin_approved_at:
            qual.qualification_status = WorkerQualificationStatus.QUALIFIED_PENDING_APPROVAL
        else:
            qual.qualification_status = WorkerQualificationStatus.APPROVED
            qual.valid_from = qual.valid_from or datetime.now(timezone.utc)

        await db.flush()
        if (
            qual.qualification_status == WorkerQualificationStatus.APPROVED
            and prev_status != WorkerQualificationStatus.APPROVED
        ):
            unlocked.append(
                target.service_code if _is_service(target) else target.package_code
            )
    return unlocked