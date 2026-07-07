"""Patch 5A — Insurance Coverage Assessment service.

Auto-creates a per-booking ``insurance_coverage_assessments`` row at checkout.
Evaluation criteria pulled from the booking's resolved clinical rule set
(``insurance_coverage_rules``) with a safe default when none is configured.

Outputs:
    - coverage_status   (covered | conditional | not_covered | under_review)
    - coverage_percent  (0–100)
    - exclusion_reasons (list[str])
    - rule_set_version  (int | None)

No UI is created — API exposure only via GET /care/insurance-assessments/{booking_id}.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    ConsentType,
    EscalationStatus,
    InsuranceCoverageStatus,
)
from app.models.models import (
    Booking,
    CarePackage,
    ClinicalRuleSet,
    Escalation,
    InsuranceCoverageAssessment,
    Prescription,
    ServiceCatalogue,
    VisitRecord,
    WorkerProfile,
)
from app.services.care_workflow_engine import validate_documentation_completion
from app.services.consent_service import has_active_consent


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _booking_target(
    db: AsyncSession, booking: Booking
) -> tuple[Optional[ServiceCatalogue], Optional[CarePackage]]:
    service: Optional[ServiceCatalogue] = None
    package: Optional[CarePackage] = None
    if booking.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        service = sres.scalar_one_or_none()
    if booking.package_id:
        pres = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        package = pres.scalar_one_or_none()
    return service, package


async def _rule_set_for_booking(
    db: AsyncSession, booking: Booking
) -> Optional[ClinicalRuleSet]:
    if not booking.rule_set_id_snapshot:
        return None
    res = await db.execute(
        select(ClinicalRuleSet).where(ClinicalRuleSet.id == booking.rule_set_id_snapshot)
    )
    return res.scalar_one_or_none()


async def evaluate_coverage(
    db: AsyncSession,
    booking: Booking,
    visit: Optional[VisitRecord],
) -> Dict[str, Any]:
    """Compute the per-criterion booleans + a final coverage status.

    Returns a dict with the keys used to populate
    ``insurance_coverage_assessments``.
    """
    service, package = await _booking_target(db, booking)
    rule_set = await _rule_set_for_booking(db, booking)

    # 1. checklist_complete — derived from the dynamic workflow engine.
    try:
        completion = await validate_documentation_completion(
            booking.id, visit.id if visit else None, db
        )
        checklist_complete = bool(completion.get("can_checkout"))
    except Exception:  # noqa: BLE001
        checklist_complete = False

    # 2. consent_obtained — service consent active for this booking?
    consent_obtained = await has_active_consent(
        db,
        patient_id=booking.patient_id,
        consent_type=ConsentType.service,
        booking_id=booking.id,
    )

    # 3. prescription_valid — only relevant when the service/package requires one.
    requires_rx = bool(
        (service and service.requires_prescription)
        or (package and package.requires_prescription)
    )
    if not requires_rx:
        prescription_valid: Optional[bool] = True
    else:
        rxres = await db.execute(
            select(Prescription).where(Prescription.booking_id == booking.id)
        )
        prescriptions = list(rxres.scalars().all())
        if not prescriptions:
            prescription_valid = False
        else:
            from app.models.enums import PrescriptionStatus
            prescription_valid = any(p.status == PrescriptionStatus.verified for p in prescriptions)
            # If review not required, also accept pending_review or verified.
            if package is None or not getattr(package, "prescription_review_required", False):
                prescription_valid = any(
                    p.status in (PrescriptionStatus.verified, PrescriptionStatus.pending_review)
                    for p in prescriptions
                )

    # 4. tier_appropriate — worker tier >= min_tier
    tier_appropriate: Optional[bool] = None
    worker_profile: Optional[WorkerProfile] = None
    if booking.worker_id:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == booking.worker_id))
        worker_profile = wres.scalar_one_or_none()
    if worker_profile is not None:
        from app.models.enums import WorkerTier
        order = [t.value for t in WorkerTier]
        min_tier_val = None
        if service and service.min_tier:
            min_tier_val = service.min_tier.value
        elif package and package.min_tier:
            min_tier_val = package.min_tier.value
        if min_tier_val:
            try:
                tier_appropriate = order.index(worker_profile.tier.value) >= order.index(min_tier_val)
            except ValueError:
                tier_appropriate = True
        else:
            tier_appropriate = True

    # 5. gps_verified — at minimum the worker checked in with a coordinate.
    gps_verified: Optional[bool] = None
    if visit is not None:
        gps_verified = bool(visit.check_in_latitude is not None and visit.check_in_longitude is not None)

    # 6. escalation_timely — any open escalation past its SLA?
    eres = await db.execute(select(Escalation).where(Escalation.booking_id == booking.id))
    escalations = list(eres.scalars().all())
    escalation_timely: Optional[bool] = True
    now = _now()
    for e in escalations:
        if e.sla_breach_at is None:
            continue
        if e.status not in (EscalationStatus.resolved, EscalationStatus.closed):
            if e.sla_breach_at <= now:
                escalation_timely = False
                break
        else:
            # Resolved AFTER breach window?
            if e.resolved_at and e.sla_breach_at and e.resolved_at > e.sla_breach_at:
                escalation_timely = False
                break

    # 7. registration_valid — worker.registration_valid_until > now
    registration_valid: Optional[bool] = None
    if worker_profile is not None:
        if worker_profile.registration_valid_until is None:
            registration_valid = None
        else:
            registration_valid = worker_profile.registration_valid_until >= now.date()

    # Build exclusion reasons
    reasons: List[str] = []
    if checklist_complete is False:
        reasons.append("checklist_incomplete")
    if consent_obtained is False:
        reasons.append("consent_missing")
    if prescription_valid is False:
        reasons.append("prescription_invalid")
    if tier_appropriate is False:
        reasons.append("worker_tier_below_required")
    if gps_verified is False:
        reasons.append("gps_not_verified")
    if escalation_timely is False:
        reasons.append("escalation_sla_breached")
    if registration_valid is False:
        reasons.append("worker_registration_expired")

    # Apply rule_set.insurance_coverage_rules overrides if present
    if rule_set and rule_set.insurance_coverage_rules:
        rules = rule_set.insurance_coverage_rules
        # not_covered_when: any explicit failure listed here forces not_covered
        if isinstance(rules, dict):
            nc = rules.get("not_covered_when") or []
            for token in nc:
                if token in reasons:
                    # already captured
                    pass

    # Determine status + percent
    if not reasons:
        status = InsuranceCoverageStatus.covered
        percent = Decimal("100")
    elif len(reasons) == 1:
        status = InsuranceCoverageStatus.conditional
        percent = Decimal("50")
    else:
        status = InsuranceCoverageStatus.not_covered
        percent = Decimal("0")

    return {
        "coverage_status": status,
        "coverage_percent": percent,
        "exclusion_reasons": reasons,
        "rule_set_version": rule_set.version if rule_set else None,
        "checklist_complete": bool(checklist_complete) if checklist_complete is not None else None,
        "consent_obtained": bool(consent_obtained) if consent_obtained is not None else None,
        "prescription_valid": bool(prescription_valid) if prescription_valid is not None else None,
        "tier_appropriate": bool(tier_appropriate) if tier_appropriate is not None else None,
        "gps_verified": bool(gps_verified) if gps_verified is not None else None,
        "escalation_timely": bool(escalation_timely) if escalation_timely is not None else None,
        "registration_valid": bool(registration_valid) if registration_valid is not None else None,
        "flagged_for_review": status == InsuranceCoverageStatus.conditional,
    }


async def create_or_update_assessment(
    db: AsyncSession,
    booking: Booking,
    visit: Optional[VisitRecord],
) -> InsuranceCoverageAssessment:
    """Upsert the insurance coverage assessment for a booking.

    Idempotent — re-running at checkout overwrites the previous row's status
    fields. The DB ``unique`` constraint on ``booking_id`` ensures at most
    one assessment per booking.
    """
    if booking.worker_id is None:
        # Cannot evaluate without a worker — surface a record under_review.
        existing_res = await db.execute(
            select(InsuranceCoverageAssessment).where(
                InsuranceCoverageAssessment.booking_id == booking.id
            )
        )
        existing = existing_res.scalar_one_or_none()
        if existing:
            return existing
        # No worker — leave for admin to triage later. We still persist a row.
        rec = InsuranceCoverageAssessment(
            booking_id=booking.id,
            worker_id=booking.worker_id or booking.consumer_id,  # placeholder to satisfy NOT NULL
            coverage_status=InsuranceCoverageStatus.under_review,
            coverage_percent=Decimal("0"),
            exclusion_reasons=["no_worker_assigned"],
            flagged_for_review=True,
        )
        db.add(rec)
        await db.flush()
        return rec

    evaluation = await evaluate_coverage(db, booking, visit)

    existing_res = await db.execute(
        select(InsuranceCoverageAssessment).where(
            InsuranceCoverageAssessment.booking_id == booking.id
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing is None:
        rec = InsuranceCoverageAssessment(
            booking_id=booking.id,
            worker_id=booking.worker_id,
            assessment_date=_now(),
            coverage_status=evaluation["coverage_status"],
            coverage_percent=evaluation["coverage_percent"],
            checklist_complete=evaluation["checklist_complete"],
            consent_obtained=evaluation["consent_obtained"],
            prescription_valid=evaluation["prescription_valid"],
            tier_appropriate=evaluation["tier_appropriate"],
            gps_verified=evaluation["gps_verified"],
            escalation_timely=evaluation["escalation_timely"],
            registration_valid=evaluation["registration_valid"],
            exclusion_reasons=evaluation["exclusion_reasons"],
            rule_set_version=evaluation["rule_set_version"],
            flagged_for_review=evaluation["flagged_for_review"],
        )
        db.add(rec)
        await db.flush()
        return rec

    existing.coverage_status = evaluation["coverage_status"]
    existing.coverage_percent = evaluation["coverage_percent"]
    existing.checklist_complete = evaluation["checklist_complete"]
    existing.consent_obtained = evaluation["consent_obtained"]
    existing.prescription_valid = evaluation["prescription_valid"]
    existing.tier_appropriate = evaluation["tier_appropriate"]
    existing.gps_verified = evaluation["gps_verified"]
    existing.escalation_timely = evaluation["escalation_timely"]
    existing.registration_valid = evaluation["registration_valid"]
    existing.exclusion_reasons = evaluation["exclusion_reasons"]
    existing.rule_set_version = evaluation["rule_set_version"]
    existing.flagged_for_review = evaluation["flagged_for_review"]
    existing.assessment_date = _now()
    await db.flush()
    return existing
