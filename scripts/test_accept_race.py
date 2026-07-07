"""End-to-end concurrency test for POST /bookings/{id}/accept.

Spawns two workers and has them race to claim the same booking. Verifies:
1. Exactly one worker wins (HTTP 200) and gets booking.worker_id set.
2. The losing worker receives HTTP 409 BOOKING_ALREADY_CLAIMED.
3. Only one VisitRecord exists for the booking.
4. Idempotent retry by the winner returns 200, no second VisitRecord.
5. After winner wins, the booking disappears from /bookings/worker/new-requests
   for any worker.
6. Fast repeated taps (10 concurrent requests from random workers) still only
   result in one winning row and one VisitRecord.
"""
import asyncio
import logging
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

# Use the app's DB session for fixture setup.
sys.path.insert(0, "/app/backend")
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.enums import (  # noqa: E402
    BookingStatus,
    BookingType,
    UserRole,
    UserStatus,
    WorkerAvailability,
    WorkerOnboardingStatus,
    WorkerTier,
)
from app.models.models import (  # noqa: E402
    Booking,
    ConsumerProfile,
    Patient,
    User,
    VisitRecord,
    WorkerProfile,
)

API = "http://127.0.0.1:8001/api"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("race")


async def _ensure_worker(db, phone: str, name: str) -> tuple[User, WorkerProfile]:
    res = await db.execute(select(User).where(User.phone_e164 == phone))
    u = res.scalar_one_or_none()
    if not u:
        u = User(phone_e164=phone, full_name=name, role=UserRole.worker,
                 status=UserStatus.active, password_hash=hash_password("Test@1234"))
        db.add(u)
        await db.flush()
    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == u.id))
    wp = wres.scalar_one_or_none()
    if not wp:
        wp = WorkerProfile(
            user_id=u.id, tier=WorkerTier.tier3,
            onboarding_status=WorkerOnboardingStatus.approved,
            availability=WorkerAvailability.online, base_city="Mumbai",
        )
        db.add(wp)
        await db.flush()
    return u, wp


async def _ensure_consumer(db) -> tuple[User, ConsumerProfile, Patient]:
    phone = "+919999111000"
    res = await db.execute(select(User).where(User.phone_e164 == phone))
    u = res.scalar_one_or_none()
    if not u:
        u = User(phone_e164=phone, full_name="Race Tester", role=UserRole.consumer,
                 status=UserStatus.active, password_hash=hash_password("Test@1234"))
        db.add(u)
        await db.flush()
    cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == u.id))
    cp = cres.scalar_one_or_none()
    if not cp:
        cp = ConsumerProfile(user_id=u.id, city="Mumbai", state="Maharashtra", pincode="400001",
                             address_line1="42 Marine Drive")
        db.add(cp)
        await db.flush()
    pres = await db.execute(select(Patient).where(Patient.consumer_id == cp.id))
    p = pres.scalar_one_or_none()
    if not p:
        p = Patient(consumer_id=cp.id, full_name="Test Patient", is_minor=False)
        db.add(p)
        await db.flush()
    return u, cp, p


async def create_open_booking(consumer_id, patient_id) -> str:
    """Insert a confirmed, unassigned booking directly via DB."""
    async with AsyncSessionLocal() as db:
        ref = f"NC{datetime.now().strftime('%y%m%d')}{uuid.uuid4().hex[:6].upper()}"
        b = Booking(
            booking_ref=ref,
            consumer_id=consumer_id,
            patient_id=patient_id,
            booking_type=BookingType.one_time,
            status=BookingStatus.confirmed,
            scheduled_date=date.today() + timedelta(days=1),
            scheduled_start_time=time(10, 0),
            scheduled_duration_minutes=60,
            address_snapshot={"line1": "42 Marine Drive", "city": "Mumbai", "state": "MH", "pincode": "400001"},
            latitude=Decimal("19.0"),
            longitude=Decimal("72.8"),
            base_amount=Decimal("499"),
            total_amount=Decimal("499"),
        )
        db.add(b)
        await db.commit()
        return str(b.id)


async def login_worker(client: httpx.AsyncClient, phone: str) -> str:
    r = await client.post(f"{API}/auth/send-otp", json={"phone_e164": phone, "purpose": "login"})
    r.raise_for_status()
    r = await client.post(f"{API}/auth/verify-otp", json={"phone_e164": phone, "code": "123456", "purpose": "login"})
    r.raise_for_status()
    return r.json()["tokens"]["access_token"]


async def fetch_db_state(booking_id: str):
    async with AsyncSessionLocal() as db:
        bres = await db.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        b = bres.scalar_one()
        vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == uuid.UUID(booking_id)))
        visits = vres.scalars().all()
        return b, visits


