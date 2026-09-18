"""Visit lifecycle: check-in, check-out, vitals, medications, checklist, rating, care notes."""
import html
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
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
from app.core.config import settings
from app.core.redis_client import redis_client
from app.integrations.providers import msg91_client
from app.models.enums import (
    BookingStatus,
    ConsentType,
    EscalationLevel,
    EscalationStatus,
    NotificationChannel,
    UserRole,
    VisitStatus,
)
from app.models.models import (
    Booking,
    User,
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
logger = logging.getLogger(__name__)


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


async def _ensure_visit_start_otp(db: AsyncSession, booking: Booking) -> dict:
    """Idempotently ensure a visit-start OTP exists for this booking —
    returns the existing one if still active, otherwise generates a new
    4-digit code, stores it in Redis for 10 minutes, and best-effort SMSes
    the consumer.

    The code is scoped to a specific accepted worker implicitly: verify
    only succeeds when called by the worker on `booking.worker_id`, so
    even though the OTP itself is a bare 4-digit code, it's useless to any
    nurse other than the one who accepted this booking.

    Real SMS delivery isn't reliably configured in most environments this
    app runs in, so the code is also returned in the response body
    whenever OTP_DEV_MODE is on, or whenever the SMS send itself failed —
    matching the on-screen fallback this endpoint already promised in its
    own message text ("Show it to your nurse from the app").
    """
    existing = await redis_client.get(_otp_key(booking.id))
    if existing:
        ttl = await redis_client.ttl(_otp_key(booking.id))
        otp_code = existing.decode() if isinstance(existing, bytes) else existing
        return {
            "sent": True,
            "sms_sent": None,
            "message": "Show this code to your nurse when they arrive.",
            "expires_in_seconds": ttl,
            "otp": otp_code,
        }

    otp_code = f"{secrets.randbelow(9000) + 1000}"
    await redis_client.setex(_otp_key(booking.id), _OTP_TTL_SECONDS, otp_code)
    await redis_client.delete(_attempts_key(booking.id))

    from app.models.models import ConsumerProfile as _ConsumerProfile, User
    cres = await db.execute(select(_ConsumerProfile).where(_ConsumerProfile.id == booking.consumer_id))
    consumer_profile = cres.scalar_one_or_none()
    phone = None
    consumer_user_id = consumer_profile.user_id if consumer_profile else None
    if consumer_user_id:
        ures = await db.execute(select(User).where(User.id == consumer_user_id))
        user = ures.scalar_one_or_none()
        phone = user.phone_e164 if user else None

    sms_sent = False
    if phone:
        try:
            resp = await msg91_client.send_otp(
                phone,
                otp_code,
                template_id=getattr(settings, "MSG91_VISIT_OTP_TEMPLATE_ID", None) or None,
                purpose="visit_start",
            )
            sms_sent = resp.get("type") == "success"
        except Exception:
            # SMS failure must not block — the code is still shown in-app below.
            sms_sent = False

    if consumer_user_id:
        await audit(
            db, consumer_user_id, "consumer",
            "visit.otp_generated", "booking", booking.id,
            {"sms_sent": sms_sent},
        )
        await db.commit()

    return {
        "sent": True,
        "sms_sent": sms_sent,
        "message": (
            "Visit code sent to your registered number — also shown below."
            if sms_sent
            else "Visit code generated. Show it to your nurse from the app."
        ),
        "expires_in_seconds": _OTP_TTL_SECONDS,
        # Always shown on the consumer's booking card, regardless of SMS
        # delivery — SMS is a best-effort convenience, not the source of
        # truth for the code the consumer hands to their nurse.
        "otp": otp_code,
    }


@router.post("/{booking_id}/generate-start-otp")
async def generate_visit_start_otp(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the CONSUMER (or auto-triggered right when a nurse accepts —
    see bookings.py accept_booking) to ensure a visit-start OTP is ready.
    The consumer reads the code aloud to the nurse, who enters it in the
    nurse app to start the visit.
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

    return await _ensure_visit_start_otp(db, booking)


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

    # OTP matches — but don't consume it yet. If a downstream check (consent,
    # already-checked-in) fails, the nurse/consumer shouldn't have to
    # generate a brand new code for something unrelated to the code itself.
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

    # All checks passed — the code is now spent, whether or not the rest of
    # the check-in succeeds (matches the original all-or-nothing behavior
    # for genuine check-in failures past this point).
    await redis_client.delete(_otp_key(booking_id))
    await redis_client.delete(_attempts_key(booking_id))

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

    # Stage whatever the nurse submitted with this request onto the visit
    # record BEFORE validating. The baseline report gate in
    # validate_documentation_completion reads the persisted VisitRecord, so
    # without this a report supplied in the checkout payload itself would be
    # invisible to the gate and a legitimate checkout would be rejected.
    # Nothing is committed until the gate passes.
    if payload.care_notes and payload.care_notes.strip():
        visit.care_notes = payload.care_notes.strip()
    if getattr(payload, "family_summary", None) and payload.family_summary.strip():
        visit.family_summary = payload.family_summary.strip()
    await db.flush()

    # Patch 4 — dynamic, template-driven completion validation. Replaces the
    # previous hardcoded "checklist + vitals + family_summary + care_notes"
    # gate. All requirements are now derived from the booking's resolved
    # checklist + documentation templates (package > service > fallback),
    # plus a baseline report floor when no template governs the booking.
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
        await db.rollback()
        blocking = status["blocking_items"] or status["missing_items"]
        report_only = bool(blocking) and all(i.get("type") == "report" for i in blocking)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                # Distinct code so the app can route the nurse straight to the
                # visit report form rather than to a generic documentation
                # screen that may not exist for this booking.
                "code": "VISIT_REPORT_REQUIRED" if report_only
                else "MANDATORY_DOCUMENTATION_INCOMPLETE",
                "message": "Fill in and submit your visit report before completing this visit."
                if report_only
                else "Mandatory documentation is incomplete.",
                "missing_items": blocking,
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

    # Bug fix: the family/consumer was never actually notified that the
    # visit report was ready — checkout only broadcast over the live
    # websocket (booking_topic), which only reaches a client that happens to
    # be connected at that exact moment, and persisted nothing to the
    # notification center. Send a real notification (in-app + push, so it
    # survives even if the family isn't looking at the app right now) with
    # the family summary itself, not just a "something happened" ping.
    try:
        await notify_parties(
            db,
            ["family"],
            {"booking_id": str(booking_id), "visit_id": str(visit.id)},
            "visit.completed.report_ready",
            "Visit report is ready",
            family_summary,
        )
    except Exception as exc:  # noqa: BLE001
        # Never block checkout on a notification-delivery glitch — the
        # report itself is already saved on the visit record and viewable
        # in-app either way. Just don't let it silently vanish from logs.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "family notification failed for booking %s: %s", booking.id, exc
        )

    # WhatsApp feedback request — fired the moment the visit is checked out,
    # separate from the in-app/push "report ready" notification above so it
    # reaches the family even if they never open the app. Uses the Interakt
    # WhatsApp provider (see app/integrations/providers.py). Delivery/read
    # status for this message comes back asynchronously on
    # POST /api/webhooks/whatsapp/interakt and updates the NotificationLog
    # row by provider_message_id.
    try:
        feedback_link = f"{settings.FEEDBACK_LINK_BASE_URL}/{booking_id}"
        await notify_parties(
            db,
            ["family"],
            {"booking_id": str(booking_id), "visit_id": str(visit.id), "feedback_link": feedback_link},
            settings.INTERAKT_FEEDBACK_TEMPLATE,
            "How was the visit?",
            f"The visit is complete. We'd love to hear how it went — please share your feedback: {feedback_link}",
            channels=[NotificationChannel.whatsapp],
        )
    except Exception as exc:  # noqa: BLE001
        # Never block checkout on a WhatsApp delivery glitch — the visit is
        # already saved and the family can still be reached via the in-app
        # notification sent above.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "whatsapp feedback request failed for booking %s: %s", booking.id, exc
        )

    # Generate the nurse's payout for this completed visit. Idempotent, so a
    # retried/replayed checkout never pays twice. A payout glitch must never
    # block the nurse from completing the visit, so it's best-effort and logged.
    try:
        from app.services.payout_service import create_payout_for_booking
        await create_payout_for_booking(db, booking)

        # First-ever completed booking -> Stage 2 (e-stamp Master Agreement)
        # just unlocked. Nudge the nurse immediately rather than waiting for
        # her to happen to open the app and notice.
        if profile.completed_visits_count == 1:
            await notify_parties(
                db,
                ["worker"],
                {"booking_id": str(booking_id)},
                "contract.stage2.unlocked",
                "Complete your Partner Agreement",
                "Congrats on your first booking! Please e-sign your Master Agreement to unlock future bookings.",
            )
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "payout generation failed for booking %s: %s", booking.id, exc
        )

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
    out = VisitRecordOut.model_validate(visit)
    if current.role == UserRole.consumer:
        # `care_notes` is the nurse's internal working record. The dedicated
        # family endpoint (/report/consumer) already excluded it, but this
        # generic endpoint returned it to the family verbatim — and the web
        # family page rendered it under "Nurse's notes".
        out = out.model_copy(update={"care_notes": None})
    return out


