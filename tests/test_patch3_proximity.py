"""Patch 3 — Proximity dispatch (Haversine + radius waves) backend tests.

Covers:
- POST /api/workers/me/location (JWT worker auth) + persistence
- 401/403 when called without token or with consumer token
- GET /api/bookings/worker/new-requests returns distance_km
- Acceptance #1: worker inside wave-1 radius sees normal booking (~1.1 km)
- Acceptance #2: worker outside wave-1 radius (~10 km) does NOT see booking
- Acceptance #3: wave bump to 3 → far worker sees the booking
- Acceptance #4: far worker can claim first; near worker gets 409 BOOKING_ALREADY_CLAIMED
- Urgent booking with worker having no coords is excluded
- No paid Google Maps backend APIs in repo

Direct DB access (psycopg2) is used to:
- flip booking status from `pending_payment` → `confirmed`
- bump booking.assignment_wave for Check #3
- temporarily NULL home/current coords for the no-coords urgent test
"""
from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta

import psycopg2
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

CONSUMER_PHONE = "+919999000001"
RIYA_PHONE = "+919999000002"   # ~1 km worker
MEERA_PHONE = "+919999000007"  # ~10 km worker in our tests

CONSUMER_LAT = "18.9430"
CONSUMER_LNG = "72.8235"
RIYA_LAT = 18.953   # ~1.1 km from consumer
RIYA_LNG = 72.8235
MEERA_LAT = 19.033  # ~10 km from consumer
MEERA_LNG = 72.8235


# ---------- helpers ----------
def _login(phone: str, role: str) -> dict:
    r = requests.post(
        f"{API}/auth/login",
        json={"phone_e164": phone, "code": "123456", "role": role},
        timeout=15,
    )
    assert r.status_code == 200, f"login {phone}/{role} failed: {r.status_code} {r.text}"
    body = r.json()
    assert "tokens" in body and "access_token" in body["tokens"], body
    return body


def _h(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}


def _pg_conn():
    return psycopg2.connect(
        host="127.0.0.1", port=5432, dbname="nurseconnect",
        user="nurseconnect", password="nurseconnect",
    )


# ---------- session fixtures ----------
@pytest.fixture(scope="session")
def consumer_auth():
    return _login(CONSUMER_PHONE, "consumer")


@pytest.fixture(scope="session")
def riya_auth():
    return _login(RIYA_PHONE, "worker")


@pytest.fixture(scope="session")
def meera_auth():
    return _login(MEERA_PHONE, "worker")


@pytest.fixture(scope="session")
def general_nursing_service():
    r = requests.get(f"{API}/services", timeout=10)
    assert r.status_code == 200, r.text
    svcs = {s["service_code"]: s for s in r.json()}
    assert "GENERAL_NURSING" in svcs, "GENERAL_NURSING not seeded"
    return svcs["GENERAL_NURSING"]


def _create_confirmed_booking(consumer_auth: dict, service_id: str, *,
                              latitude: str = CONSUMER_LAT,
                              longitude: str = CONSUMER_LNG,
                              is_urgent: bool = False,
                              days_ahead: int = 3) -> str:
    ch = _h(consumer_auth)
    patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
    assert patients, "no seeded patient"
    pid = patients[0]["id"]
    bk = requests.post(
        f"{API}/bookings/",
        headers=ch,
        json={
            "patient_id": pid,
            "service_id": service_id,
            "scheduled_date": (date.today() + timedelta(days=days_ahead)).isoformat(),
            "scheduled_start_time": "10:30:00",
            "address": {"line1": "42 MG Rd", "city": "Mumbai", "state": "MH", "pincode": "400001"},
            "latitude": latitude,
            "longitude": longitude,
            "is_urgent": is_urgent,
        },
        timeout=15,
    )
    assert bk.status_code == 200, bk.text
    bid = bk.json()["id"]
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status='confirmed', worker_id=NULL, assignment_wave=1 WHERE id=%s",
                (bid,),
            )
        conn.commit()
    finally:
        conn.close()
    return bid


