"""Live-server tests for the booking / payment / visit-start / report hardening.

These need a running API + Postgres + Redis, exactly like the rest of tests/.

    pytest tests/test_hardening_integration.py -v

GROUP A (expired slots, time buckets, report immutability) runs against the
normal test server (EXPO_PUBLIC_BACKEND_URL).

GROUP B (production-strict visit start: OTP + location + en-route) needs a
SECOND server started with the strict switches, on another port, against the
same test database:

    $env:ENFORCE_VISIT_START_GEOFENCE="true"
    $env:ALLOW_LEGACY_CHECKIN="false"
    uvicorn server:app --port 8003            # in its own terminal
    $env:STRICT_MODE_URL="http://127.0.0.1:8003"
    pytest tests/test_hardening_integration.py -v -k Strict

Without STRICT_MODE_URL group B is skipped (it can never pass on the relaxed
server, whose legacy /checkin and disabled geofence are test-only switches).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import requests

from tests.conftest import (
    API,
    CONSUMER_PHONE,
    TEST_DB_DSN,
    WORKER_PHONE,
    _release_worker_schedule,
)

STRICT_URL = os.environ.get("STRICT_MODE_URL", "").rstrip("/")
STRICT_API = f"{STRICT_URL}/api" if STRICT_URL else ""

NEAR = {"latitude": 19.0760, "longitude": 72.8777}      # the booking's address
FAR = {"latitude": 19.2000, "longitude": 72.8777}       # ~14 km away


# ----------------------------------------------------------------- helpers
def _sql(query: str, params: tuple = ()) -> None:
    import psycopg

    with psycopg.connect(TEST_DB_DSN, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


def _login_at(api: str, phone: str, role: str) -> dict:
    requests.post(f"{api}/auth/otp/send", json={"phone_e164": phone, "role": role}, timeout=15)
    r = requests.post(
        f"{api}/auth/otp/verify",
        json={"phone_e164": phone, "code": "123456", "role": role,
              "device_id": "pytest-hardening", "device_platform": "cli"},
        timeout=15,
    )
    assert r.status_code == 200, f"login {phone}: {r.status_code} {r.text}"
    return r.json()


def _h(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}


def _detail(resp: requests.Response) -> dict:
    """The {code, message, ...} payload, however FastAPI nested it."""
    body = resp.json()
    d = body.get("detail", body)
    return d if isinstance(d, dict) else {"message": d}


def _patient_id(api: str, ch: dict) -> str:
    patients = requests.get(f"{api}/patients", headers=ch, timeout=15).json()
    if patients:
        return patients[0]["id"]
    r = requests.post(
        f"{api}/patients", headers=ch, timeout=15,
        json={"full_name": "Hardening Patient", "date_of_birth": "1980-01-01",
              "gender": "male", "relationship_to_consumer": "self"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _create_booking(api: str, ch: dict, *, days: int, start: str = "10:30:00") -> str:
    svc_id = requests.get(f"{api}/services", timeout=15).json()[0]["id"]
    r = requests.post(
        f"{api}/bookings/", headers=ch, timeout=15,
        json={
            "patient_id": _patient_id(api, ch),
            "service_id": svc_id,
            "scheduled_date": (date.today() + timedelta(days=days)).isoformat(),
            "scheduled_start_time": start,
            "address": {"line1": "Hardening Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
            **NEAR,
            "is_urgent": False,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _grant_consents(api: str, ch: dict, bid: str) -> None:
    pid = _patient_id(api, ch)
    for consent_type in ("service", "medication"):
        cr = requests.post(
            f"{api}/consents", headers=ch, timeout=15,
            json={"patient_id": pid, "booking_id": bid, "consent_type": consent_type,
                  "consented_by_name": "Hardening Family", "relationship_to_patient": "self"},
        )
        assert cr.status_code == 200, cr.text


def _assigned_booking(api: str, ch: dict, wh: dict, *, days: int, start: str) -> str:
    """A booking that is paid, consented and accepted by the shared test worker."""
    _release_worker_schedule(WORKER_PHONE)
    bid = _create_booking(api, ch, days=days, start=start)
    _sql("UPDATE bookings SET status='confirmed', payment_status='captured', worker_id=NULL WHERE id=%s", (bid,))
    _grant_consents(api, ch, bid)
    # the nurse must be where the booking is for any location-aware rule
    requests.post(f"{api}/workers/me/location", headers=wh, json=NEAR, timeout=15)
    r = requests.post(f"{api}/bookings/{bid}/accept", headers=wh, timeout=15)
    assert r.status_code == 200, f"accept: {r.status_code} {r.text}"
    return bid


# ------------------------------------------------------------------ GROUP A
@pytest.fixture(scope="module")
def consumer():
    return _login_at(API, CONSUMER_PHONE, "consumer")


@pytest.fixture(scope="module")
def worker():
    return _login_at(API, WORKER_PHONE, "worker")


def _expire(bid: str) -> None:
    """Move the slot two days into the past, whatever the schedule-limit setting."""
    _sql("UPDATE bookings SET scheduled_date = current_date - 2 WHERE id=%s", (bid,))


class TestExpiredSlotPayments:
    def test_future_slot_can_still_get_an_order(self, consumer):
        """Control: the guard must not block a normal, future booking."""
        bid = _create_booking(API, _h(consumer), days=3)
        r = requests.post(f"{API}/payments/order", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["razorpay_order_id"]

    def test_order_for_an_expired_slot_is_rejected_by_the_backend(self, consumer):
        bid = _create_booking(API, _h(consumer), days=3)
        _expire(bid)
        r = requests.post(f"{API}/payments/order", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        assert r.status_code == 409, r.text
        assert _detail(r)["code"] == "BOOKING_SLOT_EXPIRED"

    def test_cash_selection_for_an_expired_slot_is_rejected(self, consumer):
        bid = _create_booking(API, _h(consumer), days=3)
        _expire(bid)
        r = requests.post(f"{API}/payments/cash/select", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        assert r.status_code == 409, r.text
        assert _detail(r)["code"] == "BOOKING_SLOT_EXPIRED"

    def test_cancelled_booking_cannot_be_paid(self, consumer):
        bid = _create_booking(API, _h(consumer), days=4)
        c = requests.post(f"{API}/bookings/{bid}/cancel", headers=_h(consumer), json={"reason": "test"}, timeout=15)
        assert c.status_code == 200, c.text
        r = requests.post(f"{API}/payments/order", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        assert r.status_code == 409, r.text
        assert _detail(r)["code"] == "BOOKING_CANCELLED"

    def test_second_pay_tap_reuses_the_order_instead_of_crashing(self, consumer):
        """Regression: the reuse branch used to read an expired ORM object -> 500."""
        bid = _create_booking(API, _h(consumer), days=3)
        first = requests.post(f"{API}/payments/order", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        second = requests.post(f"{API}/payments/order", headers=_h(consumer), json={"booking_id": bid}, timeout=15)
        assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
        assert first.json()["razorpay_order_id"] == second.json()["razorpay_order_id"]


class TestUpcomingBucket:
    def _bucket_of(self, consumer, bid: str) -> dict:
        rows = requests.get(f"{API}/bookings/consumer", headers=_h(consumer), timeout=20).json()
        row = next((b for b in rows if b["id"] == bid), None)
        assert row is not None, f"booking {bid} missing from /bookings/consumer"
        return row

    def test_future_unpaid_booking_is_upcoming(self, consumer):
        bid = _create_booking(API, _h(consumer), days=3)
        row = self._bucket_of(consumer, bid)
        assert row["time_bucket"] == "upcoming" and row["is_expired"] is False

    def test_expired_unpaid_booking_is_never_upcoming(self, consumer):
        bid = _create_booking(API, _h(consumer), days=3)
        _expire(bid)
        row = self._bucket_of(consumer, bid)
        assert row["time_bucket"] == "past", row
        assert row["is_expired"] is True

    def test_bucket_filter_excludes_expired_from_upcoming(self, consumer):
        bid = _create_booking(API, _h(consumer), days=3)
        _expire(bid)
        rows = requests.get(f"{API}/bookings/consumer", params={"bucket": "upcoming"},
                            headers=_h(consumer), timeout=20).json()
        assert bid not in [b["id"] for b in rows]


class TestReportImmutability:
    """One self-contained flow (no state shared between test functions)."""

    def test_finalized_report_and_vitals_cannot_be_changed(self, consumer, worker):
        ch, wh = _h(consumer), _h(worker)
        bid = _assigned_booking(API, ch, wh, days=5, start="12:00:00")

        ci = requests.post(f"{API}/visits/{bid}/checkin", headers=wh, json=NEAR, timeout=15)
        if ci.status_code == 403 and _detail(ci).get("code") == "OTP_REQUIRED":
            pytest.skip("server has the legacy /checkin switched off (strict mode)")
        assert ci.status_code == 200, ci.text

        v = requests.post(f"{API}/visits/{bid}/vitals", headers=wh, timeout=15,
                          json={"bp_systolic": 120, "bp_diastolic": 80, "pulse": 72, "spo2": 98})
        assert v.status_code == 200, v.text

        saved = requests.put(f"{API}/visits/{bid}/report", headers=wh, timeout=15,
                             json={"care_notes": "draft notes", "family_summary": "draft summary"})
        assert saved.status_code == 200, saved.text  # a DRAFT is editable

        out = requests.post(f"{API}/visits/{bid}/checkout", headers=wh, timeout=15,
                            json={**NEAR, "family_summary": "Final summary", "care_notes": "Final notes"})
        assert out.status_code == 200, out.text

        # ---- finalized: every content write path must now refuse -----------
        edit = requests.put(f"{API}/visits/{bid}/report", headers=wh, timeout=15,
                            json={"care_notes": "tampered", "family_summary": "tampered"})
        assert edit.status_code == 409, edit.text
        assert _detail(edit)["code"] == "REPORT_FINALIZED"

        late_vitals = requests.post(f"{API}/visits/{bid}/vitals", headers=wh, timeout=15,
                                    json={"pulse": 65})
        assert late_vitals.status_code == 409, late_vitals.text
        assert _detail(late_vitals)["code"] == "REPORT_FINALIZED"

        late_checklist = requests.post(f"{API}/visits/{bid}/checklist", headers=wh, timeout=15,
                                       json={"responses": {"hand_hygiene": False}})
        assert late_checklist.status_code == 409, late_checklist.text

        # ---- and what was finalized is exactly what was submitted ----------
        rep = requests.get(f"{API}/visits/{bid}/report", headers=wh, timeout=15).json()
        assert rep["care_notes"] == "Final notes"
        assert rep["family_summary"] == "Final summary"
        assert rep["is_final"] is True and rep["report_finalized_at"]


class TestVitalsIntegrity:
    def test_empty_and_impossible_readings_are_rejected_but_critical_partial_bp_is_saved(self, consumer, worker):
        ch, wh = _h(consumer), _h(worker)
        bid = _assigned_booking(API, ch, wh, days=6, start="13:00:00")
        ci = requests.post(f"{API}/visits/{bid}/checkin", headers=wh, json=NEAR, timeout=15)
        if ci.status_code == 403 and _detail(ci).get("code") == "OTP_REQUIRED":
            pytest.skip("server has the legacy /checkin switched off (strict mode)")
        assert ci.status_code == 200, ci.text

        empty = requests.post(f"{API}/visits/{bid}/vitals", headers=wh, json={}, timeout=15)
        assert empty.status_code == 422 and _detail(empty)["code"] == "VITALS_INVALID"

        zero = requests.post(f"{API}/visits/{bid}/vitals", headers=wh, json={"pulse": 0}, timeout=15)
        assert zero.status_code == 422 and _detail(zero)["code"] == "VITALS_INVALID"

        # patient safety: a critical SpO2 with only a systolic BP must be SAVED
        crit = requests.post(f"{API}/visits/{bid}/vitals", headers=wh, timeout=15,
                             json={"spo2": 80, "bp_systolic": 120, "pulse": 80})
        assert crit.status_code == 200, crit.text


class TestAmarFindingsLive:
    """Live regression tests for the two UAT findings (not run in CI sandbox)."""

    def test_booking_with_zero_coordinates_is_refused_with_a_plain_message(self, consumer):
        ch = _h(consumer)
        svc_id = requests.get(f"{API}/services", timeout=15).json()[0]["id"]
        r = requests.post(
            f"{API}/bookings/", headers=ch, timeout=15,
            json={
                "patient_id": _patient_id(API, ch),
                "service_id": svc_id,
                "scheduled_date": (date.today() + timedelta(days=3)).isoformat(),
                "scheduled_start_time": "10:30:00",
                "address": {"line1": "No GPS Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
                "latitude": 0, "longitude": 0, "is_urgent": False,
            },
        )
        assert r.status_code == 400, r.text
        msg = r.json().get("detail")
        msg = msg if isinstance(msg, str) else str(msg)
        assert "location of this address" in msg and "latitude" not in msg.lower(), msg

    def test_a_qualified_nurse_can_opt_out_and_back_in(self, worker):
        wh = _h(worker)
        items = requests.get(f"{API}/workers/me/service-eligibility", headers=wh, timeout=20).json()
        item = next((i for i in items if i["can_opt_in"] and i["preference_status"] == "OPTED_IN"), None)
        if item is None:
            pytest.skip("test nurse has no qualified item currently opted in")
        body = {"target_type": item["target_type"], "target_id": item["id"]}
        try:
            out = requests.put(f"{API}/workers/me/service-preferences", headers=wh, timeout=15,
                               json={**body, "preference_status": "OPTED_OUT"})
            assert out.status_code == 200, out.text
            assert out.json()["preference_status"] == "OPTED_OUT"
            back = requests.put(f"{API}/workers/me/service-preferences", headers=wh, timeout=15,
                                json={**body, "preference_status": "OPTED_IN"})
            assert back.status_code == 200, back.text
            assert back.json()["preference_status"] == "OPTED_IN"
        finally:  # leave the shared test nurse the way we found her
            requests.put(f"{API}/workers/me/service-preferences", headers=wh, timeout=15,
                         json={**body, "preference_status": "OPTED_IN"})


# ------------------------------------------------------------------ GROUP B
strict_only = pytest.mark.skipif(
    not STRICT_URL,
    reason="set STRICT_MODE_URL to a server started with ENFORCE_VISIT_START_GEOFENCE=true "
           "and ALLOW_LEGACY_CHECKIN=false (see module docstring)",
)


def _go_en_route(ch_wh_bid) -> None:
    _, wh, bid = ch_wh_bid
    chk = requests.post(
        f"{STRICT_API}/workers/me/alertness-checks", headers=wh, timeout=15,
        json={"booking_id": bid, "round_reaction_times_ms": [300, 310, 305, 295, 300],
              "declaration_confirmed": True},
    )
    assert chk.status_code == 201, chk.text
    r = requests.post(f"{STRICT_API}/bookings/{bid}/en-route", headers=wh, timeout=15)
    assert r.status_code == 200, f"en-route: {r.status_code} {r.text}"


def _visit_code(ch: dict, bid: str) -> str:
    r = requests.post(f"{STRICT_API}/visits/{bid}/generate-start-otp", headers=ch, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["otp"]


def _start(wh: dict, bid: str, otp: str, loc: dict) -> requests.Response:
    return requests.post(f"{STRICT_API}/visits/{bid}/verify-start-otp", headers=wh, timeout=15,
                         json={"otp": otp, **loc})


@pytest.fixture()
def strict_ctx():
    ch = _h(_login_at(STRICT_API, CONSUMER_PHONE, "consumer"))
    wh = _h(_login_at(STRICT_API, WORKER_PHONE, "worker"))
    bid = _assigned_booking(STRICT_API, ch, wh, days=7 + int(uuid4().int % 20), start="09:00:00")
    return ch, wh, bid


@strict_only
class TestStrictVisitStart:
    def test_legacy_checkin_bypass_is_closed(self, strict_ctx):
        ch, wh, bid = strict_ctx
        r = requests.post(f"{STRICT_API}/visits/{bid}/checkin", headers=wh, json=NEAR, timeout=15)
        assert r.status_code == 403, r.text
        assert _detail(r)["code"] == "OTP_REQUIRED"

    def test_correct_otp_is_useless_before_the_nurse_went_en_route(self, strict_ctx):
        ch, wh, bid = strict_ctx
        r = _start(wh, bid, _visit_code(ch, bid), NEAR)
        assert r.status_code == 409, r.text
        assert _detail(r)["code"] == "VISIT_NOT_EN_ROUTE"

    def test_correct_otp_from_far_away_is_refused_and_the_code_survives(self, strict_ctx):
        ch, wh, bid = strict_ctx
        _go_en_route(strict_ctx)
        otp = _visit_code(ch, bid)

        far = _start(wh, bid, otp, FAR)
        assert far.status_code == 403, far.text
        d = _detail(far)
        assert d["code"] == "NOT_AT_CUSTOMER_LOCATION" and d["distance_m"] > 1000

        # the location check comes BEFORE the OTP, so the same code still works at the door
        ok = _start(wh, bid, otp, NEAR)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "in_progress"

    def test_no_gps_fix_cannot_start_the_visit(self, strict_ctx):
        ch, wh, bid = strict_ctx
        _go_en_route(strict_ctx)
        r = _start(wh, bid, _visit_code(ch, bid), {"latitude": 0.0, "longitude": 0.0})
        assert r.status_code == 403, r.text
        assert _detail(r)["code"] == "NURSE_LOCATION_REQUIRED"

    def test_wrong_code_is_counted_and_does_not_start_anything(self, strict_ctx):
        ch, wh, bid = strict_ctx
        _go_en_route(strict_ctx)
        real = _visit_code(ch, bid)
        wrong = "0000" if real != "0000" else "1111"
        r = _start(wh, bid, wrong, NEAR)
        assert r.status_code == 400, r.text
        d = _detail(r)
        assert d["code"] == "OTP_INVALID" and d["attempts_remaining"] == 4
        # and the real code still works afterwards
        assert _start(wh, bid, real, NEAR).status_code == 200

    def test_code_is_single_use_and_no_second_code_appears_after_start(self, strict_ctx):
        ch, wh, bid = strict_ctx
        _go_en_route(strict_ctx)
        otp = _visit_code(ch, bid)
        assert _start(wh, bid, otp, NEAR).status_code == 200

        again = _start(wh, bid, otp, NEAR)
        assert again.status_code == 400, again.text
        assert _detail(again)["code"] == "VISIT_ALREADY_STARTED"

        # the "extra timed code after Visit Start" bug: a started visit mints no new code
        extra = requests.post(f"{STRICT_API}/visits/{bid}/generate-start-otp", headers=ch, timeout=15)
        assert extra.status_code == 400, extra.text
        assert _detail(extra)["code"] == "BOOKING_NOT_READY"

    def test_another_nurse_cannot_use_the_customers_code(self, strict_ctx):
        ch, wh, bid = strict_ctx
        _go_en_route(strict_ctx)
        otp = _visit_code(ch, bid)
        other = _h(_login_at(STRICT_API, "+919999000007", "worker"))
        r = _start(other, bid, otp, NEAR)
        assert r.status_code in (403, 404), r.text
