"""Phase 5 — Payment idempotency + booking/visit/tracking lifecycle (backend-only).

Covers all 10 items in the Phase 5 review request:
 1.  /payments/verify idempotent on same razorpay_payment_id (no dup ledger; booking stays confirmed once)
 2.  /payments/verify with DIFFERENT razorpay_payment_id but same order_id after capture → idempotent_replay:true
       and stored razorpay_payment_id stays the FIRST id (no leak/overwrite)
 3.  /payments/consumer/history dedupe → exactly ONE captured row per booking after replays
 4.  /payments/order on already-captured booking returns 400 "Already paid"
 5.  /payments/webhook/razorpay idempotent for the same razorpay_payment_id → second call returns duplicate:true
 6.  Booking lifecycle smoke (otp → services → patient → booking → order → verify → confirmed/captured)
 7.  Worker accept lifecycle (accept → checkin → checkout)
 8.  Tracking /tracking/location returns ok:true for worker token + booking_id
 9.  Visit lifecycle (vitals + medication + care-note) return 200 on happy path
 10. Verify response shape after capture: must include idempotent_replay:true
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
import requests

# Backend is local in this environment; the public URL also works. Use 8001 directly
# because the review request explicitly says backend at http://localhost:8001/api.
BASE_URL = os.environ.get("BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

OTP = "123456"


# ----------------------------- helpers -----------------------------
def _unique_phone() -> str:
    """Fresh consumer phone per scenario to avoid stale-state collisions."""
    return f"+9199{str(uuid4().int)[-9:]}"


def _login(phone: str, role: str) -> tuple[str, str]:
    r = requests.post(f"{API}/auth/send-otp", json={"phone_e164": phone, "role": role}, timeout=10)
    assert r.status_code == 200, f"send-otp {r.status_code}: {r.text}"
    r = requests.post(
        f"{API}/auth/verify-otp",
        json={
            "phone_e164": phone,
            "code": OTP,
            "role": role,
            "device_id": "pytest-phase5",
            "device_platform": "cli",
        },
        timeout=10,
    )
    assert r.status_code == 200, f"verify-otp {r.status_code}: {r.text}"
    d = r.json()
    return d["tokens"]["access_token"], d["user"]["id"]


def _h(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _ensure_patient(token: str) -> str:
    r = requests.get(f"{API}/patients", headers=_h(token), timeout=10)
    r.raise_for_status()
    patients = r.json()
    if patients:
        return patients[0]["id"]
    r = requests.post(
        f"{API}/patients",
        headers=_h(token),
        json={
            "full_name": "Phase5 Patient",
            "date_of_birth": "1980-01-01",
            "gender": "male",
            "relationship_to_consumer": "self",
        },
        timeout=10,
    )
    assert r.status_code in (200, 201), f"create patient: {r.status_code} {r.text}"
    return r.json()["id"]


def _services() -> list[dict]:
    r = requests.get(f"{API}/services", timeout=10)
    r.raise_for_status()
    return r.json()


def _create_booking(token: str, svc_id: str, pid: str) -> dict:
    payload = {
        "patient_id": pid,
        "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=2)).isoformat(),
        "scheduled_start_time": "11:00:00",
        "address": {"line1": "1 Phase5 Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = requests.post(f"{API}/bookings/", headers=_h(token), json=payload, timeout=10)
    assert r.status_code in (200, 201), f"create booking: {r.status_code} {r.text}"
    return r.json()


def _order(token: str, bid: str) -> dict:
    r = requests.post(f"{API}/payments/order", headers=_h(token), json={"booking_id": bid}, timeout=10)
    assert r.status_code == 200, f"order: {r.status_code} {r.text}"
    return r.json()


def _verify(token: str, bid: str, order_id: str, payment_id: str) -> requests.Response:
    return requests.post(
        f"{API}/payments/verify",
        headers=_h(token),
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
            "booking_id": bid,
        },
        timeout=10,
    )


# ----------------------------- shared session fixtures -----------------------------
@pytest.fixture(scope="module")
def captured_booking() -> dict:
    """A consumer+booking that is already captured. Used by multiple idempotency tests
    to share state (same razorpay_payment_id, same booking)."""
    phone = _unique_phone()
    token, uid = _login(phone, "consumer")
    pid = _ensure_patient(token)
    svc_id = _services()[0]["id"]
    booking = _create_booking(token, svc_id, pid)
    order = _order(token, booking["id"])
    pay_id = "pay_mock_phase5_" + uuid4().hex[:10]
    v1 = _verify(token, booking["id"], order["razorpay_order_id"], pay_id)
    assert v1.status_code == 200, v1.text
    body = v1.json()
    assert body["verified"] is True
    assert body["payment_status"] == "captured"
    assert body["booking_status"] == "confirmed"
    assert "idempotent_replay" not in body, f"first verify must NOT be a replay: {body}"
    return {
        "token": token,
        "consumer_id": uid,
        "booking_id": booking["id"],
        "booking_ref": booking["booking_ref"],
        "order_id": order["razorpay_order_id"],
        "razorpay_payment_id": pay_id,
    }


# ----------------------------- TESTS -----------------------------
# --- Items 1, 10: verify idempotency on same payment_id and response shape ---
class TestVerifyIdempotency:
    def test_replay_same_payment_id_returns_idempotent_flag(self, captured_booking):
        c = captured_booking
        r = _verify(c["token"], c["booking_id"], c["order_id"], c["razorpay_payment_id"])
        assert r.status_code == 200, r.text
        body = r.json()
        # Response shape item #10
        assert body.get("idempotent_replay") is True, f"replay flag missing: {body}"
        assert body["verified"] is True
        assert body["payment_status"] == "captured"
        assert body["booking_status"] == "confirmed"

    def test_history_has_exactly_one_captured_row_for_booking(self, captured_booking):
        # Hit /verify again before reading history to force the dedupe code path
        c = captured_booking
        _verify(c["token"], c["booking_id"], c["order_id"], c["razorpay_payment_id"])
        r = requests.get(f"{API}/payments/consumer/history", headers=_h(c["token"]), timeout=10)
        assert r.status_code == 200, r.text
        hist = r.json()
        matches = [h for h in hist if h["booking_id"] == c["booking_id"]]
        assert len(matches) == 1, f"duplicate ledger entry detected: {matches}"
        assert matches[0]["payment_status"] == "captured"
        assert matches[0]["razorpay_payment_id"] == c["razorpay_payment_id"]


# --- Item 2: replay with a DIFFERENT payment_id but same order ---
class TestDifferentPaymentIdReplay:
    def test_different_payment_id_does_not_override_stored_id(self, captured_booking):
        c = captured_booking
        other_pid = "pay_mock_phase5_other_" + uuid4().hex[:8]
        r = _verify(c["token"], c["booking_id"], c["order_id"], other_pid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("idempotent_replay") is True, f"must still treat as already paid: {body}"
        # Stored payment_id in history must remain the FIRST one (no overwrite leak).
        h = requests.get(f"{API}/payments/consumer/history", headers=_h(c["token"]), timeout=10).json()
        matches = [x for x in h if x["booking_id"] == c["booking_id"]]
        assert len(matches) == 1
        assert matches[0]["razorpay_payment_id"] == c["razorpay_payment_id"], (
            f"second pid leaked into stored row: {matches[0]}"
        )


# --- Item 4: /payments/order on already-captured booking is rejected ---
class TestOrderGuardAfterCapture:
    def test_order_after_capture_returns_400_already_paid(self, captured_booking):
        c = captured_booking
        r = requests.post(
            f"{API}/payments/order", headers=_h(c["token"]), json={"booking_id": c["booking_id"]}, timeout=10
        )
        assert r.status_code == 400, f"expected 400 already paid, got {r.status_code} {r.text}"
        assert "already" in r.text.lower(), r.text


# --- Item 5: webhook idempotency ---
class TestWebhookIdempotency:
    def test_payment_captured_webhook_dedupes_by_payment_id(self):
        # Build a fresh booking to a NEW order id and use the webhook path only
        # (no /verify) — so the webhook handler creates the ledger row, and a
        # second call must short-circuit with duplicate:true.
        phone = _unique_phone()
        token, _ = _login(phone, "consumer")
        pid = _ensure_patient(token)
        svc = _services()[0]
        booking = _create_booking(token, svc["id"], pid)
        order = _order(token, booking["id"])
        pay_id = "pay_mock_webhook_" + uuid4().hex[:10]

        webhook_body = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"id": pay_id, "order_id": order["razorpay_order_id"], "status": "captured"}
                }
            },
        }
        raw = json.dumps(webhook_body)
        headers = {"Content-Type": "application/json", "x-razorpay-signature": "mock_webhook_sig"}

        r1 = requests.post(f"{API}/payments/webhook/razorpay", data=raw, headers=headers, timeout=10)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1.get("received") is True
        assert not b1.get("duplicate"), f"first webhook must not be duplicate: {b1}"

        r2 = requests.post(f"{API}/payments/webhook/razorpay", data=raw, headers=headers, timeout=10)
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2.get("received") is True and b2.get("duplicate") is True, f"second call must be duplicate: {b2}"

        # And the consumer history still has exactly one captured row for this booking.
        h = requests.get(f"{API}/payments/consumer/history", headers=_h(token), timeout=10).json()
        matches = [x for x in h if x["booking_id"] == booking["id"]]
        assert len(matches) == 1, f"webhook dedupe failed at ledger: {matches}"
        assert matches[0]["razorpay_payment_id"] == pay_id


# --- Items 6, 7, 8, 9: end-to-end booking + worker accept + visit + tracking ---
class TestBookingWorkerVisitLifecycle:
    @pytest.fixture(scope="class")
    def lifecycle_ctx(self):
        cphone = _unique_phone()
        ctoken, _ = _login(cphone, "consumer")
        wtoken, _ = _login("+919999000002", "worker")
        pid = _ensure_patient(ctoken)
        svc = _services()[0]
        booking = _create_booking(ctoken, svc["id"], pid)
        order = _order(ctoken, booking["id"])
        v = _verify(
            ctoken,
            booking["id"],
            order["razorpay_order_id"],
            "pay_mock_lc_" + uuid4().hex[:10],
        )
        assert v.status_code == 200, v.text
        vbody = v.json()
        assert vbody["booking_status"] == "confirmed"
        assert vbody["payment_status"] == "captured"
        return {
            "ctoken": ctoken,
            "wtoken": wtoken,
            "booking_id": booking["id"],
            "patient_id": pid,
        }

    # Item 6: booking lifecycle smoke
    def test_booking_confirmed_and_captured(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        # Re-read via consumer's listing
        r = requests.get(f"{API}/bookings/{ctx['booking_id']}", headers=_h(ctx["ctoken"]), timeout=10)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["status"] == "confirmed"
        assert b["payment_status"] == "captured"

    # Item 7a: worker accept → assigned/accepted
    def test_worker_accept_assigns_booking(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/bookings/{ctx['booking_id']}/accept", headers=_h(ctx["wtoken"]), timeout=10
        )
        assert r.status_code == 200, f"accept failed: {r.status_code} {r.text}"
        body = r.json()
        # Either 'assigned' or 'accepted' per spec
        assert body.get("status") in ("assigned", "accepted"), body

    # Item 8: tracking sanity
    def test_tracking_location_post(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/tracking/location",
            headers=_h(ctx["wtoken"]),
            json={"latitude": "19.0762", "longitude": "72.8779", "booking_id": ctx["booking_id"]},
            timeout=10,
        )
        assert r.status_code == 200, f"tracking: {r.status_code} {r.text}"
        body = r.json()
        assert body.get("ok") is True, body

    # Item 7b: visit checkin
    def test_visit_checkin(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/visits/{ctx['booking_id']}/checkin",
            headers=_h(ctx["wtoken"]),
            json={"latitude": "19.0760", "longitude": "72.8777"},
            timeout=10,
        )
        assert r.status_code == 200, f"checkin: {r.status_code} {r.text}"
        body = r.json()
        # status may be 'in_progress' or 'checked_in'
        assert body.get("status") in ("in_progress", "checked_in", "started", "ongoing"), body

    # Item 9: vitals
    def test_visit_vitals_submit(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/visits/{ctx['booking_id']}/vitals",
            headers=_h(ctx["wtoken"]),
            json={
                "bp_systolic": 120,
                "bp_diastolic": 80,
                "pulse": 75,
                "spo2": 98,
                "temperature_f": 98.6,
                "pain_score": 1,
            },
            timeout=10,
        )
        assert r.status_code == 200, f"vitals: {r.status_code} {r.text}"

    # Item 9: medication
    def test_visit_medication_submit(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/visits/{ctx['booking_id']}/medications",
            headers=_h(ctx["wtoken"]),
            json={
                "drug_name": "Paracetamol 500mg",
                "dose_amount": "500mg",
                "route": "oral",
                "allergy_check_done": True,
                "allergy_confirmed_clear": True,
                "patient_identified": True,
                "expiry_checked": True,
                "administered_at": "2026-05-11T11:00:00+00:00",
            },
            timeout=10,
        )
        assert r.status_code == 200, f"medication: {r.status_code} {r.text}"

    # Item 9: care-note
    def test_care_note_create(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/care-notes/",
            headers=_h(ctx["wtoken"]),
            json={
                "patient_id": ctx["patient_id"],
                "booking_id": ctx["booking_id"],
                "content": "Phase5 happy-path note: patient stable.",
                "note_type": "observation",
            },
            timeout=10,
        )
        assert r.status_code == 200, f"care-note: {r.status_code} {r.text}"

    # Item 7c: visit checkout (allow 400 if escalations open — per review note)
    def test_visit_checkout(self, lifecycle_ctx):
        ctx = lifecycle_ctx
        r = requests.post(
            f"{API}/visits/{ctx['booking_id']}/checkout",
            headers=_h(ctx["wtoken"]),
            json={
                "latitude": "19.0760",
                "longitude": "72.8777",
                "family_summary": "Visit completed.",
                "care_notes": "All vitals stable.",
            },
            timeout=10,
        )
        assert r.status_code in (200, 400), f"checkout: {r.status_code} {r.text}"
        if r.status_code == 400:
            # Pre-existing acceptable 400 reasons (per review note): open escalations
            # OR incomplete documentation gates (vitals/checklist/care-note required).
            txt = r.text.lower()
            assert any(k in txt for k in (
                "escalation", "open", "incomplete_documentation",
                "documentation", "checklist", "vitals", "care",
            )), r.text