async def main():
    # ---- Seed fixtures ----
    async with AsyncSessionLocal() as db:
        consumer, cp, patient = await _ensure_consumer(db)
        await _ensure_worker(db, "+919999000002", "Worker A")  # already in seed
        await _ensure_worker(db, "+919999222002", "Worker B")
        await _ensure_worker(db, "+919999333003", "Worker C")
        await db.commit()
        consumer_id, patient_id = cp.id, patient.id

    async with httpx.AsyncClient(timeout=30) as client:
        token_a = await login_worker(client, "+919999000002")
        token_b = await login_worker(client, "+919999222002")
        token_c = await login_worker(client, "+919999333003")

        # ============ TEST 1: head-to-head race ============
        b1 = await create_open_booking(consumer_id, patient_id)
        log.info("[T1] open booking %s — A and B race", b1)
        ra, rb = await asyncio.gather(
            client.post(f"{API}/bookings/{b1}/accept", headers={"Authorization": f"Bearer {token_a}"}),
            client.post(f"{API}/bookings/{b1}/accept", headers={"Authorization": f"Bearer {token_b}"}),
        )
        statuses = sorted([ra.status_code, rb.status_code])
        assert statuses == [200, 409], f"Expected one 200 + one 409, got {statuses}"
        loser = ra if ra.status_code == 409 else rb
        body = loser.json()
        assert body.get("code") == "BOOKING_ALREADY_CLAIMED", f"Bad loser payload: {body}"
        b, visits = await fetch_db_state(b1)
        assert b.worker_id is not None and b.status == BookingStatus.assigned
        assert len(visits) == 1, f"Expected 1 VisitRecord, got {len(visits)}"
        log.info("[T1] PASS — winner=%s, loser=%s code=%s, 1 visit", ra.status_code, rb.status_code, body["code"])

        # ============ TEST 2: idempotent retry by winner ============
        winner_token = token_a if ra.status_code == 200 else token_b
        retry = await client.post(f"{API}/bookings/{b1}/accept", headers={"Authorization": f"Bearer {winner_token}"})
        assert retry.status_code == 200, f"Idempotent retry must return 200, got {retry.status_code}"
        _, visits2 = await fetch_db_state(b1)
        assert len(visits2) == 1, f"Idempotent retry must not create duplicate VisitRecord (got {len(visits2)})"
        log.info("[T2] PASS — idempotent retry returns 200, still 1 visit")

        # ============ TEST 3: stale acceptance after winner wins ============
        late = await client.post(f"{API}/bookings/{b1}/accept", headers={"Authorization": f"Bearer {token_c}"})
        assert late.status_code == 409, f"Late accept must be 409, got {late.status_code}"
        assert late.json().get("code") == "BOOKING_ALREADY_CLAIMED"
        log.info("[T3] PASS — late accept by C returns 409 BOOKING_ALREADY_CLAIMED")

        # ============ TEST 4: new-requests no longer returns assigned booking ============
        nr = await client.get(f"{API}/bookings/worker/new-requests", headers={"Authorization": f"Bearer {token_c}"})
        nr.raise_for_status()
        ids = [item["id"] for item in nr.json()]
        assert b1 not in ids, f"Assigned booking {b1} must not appear in new-requests, got {ids}"
        log.info("[T4] PASS — assigned booking absent from new-requests")

        # ============ TEST 5: 10 simultaneous taps from 3 workers ============
        b2 = await create_open_booking(consumer_id, patient_id)
        log.info("[T5] new booking %s — 10 concurrent taps", b2)
        tokens_cycle = [token_a, token_b, token_c] * 4
        reqs = [
            client.post(f"{API}/bookings/{b2}/accept", headers={"Authorization": f"Bearer {t}"})
            for t in tokens_cycle[:10]
        ]
        results = await asyncio.gather(*reqs, return_exceptions=True)
        statuses = [r.status_code for r in results if hasattr(r, "status_code")]
        n200 = statuses.count(200)
        n409 = statuses.count(409)
        # The winner may also retry idempotently and return 200, so 200 count == count of winner's requests.
        assert n200 + n409 == 10, f"All responses must be 200/409, got {statuses}"
        assert n409 >= 1, "At least one losing request must 409"
        b, visits = await fetch_db_state(b2)
        assert len(visits) == 1, f"Expected exactly 1 VisitRecord, got {len(visits)}"
        log.info("[T5] PASS — %d 200s + %d 409s, 1 winner_id, 1 VisitRecord", n200, n409)

        # ============ TEST 6: BOOKING_NOT_AVAILABLE for cancelled booking ============
        b3 = await create_open_booking(consumer_id, patient_id)
        async with AsyncSessionLocal() as db:
            bres = await db.execute(select(Booking).where(Booking.id == uuid.UUID(b3)))
            row = bres.scalar_one()
            row.status = BookingStatus.cancelled
            await db.commit()
        resp = await client.post(f"{API}/bookings/{b3}/accept", headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 410, f"Cancelled booking must return 410, got {resp.status_code}"
        assert resp.json().get("code") == "BOOKING_NOT_AVAILABLE"
        log.info("[T6] PASS — cancelled booking returns 410 BOOKING_NOT_AVAILABLE")

    log.info("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
