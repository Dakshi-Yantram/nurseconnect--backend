"""Booking dispatch: ONE eligibility rule for push, pull and accept.

ROOT CAUSES fixed here (why some eligible nearby nurses got bookings and
others did not):

1. Three code paths, three different rules.
   * push (``notify_nearby_workers``): approved + availability==online, wave-1
     radius only, schedule conflict computed against UTC-shifted windows.
   * pull (``GET /bookings/worker/new-requests``): no approval/availability
     check at all, widening waves, ``LIMIT 50`` applied *before* eligibility.
   * accept: no proximity, approval or availability check at all.
   Now all three call ``evaluate_worker_for_booking``.
2. Push only ever covered the wave-1 radius (5 km, 3 km urgent).  Nurses in the
   wave-2/3 ring only found the job if they happened to open the app and poll.
   ``rebroadcast_open_bookings`` (beat task) now pushes each wave as it opens.
3. ``availability == online`` excluded nurses who are ``on_visit`` / ``busy``
   even though they can legitimately take a later, non-overlapping slot.
   Only ``offline`` / ``on_leave`` are excluded now (same convention the admin
   rematch list already used).
4. Schedule-conflict windows treated IST wall-clock as UTC (see core.timeutil).
5. Every /verify replay, webhook, reconcile and cash-collect re-ran the whole
   broadcast, so nurses were re-pinged for bookings that were already assigned.
   Broadcast is now recorded per (booking, worker, cycle) with a UNIQUE
   constraint and only proceeds for still-open, non-expired bookings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import booking_end_utc, booking_start_utc, is_booking_expired
from app.models.enums import BookingStatus, WorkerAvailability, WorkerOnboardingStatus
from app.models.models import (
    Booking,
    BookingDispatchNotification,
    CarePackage,
    ServiceCatalogue,
    User,
    WorkerProfile,
)

logger = logging.getLogger(__name__)

# Statuses that mean the worker is committed to a visit at that time.
_OCCUPYING_STATUSES = (
    BookingStatus.assigned,
    BookingStatus.worker_en_route,
    BookingStatus.worker_arrived,
    BookingStatus.in_progress,
)

# A booking is open to workers only in these states (and with no worker).
DISPATCHABLE_STATUSES = (
    BookingStatus.confirmed,
    BookingStatus.rematch_pending,
    BookingStatus.searching_nurse,
)

# Workers who have explicitly stepped away never get requests.
_UNAVAILABLE = (WorkerAvailability.offline, WorkerAvailability.on_leave)


def _window(b: Booking) -> tuple[datetime, datetime]:
    return booking_start_utc(b), booking_end_utc(b)


def _overlaps(a: Booking, b: Booking) -> bool:
    a1, a2 = _window(a)
    b1, b2 = _window(b)
    return a1 < b2 and b1 < a2


async def lock_worker_schedule(db: AsyncSession, worker_id: UUID) -> None:
    """Serialize schedule decisions for one worker for the rest of this txn.

    Without it two concurrent /accept calls for *different* overlapping
    bookings both pass ``worker_has_schedule_conflict`` (each sees no
    committed conflict) and the nurse ends up double-booked. The advisory lock
    is transaction-scoped, so it releases on commit/rollback.
    """
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"worker-schedule:{worker_id}"},
        )
    except Exception:  # noqa: BLE001 — non-PG dialect (unit tests)
        logger.debug("advisory lock unavailable; continuing without it")


async def worker_has_schedule_conflict(
    db: AsyncSession, worker_id: UUID, booking: Booking
) -> bool:
    """True if the worker already has a committed booking overlapping this one."""
    # A visit that starts late in the evening IST can spill into the next IST
    # day, so prefilter on the adjacent days rather than an exact-date match.
    res = await db.execute(
        select(Booking).where(
            Booking.worker_id == worker_id,
            Booking.status.in_(_OCCUPYING_STATUSES),
            Booking.scheduled_date >= booking.scheduled_date - timedelta(days=1),
            Booking.scheduled_date <= booking.scheduled_date + timedelta(days=1),
        )
    )
    for other in res.scalars().all():
        if other.id == booking.id:
            continue
        if _overlaps(other, booking):
            return True
    return False


def booking_dispatch_block_reason(b: Booking, *, now: Optional[datetime] = None) -> Optional[str]:
    """Why a booking may not be offered/claimed right now (None = open)."""
    if b.worker_id is not None:
        return "ALREADY_ASSIGNED"
    if b.status not in DISPATCHABLE_STATUSES:
        return "NOT_DISPATCHABLE_STATUS"
    if is_booking_expired(b, now=now):
        return "SLOT_EXPIRED"
    return None


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: Optional[str] = None
    distance_km: Optional[float] = None


async def resolve_target(db: AsyncSession, booking: Booking):
    """The service/package the customer actually bought (package wins)."""
    if booking.package_id:
        r = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        pkg = r.scalar_one_or_none()
        if pkg is not None:
            return pkg
    if booking.service_id:
        r = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        return r.scalar_one_or_none()
    return None


async def evaluate_worker_for_booking(
    db: AsyncSession,
    worker: WorkerProfile,
    booking: Booking,
    target,
    *,
    now: Optional[datetime] = None,
    wave: Optional[int] = None,
    check_schedule: bool = True,
) -> Eligibility:
    """The single eligibility decision. Order is cheap-checks-first."""
    from app.core.provider_types import is_physical_capable, is_tele_capable
    from app.services.proximity import (
        compute_current_wave,
        effective_origin_for_worker,
        haversine_km,
        radius_for_wave,
    )
    from app.services.qualification import can_worker_receive_service

    now = now or datetime.now(timezone.utc)

    if worker.onboarding_status != WorkerOnboardingStatus.approved:
        return Eligibility(False, "WORKER_NOT_APPROVED")
    if worker.availability in _UNAVAILABLE:
        return Eligibility(False, "WORKER_UNAVAILABLE")
    if target is None:
        return Eligibility(False, "NO_TARGET")

    allowed, reason = await can_worker_receive_service(worker, target, db)
    if not allowed:
        return Eligibility(False, reason or "NOT_QUALIFIED")

    distance_km: Optional[float] = None
    tele_only = is_tele_capable(worker.worker_type) and not is_physical_capable(worker.worker_type)
    if not tele_only:
        wave_no = wave if wave is not None else compute_current_wave(booking, now=now)
        radius_km = radius_for_wave(wave_no, booking.is_urgent) or 10
        origin = effective_origin_for_worker(worker)
        has_coords = booking.latitude is not None and booking.longitude is not None
        addr = booking.address_snapshot if isinstance(booking.address_snapshot, dict) else {}
        addr_city = addr.get("city")
        if has_coords and origin is not None:
            distance_km = haversine_km(origin[0], origin[1], float(booking.latitude), float(booking.longitude))
            if distance_km > radius_km:
                return Eligibility(False, "OUT_OF_RADIUS", distance_km)
        elif origin is None and booking.is_urgent:
            # Urgent jobs are never shown to a worker we cannot locate.
            return Eligibility(False, "NO_WORKER_LOCATION_FOR_URGENT")
        else:
            # No usable geometry on one side: fall back to city match.
            if worker.base_city and addr_city and worker.base_city != addr_city:
                return Eligibility(False, "CITY_MISMATCH")

    if check_schedule and await worker_has_schedule_conflict(db, worker.id, booking):
        return Eligibility(False, "SCHEDULE_CONFLICT", distance_km)

    return Eligibility(True, None, distance_km)


async def _record_broadcast(
    db: AsyncSession, booking_id: UUID, worker_id: UUID, cycle: int, wave: int
) -> bool:
    """Atomically claim the right to notify this worker. True = we own it."""
    stmt = (
        pg_insert(BookingDispatchNotification)
        .values(booking_id=booking_id, worker_id=worker_id, cycle=cycle, wave=wave)
        .on_conflict_do_nothing(constraint="ux_dispatch_notif_booking_worker_cycle")
        .returning(BookingDispatchNotification.id)
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none() is not None


async def notify_nearby_workers(
    db: AsyncSession, booking: Booking, *, new_cycle: bool = False
) -> int:
    """Push a booking to every eligible worker who has not been pushed it yet.

    Idempotent: safe to call from /verify replays, the webhook, reconcile and
    the wave re-broadcast task. Returns the number of workers *newly* notified
    by this call. Never raises into the caller's transaction path.

    ``new_cycle=True`` is for a booking handed back to dispatch (nurse
    cancelled / failed safety check): it opens a fresh broadcast cycle so the
    previously-notified pool can be pinged again.
    """
    from app.services.common_services import send_notification
    from app.services.proximity import compute_current_wave

    now = datetime.now(timezone.utc)

    # Lock the booking row so concurrent broadcasters serialize and observe a
    # consistent status/worker/cycle.
    locked = await db.execute(
        select(Booking).where(Booking.id == booking.id).with_for_update()
    )
    b = locked.scalar_one_or_none()
    if b is None:
        return 0

    block = booking_dispatch_block_reason(b, now=now)
    if block is not None:
        logger.info("dispatch skipped booking=%s reason=%s", b.id, block)
        return 0

    if new_cycle:
        b.dispatch_cycle = int(b.dispatch_cycle or 1) + 1
        await db.flush()
    cycle = int(b.dispatch_cycle or 1)

    target = await resolve_target(db, b)
    if target is None:
        return 0

    wave = compute_current_wave(b, now=now)

    res = await db.execute(
        select(WorkerProfile).where(
            WorkerProfile.onboarding_status == WorkerOnboardingStatus.approved,
            WorkerProfile.availability.notin_(_UNAVAILABLE),
        )
    )
    workers = list(res.scalars().all())

    notified = 0
    for w in workers:
        elig = await evaluate_worker_for_booking(db, w, b, target, now=now, wave=wave)
        if not elig.ok:
            continue
        if not await _record_broadcast(db, b.id, w.id, cycle, wave):
            continue  # already pushed this cycle
        ures = await db.execute(select(User).where(User.id == w.user_id))
        wuser = ures.scalar_one_or_none()
        if not wuser:
            continue
        try:
            await send_notification(
                db,
                wuser.id,
                "new_booking_request",
                "New booking request",
                f"A {target.name} booking is available near you on "
                f"{b.scheduled_date.isoformat()} at "
                f"{b.scheduled_start_time.strftime('%H:%M')}.",
                {"booking_id": str(b.id), "booking_ref": b.booking_ref},
            )
            notified += 1
        except Exception:  # noqa: BLE001
            logger.exception("dispatch notify failed booking=%s worker=%s", b.id, w.id)
            continue
    return notified


async def broadcast_count(db: AsyncSession, booking_id: UUID, cycle: int) -> int:
    """How many workers have ever been pushed this booking in this cycle."""
    from sqlalchemy import func

    r = await db.execute(
        select(func.count(BookingDispatchNotification.id)).where(
            BookingDispatchNotification.booking_id == booking_id,
            BookingDispatchNotification.cycle == cycle,
        )
    )
    return int(r.scalar_one() or 0)


async def claim_no_worker_alert(db: AsyncSession, booking_id: UUID, cycle: int) -> bool:
    """Atomically claim the one-per-cycle "nobody reachable" ops alert."""
    from sqlalchemy import or_, update

    r = await db.execute(
        update(Booking)
        .where(
            Booking.id == booking_id,
            or_(Booking.no_worker_alerted_cycle.is_(None), Booking.no_worker_alerted_cycle != cycle),
        )
        .values(no_worker_alerted_cycle=cycle)
        .execution_options(synchronize_session=False)
    )
    return r.rowcount == 1
