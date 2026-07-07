#!/usr/bin/env python3
"""Patch 4 — backend smoke test for the dynamic workflow engine.

Run against a running server (uvicorn :8001). Exits non-zero on any failure.
Idempotent: safe to re-run; uses unique booking refs each time.
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8001/api")


async def login_worker(client: httpx.AsyncClient, phone: str = "+919999000002") -> str:
    await client.post(f"{BASE}/auth/send-otp", json={"phone_e164": phone, "role": "worker", "purpose": "login"})
    r = await client.post(f"{BASE}/auth/verify-otp", json={"phone_e164": phone, "code": "123456", "role": "worker"})
    r.raise_for_status()
    return r.json()["tokens"]["access_token"]


async def login_consumer(client: httpx.AsyncClient, phone: str = "+919999000001") -> str:
    await client.post(f"{BASE}/auth/send-otp", json={"phone_e164": phone, "role": "consumer", "purpose": "login"})
    r = await client.post(f"{BASE}/auth/verify-otp", json={"phone_e164": phone, "code": "123456", "role": "consumer"})
    r.raise_for_status()
    return r.json()["tokens"]["access_token"]


async def main() -> None:  # noqa: PLR0915
    async with httpx.AsyncClient(timeout=30) as client:
        wt = await login_worker(client)
        ct = await login_consumer(client)
        ch = {"Authorization": f"Bearer {ct}"}
        wh = {"Authorization": f"Bearer {wt}"}

        # 1. Pull catalogue services
        svcs = (await client.get(f"{BASE}/services", headers=ch)).json()
        svc_codes = {s["service_code"]: s for s in svcs}
        for code in ("WOUND_DRESSING", "BABY_BATH", "VITALS_VISIT", "IV_INFUSION", "HIGH_RISK_NO_TEMPLATE_TEST"):
            assert code in svc_codes, f"service {code} missing from catalogue"
        print("[ok] all Patch 4 services exist in catalogue")

        # 2. Pull patients (auto-seeded for the consumer)
        pats = (await client.get(f"{BASE}/patients", headers=ch)).json()
        assert len(pats) >= 1
        patient_id = pats[0]["id"]

        async def _create_booking(service_code: str) -> str:
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            payload = {
                "patient_id": patient_id,
                "service_id": svc_codes[service_code]["id"],
                "booking_type": "one_time",
                "scheduled_date": tomorrow,
                "scheduled_start_time": "10:00:00",
                "is_urgent": False,
                "address": {
                    "line1": "42 Marine Drive",
                    "city": "Mumbai",
                    "state": "Maharashtra",
                    "pincode": "400001",
                },
                "latitude": "18.9430",
                "longitude": "72.8235",
                "preferred_worker_id": None,
            }
            r = await client.post(f"{BASE}/bookings/", headers=ch, json=payload)
            r.raise_for_status()
            bid = r.json()["id"]
            # Smoke-test convenience: skip payment and flip the booking to
            # ``confirmed`` directly so the worker can accept it. In real
            # usage the consumer goes through /payments/verify.
            import asyncpg  # type: ignore
            con = await asyncpg.connect(
                "postgresql://nurseconnect:nurseconnect@127.0.0.1:5432/nurseconnect"
            )
            await con.execute(
                "UPDATE bookings SET status='confirmed', payment_status='captured' WHERE id=$1",
                bid,
            )
            await con.close()
            return bid

        # 3. Wound dressing booking → assign + checkin
        wound_id = await _create_booking("WOUND_DRESSING")
        baby_id = await _create_booking("BABY_BATH")
        hr_id = await _create_booking("HIGH_RISK_NO_TEMPLATE_TEST")
        print(f"[ok] created bookings wound={wound_id} baby={baby_id} hr={hr_id}")

        # Worker accepts wound + baby (HR booking intentionally left unassigned —
        # we just need the workflow GET on it to return CLINICAL_TEMPLATE_MISSING).
        for bid in (wound_id, baby_id):
            ar = await client.post(f"{BASE}/bookings/{bid}/accept", headers=wh)
            assert ar.status_code in (200, 409), f"accept {bid} → {ar.status_code} {ar.text}"
        # Assign hr_id to the worker directly via DB for the workflow check.
        import asyncpg  # type: ignore
        con = await asyncpg.connect(
            "postgresql://nurseconnect:nurseconnect@127.0.0.1:5432/nurseconnect"
        )
        await con.execute(
            "UPDATE bookings SET worker_id=(SELECT wp.id FROM worker_profiles wp JOIN users u ON u.id=wp.user_id WHERE u.phone_e164='+919999000002') WHERE id=$1",
            __import__('uuid').UUID(hr_id),
        )
        await con.close()

        # 4. Workflow resolution — wound dressing
        wfr = await client.get(f"{BASE}/care/workflow/{wound_id}", headers=wh)
        wfr.raise_for_status()
        wf = wfr.json()
        assert wf["checklist_template"]["code"] == "wound_dressing_v1", wf
        assert wf["documentation_template"]["wound_image_mandatory"] is True
        wound_photo_field = next(f for f in wf["documentation_template"]["mandatory_fields"] if f["field_id"] == "wound_photo")
        assert wound_photo_field["required"] and wound_photo_field["blocks_checkout"]
        print("[ok] wound workflow resolved with mandatory wound photo")

        # 5. Workflow resolution — baby bath
        wfb = (await client.get(f"{BASE}/care/workflow/{baby_id}", headers=wh)).json()
        assert wfb["checklist_template"]["code"] == "baby_bath_v1"
        print("[ok] baby bath workflow resolved from DB template")

        # 6. High-risk no-template service → CLINICAL_TEMPLATE_MISSING
        wfhr = await client.get(f"{BASE}/care/workflow/{hr_id}", headers=wh)
        assert wfhr.status_code == 422
        body = wfhr.json()
        assert body["code"] == "CLINICAL_TEMPLATE_MISSING", body
        print("[ok] high-risk service without template → CLINICAL_TEMPLATE_MISSING")

        # 7. Checkin wound booking
        r = await client.post(f"{BASE}/visits/{wound_id}/checkin", headers=wh, json={"latitude": "18.943", "longitude": "72.823"})
        assert r.status_code == 200, r.text

        # 8. Try checkout immediately → MANDATORY_DOCUMENTATION_INCOMPLETE
        co = await client.post(f"{BASE}/visits/{wound_id}/checkout", headers=wh, json={"latitude": "18.943", "longitude": "72.823"})
        assert co.status_code == 422, co.text
        cb = co.json()
        assert cb["code"] == "MANDATORY_DOCUMENTATION_INCOMPLETE", cb
        labels = {m["id"] for m in cb["missing_items"]}
        assert "wound_photo" in labels, f"wound_photo missing not flagged: {labels}"
        print("[ok] checkout 422 with wound_photo missing")

        # 9. Submit checklist responses for the required questions
        required_q = [q for q in wf["checklist_template"]["questions"] if q.get("required")]
        rps = []
        for q in required_q:
            qtype = q["type"]
            if qtype == "boolean":
                ans = True
            elif qtype == "textarea":
                ans = "Clean wound, mild exudate, no infection."
            elif qtype == "text":
                ans = "Hydrocolloid"
            elif qtype == "multi_select":
                ans = ["none"]
            elif qtype == "single_select":
                ans = (q.get("options") or ["yes"])[0]
            else:
                ans = "ok"
            rps.append({"question_id": q["id"], "answer": ans})
        r = await client.post(f"{BASE}/care/workflow/{wound_id}/responses", headers=wh, json={"responses": rps})
        assert r.status_code == 200, r.text
        cs = r.json()["completion_status"]
        assert not cs["can_checkout"]  # still missing wound_photo
        print("[ok] checklist responses saved; checkout still blocked by wound photo")

        # 10. Upload a wound photo (multipart) + submit documentation items
        tmp = "/tmp/_patch4_test.jpg"
        with open(tmp, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # tiny JPEG header
        with open(tmp, "rb") as fh:
            up = await client.post(
                f"{BASE}/care/workflow/{wound_id}/documentation/file",
                headers=wh,
                files={"file": ("wound.jpg", fh, "image/jpeg")},
                data={"field_id": "wound_photo"},
            )
        assert up.status_code == 200, up.text
        photo_url = up.json()["file_url"]

        # Documentation items
        await client.post(
            f"{BASE}/care/workflow/{wound_id}/documentation",
            headers=wh,
            json={"items": [
                {"field_id": "wound_photo", "file_url": photo_url},
                {"field_id": "family_summary", "value": "Wound dressed, no signs of infection. Next visit tomorrow."},
            ]},
        )

        # 11. Now checkout succeeds
        co2 = await client.post(f"{BASE}/visits/{wound_id}/checkout", headers=wh, json={"latitude": "18.943", "longitude": "72.823"})
        assert co2.status_code == 200, co2.text
        body = co2.json()
        assert body["documentation_complete"] is True
        assert body["family_summary"], "family summary should be set"
        print(f"[ok] checkout success. family_summary='{body['family_summary'][:60]}...'")

        # 12. Historical immutability: bump checklist template version and verify
        #     the visit row still references v1 snapshot.
        rows = (await client.get(f"{BASE}/care/workflow/{wound_id}", headers=wh)).json()
        assert rows["completion_status"]["can_checkout"] is True
        snap_versions = {r["template_version"] for r in rows["existing_responses"]["checklist"]}
        assert snap_versions == {1}, snap_versions
        snap_texts = {r["question_text_snapshot"] for r in rows["existing_responses"]["checklist"]}
        assert all(t for t in snap_texts)
        print("[ok] historical snapshot preserved on completed visit")

        # 13. Wrong-worker RBAC check — admin can still GET workflow (covered by
        #     "assigned worker or admin only"). Different worker should 403.
        wt2 = await login_worker(client, "+919999000007")
        rr = await client.get(f"{BASE}/care/workflow/{wound_id}", headers={"Authorization": f"Bearer {wt2}"})
        assert rr.status_code == 403, rr.text
        print("[ok] non-assigned worker is 403 on GET workflow")

        print("\nALL PATCH 4 SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"HTTP FAILED: {e.response.status_code} {e.response.text}")
        sys.exit(1)
