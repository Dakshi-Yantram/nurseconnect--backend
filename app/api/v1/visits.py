"""Visit lifecycle: check-in, check-out, vitals, medications, checklist, rating, care notes."""
import random
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import (
    CurrentUser,
    get_consumer_profile,
    get_current_user,
    get_worker_profile,
    is_admin,
)
from app.core.redis_client import redis_client
from app.integrations.providers import msg91_client
from app.models.enums import (
    BookingStatus,
    ConsentType,
    EscalationLevel,
    EscalationStatus,
    UserRole,
    VisitStatus,
)
from app.models.models import (
    Booking,
    CareNote,
    ClinicalRuleSet,
    ConsentRecord,
    ConsumerProfile,
    Escalation,
    MedicationAdministration,
    VisitRecord,
    VitalSignReading,
    WorkerProfile,
)
from app.schemas.schemas import (
    CareNoteCreate,
    CareNoteOut,
    CheckInRequest,
    CheckOutRequest,
    ChecklistSubmit,
    EscalationOut,
    MedicationSubmit,
    RatingSubmit,
    VisitRecordOut,
    VitalSignsOut,
    VitalSignsSubmit,
)
from app.services.clinical_engine import (
    compute_sla_breach,
    evaluate_checklist_payload,
    evaluate_vitals,
    get_escalation_metadata,
)
from app.services.care_workflow_engine import (
    WorkflowError,
    render_family_summary,
    validate_documentation_completion,
)
from app.services.common_services import audit, notify_parties
from app.services.consent_service import (
    ConsentMissingError,
    has_active_consent,
    require_consent,
)
from app.services.insurance_service import create_or_update_assessment
from app.websockets.manager import booking_topic, manager

router = APIRouter(prefix="/visits", tags=["visits"])


async def _get_visit_for_worker(db: AsyncSession, booking_id: UUID, worker_id: UUID) -> tuple[Booking, VisitRecord]:
    bres = await db.execute(select(Booking).where(Booking.id == booking_id, Booking.worker_id == worker_id))
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found or not assigned to you")
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if not visit:
        visit = VisitRecord(booking_id=booking.id, worker_id=worker_id, patient_id=booking.patient_id)
        db.add(visit)
        await db.flush()
    return booking, visit


