"""Patch 2 — Worker package/service qualification + opt-in tests.

Covers:
- Phone-based login for demo workers + consumer
- GET /workers/me/service-eligibility (qualification, preference, locked_reason)
- PUT /workers/me/service-preferences gating by qualification
- GET /bookings/worker/new-requests filtered by qualification AND opt-in
- POST /bookings/{id}/accept atomic claim race + qualification/opt-in gates
- Training -> qualification bridge on assessment pass
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, timedelta
from typing import Optional

import httpx
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

CONSUMER_PHONE = "+919999000001"
WORKER1_PHONE = "+919999000002"
WORKER2_PHONE = "+919999000007"


# --------- helpers ---------
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


# --------- session fixtures ---------
@pytest.fixture(scope="session")
def consumer_auth():
    return _login(CONSUMER_PHONE, "consumer")


@pytest.fixture(scope="session")
def worker1_auth():
    return _login(WORKER1_PHONE, "worker")


@pytest.fixture(scope="session")
def worker2_auth():
    return _login(WORKER2_PHONE, "worker")


@pytest.fixture(scope="session")
def services() -> dict:
    """Map service_code -> service dict."""
    r = requests.get(f"{API}/services", timeout=10)
    assert r.status_code == 200, r.text
    return {s["service_code"]: s for s in r.json()}


@pytest.fixture(scope="session")
def packages() -> dict:
    r = requests.get(f"{API}/care-packages", timeout=10)
    assert r.status_code == 200, r.text
    return {p["package_code"]: p for p in r.json()}


# --------- Login ---------
class TestPatch2Login:
    def test_login_consumer(self):
        data = _login(CONSUMER_PHONE, "consumer")
        assert data["user"]["role"] == "consumer"

    def test_login_worker1(self):
        data = _login(WORKER1_PHONE, "worker")
        assert data["user"]["role"] == "worker"

    def test_login_worker2(self):
        data = _login(WORKER2_PHONE, "worker")
        assert data["user"]["role"] == "worker"


# --------- Service eligibility ---------
class TestServiceEligibility:
    def test_eligibility_returns_all_services_and_packages(self, worker1_auth, services, packages):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker1_auth), timeout=15)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list)
        # Should cover all services and packages
        codes = {row["code"] for row in rows}
        for sc in services.keys():
            assert sc in codes, f"missing service {sc} in eligibility"
        for pc in packages.keys():
            assert pc in codes, f"missing package {pc} in eligibility"

    def test_iv_infusion_training_required(self, worker1_auth):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker1_auth), timeout=15)
        rows = r.json()
        iv = next((x for x in rows if x["code"] == "IV_INFUSION"), None)
        assert iv is not None
        assert iv["qualification_status"] == "TRAINING_REQUIRED"
        assert iv["can_opt_in"] is False
        assert iv["locked_reason"] == "TRAINING_REQUIRED"
        assert iv["risk_level"] == "MEDIUM"

    def test_picc_tier_too_low(self, worker1_auth):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker1_auth), timeout=15)
        rows = r.json()
        picc = next((x for x in rows if x["code"] == "PICC_LINE_CARE"), None)
        assert picc is not None
        assert picc["can_opt_in"] is False
        assert picc["locked_reason"] == "TIER_TOO_LOW"
        assert picc["risk_level"] == "CRITICAL"

    def test_post_op_package_missing_qualification(self, worker1_auth):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker1_auth), timeout=15)
        rows = r.json()
        po = next((x for x in rows if x["code"] == "POST_OP_CARE_7D"), None)
        assert po is not None
        assert po["can_opt_in"] is False
        assert po["locked_reason"] == "QUALIFICATION_RECORD_MISSING"

    def test_general_nursing_approved_opted_in(self, worker1_auth):
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker1_auth), timeout=15)
        rows = r.json()
        gn = next((x for x in rows if x["code"] == "GENERAL_NURSING"), None)
        assert gn is not None
        assert gn["qualification_status"] == "APPROVED"
        assert gn["can_opt_in"] is True


# --------- Preference updates ---------
class TestServicePreferenceUpdate:
    def test_opt_in_iv_infusion_blocked(self, worker1_auth, services):
        iv = services["IV_INFUSION"]
        r = requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker1_auth),
            json={"target_type": "service", "target_id": iv["id"], "preference_status": "OPTED_IN"},
            timeout=10,
        )
        assert r.status_code == 403, r.text
        body = r.json()
        # FastAPI wraps in detail
        detail = body.get("detail", body)
        assert detail.get("code") == "WORKER_NOT_QUALIFIED_FOR_SERVICE", body

    def test_opt_in_wound_dressing_succeeds(self, worker1_auth, services):
        # Worker is APPROVED for wound dressing but OPTED_OUT; flipping to OPTED_IN must succeed
        wd = services["WOUND_DRESSING"]
        r = requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker1_auth),
            json={"target_type": "service", "target_id": wd["id"], "preference_status": "OPTED_IN"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["preference_status"] == "OPTED_IN"
        assert body["willing_to_accept"] is True

    def test_opt_out_general_nursing_succeeds(self, worker1_auth, services):
        gn = services["GENERAL_NURSING"]
        r = requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker1_auth),
            json={"target_type": "service", "target_id": gn["id"], "preference_status": "OPTED_OUT"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["preference_status"] == "OPTED_OUT"


# --------- Helper to create+confirm a booking ---------
def _create_confirmed_booking(consumer_auth: dict, service_id: str, days_ahead: int = 1) -> str:
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
            "latitude": "19.0760",
            "longitude": "72.8777",
            "is_urgent": False,
        },
        timeout=15,
    )
    assert bk.status_code == 200, bk.text
    bid = bk.json()["id"]
    # Flip status to 'confirmed' directly via DB using sync psycopg2
    import psycopg2 as _pg  # noqa: E402

    conn = _pg.connect(host="127.0.0.1", port=5432, dbname="nurseconnect", user="nurseconnect", password="nurseconnect")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bookings SET status='confirmed', worker_id=NULL WHERE id=%s", (bid,))
        conn.commit()
    finally:
        conn.close()
    return bid


# --------- new-requests filtering + accept race ---------
class TestNewRequestsAndAccept:
    def test_general_nursing_visibility_after_optout(self, consumer_auth, worker1_auth, worker2_auth, services):
        # Pre: worker1 has opted out from GENERAL_NURSING (previous class).
        # Make sure worker2 is opted in (idempotent re-apply).
        gn = services["GENERAL_NURSING"]
        requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker2_auth),
            json={"target_type": "service", "target_id": gn["id"], "preference_status": "OPTED_IN"},
            timeout=10,
        )
        bid = _create_confirmed_booking(consumer_auth, gn["id"], days_ahead=5)

        r1 = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(worker1_auth), timeout=15)
        r2 = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(worker2_auth), timeout=15)
        assert r1.status_code == 200 and r2.status_code == 200
        ids_1 = {b["id"] for b in r1.json()}
        ids_2 = {b["id"] for b in r2.json()}
        assert bid not in ids_1, "worker1 opted out but still sees GENERAL_NURSING booking"
        assert bid in ids_2, "worker2 should see GENERAL_NURSING booking"

    def test_iv_infusion_not_visible_to_unqualified(self, consumer_auth, worker1_auth, services):
        iv = services["IV_INFUSION"]
        bid = _create_confirmed_booking(consumer_auth, iv["id"], days_ahead=6)
        r = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(worker1_auth), timeout=15)
        assert r.status_code == 200
        ids = {b["id"] for b in r.json()}
        assert bid not in ids, "worker not qualified for IV_INFUSION should not see booking"

    def test_accept_rejects_unqualified(self, consumer_auth, worker1_auth, services):
        iv = services["IV_INFUSION"]
        bid = _create_confirmed_booking(consumer_auth, iv["id"], days_ahead=7)
        r = requests.post(f"{API}/bookings/{bid}/accept", headers=_h(worker1_auth), timeout=15)
        assert r.status_code == 403, r.text
        body = r.json()
        assert body.get("code") == "WORKER_NOT_QUALIFIED_FOR_SERVICE", body

    def test_accept_rejects_not_opted_in(self, consumer_auth, worker1_auth, services):
        # worker1 has opted out of GENERAL_NURSING earlier in this test session.
        gn = services["GENERAL_NURSING"]
        bid = _create_confirmed_booking(consumer_auth, gn["id"], days_ahead=8)
        r = requests.post(f"{API}/bookings/{bid}/accept", headers=_h(worker1_auth), timeout=15)
        assert r.status_code == 403, r.text
        body = r.json()
        assert body.get("code") == "WORKER_NOT_OPTED_IN_FOR_SERVICE", body

    def test_concurrent_accept_race(self, consumer_auth, worker1_auth, worker2_auth, services):
        # Re-opt worker1 IN to GENERAL_NURSING so both are eligible
        gn = services["GENERAL_NURSING"]
        r = requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker1_auth),
            json={"target_type": "service", "target_id": gn["id"], "preference_status": "OPTED_IN"},
            timeout=10,
        )
        assert r.status_code == 200, r.text

        bid = _create_confirmed_booking(consumer_auth, gn["id"], days_ahead=9)

        async def _hit(token):
            async with httpx.AsyncClient(timeout=20) as cli:
                return await cli.post(
                    f"{API}/bookings/{bid}/accept",
                    headers={"Authorization": f"Bearer {token}"},
                )

        async def _race():
            return await asyncio.gather(
                _hit(worker1_auth["tokens"]["access_token"]),
                _hit(worker2_auth["tokens"]["access_token"]),
            )

        r1, r2 = asyncio.run(_race())
        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409], f"unexpected race outcome: {codes} | {r1.text} | {r2.text}"
        loser = r1 if r1.status_code == 409 else r2
        assert loser.json().get("code") == "BOOKING_ALREADY_CLAIMED", loser.text

        # After successful claim, neither worker should see it in new-requests
        for w in (worker1_auth, worker2_auth):
            nr = requests.get(f"{API}/bookings/worker/new-requests", headers=_h(w), timeout=15).json()
            assert bid not in {b["id"] for b in nr}, "claimed booking still visible in new-requests"

        # Winner sees it under assigned
        winner_resp = r1 if r1.status_code == 200 else r2
        winner_auth = worker1_auth if r1.status_code == 200 else worker2_auth
        assigned = requests.get(
            f"{API}/bookings/worker?status=assigned", headers=_h(winner_auth), timeout=15
        )
        assert assigned.status_code == 200
        assert bid in {b["id"] for b in assigned.json()}, "winner should see booking in assigned list"

        # Verify only one VisitRecord
        import psycopg2 as _pg
        conn = _pg.connect(host="127.0.0.1", port=5432, dbname="nurseconnect", user="nurseconnect", password="nurseconnect")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM visit_records WHERE booking_id=%s", (bid,))
                n_visits = cur.fetchone()[0]
                cur.execute("SELECT worker_id FROM bookings WHERE id=%s", (bid,))
                assigned_worker = cur.fetchone()[0]
        finally:
            conn.close()
        assert n_visits == 1, f"expected exactly 1 VisitRecord, got {n_visits}"
        assert assigned_worker is not None


# --------- Training -> qualification bridge ---------
class TestTrainingQualificationBridge:
    def test_iv_training_pass_flips_qualification_to_approved(self, worker2_auth, services):
        # Find IV_INFUSION_V1 training module
        modules = requests.get(f"{API}/training/modules", headers=_h(worker2_auth), timeout=10).json()
        iv_mod = next((m for m in modules if m["code"] == "IV_INFUSION_V1"), None)
        assert iv_mod is not None, "IV_INFUSION_V1 module not seeded"

        # Fetch full module to get assessment with correct answers (server only returns options)
        # Use seeded values: both correct answers are index 1
        answers = [1, 1]
        r = requests.post(
            f"{API}/training/modules/{iv_mod['id']}/assessment/submit",
            headers=_h(worker2_auth),
            json=answers,
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["passed"] is True, body

        # Now eligibility should show IV_INFUSION APPROVED for worker2
        time.sleep(0.5)
        elig = requests.get(
            f"{API}/workers/me/service-eligibility", headers=_h(worker2_auth), timeout=15
        ).json()
        iv = next((x for x in elig if x["code"] == "IV_INFUSION"), None)
        assert iv is not None
        assert iv["qualification_status"] == "APPROVED", f"expected APPROVED, got {iv}"
        assert iv["can_opt_in"] is True

        # Worker should now be able to opt in
        iv_svc = services["IV_INFUSION"]
        r2 = requests.put(
            f"{API}/workers/me/service-preferences",
            headers=_h(worker2_auth),
            json={"target_type": "service", "target_id": iv_svc["id"], "preference_status": "OPTED_IN"},
            timeout=10,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["preference_status"] == "OPTED_IN"