# ----- CARE NOTES -----
notes_router = APIRouter(prefix="/care-notes", tags=["care-notes"])


@notes_router.post("/", response_model=CareNoteOut)
async def add_care_note(payload: CareNoteCreate, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Previously any authenticated user could attach a note to ANY patient.
    from app.security.access_control import assert_user_can_access_patient
    await assert_user_can_access_patient(db, current, payload.patient_id)
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
    # Previously there was NO ownership check: any logged-in consumer/worker
    # could read any patient's notes just by knowing (or guessing) its id.
    from app.security.access_control import assert_user_can_access_patient
    await assert_user_can_access_patient(db, current, patient_id)
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

# ===========================================================================
# Visit report  (nurse app items 2 & 10, patient/family app item 14)
# ===========================================================================
# Before this, a visit report had no dedicated surface at all: the nurse
# could only pass `care_notes` / `family_summary` inside the checkout call,
# there was no way to draft or revise a report, and no way for the family to
# read one back. These three endpoints give the report its own lifecycle --
# fetch what's outstanding, save it (repeatedly, before or after checkout),
# and let the patient side read the finished version.


class VisitReportUpdate(BaseModel):
    """A nurse's visit report. Both fields are optional per-request so the
    form can autosave a partial draft; completeness is judged by the
    checkout gate, not here."""
    care_notes: Optional[str] = None
    family_summary: Optional[str] = None


def _report_payload(visit: VisitRecord, status: dict | None = None) -> dict:
    out = {
        "booking_id": str(visit.booking_id),
        "visit_id": str(visit.id),
        "care_notes": visit.care_notes,
        "family_summary": visit.family_summary,
        "documentation_complete": visit.documentation_complete,
        "check_in_at": visit.check_in_at.isoformat() if visit.check_in_at else None,
        "check_out_at": visit.check_out_at.isoformat() if visit.check_out_at else None,
        "actual_duration_minutes": visit.actual_duration_minutes,
        "status": visit.status.value,
    }
    if status is not None:
        out["can_complete_visit"] = status["can_checkout"]
        out["missing_items"] = status["blocking_items"] or status["missing_items"]
    return out


@router.get("/{booking_id}/report")
async def get_visit_report_for_worker(
    booking_id: UUID,
    request: Request,
    response: Response,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """The nurse's own report for a visit, plus what's still outstanding.

    Works both before checkout (to drive the report form and show what is
    blocking completion) and after (so the report stays reachable from the
    Visits list rather than disappearing the moment the visit completes --
    which was the specific gap in item 10).
    """
    _booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    try:
        status = await validate_documentation_completion(booking_id, visit.id, db)
    except WorkflowError:
        status = None
    payload = _report_payload(visit, status)
    response.headers["Cache-Control"] = "no-store, private"
    if visit.check_out_at:
        # Only the finished care summary is audited as a "view"; loading the
        # draft form mid-visit is not a view of a report. (Also: committing
        # before checkout would persist the placeholder VisitRecord that
        # _get_visit_for_worker creates for not-yet-started visits.)
        from app.services.report_access import ACTION_VIEWED, audit_report_event
        await audit_report_event(
            db, actor_id=profile.user_id, actor_type="worker", action=ACTION_VIEWED,
            booking_id=booking_id, request=request, details={"view": "worker"},
        )
        await db.commit()
    return payload


@router.put("/{booking_id}/report")
async def save_visit_report(
    booking_id: UUID,
    payload: VisitReportUpdate,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Save (or re-save) the nurse's visit report.

    Deliberately permitted after checkout as well: a nurse correcting a
    typo in a report an hour later is normal, and refusing it would push
    people into raising support tickets to fix their own notes. Every save
    is audited, so an after-the-fact edit is traceable.
    """
    _booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)

    if payload.care_notes is not None:
        visit.care_notes = payload.care_notes.strip() or None
    if payload.family_summary is not None:
        visit.family_summary = payload.family_summary.strip() or None

    await audit(
        db,
        profile.user_id,
        "worker",
        "visit.report_saved",
        "visit",
        visit.id,
        {"after_checkout": bool(visit.check_out_at)},
    )
    await db.commit()
    await db.refresh(visit)

    try:
        status = await validate_documentation_completion(booking_id, visit.id, db)
    except WorkflowError:
        status = None
    return _report_payload(visit, status)


@router.get("/{booking_id}/report/consumer")
async def get_visit_report_for_consumer(
    booking_id: UUID,
    request: Request,
    response: Response,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """The family-facing visit report (item 14).

    Returns the family summary rather than the nurse's raw clinical notes:
    `care_notes` is the nurse's own working record and is not part of what
    the patient side is shown here.
    """
    bres = await db.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.consumer_id == profile.id,
        )
    )
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOOKING_NOT_FOUND", "message": "This booking could not be found on your account."},
        )

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if not visit:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NO_VISIT_YET",
                "message": "This visit hasn't started yet, so there's no report.",
            },
        )
    if not visit.check_out_at:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VISIT_IN_PROGRESS",
                "message": "The nurse is still with the patient. The report will appear here once the visit is complete.",
            },
        )

    from app.services.report_access import ACTION_VIEWED, audit_report_event
    from app.services.visit_report_service import latest_vitals

    payload = {
        "booking_id": str(booking_id),
        "visit_id": str(visit.id),
        "family_summary": visit.family_summary,
        "check_in_at": visit.check_in_at.isoformat() if visit.check_in_at else None,
        "check_out_at": visit.check_out_at.isoformat() if visit.check_out_at else None,
        "actual_duration_minutes": visit.actual_duration_minutes,
        "photo_urls": list(visit.photo_urls or []),
        "rating_by_consumer": visit.rating_by_consumer,
        # Added so the family's care-summary screen is ONE audited call
        # instead of GET /visits/{id} (which leaked care_notes) + /vitals.
        # Booleans only — the raw checklist/documentation payloads stay out.
        "latest_vitals": await latest_vitals(db, booking_id),
        "has_checklist": bool(visit.checklist_responses),
        "has_documentation": bool(visit.documentation_responses),
    }
    await audit_report_event(
        db, actor_id=profile.user_id, actor_type="consumer", action=ACTION_VIEWED,
        booking_id=booking_id, request=request, details={"view": "consumer"},
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store, private"
    return payload


# ---------------------------------------------------------------------------
# Visit report — PDF
# ---------------------------------------------------------------------------
# Two views of the same document, mirroring the family_summary / care_notes
# split enforced above: the nurse's copy carries her clinical notes, the
# family's copy never does. Which fields are visible is decided here, at the
# API boundary, by which endpoint (and therefore which auth dependency) was
# called.
#
# Flow (replaces "upload to a public Cloudinary URL"):
#   1. GET /{id}/report/pdf | /{id}/report/consumer/pdf   (bearer auth)
#        -> {"pdf_url", "download_path", "expires_in_seconds"}
#      `pdf_url` keeps the old response contract for the mobile apps, but it
#      is now a ~60-second, single-use, user-bound link — not a public file.
#   2. GET /{id}/report/download?token=...
#        -> re-checks access, writes the audit row, renders a PDF watermarked
#           with the viewer's identity + that audit row's id, streams it.
#      Nothing is stored; nothing is cached (Cache-Control: no-store).

_REPORT_PDF_RATE = (20, 10 * 60)  # per user: 20 links / 10 min


def _checkout_required(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "VISIT_NOT_CHECKED_OUT", "message": message})


async def _issue_report_link(
    request: Request, response: Response, current: CurrentUser, booking_id: UUID, view: str
) -> dict:
    from app.core.rate_limit import enforce_rate_limit
    from app.services.report_access import issue_download_token, public_api_base

    await enforce_rate_limit(
        "report_pdf", str(current.id), *_REPORT_PDF_RATE,
        message="You've downloaded this report many times in a short period. Please wait a few minutes.",
    )
    token = issue_download_token(current, booking_id, view)
    path = f"/api/visits/{booking_id}/report/download?token={token}"
    response.headers["Cache-Control"] = "no-store, private"
    return {
        "pdf_url": f"{public_api_base(request)}{path}",
        "download_path": path,
        "expires_in_seconds": settings.REPORT_DOWNLOAD_TOKEN_TTL_SECONDS,
    }


@router.get("/{booking_id}/report/pdf")
async def get_visit_report_pdf_for_worker(
    booking_id: UUID,
    request: Request,
    response: Response,
    profile: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One-time link to the nurse's own copy (clinical notes included)."""
    _booking, visit = await _get_visit_for_worker(db, booking_id, profile.id)
    if not visit.check_out_at:
        raise _checkout_required("The report becomes downloadable once the visit is checked out.")
    return await _issue_report_link(request, response, current, booking_id, "worker")


@router.get("/{booking_id}/report/consumer/pdf")
async def get_visit_report_pdf_for_consumer(
    booking_id: UUID,
    request: Request,
    response: Response,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One-time link to the family's copy — never the internal clinical notes."""
    bres = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    if not bres.scalar_one_or_none():
        raise HTTPException(
            status_code=404,
            detail={"code": "BOOKING_NOT_FOUND", "message": "This booking could not be found on your account."},
        )
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if not visit or not visit.check_out_at:
        raise _checkout_required("The report becomes downloadable once the visit is complete.")
    return await _issue_report_link(request, response, current, booking_id, "consumer")


def _download_error(request: Request, status: int, code: str, message: str) -> Response:
    """JSON for the web app (fetch), a tiny HTML page for the mobile apps,
    which open `pdf_url` in a browser/webview and would otherwise show raw JSON."""
    headers = {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}
    if "text/html" in (request.headers.get("accept") or ""):
        body = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Report unavailable</title><body style='font-family:system-ui;padding:24px'>"
            f"<h3>Report unavailable</h3><p>{html.escape(message)}</p>"
            "<p>Return to the NurseConnect app and tap <b>Download report</b> again.</p></body>"
        )
        return HTMLResponse(body, status_code=status, headers=headers)
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


@router.get("/{booking_id}/report/download", include_in_schema=False)
async def download_visit_report_pdf(
    booking_id: UUID,
    request: Request,
    token: str = Query(..., max_length=4096),
    db: AsyncSession = Depends(get_db),
):
    """Redeem a one-time link: re-authorize, audit, watermark, stream.

    No bearer header here (a browser/webview navigation can't send one) —
    the signed, 60-second, single-use, user+booking-bound token IS the
    credential, and access is re-checked against the DB at redemption so a
    link issued just before a nurse was unassigned stops working.
    """
    from app.services import report_access as ra
    from app.services.visit_report_pdf import PdfWatermark
    from app.services.visit_report_service import (
        load_visit_report_pdf_inputs, render_visit_report, role_label, watermark_identity,
    )

    async def deny(status: int, code: str, message: str, reason: str, actor_id=None, actor_type="anonymous"):
        await ra.audit_report_event(
            db, actor_id=actor_id, actor_type=actor_type, action=ra.ACTION_PDF_DENIED,
            booking_id=booking_id, request=request, details={"reason": reason},
        )
        await db.commit()
        return _download_error(request, status, code, message)

    # This endpoint takes no bearer token, so throttle by IP before doing any
    # work (and before writing "denied" audit rows an attacker could flood).
    from app.core.rate_limit import client_ip, enforce_rate_limit
    await enforce_rate_limit("report_download:ip", client_ip(request), 60, 10 * 60)

    try:
        claims = ra.decode_download_token(token, booking_id)
    except ra.DownloadTokenError as e:
        return await deny(e.status, e.code, e.message, e.reason)

    user_id = UUID(claims["sub"])
    ures = await db.execute(select(User).where(User.id == user_id))
    user = ures.scalar_one_or_none()
    from app.models.enums import UserStatus
    # Same rule as get_current_user: blocked if suspended/deactivated/unverified.
    if user is None or user.status in (
        UserStatus.suspended, UserStatus.deactivated, UserStatus.pending_verification,
    ):
        return await deny(403, "ACCOUNT_NOT_ACTIVE",
                          "Your account can't download reports right now. Please sign in again.",
                          "user_inactive", actor_id=user.id if user else None)

    view = claims["view"]
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    allowed = False
    if booking is not None:
        if view == ra.VIEW_WORKER and user.role == UserRole.worker:
            wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == user.id))
            wp = wres.scalar_one_or_none()
            allowed = wp is not None and booking.worker_id == wp.id
        elif view == ra.VIEW_CONSUMER and user.role == UserRole.consumer:
            cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == user.id))
            cp = cres.scalar_one_or_none()
            allowed = cp is not None and booking.consumer_id == cp.id
    if not allowed:
        return await deny(403, "NOT_AUTHORIZED",
                          "You no longer have access to this visit report.",
                          "access_revoked_or_role_mismatch", actor_id=user.id, actor_type=user.role.value)

    # Consume only after the checks above so a rejected attempt doesn't burn
    # a still-valid link for its rightful owner.
    try:
        await ra.consume_download_token(claims)
    except ra.DownloadTokenError as e:
        return await deny(e.status, e.code, e.message, e.reason, actor_id=user.id, actor_type=user.role.value)

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if visit is None or not visit.check_out_at:
        return await deny(409, "VISIT_NOT_CHECKED_OUT",
                          "The report becomes downloadable once the visit is complete.",
                          "not_checked_out", actor_id=user.id, actor_type=user.role.value)

    include_notes = view == ra.VIEW_WORKER
    generated_at = datetime.now(timezone.utc)
    # The audit row is written first so its id can be printed on the PDF as
    # the traceability "Ref": leaked copy -> ref -> who / when / IP.
    entry = await ra.audit_report_event(
        db, actor_id=user.id, actor_type=user.role.value, action=ra.ACTION_PDF_DOWNLOADED,
        booking_id=booking_id, request=request,
        details={"view": view, "includes_clinical_notes": include_notes},
    )
    ref = entry.id.hex[:12].upper()
    name, hint = watermark_identity(user)
    try:
        inputs = await load_visit_report_pdf_inputs(
            db, visit, booking.booking_ref, include_clinical_notes=include_notes,
        )
        pdf_bytes = await run_in_threadpool(
            render_visit_report, inputs,
            PdfWatermark(viewer_name=name, viewer_role=role_label(user.role),
                         generated_at=generated_at, ref=ref, viewer_hint=hint),
        )
    except Exception as exc:  # noqa: BLE001
        # Log the exception type only — never the rendered content.
        logger.error("visit report PDF render failed booking=%s ref=%s err=%s",
                     booking_id, ref, type(exc).__name__)
        await db.rollback()
        await ra.audit_report_event(
            db, actor_id=user.id, actor_type=user.role.value, action=ra.ACTION_PDF_FAILED,
            booking_id=booking_id, request=request, details={"view": view, "error": type(exc).__name__},
        )
        await db.commit()
        return _download_error(request, 500, "PDF_GENERATION_FAILED",
                               "We couldn't generate the report PDF. Please try again in a moment.")

    # Commit the audit row BEFORE handing out the file: no untraceable copies.
    entry.changes = {**(entry.changes or {}), "ref": ref, "bytes": len(pdf_bytes)}
    await db.commit()

    safe_ref = re.sub(r"[^A-Za-z0-9_-]", "", booking.booking_ref or "")[:40] or "visit"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="care-summary-{safe_ref}.pdf"',
            "Cache-Control": "no-store, private, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Report-Ref": ref,
        },
    )