@router.post("/{booking_id}/checkin", response_model=VisitRecordOut)
async def checkin(
    booking_id: UUID,
    payload: CheckInRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    if visit.check_in_at:
        raise HTTPException(status_code=400, detail="Already checked in")
    # Patch 5A — service consent gate
    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.service,
            booking_id=booking.id,
            action="start the visit",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None
    visit.check_in_at = datetime.now(timezone.utc)
    visit.check_in_latitude = payload.latitude
    visit.check_in_longitude = payload.longitude
    visit.status = VisitStatus.in_progress
    booking.status = BookingStatus.in_progress
    await audit(db, profile.user_id, "worker", "visit.checkin", "visit", visit.id)
    await db.commit()
    await db.refresh(visit)
    await manager.broadcast(booking_topic(booking_id), {"type": "visit.checked_in", "booking_id": str(booking_id)})
    return VisitRecordOut.model_validate(visit)


# ============================================================================
# PATCH 4 — OTP-to-start-visit
#
# Two endpoints:
#   POST /{booking_id}/generate-start-otp  — consumer triggers, SMS sent
#   POST /{booking_id}/verify-start-otp    — nurse enters code, starts visit
#
# Redis keys:
#   visit_start_otp:{booking_id}            4-digit code, TTL 600s
#   visit_start_otp_attempts:{booking_id}   attempt counter, TTL 600s
#
# NOTE ON BOOKING STATUS — fixed from the original patch draft:
# The original patch checked for BookingStatus.active / BookingStatus.claimed,
# neither of which exists on this enum (see app/models/enums.py). The real
# states a booking passes through before/during a visit are:
#   assigned -> worker_en_route -> worker_arrived -> in_progress -> completed
# OTP generation should be allowed once a worker is assigned and en route to
# arrived (i.e. the nurse could plausibly be at the door), and naturally
# also while in_progress already (e.g. consumer hits the button twice).
# Adjust this tuple if your dispatch flow differs.
# ============================================================================

_OTP_TTL_SECONDS = 600          # 10 minutes
_OTP_MAX_ATTEMPTS = 5           # brute-force cap
_OTP_KEY_PREFIX = "visit_start_otp"
_OTP_ATTEMPTS_PREFIX = "visit_start_otp_attempts"

_OTP_ELIGIBLE_STATUSES = (
    BookingStatus.assigned,
    BookingStatus.worker_en_route,
    BookingStatus.worker_arrived,
    BookingStatus.in_progress,
)


def _otp_key(booking_id) -> str:
    return f"{_OTP_KEY_PREFIX}:{booking_id}"


def _attempts_key(booking_id) -> str:
    return f"{_OTP_ATTEMPTS_PREFIX}:{booking_id}"


class VisitStartOtpVerifyRequest(BaseModel):
    otp: str
    latitude: float
    longitude: float


@router.post("/{booking_id}/generate-start-otp")
async def generate_visit_start_otp(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the CONSUMER when the nurse has arrived at the door.
    Generates a 4-digit OTP, stores it in Redis for 10 minutes,
    and SMSes it to the consumer's registered phone.

    The consumer reads the code aloud to the nurse, who enters it in the
    nurse app to start the visit. The OTP is never returned in the API
    response to prevent interception.
    """
    bres = await db.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.consumer_id == profile.id,
        )
    )
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status not in _OTP_ELIGIBLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "BOOKING_NOT_READY",
                "message": "Visit cannot be started in the current booking state.",
            },
        )

    # Check if OTP already exists and is still valid — don't spam
    existing = await redis_client.get(_otp_key(booking_id))
    if existing:
        ttl = await redis_client.ttl(_otp_key(booking_id))
        return {
            "sent": True,
            "message": "OTP already active. Ask your nurse to enter it.",
            "expires_in_seconds": ttl,
        }

    otp_code = str(random.randint(1000, 9999))

    await redis_client.setex(_otp_key(booking_id), _OTP_TTL_SECONDS, otp_code)
    await redis_client.delete(_attempts_key(booking_id))

    from app.models.models import User
    ures = await db.execute(select(User).where(User.id == profile.user_id))
    user = ures.scalar_one_or_none()
    phone = user.phone_e164 if user else None

    sms_sent = False
    if phone:
        try:
            resp = await msg91_client.send_otp(phone, otp_code)
            sms_sent = resp.get("type") == "success"
        except Exception:
            # SMS failure must not block — the nurse can still manually share
            # the code from the consumer's screen
            sms_sent = False

    await audit(
        db, profile.user_id, "consumer",
        "visit.otp_generated", "booking", booking_id,
        {"sms_sent": sms_sent},
    )
    await db.commit()

    return {
        "sent": True,
        "sms_sent": sms_sent,
        "message": (
            "Visit code sent to your registered number."
            if sms_sent
            else "Visit code generated. Show it to your nurse from the app."
        ),
        "expires_in_seconds": _OTP_TTL_SECONDS,
        # DEV ONLY — never enable in production. Uncomment only for local testing.
        # "_dev_otp": otp_code,
    }


@router.post("/{booking_id}/verify-start-otp", response_model=VisitRecordOut)
async def verify_visit_start_otp(
    booking_id: UUID,
    payload: VisitStartOtpVerifyRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the NURSE after the consumer reads the OTP aloud.
    On success, checks the nurse in and starts the visit — identical outcome
    to /checkin but gated on OTP verification first.
    """
    bres = await db.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.worker_id == profile.id,
        )
    )
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found or not assigned to you")

    # ── Brute-force guard ───────────────────────────────────────────────────
    attempts_raw = await redis_client.get(_attempts_key(booking_id))
    attempts = int(attempts_raw) if attempts_raw else 0
    if attempts >= _OTP_MAX_ATTEMPTS:
        await redis_client.delete(_otp_key(booking_id))
        await redis_client.delete(_attempts_key(booking_id))
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OTP_MAX_ATTEMPTS_EXCEEDED",
                "message": (
                    "Too many incorrect attempts. "
                    "Ask the consumer to generate a new visit code."
                ),
            },
        )

    stored_otp = await redis_client.get(_otp_key(booking_id))
    if not stored_otp:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OTP_EXPIRED",
                "message": "Visit code has expired. Ask the consumer to generate a new one.",
            },
        )

    if payload.otp.strip() != stored_otp:
        pipe = redis_client.pipeline()
        pipe.incr(_attempts_key(booking_id))
        pipe.expire(_attempts_key(booking_id), _OTP_TTL_SECONDS)
        await pipe.execute()

        remaining = _OTP_MAX_ATTEMPTS - (attempts + 1)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OTP_INVALID",
                "message": f"Incorrect visit code. {remaining} attempt(s) remaining.",
                "attempts_remaining": remaining,
            },
        )

    # OTP verified — delete keys immediately
    await redis_client.delete(_otp_key(booking_id))
    await redis_client.delete(_attempts_key(booking_id))

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if visit and visit.check_in_at:
        raise HTTPException(status_code=400, detail="Already checked in")

    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.service,
            booking_id=booking.id,
            action="start the visit",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None

    if not visit:
        visit = VisitRecord(
            booking_id=booking.id,
            worker_id=profile.id,
            patient_id=booking.patient_id,
        )
        db.add(visit)
        await db.flush()

    visit.check_in_at = datetime.now(timezone.utc)
    visit.check_in_latitude = payload.latitude
    visit.check_in_longitude = payload.longitude
    visit.status = VisitStatus.in_progress
    booking.status = BookingStatus.in_progress

    await audit(
        db, profile.user_id, "worker",
        "visit.checkin_via_otp", "visit", visit.id,
        {"otp_verified": True},
    )
    await db.commit()
    await db.refresh(visit)

    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "visit.checked_in", "booking_id": str(booking_id), "method": "otp"},
    )

    return VisitRecordOut.model_validate(visit)


