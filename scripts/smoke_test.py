#!/usr/bin/env python3
"""End-to-end smoke test for NurseConnect backend."""
import json
import sys
from datetime import date, timedelta

import httpx

BASE = "http://localhost:8001/api"


def login(phone: str, role: str) -> tuple[str, str]:
    httpx.post(f"{BASE}/auth/send-otp", json={"phone_e164": phone, "role": role})
    r = httpx.post(f"{BASE}/auth/verify-otp", json={
        "phone_e164": phone, "code": "123456", "role": role, "device_id": "test-cli", "device_platform": "cli"
    })
    r.raise_for_status()
    d = r.json()
    return d["tokens"]["access_token"], d["user"]["id"]


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main():
    print("=== Consumer login ===")
    ctoken, cid = login("+919999000001", "consumer")
    print("consumer id:", cid)

    print("=== Worker login ===")
    wtoken, wid = login("+919999000002", "worker")
    print("worker id:", wid)

    print("=== Admin login ===")
    atoken, aid = login("+919999000003", "admin_ops")
    print("admin id:", aid)

    print("=== List services ===")
    services = httpx.get(f"{BASE}/services").json()
    print(f"  {len(services)} services seeded")
    svc_id = services[0]["id"]

    print("=== List patients ===")
    patients = httpx.get(f"{BASE}/patients", headers=hdr(ctoken)).json()
    pid = patients[0]["id"]
    print("  patient:", patients[0]["full_name"])

    print("=== Search workers ===")
    workers_list = httpx.get(f"{BASE}/workers/search", headers=hdr(ctoken)).json()
    print(f"  found {len(workers_list)} workers")

    print("=== Create booking ===")
    booking_payload = {
        "patient_id": pid,
        "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=1)).isoformat(),
        "scheduled_start_time": "10:30:00",
        "address": {"line1": "42 MG Road", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = httpx.post(f"{BASE}/bookings/", headers=hdr(ctoken), json=booking_payload)
    r.raise_for_status()
    booking = r.json()
    bid = booking["id"]
    print(f"  booking_ref={booking['booking_ref']} total={booking['total_amount']}")

    print("=== Create payment order ===")
    order = httpx.post(f"{BASE}/payments/order", headers=hdr(ctoken), json={"booking_id": bid}).json()
    print("  order:", order["razorpay_order_id"])

    print("=== Verify payment ===")
    vr = httpx.post(f"{BASE}/payments/verify", headers=hdr(ctoken), json={
        "razorpay_order_id": order["razorpay_order_id"],
        "razorpay_payment_id": "pay_mock_xyz",
        "razorpay_signature": "mock_signature_value_at_least_thirty_two",
        "booking_id": bid,
    }).json()
    print("  verify:", vr)

    print("=== Worker accept ===")
    a = httpx.post(f"{BASE}/bookings/{bid}/accept", headers=hdr(wtoken)).json()
    print("  status:", a.get("status"))

    print("=== Worker check-in ===")
    ci = httpx.post(f"{BASE}/visits/{bid}/checkin", headers=hdr(wtoken), json={"latitude": "19.0760", "longitude": "72.8777"}).json()
    print("  visit status:", ci.get("status"))

    print("=== Submit vitals (with critical BP for escalation) ===")
    v = httpx.post(f"{BASE}/visits/{bid}/vitals", headers=hdr(wtoken), json={
        "bp_systolic": 195, "bp_diastolic": 110, "pulse": 85, "spo2": 97,
        "temperature_f": 98.6, "pain_score": 3,
    }).json()
    print("  flags:", v.get("abnormal_flags"), "level:", v.get("escalation_level"), "triggered:", v.get("escalation_triggered"))

    print("=== Submit medication ===")
    m = httpx.post(f"{BASE}/visits/{bid}/medications", headers=hdr(wtoken), json={
        "drug_name": "Paracetamol 500mg", "dose_amount": "500mg", "route": "oral",
        "allergy_check_done": True, "allergy_confirmed_clear": True,
        "patient_identified": True, "expiry_checked": True,
        "administered_at": "2026-05-11T11:00:00+00:00",
    }).json()
    print("  med:", m)

    print("=== Care note ===")
    n = httpx.post(f"{BASE}/care-notes/", headers=hdr(wtoken), json={
        "patient_id": pid, "booking_id": bid,
        "content": "Patient appears stable post-medication. Recommend follow-up.",
        "note_type": "observation",
    }).json()
    print("  care note id:", n.get("id"))

    print("=== Consent ===")
    cn = httpx.post(f"{BASE}/consents", headers=hdr(ctoken), json={
        "patient_id": pid, "booking_id": bid,
        "consent_type": "service", "consented_by_name": "Aanya Sharma", "relationship_to_patient": "self",
    }).json()
    print("  consent:", cn)

    print("=== Location update ===")
    lu = httpx.post(f"{BASE}/tracking/location", headers=hdr(wtoken), json={
        "latitude": "19.0762", "longitude": "72.8779", "booking_id": bid,
    }).json()
    print("  location:", lu)

    print("=== Manual escalate ===")
    esc = httpx.post(f"{BASE}/bookings/{bid}/escalate", headers=hdr(wtoken), json={
        "level": "inform_family", "trigger_type": "manual", "notes": "Family asked about pain management.",
    }).json()
    print("  escalation:", esc)

    print("=== Admin: list open escalations ===")
    opens = httpx.get(f"{BASE}/escalations/open", headers=hdr(atoken)).json()
    print(f"  open: {len(opens)}")

    print("=== Worker submit checklist (required before checkout) ===")
    chk = httpx.post(f"{BASE}/visits/{bid}/checklist", headers=hdr(wtoken), json={
        "responses": {
            "visit_completed": True,
            "patient_response": "stable",
            "follow_up_required": False,
        },
    })
    chk.raise_for_status()
    print("  checklist:", chk.json())

    print("=== Worker check-out ===")
    co = httpx.post(f"{BASE}/visits/{bid}/checkout", headers=hdr(wtoken), json={
        "latitude": "19.0760", "longitude": "72.8777",
        "family_summary": "Visit completed successfully.",
        "care_notes": "All vitals stable.",
    })
    co.raise_for_status()
    co = co.json()
    print("  visit:", co.get("status"), "duration:", co.get("actual_duration_minutes"))

    print("=== Consumer rating ===")
    rate = httpx.post(f"{BASE}/visits/{bid}/rating", headers=hdr(ctoken), json={
        "rating": 5, "comment": "Excellent service!",
    }).json()
    print("  rated:", rate.get("rating_by_consumer"))

    print("=== Admin dashboard ===")
    dash = httpx.get(f"{BASE}/admin/dashboard", headers=hdr(atoken)).json()
    print("  dashboard:", dash)

    print("=== Notifications ===")
    notifs = httpx.get(f"{BASE}/notifications/", headers=hdr(ctoken)).json()
    print(f"  consumer received {len(notifs)} notifications")

    print()
    print("ALL E2E STEPS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as e:
        print("HTTP ERROR:", e.response.status_code, e.response.text)
        sys.exit(1)
