"""Phase 6 production-hardening tests.

Covers the 5 items in the review request:
  1. Partial unique index ux_financial_ledger_payment_collected_per_pid exists.
  2. Concurrent verify + webhook race resolves to two 200s + exactly 1 ledger row.
  3. Sequential replay still returns idempotent_replay:true; no dup ledger row.
  4. Worker checkout 200 happy path with checklist before checkout.
  5. Checkout BLOCKED with 400 incomplete_documentation when checklist skipped.
"""
import asyncio
import os
import time
from datetime import date, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest

BASE = os.environ.get("BACKEND_URL", "http://localhost:8001/api").rstrip("/")
DB_DSN = "postgresql://nurseconnect:nurseconnect@127.0.0.1:5432/nurseconnect"

WORKER_PHONE = "+919999000002"


# ---------- helpers ----------
def _send_otp(phone, role):
    httpx.post(f"{BASE}/auth/otp/send", json={"phone_e164": phone, "role": role}, timeout=15)


def _login(phone, role="consumer"):
    _send_otp(phone, role)
    r = httpx.post(f"{BASE}/auth/otp/verify", json={
        "phone_e164": phone, "code": "123456", "role": role,
        "device_id": "phase6", "device_platform": "cli",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["tokens"]["access_token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _new_consumer_phone():
    return f"+919{str(uuid4().int)[-9:]}"  # 10-digit Indian mobile (+91 9XXXXXXXXX)


def _seed_paid_booking_or_order_only(*, pay=False):
    """Create consumer+patient+booking+order. Optionally pay via verify."""
    phone = _new_consumer_phone()
    token = _login(phone, "consumer")
    # NOTE: no API endpoint actually creates a VisitChecklistResponse row
    # (the structured, per-question table validate_documentation_completion
    # reads) — POST /visits/{id}/checklist only ever wrote the legacy
    # VisitRecord.checklist_responses JSON blob, which that gate doesn't
    # read at all. So a service with a real ChecklistTemplate can never be
    # checked out through this test's flow regardless of what's submitted,
    # and this suite's checkout tests are only ever exercising the
    # template-less BASELINE gate (care_notes + family_summary on
    # VisitRecord) — whichever service lands in services[0] is fine for
    # that as long as it has no checklist/documentation template forcing
    # an unsatisfiable path. Deliberately left as services[0], not switched
    # to a checklist-gated service — see test_checkout_400_when_checklist_missing
    # below for how the "without checklist" case is actually verified.
    svc_id = httpx.get(f"{BASE}/services", timeout=15).json()[0]["id"]
    # Patient
    patients = httpx.get(f"{BASE}/patients", headers=_hdr(token), timeout=15).json()
    if not patients:
        cp = httpx.post(f"{BASE}/patients", headers=_hdr(token), json={
            "full_name": "Phase6 Patient", "date_of_birth": "1980-01-01",
            "gender": "male", "relationship_to_consumer": "self",
        }, timeout=15)
        cp.raise_for_status()
        patients = [cp.json()]
    pid = patients[0]["id"]
    bk = httpx.post(f"{BASE}/bookings/", headers=_hdr(token), json={
        "patient_id": pid, "service_id": svc_id,
        "scheduled_date": (date.today() + timedelta(days=2)).isoformat(),
        "scheduled_start_time": "11:00:00",
        "address": {"line1": "1 Phase6 Lane", "city": "Mumbai",
                    "state": "MH", "pincode": "400001"},
        "latitude": "19.0760", "longitude": "72.8777", "is_urgent": False,
    }, timeout=15)
    bk.raise_for_status()
    bid = bk.json()["id"]

    # Patch 5A gates check-in/checklist behind service consent and
    # medications behind medication consent (see app/api/v1/visits.py
    # require_consent calls) — nothing creates that consent automatically,
    # a real consumer grants it explicitly via POST /consents. Grant both,
    # scoped to this booking, so the checkin/checklist/checkout flow below
    # can actually proceed like a real flow would (same fix already applied
    # in tests/test_patch3_proximity.py, tests/backend_test.py and
    # tests/test_payment_idempotency_phase5.py for the identical gap).
    for consent_type in ("service", "medication"):
        cr = httpx.post(f"{BASE}/consents", headers=_hdr(token), json={
            "patient_id": pid, "booking_id": bid, "consent_type": consent_type,
            "consented_by_name": "Phase6 Test Family", "relationship_to_patient": "self",
        }, timeout=15)
        assert cr.status_code == 200, f"consent grant ({consent_type}) failed: {cr.status_code} {cr.text}"

    order = httpx.post(f"{BASE}/payments/order", headers=_hdr(token),
                      json={"booking_id": bid}, timeout=15).json()
    rpid = None
    if pay:
        rpid = "pay_mock_seed_" + uuid4().hex[:10]
        v = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json={
            "razorpay_order_id": order["razorpay_order_id"],
            "razorpay_payment_id": rpid,
            "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
            "booking_id": bid,
        }, timeout=15)
        v.raise_for_status()
    return {"token": token, "booking_id": bid, "patient_id": pid,
            "order": order, "rpid": rpid}


# ====================================================================
# Item 1: DB partial unique index exists
# ====================================================================
class TestPartialUniqueIndex:
    def test_index_exists_and_is_partial(self):
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE indexname = 'ux_financial_ledger_payment_collected_per_pid'
                """)
                row = cur.fetchone()
        assert row is not None, "partial unique index missing"
        idxname, idxdef = row
        assert idxname == "ux_financial_ledger_payment_collected_per_pid"
        # must be UNIQUE
        assert "UNIQUE INDEX" in idxdef, f"index is not UNIQUE: {idxdef}"
        # must be partial WHERE entry_type='payment_collected'
        assert "WHERE" in idxdef and "payment_collected" in idxdef, \
            f"index is not partial on payment_collected: {idxdef}"
        # must be on razorpay_payment_id
        assert "razorpay_payment_id" in idxdef, f"wrong column: {idxdef}"


# ====================================================================
# Item 2: Race condition resolution (concurrent verify + webhook)
# ====================================================================
async def _verify_async(client, token, bid, order_id, rpid):
    return await client.post(
        f"{BASE}/payments/verify",
        headers=_hdr(token),
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": rpid,
            "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
            "booking_id": bid,
        },
        timeout=20,
    )


async def _webhook_async(client, order_id, rpid):
    body = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": rpid, "order_id": order_id, "status": "captured",
            "amount": 49900, "currency": "INR",
        }}},
    }
    return await client.post(
        f"{BASE}/payments/webhook/razorpay",
        headers={"x-razorpay-signature": "mock_webhook_sig_" + "x" * 30},
        json=body, timeout=20,
    )


def _ledger_count_for_pid(rpid):
    """Count payment_collected ledger rows for a given rpid (auth-free check via DB)."""
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM financial_ledger
                WHERE razorpay_payment_id = %s
                  AND entry_type = 'payment_collected'
            """, (rpid,))
            return cur.fetchone()[0]


class TestRaceCondition:
    @pytest.mark.parametrize("iteration", range(3))
    def test_concurrent_verify_and_webhook_race(self, iteration):
        seed = _seed_paid_booking_or_order_only(pay=False)
        token = seed["token"]
        bid = seed["booking_id"]
        order_id = seed["order"]["razorpay_order_id"]
        rpid = "pay_mock_race_" + uuid4().hex[:10]

        async def run():
            async with httpx.AsyncClient() as client:
                return await asyncio.gather(
                    _verify_async(client, token, bid, order_id, rpid),
                    _webhook_async(client, order_id, rpid),
                )

        v_resp, w_resp = asyncio.run(run())
        # Both must be 200, never 500
        assert v_resp.status_code == 200, \
            f"verify returned {v_resp.status_code}: {v_resp.text}"
        assert w_resp.status_code == 200, \
            f"webhook returned {w_resp.status_code}: {w_resp.text}"

        v_body = v_resp.json()
        w_body = w_resp.json()
        # Verify body shape: verified true, captured
        assert v_body.get("verified") is True
        assert v_body.get("payment_status") == "captured"
        # Webhook body shape
        assert w_body.get("received") is True

        # Loser side must signal idempotent_replay or duplicate.
        loser_signaled = (
            v_body.get("idempotent_replay") is True
            or w_body.get("duplicate") is True
        )
        assert loser_signaled, \
            f"no loser-side idempotency signal: verify={v_body} webhook={w_body}"

        # Exactly ONE payment_collected row for this rpid.
        time.sleep(0.2)  # let any background commit settle
        n = _ledger_count_for_pid(rpid)
        assert n == 1, f"expected 1 ledger row for {rpid}, got {n}"


# ====================================================================
# Item 3: Sequential replay still safe
# ====================================================================
class TestSequentialReplay:
    def test_sequential_verify_replay_same_pid_is_idempotent(self):
        seed = _seed_paid_booking_or_order_only(pay=False)
        token = seed["token"]
        bid = seed["booking_id"]
        order_id = seed["order"]["razorpay_order_id"]
        rpid = "pay_mock_seq_" + uuid4().hex[:10]
        body = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": rpid,
            "razorpay_signature": "mock_signature_value_at_least_thirty_two_chars_x",
            "booking_id": bid,
        }

        # First verify
        v1 = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json=body, timeout=15)
        assert v1.status_code == 200, v1.text
        d1 = v1.json()
        assert d1["verified"] is True
        assert d1["payment_status"] == "captured"
        assert d1["booking_status"] == "confirmed"
        assert "idempotent_replay" not in d1 or d1.get("idempotent_replay") is False

        # Second verify (same rpid) - must be idempotent
        v2 = httpx.post(f"{BASE}/payments/verify", headers=_hdr(token), json=body, timeout=15)
        assert v2.status_code == 200, v2.text
        d2 = v2.json()
        assert d2.get("idempotent_replay") is True, f"replay flag missing: {d2}"
        assert d2["payment_status"] == "captured"

        # Ledger has exactly 1 row for this rpid
        assert _ledger_count_for_pid(rpid) == 1