# ---------- Health ----------
class TestHealth:
    def test_health_ok(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        # Accept either {"status":"ok"} or nested db/redis flags
        assert body.get("status", "ok") in ("ok", "healthy") or body.get("db") in (True, "ok") or body.get("ok")


# ---------- Login ----------
class TestLogin:
    def test_consumer_login(self):
        body = _login(CONSUMER_PHONE, "consumer")
        assert body["user"]["role"] == "consumer"

    def test_riya_login(self):
        body = _login(RIYA_PHONE, "worker")
        assert body["user"]["role"] == "worker"
        assert "access_token" in body["tokens"]

    def test_meera_login(self):
        body = _login(MEERA_PHONE, "worker")
        assert body["user"]["role"] == "worker"
        assert "access_token" in body["tokens"]


# ---------- POST /workers/me/location ----------
class TestWorkerLocationEndpoint:
    def test_requires_auth_no_token(self):
        r = requests.post(
            f"{API}/workers/me/location",
            json={"latitude": RIYA_LAT, "longitude": RIYA_LNG, "accuracy": 10},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"expected 401/403 without token, got {r.status_code} {r.text}"

    def test_requires_worker_role(self, consumer_auth):
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(consumer_auth),
            json={"latitude": RIYA_LAT, "longitude": RIYA_LNG, "accuracy": 10},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"expected 401/403 for consumer, got {r.status_code} {r.text}"

    def test_worker_can_post_location(self, riya_auth):
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(riya_auth),
            json={"latitude": RIYA_LAT, "longitude": RIYA_LNG, "accuracy": 12},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert abs(float(body["current_latitude"]) - RIYA_LAT) < 1e-4
        assert abs(float(body["current_longitude"]) - RIYA_LNG) < 1e-4
        assert "current_location_updated_at" in body

    def test_location_persists_in_db(self, riya_auth):
        # Re-post a slightly tweaked location and read back from DB
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(riya_auth),
            json={"latitude": RIYA_LAT, "longitude": RIYA_LNG, "accuracy": 7},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_latitude, current_longitude, current_location_updated_at "
                    "FROM worker_profiles wp JOIN users u ON u.id = wp.user_id "
                    "WHERE u.phone_e164 = %s",
                    (RIYA_PHONE,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "worker profile not found"
        assert abs(float(row[0]) - RIYA_LAT) < 1e-4
        assert abs(float(row[1]) - RIYA_LNG) < 1e-4
        assert row[2] is not None


# ---------- Patch 2 regression — service eligibility ----------
class TestPatch2EligibilityRegression:
    def test_four_seeded_states(self, riya_auth):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(riya_auth), timeout=15)
        assert r.status_code == 200, r.text
        rows = {x["code"]: x for x in r.json()}
        gn = rows.get("GENERAL_NURSING")
        wd = rows.get("WOUND_DRESSING")
        iv = rows.get("IV_INFUSION")
        pi = rows.get("PICC_LINE_CARE")
        assert gn and gn["qualification_status"] == "APPROVED", gn
        assert gn["can_opt_in"] is True
        assert wd and wd["qualification_status"] == "APPROVED", wd
        assert iv and iv["qualification_status"] == "TRAINING_REQUIRED", iv
        assert pi is not None, "PICC_LINE_CARE missing"
        # PICC may be locked due to tier or pending approval — both spec-acceptable.
        assert pi["can_opt_in"] is False


# ---------- Acceptance checks #1–#4 ----------
class TestAcceptanceProximityFlow:
    """The four headline Patch 3 acceptance scenarios.

    Tests share state via class attributes — order matters within this class.
    """

    booking_id: str = ""

    def test_01_set_worker_locations_and_create_booking(
        self, riya_auth, meera_auth, consumer_auth, general_nursing_service
    ):
        # Riya inside ~1 km
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(riya_auth),
            json={"latitude": RIYA_LAT, "longitude": RIYA_LNG, "accuracy": 10},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        # Meera far away (~10 km)
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(meera_auth),
            json={"latitude": MEERA_LAT, "longitude": MEERA_LNG, "accuracy": 10},
            timeout=10,
        )
        assert r.status_code == 200, r.text

        # Ensure both opted IN to GENERAL_NURSING
        gn = general_nursing_service
        for w in (riya_auth, meera_auth):
            requests.put(
                f"{API}/workers/me/service-preferences",
                headers=_h(w),
                json={"target_type": "service", "target_id": gn["id"], "preference_status": "OPTED_IN"},
                timeout=10,
            )

        bid = _create_confirmed_booking(consumer_auth, gn["id"])
        TestAcceptanceProximityFlow.booking_id = bid
        assert bid

    def test_02_check1_riya_sees_booking_with_distance(self, riya_auth):
        bid = TestAcceptanceProximityFlow.booking_id
        r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(riya_auth), timeout=15)
        assert r.status_code == 200, r.text
        items = r.json()
        match = next((b for b in items if b["id"] == bid), None)
        assert match is not None, f"Riya should see booking {bid}; got {[b['id'] for b in items]}"
        assert match.get("assignment_wave") == 1
        assert match.get("distance_km") is not None
        assert 0.9 <= float(match["distance_km"]) <= 1.4, f"distance_km {match.get('distance_km')} not ~1.1km"

    def test_03_check2_meera_does_not_see_wave1_booking(self, meera_auth):
        bid = TestAcceptanceProximityFlow.booking_id
        r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(meera_auth), timeout=15)
        assert r.status_code == 200, r.text
        items = r.json()
        ids = {b["id"] for b in items}
        assert bid not in ids, f"Meera (~10km) must NOT see wave-1 booking; saw {ids}"

    def test_04_check3_bump_wave_to_3_meera_sees(self, meera_auth):
        bid = TestAcceptanceProximityFlow.booking_id
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET assignment_wave=3 WHERE id=%s", (bid,))
            conn.commit()
        finally:
            conn.close()

        r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(meera_auth), timeout=15)
        assert r.status_code == 200, r.text
        items = r.json()
        match = next((b for b in items if b["id"] == bid), None)
        assert match is not None, f"Meera should see wave-3 booking {bid}; got {[b['id'] for b in items]}"
        assert match.get("distance_km") is not None
        assert 9.0 <= float(match["distance_km"]) <= 11.5, f"distance_km {match['distance_km']} not ~10km"
        # assignment_wave should remain >= 3 (the endpoint only bumps UP)
        assert (match.get("assignment_wave") or 0) >= 3

    def test_05_check4_meera_claims_first_riya_409(self, meera_auth, riya_auth):
        bid = TestAcceptanceProximityFlow.booking_id
        # Ensure wave still 3 (test_04 left it at 3 and endpoint only bumps up)
        # Meera claims first
        r_meera = requests.post(f"{API}/bookings/{bid}/accept", headers=_h(meera_auth), timeout=15)
        assert r_meera.status_code == 200, f"Meera first claim should succeed: {r_meera.status_code} {r_meera.text}"
        body_m = r_meera.json()
        assert body_m.get("status") == "assigned", body_m

        # Riya tries to claim — must lose with 409
        r_riya = requests.post(f"{API}/bookings/{bid}/accept", headers=_h(riya_auth), timeout=15)
        assert r_riya.status_code == 409, f"Riya late claim must 409, got {r_riya.status_code} {r_riya.text}"
        body_r = r_riya.json()
        # Patch 2 envelope shape: {success:false, code:'BOOKING_ALREADY_CLAIMED', message:...}
        assert body_r.get("code") == "BOOKING_ALREADY_CLAIMED", body_r
        assert body_r.get("success") is False
        assert "message" in body_r


