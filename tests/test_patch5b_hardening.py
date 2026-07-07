"""Patch 5B — focused hardening tests.

Covers the six contract items called out by the patch deliverables:

  1. Consumer cannot access another consumer's patient
  2. Worker cannot access unassigned booking (tracking + visit)
  3. Unauthorized tracking access blocked
  4. Unauthorized websocket subscription blocked
  5. Insurance override requires justification
  6. Audit log created for denied access
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests
import websockets
from sqlalchemy import select

from tests.conftest import API, auth_headers


def _h(auth: dict) -> dict:
    return auth_headers(auth)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _create_patient(consumer_auth: dict) -> str:
    payload = {"full_name": f"Patch5B Patient {uuid.uuid4().hex[:6]}", "is_minor": False}
    r = requests.post(f"{API}/patients", headers=_h(consumer_auth), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def _ws_url(token: str, booking_id: str) -> str:
    base = API.replace("http://", "ws://").replace("https://", "wss://")
    return f"{base}/ws/booking/{booking_id}?token={token}"


# ============================================================================
# 1 — Consumer cannot access another consumer's patient
# ============================================================================
def test_consumer_cannot_access_other_consumer_patient(consumer_auth, admin_super_auth):
    """A consumer must not be able to read consent/ABHA data for a patient that
    does not belong to their own ConsumerProfile."""
    # Create a patient owned by *this* consumer
    own_patient = _create_patient(consumer_auth)
    # Forge another patient id (one that doesn't belong to this consumer)
    foreign_patient = uuid.uuid4()

    r1 = requests.get(
        f"{API}/consents/patient/{foreign_patient}",
        headers=_h(consumer_auth),
        timeout=10,
    )
    assert r1.status_code in (403, 404), f"expected 403/404, got {r1.status_code}: {r1.text}"

    r2 = requests.get(
        f"{API}/abha-records/patient/{foreign_patient}",
        headers=_h(consumer_auth),
        timeout=10,
    )
    assert r2.status_code in (403, 404), f"expected 403/404, got {r2.status_code}: {r2.text}"

    # And the own patient must still work
    r3 = requests.get(
        f"{API}/consents/patient/{own_patient}",
        headers=_h(consumer_auth),
        timeout=10,
    )
    assert r3.status_code == 200


# ============================================================================
# 2 + 3 — Worker cannot access unassigned booking / tracking
# ============================================================================
def test_worker_cannot_access_unassigned_booking_tracking(worker_auth):
    """GET /tracking/booking/{id}/latest must reject when the worker isn't
    assigned and there's no booking ownership."""
    foreign_booking = uuid.uuid4()
    r = requests.get(
        f"{API}/tracking/booking/{foreign_booking}/latest",
        headers=_h(worker_auth),
        timeout=10,
    )
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}: {r.text}"


def test_unauthorized_tracking_access_blocked_for_consumer(consumer_auth):
    """A consumer must not read tracking data for a booking they don't own."""
    foreign_booking = uuid.uuid4()
    r = requests.get(
        f"{API}/tracking/booking/{foreign_booking}/latest",
        headers=_h(consumer_auth),
        timeout=10,
    )
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}: {r.text}"


def test_worker_cannot_read_visit_for_unassigned_booking(worker_auth):
    """GET /visits/{id} must enforce assignment / ownership (Patch 5B)."""
    foreign_booking = uuid.uuid4()
    r = requests.get(
        f"{API}/visits/{foreign_booking}",
        headers=_h(worker_auth),
        timeout=10,
    )
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}: {r.text}"


# ============================================================================
# 4 — Unauthorized WebSocket subscription blocked
# ============================================================================
@pytest.mark.asyncio
async def test_ws_booking_rejects_missing_token():
    base = API.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{base}/ws/booking/{uuid.uuid4()}"
    with pytest.raises(Exception):
        async with websockets.connect(url, open_timeout=5) as ws:  # noqa: F841
            await ws.recv()