class ReportClientEvent(BaseModel):
    event: str = Field(..., max_length=40)
    surface: str = Field("care_summary", max_length=40)


@router.post("/{booking_id}/report/client-events", status_code=204)
async def record_report_client_event(
    booking_id: UUID,
    payload: ReportClientEvent,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Browser-reported print/copy/screenshot-key attempts on the care summary.

    DETECTION ONLY, and client-reported: a user who strips the JS never sends
    these. Useful as a signal ("this account kept trying to print"), not as
    proof. Same access rule as the report itself.
    """
    from app.core.rate_limit import enforce_rate_limit
    from app.security.access_control import assert_user_can_access_booking
    from app.services.report_access import ACTION_CLIENT_EVENT, CLIENT_EVENTS, audit_report_event

    if payload.event not in CLIENT_EVENTS:
        raise HTTPException(status_code=422, detail={"code": "UNKNOWN_EVENT", "message": "Unknown event."})
    await assert_user_can_access_booking(db, current, booking_id)
    await enforce_rate_limit("report_client_event", str(current.id), 30, 10 * 60)
    await audit_report_event(
        db, actor_id=current.id, actor_type=current.role.value, action=ACTION_CLIENT_EVENT,
        booking_id=booking_id, request=request,
        details={"event": payload.event, "surface": payload.surface[:40]},
    )
    await db.commit()
    return Response(status_code=204)
