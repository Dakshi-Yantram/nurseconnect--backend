"""End-to-end money flow: pricing -> invoice -> payout -> admin release.

    python -m unittest tests.test_payment_flow -v

Walks a booking through the whole feature with the real modules, using the
offline harness for the database and Razorpay. What it verifies is the thing
that matters most across a payments feature: that the same rupee is described
consistently at every stage, and that nothing is created or lost between the
customer's invoice and the nurse's bank transfer.

The reconciliation identity everything is checked against:

    customer_total  ==  worker_gross + platform_fee_gross + total_GST
    final_disbursal ==  worker_gross - deductions

If either drifts, someone is either short-paid or the platform is eating a
loss, so both are asserted at every stage rather than only at the end.

Environment limits: there is no Postgres and no RazorpayX account here, so
SQL validity and real transfers are NOT covered. See the report.
"""
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from tests import _offline_stubs

_offline_stubs.install()

from tests._offline_stubs import FakeResult, FakeSession  # noqa: E402

from app.models.enums import PayoutApprovalStatus, WorkerPayoutStatus  # noqa: E402
from app.services import payout_service  # noqa: E402
from app.services.pricing_engine import (  # noqa: E402
    Deduction,
    PricingComponent,
    admin_view,
    compute_breakdown,
    customer_view,
    money,
)
from app.services.payout_service import release_payout  # noqa: E402
from tests.test_payout_release import FakeRazorpay, make_payout, make_worker  # noqa: E402

D = Decimal

logging.getLogger("app.services.payout_service").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Three representative rate cards, matching the three invoice templates.
# ---------------------------------------------------------------------------
EXEMPT_NURSING = [
    PricingComponent(code="nursing", label="Injection Administration",
                     input_amount=D("480.00"), basis="earning_rate",
                     gst_rate_pct=D("0"), sac_code="999314"),
    PricingComponent(code="kit", label="Consumables Kit",
                     input_amount=D("150.00"), gst_rate_pct=D("18"),
                     sac_code="998599", earns_to="platform"),
]

TAXABLE_NON_MEDIC = [
    PricingComponent(code="escort", label="Elder Escort Companion",
                     input_amount=D("900.00"), gst_rate_pct=D("18"),
                     sac_code="999334"),
]

TELECONSULT = [
    PricingComponent(code="consult", label="Medical Tele-Consultation",
                     input_amount=D("350.00"), gst_rate_pct=D("0"),
                     sac_code="999312"),
    PricingComponent(code="access", label="Digital Health Platform Access",
                     input_amount=D("150.00"), gst_rate_pct=D("18"),
                     sac_code="998413", earns_to="platform"),
]


def reconcile(breakdown, case: str, test: unittest.TestCase) -> None:
    """The identity that must hold for every booking, in every GST regime."""
    test.assertEqual(
        breakdown.customer_total,
        money(breakdown.worker_gross + breakdown.platform_fee_gross
              + breakdown.total_gst - breakdown.subsidy_amount),
        f"{case}: customer total does not reconcile to the split + GST",
    )
    test.assertEqual(
        money(breakdown.worker_gross + breakdown.platform_fee_gross),
        breakdown.subtotal,
        f"{case}: split does not re-sum to the pre-GST value",
    )
    test.assertEqual(
        breakdown.total_cgst + breakdown.total_sgst,
        breakdown.total_gst,
        f"{case}: CGST + SGST != total GST",
    )
    test.assertEqual(
        breakdown.worker_net_payable,
        money(breakdown.worker_gross - breakdown.total_deductions),
        f"{case}: payout does not reconcile to earnings minus deductions",
    )


class TestStage1Pricing(unittest.TestCase):
    """Payment stage: the amount charged to the customer."""

    def test_all_three_gst_regimes_reconcile(self):
        for name, card in (("exempt+taxable", EXEMPT_NURSING),
                           ("fully taxable", TAXABLE_NON_MEDIC),
                           ("teleconsult", TELECONSULT)):
            with self.subTest(case=name):
                reconcile(compute_breakdown(card, default_commission_pct=D("20")),
                          name, self)

    def test_gst_is_not_applied_to_exempt_lines(self):
        b = compute_breakdown(EXEMPT_NURSING, default_commission_pct=D("20"))
        nursing, kit = b.lines
        self.assertEqual(nursing.gst_amount, D("0.00"))
        self.assertGreater(kit.gst_amount, D("0.00"))
        self.assertEqual(b.exempt_value, D("600.00"))
        self.assertEqual(b.taxable_value, D("127.12"))

    def test_customer_total_is_what_razorpay_would_be_charged(self):
        b = compute_breakdown(EXEMPT_NURSING, default_commission_pct=D("20"))
        self.assertEqual(b.customer_total, D("750.00"))
        self.assertEqual(int(b.customer_total * 100), 75000)  # paise