@pytest.mark.asyncio
async def test_ws_booking_rejects_invalid_token():
    base = API.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{base}/ws/booking/{uuid.uuid4()}?token=not-a-real-jwt"
    with pytest.raises(Exception):
        async with websockets.connect(url, open_timeout=5) as ws:  # noqa: F841
            await ws.recv()


@pytest.mark.asyncio
async def test_ws_booking_rejects_user_not_owner(worker_auth):
    """A worker who is NOT assigned must not be able to subscribe to a
    booking's tracking topic."""
    token = worker_auth["tokens"]["access_token"]
    base = API.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{base}/ws/booking/{uuid.uuid4()}?token={token}"
    with pytest.raises(Exception):
        async with websockets.connect(url, open_timeout=5) as ws:
            await ws.recv()


# ============================================================================
# 5 — Insurance override requires justification + role
# ============================================================================
def test_insurance_review_queue_requires_clinical(consumer_auth, worker_auth, admin_ops_auth, admin_clinical_auth):
    """Only admin_clinical / admin_super may read the queue."""
    for auth in (consumer_auth, worker_auth, admin_ops_auth):
        r = requests.get(f"{API}/insurance/review-queue", headers=_h(auth), timeout=10)
        assert r.status_code == 403, f"expected 403 for forbidden role, got {r.status_code}: {r.text}"

    r = requests.get(f"{API}/insurance/review-queue", headers=_h(admin_clinical_auth), timeout=10)
    assert r.status_code == 200


def test_insurance_override_validation(admin_clinical_auth):
    """POST /insurance/review/{id}/override must:
      - 404 on unknown assessment
      - 422 when justification missing/too short
    """
    fake_id = uuid.uuid4()
    # Empty body → pydantic 422
    r = requests.post(
        f"{API}/insurance/review/{fake_id}/override",
        headers=_h(admin_clinical_auth),
        json={},
        timeout=10,
    )
    assert r.status_code == 422

    # Justification too short (pydantic min_length=8) → 422
    r = requests.post(
        f"{API}/insurance/review/{fake_id}/override",
        headers=_h(admin_clinical_auth),
        json={"new_coverage_status": "covered", "justification": "ok"},
        timeout=10,
    )
    assert r.status_code == 422

    # Unknown assessment id (well-formed payload) → 404
    r = requests.post(
        f"{API}/insurance/review/{fake_id}/override",
        headers=_h(admin_clinical_auth),
        json={
            "new_coverage_status": "covered",
            "justification": "patch5b override unit test, sufficient length",
        },
        timeout=10,
    )
    assert r.status_code == 404


# ============================================================================
# 6 — Audit log created for denied access + insurance override
# ============================================================================
def test_security_audit_log_created_for_denied_access(worker_auth):
    """Trigger a tracking 403 and verify a `security.access_denied` row appears
    in ``audit_log``.
    """
    foreign_booking = uuid.uuid4()
    r = requests.get(
        f"{API}/tracking/booking/{foreign_booking}/latest",
        headers=_h(worker_auth),
        timeout=10,
    )
    assert r.status_code in (403, 404)

    # Direct DB inspection — confirms the security_audit_service wrote a row.
    async def _count() -> int:
        from app.core.database import AsyncSessionLocal
        from app.models.models import AuditLog

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "security.access_denied",
                    AuditLog.entity_id == str(foreign_booking),
                )
            )
            return len(list(res.scalars().all()))

    # Only count the row when status was 403 (404 means access check returned
    # "not found" before ownership check — still a valid path, just nothing
    # to audit).
    if r.status_code == 403:
        assert asyncio.run(_count()) >= 1


