"""
Diagnostic — for a given worker email, shows EXACTLY why each confirmed/
unassigned booking is or isn't showing up in /worker/new-requests: prints
worker's location + base_city, and for each booking its wave, radius,
distance, and city, plus the pass/fail reason.

USAGE (from backend/ folder):

    python check_new_requests_filter.py nayakbhawna778@gmail.com
"""
import asyncio
import sys
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import User, WorkerProfile, Booking, ServiceCatalogue, CarePackage
from app.models.enums import BookingStatus
from sqlalchemy import select


async def main():
    if len(sys.argv) < 2:
        print("Usage: python check_new_requests_filter.py <worker email>")
        sys.exit(1)
    email = sys.argv[1]

    from app.services.proximity import (
        compute_current_wave,
        effective_origin_for_worker,
        haversine_km,
        radius_for_wave,
    )
    from app.services.qualification import can_worker_receive_service

    async with AsyncSessionLocal() as session:
        ures = await session.execute(select(User).where(User.email == email))
        user = ures.scalar_one_or_none()
        if not user:
            print(f"No user with email {email}")
            return
        wres = await session.execute(select(WorkerProfile).where(WorkerProfile.user_id == user.id))
        profile = wres.scalar_one_or_none()
        if not profile:
            print("No WorkerProfile for this user.")
            return

        worker_origin = effective_origin_for_worker(profile)
        print(f"Worker: {email}")
        print(f"  base_city        = {profile.base_city}")
        print(f"  worker_origin    = {worker_origin}  (None means no usable lat/lng on file)")
        print()

        bres = await session.execute(
            select(Booking).where(
                Booking.worker_id.is_(None),
                Booking.status.in_([BookingStatus.confirmed, BookingStatus.rematch_pending]),
            )
        )
        items = bres.scalars().all()
        now = datetime.now(timezone.utc)

        for b in items:
            target = None
            if b.service_id:
                sr = await session.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == b.service_id))
                target = sr.scalar_one_or_none()
            elif b.package_id:
                pr = await session.execute(select(CarePackage).where(CarePackage.id == b.package_id))
                target = pr.scalar_one_or_none()

            print(f"Booking {b.booking_ref} (patient_id={b.patient_id}):")
            if not target:
                print("  -> HIDDEN: no matching service/package found\n")
                continue

            allowed, reason = await can_worker_receive_service(profile, target, session)
            if not allowed:
                print(f"  -> HIDDEN: qualification check failed ({reason})\n")
                continue

            current_wave = compute_current_wave(b, now=now)
            radius_km = radius_for_wave(current_wave, b.is_urgent)
            print(f"  wave={current_wave}  radius_km={radius_km}  is_urgent={b.is_urgent}")
            if radius_km is None:
                print("  -> HIDDEN: past final wave, escalated to admin\n")
                continue

            booking_has_coords = b.latitude is not None and b.longitude is not None
            addr_city = (b.address_snapshot or {}).get("city") if isinstance(b.address_snapshot, dict) else None
            print(f"  booking_coords={(b.latitude, b.longitude) if booking_has_coords else None}  addr_city={addr_city}")

            if booking_has_coords and worker_origin is not None:
                dist = haversine_km(worker_origin[0], worker_origin[1], b.latitude, b.longitude)
                print(f"  distance_km={round(dist,2)}")
                if dist > radius_km:
                    print(f"  -> HIDDEN: distance {round(dist,2)}km > radius {radius_km}km\n")
                    continue
            elif booking_has_coords and worker_origin is None:
                if b.is_urgent:
                    print("  -> HIDDEN: urgent booking but worker has no location on file\n")
                    continue
                if profile.base_city and addr_city and profile.base_city != addr_city:
                    print(f"  -> HIDDEN: city mismatch (worker base_city='{profile.base_city}' vs booking city='{addr_city}')\n")
                    continue
            elif not booking_has_coords:
                if profile.base_city and addr_city and profile.base_city != addr_city:
                    print(f"  -> HIDDEN: city mismatch (worker base_city='{profile.base_city}' vs booking city='{addr_city}')\n")
                    continue

            print("  -> VISIBLE ✔\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())