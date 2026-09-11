"""The two guarded visit workflows.

Workflow 1 — Customer Books WITH Material (Composite Care Package): the
platform supplies the procedural kit (`material_included=True`).
Workflow 2 — Customer Books WITHOUT Material (Service-Only): the patient
supplies their own materials, so booking carries a supply guardrail and the
nurse additionally inspects and expiry-checks those supplies on arrival.

Steps 2–7 are identical for both, so they share one set of endpoints that
branch on `booking.material_included` rather than being duplicated:

  Step 1  POST /composite-care/bookings                          (W1: books + uploads Rx)
          POST /composite-care/bookings/service-only             (W2: + supply guardrail)
  Step 2  POST /composite-care/bookings/{id}/approve-prescription (pharmacist/reviewer)
          POST /composite-care/bookings/{id}/reject-prescription
  Step 3  Dispatch reuses the existing accept-booking flow (bookings.py) —
          searching_nurse is claimable exactly like confirmed (see patch there).
  Step 4  POST /composite-care/bookings/{id}/nurse-safety-checklist   (nurse)
          POST /composite-care/bookings/{id}/patient-safety-verification (consumer)
          POST /composite-care/bookings/{id}/report-supply-issue       (W2 nurse)
          GET  /composite-care/bookings/{id}/safety-checklist-status
  Step 5  POST /composite-care/bookings/{id}/pre-procedure-photo     (nurse)
  Step 6  POST /composite-care/bookings/{id}/post-procedure-photo    (nurse)
          POST /composite-care/bookings/{id}/generate-completion-otp (consumer)
          POST /composite-care/bookings/{id}/verify-completion-otp   (nurse -> checkout)
  Step 7  GET  /composite-care/bookings/{id}/invoice

Start-OTP (Step 4's trigger) reuses the platform's existing visit-start-OTP
handshake in visits.py (POST /visits/{id}/generate-start-otp,
POST /visits/{id}/verify-start-otp) — no need to duplicate it. Once that
succeeds, the nurse and patient safety-checklist screens in this file
unlock.
"""
import random
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import (
    CurrentUser,
    get_consumer_profile,
    get_current_user,
    get_worker_profile,
    require_roles,
)
from app.core.redis_client import redis_client
from app.integrations.providers import ExternalProviderError, cloudinary_client, msg91_client
from app.models.enums import (
    BookingStatus,
    BookingType,
    EscalationLevel,
    EscalationStatus,
    NotificationChannel,
    PaymentStatus,
    PrescriptionStatus,
    UserRole,
    VisitStatus,
)
from app.models.models import (
    Booking,
    CarePackage,
    ConsumerProfile,
    Escalation,
    Patient,
    Prescription,
    User,
    VisitRecord,
    WorkerProfile,
)
from app.schemas.schemas import (
    CompositeBookingCreate,
    CompletionOtpVerifyRequest,
    InvoiceOut,
    NurseSafetyChecklistSubmit,
    PatientSafetyVerificationSubmit,
    PostProcedurePhotoSubmit,
    PreProcedurePhotoSubmit,
    PrescriptionQueueItem,
    SafetyChecklistStatusOut,
    ServiceOnlyBookingCreate,
    SupplyIssueReport,
    VisitRecordOut,
)
from app.schemas.schemas import BookingOut
from app.services.care_workflow_engine import (
    WorkflowError,
    render_family_summary,
    validate_documentation_completion,
)
from app.services.common_services import audit, notify_parties
from app.services.composite_care_workflow import (
    checklist_items_for,
    diff_safety_checklists,
    generate_invoice,
    is_guarded_workflow,
)
from app.services.insurance_service import create_or_update_assessment
from app.websockets.manager import booking_topic, manager

router = APIRouter(prefix="/composite-care", tags=["composite-care"])


def _gen_booking_ref() -> str:
    return f"NC{datetime.now().strftime('%y%m%d')}{uuid4().hex[:6].upper()}"


async def _upload_base64(data_base64: str, folder: str, resource_type: str = "image") -> dict:
    try:
        return await cloudinary_client.upload_base64(
            data_base64, folder=folder, resource_type=resource_type
        )
    except ExternalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _resolve_prescription(payload, consumer_id) -> tuple[str, Optional[str]]:
    """Both workflows require a doctor's prescription at booking time, passed
    either as an already-hosted URL or as base64 for us to upload."""
    url = payload.prescription_cloudinary_url
    public_id = payload.prescription_cloudinary_public_id
    if payload.prescription_base64:
        upload = await _upload_base64(
            payload.prescription_base64,
            folder=f"nurseconnect/prescriptions/{consumer_id}",
            resource_type="auto",
        )
        url = upload.get("secure_url") or upload.get("url")
        public_id = upload.get("public_id")
    if not url:
        raise HTTPException(
            status_code=400, detail="Attach a prescription (photo or file) before booking"
        )
    return url, public_id