class TestStage2Invoice(unittest.TestCase):
    """Invoice stage: what the customer's document says."""

    def setUp(self):
        self.breakdown = compute_breakdown(EXEMPT_NURSING,
                                           default_commission_pct=D("20"))
        self.view = customer_view(self.breakdown)

    def test_invoice_totals_match_the_amount_charged(self):
        self.assertEqual(D(self.view["total_amount"]), self.breakdown.customer_total)

    def test_invoice_line_totals_sum_to_the_invoice_total(self):
        summed = money(sum(D(i["line_total"]) for i in self.view["line_items"]))
        self.assertEqual(summed, D(self.view["total_amount"]))

    def test_invoice_carries_no_internal_split(self):
        import json

        blob = json.dumps(self.view).lower()
        for leak in ("commission", "platform_fee", "worker", "payout"):
            self.assertNotIn(leak, blob)

    def test_admin_view_of_the_same_invoice_shows_everything(self):
        adm = admin_view(self.breakdown)
        self.assertEqual(D(adm["customer"]["total_amount"]),
                         self.breakdown.customer_total)
        self.assertEqual(adm["split"]["worker_share_pct"], "80")
        self.assertEqual(D(adm["split"]["worker_gross"]), D("480.00"))


class TestStage3PayoutCalculation(unittest.TestCase):
    """Payout stage: what the nurse is owed for the same booking."""

    def test_nurse_earns_80_percent_of_her_own_lines_only(self):
        b = compute_breakdown(EXEMPT_NURSING, default_commission_pct=D("20"))
        # The kit is the platform's supply — it must not inflate her payout.
        self.assertEqual(b.worker_service_value, D("600.00"))
        self.assertEqual(b.worker_gross, D("480.00"))
        self.assertEqual(b.worker_platform_fee_gross, D("120.00"))

    def test_platform_fee_gst_is_carved_out_of_the_fee(self):
        b = compute_breakdown(EXEMPT_NURSING, default_commission_pct=D("20"))
        self.assertEqual(
            money(b.worker_platform_fee_taxable + b.worker_platform_fee_gst),
            b.worker_platform_fee_gross,
        )

    def test_e_stamp_recovery_reduces_only_the_disbursal(self):
        b = compute_breakdown(
            EXEMPT_NURSING, default_commission_pct=D("20"),
            deductions=[Deduction(code="estamp", label="E-Stamp Advance",
                                  amount=D("50.00"), note="Inst. 1 of 4")],
        )
        self.assertEqual(b.customer_total, D("750.00"))   # customer unaffected
        self.assertEqual(b.worker_gross, D("480.00"))
        self.assertEqual(b.worker_net_payable, D("430.00"))
        reconcile(b, "with e-stamp", self)

    def test_taxable_package_payout_excludes_government_gst(self):
        """The nurse's 80% is computed on the pre-tax value; the 18% GST
        collected belongs to the government, not to either party."""
        b = compute_breakdown(TAXABLE_NON_MEDIC, default_commission_pct=D("20"))
        self.assertEqual(b.customer_total, D("900.00"))
        self.assertEqual(b.taxable_value, D("762.71"))
        self.assertEqual(b.worker_gross, D("610.17"))
        self.assertEqual(money(b.worker_gross + b.platform_fee_gross), D("762.71"))


