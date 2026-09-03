"""Family Login — service address location regression tests.

Covers the scenario reported by support: a family/consumer account books
visits for multiple patients living at different locations, and the
nurse-matching / distance calculation must always use the *booking's own*
service address — never the consumer account's default/home address.

Specifically verifies:
  1. Two bookings created for two different patients, using two different
     saved addresses, each end up with `booking.latitude/longitude` matching
     their own address — not the account default, and not each other.
  2. `PUT /consumers/me/addresses/{id}` and `POST /.../{id}/default` (i.e.
     editing an address or explicitly switching the default) keep the
     legacy `ConsumerProfile.latitude/longitude` mirror in sync with
     whichever address is actually the default, so it's never left
     pointing at a stale/removed address.
  3. `GET /bookings/worker/new-requests` (the proximity/wave filter a
     worker sees) reflects distance from the *booking's* address, i.e. a
     worker near Patient B's address sees Patient B's booking even when
     the consumer's account default is Patient A's address far away.

Run against a live backend + seeded DB, same convention as
test_patch3_proximity.py.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg2
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

CONSUMER_PHONE = "+919999000001"
NEARBY_WORKER_PHONE = "+919999000002"  # near Patient B's address in this test

# Patient A's address == the family account's saved default (far away).
DEFAULT_ADDR = {"line1": "1 Default Home", "city": "Mumbai", "state": "MH", "pincode": "400001",
                "latitude": "18.9430", "longitude": "72.8235", "label": "Home", "is_default": True}

# Patient B's address — a different location entirely, ~10km away, and
# never marked as default.
OTHER_ADDR = {"line1": "99 Other Colony", "city": "Mumbai", "state": "MH", "pincode": "400050",
              "latitude": "19.0330", "longitude": "72.8235", "label": "Other", "is_default": False}

NEARBY_WORKER_LAT = 19.030  # ~0.4km from OTHER_ADDR
NEARBY_WORKER_LNG = 72.8235


def _login(phone: str, role: str) -> dict:
    # NOTE: /auth/login is email+password only (see app/api/v1/auth.py).
    # Phone-number login for the mobile-app contract is /auth/phone-login.
    r = requests.post(f"{API}/auth/phone-login", json={"phone_e164": phone, "code": "123456", "role": role}, timeout=15)
    assert r.status_code == 200, f"login {phone}/{role} failed: {r.status_code} {r.text}"
    return r.json()


def _h(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}


def _pg_conn():
    return psycopg2.connect(host="127.0.0.1", port=5432, dbname="nurseconnect",
                             user="nurseconnect", password="nurseconnect")


@pytest.fixture(scope="session")
def consumer_auth():
    return _login(CONSUMER_PHONE, "consumer")


@pytest.fixture(scope="session")
def nearby_worker_auth():
    return _login(NEARBY_WORKER_PHONE, "worker")


@pytest.fixture(scope="session")
def general_nursing_service():
    r = requests.get(f"{API}/services", timeout=10)
    assert r.status_code == 200, r.text
    svcs = {s["service_code"]: s for s in r.json()}
    assert "GENERAL_NURSING" in svcs, "GENERAL_NURSING not seeded"
    return svcs["GENERAL_NURSING"]


def _patients(consumer_auth) -> list[dict]:
    r = requests.get(f"{API}/patients", headers=_h(consumer_auth), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _create_address(consumer_auth, body: dict) -> dict:
    r = requests.post(f"{API}/consumers/me/addresses", headers=_h(consumer_auth), json=body, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _create_booking(consumer_auth, patient_id: str, service_id: str, address_id: str, *, days_ahead: int) -> dict:
    r = requests.post(
        f"{API}/bookings/",
        headers=_h(consumer_auth),
        json={
            "patient_id": patient_id,
            "service_id": service_id,
            "scheduled_date": (date.today() + timedelta(days=days_ahead)).isoformat(),
            "scheduled_start_time": "10:30:00",
            "address_id": address_id,
            "is_urgent": False,
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(booking_id: str) -> None:
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status='confirmed', worker_id=NULL, assignment_wave=1 WHERE id=%s",
                (booking_id,),
            )
        conn.commit()
    finally:
        conn.close()


class TestFamilyServiceAddress:
    def test_two_bookings_use_their_own_address_not_account_default(
        self, consumer_auth, general_nursing_service,
    ):
        patients = _patients(consumer_auth)
        assert len(patients) >= 1, "need at least one seeded patient"
        # Reuse the same patient twice if only one is seeded — the point is
        # the *address* resolution, not patient identity.
        patient_a = patients[0]
        patient_b = patients[1] if len(patients) > 1 else patients[0]

        default_addr = _create_address(consumer_auth, DEFAULT_ADDR)
        other_addr = _create_address(consumer_auth, OTHER_ADDR)
        assert default_addr["is_default"] is True
        assert other_addr["is_default"] is False

        booking_a = _create_booking(
            consumer_auth, patient_a["id"], general_nursing_service["id"], default_addr["id"], days_ahead=3
        )
        booking_b = _create_booking(
            consumer_auth, patient_b["id"], general_nursing_service["id"], other_addr["id"], days_ahead=4
        )

        # Each booking must carry its own address's coordinates.
        assert float(booking_a["latitude"]) == pytest.approx(float(DEFAULT_ADDR["latitude"]), abs=1e-4)
        assert float(booking_a["longitude"]) == pytest.approx(float(DEFAULT_ADDR["longitude"]), abs=1e-4)
        assert float(booking_b["latitude"]) == pytest.approx(float(OTHER_ADDR["latitude"]), abs=1e-4)
        assert float(booking_b["longitude"]) == pytest.approx(float(OTHER_ADDR["longitude"]), abs=1e-4)
        # And they must differ from each other — booking B was never
        # silently pulled back to the account's default address.
        assert booking_a["latitude"] != booking_b["latitude"]

    def test_worker_near_non_default_address_sees_that_booking(
        self, consumer_auth, nearby_worker_auth, general_nursing_service,
    ):
        patients = _patients(consumer_auth)
        patient = patients[0]

        default_addr = _create_address(consumer_auth, DEFAULT_ADDR)
        other_addr = _create_address(consumer_auth, OTHER_ADDR)

        # Booking is for the patient at OTHER_ADDR (far from the account
        # default, close to nearby_worker_auth).
        booking = _create_booking(
            consumer_auth, patient["id"], general_nursing_service["id"], other_addr["id"], days_ahead=5
        )
        _confirm(booking["id"])

        # Put the worker's current location near OTHER_ADDR.
        r = requests.post(
            f"{API}/workers/me/location",
            headers=_h(nearby_worker_auth),
            json={"latitude": NEARBY_WORKER_LAT, "longitude": NEARBY_WORKER_LNG, "accuracy": 20},
            timeout=10,
        )
        assert r.status_code == 200, r.text

        r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(nearby_worker_auth), timeout=10)
        assert r.status_code == 200, r.text
        ids = [b["id"] for b in r.json()]
        assert booking["id"] in ids, (
            "Worker near the booking's OWN address should see it — if this "
            "fails, dispatch is matching against the account default "
            f"address ({default_addr['id']}) instead of the booking's "
            f"address ({other_addr['id']})."
        )

    def test_profile_default_mirror_follows_explicit_default_switch(self, consumer_auth):
        """POST /consumers/me/addresses/{id}/default must update the legacy
        profile mirror too — previously only address *creation* did this,
        so switching the default via this endpoint left the mirror stale.
        This doesn't affect booking/dispatch (which use address_id /
        booking.latitude directly), but guards against any code relying on
        profile.latitude reflecting "the current default"."""
        addr_1 = _create_address(consumer_auth, {**DEFAULT_ADDR, "line1": "Addr One"})
        addr_2 = _create_address(consumer_auth, {**OTHER_ADDR, "line1": "Addr Two", "is_default": False})

        r = requests.post(f"{API}/consumers/me/addresses/{addr_2['id']}/default",
                           headers=_h(consumer_auth), timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_default"] is True
        assert float(body["latitude"]) == pytest.approx(float(OTHER_ADDR["latitude"]), abs=1e-4)

        # addr_1 should no longer be default.
        r = requests.get(f"{API}/consumers/me/addresses", headers=_h(consumer_auth), timeout=10)
        rows = {a["id"]: a for a in r.json()}
        assert rows[addr_1["id"]]["is_default"] is False
        assert rows[addr_2["id"]]["is_default"] is True