# ============================================================================
# Step 1 — Booking & Payment
# ============================================================================
@router.post("/bookings", response_model=BookingOut)
async def create_composite_booking(
    payload: CompositeBookingCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Patient selects a Composite Care Package, uploads their Rx, and the
    booking is created pending payment. Payment itself still goes through
    the existing /payments/order + /payments/verify flow — on capture,
    payments.py routes material_included bookings to PRESCRIPTION_PENDING
    instead of CONFIRMED (see patch there)."""
    pres = await db.execute(
        select(Patient).where(Patient.id == payload.patient_id, Patient.consumer_id == profile.id)
    )
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    kres = await db.execute(
        select(CarePackage).where(CarePackage.id == payload.package_id, CarePackage.is_active.is_(True))
    )
    package = kres.scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Care package not found")
    if not package.material_included:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NOT_A_COMPOSITE_PACKAGE",
                "message": "This package does not bundle a procedural kit. Use the standard booking flow.",
            },
        )

    package_fee = package.package_price or package.per_visit_price
    if not package_fee:
        raise HTTPException(status_code=400, detail="Package has no configured price")

    prescription_url, prescription_public_id = await _resolve_prescription(payload, profile.id)

    booking = Booking(
        booking_ref=_gen_booking_ref(),
        consumer_id=profile.id,
        patient_id=patient.id,
        booking_type=BookingType.package,
        package_id=package.id,
        worker_id=None,
        status=BookingStatus.pending_payment,
        scheduled_date=payload.scheduled_date,
        scheduled_start_time=payload.scheduled_start_time,
        scheduled_duration_minutes=package.shift_hours * 60 if package.shift_hours else 60,
        address_snapshot=payload.address_snapshot,
        latitude=payload.latitude,
        longitude=payload.longitude,
        base_amount=package_fee,
        surge_amount=0,
        subsidy_amount=0,
        tax_amount=0,
        total_amount=package_fee,  # single bundled Package_Fee — kit is not billed separately
        payment_status=PaymentStatus.pending,
        special_instructions=payload.special_instructions,
        material_included=True,
    )
    db.add(booking)
    await db.flush()

    prescription = Prescription(
        patient_id=patient.id,
        booking_id=booking.id,
        uploaded_by=profile.user_id,
        cloudinary_url=prescription_url,
        cloudinary_public_id=prescription_public_id,
        status=PrescriptionStatus.pending_review,
    )
    db.add(prescription)
    await db.flush()
    booking.prescription_id = prescription.id

    await audit(db, profile.user_id, "consumer", "composite_care.booking_created", "booking", booking.id)
    await db.commit()
    await db.refresh(booking)
    return BookingOut.model_validate(booking)


@router.post("/bookings/service-only", response_model=BookingOut)
async def create_service_only_booking(
    payload: ServiceOnlyBookingCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Workflow 2 Step 1 — patient books a Service-Only package and provides
    their own materials.

    The supply guardrail is enforced here rather than in the client: every
    confirmation item must be ticked AND a photo of the supplies laid out
    next to the prescription must be attached. Because the booking is created
    in `pending_payment`, failing either check means no payable booking ever
    exists — which is what "the app blocks the payment step" means on the
    server side, where it can't be bypassed."""
    pres = await db.execute(
        select(Patient).where(Patient.id == payload.patient_id, Patient.consumer_id == profile.id)
    )
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    kres = await db.execute(
        select(CarePackage).where(CarePackage.id == payload.package_id, CarePackage.is_active.is_(True))
    )
    package = kres.scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Care package not found")
    if package.material_included:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NOT_A_SERVICE_ONLY_PACKAGE",
                "message": "This package bundles a procedural kit. Use the composite booking flow.",
            },
        )

    confirmation = payload.supply_confirmation.model_dump()
    unconfirmed = sorted(k for k, v in confirmation.items() if not v)
    if unconfirmed:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SUPPLIES_NOT_CONFIRMED",
                "message": "Confirm you have every required supply ready before booking.",
                "unconfirmed_items": unconfirmed,
            },
        )

    supply_photo_url = payload.supply_photo_url
    if payload.supply_photo_base64:
        upload = await _upload_base64(
            payload.supply_photo_base64,
            folder=f"nurseconnect/supply-proof/{profile.id}",
        )
        supply_photo_url = upload.get("secure_url") or upload.get("url")
    if not supply_photo_url:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SUPPLY_PHOTO_REQUIRED",
                "message": "Attach one photo of your supplies next to the prescription before booking.",
            },
        )

    package_fee = package.package_price or package.per_visit_price
    if not package_fee:
        raise HTTPException(status_code=400, detail="Package has no configured price")

    prescription_url, prescription_public_id = await _resolve_prescription(payload, profile.id)

    booking = Booking(
        booking_ref=_gen_booking_ref(),
        consumer_id=profile.id,
        patient_id=patient.id,
        booking_type=BookingType.package,
        package_id=package.id,
        worker_id=None,
        status=BookingStatus.pending_payment,
        scheduled_date=payload.scheduled_date,
        scheduled_start_time=payload.scheduled_start_time,
        scheduled_duration_minutes=package.shift_hours * 60 if package.shift_hours else 60,
        address_snapshot=payload.address_snapshot,
        latitude=payload.latitude,
        longitude=payload.longitude,
        base_amount=package_fee,
        surge_amount=0,
        subsidy_amount=0,
        tax_amount=0,
        total_amount=package_fee,  # Service-Only fee — no materials are billed
        payment_status=PaymentStatus.pending,
        special_instructions=payload.special_instructions,
        material_included=False,
        patient_supply_confirmation=confirmation,
        patient_supply_photo_url=supply_photo_url,
    )
    db.add(booking)
    await db.flush()

    prescription = Prescription(
        patient_id=patient.id,
        booking_id=booking.id,
        uploaded_by=profile.user_id,
        cloudinary_url=prescription_url,
        cloudinary_public_id=prescription_public_id,
        status=PrescriptionStatus.pending_review,
    )
    db.add(prescription)
    await db.flush()
    booking.prescription_id = prescription.id

    await audit(
        db, profile.user_id, "consumer", "composite_care.service_only_booking_created", "booking", booking.id
    )
    await db.commit()
    await db.refresh(booking)
    return BookingOut.model_validate(booking)