# ============================================================================
# END PATCH 4
# ============================================================================


@router.post("/{booking_id}/checkout", response_model=VisitRecordOut)
async def checkout(
    booking_id: UUID,
    payload: CheckOutRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    if not visit.check_in_at:
        raise HTTPException(status_code=400, detail="Cannot checkout without check-in")
    if visit.check_out_at:
        raise HTTPException(status_code=400, detail="Already checked out")

    # Patch 4 — dynamic, template-driven completion validation. Replaces the
    # previous hardcoded "checklist + vitals + family_summary + care_notes"
    # gate. All requirements are now derived from the booking's resolved
    # checklist + documentation templates (package > service > fallback).
    try:
        status = await validate_documentation_completion(booking_id, visit.id, db)
    except WorkflowError as we:
        # High-risk clinical service without template → 422 with stable code.
        from starlette.responses import JSONResponse
        return JSONResponse(
            status_code=we.http_status,
            content={
                "success": False,
                "code": we.code,
                "message": we.message,
            },
        )
    if not status["can_checkout"]:
        from starlette.responses import JSONResponse
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "code": "MANDATORY_DOCUMENTATION_INCOMPLETE",
                "message": "Mandatory documentation is incomplete.",
                "missing_items": status["blocking_items"] or status["missing_items"],
            },
        )

    # Render family summary from the resolved template when the worker did not
    # provide an override. Safe-default fallback handled inside the engine.
    family_summary = (payload.family_summary or "").strip()
    if not family_summary:
        family_summary = await render_family_summary(booking_id, visit.id, db)

    visit.check_out_at = datetime.now(timezone.utc)
    visit.check_out_latitude = payload.latitude
    visit.check_out_longitude = payload.longitude
    visit.actual_duration_minutes = int((visit.check_out_at - visit.check_in_at).total_seconds() / 60)
    visit.family_summary = family_summary
    visit.care_notes = payload.care_notes or visit.care_notes
    visit.status = VisitStatus.completed
    visit.documentation_complete = True
    booking.status = BookingStatus.completed

    # increment worker stats
    profile.completed_visits_count += 1
    # Patch 5A — auto-create / refresh the insurance coverage assessment
    # for this booking at checkout. Persisted regardless of outcome — admin
    # finance can audit it later via /care/insurance-assessments/{booking_id}.
    try:
        assessment = await create_or_update_assessment(db, booking, visit)
        coverage_summary = {
            "coverage_status": assessment.coverage_status.value,
            "coverage_percent": float(assessment.coverage_percent),
            "exclusion_reasons": list(assessment.exclusion_reasons or []),
            "rule_set_version": assessment.rule_set_version,
        }
    except Exception as exc:  # noqa: BLE001
        # Never block checkout on a coverage-evaluation glitch; surface to logs.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "insurance assessment failed for booking %s: %s", booking.id, exc
        )
        coverage_summary = None

    await audit(db, profile.user_id, "worker", "visit.checkout", "visit", visit.id, {"duration_min": visit.actual_duration_minutes, "coverage": coverage_summary})
    await db.commit()
    await db.refresh(visit)
    await manager.broadcast(booking_topic(booking_id), {"type": "visit.completed", "booking_id": str(booking_id), "coverage": coverage_summary})
    return VisitRecordOut.model_validate(visit)