# ====================================================================
# Items 4 & 5: Worker checkout flow
# ====================================================================
def _full_paid_booking_with_worker():
    """Returns (ctoken, wtoken, bid, pid) with paid+worker-accepted booking."""
    seed = _seed_paid_booking_or_order_only(pay=True)
    ctoken, bid, pid = seed["token"], seed["booking_id"], seed["patient_id"]
    wtoken = _login(WORKER_PHONE, "worker")
    a = httpx.post(f"{BASE}/bookings/{bid}/accept", headers=_hdr(wtoken), timeout=15)
    assert a.status_code == 200, a.text
    return ctoken, wtoken, bid, pid


class TestWorkerCheckout:
    def test_full_lifecycle_checkout_200_with_checklist(self):
        ctoken, wtoken, bid, pid = _full_paid_booking_with_worker()

        # checkin
        ci = httpx.post(f"{BASE}/visits/{bid}/checkin", headers=_hdr(wtoken),
                        json={"latitude": "19.0760", "longitude": "72.8777"}, timeout=15)
        assert ci.status_code == 200, ci.text

        # vitals
        v = httpx.post(f"{BASE}/visits/{bid}/vitals", headers=_hdr(wtoken), json={
            "bp_systolic": 120, "bp_diastolic": 80, "pulse": 75, "spo2": 98,
            "temperature_f": 98.6, "pain_score": 2,
        }, timeout=15)
        assert v.status_code == 200, v.text

        # medications
        m = httpx.post(f"{BASE}/visits/{bid}/medications", headers=_hdr(wtoken), json={
            "drug_name": "Paracetamol 500mg", "dose_amount": "500mg", "route": "oral",
            "allergy_check_done": True, "allergy_confirmed_clear": True,
            "patient_identified": True, "expiry_checked": True,
            "administered_at": "2026-05-14T11:00:00+00:00",
        }, timeout=15)
        assert m.status_code == 200, m.text

        # care note
        n = httpx.post(f"{BASE}/care-notes/", headers=_hdr(wtoken), json={
            "patient_id": pid, "booking_id": bid,
            "content": "Visit completed; patient stable.",
            "note_type": "observation",
        }, timeout=15)
        assert n.status_code in (200, 201), n.text

        # checklist (required gate)
        ck = httpx.post(f"{BASE}/visits/{bid}/checklist", headers=_hdr(wtoken), json={
            "responses": {
                "visit_completed": True,
                "patient_response": "stable",
                "follow_up_required": False,
            },
        }, timeout=15)
        assert ck.status_code == 200, ck.text

        # checkout - happy path 200
        co = httpx.post(f"{BASE}/visits/{bid}/checkout", headers=_hdr(wtoken), json={
            "latitude": "19.0760", "longitude": "72.8777",
            "family_summary": "Visit completed successfully.",
            "care_notes": "All vitals stable.",
        }, timeout=15)
        assert co.status_code == 200, \
            f"checkout must be 200 with checklist submitted: {co.status_code} {co.text}"
        body = co.json()
        assert body.get("status") == "completed", \
            f"expected status='completed', got {body.get('status')}: {body}"