# ============================================================================
# Step 2 — Quality & Verification (pharmacist / admin dashboard)
# ============================================================================
@router.get("/prescription-queue", response_model=List[PrescriptionQueueItem])
async def prescription_queue(
    current: CurrentUser = Depends(require_roles(UserRole.admin, UserRole.reviewer, UserRole.operations)),
    db: AsyncSession = Depends(get_db),
):
    """Every booking waiting on pharmacist Rx review, oldest first.

    Covers both guarded workflows — Workflow 2 rows additionally carry the
    patient's supply confirmation and supply photo, which the pharmacist
    checks against the prescription before approving."""
    res = await db.execute(
        select(Booking, Prescription, Patient, User)
        .join(Prescription, Prescription.id == Booking.prescription_id)
        .join(Patient, Patient.id == Booking.patient_id)
        .join(ConsumerProfile, ConsumerProfile.id == Booking.consumer_id)
        .join(User, User.id == ConsumerProfile.user_id)
        .where(Booking.status == BookingStatus.prescription_pending)
        .order_by(Booking.created_at.asc())
    )
    return [
        PrescriptionQueueItem(
            booking_id=booking.id,
            booking_ref=booking.booking_ref,
            patient_name=patient.full_name,
            consumer_name=user.full_name,
            scheduled_date=booking.scheduled_date,
            scheduled_start_time=booking.scheduled_start_time,
            total_amount=booking.total_amount,
            material_included=booking.material_included,
            prescription_url=prescription.cloudinary_url,
            patient_supply_confirmation=booking.patient_supply_confirmation,
            patient_supply_photo_url=booking.patient_supply_photo_url,
            created_at=booking.created_at,
        )
        for booking, prescription, patient, user in res.all()
    ]


@router.post("/bookings/{booking_id}/approve-prescription", response_model=BookingOut)
async def approve_prescription(
    booking_id: UUID,
    current: CurrentUser = Depends(require_roles(UserRole.admin, UserRole.reviewer, UserRole.operations)),
    db: AsyncSession = Depends(get_db),
):
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking or not is_guarded_workflow(booking):
        raise HTTPException(status_code=404, detail="Guarded care booking not found")
    if booking.status != BookingStatus.prescription_pending:
        raise HTTPException(
            status_code=400,
            detail={"code": "NOT_PRESCRIPTION_PENDING", "message": f"Booking is in status {booking.status.value}"},
        )

    rxres = await db.execute(select(Prescription).where(Prescription.id == booking.prescription_id))
    prescription = rxres.scalar_one_or_none()
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")

    from app.services.composite_care_workflow import is_prescription_expired
    if is_prescription_expired(prescription) and not prescription.renewal_consultation_paid:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRESCRIPTION_EXPIRED",
                "message": (
                    "This prescription is more than 6 months old, so it can't be approved as-is. "
                    "The patient needs to pay a ₹100 doctor consultation charge to renew it "
                    "before this can be approved."
                ),
                "renewal_consultation_fee_inr": 100,
            },
        )

    prescription.status = PrescriptionStatus.verified
    prescription.verified_by = current.id
    prescription.verified_at = datetime.now(timezone.utc)

    booking.status = BookingStatus.searching_nurse
    if booking.dispatch_started_at is None:
        booking.dispatch_started_at = datetime.now(timezone.utc)

    await audit(db, current.id, current.role.value, "composite_care.prescription_approved", "booking", booking.id)
    await db.commit()

    # Best-effort push to nearby qualified workers, same as a normal
    # confirmed booking entering dispatch.
    try:
        from app.services.dispatch import notify_nearby_workers
        await notify_nearby_workers(db, booking)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    await db.refresh(booking)
    return BookingOut.model_validate(booking)


