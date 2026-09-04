"""Phase 3 — Nurse Flow Integration end-to-end backend tests.

Covers the full nurse lifecycle as outlined in the Phase 3 review_request:
auth -> workers/me -> kit list/update -> earnings -> training modules ->
bookings/worker -> create consumer booking -> direct SQL update to 'confirmed' ->
bookings/worker/new-requests -> accept -> checkin (idempotent) -> vitals ->
medications -> checklist -> manual escalate -> checkout (idempotent) ->
GET /visits/{id} -> GET /escalations/?booking_id=... -> GET /care-notes/patient/{id}.

Also verifies:
- escalation level enum: watch | inform_family | contact_doctor | emergency
- availability enum: online | offline | busy | on_leave
"""
from __future__ import annotations

import os
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
import requests

from tests.conftest import API, _login, auth_headers

# Direct-SQL DSN for status flips that have no API endpoint. Overridable via
# the existing PG_TEST_DSN env var (already set by CI and used elsewhere in
# this project's tooling) so a local Postgres on a non-default host port
# (e.g. a Docker mapping like 55433->5432) works without touching this file
# again; falls back to the exact value that matched the CI Postgres service
# container when PG_TEST_DSN isn't set.
PG_DSN = os.environ.get(
    "PG_TEST_DSN",
    "host=127.0.0.1 port=5432 user=nurseconnect password=nurseconnect dbname=nurseconnect",
)


def _sql(query: str, params: tuple = ()) -> None:
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


def _fetchone(query: str, params: tuple = ()):
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def _unique_future_slot(base_days: int = 5000) -> tuple[str, str]:
    """(scheduled_date, scheduled_start_time) pair that won't collide with
    another booking — this suite's own bookings included — even across
    repeated runs on a persistent DB. A fixed date+time (as several tests
    here originally used) eventually collides with a worker's own
    already-assigned visit from an earlier run and trips the very real
    worker_has_schedule_conflict check for a scenario that has nothing to
    do with what's actually under test.

    Pushes far into the future and picks the date offset and time-of-day
    with two INDEPENDENT random.randint() calls (Python's random module is
    seeded from OS entropy at interpreter start, not from wall-clock alone)
    over a wide space (3650 days x 840 minute-slots ~= 3M combinations) —
    collision-resistant across effectively any number of repeated runs.
    An earlier version of this helper derived both the date offset and the
    time-of-day from the same millisecond-of-second wall-clock value, which
    gave the pair a combined repeat cycle of only ~36 seconds — worse than
    the fixed value it replaced once run more than a handful of times.
    worker_has_schedule_conflict itself is untouched either way.
    """
    scheduled_date = (
        date.today() + timedelta(days=base_days + random.randint(0, 3650))
    ).isoformat()
    hour = random.randint(6, 19)
    minute = random.randint(0, 59)
    scheduled_start_time = f"{hour:02d}:{minute:02d}:00"
    return scheduled_date, scheduled_start_time


# --------- Module-scoped auth + shared context ---------
@pytest.fixture(scope="module")
def nurse_auth():
    return _login("+919999000002", "worker")


@pytest.fixture(scope="module")
def family_auth():
    return _login("+919999000001", "consumer")


