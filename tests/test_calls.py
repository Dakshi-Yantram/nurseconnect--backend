"""In-app voice calling (RealtimeKit, formerly Dyte) — end-to-end tests.

Covers the flow documented in app/api/v1/calls.py:
    POST /bookings/{id}/call/start
    POST /bookings/{id}/call/{call_session_id}/join
    POST /bookings/{id}/call/{call_session_id}/end
    POST /notifications/devices
    DELETE /notifications/devices/{device_id}

Runs against the MOCKED RealtimeKit provider (MOCK_EXTERNAL_PROVIDERS=true,
the default for this test suite / CI). It verifies our own API contract,
authorization rules, and state machine — it does NOT verify that a real
Cloudflare RealtimeKit meeting actually carries audio between two devices.
That still requires a manual test with two real phones/browsers on a real
booking, exactly as flagged separately.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg
import pytest
import requests

from tests.conftest import API, auth_headers

PG_DSN = os.environ.get(
    "PG_TEST_DSN",
    "postgresql://nurseconnect:nurseconnect@127.0.0.1:5432/nurseconnect",
)


def _sql(query: str, params: tuple = ()) -> None:
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


def _create_and_assign_booking(consumer_headers: dict, worker_headers: dict) -> str:
    """Create a booking as the consumer, bypass payment via direct SQL (same
    pattern used by tests/test_phase3_nurse_flow.py), then have the worker
    accept it so booking.worker_id is set — a call cannot start without one
    (see _load_booking_parties in calls.py)."""
    svcs = requests.get(f"{API}/services", timeout=10).json()
    patients = requests.get(f"{API}/patients", headers=consumer_headers, timeout=10).json()
    payload = {
        "patient_id": patients[0]["id"],
        "service_id": svcs[0]["id"],
        "scheduled_date": (date.today() + timedelta(days=1)).isoformat(),
        "scheduled_start_time": "10:00:00",
        "address": {"line1": "Calls Test Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = requests.post(f"{API}/bookings/", headers=consumer_headers, json=payload, timeout=10)
    assert r.status_code == 200, f"create booking failed: {r.status_code} {r.text}"
    bid = r.json()["id"]

    _sql(
        "UPDATE bookings SET status='confirmed', payment_status='captured', worker_id=NULL WHERE id=%s",
        (bid,),
    )
    r = requests.post(f"{API}/bookings/{bid}/accept", headers=worker_headers, timeout=10)
    assert r.status_code == 200, f"accept booking failed: {r.status_code} {r.text}"
    assert r.json()["worker_id"], "booking has no worker_id after accept — call cannot start"
    return bid


@pytest.fixture(scope="module")
def booking_id(consumer_auth, worker_auth):
    return _create_and_assign_booking(auth_headers(consumer_auth), auth_headers(worker_auth))


# ============================================================
# Happy path: consumer calls -> worker joins -> either ends it
# ============================================================
class TestCallLifecycle:
    def test_01_consumer_starts_call(self, booking_id, consumer_auth):
        ch = auth_headers(consumer_auth)
        r = requests.post(f"{API}/bookings/{booking_id}/call/start", headers=ch, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["call_session_id"]
        assert body["dyte_meeting_id"]
        assert body["dyte_auth_token"], "no auth token returned — frontend cannot join the meeting without this"
        TestCallLifecycle.call_session_id = body["call_session_id"]
        TestCallLifecycle.meeting_id = body["dyte_meeting_id"]

    def test_02_worker_joins_call(self, booking_id, worker_auth):
        wh = auth_headers(worker_auth)
        csid = TestCallLifecycle.call_session_id
        r = requests.post(f"{API}/bookings/{booking_id}/call/{csid}/join", headers=wh, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dyte_auth_token"]
        # Same meeting — join must not spin up a second meeting for the same call.
        assert body["dyte_meeting_id"] == TestCallLifecycle.meeting_id

    def test_03_redial_reuses_the_same_meeting(self, booking_id, consumer_auth):
        """A second /call/start on the same booking (e.g. the call dropped and
        the consumer redials) should reuse the existing meeting rather than
        create a new one, per the comment in calls.py:start_call."""
        ch = auth_headers(consumer_auth)
        r = requests.post(f"{API}/bookings/{booking_id}/call/start", headers=ch, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["dyte_meeting_id"] == TestCallLifecycle.meeting_id

    def test_04_end_call_marks_ended_with_duration(self, booking_id, worker_auth):
        wh = auth_headers(worker_auth)
        csid = TestCallLifecycle.call_session_id
        r = requests.post(
            f"{API}/bookings/{booking_id}/call/{csid}/end",
            headers=wh,
            json={"end_reason": "completed"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # callee (worker) had joined in test_02, so this must resolve to
        # "ended" (not "missed") and carry a duration.
        assert body["status"] == "ended"
        assert body["duration_seconds"] is not None
        assert body["ended_at"]

    def test_05_ending_an_already_ended_call_is_idempotent(self, booking_id, worker_auth):
        wh = auth_headers(worker_auth)
        csid = TestCallLifecycle.call_session_id
        r = requests.post(
            f"{API}/bookings/{booking_id}/call/{csid}/end",
            headers=wh,
            json={"end_reason": "completed"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        # Status must not flip or duration change on a second /end call.
        assert r.json()["status"] == "ended"


# ============================================================
# Missed call: caller ends before the callee ever joins
# ============================================================
class TestMissedCall:
    def test_missed_call_when_callee_never_joined(self, consumer_auth, worker_auth):
        bid = _create_and_assign_booking(auth_headers(consumer_auth), auth_headers(worker_auth))
        ch = auth_headers(consumer_auth)
        r = requests.post(f"{API}/bookings/{bid}/call/start", headers=ch, timeout=10)
        assert r.status_code == 200, r.text
        csid = r.json()["call_session_id"]

        # Caller hangs up without the worker ever joining.
        r = requests.post(
            f"{API}/bookings/{bid}/call/{csid}/end",
            headers=ch,
            json={"end_reason": "no_answer"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "missed"
        assert body["duration_seconds"] is None


# ============================================================
# Authorization: only the two parties to the booking may touch the call
# ============================================================
class TestCallAuthorization:
    def test_third_party_cannot_start_call(self, consumer_auth, worker_auth, admin_ops_auth):
        bid = _create_and_assign_booking(auth_headers(consumer_auth), auth_headers(worker_auth))
        ah = auth_headers(admin_ops_auth)
        r = requests.post(f"{API}/bookings/{bid}/call/start", headers=ah, timeout=10)
        assert r.status_code == 403, r.text

    def test_call_start_without_assigned_worker_is_rejected(self, consumer_auth):
        ch = auth_headers(consumer_auth)
        svcs = requests.get(f"{API}/services", timeout=10).json()
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        payload = {
            "patient_id": patients[0]["id"],
            "service_id": svcs[0]["id"],
            "scheduled_date": (date.today() + timedelta(days=1)).isoformat(),
            "scheduled_start_time": "12:00:00",
            "address": {"line1": "Unassigned Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
            "latitude": "19.0760",
            "longitude": "72.8777",
            "is_urgent": False,
        }
        r = requests.post(f"{API}/bookings/", headers=ch, json=payload, timeout=10)
        assert r.status_code == 200, r.text
        bid = r.json()["id"]
        # No worker assigned yet -> call/start must fail, not silently create
        # a meeting nobody can be rung for.
        r = requests.post(f"{API}/bookings/{bid}/call/start", headers=ch, timeout=10)
        assert r.status_code == 409, r.text

    def test_third_party_cannot_join_call(self, consumer_auth, worker_auth, admin_ops_auth):
        bid = _create_and_assign_booking(auth_headers(consumer_auth), auth_headers(worker_auth))
        ch = auth_headers(consumer_auth)
        r = requests.post(f"{API}/bookings/{bid}/call/start", headers=ch, timeout=10)
        csid = r.json()["call_session_id"]

        ah = auth_headers(admin_ops_auth)
        r = requests.post(f"{API}/bookings/{bid}/call/{csid}/join", headers=ah, timeout=10)
        assert r.status_code == 403, r.text

    def test_unknown_call_session_returns_404(self, consumer_auth, worker_auth):
        bid = _create_and_assign_booking(auth_headers(consumer_auth), auth_headers(worker_auth))
        ch = auth_headers(consumer_auth)
        fake_csid = "00000000-0000-0000-0000-000000000000"
        r = requests.post(f"{API}/bookings/{bid}/call/{fake_csid}/join", headers=ch, timeout=10)
        assert r.status_code == 404, r.text


# ============================================================
# Push token registration (feeds the FCM/APNs ring path in _ring_callee)
# ============================================================
class TestDeviceRegistration:
    def test_register_fcm_token(self, worker_auth):
        wh = auth_headers(worker_auth)
        r = requests.post(
            f"{API}/notifications/devices",
            headers=wh,
            json={"device_id": "pytest-device-1", "fcm_token": "fake-fcm-token-abc", "platform": "android"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["registered"] is True

    def test_register_requires_at_least_one_token(self, worker_auth):
        wh = auth_headers(worker_auth)
        r = requests.post(
            f"{API}/notifications/devices",
            headers=wh,
            json={"device_id": "pytest-device-2"},
            timeout=10,
        )
        assert r.status_code == 400, r.text

    def test_unregister_device_clears_tokens(self, worker_auth):
        wh = auth_headers(worker_auth)
        r = requests.delete(f"{API}/notifications/devices/pytest-device-1", headers=wh, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["unregistered"] is True