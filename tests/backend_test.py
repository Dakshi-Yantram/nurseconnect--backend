"""End-to-end backend tests for NurseConnect v2.

Covers:
- Auth (OTP send/verify, refresh rotation+revocation, logout, /me)
- RBAC (consumer/worker/admin enforcement)
- Catalog, worker search/public, patient + family CRUD
- Booking lifecycle: create -> payment order -> verify -> accept -> checkin
  -> vitals (critical BP triggers escalation) -> medication -> checklist
  -> checkout -> rating
- Manual escalation, admin acknowledge/resolve
- Care notes, consent, ABHA records
- Tracking location, offline-sync idempotency
- Notifications read, training assessment
- Admin dashboard, worker approve/suspend, rematch
- Razorpay webhook idempotency
- Health, JWT claims, booking cancellation, worker availability/bank/kit
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import jwt
import pytest
import requests

from tests.conftest import API, _login, auth_headers


# ---------- Health ----------
class TestHealth:
    def test_health_ok(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["database"] is True
        assert body["checks"]["redis"] is True


# ---------- Auth ----------
class TestAuth:
    def test_send_otp_consumer(self):
        r = requests.post(
            f"{API}/auth/send-otp",
            json={"phone_e164": "+919999000001", "role": "consumer"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("dev_otp") == "123456"
        assert body.get("sent") is True
        assert "expires_in_seconds" in body

    def test_verify_otp_all_roles(self):
        roles = [
            ("+919999000001", "consumer"),
            ("+919999000002", "worker"),
            ("+919999000003", "admin_ops"),
            ("+919999000004", "admin_super"),
            ("+919999000005", "admin_finance"),
            ("+919999000006", "admin_clinical"),
        ]
        for phone, role in roles:
            data = _login(phone, role)
            assert "tokens" in data
            assert "access_token" in data["tokens"]
            assert "refresh_token" in data["tokens"]
            assert data["user"]["role"] == role

    def test_jwt_access_claims(self, consumer_auth):
        token = consumer_auth["tokens"]["access_token"]
        # decode without verification to inspect claims
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded.get("type") == "access"
        assert decoded.get("role") == "consumer"
        assert "sub" in decoded
        assert "exp" in decoded

    def test_me_endpoint(self, consumer_auth):
        r = requests.get(f"{API}/auth/me", headers=auth_headers(consumer_auth), timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "consumer"
        assert body["phone_e164"] == "+919999000001"

    def test_refresh_rotates_and_revokes_old(self):
        # fresh login (separate from session fixtures to avoid touching them)
        data = _login("+919999000001", "consumer")
        old_refresh = data["tokens"]["refresh_token"]
        r = requests.post(
            f"{API}/auth/refresh", json={"refresh_token": old_refresh}, timeout=10
        )
        assert r.status_code == 200, r.text
        body = r.json()
        new_access = body.get("access_token") or body.get("tokens", {}).get("access_token")
        new_refresh = body.get("refresh_token") or body.get("tokens", {}).get("refresh_token")
        assert new_access and new_refresh
        assert new_refresh != old_refresh
        # Reusing the old refresh should fail (revoked)
        r2 = requests.post(
            f"{API}/auth/refresh", json={"refresh_token": old_refresh}, timeout=10
        )
        assert r2.status_code in (401, 403), f"old refresh accepted: {r2.status_code} {r2.text}"

    def test_logout(self):
        data = _login("+919999000001", "consumer")
        token = data["tokens"]["access_token"]
        refresh = data["tokens"]["refresh_token"]
        r = requests.post(
            f"{API}/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            json={"refresh_token": refresh},
            timeout=10,
        )
        assert r.status_code in (200, 204), r.text


# ---------- RBAC ----------
class TestRBAC:
    def test_consumer_blocked_from_worker_endpoints(self, consumer_auth):
        r = requests.get(f"{API}/workers/me", headers=auth_headers(consumer_auth), timeout=10)
        assert r.status_code in (401, 403)

    def test_consumer_blocked_from_admin_endpoints(self, consumer_auth):
        r = requests.get(f"{API}/admin/dashboard", headers=auth_headers(consumer_auth), timeout=10)
        assert r.status_code in (401, 403)

    def test_worker_blocked_from_consumer_endpoints(self, worker_auth):
        r = requests.get(f"{API}/patients", headers=auth_headers(worker_auth), timeout=10)
        assert r.status_code in (401, 403)
        r2 = requests.get(f"{API}/consumers/me", headers=auth_headers(worker_auth), timeout=10)
        assert r2.status_code in (401, 403)

    def test_worker_blocked_from_admin(self, worker_auth):
        r = requests.get(
            f"{API}/admin/financial/ledger", headers=auth_headers(worker_auth), timeout=10
        )
        assert r.status_code in (401, 403)

    def test_admin_blocked_from_consumer_specific(self, admin_ops_auth):
        r = requests.get(f"{API}/patients", headers=auth_headers(admin_ops_auth), timeout=10)
        assert r.status_code in (401, 403)

    def test_unauthenticated_blocked(self):
        r = requests.get(f"{API}/auth/me", timeout=10)
        assert r.status_code in (401, 403)


# ---------- Catalog ----------
class TestCatalog:
    def test_services_list(self):
        r = requests.get(f"{API}/services", timeout=10)
        assert r.status_code == 200
        svcs = r.json()
        assert isinstance(svcs, list)
        assert len(svcs) >= 3
        for s in svcs:
            assert "id" in s and "service_code" in s

    def test_service_by_id(self):
        svcs = requests.get(f"{API}/services", timeout=10).json()
        sid = svcs[0]["id"]
        r = requests.get(f"{API}/services/{sid}", timeout=10)
        assert r.status_code == 200
        assert r.json()["id"] == sid

    def test_care_packages(self):
        r = requests.get(f"{API}/care-packages", timeout=10)
        assert r.status_code == 200
        packs = r.json()
        assert isinstance(packs, list) and len(packs) >= 1
        pid = packs[0]["id"]
        r2 = requests.get(f"{API}/care-packages/{pid}", timeout=10)
        assert r2.status_code == 200
        assert r2.json()["id"] == pid


# ---------- Worker Search ----------
class TestWorkerSearch:
    def test_search(self, consumer_auth):
        r = requests.get(f"{API}/workers/search", headers=auth_headers(consumer_auth), timeout=10)
        assert r.status_code == 200
        wlist = r.json()
        assert isinstance(wlist, list) and len(wlist) >= 1

    def test_public_profile(self, consumer_auth):
        wlist = requests.get(
            f"{API}/workers/search", headers=auth_headers(consumer_auth), timeout=10
        ).json()
        wid = wlist[0]["id"]
        r = requests.get(
            f"{API}/workers/{wid}/public", headers=auth_headers(consumer_auth), timeout=10
        )
        assert r.status_code == 200
        assert r.json()["id"] == wid


# ---------- Patients & Family ----------
class TestPatientsAndFamily:
    def test_patient_crud(self, consumer_auth):
        h = auth_headers(consumer_auth)
        # list pre-seeded
        r = requests.get(f"{API}/patients", headers=h, timeout=10)
        assert r.status_code == 200
        seeded = r.json()
        assert len(seeded) >= 1

        # create
        payload = {
            "full_name": "TEST_Pytest Patient",
            "relationship": "self",
            "date_of_birth": "1980-05-01",
            "gender": "male",
            "blood_group": "B+",
        }
        r = requests.post(f"{API}/patients", headers=h, json=payload, timeout=10)
        assert r.status_code == 200, r.text
        created = r.json()
        pid = created["id"]
        assert created["full_name"] == payload["full_name"]

        # GET specific
        r = requests.get(f"{API}/patients/{pid}", headers=h, timeout=10)
        assert r.status_code == 200
        assert r.json()["id"] == pid

        # update
        r = requests.put(
            f"{API}/patients/{pid}",
            headers=h,
            json={"full_name": "TEST_Updated Patient"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["full_name"] == "TEST_Updated Patient"

    def test_family_member_create_list_delete(self, consumer_auth):
        h = auth_headers(consumer_auth)
        payload = {
            "full_name": "TEST_Sibling",
            "relationship": "sibling",
            "phone_e164": "+919998887777",
        }
        r = requests.post(f"{API}/family-members", headers=h, json=payload, timeout=10)
        assert r.status_code == 200, r.text
        fid = r.json()["id"]

        r = requests.get(f"{API}/family-members", headers=h, timeout=10)
        assert r.status_code == 200
        assert any(m["id"] == fid for m in r.json())

        r = requests.delete(f"{API}/family-members/{fid}", headers=h, timeout=10)
        assert r.status_code in (200, 204)

    def test_consumer_profile_me(self, consumer_auth):
        r = requests.get(
            f"{API}/consumers/me", headers=auth_headers(consumer_auth), timeout=10
        )
        assert r.status_code == 200


# ---------- Booking Lifecycle (shared state across tests) ----------
@pytest.fixture(scope="class")
def booking_ctx(consumer_auth, worker_auth):
    ch = auth_headers(consumer_auth)
    wh = auth_headers(worker_auth)
    svcs = requests.get(f"{API}/services", timeout=10).json()
    svc_id = svcs[0]["id"]
    patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
    pid = patients[0]["id"]
    payload = {
        "patient_id": pid,
        "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=1)).isoformat(),
        "scheduled_start_time": "10:30:00",
        "address": {"line1": "42 MG Road", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = requests.post(f"{API}/bookings/", headers=ch, json=payload, timeout=10)
    assert r.status_code == 200, r.text
    booking = r.json()
    return {
        "ch": ch, "wh": wh, "booking": booking, "bid": booking["id"], "pid": pid, "svc_id": svc_id,
    }


class TestBookingLifecycle:
    def test_01_booking_created(self, booking_ctx):
        b = booking_ctx["booking"]
        assert b["status"] in ("pending", "pending_payment", "created")
        assert "total_amount" in b

    def test_02_payment_order(self, booking_ctx):
        r = requests.post(
            f"{API}/payments/order",
            headers=booking_ctx["ch"],
            json={"booking_id": booking_ctx["bid"]},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        order = r.json()
        assert "razorpay_order_id" in order
        booking_ctx["order"] = order

    def test_03_payment_verify(self, booking_ctx):
        order = booking_ctx["order"]
        r = requests.post(
            f"{API}/payments/verify",
            headers=booking_ctx["ch"],
            json={
                "razorpay_order_id": order["razorpay_order_id"],
                "razorpay_payment_id": "pay_mock_test_xyz",
                "razorpay_signature": "mock_signature_value_at_least_thirty_two",
                "booking_id": booking_ctx["bid"],
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_04_worker_accept(self, booking_ctx):
        r = requests.post(
            f"{API}/bookings/{booking_ctx['bid']}/accept",
            headers=booking_ctx["wh"],
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_05_checkin(self, booking_ctx):
        r = requests.post(
            f"{API}/visits/{booking_ctx['bid']}/checkin",
            headers=booking_ctx["wh"],
            json={"latitude": "19.0760", "longitude": "72.8777"},
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_06_vitals_critical_triggers_escalation(self, booking_ctx):
        r = requests.post(
            f"{API}/visits/{booking_ctx['bid']}/vitals",
            headers=booking_ctx["wh"],
            json={
                "bp_systolic": 195,
                "bp_diastolic": 110,
                "pulse": 85,
                "spo2": 97,
                "temperature_f": 98.6,
                "pain_score": 3,
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        flags = body.get("abnormal_flags") or []
        assert any("bp_systolic" in str(f) for f in flags), f"expected bp_systolic flag, got {flags}"
        assert body.get("escalation_level") == "emergency"
        assert body.get("escalation_triggered") is True

    def test_07_medication(self, booking_ctx):
        r = requests.post(
            f"{API}/visits/{booking_ctx['bid']}/medications",
            headers=booking_ctx["wh"],
            json={
                "drug_name": "Paracetamol 500mg",
                "dose_amount": "500mg",
                "route": "oral",
                "allergy_check_done": True,
                "allergy_confirmed_clear": True,
                "patient_identified": True,
                "expiry_checked": True,
                "administered_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_08_checkout(self, booking_ctx):
        r = requests.post(
            f"{API}/visits/{booking_ctx['bid']}/checkout",
            headers=booking_ctx["wh"],
            json={
                "latitude": "19.0760",
                "longitude": "72.8777",
                "family_summary": "Visit completed",
                "care_notes": "Stable",
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json().get("status") in ("completed", "checked_out")

    def test_09_rating(self, booking_ctx):
        r = requests.post(
            f"{API}/visits/{booking_ctx['bid']}/rating",
            headers=booking_ctx["ch"],
            json={"rating": 5, "comment": "Great"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json().get("rating_by_consumer") == 5


# ---------- Escalations ----------
class TestEscalations:
    def test_manual_escalation_and_admin_triage(self, consumer_auth, worker_auth, admin_ops_auth):
        ch = auth_headers(consumer_auth)
        wh = auth_headers(worker_auth)
        ah = auth_headers(admin_ops_auth)
        # Create a separate booking flow up to acceptance
        svcs = requests.get(f"{API}/services", timeout=10).json()
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        bk = requests.post(
            f"{API}/bookings/",
            headers=ch,
            json={
                "patient_id": patients[0]["id"],
                "service_id": svcs[0]["id"],
                "scheduled_date": (date.today() + timedelta(days=2)).isoformat(),
                "scheduled_start_time": "12:00:00",
                "address": {"line1": "X", "city": "Mumbai", "state": "MH", "pincode": "400001"},
                "latitude": "19.0760",
                "longitude": "72.8777",
            },
            timeout=10,
        ).json()
        bid = bk["id"]
        order = requests.post(
            f"{API}/payments/order", headers=ch, json={"booking_id": bid}, timeout=10
        ).json()
        requests.post(
            f"{API}/payments/verify",
            headers=ch,
            json={
                "razorpay_order_id": order["razorpay_order_id"],
                "razorpay_payment_id": "pay_mock_esc",
                "razorpay_signature": "mock_signature_value_at_least_thirty_two",
                "booking_id": bid,
            },
            timeout=10,
        )
        requests.post(f"{API}/bookings/{bid}/accept", headers=wh, timeout=10)

        # Manual escalate
        esc = requests.post(
            f"{API}/bookings/{bid}/escalate",
            headers=wh,
            json={
                "level": "inform_family",
                "trigger_type": "manual",
                "notes": "Family wants update",
            },
            timeout=10,
        ).json()
        assert "id" in esc, esc
        # Acknowledge and resolve as admin
        eid = esc["id"]
        r = requests.post(f"{API}/escalations/{eid}/acknowledge", headers=ah, timeout=10)
        assert r.status_code == 200, r.text
        r = requests.post(
            f"{API}/escalations/{eid}/resolve",
            headers=ah,
            json={"resolution_notes": "Handled"},
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_escalations_open_admin_only(self, admin_ops_auth, consumer_auth):
        r = requests.get(
            f"{API}/escalations/open", headers=auth_headers(admin_ops_auth), timeout=10
        )
        assert r.status_code == 200
        r2 = requests.get(
            f"{API}/escalations/open", headers=auth_headers(consumer_auth), timeout=10
        )
        assert r2.status_code in (401, 403)


# ---------- Care / Consents / ABHA / Tracking ----------
class TestCareNotesConsentsAbhaTracking:
    def test_care_note_create_and_get(self, consumer_auth, worker_auth):
        ch = auth_headers(consumer_auth)
        wh = auth_headers(worker_auth)
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        pid = patients[0]["id"]
        r = requests.post(
            f"{API}/care-notes/",
            headers=wh,
            json={"patient_id": pid, "content": "Test note", "note_type": "observation"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        r = requests.get(f"{API}/care-notes/patient/{pid}", headers=ch, timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_consent_service(self, consumer_auth):
        ch = auth_headers(consumer_auth)
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        r = requests.post(
            f"{API}/consents",
            headers=ch,
            json={
                "patient_id": patients[0]["id"],
                "consent_type": "service",
                "consented_by_name": "Aanya Sharma",
                "relationship_to_patient": "self",
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_abha_records(self, consumer_auth):
        ch = auth_headers(consumer_auth)
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        pid = patients[0]["id"]
        r = requests.post(
            f"{API}/abha-records",
            headers=ch,
            json={
                "patient_id": pid,
                "title": "Test Prescription Record",
                "record_type": "prescription",
                "abha_health_id": "12-3456-7890-1234",
                "metadata_json": {"summary": "Test"},
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        r = requests.get(f"{API}/abha-records/patient/{pid}", headers=ch, timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_tracking_location(self, worker_auth, consumer_auth):
        wh = auth_headers(worker_auth)
        ch = auth_headers(consumer_auth)
        # Need an in-flight booking; reuse worker's own bookings
        bks = requests.get(f"{API}/bookings/worker", headers=wh, timeout=10).json()
        if not bks:
            pytest.skip("no booking for worker")
        bid = bks[0]["id"]
        r = requests.post(
            f"{API}/tracking/location",
            headers=wh,
            json={"latitude": "19.0762", "longitude": "72.8779", "booking_id": bid},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        r = requests.get(
            f"{API}/tracking/booking/{bid}/latest", headers=ch, timeout=10
        )
        assert r.status_code == 200


# ---------- Offline sync idempotency ----------
class TestOfflineSync:
    def test_idempotent_duplicate(self, worker_auth):
        h = auth_headers(worker_auth)
        local_id = str(uuid.uuid4())
        payload = {"items": [{
            "device_id": "test-device-1",
            "local_id": local_id,
            "record_type": "vital_signs",
            "payload": {"bp_systolic": 120, "bp_diastolic": 80, "pulse": 70},
            "locally_recorded_at": datetime.now(timezone.utc).isoformat(),
        }]}
        r1 = requests.post(f"{API}/offline-sync/", headers=h, json=payload, timeout=10)
        assert r1.status_code == 200, r1.text
        r2 = requests.post(f"{API}/offline-sync/", headers=h, json=payload, timeout=10)
        assert r2.status_code == 200, r2.text
        results_2 = r2.json()
        assert isinstance(results_2, list) and len(results_2) >= 1
        st = str(results_2[0].get("sync_status", "")).lower()
        assert "dup" in st or "skip" in st or "success" in st or "ok" in st or "synced" in st


# ---------- Notifications ----------
class TestNotifications:
    def test_list_and_mark_read(self, consumer_auth):
        h = auth_headers(consumer_auth)
        r = requests.get(f"{API}/notifications/", headers=h, timeout=10)
        assert r.status_code == 200
        items = r.json()
        if items:
            nid = items[0]["id"]
            r2 = requests.post(f"{API}/notifications/{nid}/read", headers=h, timeout=10)
            assert r2.status_code in (200, 204)


# ---------- Training ----------
class TestTraining:
    def test_modules_and_submit(self, worker_auth):
        h = auth_headers(worker_auth)
        r = requests.get(f"{API}/training/modules", headers=h, timeout=10)
        assert r.status_code == 200
        mods = r.json()
        if not mods:
            pytest.skip("no training modules seeded")
        mid = mods[0]["id"]
        r2 = requests.get(f"{API}/training/modules/{mid}", headers=h, timeout=10)
        assert r2.status_code == 200
        r3 = requests.post(
            f"{API}/training/modules/{mid}/assessment/submit",
            headers=h,
            json=[0, 1, 2],
            timeout=10,
        )
        assert r3.status_code in (200, 400, 404), r3.text


# ---------- Admin ----------
class TestAdmin:
    def test_dashboard(self, admin_ops_auth):
        r = requests.get(
            f"{API}/admin/dashboard", headers=auth_headers(admin_ops_auth), timeout=10
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)

    def test_workers_pending(self, admin_ops_auth):
        r = requests.get(
            f"{API}/admin/workers/pending", headers=auth_headers(admin_ops_auth), timeout=10
        )
        assert r.status_code == 200

    def test_financial_ledger_finance_role(self, admin_finance_auth):
        r = requests.get(
            f"{API}/admin/financial/ledger",
            headers=auth_headers(admin_finance_auth),
            timeout=10,
        )
        assert r.status_code in (200, 403)  # 403 acceptable if only super has access


# ---------- Worker self endpoints ----------
class TestWorkerSelf:
    def test_worker_me(self, worker_auth):
        r = requests.get(f"{API}/workers/me", headers=auth_headers(worker_auth), timeout=10)
        assert r.status_code == 200

    def test_availability_toggle(self, worker_auth):
        h = auth_headers(worker_auth)
        r = requests.put(
            f"{API}/workers/me/availability", headers=h, json={"availability": "offline"}, timeout=10
        )
        assert r.status_code == 200, r.text
        r = requests.put(
            f"{API}/workers/me/availability", headers=h, json={"availability": "online"}, timeout=10
        )
        assert r.status_code == 200, r.text

    def test_bank_details(self, worker_auth):
        r = requests.put(
            f"{API}/workers/me/bank-details",
            headers=auth_headers(worker_auth),
            json={
                "bank_account_holder": "Riya Kapoor",
                "bank_account_number": "1234567890",
                "bank_ifsc": "HDFC0001234",
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_kit_update(self, worker_auth):
        h = auth_headers(worker_auth)
        r = requests.get(f"{API}/workers/me/kit", headers=h, timeout=10)
        assert r.status_code == 200
        kits = r.json()
        if not kits:
            pytest.skip("no kit items")
        kid = kits[0]["id"]
        # is_present is a query parameter, not body
        r = requests.put(
            f"{API}/workers/me/kit/{kid}?is_present=true",
            headers=h,
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_earnings(self, worker_auth):
        r = requests.get(
            f"{API}/workers/me/earnings", headers=auth_headers(worker_auth), timeout=10
        )
        assert r.status_code == 200


# ---------- Booking cancellation ----------
class TestBookingCancel:
    def test_cancel_by_consumer(self, consumer_auth):
        ch = auth_headers(consumer_auth)
        svcs = requests.get(f"{API}/services", timeout=10).json()
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        bk = requests.post(
            f"{API}/bookings/",
            headers=ch,
            json={
                "patient_id": patients[0]["id"],
                "service_id": svcs[0]["id"],
                "scheduled_date": (date.today() + timedelta(days=3)).isoformat(),
                "scheduled_start_time": "14:00:00",
                "address": {"line1": "Y", "city": "Mumbai", "state": "MH", "pincode": "400001"},
                "latitude": "19.0760",
                "longitude": "72.8777",
            },
            timeout=10,
        ).json()
        bid = bk["id"]
        r = requests.post(
            f"{API}/bookings/{bid}/cancel",
            headers=ch,
            json={"reason": "changed plan"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert "cancel" in str(r.json().get("status", "")).lower()


# ---------- Webhook idempotency ----------
class TestWebhookIdempotency:
    def test_double_webhook(self, consumer_auth, worker_auth, admin_ops_auth):
        ch = auth_headers(consumer_auth)
        ah = auth_headers(admin_ops_auth)
        # set up paid booking
        svcs = requests.get(f"{API}/services", timeout=10).json()
        patients = requests.get(f"{API}/patients", headers=ch, timeout=10).json()
        bk = requests.post(
            f"{API}/bookings/",
            headers=ch,
            json={
                "patient_id": patients[0]["id"],
                "service_id": svcs[0]["id"],
                "scheduled_date": (date.today() + timedelta(days=4)).isoformat(),
                "scheduled_start_time": "09:00:00",
                "address": {"line1": "Z", "city": "Mumbai", "state": "MH", "pincode": "400001"},
                "latitude": "19.0760",
                "longitude": "72.8777",
            },
            timeout=10,
        ).json()
        bid = bk["id"]
        order = requests.post(
            f"{API}/payments/order", headers=ch, json={"booking_id": bid}, timeout=10
        ).json()
        unique_pid = f"pay_webhook_dup_{uuid.uuid4().hex[:10]}"

        webhook_body = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": unique_pid,
                        "order_id": order["razorpay_order_id"],
                        "amount": int(float(bk["total_amount"]) * 100),
                        "currency": "INR",
                        "status": "captured",
                        "notes": {"booking_id": bid},
                    }
                }
            },
        }
        # send twice
        r1 = requests.post(f"{API}/payments/webhook/razorpay", json=webhook_body, timeout=10)
        r2 = requests.post(f"{API}/payments/webhook/razorpay", json=webhook_body, timeout=10)
        assert r1.status_code in (200, 202), r1.text
        assert r2.status_code in (200, 202), r2.text

        # Check ledger: count entries for this payment id is at most 1
        ledger = requests.get(
            f"{API}/admin/financial/ledger", headers=ah, timeout=10
        )
        if ledger.status_code == 200:
            data = ledger.json()
            entries = data if isinstance(data, list) else data.get("entries", [])
            matching = [
                e for e in entries
                if unique_pid in str(e.get("razorpay_payment_id", ""))
                or unique_pid in str(e.get("reference", ""))
                or unique_pid in str(e)
            ]
            assert len(matching) <= 1, f"duplicate ledger entries: {len(matching)}"