@pytest.fixture(scope="module")
def ctx(nurse_auth, family_auth):
    """Build a confirmed booking owned by consumer and assignable by nurse."""
    ch = auth_headers(family_auth)
    wh = auth_headers(nurse_auth)

    svcs = requests.get(f"{API}/services", timeout=10).json()
    svc_id = svcs[0]["id"]
    patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
    pid = patients[0]["id"]

    scheduled_date, scheduled_start_time = _unique_future_slot()
    payload = {
        "patient_id": pid,
        "service_id": svc_id,
        "scheduled_date": scheduled_date,
        "scheduled_start_time": scheduled_start_time,
        "address": {"line1": "Phase3 Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = requests.post(f"{API}/bookings/", headers=ch, json=payload, timeout=10)
    assert r.status_code == 200, r.text
    booking = r.json()
    bid = booking["id"]
    # Bypass payment by setting booking confirmed + payment captured (mocked provider)
    _sql(
        "UPDATE bookings SET status='confirmed', payment_status='captured', worker_id=NULL WHERE id=%s",
        (bid,),
    )
    # Patch 5A gates check-in/checklist behind service consent and
    # medications behind medication consent (see app/api/v1/visits.py
    # require_consent calls) — nothing creates that consent automatically,
    # a real consumer grants it explicitly via POST /care/consents. Grant
    # both, scoped to this booking, so the visit lifecycle below can
    # actually proceed through check-in and medications like a real flow
    # would, instead of asserting around a step this suite never took.
    for consent_type in ("service", "medication"):
        cr = requests.post(
            f"{API}/care/consents",
            headers=ch,
            json={
                "patient_id": pid,
                "booking_id": bid,
                "consent_type": consent_type,
                "consented_by_name": "Phase3 Test Family",
                "relationship_to_patient": "self",
            },
            timeout=10,
        )
        assert cr.status_code == 200, cr.text
    return {"ch": ch, "wh": wh, "bid": bid, "pid": pid, "booking": booking}


# --------- 1. Auth + workers/me ---------
class TestAuthAndProfile:
    def test_send_otp_and_verify_for_worker(self):
        r = requests.post(
            f"{API}/auth/otp/send",
            json={"phone_e164": "+919999000002", "role": "worker", "purpose": "login"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("sent") is True
        # verify
        r = requests.post(
            f"{API}/auth/otp/verify",
            json={"phone_e164": "+919999000002", "code": "123456", "role": "worker"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "tokens" in data and "access_token" in data["tokens"]
        assert data["user"]["role"] == "worker"

    def test_workers_me_returns_profile(self, nurse_auth):
        r = requests.get(f"{API}/workers/me", headers=auth_headers(nurse_auth), timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "id" in body
        assert "availability" in body


# --------- 2. Kit ---------
class TestKit:
    @pytest.fixture(scope="class", autouse=True)
    def _seed_kit_items(self, nurse_auth):
        """Nothing in app/ code ever provisions WorkerKitItem rows for a
        worker (no seed step, no auto-create on approval) — GET /me/kit
        just lists whatever exists. Seed a couple of baseline items
        directly, once, so these tests exercise the real GET/PUT
        /workers/me/kit endpoints against realistic data instead of
        changing application logic to add auto-provisioning that isn't
        this suite's concern. Idempotent: only inserts if this worker has
        no kit items yet, so repeated runs on a persistent DB don't pile
        up duplicates.
        """
        row = _fetchone(
            "SELECT wp.id FROM worker_profiles wp JOIN users u ON u.id = wp.user_id "
            "WHERE u.phone_e164 = %s",
            ("+919999000002",),
        )
        assert row, "nurse worker profile not found for kit seeding"
        worker_id = row[0]

        existing = _fetchone(
            "SELECT COUNT(*) FROM worker_kit_items WHERE worker_id = %s", (worker_id,)
        )
        if not existing or existing[0] == 0:
            for item_code, item_name in (
                ("BP_MONITOR", "BP Monitor"),
                ("THERMOMETER", "Digital Thermometer"),
            ):
                _sql(
                    "INSERT INTO worker_kit_items (id, worker_id, item_code, item_name, is_present) "
                    "VALUES (%s, %s, %s, %s, true)",
                    (str(uuid.uuid4()), worker_id, item_code, item_name),
                )
            # Verify from a fresh connection immediately, so a failure here
            # points straight at the actual mechanism (wrong worker_id, a
            # trigger/constraint silently rejecting the row, etc.) instead
            # of surfacing three test methods downstream as a generic
            # "empty kit" assertion with no further information.
            verify = _fetchone(
                "SELECT COUNT(*) FROM worker_kit_items WHERE worker_id = %s", (worker_id,)
            )
            assert verify and verify[0] >= 2, (
                f"kit item seeding did not take effect: worker_id={worker_id!r}, "
                f"post-insert count={verify!r}"
            )

    def test_list_kit_has_items(self, nurse_auth):
        r = requests.get(f"{API}/workers/me/kit", headers=auth_headers(nurse_auth), timeout=10)
        assert r.status_code == 200, r.text
        items = r.json()
        assert isinstance(items, list) and len(items) >= 1
        assert {"id", "item_code", "item_name", "is_present"}.issubset(items[0].keys())

    def test_update_kit_is_present_false(self, nurse_auth):
        h = auth_headers(nurse_auth)
        kits = requests.get(f"{API}/workers/me/kit", headers=h, timeout=10).json()
        kid = kits[0]["id"]
        r = requests.put(
            f"{API}/workers/me/kit/{kid}?is_present=false", headers=h, timeout=10
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("kit_complete") is False
        # restore
        r2 = requests.put(
            f"{API}/workers/me/kit/{kid}?is_present=true", headers=h, timeout=10
        )
        assert r2.status_code == 200


# --------- 3. Earnings ---------
class TestEarnings:
    def test_earnings_shape(self, nurse_auth):
        r = requests.get(f"{API}/workers/me/earnings", headers=auth_headers(nurse_auth), timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert {"total_paid", "total_pending", "payouts"}.issubset(body.keys())
        assert isinstance(body["payouts"], list)


# --------- 4. Training ---------
class TestTraining:
    def test_modules_list(self, nurse_auth):
        r = requests.get(f"{API}/training/modules", headers=auth_headers(nurse_auth), timeout=10)
        assert r.status_code == 200, r.text
        mods = r.json()
        assert isinstance(mods, list) and len(mods) >= 1
        assert {"id", "code", "title", "completed"}.issubset(mods[0].keys())


# --------- 5. Worker bookings list ---------
class TestWorkerBookings:
    def test_worker_bookings(self, nurse_auth):
        r = requests.get(f"{API}/bookings/worker", headers=auth_headers(nurse_auth), timeout=10)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_worker_new_requests_contains_our_booking(self, nurse_auth, ctx):
        r = requests.get(
            f"{API}/bookings/worker/new-requests", headers=auth_headers(nurse_auth), timeout=10
        )
        assert r.status_code == 200, r.text
        items = r.json()
        ids = [b["id"] for b in items]
        assert ctx["bid"] in ids, f"booking {ctx['bid']} should be in new-requests, got {ids[:5]}"


# --------- 6. Visit lifecycle ---------
class TestVisitLifecycle:
    def test_01_accept(self, ctx):
        r = requests.post(f"{API}/bookings/{ctx['bid']}/accept", headers=ctx["wh"], timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "assigned"
        assert body["worker_id"]

    def test_02_checkin(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/checkin",
            headers=ctx["wh"],
            json={"latitude": "19.0760", "longitude": "72.8777"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "in_progress"
        assert body["check_in_at"]

    def test_03_checkin_idempotent_rejects_double(self, ctx):
        """Calling checkin twice should NOT corrupt state — must 400 with 'Already checked in'."""
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/checkin",
            headers=ctx["wh"],
            json={"latitude": "19.0760", "longitude": "72.8777"},
            timeout=10,
        )
        assert r.status_code == 400, r.text
        assert "checked in" in r.text.lower()

    def test_04_vitals(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/vitals",
            headers=ctx["wh"],
            json={
                "bp_systolic": 120,
                "bp_diastolic": 80,
                "pulse": 72,
                "spo2": 98,
                "temperature_f": 98.6,
                "blood_sugar_random": 110,
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bp_systolic"] == 120
        assert body["bp_diastolic"] == 80

    def test_05_medications(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/medications",
            headers=ctx["wh"],
            json={
                "drug_name": "Paracetamol",
                "dose_amount": "500mg",
                "allergy_check_done": True,
                "allergy_confirmed_clear": True,
                "patient_identified": True,
                "expiry_checked": True,
                "administered_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("id")

    def test_06_checklist(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/checklist",
            headers=ctx["wh"],
            json={"responses": {"hand_hygiene": True, "consent_taken": True, "vitals_recorded": True}},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("checklist_responses", {}).get("hand_hygiene") is True

    def test_07_manual_escalate_inform_family(self, ctx):
        r = requests.post(
            f"{API}/bookings/{ctx['bid']}/escalate",
            headers=ctx["wh"],
            json={
                "level": "inform_family",
                "trigger_type": "manual",
                "notes": "Family wants an update",
                "trigger_details": {"reason": "family_request"},
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["level"] == "inform_family"
        assert body["status"] == "open"

    def test_08_checkout(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/checkout",
            headers=ctx["wh"],
            json={
                "latitude": "19.0760",
                "longitude": "72.8777",
                "family_summary": "All good",
                "care_notes": "Patient stable",
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"
        assert body["documentation_complete"] is True
        assert body["check_out_at"]

    def test_09_checkout_idempotent_rejects_double(self, ctx):
        r = requests.post(
            f"{API}/visits/{ctx['bid']}/checkout",
            headers=ctx["wh"],
            json={
                "latitude": "19.0760",
                "longitude": "72.8777",
                "family_summary": "dup",
                "care_notes": "dup",
            },
            timeout=10,
        )
        assert r.status_code == 400, r.text
        assert "checked out" in r.text.lower()

    def test_10_get_visit_completed(self, ctx):
        r = requests.get(f"{API}/visits/{ctx['bid']}", headers=ctx["wh"], timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"
        assert body["documentation_complete"] is True

    def test_11_escalations_by_booking(self, ctx):
        r = requests.get(
            f"{API}/escalations/?booking_id={ctx['bid']}", headers=ctx["wh"], timeout=10
        )
        assert r.status_code == 200, r.text
        items = r.json()
        assert isinstance(items, list)
        assert any(e["level"] == "inform_family" for e in items), items

    def test_12_care_notes_for_patient(self, ctx, family_auth):
        # Use consumer because worker visibility depends on note flags
        r = requests.get(
            f"{API}/care-notes/patient/{ctx['pid']}",
            headers=auth_headers(family_auth),
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)


# --------- 7. Enum coverage ---------
class TestEnumCoverage:
    @pytest.mark.parametrize("level", ["watch", "inform_family", "contact_doctor", "emergency"])
    def test_all_escalation_levels_accepted(self, nurse_auth, family_auth, level):
        ch = auth_headers(family_auth)
        wh = auth_headers(nurse_auth)
        svcs = requests.get(f"{API}/services", timeout=10).json()
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        scheduled_date, scheduled_start_time = _unique_future_slot()
        bk = requests.post(
            f"{API}/bookings/",
            headers=ch,
            json={
                "patient_id": patients[0]["id"],
                "service_id": svcs[0]["id"],
                "scheduled_date": scheduled_date,
                "scheduled_start_time": scheduled_start_time,
                "address": {"line1": "Lvl", "city": "Mumbai", "state": "MH", "pincode": "400001"},
                "latitude": "19.0760",
                "longitude": "72.8777",
            },
            timeout=10,
        ).json()
        bid = bk["id"]
        _sql(
            "UPDATE bookings SET status='confirmed', payment_status='captured', worker_id=NULL WHERE id=%s",
            (bid,),
        )
        ar = requests.post(f"{API}/bookings/{bid}/accept", headers=wh, timeout=10)
        assert ar.status_code == 200, ar.text
        r = requests.post(
            f"{API}/bookings/{bid}/escalate",
            headers=wh,
            json={"level": level, "trigger_type": "manual", "notes": f"test {level}"},
            timeout=10,
        )
        assert r.status_code == 200, f"level={level} -> {r.status_code} {r.text}"
        assert r.json()["level"] == level

    @pytest.mark.parametrize("av", ["online", "offline", "busy", "on_leave"])
    def test_all_availability_values(self, nurse_auth, av):
        r = requests.put(
            f"{API}/workers/me/availability",
            headers=auth_headers(nurse_auth),
            json={"availability": av},
            timeout=10,
        )
        assert r.status_code == 200, f"availability={av} -> {r.status_code} {r.text}"
        assert r.json()["availability"] == av
        # restore at the end
        if av != "online":
            requests.put(
                f"{API}/workers/me/availability",
                headers=auth_headers(nurse_auth),
                json={"availability": "online"},
                timeout=10,
            )