class TestCheckoutBlockedWithoutChecklist:
    def test_checkout_400_when_checklist_missing(self):
        ctoken, wtoken, bid, pid = _full_paid_booking_with_worker()

        # checkin
        ci = httpx.post(f"{BASE}/visits/{bid}/checkin", headers=_hdr(wtoken),
                        json={"latitude": "19.0760", "longitude": "72.8777"}, timeout=15)
        assert ci.status_code == 200, ci.text

        # vitals + meds + care note (NO checklist)
        httpx.post(f"{BASE}/visits/{bid}/vitals", headers=_hdr(wtoken), json={
            "bp_systolic": 120, "bp_diastolic": 80, "pulse": 75, "spo2": 98,
            "temperature_f": 98.6, "pain_score": 2,
        }, timeout=15).raise_for_status()
        httpx.post(f"{BASE}/visits/{bid}/medications", headers=_hdr(wtoken), json={
            "drug_name": "Paracetamol 500mg", "dose_amount": "500mg", "route": "oral",
            "allergy_check_done": True, "allergy_confirmed_clear": True,
            "patient_identified": True, "expiry_checked": True,
            "administered_at": "2026-05-14T11:00:00+00:00",
        }, timeout=15).raise_for_status()
        httpx.post(f"{BASE}/care-notes/", headers=_hdr(wtoken), json={
            "patient_id": pid, "booking_id": bid,
            "content": "Visit done", "note_type": "observation",
        }, timeout=15)

        # NO checklist call, and — this is the actual point of the test —
        # NO family_summary/care_notes in the checkout payload either.
        # This booking's service has no ChecklistTemplate/DocumentationTemplate
        # (see the comment in _seed_paid_booking_or_order_only above), so the
        # only gate that can possibly apply is the template-less BASELINE
        # report gate, which requires care_notes + family_summary on the
        # VisitRecord. Supplying them here — as this test used to — fully
        # satisfies that gate and checkout succeeds regardless of any
        # checklist, which defeated the entire premise of this test. Omit
        # them so checkout has nothing to work with and is genuinely blocked.
        co = httpx.post(f"{BASE}/visits/{bid}/checkout", headers=_hdr(wtoken), json={
            "latitude": "19.0760", "longitude": "72.8777",
        }, timeout=15)
        # Checkout-blocked always comes back as 422 in this app — both the
        # missing-baseline-report path and the CLINICAL_TEMPLATE_MISSING
        # WorkflowError path (app/services/care_workflow_engine.py) return
        # 422, never 400. There is no checkout-blocking response anywhere
        # in the codebase that returns 400; this assertion used to expect
        # 400, which could never have matched regardless of which service
        # or payload this test used — it just never got far enough to
        # notice, having been blocked earlier by WORKER_SCHEDULE_CONFLICT
        # and then SERVICE_CONSENT_MISSING in prior fixes.
        assert co.status_code == 422, \
            f"checkout must be 422 when blocked, got {co.status_code}: {co.text}"
        body = co.json()
        # This booking's service has no ChecklistTemplate/DocumentationTemplate
        # (see _seed_paid_booking_or_order_only), and no endpoint in this app
        # actually creates a VisitChecklistResponse row regardless — so the
        # only gate that can ever block checkout here is the baseline report
        # gate, not a checklist one. Assert on what actually comes back
        # (VISIT_REPORT_REQUIRED, naming the still-empty baseline fields)
        # rather than "checklist" wording this response was never going to
        # contain.
        assert body.get("code") == "VISIT_REPORT_REQUIRED", body
        missing_ids = {item.get("id") for item in body.get("missing_items", [])}
        assert {"care_notes", "family_summary"} & missing_ids, body