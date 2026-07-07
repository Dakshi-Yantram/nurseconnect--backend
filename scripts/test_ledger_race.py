#!/usr/bin/env python3
"""Race-condition test for FinancialLedger unique index.

Simulates a concurrent /payments/verify + /payments/webhook race for the same
razorpay_payment_id and asserts:

  1. At least one path succeeds (200).
  2. The other path resolves cleanly (200 with idempotent_replay or duplicate flag,
     NOT a 500 from leaking IntegrityError).
  3. Exactly one ledger row exists for the payment_id afterwards.
"""
import asyncio
import sys
from datetime import date, timedelta
from uuid import uuid4

import httpx

BASE = "http://localhost:8001/api"


def _login_sync(phone: str, role: str = "consumer"):
    httpx.post(f"{BASE}/auth/send-otp", json={"phone_e164": phone, "role": role})
    r = httpx.post(f"{BASE}/auth/verify-otp", json={
        "phone_e164": phone, "code": "123456", "role": role,
        "device_id": "race-test", "device_platform": "cli",
    })
    r.raise_for_status()
    return r.json()["tokens"]["access_token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _seed_booking_sync():
    phone = f"+9199{str(uuid4().int)[-9:]}"
    token = _login_sync(phone, "consumer")
    svc_id = httpx.get(f"{BASE}/services").json()[0]["id"]
    patients = httpx.get(f"{BASE}/patients", headers=_hdr(token)).json()
    if not patients:
        cp = httpx.post(f"{BASE}/patients", headers=_hdr(token), json={
            "full_name": "Race Test", "date_of_birth": "1980-01-01",
            "gender": "male", "relationship_to_consumer": "self",
        })
        cp.raise_for_status()
        patients = [cp.json()]
    pid = patients[0]["id"]
    r = httpx.post(f"{BASE}/bookings/", headers=_hdr(token), json={
        "patient_id": pid, "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=2)).isoformat(),
        "scheduled_start_time": "11:00:00",
        "address": {"line1": "1 Test", "city": "Mumbai", "state": "MH", "pincode": "400001"},
        "latitude": "19.0760", "longitude": "72.8777", "is_urgent": False,
    })
    r.raise_for_status()
    bid = r.json()["id"]
    order = httpx.post(f"{BASE}/payments/order", headers=_hdr(token), json={"booking_id": bid}).json()
    return token, bid, order


async def _verify(client: httpx.AsyncClient, token: str, bid: str, order_id: str, rpid: str):
    return await client.post(
        f"{BASE}/payments/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": rpid,
            "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
            "booking_id": bid,
        },
        timeout=15,
    )


async def _webhook(client: httpx.AsyncClient, order_id: str, rpid: str):
    body = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": rpid,
                    "order_id": order_id,
                    "status": "captured",
                    "amount": 49900,
                    "currency": "INR",
                }
            }
        },
    }
    return await client.post(
        f"{BASE}/payments/webhook/razorpay",
        headers={"x-razorpay-signature": "mock_webhook_sig"},
        json=body,
        timeout=15,
    )


async def race(token: str, bid: str, order):
    rpid = "pay_mock_race_" + uuid4().hex[:8]
    async with httpx.AsyncClient() as client:
        verify_task = _verify(client, token, bid, order["razorpay_order_id"], rpid)
        webhook_task = _webhook(client, order["razorpay_order_id"], rpid)
        # Fire both at the same time
        v_resp, w_resp = await asyncio.gather(verify_task, webhook_task)

    print("verify HTTP:", v_resp.status_code, v_resp.json())
    print("webhook HTTP:", w_resp.status_code, w_resp.json())
    assert v_resp.status_code == 200, f"verify must not 500: {v_resp.text}"
    assert w_resp.status_code == 200, f"webhook must not 500: {w_resp.text}"

    # Ledger must have exactly one payment_collected row for this rpid.
    hist = httpx.get(f"{BASE}/payments/consumer/history", headers=_hdr(token)).json()
    matches = [h for h in hist if h["razorpay_payment_id"] == rpid]
    print("ledger rows for rpid:", matches)
    assert len(matches) == 1, f"DB unique index failed to dedupe! got {len(matches)} rows: {matches}"

    print()
    print("RACE-PROOF: DB UNIQUE INDEX HELD ✅")


def main():
    token, bid, order = _seed_booking_sync()
    asyncio.run(race(token, bid, order))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as e:
        print("HTTP ERROR:", e.response.status_code, e.response.text)
        sys.exit(1)
    except AssertionError as e:
        print("ASSERTION FAILED:", e)
        sys.exit(2)
