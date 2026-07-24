"""Booking lifecycle: create, accept, cancel, list, escalate."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
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
    BookingCancelRequest,
    BookingCreate,
    BookingOut,
    EscalationCreateRequest,
)
from app.services.clinical_engine import compute_sla_breach, get_escalation_metadata
from app.services.common_services import audit, notify_parties, send_notification
from app.websockets.manager import booking_topic, manager

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _gen_booking_ref() -> str:
    return f"NC{datetime.now().strftime('%y%m%d')}{uuid4().hex[:6].upper()}"


@router.post("/", response_model=BookingOut)
async def create_booking(
    payload: BookingCreate,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    if not payload.service_id and not payload.package_id:
        raise HTTPException(status_code=400, detail="Either service_id or package_id required")

    # Verify patient belongs to consumer
    pres = await db.execute(select(Patient).where(Patient.id == payload.patient_id, Patient.consumer_id == profile.id))
    patient = pres.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    # Resolve the service address: prefer a saved address_id, else inline fields.
    from app.models.models import ConsumerAddress
    resolved_snapshot = None
    resolved_lat = payload.latitude
    resolved_lng = payload.longitude
    if payload.address_id:
        ares = await db.execute(
            select(ConsumerAddress).where(
                ConsumerAddress.id == payload.address_id,
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
    elif payload.address is not None:
        resolved_snapshot = payload.address.model_dump()
    if resolved_snapshot is None or resolved_lat is None or resolved_lng is None:
        raise HTTPException(status_code=400, detail="Provide address_id or address + latitude/longitude")

    service: Optional[ServiceCatalogue] = None
    package: Optional[CarePackage] = None
    base_amount = Decimal("0")
    duration = 60

    if payload.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == payload.service_id, ServiceCatalogue.is_active.is_(True)))
        service = sres.scalar_one_or_none()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        base_amount = service.base_price
        duration = service.duration_minutes

    if payload.package_id:
        kres = await db.execute(select(CarePackage).where(CarePackage.id == payload.package_id, CarePackage.is_active.is_(True)))
        package = kres.scalar_one_or_none()
        if not package:
            raise HTTPException(status_code=404, detail="Care package not found")
        base_amount = package.per_visit_price or package.package_price or Decimal("0")

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
        worker_id=payload.preferred_worker_id,
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
        rule_set_id_snapshot=(service.escalation_rule_set_id if service else (package.escalation_rule_set_id if package else None)),
        checklist_template_id_snapshot=(service.checklist_template_id if service else (package.checklist_template_id if package else None)),
        documentation_template_id_snapshot=(service.documentation_template_id if service else (package.documentation_template_id if package else None)),
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

        if b.service_id:
            if b.service_id not in svc_cache:
                sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                svc_cache[b.service_id] = sr.scalar_one_or_none()
            if svc_cache[b.service_id]:
                bm.service_name = svc_cache[b.service_id].name
        elif b.package_id:
            if b.package_id not in pkg_cache:
                pr = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                pkg_cache[b.package_id] = pr.scalar_one_or_none()
            if pkg_cache[b.package_id]:
                bm.service_name = pkg_cache[b.package_id].name

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

        if b.service_id:
            if b.service_id not in svc_cache:
                sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                svc_cache[b.service_id] = sr.scalar_one_or_none()
            if svc_cache[b.service_id]:
                bm.service_name = svc_cache[b.service_id].name
        elif b.package_id:
            if b.package_id not in pkg_cache:
                pr = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                pkg_cache[b.package_id] = pr.scalar_one_or_none()
            if pkg_cache[b.package_id]:
                bm.service_name = pkg_cache[b.package_id].name

        out.append(bm)
    return out


# Backward-compatible alias for older frontend bundles that still call
# /api/bookings/available. Keep it before /{booking_id}, otherwise FastAPI
# treats "available" as a UUID path param and returns 422.
@router.get("/available", response_model=List[BookingOut], include_in_schema=False)
@router.get("/worker/new-requests", response_model=List[BookingOut])
async def new_requests(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    """Unassigned bookings the worker is qualified for, opted into, AND inside
    the current radius wave.

    Patch 2 filters (qualification + opt-in) run BEFORE Patch 3 proximity
    filtering, so we don't pay the Haversine cost for ineligible rows. Wave
    progression is computed opportunistically per booking based on elapsed
    time since ``booking.created_at`` — no scheduler required.
    """
    from app.services.proximity import (
        compute_current_wave,
        effective_origin_for_worker,
        haversine_km,
        radius_for_wave,
    )
    from app.services.qualification import can_worker_receive_service
    from app.services.dispatch import worker_has_schedule_conflict
    res = await db.execute(
        select(Booking).where(
            Booking.worker_id.is_(None),
            Booking.status.in_([BookingStatus.confirmed, BookingStatus.rematch_pending]),
        ).order_by(Booking.scheduled_date.asc()).limit(50)
    )
    items: list[Booking] = list(res.scalars().all())

    # Patch 3 — compute worker effective origin once. ``None`` means we have
    # neither a fresh current location nor a home location on file.
    worker_origin = effective_origin_for_worker(profile)

    visible: list[tuple[Booking, Optional[float]]] = []
    svc_cache: dict = {}
    pkg_cache: dict = {}
    patient_cache: dict = {}
    now = datetime.now(timezone.utc)
    wave_dirty: list[Booking] = []
    for b in items:
        # ----- Patch 2 filters first (cheap) ---------------------------------
        target = None
        if b.service_id:
            if b.service_id in svc_cache:
                target = svc_cache[b.service_id]
            else:
                sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                target = sr.scalar_one_or_none()
                svc_cache[b.service_id] = target
        elif b.package_id:
            if b.package_id in pkg_cache:
                target = pkg_cache[b.package_id]
            else:
                pr = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                target = pr.scalar_one_or_none()
                pkg_cache[b.package_id] = target
        if not target:
            continue
        allowed, _ = await can_worker_receive_service(profile, target, db)
        if not allowed:
            continue

        # ----- Patch 3 — opportunistic wave progression ---------------------
        current_wave = compute_current_wave(b, now=now)
        if current_wave > (b.assignment_wave or 1):
            b.assignment_wave = current_wave
            if current_wave >= 4 and b.assignment_escalated_at is None:
                b.assignment_escalated_at = now
            wave_dirty.append(b)

        # ----- Patch 3 — proximity / wave radius filter ----------------------
        radius_km = radius_for_wave(b.assignment_wave or 1, b.is_urgent)
        distance_km: Optional[float] = None
        if radius_km is None:
            # Past last wave → escalated. Do not show to workers; admin handles.
            continue

        booking_has_coords = b.latitude is not None and b.longitude is not None
        if booking_has_coords and worker_origin is not None:
            distance_km = haversine_km(
                worker_origin[0], worker_origin[1], b.latitude, b.longitude
            )
            if distance_km > radius_km:
                continue
        elif booking_has_coords and worker_origin is None:
            # Worker has no fresh-or-home coordinates: per spec, urgent jobs
            # must NOT be shown. For normal jobs we fall back to city match.
            if b.is_urgent:
                continue
            addr_city = (b.address_snapshot or {}).get("city") if isinstance(b.address_snapshot, dict) else None
            if profile.base_city and addr_city and profile.base_city != addr_city:
                continue
        # If booking has no lat/lng we fall back to city/zone match as well.
        elif not booking_has_coords:
            addr_city = (b.address_snapshot or {}).get("city") if isinstance(b.address_snapshot, dict) else None
            if profile.base_city and addr_city and profile.base_city != addr_city:
                continue
            if b.is_urgent and worker_origin is None:
                continue

        # Only surface bookings the worker is actually free for.
        if await worker_has_schedule_conflict(db, profile.id, b):
            continue

        visible.append((b, distance_km))
        if len(visible) >= 20:
            break

    # Persist wave bumps in a single commit so the next call doesn't redo work.
    if wave_dirty:
        try:
            await db.commit()
        except Exception:
            await db.rollback()

    out: list[BookingOut] = []
    for b, dist in visible:
        bm = BookingOut.model_validate(b)

        if b.patient_id:
            if b.patient_id not in patient_cache:
                pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
                patient_cache[b.patient_id] = pres.scalar_one_or_none()
            patient = patient_cache[b.patient_id]
            if patient:
                bm.patient_name = patient.full_name

        if b.service_id and b.service_id in svc_cache and svc_cache[b.service_id]:
            bm.service_name = svc_cache[b.service_id].name
        elif b.package_id and b.package_id in pkg_cache and pkg_cache[b.package_id]:
            bm.service_name = pkg_cache[b.package_id].name

        if dist is not None:
            bm.distance_km = round(dist, 2)
        out.append(bm)
    return out


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

    # Enrich with patient_name / service_name / worker_name — this endpoint
    # backs the consumer/nurse "booking detail" pages, which otherwise show
    # blank or generic placeholder text ("Service", empty nurse field) since
    # BookingOut only carries the raw *_id foreign keys.
    if b.patient_id:
        pres = await db.execute(select(Patient).where(Patient.id == b.patient_id))
        patient = pres.scalar_one_or_none()
        if patient:
            bm.patient_name = patient.full_name

    if b.service_id:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
        svc = sres.scalar_one_or_none()
        if svc:
            bm.service_name = svc.name
    elif b.package_id:
        pkres = await db.execute(select(CarePackage).where(CarePackage.id == b.package_id))
        pkg = pkres.scalar_one_or_none()
        if pkg:
            bm.service_name = pkg.name

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
    claimable_statuses = (BookingStatus.confirmed, BookingStatus.rematch_pending)

    # Patch 2 — qualification + opt-in re-check before claim. Fetch booking
    # (without locking it) to identify the target service/package.
    pre_res = await db.execute(select(Booking).where(Booking.id == booking_id))
    pre_b = pre_res.scalar_one_or_none()
    if not pre_b:
        raise HTTPException(status_code=404, detail="Booking not found")

    target = None
    if pre_b.service_id:
        sr = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == pre_b.service_id))
        target = sr.scalar_one_or_none()
    elif pre_b.package_id:
        pr = await db.execute(select(CarePackage).where(CarePackage.id == pre_b.package_id))
        target = pr.scalar_one_or_none()

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

    # Schedule guard — a worker cannot hold two overlapping visits.
    from app.services.dispatch import worker_has_schedule_conflict
    if await worker_has_schedule_conflict(db, worker_id, pre_b):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "WORKER_SCHEDULE_CONFLICT",
                "message": "You already have a visit booked at this time.",
            },
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
            try:
                db.add(VisitRecord(
                    booking_id=b.id,
                    worker_id=worker_id,
                    patient_id=b.patient_id,
                    status=VisitStatus.scheduled,
                ))
                await db.flush()
            except IntegrityError:
                # A concurrent transaction created the visit first — ignore.
                await db.rollback()
                # Re-fetch booking after rollback so caller still gets fresh row.
                bres = await db.execute(select(Booking).where(Booking.id == booking_id))
                b = bres.scalar_one()
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


# Neither the nurse nor the customer may cancel inside this window before
# the scheduled visit start. Admin/ops can always cancel (support cases).
_CANCELLATION_CUTOFF_HOURS = 6


def _scheduled_start_utc(b: Booking) -> datetime:
    # Same convention as dispatch._window: scheduled_date + scheduled_start_time
    # are treated as UTC wall-clock throughout the codebase.
    return datetime.combine(b.scheduled_date, b.scheduled_start_time, tzinfo=timezone.utc)


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
            await notify_nearby_workers(db, b)
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