# ============================================================================
# Prescription renewal-consultation fee (₹100) — required before an expired
# Rx (see composite_care_workflow.is_prescription_expired) can be approved.
# Mirrors the plain booking-payment flow in app/api/v1/payments.py, but is a
# small standalone Razorpay order tied to the Prescription row rather than
# the booking's own total_amount.
# ============================================================================
class PrescriptionRenewalOrderResponse(BaseModel):
    razorpay_order_id: str
    razorpay_key_id: str
    amount: int
    currency: str = "INR"
    prescription_id: UUID


class PrescriptionRenewalVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/bookings/{booking_id}/prescription-renewal-order", response_model=PrescriptionRenewalOrderResponse)
async def create_prescription_renewal_order(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    from app.core.config import settings
    from app.integrations import razorpay_client
    from app.services.composite_care_workflow import (
        PRESCRIPTION_RENEWAL_CONSULTATION_FEE_INR,
        is_prescription_expired,
    )

    bres = await db.execute(select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id))
    booking = bres.scalar_one_or_none()
    if not booking or not booking.prescription_id:
        raise HTTPException(status_code=404, detail="Booking or prescription not found")

    rxres = await db.execute(select(Prescription).where(Prescription.id == booking.prescription_id))
    prescription = rxres.scalar_one_or_none()
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    if not is_prescription_expired(prescription):
        raise HTTPException(status_code=400, detail="This prescription is not expired — no renewal fee needed")
    if prescription.renewal_consultation_paid:
        raise HTTPException(status_code=400, detail="Renewal consultation already paid")

    amount_paise = PRESCRIPTION_RENEWAL_CONSULTATION_FEE_INR * 100
    order = await razorpay_client.create_order(
        amount_paise=amount_paise,
        currency="INR",
        receipt=f"rxrenewal-{prescription.id}",
        notes={"booking_id": str(booking.id), "prescription_id": str(prescription.id), "purpose": "prescription_renewal_consultation"},
    )
    prescription.renewal_consultation_order_id = order["id"]
    await db.commit()

    return PrescriptionRenewalOrderResponse(
        razorpay_order_id=order["id"],
        razorpay_key_id=settings.RAZORPAY_KEY_ID or "rzp_test_placeholder",
        amount=amount_paise,
        prescription_id=prescription.id,
    )


@router.post("/bookings/{booking_id}/prescription-renewal-verify")
async def verify_prescription_renewal_payment(
    booking_id: UUID,
    payload: PrescriptionRenewalVerifyRequest,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    from app.integrations import razorpay_client

    bres = await db.execute(select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id))
    booking = bres.scalar_one_or_none()
    if not booking or not booking.prescription_id:
        raise HTTPException(status_code=404, detail="Booking or prescription not found")

    rxres = await db.execute(select(Prescription).where(Prescription.id == booking.prescription_id))
    prescription = rxres.scalar_one_or_none()
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")

    if prescription.renewal_consultation_paid:
        return {"verified": True, "idempotent_replay": True}

    if prescription.renewal_consultation_order_id != payload.razorpay_order_id:
        raise HTTPException(status_code=400, detail="Order mismatch")

    ok = razorpay_client.verify_payment_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid signature")

    prescription.renewal_consultation_paid = True
    prescription.renewal_consultation_paid_at = datetime.now(timezone.utc)
    await audit(db, profile.user_id, "consumer", "prescription.renewal_consultation_paid", "prescription", prescription.id)
    await db.commit()
    return {"verified": True}


class PrescriptionRejectRequest(BaseModel):
    """Why the pharmacist rejected the Rx. Sent as a body rather than a query
    string — it's free text shown to the patient, and reasons routinely run
    past what belongs in a URL."""
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/bookings/{booking_id}/reject-prescription", response_model=BookingOut)
async def reject_prescription(
    booking_id: UUID,
    payload: PrescriptionRejectRequest,
    current: CurrentUser = Depends(require_roles(UserRole.admin, UserRole.reviewer, UserRole.operations)),
    db: AsyncSession = Depends(get_db),
):
    reason = payload.reason.strip()
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking or not is_guarded_workflow(booking):
        raise HTTPException(status_code=404, detail="Guarded care booking not found")

    rxres = await db.execute(select(Prescription).where(Prescription.id == booking.prescription_id))
    prescription = rxres.scalar_one_or_none()
    if prescription:
        prescription.status = PrescriptionStatus.rejected
        prescription.verified_by = current.id
        prescription.verified_at = datetime.now(timezone.utc)
        prescription.rejection_reason = reason

    booking.status = BookingStatus.cancelled
    booking.cancellation_reason = f"Prescription rejected: {reason}"
    booking.cancelled_at = datetime.now(timezone.utc)

    await audit(
        db, current.id, current.role.value, "composite_care.prescription_rejected", "booking", booking.id,
        {"reason": reason},
    )
    await db.commit()
    await db.refresh(booking)
    return BookingOut.model_validate(booking)