@router.post("/{booking_id}/vitals", response_model=VitalSignsOut)
async def submit_vitals(
    booking_id: UUID,
    payload: VitalSignsSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    # Evaluate against rule set
    rule_set = None
    if booking.rule_set_id_snapshot:
        rres = await db.execute(select(ClinicalRuleSet).where(ClinicalRuleSet.id == booking.rule_set_id_snapshot))
        rule_set = rres.scalar_one_or_none()

    flags: List[str] = []
    level = "none"
    if rule_set:
        flags, level = evaluate_vitals(rule_set, payload.model_dump())

    reading = VitalSignReading(
        visit_record_id=visit.id,
        patient_id=booking.patient_id,
        booking_id=booking.id,
        recorded_by=profile.id,
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        pulse=payload.pulse,
        spo2=payload.spo2,
        temperature_f=payload.temperature_f,
        respiratory_rate=payload.respiratory_rate,
        blood_sugar_fasting=payload.blood_sugar_fasting,
        blood_sugar_random=payload.blood_sugar_random,
        weight_kg=payload.weight_kg,
        pain_score=payload.pain_score,
        gcs_score=payload.gcs_score,
        abnormal_flags=flags,
        escalation_triggered=level != "none",
        escalation_level=EscalationLevel(level),
        rule_set_version=rule_set.version if rule_set else None,
        measurement_device=payload.measurement_device,
        is_offline_submitted=payload.is_offline_submitted,
        recorded_at=payload.recorded_at or datetime.now(timezone.utc),
        synced_at=None if payload.is_offline_submitted else datetime.now(timezone.utc),
    )
    db.add(reading)

    # Auto-create escalation if level != none
    if level != "none" and rule_set:
        meta = get_escalation_metadata(rule_set, level)
        esc = Escalation(
            booking_id=booking.id,
            visit_record_id=visit.id,
            worker_id=profile.id,
            patient_id=booking.patient_id,
            level=EscalationLevel(level),
            status=EscalationStatus.open,
            trigger_type="vital_threshold",
            trigger_details={"flags": flags, "vitals": payload.model_dump(mode="json")},
            notes=f"Auto-escalation from vitals: {', '.join(flags)}",
            notified_parties=meta.get("notify"),
            sla_minutes=meta.get("sla_minutes"),
            sla_breach_at=compute_sla_breach(meta.get("sla_minutes")),
            auto_call_112=bool(meta.get("auto_call_112")),
            rule_set_id=rule_set.id,
            rule_set_version=rule_set.version,
        )
        db.add(esc)
        visit.escalation_triggered = True
        await db.flush()
        await notify_parties(
            db,
            meta.get("notify", []),
            {"booking_id": str(booking.id), "escalation_id": str(esc.id)},
            template_code="vital_escalation",
            title=f"Vital sign alert: {level}",
            body=f"Abnormal: {', '.join(flags)}",
        )
        await manager.broadcast(booking_topic(booking.id), {"type": "escalation.created", "level": level, "flags": flags})

    await audit(db, profile.user_id, "worker", "visit.vitals", "vital_sign_reading", reading.id, {"level": level})
    await db.commit()
    await db.refresh(reading)
    return VitalSignsOut.model_validate(reading)


@router.get("/{booking_id}/vitals", response_model=List[VitalSignsOut])
async def list_vitals(booking_id: UUID, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    # Patch 5B — enforce booking ownership before exposing clinical readings.
    from app.security.access_control import assert_user_can_access_booking
    from app.services.security_audit_service import log_access_denied
    try:
        await assert_user_can_access_booking(db, current, booking_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            await log_access_denied(
                db,
                user_id=current.id,
                role=current.role.value,
                endpoint="GET /visits/{id}/vitals",
                reason="visit_booking_ownership",
                entity_type="booking",
                entity_id=booking_id,
            )
            await db.commit()
        raise
    res = await db.execute(select(VitalSignReading).where(VitalSignReading.booking_id == booking_id).order_by(VitalSignReading.recorded_at.desc()))
    return [VitalSignsOut.model_validate(v) for v in res.scalars().all()]


@router.post("/{booking_id}/medications")
async def submit_medication(
    booking_id: UUID,
    payload: MedicationSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    # Patch 5A — medication consent gate
    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.medication,
            booking_id=booking.id,
            action="administer medication",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None

    # Patch 5A — prescription required when service/package mandates it
    from app.models.models import CarePackage, Prescription, ServiceCatalogue
    requires_rx = False
    service = None
    package = None
    if booking.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        service = sres.scalar_one_or_none()
        if service and service.requires_prescription:
            requires_rx = True
    if booking.package_id:
        pres = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        package = pres.scalar_one_or_none()
        if package and package.requires_prescription:
            requires_rx = True

    if requires_rx and not payload.prescription_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PRESCRIPTION_REQUIRED",
                "message": "A verified prescription is required for this medication.",
            },
        )
    if payload.prescription_id:
        rxres = await db.execute(select(Prescription).where(Prescription.id == payload.prescription_id))
        rx = rxres.scalar_one_or_none()
        if not rx or rx.patient_id != booking.patient_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "PRESCRIPTION_NOT_FOUND", "message": "Prescription not found for this patient."},
            )

    # Patch 5A — patient identity confirmation
    if not payload.patient_identified:
        raise HTTPException(
            status_code=422,
            detail={"code": "PATIENT_IDENTITY_NOT_CONFIRMED", "message": "Confirm patient identity before administration."},
        )

    # Enforce allergy check per rule set
    rule_set = None
    if booking.rule_set_id_snapshot:
        rres = await db.execute(select(ClinicalRuleSet).where(ClinicalRuleSet.id == booking.rule_set_id_snapshot))
        rule_set = rres.scalar_one_or_none()
    if rule_set and rule_set.allergy_check_required and not payload.allergy_check_done:
        raise HTTPException(
            status_code=422,
            detail={"code": "ALLERGY_CHECK_REQUIRED", "message": "Allergy check required by current clinical rule set."},
        )
    if rule_set and rule_set.allergy_check_required and not payload.allergy_confirmed_clear:
        if rule_set.drug_allergy_escalation.value == "block":
            raise HTTPException(
                status_code=422,
                detail={"code": "ALLERGY_NOT_CLEARED", "message": "Allergy not cleared — administration blocked by rule set."},
            )

    med = MedicationAdministration(
        visit_record_id=visit.id,
        patient_id=booking.patient_id,
        booking_id=booking.id,
        administered_by=profile.id,
        drug_name=payload.drug_name,
        drug_generic_name=payload.drug_generic_name,
        drug_class=payload.drug_class,
        dose_amount=payload.dose_amount,
        dose_unit=payload.dose_unit,
        route=payload.route,
        site=payload.site,
        prescription_id=payload.prescription_id,
        allergy_check_done=payload.allergy_check_done,
        allergy_confirmed_clear=payload.allergy_confirmed_clear,
        patient_identified=payload.patient_identified,
        expiry_checked=payload.expiry_checked,
        administered_at=payload.administered_at,
        patient_response=payload.patient_response,
        adverse_reaction=payload.adverse_reaction,
        adverse_reaction_notes=payload.adverse_reaction_notes,
        batch_number=payload.batch_number,
        manufacturer=payload.manufacturer,
        is_offline_submitted=payload.is_offline_submitted,
        synced_at=None if payload.is_offline_submitted else datetime.now(timezone.utc),
    )
    if payload.adverse_reaction:
        med.escalation_triggered = True
    db.add(med)
    await audit(db, profile.user_id, "worker", "visit.medication", "medication_administration", med.id, {"drug": payload.drug_name})
    await db.commit()
    await db.refresh(med)
    return {"id": str(med.id), "escalation_triggered": med.escalation_triggered}