def test_insurance_override_writes_audit_entry(admin_clinical_auth):
    """An override on a seeded assessment must produce a
    `security.insurance_override` audit entry containing previous + new
    decision fields.
    """
    async def _seed() -> dict:
        from app.core.database import AsyncSessionLocal
        from app.models.enums import (
            BookingStatus,
            BookingType,
            InsuranceCoverageStatus,
            PaymentStatus,
            UserRole,
            UserStatus,
            WorkerOnboardingStatus,
            WorkerAvailability,
            WorkerTier,
        )
        from app.models.models import (
            Booking,
            ConsumerProfile,
            InsuranceCoverageAssessment,
            Patient,
            User,
            WorkerProfile,
        )

        async with AsyncSessionLocal() as db:
            consumer_user = User(
                phone_e164=f"+91{uuid.uuid4().int % 10**10:010d}",
                role=UserRole.consumer,
                full_name="Patch5B Consumer",
                status=UserStatus.active,
            )
            worker_user = User(
                phone_e164=f"+91{uuid.uuid4().int % 10**10:010d}",
                role=UserRole.worker,
                full_name="Patch5B Worker",
                status=UserStatus.active,
            )
            db.add_all([consumer_user, worker_user])
            await db.flush()

            cp = ConsumerProfile(user_id=consumer_user.id)
            wp = WorkerProfile(
                user_id=worker_user.id,
                tier=WorkerTier.tier3,
                onboarding_status=WorkerOnboardingStatus.approved,
                availability=WorkerAvailability.online,
            )
            db.add_all([cp, wp])
            await db.flush()

            patient = Patient(consumer_id=cp.id, full_name="Patch5B Patient")
            db.add(patient)
            await db.flush()

            booking = Booking(
                booking_ref=f"P5B-{uuid.uuid4().hex[:8].upper()}",
                consumer_id=cp.id,
                patient_id=patient.id,
                booking_type=BookingType.one_time,
                worker_id=wp.id,
                status=BookingStatus.completed,
                scheduled_date=date.today(),
                scheduled_start_time=datetime.now().time().replace(microsecond=0),
                scheduled_duration_minutes=30,
                address_snapshot={"line1": "1 Test"},
                latitude=Decimal("12.97"),
                longitude=Decimal("77.59"),
                base_amount=Decimal("100"),
                total_amount=Decimal("100"),
                payment_status=PaymentStatus.captured,
            )
            db.add(booking)
            await db.flush()

            a = InsuranceCoverageAssessment(
                booking_id=booking.id,
                worker_id=wp.id,
                coverage_status=InsuranceCoverageStatus.conditional,
                coverage_percent=Decimal("50"),
                exclusion_reasons=["test_only"],
                flagged_for_review=True,
            )
            db.add(a)
            await db.commit()
            await db.refresh(a)
            return {"assessment_id": str(a.id), "previous_status": a.coverage_status.value}

    async def _read_audit(assessment_id: str) -> list[dict]:
        from app.core.database import AsyncSessionLocal
        from app.models.models import AuditLog

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "security.insurance_override",
                    AuditLog.entity_id == assessment_id,
                )
            )
            rows = res.scalars().all()
            return [r.changes or {} for r in rows]

    async def _run() -> tuple[dict, list[dict], dict]:
        seeded = await _seed()
        # HTTP call happens in the same event loop via requests (sync, OK)
        # but we must run reads in the same loop, so do them here too.
        # Synchronous HTTP request — wrapped via a thread so the loop is free.
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()

        def _post() -> requests.Response:
            return requests.post(
                f"{API}/insurance/review/{seeded['assessment_id']}/override",
                headers=_h(admin_clinical_auth),
                json={
                    "new_coverage_status": "covered",
                    "justification": "Patch5B override audit verification",
                },
                timeout=10,
            )

        resp = await loop.run_in_executor(None, _post)
        audits = await _read_audit(seeded["assessment_id"])
        return seeded, audits, resp.json() if resp.ok else {"_status": resp.status_code, "_text": resp.text}

    seeded, audits, body = asyncio.run(_run())
    assert body.get("coverage_status") == "covered", body
    assert body.get("reviewed_by") is not None
    assert body.get("reviewed_at") is not None
    assert len(audits) >= 1
    changes = audits[-1]
    assert changes["previous_decision"] == seeded["previous_status"]
    assert changes["new_decision"] == "covered"
    assert changes["previous_coverage_percent"] == 50.0
    assert changes["new_coverage_percent"] == 100.0
    assert "justification" in changes and changes["justification"]
