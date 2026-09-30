"""Offline unit tests for the booking / visit / report hardening.

    python -m unittest tests.test_hardening_offline -v

Runs with no server, database or network (same approach as
tests/test_invoice_pdf.py): third-party imports are stubbed by
tests/_offline_stubs, every line of application logic under test is real.

WHAT THIS PROVES: the pure decision logic — IST slot/expiry maths, upcoming
bucketing, arrival geofence, catalogue/test-package blocking, vitals integrity,
report finalization, dispatch eligibility building blocks, invoice/receipt
labelling and PDF honesty.
WHAT IT DOES NOT PROVE: SQL validity, row locking, ON CONFLICT behaviour,
Redis atomics or HTTP wiring. Those need the live-server suite
(pytest tests/) against Postgres+Redis; concurrency tests (parallel accept /
verify / OTP) have NOT been written yet.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from tests import _offline_stubs

_offline_stubs.install()

# fastapi is not part of the offline stub set; the modules under test only need
# HTTPException from it.
_FAKE_FASTAPI = "fastapi" not in sys.modules
if _FAKE_FASTAPI:
    _fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail=None, headers=None):
            self.status_code, self.detail = status_code, detail

    _fa.HTTPException = HTTPException
    sys.modules["fastapi"] = _fa


def tearDownModule() -> None:
    _offline_stubs.uninstall()
    if _FAKE_FASTAPI:
        sys.modules.pop("fastapi", None)


from app.core import timeutil  # noqa: E402
from app.services import catalog_guard, geofence, report_lock, vitals_integrity  # noqa: E402
from app.models.enums import ServiceRiskLevel  # noqa: E402

IST = timeutil.IST


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def booking(day, start, dur=60, status="confirmed", worker_id=None, **kw):
    return SimpleNamespace(
        scheduled_date=day, scheduled_start_time=start,
        scheduled_duration_minutes=dur, status=status, worker_id=worker_id, **kw,
    )


# --------------------------------------------------------------------------- 2
class TestSlotExpiryAndTimezone(unittest.TestCase):
    def test_ist_wall_clock_is_converted_not_relabelled(self):
        # 10:00 IST == 04:30 UTC. The old code called it 10:00 UTC.
        self.assertEqual(
            timeutil.slot_start_utc(date(2026, 10, 1), time(10, 0)), utc(2026, 10, 1, 4, 30)
        )

    def test_late_evening_ist_rolls_into_previous_utc_day(self):
        self.assertEqual(
            timeutil.slot_start_utc(date(2026, 10, 1), time(1, 0)), utc(2026, 9, 30, 19, 30)
        )

    def test_expired_boundaries_with_grace(self):
        d, t = date(2026, 10, 1), time(10, 0)  # starts 04:30 UTC
        start = utc(2026, 10, 1, 4, 30)
        self.assertFalse(timeutil.is_slot_expired(d, t, now=start - timedelta(minutes=1)))
        self.assertFalse(timeutil.is_slot_expired(d, t, now=start))
        self.assertFalse(timeutil.is_slot_expired(d, t, now=start + timedelta(minutes=5)))  # grace edge
        self.assertTrue(timeutil.is_slot_expired(d, t, now=start + timedelta(minutes=5, seconds=1)))

    def test_bug_window_5h30_is_closed(self):
        # 10:00 IST today; it is 11:00 IST (05:30 UTC). Old UTC-relabel logic
        # thought the slot was 5.5h in the FUTURE and accepted payment.
        b = booking(date(2026, 10, 1), time(10, 0))
        now = utc(2026, 10, 1, 5, 30)
        self.assertTrue(timeutil.is_booking_expired(b, now=now))

    def test_no_start_time_expires_end_of_ist_day(self):
        d = date(2026, 10, 1)
        self.assertFalse(timeutil.is_slot_expired(d, None, now=utc(2026, 10, 1, 17, 0)))   # 22:30 IST
        self.assertTrue(timeutil.is_slot_expired(d, None, now=utc(2026, 10, 1, 18, 31)))   # 00:01 IST next day

    def test_naive_now_is_treated_as_utc(self):
        self.assertTrue(
            timeutil.is_slot_expired(date(2026, 9, 1), time(9, 0), now=datetime(2026, 10, 1, 0, 0))
        )


class TestUpcomingBucket(unittest.TestCase):
    NOW = utc(2026, 10, 1, 6, 0)  # 11:30 IST

    def bucket(self, **kw):
        return timeutil.time_bucket(booking(**kw), now=self.NOW)

    def test_past_unpaid_is_never_upcoming(self):
        self.assertEqual(self.bucket(day=date(2026, 9, 29), start=time(9, 0), status="pending_payment"), "past")

    def test_past_confirmed_unassigned_is_past(self):
        self.assertEqual(self.bucket(day=date(2026, 10, 1), start=time(9, 0), status="confirmed"), "past")

    def test_future_paid_is_upcoming(self):
        self.assertEqual(self.bucket(day=date(2026, 10, 2), start=time(9, 0), status="confirmed"), "upcoming")

    def test_started_visit_is_active_and_terminal_is_past(self):
        self.assertEqual(self.bucket(day=date(2026, 9, 1), start=time(9, 0), status="in_progress"), "active")
        self.assertEqual(self.bucket(day=date(2026, 10, 2), start=time(9, 0), status="cancelled"), "past")
        self.assertEqual(self.bucket(day=date(2026, 10, 2), start=time(9, 0), status="completed"), "past")

    def test_assigned_slot_still_running_is_active(self):
        # 10:00 IST + 3h window still running at 11:30 IST
        self.assertEqual(
            self.bucket(day=date(2026, 10, 1), start=time(10, 0), dur=180, status="assigned", worker_id="w"),
            "active",
        )


# --------------------------------------------------------------------------- 4
class TestArrivalGeofence(unittest.TestCase):
    CUST = (17.4400, 78.3489)

    def chk(self, lat, lng, **kw):
        return geofence.check_arrival(
            nurse_lat=lat, nurse_lng=lng, customer_lat=self.CUST[0], customer_lng=self.CUST[1], **kw
        )

    def test_at_the_door_passes(self):
        r = self.chk(17.44005, 78.34892)
        self.assertTrue(r.ok, r)
        self.assertLessEqual(r.distance_m, 20)

    def test_two_km_away_is_rejected_even_though_otp_would_be_valid(self):
        r = self.chk(17.4580, 78.3489)  # ~1.55 km north
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "NOT_AT_CUSTOMER_LOCATION")
        self.assertGreater(r.distance_m, 1000)

    def test_just_outside_radius_rejected_just_inside_accepted(self):
        # ~0.00135 deg lat ~ 150 m
        self.assertTrue(self.chk(17.4400 + 0.0012, 78.3489).ok)
        self.assertFalse(self.chk(17.4400 + 0.0016, 78.3489).ok)

    def test_null_island_and_garbage_coordinates_rejected(self):
        for lat, lng in [(0, 0), (None, None), (float("nan"), 78.0), (91, 78), (17, 181), ("x", 1)]:
            r = self.chk(lat, lng)
            self.assertFalse(r.ok, (lat, lng))
            self.assertEqual(r.code, "NURSE_LOCATION_REQUIRED")

    def test_missing_customer_location_fails_closed(self):
        r = geofence.check_arrival(nurse_lat=17.44, nurse_lng=78.35, customer_lat=None, customer_lng=None)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "CUSTOMER_LOCATION_UNAVAILABLE")

    def test_stale_or_future_fix_rejected(self):
        now = utc(2026, 10, 1, 6, 0)
        stale = self.chk(*self.CUST, fix_captured_at=now - timedelta(minutes=10), now=now)
        self.assertEqual(stale.code, "NURSE_LOCATION_STALE")
        future = self.chk(*self.CUST, fix_captured_at=now + timedelta(minutes=10), now=now)
        self.assertEqual(future.code, "NURSE_LOCATION_STALE")
        fresh = self.chk(*self.CUST, fix_captured_at=now - timedelta(seconds=20), now=now)
        self.assertTrue(fresh.ok)

    def test_poor_accuracy_rejected_and_tolerance_is_capped(self):
        self.assertEqual(self.chk(*self.CUST, accuracy_m=500).code, "NURSE_LOCATION_INACCURATE")
        # 190 m accuracy widens by at most the radius (150), never unbounded:
        far = self.chk(17.4400 + 0.0040, 78.3489, accuracy_m=190)  # ~445 m away
        self.assertFalse(far.ok)


# --------------------------------------------------------------------------- 3
class TestCatalogGuard(unittest.TestCase):
    def prod(self, value=True, allow=False):
        return mock.patch.object(
            catalog_guard, "settings",
            SimpleNamespace(is_production=value, ALLOW_TEST_CATALOG_ITEMS=allow),
        )

    def svc(self, name="Wound Dressing", code="SVC-100", risk=ServiceRiskLevel.LOW, **kw):
        base = dict(name=name, service_code=code, risk_level=risk, is_active=True,
                    checklist_template_id=None, documentation_template_id=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_the_reported_test_row_is_detected(self):
        row = self.svc("High-Risk Service (template missing — test only)", "SVC-HR-TEST", ServiceRiskLevel.HIGH)
        self.assertTrue(catalog_guard.is_test_only(row))

    def test_blocked_in_production_and_staging_but_visible_in_dev(self):
        row = self.svc("High-Risk Service (template missing — test only)", risk=ServiceRiskLevel.HIGH)
        with self.prod(True):
            self.assertEqual(catalog_guard.unbookable_reason(row), "ITEM_TEST_ONLY")
            self.assertFalse(catalog_guard.is_publicly_visible(row))
        with self.prod(False):
            self.assertIsNone(catalog_guard.unbookable_reason(row))

    def test_explicit_allow_flag_lets_the_test_suite_through(self):
        row = self.svc("X (test only)")
        with self.prod(True, allow=True):
            self.assertIsNone(catalog_guard.unbookable_reason(row))

    def test_high_risk_without_any_template_is_unbookable_in_production(self):
        row = self.svc("Central Line Care", "SVC-CL", ServiceRiskLevel.HIGH)
        with self.prod():
            self.assertEqual(catalog_guard.unbookable_reason(row), "ITEM_TEMPLATE_MISSING")

    def test_template_on_item_or_primary_service_makes_it_sellable(self):
        with_tpl = self.svc("Central Line Care", "SVC-CL", ServiceRiskLevel.HIGH, checklist_template_id="t1")
        pkg = SimpleNamespace(name="Line Package", package_code="PKG-LINE", risk_level=ServiceRiskLevel.HIGH,
                              is_active=True, is_deleted=False,
                              checklist_template_id=None, documentation_template_id=None)
        with self.prod():
            self.assertIsNone(catalog_guard.unbookable_reason(with_tpl))
            self.assertEqual(catalog_guard.unbookable_reason(pkg), "ITEM_TEMPLATE_MISSING")
            self.assertIsNone(catalog_guard.unbookable_reason(pkg, fallback_items=(with_tpl,)))

    def test_low_risk_without_template_is_fine_and_normal_names_are_not_flagged(self):
        with self.prod():
            self.assertIsNone(catalog_guard.unbookable_reason(self.svc()))
            self.assertFalse(catalog_guard.is_test_only(self.svc("Diabetes Home Care Plan", "PKG-DIAB")))
            self.assertFalse(catalog_guard.is_test_only(self.svc("Latest Wound Care", "SVC-9")))

    def test_deleted_and_inactive_are_never_bookable_anywhere(self):
        with self.prod(False):
            self.assertEqual(catalog_guard.unbookable_reason(self.svc(is_active=False)), "ITEM_INACTIVE")
            self.assertEqual(catalog_guard.unbookable_reason(self.svc(is_deleted=True)), "ITEM_DELETED")
            self.assertEqual(catalog_guard.unbookable_reason(None), "NOT_FOUND")


# --------------------------------------------------------------------------- 7
class TestVitalsIntegrity(unittest.TestCase):
    def test_empty_payload_is_rejected_not_stored_as_a_reading(self):
        with self.assertRaises(Exception) as cm:
            vitals_integrity.assert_valid_vitals({})
        self.assertEqual(cm.exception.detail["code"], "VITALS_INVALID")
        with self.assertRaises(Exception):
            vitals_integrity.assert_valid_vitals({"pulse": None, "spo2": None, "measurement_device": "x"})

    def test_placeholder_zeros_and_impossible_values_rejected(self):
        for bad in ({"pulse": 0}, {"spo2": 400}, {"temperature_f": 9.8}, {"pain_score": 11},
                    {"bp_systolic": 0, "bp_diastolic": 0}, {"pulse": -5}):
            self.assertTrue(vitals_integrity.validate_vitals(bad), bad)

    def test_bp_needs_both_and_systolic_above_diastolic(self):
        self.assertTrue(vitals_integrity.validate_vitals({"bp_systolic": 120}))
        self.assertTrue(vitals_integrity.validate_vitals({"bp_systolic": 80, "bp_diastolic": 120}))
        self.assertEqual(vitals_integrity.validate_vitals({"bp_systolic": 120, "bp_diastolic": 80}), [])

    def test_optional_data_may_stay_missing(self):
        self.assertEqual(vitals_integrity.validate_vitals({"pulse": 72}), [])
        self.assertEqual(vitals_integrity.validate_vitals({"spo2": 97, "temperature_f": Decimal("98.6")}), [])

    def test_vitals_entry_answers_must_carry_a_real_measurement(self):
        self.assertFalse(vitals_integrity.vitals_dict_is_meaningful({}))
        self.assertFalse(vitals_integrity.vitals_dict_is_meaningful({"notes": "patient fine"}))
        self.assertFalse(vitals_integrity.vitals_dict_is_meaningful({"pulse": 900}))
        self.assertTrue(vitals_integrity.vitals_dict_is_meaningful({"pulse": 72, "notes": "resting"}))

    def test_required_vitals_checklist_item_is_not_completed_by_a_stub(self):
        from app.services.care_workflow_engine import _is_question_complete
        self.assertFalse(_is_question_complete("vitals_entry", {"notes": "x"}))
        self.assertFalse(_is_question_complete("vitals_entry", {}))
        self.assertTrue(_is_question_complete("vitals_entry", {"pulse": 80}))

    def test_fallback_family_summary_makes_no_clinical_claim(self):
        from app.services.care_workflow_engine import _FALLBACK_FAMILY_SUMMARY
        self.assertNotIn("as planned", _FALLBACK_FAMILY_SUMMARY.lower())


# --------------------------------------------------------------------------- 6
class TestReportImmutability(unittest.TestCase):
    def visit(self, **kw):
        base = dict(report_finalized_at=None, check_out_at=None, status="in_progress",
                    care_notes="n", family_summary="s", checklist_responses={"a": 1},
                    documentation_responses=None, check_in_at=utc(2026, 10, 1, 5), report_finalized_by=None,
                    report_content_hash=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_open_draft_is_editable(self):
        report_lock.assert_report_editable(self.visit())

    def test_finalized_by_stamp_checkout_or_completed_status_is_locked(self):
        from app.models.enums import VisitStatus
        for v in (self.visit(report_finalized_at=utc(2026, 10, 1, 6)),
                  self.visit(check_out_at=utc(2026, 10, 1, 6)),          # legacy row, no stamp
                  self.visit(status=VisitStatus.completed)):             # legacy row
            with self.assertRaises(Exception) as cm:
                report_lock.assert_report_editable(v)
            self.assertEqual(cm.exception.status_code, 409)
            self.assertEqual(cm.exception.detail["code"], "REPORT_FINALIZED")

    def test_finalize_stamps_once_and_never_overwrites(self):
        v = self.visit(check_out_at=utc(2026, 10, 1, 6, 30))
        report_lock.finalize_report(v, "user-1")
        first = (v.report_finalized_at, v.report_finalized_by, v.report_content_hash)
        self.assertEqual(v.report_finalized_at, utc(2026, 10, 1, 6, 30))
        self.assertEqual(len(v.report_content_hash), 64)
        v.care_notes = "tampered"
        report_lock.finalize_report(v, "admin-9")
        self.assertEqual((v.report_finalized_at, v.report_finalized_by, v.report_content_hash), first)

    def test_hash_detects_content_change_and_is_stable(self):
        a, b = self.visit(), self.visit()
        self.assertEqual(report_lock.compute_report_hash(a), report_lock.compute_report_hash(b))
        b.family_summary = "different"
        self.assertNotEqual(report_lock.compute_report_hash(a), report_lock.compute_report_hash(b))

    def test_no_visit_is_not_locked(self):
        report_lock.assert_report_editable(None)


# --------------------------------------------------------------------------- 1
class TestDispatchBuildingBlocks(unittest.TestCase):
    def setUp(self):
        from app.services import dispatch
        from app.models.enums import BookingStatus
        self.d, self.BS = dispatch, BookingStatus

    def open_booking(self, **kw):
        base = dict(worker_id=None, status=self.BS.confirmed,
                    scheduled_date=date(2026, 10, 2), scheduled_start_time=time(10, 0),
                    scheduled_duration_minutes=60)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_block_reasons(self):
        now = utc(2026, 10, 1, 6)
        self.assertIsNone(self.d.booking_dispatch_block_reason(self.open_booking(), now=now))
        self.assertEqual(self.d.booking_dispatch_block_reason(self.open_booking(worker_id="w"), now=now), "ALREADY_ASSIGNED")
        self.assertEqual(self.d.booking_dispatch_block_reason(self.open_booking(status=self.BS.cancelled), now=now), "NOT_DISPATCHABLE_STATUS")
        self.assertEqual(self.d.booking_dispatch_block_reason(self.open_booking(status=self.BS.pending_payment), now=now), "NOT_DISPATCHABLE_STATUS")
        self.assertEqual(
            self.d.booking_dispatch_block_reason(self.open_booking(scheduled_date=date(2026, 9, 30)), now=now),
            "SLOT_EXPIRED",
        )

    def test_overlap_uses_ist_windows_and_adjacent_slots_do_not_conflict(self):
        a = self.open_booking(scheduled_start_time=time(10, 0), scheduled_duration_minutes=60)
        overlap = self.open_booking(scheduled_start_time=time(10, 30))
        adjacent = self.open_booking(scheduled_start_time=time(11, 0))
        self.assertTrue(self.d._overlaps(a, overlap))
        self.assertFalse(self.d._overlaps(a, adjacent))

    def test_overnight_visit_conflicts_across_the_ist_date_boundary(self):
        night = self.open_booking(scheduled_date=date(2026, 10, 2), scheduled_start_time=time(22, 0),
                                  scheduled_duration_minutes=8 * 60)
        early = self.open_booking(scheduled_date=date(2026, 10, 3), scheduled_start_time=time(3, 0))
        self.assertTrue(self.d._overlaps(night, early))  # the old same-day prefilter missed this

    def test_wave_window_now_reaches_wave_two_and_three_rings(self):
        from app.services.proximity import radius_for_wave
        self.assertEqual([radius_for_wave(w, False) for w in (1, 2, 3, 4)], [5, 8, 12, 12])


# --------------------------------------------------------------------------- 8
class TestInvoiceUsesPurchasedOffering(unittest.TestCase):
    def test_legacy_component_carries_the_package_name(self):
        from app.services import pricing_resolver as pr
        b = SimpleNamespace(base_amount=Decimal("999"), surge_amount=Decimal("0"), tax_amount=Decimal("0"))
        self.assertEqual(pr._legacy_component(b, "Diabetes Care Plan — Monthly").label, "Diabetes Care Plan — Monthly")
        self.assertEqual(pr._legacy_component(b).label, "Professional Nursing Service")  # only when nothing is known

    def test_build_components_relabels_generic_rate_card_rows_only(self):
        from app.services import pricing_resolver as pr
        generic = pr.PricingComponent(code="service", label="Professional Nursing Service",
                                      input_amount=Decimal("500"), basis="customer_rate")
        specific = pr.PricingComponent(code="kit", label="Consumables Kit", input_amount=Decimal("100"),
                                       basis="customer_rate")
        with mock.patch.object(pr, "resolve_offering_name", mock.AsyncMock(return_value="Post-Op Wound Care")), \
             mock.patch.object(pr, "load_rate_card", mock.AsyncMock(return_value=[generic, specific])):
            out = asyncio.run(pr.build_components(None, SimpleNamespace()))
        self.assertEqual([c.label for c in out], ["Post-Op Wound Care", "Consumables Kit"])

    def test_build_components_without_rate_card_uses_offering_name(self):
        from app.services import pricing_resolver as pr
        b = SimpleNamespace(base_amount=Decimal("300"), surge_amount=Decimal("0"), tax_amount=Decimal("0"))
        with mock.patch.object(pr, "resolve_offering_name", mock.AsyncMock(return_value="Elder Care Basic")), \
             mock.patch.object(pr, "load_rate_card", mock.AsyncMock(return_value=[])):
            out = asyncio.run(pr.build_components(None, b))
        self.assertEqual(out[0].label, "Elder Care Basic")

    def test_receipt_pdf_shows_package_or_service_and_its_label(self):
        from tests.test_invoice_pdf import COMPANY, extract_text
        from app.services.payment_receipt_pdf import render_payment_receipt_pdf
        common = dict(company=COMPANY, receipt_number="R-1", receipt_date=date(2026, 10, 1),
                      booking_ref="BK-1", patient_name="A B", package_code="CODE-1",
                      service_period="01-Oct-2026", payment_id="pay_1", payment_datetime=None,
                      payment_method_label="Razorpay (Online)", payment_status_label="Captured",
                      amount_paid=Decimal("500"))
        pkg = extract_text(render_payment_receipt_pdf(package_name="Diabetes Care Plan", **common))
        self.assertIn("Care Package: Diabetes Care Plan", pkg)
        svc = extract_text(render_payment_receipt_pdf(package_name="Injection at Home",
                                                      offering_label="Service", **common))
        self.assertIn("Service: Injection at Home", svc)
        self.assertNotIn("Care Package", svc)


# --------------------------------------------------------------------------- 7 (PDF)
class TestVisitReportPdfHonesty(unittest.TestCase):
    def render(self, vitals):
        from tests.test_invoice_pdf import COMPANY, extract_text
        from app.services.visit_report_pdf import PdfWatermark, render_visit_report_pdf
        # The real renderer encrypts the PDF (anti-copy deterrent); the text
        # extractor can't read RC4/AES streams, so encryption is switched off
        # for the assertion only. Content generation is the real code path.
        from app.services import visit_report_pdf as _vrp
        with mock.patch.object(_vrp, "StandardEncryption", lambda *a, **k: None):
            pdf = self._render(render_visit_report_pdf, COMPANY, PdfWatermark, vitals)
        return extract_text(pdf)

    def _render(self, render_visit_report_pdf, COMPANY, PdfWatermark, vitals):
        pdf = render_visit_report_pdf(
            company=COMPANY, booking_ref="BK-1", patient_name="Pat", nurse_name="Nurse",
            nurse_council_no=None, check_in_at=utc(2026, 10, 1, 5), check_out_at=utc(2026, 10, 1, 6),
            duration_minutes=60, vitals=vitals, summary_text="Visit done.", summary_label="Family summary",
            include_clinical_notes=False,
            watermark=PdfWatermark(viewer_name="V", viewer_role="family",
                                   generated_at=utc(2026, 10, 1, 7), ref="ref"),
        )
        return pdf

    def test_no_vitals_says_so_and_prints_no_numbers(self):
        for v in (None, {}, {"bp_systolic": None, "bp_diastolic": None, "pulse": None,
                                "spo2": None, "temperature_f": None}):
            text = self.render(v)
            self.assertIn("No vitals were recorded during this visit.", text)
            for label in ("Blood pressure", "Heart rate", "SpO2", "Temperature"):
                self.assertNotIn(label, text)

    def test_only_recorded_vitals_are_printed(self):
        text = self.render({"pulse": 72, "spo2": None, "bp_systolic": None, "bp_diastolic": None,
                            "temperature_f": None})
        self.assertIn("Heart rate", text)
        self.assertIn("72 bpm", text)
        self.assertNotIn("SpO2", text)
        self.assertNotIn("Blood pressure", text)


if __name__ == "__main__":
    unittest.main()