@router.post("/{booking_id}/checklist", response_model=VisitRecordOut)
async def submit_checklist(
    booking_id: UUID,
    payload: ChecklistSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    # Patch 5A — service consent gate
    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.service,
            booking_id=booking.id,
            action="submit clinical checklist",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None
    visit.checklist_responses = payload.responses
    await audit(db, profile.user_id, "worker", "visit.checklist", "visit", visit.id)
    await db.commit()
    await db.refresh(visit)
    return VisitRecordOut.model_validate(visit)


@router.get("/{booking_id}/insurance-assessment")
async def get_insurance_assessment(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Patch 5A — fetch the per-booking insurance coverage assessment.

    Visible to:
      * consumer who owns the booking
      * assigned worker
      * admin
    """
    from app.models.models import InsuranceCoverageAssessment
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    # Ownership
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one_or_none()
        if not cp or booking.consumer_id != cp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one_or_none()
        if not wp or booking.worker_id != wp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Forbidden")

    ares = await db.execute(
        select(InsuranceCoverageAssessment).where(InsuranceCoverageAssessment.booking_id == booking_id)
    )
    a = ares.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="No insurance assessment yet for this booking")
    return {
        "id": str(a.id),
        "booking_id": str(a.booking_id),
        "worker_id": str(a.worker_id),
        "assessment_date": a.assessment_date.isoformat() if a.assessment_date else None,
        "coverage_status": a.coverage_status.value,
        "coverage_percent": float(a.coverage_percent),
        "checklist_complete": a.checklist_complete,
        "consent_obtained": a.consent_obtained,
        "prescription_valid": a.prescription_valid,
        "tier_appropriate": a.tier_appropriate,
        "gps_verified": a.gps_verified,
        "escalation_timely": a.escalation_timely,
        "registration_valid": a.registration_valid,
        "exclusion_reasons": list(a.exclusion_reasons or []),
        "rule_set_version": a.rule_set_version,
        "flagged_for_review": a.flagged_for_review,
    }


@router.post("/{booking_id}/rating", response_model=VisitRecordOut)
async def rate_visit(
    booking_id: UUID,
    payload: RatingSubmit,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    bres = await db.execute(select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id))
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Visit record not found")
    visit.rating_by_consumer = payload.rating
    visit.rating_comment = payload.comment
    visit.rated_at = datetime.now(timezone.utc)
    # Update worker rating average
    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == booking.worker_id))
    wp = wres.scalar_one_or_none()
    if wp:
        new_count = wp.rating_count + 1
        wp.rating_average = ((wp.rating_average * wp.rating_count) + payload.rating) / new_count
        wp.rating_count = new_count
    await db.commit()
    await db.refresh(visit)
    return VisitRecordOut.model_validate(visit)


@router.get("/{booking_id}", response_model=VisitRecordOut)
async def get_visit(booking_id: UUID, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    # Patch 5B — enforce booking ownership before exposing the visit record.
    from app.security.access_control import assert_user_can_access_booking
    from app.services.security_audit_service import log_access_denied
    try:
        await assert_user_can_access_booking(db, current, booking_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            await log_access_denied(
                db,
                user_id=current.id,
                role=current.role.value,
                endpoint="GET /visits/{id}",
                reason="visit_booking_ownership",
                entity_type="booking",
                entity_id=booking_id,
            )
            await db.commit()
        raise
    res = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = res.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found")
    return VisitRecordOut.model_validate(visit)


# ----- CARE NOTES -----
notes_router = APIRouter(prefix="/care-notes", tags=["care-notes"])


@notes_router.post("/", response_model=CareNoteOut)
async def add_care_note(payload: CareNoteCreate, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    n = CareNote(
        patient_id=payload.patient_id,
        booking_id=payload.booking_id,
        author_id=current.id,
        author_role=current.role.value,
        title=payload.title,
        content=payload.content,
        note_type=payload.note_type,
        visible_to_family=payload.visible_to_family,
        visible_to_worker=payload.visible_to_worker,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return CareNoteOut.model_validate(n)


@notes_router.get("/patient/{patient_id}", response_model=List[CareNoteOut])
async def list_care_notes(patient_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CareNote).where(CareNote.patient_id == patient_id).order_by(CareNote.created_at.desc()))
    items = res.scalars().all()
    # Filter visibility
    out = []
    for n in items:
        if current.role == UserRole.consumer and not n.visible_to_family:
            continue
        if current.role == UserRole.worker and not n.visible_to_worker:
            continue
        out.append(n)
    return [CareNoteOut.model_validate(n) for n in out]