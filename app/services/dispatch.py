"""Booking dispatch: notify nearby qualified workers + schedule-conflict guard.

Two responsibilities:
  1. worker_has_schedule_conflict() — is the worker already committed to another
     visit overlapping this booking's time window? Used to keep a worker's
     schedule free (dispatch filter + accept guard).
  2. notify_nearby_workers() — when a booking is confirmed, PUSH a request to
     every approved + online worker who is qualified, in range, and free at the
     scheduled time. (The pull endpoint /bookings/worker/new-requests still
     works; this just makes it a push as well.)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import BookingStatus, WorkerAvailability, WorkerOnboardingStatus
from app.models.models import (
    Booking,
    CarePackage,
    ConsumerProfile,
    ServiceCatalogue,
    User,
    WorkerProfile,
)

# Statuses that mean the worker is committed to a visit at that time.
_OCCUPYING_STATUSES = (
    BookingStatus.assigned,
    BookingStatus.worker_en_route,
    BookingStatus.worker_arrived,
    BookingStatus.in_progress,
)


def _window(b: Booking) -> tuple[datetime, datetime]:
    start = datetime.combine(b.scheduled_date, b.scheduled_start_time, tzinfo=timezone.utc)
    end = start + timedelta(minutes=int(b.scheduled_duration_minutes or 60))
    return start, end


def _overlaps(a: Booking, b: Booking) -> bool:
    a1, a2 = _window(a)
    b1, b2 = _window(b)
    return a1 < b2 and b1 < a2


async def worker_has_schedule_conflict(
    db: AsyncSession, worker_id: UUID, booking: Booking
) -> bool:
    """True if the worker already has a committed booking overlapping this one."""
    res = await db.execute(
        select(Booking).where(
            Booking.worker_id == worker_id,
            Booking.status.in_(_OCCUPYING_STATUSES),
            Booking.scheduled_date == booking.scheduled_date,  # cheap same-day prefilter
        )
    )
    for other in res.scalars().all():
        if other.id == booking.id:
            continue
        if _overlaps(other, booking):
            return True
    return False


async def notify_nearby_workers(db: AsyncSession, booking: Booking) -> int:
    """Push a booking request to nearby, qualified, free, online workers.

    Best-effort: returns the number of workers notified. Never raises into the
    caller's transaction path (payment confirmation must not fail on notify).
    """
    from app.services.common_services import send_notification
    from app.services.proximity import (
        effective_origin_for_worker,
        haversine_km,
        radius_for_wave,
    )
    from app.services.qualification import can_worker_receive_service

    # Resolve the service/package target for qualification checks.
    target = None
    if booking.service_id:
        r = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        target = r.scalar_one_or_none()
    elif booking.package_id:
        r = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        target = r.scalar_one_or_none()
    if target is None:
        return 0

    # First-wave radius (wave 1). Urgent bookings get a wider first wave.
    radius_km = radius_for_wave(1, booking.is_urgent) or 10
    b_lat = float(booking.latitude) if booking.latitude is not None else None
    b_lng = float(booking.longitude) if booking.longitude is not None else None
    addr_city = (booking.address_snapshot or {}).get("city") if isinstance(booking.address_snapshot, dict) else None

    # Candidate pool: approved + online workers only.
    res = await db.execute(
        select(WorkerProfile).where(
            WorkerProfile.onboarding_status == WorkerOnboardingStatus.approved,
            WorkerProfile.availability == WorkerAvailability.online,
        )
    )
    workers = list(res.scalars().all())

    notified = 0
    for w in workers:
        # Qualified + opted in for this service/package?
        allowed, _ = await can_worker_receive_service(w, target, db)
        if not allowed:
            continue
        # Free at the scheduled time?
        if await worker_has_schedule_conflict(db, w.id, booking):
            continue
        # In range? Prefer geo distance; fall back to city match.
        origin = effective_origin_for_worker(w)
        if b_lat is not None and b_lng is not None and origin is not None:
            if haversine_km(origin[0], origin[1], b_lat, b_lng) > radius_km:
                continue
        elif w.base_city and addr_city and w.base_city != addr_city:
            continue

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
                f"{booking.scheduled_date.isoformat()} at "
                f"{booking.scheduled_start_time.strftime('%H:%M')}.",
                {"booking_id": str(booking.id), "booking_ref": booking.booking_ref},
            )
            notified += 1
        except Exception:  # noqa: BLE001
            continue
    return notified