class TestStage4AdminRelease(unittest.IsolatedAsyncioTestCase):
    """Release stage: the calculated amount reaching Razorpay unchanged."""

    async def test_amount_released_equals_the_calculated_disbursal(self):
        b = compute_breakdown(
            EXEMPT_NURSING, default_commission_pct=D("20"),
            deductions=[Deduction(code="estamp", label="E-Stamp",
                                  amount=D("50.00"))],
        )
        payout = make_payout(gross_amount=b.worker_service_value,
                             net_amount=b.worker_net_payable)

        session = FakeSession()
        session.queue(FakeResult(make_worker()))
        rp = FakeRazorpay()
        with patch.object(payout_service, "razorpay_client", rp):
            result = await release_payout(session, payout, released_by=uuid4())

        self.assertEqual(result["status"], "paid")
        # 430.00 -> 43000 paise, with no drift introduced by the transfer.
        self.assertEqual(rp.create_calls[0]["amount_paise"],
                         int(b.worker_net_payable * 100))
        self.assertEqual(D(result["amount"]), b.worker_net_payable)

    async def test_full_chain_conserves_every_rupee(self):
        """Customer pays X; platform keeps its fee + GST; nurse receives the
        rest less recoveries. X must equal the sum of those parts exactly."""
        estamp = D("50.00")
        b = compute_breakdown(
            EXEMPT_NURSING, default_commission_pct=D("20"),
            deductions=[Deduction(code="estamp", label="E-Stamp", amount=estamp)],
        )
        payout = make_payout(gross_amount=b.worker_service_value,
                             net_amount=b.worker_net_payable)

        session = FakeSession()
        session.queue(FakeResult(make_worker()))
        rp = FakeRazorpay()
        with patch.object(payout_service, "razorpay_client", rp):
            await release_payout(session, payout, released_by=uuid4())

        paid_to_nurse = D(rp.create_calls[0]["amount_paise"]) / 100
        platform_keeps = b.platform_fee_gross + estamp
        government_gets = b.total_gst

        self.assertEqual(
            money(paid_to_nurse + platform_keeps + government_gets),
            b.customer_total,
            "money was created or lost between invoice and payout",
        )

    async def test_release_is_blocked_until_the_booking_is_approved(self):
        payout = make_payout(approval_status=PayoutApprovalStatus.pending_approval)
        session = FakeSession()
        session.queue(FakeResult(make_worker()))
        rp = FakeRazorpay()
        with patch.object(payout_service, "razorpay_client", rp):
            result = await release_payout(session, payout, released_by=uuid4())
        self.assertIn("error", result)
        self.assertEqual(rp.create_calls, [])

    async def test_unconfirmed_release_does_not_report_success(self):
        payout = make_payout()
        session = FakeSession()
        session.queue(FakeResult(make_worker()))
        rp = FakeRazorpay(create_response={"id": "pout_Q", "status": "queued"})
        with patch.object(payout_service, "razorpay_client", rp):
            result = await release_payout(session, payout, released_by=uuid4())

        self.assertNotEqual(result["status"], "paid")
        self.assertEqual(payout.status, WorkerPayoutStatus.processing)
        self.assertIsNone(payout.paid_at)


class TestRateCardIsTheSinglePointOfChange(unittest.TestCase):
    """The modular-table requirement: one edit moves everything together."""

    def test_changing_one_input_moves_customer_nurse_and_gst_coherently(self):
        before = compute_breakdown(EXEMPT_NURSING, default_commission_pct=D("20"))

        raised = [
            PricingComponent(code="nursing", label="Injection Administration",
                             input_amount=D("560.00"), basis="earning_rate",
                             gst_rate_pct=D("0"), sac_code="999314"),
            EXEMPT_NURSING[1],
        ]
        after = compute_breakdown(raised, default_commission_pct=D("20"))

        self.assertEqual(before.worker_gross, D("480.00"))
        self.assertEqual(after.worker_gross, D("560.00"))
        self.assertEqual(after.customer_total, D("850.00"))  # 700 + 150
        reconcile(after, "raised nurse rate", self)

    def test_changing_the_commission_moves_only_the_split(self):
        """At a customer_rate basis the customer price is fixed, so a
        commission change must move the split without moving the invoice."""
        card = [PricingComponent(code="n", label="Nursing",
                                 input_amount=D("600.00"), gst_rate_pct=D("0"))]
        at20 = compute_breakdown(card, default_commission_pct=D("20"))
        at30 = compute_breakdown(card, default_commission_pct=D("30"))

        self.assertEqual(at20.customer_total, at30.customer_total)
        self.assertEqual(at20.worker_gross, D("480.00"))
        self.assertEqual(at30.worker_gross, D("420.00"))
        reconcile(at30, "30pct commission", self)

    def test_flipping_a_line_to_exempt_removes_its_gst(self):
        taxable = compute_breakdown(TAXABLE_NON_MEDIC, default_commission_pct=D("20"))
        exempted = compute_breakdown(
            [PricingComponent(code="escort", label="Elder Escort Companion",
                              input_amount=D("900.00"), gst_rate_pct=D("0"),
                              sac_code="999334")],
            default_commission_pct=D("20"),
        )
        self.assertEqual(taxable.total_gst, D("137.29"))
        self.assertEqual(exempted.total_gst, D("0.00"))
        self.assertEqual(exempted.customer_total, D("900.00"))
        reconcile(exempted, "exempted escort", self)


if __name__ == "__main__":
    unittest.main(verbosity=2)
