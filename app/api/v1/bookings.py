"""Booking lifecycle: create, accept, cancel, list, escalate."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.provider_types import is_physical_capable, is_tele_capable
from app.core.deps import (
    CurrentUser,
    get_consumer_profile,
    get_current_user,
    get_worker_profile,
    is_admin,
)
from app.models.enums import (
    BookingStatus,
    BookingType,
    EscalationLevel,
    EscalationStatus,
    PackageBookingStatus,
    UserRole,
    VisitStatus,
)
from app.models.models import (
    AuditLog,
    Booking,
    CarePackage,
    CarePackageBooking,
    ConsumerProfile,
    Escalation,
    Patient,
    ServiceCatalogue,
    SubsidyEligibility,
    User,
    VisitRecord,
    WorkerProfile,
)
from app.schemas.schemas import (
    AddressSnapshot,
    BookingAddressUpdate,
    BookingCancelRequest,
    BookingCreate,
    BookingOut,
    EscalationCreateRequest,
    SOSCreateRequest,
)
from app.core.timeutil import booking_end_utc, booking_start_utc, is_booking_expired, is_slot_expired
from app.services import catalog_guard
from app.services.clinical_engine import compute_sla_breach, get_escalation_metadata
from app.services.common_services import audit, notify_admins, notify_parties, send_notification
from app.websockets.manager import booking_topic, manager, user_topic

router = APIRouter(prefix="/bookings", tags=["bookings"])

_TERMINAL = (BookingStatus.completed, BookingStatus.cancelled, BookingStatus.missed, BookingStatus.disputed)
_ACTIVE_NOW = (BookingStatus.worker_en_route, BookingStatus.worker_arrived, BookingStatus.in_progress)


def _time_bucket(b: Booking, now: Optional[datetime] = None) -> str:
    """See core.timeutil.time_bucket (server-side truth for the app tabs)."""
    from app.core.timeutil import time_bucket
    return time_bucket(b, now)


def _annotate_time(bm: BookingOut, b: Booking, now: Optional[datetime] = None) -> BookingOut:
    now = now or datetime.now(timezone.utc)
    bucket = _time_bucket(b, now)
    bm.time_bucket = bucket
    bm.is_expired = bool(b.status not in _TERMINAL and b.status not in _ACTIVE_NOW and is_booking_expired(b, now=now))
    return bm


def _offering_name(b: Booking, svc, pkg) -> Optional[str]:
    """Name of what the customer actually bought: the package wins."""
    if b.package_id and pkg is not None:
        return pkg.name
    if b.service_id and svc is not None:
        return svc.name
    return None


def _gen_booking_ref() -> str:
    return f"NC{datetime.now().strftime('%y%m%d')}{uuid4().hex[:6].upper()}"


async def _resolve_service_address(
    db: AsyncSession,
    profile: ConsumerProfile,
    *,
    address_id: Optional[UUID],
    address: Optional[AddressSnapshot],
    latitude: Optional[Decimal],
    longitude: Optional[Decimal],
) -> tuple[dict, Decimal, Decimal]:
    """Resolve the patient service location into a booking-safe snapshot."""
    from app.models.models import ConsumerAddress

    resolved_snapshot = None
    resolved_lat = latitude
    resolved_lng = longitude
    if address_id:
        ares = await db.execute(
            select(ConsumerAddress).where(
                ConsumerAddress.id == address_id,
                ConsumerAddress.consumer_id == profile.id,
            )
        )
        addr = ares.scalar_one_or_none()
        if not addr:
            raise HTTPException(status_code=404, detail="Address not found")
        resolved_snapshot = {
            "line1": addr.line1, "line2": addr.line2, "city": addr.city,
            "state": addr.state, "pincode": addr.pincode, "landmark": addr.landmark,
            "recipient_name": addr.recipient_name, "recipient_phone": addr.recipient_phone,
        }
        resolved_lat = addr.latitude
        resolved_lng = addr.longitude
    elif address is not None:
        resolved_snapshot = address.model_dump()

    if resolved_snapshot is None or resolved_lat is None or resolved_lng is None:
        raise HTTPException(status_code=400, detail="Provide address_id or address + latitude/longitude")

    return resolved_snapshot, resolved_lat, resolved_lng


_IST = timezone(timedelta(hours=5, minutes=30))
_MAX_ADVANCE_DAYS = 365


def _validate_schedule(scheduled_date, scheduled_start_time) -> None:
    """Edge case: bookings in the past (or absurdly far ahead) were accepted.

    Compared in IST because that's what the patient picked on screen; the
    server clock is UTC, so a naive date.today() is wrong for 5.5h a day.
    """
    # CI/test runs set ENFORCE_BOOKING_SCHEDULE_LIMITS=false (tests book
    # far-future slots on purpose). Production keeps the default True.
    from app.core.config import settings
    if not settings.ENFORCE_BOOKING_SCHEDULE_LIMITS:
        return

    now_ist = datetime.now(_IST)
    today = now_ist.date()
    if scheduled_date < today:
        raise HTTPException(
            status_code=422,
            detail={"code": "SCHEDULE_IN_PAST", "message": "Please choose today or a future date."},
        )
    if scheduled_date == today and scheduled_start_time is not None:
        start = datetime.combine(scheduled_date, scheduled_start_time).replace(tzinfo=_IST)
        # 5-minute grace for clock skew / slow form submission.
        if start < now_ist - timedelta(minutes=5):
            raise HTTPException(
                status_code=422,
                detail={"code": "SCHEDULE_IN_PAST", "message": "That time has already passed. Please pick a later slot."},
            )
    if scheduled_date > today + timedelta(days=_MAX_ADVANCE_DAYS):
        raise HTTPException(
            status_code=422,
            detail={"code": "SCHEDULE_TOO_FAR", "message": f"Bookings can be made up to {_MAX_ADVANCE_DAYS} days in advance."},
        )


@router.post("/", response_model=BookingOut)
async def create_booking(
    payload: BookingCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    if not payload.service_id and not payload.package_id:
        raise HTTPException(status_code=400, detail="Either service_id or package_id required")
    _validate_schedule(payload.scheduled_date, payload.scheduled_start_time)

    # Verify patient belongs to consumer
    pres = await db.execute(select(Patient).where(Patient.id == payload.patient_id, Patient.consumer_id == profile.id))
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    resolved_snapshot, resolved_lat, resolved_lng = await _resolve_service_address(
        db,
        profile,
        address_id=payload.address_id,
        address=payload.address,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    service: Optional[ServiceCatalogue] = None
    package: Optional[CarePackage] = None
    base_amount = Decimal("0")
    duration = 60

    if payload.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == payload.service_id, ServiceCatalogue.is_active.is_(True)))
        service = sres.scalar_one_or_none()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        _reason = catalog_guard.unbookable_reason(service)
        if _reason:
            raise HTTPException(
                status_code=409,
                detail={"code": _reason, "message": "This service is not available for booking."},
            )
        base_amount = service.base_price
        duration = service.duration_minutes

    if payload.package_id:
        kres = await db.execute(select(CarePackage).where(CarePackage.id == payload.package_id, CarePackage.is_active.is_(True)))
        package = kres.scalar_one_or_none()
        if not package:
            raise HTTPException(status_code=404, detail="Care package not found")
        _psvc = None
        if package.primary_service_id:
            _pr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == package.primary_service_id))
            _psvc = _pr.scalar_one_or_none()
        _reason = catalog_guard.unbookable_reason(package, fallback_items=(_psvc,))
        if _reason:
            raise HTTPException(
                status_code=409,
                detail={"code": _reason, "message": "This care package is not available for booking."},
            )
        if package.available_cities:
            _city = (resolved_snapshot or {}).get("city") if isinstance(resolved_snapshot, dict) else None
            if _city and _city not in package.available_cities:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "PACKAGE_NOT_AVAILABLE_IN_CITY", "message": "This package is not available in your city."},
                )
        # per_visit_price and package_price are both nullable — an admin can
        # save a package without ever setting either. That used to silently
        # fall through to a ₹0 booking, which shows up to the consumer as
        # "amount not assigned" and can look like the booking button is
        # broken. Reject it with a clear reason instead of charging nothing.
        if not package.per_visit_price and not package.package_price:
            raise HTTPException(
                status_code=409,
                detail="This care package doesn't have a price set yet. Please contact support.",
            )
        base_amount = package.per_visit_price or package.package_price or Decimal("0")

    if base_amount <= 0:
        raise HTTPException(
            status_code=409,
            detail="This service doesn't have a price configured yet. Please contact support.",
        )

    surge_amount = Decimal("0")
    if payload.is_urgent and service:
        surge_amount = (base_amount * Decimal(service.urgent_surge_pct) / 100)

    # Subsidy
    sub_res = await db.execute(select(SubsidyEligibility).where(SubsidyEligibility.consumer_id == profile.id, SubsidyEligibility.verified.is_(True)))
    subsidy = sub_res.scalar_one_or_none()
    subsidy_amount = Decimal("0")
    if subsidy and subsidy.subsidy_percent > 0:
        subsidy_amount = (base_amount + surge_amount) * subsidy.subsidy_percent / 100
        if subsidy.max_discount_per_booking:
            subsidy_amount = min(subsidy_amount, subsidy.max_discount_per_booking)

    tax_amount = Decimal("0")  # CGST/SGST – left at 0 unless configured
    total = base_amount + surge_amount - subsidy_amount + tax_amount

    booking = Booking(
        booking_ref=_gen_booking_ref(),
        consumer_id=profile.id,
        patient_id=patient.id,
        booking_type=payload.booking_type,
        service_id=payload.service_id,
        package_id=payload.package_id,
        # A booking is NEVER born assigned. `preferred_worker_id` used to be
        # written straight into worker_id, which let any consumer hand-pick a
        # worker and bypass the whole claim path: no accept step, no
        # qualification/opt-in gate, no schedule-conflict check, and no consent
        # from the worker. Assignment only ever happens through
        # POST /bookings/{id}/accept (or an admin rematch).
        worker_id=None,
        status=BookingStatus.pending_payment,
        scheduled_date=payload.scheduled_date,
        scheduled_start_time=payload.scheduled_start_time,
        scheduled_duration_minutes=duration,
        is_urgent=payload.is_urgent,
        address_snapshot=resolved_snapshot,
        latitude=resolved_lat,
        longitude=resolved_lng,
        base_amount=base_amount,
        surge_amount=surge_amount,
        subsidy_amount=subsidy_amount,
        tax_amount=tax_amount,
        total_amount=total,
        special_instructions=payload.special_instructions,
        # ROOT CAUSE (wrong questionnaire): snapshots were taken from the
        # *service* first, but the purchased *package* is what defines the
        # questionnaire. A package booking that also carried a service_id
        # snapshotted the service's templates. Package wins, falling back to
        # the service only for fields the package leaves empty.
        rule_set_id_snapshot=((package.escalation_rule_set_id if package and package.escalation_rule_set_id else None) or (service.escalation_rule_set_id if service else None)),
        checklist_template_id_snapshot=((package.checklist_template_id if package and package.checklist_template_id else None) or (service.checklist_template_id if service else None)),
        documentation_template_id_snapshot=((package.documentation_template_id if package and package.documentation_template_id else None) or (service.documentation_template_id if service else None)),
    )
    db.add(booking)
    await db.flush()
    await audit(db, profile.user_id, "consumer", "booking.create", "booking", booking.id, {"total": str(total)})
    await db.commit()
    await db.refresh(booking)
    return BookingOut.model_validate(booking)


@router.get("/consumer", response_model=List[BookingOut])
async def my_consumer_bookings(
    status: Optional[BookingStatus] = None,
    bucket: Optional[str] = None,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    conds = [Booking.consumer_id == profile.id]
    if status:
        conds.append(Booking.status == status)
    res = await db.execute(select(Booking).where(and_(*conds)).order_by(Booking.scheduled_date.desc(), Booking.scheduled_start_time.desc()))
    items: list[Booking] = list(res.scalars().all())

    # Enrich with patient_name / service_name / worker_name — mirrors the
    # pattern in /worker (my_worker_bookings). Without this, the consumer
    # bookings list/detail pages show a generic "Service" placeholder and a
    # blank nurse field, since BookingOut only carries raw *_id foreign keys.
    patient_cache: dict = {}
    svc_cache: dict = {}
    pkg_cache: dict = {}
    worker_name_cache: dict = {}
    out: list[BookingOut] = []
    for b in items:
        bm = BookingOut.model_validate(b)

        if b.patient_id:
            if b.patient_id not in patient_cache:
                pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
                patient_cache[b.patient_id] = pres.scalar_one_or_none()
            patient = patient_cache[b.patient_id]
            if patient:
                bm.patient_name = patient.full_name

        _svc = _pkg = None
        if b.service_id:
            if b.service_id not in svc_cache:
                sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                svc_cache[b.service_id] = sr.scalar_one_or_none()
            _svc = svc_cache[b.service_id]
        if b.package_id:
            if b.package_id not in pkg_cache:
                pr = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                pkg_cache[b.package_id] = pr.scalar_one_or_none()
            _pkg = pkg_cache[b.package_id]
        _nm = _offering_name(b, _svc, _pkg)
        if _nm:
            bm.service_name = _nm
        _annotate_time(bm, b)

        if b.worker_id:
            if b.worker_id not in worker_name_cache:
                wr = await db.execute(
                    select(User.full_name).join(WorkerProfile, WorkerProfile.user_id == User.id)
                    .where(WorkerProfile.id == b.worker_id)
                )
                worker_name_cache[b.worker_id] = wr.scalar_one_or_none()
            if worker_name_cache[b.worker_id]:
                bm.worker_name = worker_name_cache[b.worker_id]

        out.append(bm)
    if bucket in ("upcoming", "active", "past"):
        out = [o for o in out if o.time_bucket == bucket]
    return out


@router.get("/worker", response_model=List[BookingOut])
async def my_worker_bookings(
    status: Optional[BookingStatus] = None,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    conds = [Booking.worker_id == profile.id]
    if status:
        conds.append(Booking.status == status)
    res = await db.execute(select(Booking).where(and_(*conds)).order_by(Booking.scheduled_date.desc()))
    items: list[Booking] = list(res.scalars().all())

    # Enrich with patient_name / service_name — mirrors the logic in
    # /worker/new-requests. Without this, "My Visits" shows blank names.
    svc_cache: dict = {}
    pkg_cache: dict = {}
    patient_cache: dict = {}
    out: list[BookingOut] = []
    for b in items:
        bm = BookingOut.model_validate(b)

        if b.patient_id:
            if b.patient_id not in patient_cache:
                pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
                patient_cache[b.patient_id] = pres.scalar_one_or_none()
            patient = patient_cache[b.patient_id]
            if patient:
                bm.patient_name = patient.full_name

        _svc = _pkg = None
        if b.service_id:
            if b.service_id not in svc_cache:
                sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                svc_cache[b.service_id] = sr.scalar_one_or_none()
            _svc = svc_cache[b.service_id]
        if b.package_id:
            if b.package_id not in pkg_cache:
                pr = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                pkg_cache[b.package_id] = pr.scalar_one_or_none()
            _pkg = pkg_cache[b.package_id]
        _nm = _offering_name(b, _svc, _pkg)
        if _nm:
            bm.service_name = _nm
        _annotate_time(bm, b)

        out.append(bm)
    return out


# Backward-compatible alias for older frontend bundles that still call
# /api/bookings/available. Keep it before /{booking_id}, otherwise FastAPI
# treats "available" as a UUID path param and returns 422.
@router.get("/available", response_model=List[BookingOut], include_in_schema=False)
@router.get("/worker/new-requests", response_model=List[BookingOut])
async def new_requests(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    """Open bookings this worker may claim right now.

    Uses the SAME rule as the push broadcast and the accept guard
    (``dispatch.evaluate_worker_for_booking``), so what a nurse is pinged about,
    what she sees here, and what she can accept can no longer disagree.

    ROOT CAUSE fixed: the old query took the first 50 open bookings ordered by
    scheduled_date ASC *before* any eligibility/expiry filtering. Past-dated
    and far-away rows filled that window, so an eligible nearby nurse never saw
    newer bookings. Expired slots are now excluded in SQL, ordering is by
    dispatch time, and eligibility runs over a bounded candidate set.
    """
    from app.core.timeutil import IST
    from app.services.dispatch import (
        DISPATCHABLE_STATUSES,
        booking_dispatch_block_reason,
        evaluate_worker_for_booking,
        resolve_target,
    )
    from app.services.proximity import compute_current_wave

    now = datetime.now(timezone.utc)
    # Slots dated before yesterday (IST) can never be open; the exact
    # start+grace check runs in Python below.
    earliest_date = (now.astimezone(IST) - timedelta(days=1)).date()
    res = await db.execute(
        select(Booking)
        .where(
            Booking.worker_id.is_(None),
            Booking.status.in_(DISPATCHABLE_STATUSES),
            Booking.scheduled_date >= earliest_date,
        )
        .order_by(func.coalesce(Booking.dispatch_started_at, Booking.created_at).asc())
        .limit(500)
    )
    items: list[Booking] = list(res.scalars().all())

    visible: list[tuple[Booking, Optional[float], object]] = []
    target_cache: dict = {}
    patient_cache: dict = {}
    wave_dirty = False
    for b in items:
        if booking_dispatch_block_reason(b, now=now) is not None:
            continue
        key = (b.package_id, b.service_id)
        if key not in target_cache:
            target_cache[key] = await resolve_target(db, b)
        target = target_cache[key]

        current_wave = compute_current_wave(b, now=now)
        elig = await evaluate_worker_for_booking(db, profile, b, target, now=now, wave=current_wave)
        if not elig.ok:
            continue
        if current_wave > (b.assignment_wave or 1):
            b.assignment_wave = current_wave
            if current_wave >= 4 and b.assignment_escalated_at is None:
                b.assignment_escalated_at = now
            wave_dirty = True
        visible.append((b, elig.distance_km, target))
        if len(visible) >= 20:
            break

    if wave_dirty:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()

    out: list[BookingOut] = []
    for b, dist, target in visible:
        bm = BookingOut.model_validate(b)
        if b.patient_id:
            if b.patient_id not in patient_cache:
                pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
                patient_cache[b.patient_id] = pres.scalar_one_or_none()
            patient = patient_cache[b.patient_id]
            if patient:
                bm.patient_name = patient.full_name
        if target is not None:
            bm.service_name = target.name
        if dist is not None:
            bm.distance_km = round(dist, 2)
        _annotate_time(bm, b, now)
        out.append(bm)
    return out


@router.put("/{booking_id}/address", response_model=BookingOut)
async def update_booking_address(
    booking_id: UUID,
    payload: BookingAddressUpdate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Let a family correct the patient service location before confirmation."""
    res = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")

    editable_statuses = {BookingStatus.draft, BookingStatus.pending_payment}
    if b.status not in editable_statuses:
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "code": "BOOKING_LOCATION_LOCKED",
                "message": "Location can be changed only before the booking is confirmed.",
            },
        )

    resolved_snapshot, resolved_lat, resolved_lng = await _resolve_service_address(
        db,
        profile,
        address_id=payload.address_id,
        address=payload.address,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    old_snapshot = b.address_snapshot
    old_lat = b.latitude
    old_lng = b.longitude
    b.address_snapshot = resolved_snapshot
    b.latitude = resolved_lat
    b.longitude = resolved_lng
    await audit(
        db,
        profile.user_id,
        "consumer",
        "booking.address_update",
        "booking",
        b.id,
        {
            "old_address": old_snapshot,
            "new_address": resolved_snapshot,
            "old_latitude": str(old_lat) if old_lat is not None else None,
            "old_longitude": str(old_lng) if old_lng is not None else None,
            "new_latitude": str(resolved_lat),
            "new_longitude": str(resolved_lng),
        },
    )
    await db.commit()
    await db.refresh(b)
    await manager.broadcast(booking_topic(b.id), {"type": "booking.address_updated", "booking_id": str(b.id)})
    return BookingOut.model_validate(b)


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    # access control
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one()
        if b.consumer_id != cp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one()
        if b.worker_id != wp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Forbidden")

    bm = BookingOut.model_validate(b)
    _annotate_time(bm, b)

    # Enrich with patient_name / service_name / worker_name — this endpoint
    # backs the consumer/nurse "booking detail" pages, which otherwise show
    # blank or generic placeholder text ("Service", empty nurse field) since
    # BookingOut only carries the raw *_id foreign keys.
    if b.patient_id:
        pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
        patient = pres.scalar_one_or_none()
        if patient:
            bm.patient_name = patient.full_name

    svc = pkg = None
    if b.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
        svc = sres.scalar_one_or_none()
    if b.package_id:
        pkres = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
        pkg = pkres.scalar_one_or_none()
    _nm = _offering_name(b, svc, pkg)
    if _nm:
        bm.service_name = _nm

    if b.worker_id:
        wres2 = await db.execute(
            select(User.full_name).join(WorkerProfile, WorkerProfile.user_id == User.id)
            .where(WorkerProfile.id == b.worker_id)
        )
        worker_name = wres2.scalar_one_or_none()
        if worker_name:
            bm.worker_name = worker_name

    return bm


# ---------------------------------------------------------------------------
# GET /bookings/{booking_id}/history
#
# The consumer/nurse "Booking history" timeline was previously rendered
# entirely from a client-side, in-memory mock store (OrchestrationStore —
# see src/lib/orchestration/index.tsx on the frontend) that seeds one
# generic "Imported from operational seed" event per entity on page load
# and is never wired to real backend data. That's why every booking showed
# the same static/duplicated write-up regardless of what actually happened.
#
# This endpoint returns the real event trail from AuditLog for this
# booking, so the frontend can render an accurate, per-booking timeline.
# ---------------------------------------------------------------------------
@router.get("/{booking_id}/history")
async def get_booking_history(
    booking_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    # Same ownership rules as GET /bookings/{booking_id}.
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one()
        if b.consumer_id != cp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one()
        if b.worker_id != wp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Booking-level events (create, accept, cancel, checklist/documentation
    # submissions, OTP generation, etc.) are logged with entity_type="booking"
    # and entity_id=str(booking_id). Visit-scoped events (check-in, checkout,
    # vitals) are logged against the VisitRecord id instead, so pull those in
    # too via the linked visit record, when one exists.
    entity_ids = [str(booking_id)]
    vres = await db.execute(select(VisitRecord.id).where(VisitRecord.booking_id == booking_id))
    visit_id = vres.scalar_one_or_none()
    if visit_id:
        entity_ids.append(str(visit_id))

    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type.in_(["booking", "visit"]), AuditLog.entity_id.in_(entity_ids))
        .order_by(AuditLog.created_at.asc())
    )
    return [
        {
            "id": str(r.id),
            "action": r.action,
            "actor_type": r.actor_type,
            "changes": r.changes,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows.scalars().all()
    ]


@router.post("/{booking_id}/accept")
async def accept_booking(

    booking_id: UUID,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Concurrency-safe worker claim of an open booking.

    Uses an atomic conditional UPDATE so that exactly one worker can win the
    race. The DB decides the winner; the API never optimistically assigns.

    Returns 200 with the booking row on success (idempotent for the winning
    worker), 409 for already-claimed by someone else, 410 for not-claimable,
    404 if missing.
    """
    worker_id = profile.id
    now = datetime.now(timezone.utc)
    # searching_nurse == Workflow 1 (Composite Care Package) dispatch state,
    # entered once the pharmacist has approved the Rx — claimable exactly
    # like a normal confirmed booking.
    claimable_statuses = (BookingStatus.confirmed, BookingStatus.rematch_pending, BookingStatus.searching_nurse)

    # Patch 2 — qualification + opt-in re-check before claim. Fetch booking
    # (without locking it) to identify the target service/package.
    pre_res = await db.execute(select(Booking).where(Booking.id == booking_id))
    pre_b = pre_res.scalar_one_or_none()
    if not pre_b:
        raise HTTPException(status_code=404, detail="Booking not found")

    from app.services.dispatch import resolve_target as _resolve_target
    # Package-first: the purchased package defines eligibility, not a
    # service_id that may also be stored on the row.
    target = await _resolve_target(db, pre_b)

    if target is not None:
        from app.services.qualification import (
            is_worker_opted_in_for_service,
            is_worker_qualified_for_service,
        )
        qualified, locked_reason = await is_worker_qualified_for_service(profile, target, db)
        if not qualified:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "code": "WORKER_NOT_QUALIFIED_FOR_SERVICE",
                    "message": "You are not qualified for this service.",
                    "locked_reason": locked_reason,
                },
            )
        opted_in = await is_worker_opted_in_for_service(profile, target, db)
        if not opted_in:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "code": "WORKER_NOT_OPTED_IN_FOR_SERVICE",
                    "message": "You have not opted in to receive this service.",
                },
            )

    # Everything below (open/expired check, eligibility, schedule-conflict and
    # the claim UPDATE) runs under one per-worker advisory lock, so two
    # concurrent accepts of overlapping bookings by the SAME nurse cannot both
    # pass the conflict check.
    from app.services.dispatch import (
        booking_dispatch_block_reason,
        evaluate_worker_for_booking,
        lock_worker_schedule,
    )
    await lock_worker_schedule(db, worker_id)

    # Re-read after taking the lock so we judge current state.
    await db.refresh(pre_b)
    block = booking_dispatch_block_reason(pre_b, now=now)
    if block == "SLOT_EXPIRED":
        await db.rollback()
        return JSONResponse(
            status_code=410,
            content={"success": False, "code": "BOOKING_SLOT_EXPIRED",
                     "message": "This booking's time slot has already passed."},
        )

    # Same eligibility rule as the push and the pull list (approval,
    # availability, qualification/opt-in, radius for the current wave).
    # A worker who is already the winner is let through to the idempotent
    # branch below.
    already_mine = pre_b.worker_id == worker_id and pre_b.status == BookingStatus.assigned
    if not already_mine and pre_b.worker_id is None:
        elig = await evaluate_worker_for_booking(db, profile, pre_b, target, now=now)
        if not elig.ok:
            await db.rollback()
            if elig.reason == "SCHEDULE_CONFLICT":
                return JSONResponse(
                    status_code=409,
                    content={"success": False, "code": "WORKER_SCHEDULE_CONFLICT",
                             "message": "You already have a visit booked at this time."},
                )
            return JSONResponse(
                status_code=403,
                content={"success": False, "code": "WORKER_NOT_ELIGIBLE_FOR_BOOKING",
                         "reason": elig.reason,
                         "message": "This booking isn't available to you."},
            )

    # Atomic conditional update — only succeeds if booking is still open and
    # unclaimed. The DB enforces a single winner.
    upd = (
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.worker_id.is_(None),
            Booking.status.in_(claimable_statuses),
        )
        .values(worker_id=worker_id, status=BookingStatus.assigned, accepted_at=now)
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(upd)

    if result.rowcount == 1:
        # Winner — fetch the row and ensure a VisitRecord exists (unique
        # constraint on visit_records.booking_id makes this safe under races).
        bres = await db.execute(select(Booking).where(Booking.id == booking_id))
        b = bres.scalar_one()
        vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == b.id))
        visit = vres.scalar_one_or_none()
        if not visit:
            # ROOT CAUSE: the IntegrityError handler used db.rollback(), which
            # rolls back the WHOLE transaction — including the claim UPDATE
            # above — so a race on visit creation silently un-assigned the
            # booking while still returning 200. A SAVEPOINT scopes the
            # rollback to just the duplicate INSERT.
            try:
                async with db.begin_nested():
                    db.add(VisitRecord(
                        booking_id=b.id,
                        worker_id=worker_id,
                        patient_id=b.patient_id,
                        status=VisitStatus.scheduled,
                    ))
                    await db.flush()
            except IntegrityError:
                pass  # a concurrent transaction created the visit first
        await audit(db, profile.user_id, "worker", "booking.accept", "booking", b.id)
        await db.commit()
        await db.refresh(b)

        # Notify consumer (best-effort; failures must not affect claim outcome).
        try:
            cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.id == b.consumer_id))
            cp = cres.scalar_one()
            await send_notification(
                db, cp.user_id, "booking_accepted", "Nurse Confirmed",
                f"A nurse has accepted your booking {b.booking_ref}.",
                {"booking_id": str(b.id)},
            )
            await db.commit()
            await manager.broadcast(
                booking_topic(b.id),
                {"type": "booking.accepted", "booking_id": str(b.id), "worker_id": str(worker_id)},
            )
        except Exception:  # noqa: BLE001
            await db.rollback()

        # Generate the visit-start OTP right away so it's already sitting on
        # the consumer's booking card by the time they open the app — no
        # separate "send code" step needed. Best-effort; a failure here must
        # never undo the booking claim itself.
        try:
            from app.api.v1.visits import _ensure_visit_start_otp
            await _ensure_visit_start_otp(db, b)
        except Exception:  # noqa: BLE001
            await db.rollback()

        return JSONResponse(status_code=200, content=BookingOut.model_validate(b).model_dump(mode="json"))

    # rowcount == 0 — determine why and respond with structured error.
    await db.rollback()
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = bres.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Idempotent: same worker retrying after already winning.
    if b.worker_id == worker_id and b.status == BookingStatus.assigned:
        return JSONResponse(status_code=200, content=BookingOut.model_validate(b).model_dump(mode="json"))

    # Different worker already won the race.
    if b.worker_id is not None and b.worker_id != worker_id:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "BOOKING_ALREADY_CLAIMED",
                "message": "This booking has already been claimed by another care professional.",
            },
        )

    # Booking exists, unclaimed, but no longer in a claimable status
    # (cancelled / completed / in_progress / etc.).
    return JSONResponse(
        status_code=410,
        content={
            "success": False,
            "code": "BOOKING_NOT_AVAILABLE",
            "message": "This request is no longer available.",
        },
    )


# ============================================================================
# "En Route" — gated behind the Nurse Safety Check (reaction test + fitness
# declaration). The nurse completes both on one combined screen right when
# she taps this button; the frontend calls POST /workers/me/alertness-checks
# first, then this endpoint. This endpoint independently re-checks that a
# passing, declaration-confirmed attempt exists for this booking, so the
# gate can't be bypassed by skipping the app-side flow.
# ============================================================================
_SAFETY_CHECK_MAX_AGE_MINUTES = 15


@router.post("/{booking_id}/en-route", response_model=BookingOut)
async def mark_worker_en_route(
    booking_id: UUID,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    from app.models.models import WorkerAlertnessCheck
    from app.models.enums import AlertnessTier, WorkerAvailability

    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = bres.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b.worker_id != profile.id:
        raise HTTPException(status_code=403, detail="Not assigned to this booking")

    # Idempotent — nurse re-tapping after already succeeding.
    if b.status == BookingStatus.worker_en_route:
        return JSONResponse(status_code=200, content=BookingOut.model_validate(b).model_dump(mode="json"))

    if b.status != BookingStatus.assigned:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "BOOKING_NOT_IN_ASSIGNED_STATE",
                "message": "This booking can't be started right now.",
            },
        )

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_SAFETY_CHECK_MAX_AGE_MINUTES)
    cres = await db.execute(
        select(WorkerAlertnessCheck)
        .where(
            WorkerAlertnessCheck.booking_id == booking_id,
            WorkerAlertnessCheck.worker_id == profile.id,
            WorkerAlertnessCheck.created_at >= cutoff,
        )
        .order_by(WorkerAlertnessCheck.created_at.desc())
        .limit(1)
    )
    check = cres.scalar_one_or_none()

    if not check or not check.declaration_confirmed:
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "code": "SAFETY_CHECK_REQUIRED",
                "message": "Complete the reaction test and fitness declaration before heading out.",
            },
        )

    if check.tier == AlertnessTier.fail:
        # Lock the shift and hand the booking back to dispatch for the
        # nearest standby nurse, per the product spec's decision table.
        b.status = BookingStatus.rematch_pending
        b.worker_id = None
        profile.availability = WorkerAvailability.on_leave
        await audit(
            db, profile.user_id, "worker", "booking.safety_check_failed", "booking", b.id,
            {"tier": check.tier.value, "average_reaction_time_ms": check.average_reaction_time_ms},
        )
        await db.commit()
        try:
            from app.services.dispatch import notify_nearby_workers
            b.dispatch_started_at = datetime.now(timezone.utc)
            b.assignment_wave = 1
            await db.commit()
            await notify_nearby_workers(db, b, new_cycle=True)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
        await manager.broadcast(booking_topic(b.id), {"type": "booking.rematch", "booking_id": str(b.id)})
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "code": "SAFETY_CHECK_FAILED",
                "message": "You seem very fatigued — this booking has been reassigned so you can rest. Please take a break before your next visit.",
            },
        )

    if check.tier == AlertnessTier.warning:
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "code": "SAFETY_CHECK_WARNING",
                "message": "Your reaction time is a little slow. Take a 5-second breather and try the check once more.",
            },
        )

    # PASS — unlock navigation and start the journey.
    now = datetime.now(timezone.utc)
    b.status = BookingStatus.worker_en_route
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == b.id))
    visit = vres.scalar_one_or_none()
    if visit:
        visit.status = VisitStatus.en_route
        visit.en_route_at = now
    await audit(db, profile.user_id, "worker", "booking.en_route", "booking", b.id)
    await db.commit()
    await db.refresh(b)
    await manager.broadcast(booking_topic(b.id), {"type": "booking.en_route", "booking_id": str(b.id)})
    return JSONResponse(status_code=200, content=BookingOut.model_validate(b).model_dump(mode="json"))


# Neither the nurse nor the customer may cancel inside this window before
# the scheduled visit start. Admin/ops can always cancel (support cases).
_CANCELLATION_CUTOFF_HOURS = 6


def _scheduled_start_utc(b: Booking) -> datetime:
    # scheduled_date + scheduled_start_time are IST wall-clock (see core.timeutil).
    return booking_start_utc(b)


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: UUID,
    payload: BookingCancelRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b.status in (BookingStatus.completed, BookingStatus.cancelled, BookingStatus.in_progress):
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking in {b.status.value}")

    # access
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one()
        if b.consumer_id != cp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one()
        if b.worker_id != wp.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Forbidden")

    # 6-hour cutoff — applies to nurse and customer alike; admin is exempt
    # so support can still intervene on emergencies.
    if not is_admin(current.role):
        now = datetime.now(timezone.utc)
        cutoff = _scheduled_start_utc(b) - timedelta(hours=_CANCELLATION_CUTOFF_HOURS)
        if now > cutoff:
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "code": "CANCELLATION_WINDOW_CLOSED",
                    "message": (
                        f"Cancellations are only allowed up to {_CANCELLATION_CUTOFF_HOURS} hours "
                        "before the scheduled visit. Please contact support for help."
                    ),
                },
            )

    # A nurse backing out does NOT kill the booking — it goes straight back
    # into the dispatch pool for other qualified nurses (rematch_pending is
    # already included in /worker/new-requests), with the wave clock reset
    # so proximity waves start over from the rematch moment.
    if current.role == UserRole.worker:
        released_worker_id = b.worker_id
        b.worker_id = None
        b.status = BookingStatus.rematch_pending
        b.accepted_at = None
        b.rematch_count = (b.rematch_count or 0) + 1
        b.assignment_wave = 1
        b.assignment_escalated_at = None
        b.dispatch_started_at = datetime.now(timezone.utc)
        await audit(
            db, current.id, current.role.value, "booking.worker_cancel_rematch", "booking", b.id,
            {"reason": payload.reason, "released_worker_id": str(released_worker_id), "rematch_count": b.rematch_count},
        )
        await db.commit()
        await db.refresh(b)

        # Best-effort: tell the customer we're finding a replacement, and
        # push the request to other nearby qualified nurses right away.
        try:
            cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.id == b.consumer_id))
            cp = cres.scalar_one_or_none()
            if cp:
                await send_notification(
                    db, cp.user_id, "booking_rematch", "Finding You a New Nurse",
                    f"Your nurse had to cancel booking {b.booking_ref}. "
                    "We're automatically matching you with another verified nurse.",
                    {"booking_id": str(b.id)},
                )
            from app.services.dispatch import notify_nearby_workers
            await notify_nearby_workers(db, b, new_cycle=True)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
        await manager.broadcast(booking_topic(b.id), {"type": "booking.rematch", "booking_id": str(b.id)})
        return BookingOut.model_validate(b)

    # Consumer / admin cancellation — terminal.
    b.status = BookingStatus.cancelled
    b.cancelled_by = current.id
    b.cancelled_at = datetime.now(timezone.utc)
    b.cancellation_reason = payload.reason
    await audit(db, current.id, current.role.value, "booking.cancel", "booking", b.id, {"reason": payload.reason})
    await db.commit()
    await db.refresh(b)
    await manager.broadcast(booking_topic(b.id), {"type": "booking.cancelled", "booking_id": str(b.id)})
    return BookingOut.model_validate(b)


@router.post("/{booking_id}/escalate")
async def escalate_booking(
    booking_id: UUID,
    payload: EscalationCreateRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == booking_id, Booking.worker_id == profile.id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found or not assigned to you")

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == b.id))
    visit = vres.scalar_one_or_none()

    # Resolve rule set
    from app.models.models import ClinicalRuleSet
    rule_set = None
    if b.rule_set_id_snapshot:
        rres = await db.execute(select(ClinicalRuleSet).where(ClinicalRuleSet.id == b.rule_set_id_snapshot))
        rule_set = rres.scalar_one_or_none()
    meta = get_escalation_metadata(rule_set, payload.level.value) if rule_set else {"notify": ["ops"], "sla_minutes": 30, "auto_call_112": payload.level == EscalationLevel.emergency}

    esc = Escalation(
        booking_id=b.id,
        visit_record_id=visit.id if visit else None,
        worker_id=profile.id,
        patient_id=b.patient_id,
        level=payload.level,
        status=EscalationStatus.open,
        trigger_type=payload.trigger_type,
        trigger_details=payload.trigger_details,
        notes=payload.notes,
        notified_parties=meta.get("notify"),
        sla_minutes=meta.get("sla_minutes"),
        sla_breach_at=compute_sla_breach(meta.get("sla_minutes")),
        auto_call_112=bool(meta.get("auto_call_112")),
        rule_set_id=rule_set.id if rule_set else None,
        rule_set_version=rule_set.version if rule_set else None,
    )
    db.add(esc)
    if visit:
        visit.escalation_triggered = True
    await audit(db, profile.user_id, "worker", "escalation.create", "escalation", esc.id, {"level": payload.level.value})
    await db.commit()
    await db.refresh(esc)
    # Notify parties
    await notify_parties(
        db,
        meta.get("notify", []),
        {"booking_id": str(b.id), "escalation_id": str(esc.id)},
        template_code="escalation_alert",
        title=f"Escalation: {payload.level.value}",
        body=payload.notes,
    )
    await db.commit()
    await manager.broadcast(booking_topic(b.id), {"type": "escalation.created", "level": payload.level.value, "escalation_id": str(esc.id)})
    return {"id": str(esc.id), "level": esc.level.value, "status": esc.status.value, "sla_breach_at": esc.sla_breach_at.isoformat() if esc.sla_breach_at else None}


@router.post("/{booking_id}/sos")
async def trigger_sos(
    booking_id: UUID,
    payload: SOSCreateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Personal-safety panic button.

    Either the consumer or the assigned worker on a booking can fire this
    when they feel unsafe — e.g. a worker fears the customer, or a customer
    fears the worker. Unlike /escalate (clinical, worker-only), this is
    identity-agnostic and always treated as the highest severity: it opens
    an `emergency` Escalation with a short SLA and auto_call_112 set, and
    alerts admins immediately (in-app + a live WebSocket push), without
    ever notifying the other party on the booking — the point is to get the
    person who's scared to safety quietly, not to alert who they're scared of.
    """
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Caller must be a party to this booking (the consumer or the assigned
    # worker) — admins can also trigger it on someone's behalf if needed.
    triggered_by_role: str
    if current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one_or_none()
        if not cp or cp.id != b.consumer_id:
            raise HTTPException(status_code=403, detail="Not a party to this booking")
        triggered_by_role = "consumer"
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one_or_none()
        if not wp or wp.id != b.worker_id:
            raise HTTPException(status_code=403, detail="Not a party to this booking")
        triggered_by_role = "worker"
    elif is_admin(current.role):
        triggered_by_role = "admin"
    else:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not b.worker_id:
        raise HTTPException(status_code=400, detail="No nurse assigned to this booking yet")

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == b.id))
    visit = vres.scalar_one_or_none()

    esc = Escalation(
        booking_id=b.id,
        visit_record_id=visit.id if visit else None,
        worker_id=b.worker_id,
        patient_id=b.patient_id,
        level=EscalationLevel.emergency,
        status=EscalationStatus.open,
        trigger_type="safety_sos",
        trigger_details={
            "triggered_by_role": triggered_by_role,
            "triggered_by_user_id": str(current.id),
            "latitude": payload.latitude,
            "longitude": payload.longitude,
        },
        notes=payload.notes or f"Safety SOS raised by {triggered_by_role}.",
        notified_parties=["ops", "admin"],
        sla_minutes=5,
        sla_breach_at=compute_sla_breach(5),
        auto_call_112=True,
        priority="critical",
    )
    db.add(esc)
    if visit:
        visit.escalation_triggered = True
    await audit(
        db, current.id, current.role.value, "escalation.sos", "escalation", esc.id,
        {"booking_id": str(b.id), "triggered_by_role": triggered_by_role},
    )
    await db.commit()
    await db.refresh(esc)

    # Notify admins — in-app/push, AND a live WebSocket push to each admin's
    # existing /ws/user connection so it lands instantly rather than waiting
    # on the support dashboard's poll interval.
    admin_ids = await notify_admins(
        db,
        template_code="sos_alert",
        title="🆘 Safety SOS triggered",
        body=payload.notes or f"A {triggered_by_role} raised a safety SOS on booking {b.booking_ref}.",
        context={"booking_id": str(b.id), "escalation_id": str(esc.id)},
    )
    await db.commit()

    sos_event = {
        "type": "sos.alert",
        "escalation_id": str(esc.id),
        "booking_id": str(b.id),
        "booking_ref": b.booking_ref,
        "triggered_by_role": triggered_by_role,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "notes": esc.notes,
        "created_at": esc.created_at.isoformat() if esc.created_at else None,
    }
    for admin_id in admin_ids:
        await manager.broadcast(user_topic(admin_id), sos_event)
    # Also drop it on the booking's own channel in case anyone (e.g. an
    # admin already viewing that specific booking) is subscribed there.
    await manager.broadcast(booking_topic(b.id), {"type": "escalation.created", "level": "emergency", "escalation_id": str(esc.id)})

    return {
        "id": str(esc.id),
        "level": esc.level.value,
        "status": esc.status.value,
        "auto_call_112": esc.auto_call_112,
        "sla_breach_at": esc.sla_breach_at.isoformat() if esc.sla_breach_at else None,
    }