# ============================================================================
# Step 4 — Synchronized safety checklist (nurse + patient) + anti-cheat
# ============================================================================
def _require_checklist_items(answers: dict, booking: Booking) -> dict:
    """Keep only the items this booking's workflow asks, and reject the
    submission unless every one of them was actually answered.

    The request models carry the union of both workflows' fields (the two
    sets overlap), so this is where the per-workflow contract is enforced —
    a Workflow 2 nurse can't satisfy the checklist by sending Workflow 1's
    questions, and unanswered items can't slip through as null.
    """
    items = checklist_items_for(booking.material_included)
    missing = [k for k in items if answers.get(k) is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CHECKLIST_INCOMPLETE",
                "message": "Answer every safety item before submitting.",
                "missing_items": missing,
                "required_items": list(items),
            },
        )
    return {k: bool(answers[k]) for k in items}


async def _get_booking_and_visit(db: AsyncSession, booking_id: UUID) -> tuple[Booking, VisitRecord]:
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking or not is_guarded_workflow(booking):
        raise HTTPException(status_code=404, detail="Guarded care booking not found")
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    if not visit:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VISIT_NOT_STARTED",
                "message": "Start-OTP handshake has not completed yet — the nurse must verify the start OTP first.",
            },
        )
    return booking, visit


@router.post("/bookings/{booking_id}/nurse-safety-checklist")
async def submit_nurse_safety_checklist(
    booking_id: UUID,
    payload: NurseSafetyChecklistSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if not visit.check_in_at:
        raise HTTPException(status_code=400, detail="Verify the start OTP before submitting the safety checklist")

    answers = _require_checklist_items(payload.model_dump(exclude={"notes"}), booking)
    visit.pre_procedure_checklist = answers
    if payload.notes:
        visit.pre_procedure_checklist["notes"] = payload.notes
    visit.pre_procedure_checklist_at = datetime.now(timezone.utc)

    await audit(db, profile.user_id, "worker", "composite_care.nurse_checklist_submitted", "visit", visit.id)
    await db.commit()

    # Unlock / refresh the mirrored card on the patient's screen.
    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "safety_checklist.nurse_submitted", "booking_id": str(booking_id)},
    )
    return await _safety_checklist_status(booking, visit)


@router.post("/bookings/{booking_id}/patient-safety-verification")
async def submit_patient_safety_verification(
    booking_id: UUID,
    payload: PatientSafetyVerificationSubmit,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.consumer_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your booking")
    if not visit.pre_procedure_checklist:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NURSE_CHECKLIST_NOT_SUBMITTED",
                "message": "Waiting for the nurse to complete their checklist first.",
            },
        )

    verification = _require_checklist_items(payload.model_dump(), booking)
    visit.patient_safety_verification = verification
    visit.patient_safety_verification_at = datetime.now(timezone.utc)

    mismatches = diff_safety_checklists(
        visit.pre_procedure_checklist, verification, booking.material_included
    )
    if mismatches:
        visit.quality_discrepancy = True
        booking.status = BookingStatus.quality_discrepancy_alert
        escalation = Escalation(
            booking_id=booking.id,
            visit_record_id=visit.id,
            worker_id=booking.worker_id,
            patient_id=booking.patient_id,
            level=EscalationLevel.contact_doctor,
            status=EscalationStatus.open,
            trigger_type="quality_discrepancy",
            trigger_details={
                "mismatched_items": mismatches,
                "nurse_checklist": visit.pre_procedure_checklist,
                "patient_verification": verification,
            },
            notes="Nurse self-reported all-clear on safety checklist but patient/family flagged a mismatch. Needs supervisor review call before the procedure proceeds.",
        )
        db.add(escalation)
        await audit(
            db, profile.user_id, "consumer", "composite_care.quality_discrepancy_alert", "visit", visit.id,
            {"mismatched_items": mismatches},
        )
        await db.commit()
        await notify_parties(
            db,
            ["ops"],
            {"booking_id": str(booking_id)},
            "quality_discrepancy_alert",
            title="Quality discrepancy — supervisor review needed",
            body=f"Booking {booking.booking_ref}: nurse/patient safety-checklist mismatch on {', '.join(mismatches)}.",
        )
        await manager.broadcast(
            booking_topic(booking_id),
            {"type": "safety_checklist.discrepancy_alert", "booking_id": str(booking_id), "mismatched_items": mismatches},
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "QUALITY_DISCREPANCY_ALERT",
                "message": "Your answers don't match what the nurse reported. This has been flagged for a supervisor to call you.",
                "mismatched_items": mismatches,
            },
        )

    await audit(db, profile.user_id, "consumer", "composite_care.patient_verification_submitted", "visit", visit.id)
    await db.commit()
    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "safety_checklist.verified", "booking_id": str(booking_id)},
    )
    return await _safety_checklist_status(booking, visit)


