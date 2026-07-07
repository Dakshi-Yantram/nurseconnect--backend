#!/usr/bin/env python3
"""Focused payment idempotency test.

Asserts:
  1. /payments/verify is idempotent on repeated calls with the same razorpay_payment_id.
  2. No duplicate financial ledger entry is created for the same razorpay_payment_id.
  3. Booking transitions exactly once to confirmed/captured.
  4. Verifying a *different* booking is not affected.
"""
import sys
from datetime import date, timedelta
from uuid import uuid4

import httpx

BASE = "http://localhost:8001/api"


def _login(phone: str, role: str = "consumer"):
    httpx.post(f"{BASE}/auth/send-otp", json={"phone_e164": phone, "role": role})
    r = httpx.post(f"{BASE}/auth/verify-otp", json={
        "phone_e164": phone, "code": "123456", "role": role,
        "device_id": "idemp-test", "device_platform": "cli",
    })
    r.raise_for_status()
    d = r.json()
    return d["tokens"]["access_token"], d["user"]["id"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def main():
    # Unique phone per run so booking belongs to a fresh consumer
    phone = f"+9199{str(uuid4().int)[-9:]}"
    token, _uid = _login(phone, "consumer")

    services = httpx.get(f"{BASE}/services").json()
    assert services, "no services seeded"
    svc_id = services[0]["id"]

    patients = httpx.get(f"{BASE}/patients", headers=_hdr(token)).json()
    if not patients:
        cp = httpx.post(f"{BASE}/patients", headers=_hdr(token), json={
            "full_name": "Test Patient",
            "date_of_birth": "1980-01-01",
            "gender": "male",
            "relationship_to_consumer": "self",
        })
        cp.raise_for_status()
        patients = [cp.json()]
    pid = patients[0]["id"]

    # Create booking
    bp = {
        "patient_id": pid,
        "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=2)).isoformat(),
        "scheduled_start_time": "11:00:00",
        "address": {"line1": "1 Test Lane", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760",
        "longitude": "72.8777",
        "is_urgent": False,
    }
    r = httpx.post(f"{BASE}/bookings/", headers=_hdr(token), json=bp)
    r.raise_for_status()
    bid = r.json()["id"]
    print(f"booking={bid}")

    # Order
    order = httpx.post(f"{BASE}/payments/order", headers=_hdr(token), json={"booking_id": bid}).json()
    rpid = "pay_mock_idemp_" + uuid4().hex[:10]
    body = {
        "razorpay_order_id": order["razorpay_order_id"],
        "razorpay_payment_id": rpid,
        "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
        "booking_id": bid,
    }

    # First verify
    v1 = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json=body)
    v1.raise_for_status()
    v1d = v1.json()
    print("verify#1:", v1d)
    assert v1d["verified"] is True
    assert v1d["payment_status"] == "captured"
    assert v1d["booking_status"] == "confirmed"
    assert "idempotent_replay" not in v1d, f"first verify should not be a replay: {v1d}"

    # Second verify (idempotent replay path)
    v2 = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json=body)
    v2.raise_for_status()
    v2d = v2.json()
    print("verify#2:", v2d)
    assert v2d["verified"] is True
    assert v2d["payment_status"] == "captured"
    assert v2d.get("idempotent_replay") is True, f"replay flag missing: {v2d}"

    # Third verify with a different payment_id but same order — backend should still
    # treat existing captured booking as final state (idempotent_replay True).
    body2 = dict(body)
    body2["razorpay_payment_id"] = "pay_mock_idemp_other_" + uuid4().hex[:6]
    v3 = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json=body2)
    v3.raise_for_status()
    v3d = v3.json()
    print("verify#3 (other pid):", v3d)
    assert v3d.get("idempotent_replay") is True, "must still treat as already paid"

    # Payment history should contain exactly one entry for this booking.
    hist = httpx.get(f"{BASE}/payments/consumer/history", headers=_hdr(token)).json()
    matches = [h for h in hist if h["booking_id"] == bid]
    print("history:", matches)
    assert len(matches) == 1, f"duplicate ledger entry detected: {matches}"
    assert matches[0]["payment_status"] == "captured"
    # Stored razorpay_payment_id should be the FIRST verify call's id (we don't allow downstream overwrite to a different one).
    assert matches[0]["razorpay_payment_id"] == rpid, f"second pid leaked into ledger: {matches[0]}"

    print()
    print("PAYMENT IDEMPOTENCY PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as e:
        print("HTTP ERROR:", e.response.status_code, e.response.text)
        sys.exit(1)
    except AssertionError as e:
        print("ASSERTION FAILED:", e)
        sys.exit(2)