# ---------- Urgent + worker without coords → excluded ----------
class TestUrgentNoCoordsExclusion:
    def test_urgent_excluded_when_worker_has_no_coords(
        self, meera_auth, consumer_auth, general_nursing_service
    ):
        # Snapshot Meera's coords, NULL them, create an urgent booking, then restore.
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT wp.id, wp.home_latitude, wp.home_longitude, wp.current_latitude, wp.current_longitude, wp.current_location_updated_at "
                    "FROM worker_profiles wp JOIN users u ON u.id = wp.user_id WHERE u.phone_e164=%s",
                    (MEERA_PHONE,),
                )
                row = cur.fetchone()
                assert row, "Meera profile missing"
                wp_id, h_lat, h_lng, c_lat, c_lng, c_ts = row
                cur.execute(
                    "UPDATE worker_profiles SET home_latitude=NULL, home_longitude=NULL, "
                    "current_latitude=NULL, current_longitude=NULL, current_location_updated_at=NULL "
                    "WHERE id=%s",
                    (wp_id,),
                )
            conn.commit()
        finally:
            conn.close()

        try:
            bid = _create_confirmed_booking(
                consumer_auth, general_nursing_service["id"], is_urgent=True
            )
            r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(meera_auth), timeout=15)
            assert r.status_code == 200, r.text
            items = r.json()
            ids = {b["id"] for b in items}
            assert bid not in ids, (
                "Urgent booking must NOT be visible to worker with no current/home coords"
            )
        finally:
            # Restore coords so subsequent test sessions still work
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE worker_profiles SET home_latitude=%s, home_longitude=%s, "
                        "current_latitude=%s, current_longitude=%s, current_location_updated_at=%s "
                        "WHERE id=%s",
                        (h_lat, h_lng, c_lat, c_lng, c_ts, wp_id),
                    )
                conn.commit()
            finally:
                conn.close()


# ---------- No paid Google Maps API usage in backend ----------
class TestNoPaidMapsBackend:
    def test_no_google_maps_backend_calls(self):
        forbidden = ["GOOGLE_MAPS_API_KEY", "maps.googleapis.com", "geocoding", "distance-matrix"]
        # grep ignoring caches/venvs
        for needle in forbidden:
            res = subprocess.run(
                [
                    "grep", "-rI", "--exclude-dir=__pycache__",
                    "--exclude-dir=.venv", "--exclude-dir=venv",
                    "--include=*.py", needle, "/app/backend/app",
                ],
                capture_output=True, text=True,
            )
            assert res.returncode != 0 or not res.stdout.strip(), (
                f"Forbidden token '{needle}' found in backend:\n{res.stdout}"
            )