@router.post("/bookings/{booking_id}/report-supply-issue", response_model=SafetyChecklistStatusOut)
async def report_supply_issue(
    booking_id: UUID,
    payload: SupplyIssueReport,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Workflow 2 Step 4 — the nurse found a problem with the patient's own
    supplies (broken sterile packaging, expired medicine).

    Blocks the procedure and raises an ops escalation. Deliberately separate
    from `quality_discrepancy`, which is the patient disputing the nurse's
    hygiene self-report: this is the nurse reporting the patient's materials,
    so conflating them would lose the distinction ops needs to triage."""
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if booking.material_included:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NOT_A_SERVICE_ONLY_BOOKING",
                "message": "Supply inspection applies to service-only bookings, where the patient provides materials.",
            },
        )

    visit.supply_issue_reported = True
    visit.supply_issue_details = {
        "issue_type": payload.issue_type,
        "notes": payload.notes,
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "reported_by_worker_id": str(profile.id),
    }
    booking.status = BookingStatus.quality_discrepancy_alert

    db.add(
        Escalation(
            booking_id=booking.id,
            visit_record_id=visit.id,
            worker_id=booking.worker_id,
            patient_id=booking.patient_id,
            level=EscalationLevel.contact_doctor,
            status=EscalationStatus.open,
            trigger_type="supply_issue",
            trigger_details=visit.supply_issue_details,
            notes=(
                "Nurse reported a problem with the patient's own supplies. "
                "Procedure is blocked pending ops review."
            ),
        )
    )
    await audit(
        db, profile.user_id, "worker", "composite_care.supply_issue_reported", "visit", visit.id,
        {"issue_type": payload.issue_type},
    )
    await db.commit()
    await db.refresh(visit)

    await notify_parties(
        db,
        ["ops"],
        {"booking_id": str(booking_id)},
        "supply_issue_reported",
        title="Supply issue — review needed",
        body=f"Booking {booking.booking_ref}: nurse reported '{payload.issue_type}' with the patient's supplies.",
    )
    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "supply.issue_reported", "booking_id": str(booking_id), "issue_type": payload.issue_type},
    )
    return await _safety_checklist_status(booking, visit)


@router.get("/bookings/{booking_id}/safety-checklist-status", response_model=SafetyChecklistStatusOut)
async def get_safety_checklist_status(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    return await _safety_checklist_status(booking, visit)


async def _safety_checklist_status(booking: Booking, visit: VisitRecord) -> SafetyChecklistStatusOut:
    return SafetyChecklistStatusOut(
        nurse_checklist=visit.pre_procedure_checklist,
        nurse_submitted_at=visit.pre_procedure_checklist_at,
        patient_verification=visit.patient_safety_verification,
        patient_submitted_at=visit.patient_safety_verification_at,
        quality_discrepancy=visit.quality_discrepancy,
        both_submitted=bool(visit.pre_procedure_checklist and visit.patient_safety_verification),
        material_included=booking.material_included,
        checklist_items=list(checklist_items_for(booking.material_included)),
        supply_issue_reported=visit.supply_issue_reported,
    )


# ============================================================================
# Step 5 — Photo Proof & Procedure Start
# ============================================================================
@router.post("/bookings/{booking_id}/pre-procedure-photo", response_model=VisitRecordOut)
async def submit_pre_procedure_photo(
    booking_id: UUID,
    payload: PreProcedurePhotoSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if not (visit.pre_procedure_checklist and visit.patient_safety_verification):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SAFETY_CHECKLIST_INCOMPLETE",
                "message": "Both the nurse checklist and patient verification must be complete before starting the procedure.",
            },
        )
    if visit.quality_discrepancy:
        raise HTTPException(
            status_code=409,
            detail={"code": "QUALITY_DISCREPANCY_ALERT", "message": "This booking is flagged for supervisor review."},
        )
    if visit.supply_issue_reported:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUPPLY_ISSUE_REPORTED",
                "message": "You reported a problem with the patient's supplies. Ops must resolve it before the procedure can start.",
            },
        )

    photo_url = payload.photo_url
    if payload.photo_base64:
        try:
            upload = await cloudinary_client.upload_base64(
                payload.photo_base64,
                folder=f"nurseconnect/visits/{visit.id}/pre-procedure",
                resource_type="image",
            )
        except ExternalProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        photo_url = upload.get("secure_url") or upload.get("url")
    if not photo_url:
        raise HTTPException(status_code=400, detail="Attach the pre-procedure photo")

    visit.pre_procedure_photo_url = photo_url
    visit.pre_procedure_photo_meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latitude": float(payload.latitude),
        "longitude": float(payload.longitude),
        "order_id": str(booking.id),
        "booking_ref": booking.booking_ref,
    }
    visit.status = VisitStatus.in_progress
    booking.status = BookingStatus.in_progress

    await audit(db, profile.user_id, "worker", "composite_care.pre_procedure_photo", "visit", visit.id)
    await db.commit()
    await db.refresh(visit)
    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "visit.procedure_started", "booking_id": str(booking_id)},
    )
    return VisitRecordOut.model_validate(visit)


# ============================================================================
# Step 6 — Post-Procedure & Service Closure
# ============================================================================
@router.post("/bookings/{booking_id}/post-procedure-photo", response_model=VisitRecordOut)
async def submit_post_procedure_photo(
    booking_id: UUID,
    payload: PostProcedurePhotoSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if not visit.pre_procedure_photo_url:
        raise HTTPException(status_code=400, detail="Pre-procedure photo must be captured first")

    photo_url = payload.photo_url
    if payload.photo_base64:
        try:
            upload = await cloudinary_client.upload_base64(
                payload.photo_base64,
                folder=f"nurseconnect/visits/{visit.id}/post-procedure",
                resource_type="image",
            )
        except ExternalProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        photo_url = upload.get("secure_url") or upload.get("url")
    if not photo_url:
        raise HTTPException(status_code=400, detail="Attach the post-procedure photo")

    visit.post_procedure_photo_url = photo_url
    visit.post_procedure_photo_meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latitude": float(payload.latitude),
        "longitude": float(payload.longitude),
        "order_id": str(booking.id),
    }
    await audit(db, profile.user_id, "worker", "composite_care.post_procedure_photo", "visit", visit.id)
    await db.commit()
    await db.refresh(visit)
    return VisitRecordOut.model_validate(visit)


# ---- Completion OTP (mirrors visits.py's start-OTP pattern) --------------
_COMPLETION_OTP_TTL_SECONDS = 600
_COMPLETION_OTP_MAX_ATTEMPTS = 5


def _completion_otp_key(booking_id) -> str:
    return f"visit_completion_otp:{booking_id}"


def _completion_attempts_key(booking_id) -> str:
    return f"visit_completion_otp_attempts:{booking_id}"


@router.post("/bookings/{booking_id}/generate-completion-otp")
async def generate_completion_otp(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.consumer_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your booking")
    if not visit.post_procedure_photo_url:
        raise HTTPException(status_code=400, detail="Post-procedure photo must be captured before closing the visit")

    existing = await redis_client.get(_completion_otp_key(booking_id))
    if existing:
        ttl = await redis_client.ttl(_completion_otp_key(booking_id))
        otp_code = existing.decode() if isinstance(existing, bytes) else existing
        return {"sent": True, "otp": otp_code, "expires_in_seconds": ttl}

    otp_code = str(random.randint(1000, 9999))
    await redis_client.setex(_completion_otp_key(booking_id), _COMPLETION_OTP_TTL_SECONDS, otp_code)
    await redis_client.delete(_completion_attempts_key(booking_id))

    ures = await db.execute(select(User).where(User.id == profile.user_id))
    user = ures.scalar_one_or_none()
    sms_sent = False
    if user and user.phone_e164:
        try:
            resp = await msg91_client.send_otp(user.phone_e164, otp_code)
            sms_sent = resp.get("type") == "success"
        except Exception:
            sms_sent = False

    await audit(db, profile.user_id, "consumer", "composite_care.completion_otp_generated", "booking", booking.id)
    await db.commit()
    return {
        "sent": True,
        "sms_sent": sms_sent,
        "message": "Completion code generated. Give it to your nurse to close out the visit.",
        "expires_in_seconds": _COMPLETION_OTP_TTL_SECONDS,
        "otp": otp_code,
    }


@router.post("/bookings/{booking_id}/verify-completion-otp")
async def verify_completion_otp(
    booking_id: UUID,
    payload: CompletionOtpVerifyRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Nurse enters the Completion_OTP. On success this performs full
    checkout: mandatory-documentation gate, family summary, insurance
    assessment, COMPLETED status, invoice generation, and (best-effort)
    the WhatsApp post-service micro-survey."""
    booking, visit = await _get_booking_and_visit(db, booking_id)
    if booking.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if not visit.post_procedure_photo_url:
        raise HTTPException(status_code=400, detail="Post-procedure photo must be captured first")
    if visit.check_out_at:
        raise HTTPException(status_code=400, detail="Already checked out")

    attempts_raw = await redis_client.get(_completion_attempts_key(booking_id))
    attempts = int(attempts_raw) if attempts_raw else 0
    if attempts >= _COMPLETION_OTP_MAX_ATTEMPTS:
        await redis_client.delete(_completion_otp_key(booking_id))
        await redis_client.delete(_completion_attempts_key(booking_id))
        raise HTTPException(
            status_code=400,
            detail={"code": "OTP_MAX_ATTEMPTS_EXCEEDED", "message": "Too many incorrect attempts. Ask the patient to generate a new completion code."},
        )

    stored_otp = await redis_client.get(_completion_otp_key(booking_id))
    if not stored_otp:
        raise HTTPException(status_code=400, detail={"code": "OTP_EXPIRED", "message": "Completion code has expired."})
    stored_otp = stored_otp.decode() if isinstance(stored_otp, bytes) else stored_otp
    if payload.otp.strip() != stored_otp:
        pipe = redis_client.pipeline()
        pipe.incr(_completion_attempts_key(booking_id))
        pipe.expire(_completion_attempts_key(booking_id), _COMPLETION_OTP_TTL_SECONDS)
        await pipe.execute()
        remaining = _COMPLETION_OTP_MAX_ATTEMPTS - (attempts + 1)
        raise HTTPException(
            status_code=400,
            detail={"code": "OTP_INVALID", "message": f"Incorrect completion code. {remaining} attempt(s) remaining."},
        )

    try:
        doc_status = await validate_documentation_completion(booking_id, visit.id, db)
    except WorkflowError as we:
        raise HTTPException(status_code=we.http_status, detail={"code": we.code, "message": we.message}) from None
    if not doc_status["can_checkout"]:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MANDATORY_DOCUMENTATION_INCOMPLETE",
                "message": "Mandatory documentation (e.g. vitals) is incomplete.",
                "missing_items": doc_status["blocking_items"] or doc_status["missing_items"],
            },
        )

    family_summary = (payload.family_summary or "").strip() or await render_family_summary(booking_id, visit.id, db)

    await redis_client.delete(_completion_otp_key(booking_id))
    await redis_client.delete(_completion_attempts_key(booking_id))

    visit.check_out_at = datetime.now(timezone.utc)
    visit.check_out_latitude = payload.latitude
    visit.check_out_longitude = payload.longitude
    if visit.check_in_at:
        visit.actual_duration_minutes = int((visit.check_out_at - visit.check_in_at).total_seconds() / 60)
    visit.family_summary = family_summary
    visit.care_notes = payload.care_notes or visit.care_notes
    visit.status = VisitStatus.completed
    visit.documentation_complete = True
    booking.status = BookingStatus.completed
    profile.completed_visits_count += 1

    try:
        assessment = await create_or_update_assessment(db, booking, visit)
        coverage_summary = {
            "coverage_status": assessment.coverage_status.value,
            "coverage_percent": float(assessment.coverage_percent),
        }
    except Exception:  # noqa: BLE001
        coverage_summary = None

    # Step 7 — Automated Invoicing: Composite Healthcare Service, 0% GST.
    invoice = await generate_invoice(db, booking, visit)

    await audit(
        db, profile.user_id, "worker", "composite_care.checkout_via_otp", "visit", visit.id,
        {"duration_min": visit.actual_duration_minutes, "coverage": coverage_summary, "invoice_number": invoice.invoice_number},
    )
    await db.commit()
    await db.refresh(visit)
    await db.refresh(invoice)

    await manager.broadcast(
        booking_topic(booking_id),
        {"type": "visit.completed", "booking_id": str(booking_id), "invoice_number": invoice.invoice_number},
    )

    # Step 6 close-out — WhatsApp post-service micro-survey (Interakt). Passing
    # `channels` explicitly is what actually routes this over WhatsApp — the
    # notify_parties()/send_notification() default is in-app + push, so
    # without this the spec's "Automated WhatsApp feedback survey" silently
    # never reaches WhatsApp even though the audit trail looks identical.
    try:
        await notify_parties(
            db,
            ["family"],
            {"booking_id": str(booking_id)},
            "post_service_micro_survey",
            title="How was your visit?",
            body=f"Your {booking.booking_ref} visit is complete. Please rate your experience.",
            channels=[NotificationChannel.whatsapp],
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    return {
        "visit": VisitRecordOut.model_validate(visit),
        "invoice": InvoiceOut.model_validate(invoice),
    }


# ============================================================================
# Step 7 — Invoice retrieval
# ============================================================================
@router.get("/bookings/{booking_id}/invoice", response_model=InvoiceOut)
async def get_invoice(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.models import Invoice
    ires = await db.execute(select(Invoice).where(Invoice.booking_id == booking_id))
    invoice = ires.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not generated yet — booking must be completed first")
    return InvoiceOut.model_validate(invoice)
