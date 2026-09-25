"""Pricing / GST / commission engine tests.

Runs with no database, no network and no running server — the money maths is
pure `Decimal`, so it is verified in isolation:

    python -m unittest tests.test_pricing_engine -v

The invoice templates supplied by the business are used as fixtures: each
`test_template_*` reproduces one of them end to end and asserts the printed
figures. The rate-card inputs are declared inside the test, never in
application code, so nothing here leaks a hardcoded amount into production.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from app.services.pricing_engine import (
    Deduction,
    PricingComponent,
    add_exclusive,
    admin_view,
    compute_breakdown,
    customer_view,
    decompose_inclusive,
    money,
    split_cgst_sgst,
)

D = Decimal


class TestRounding(unittest.TestCase):
    def test_inclusive_split_is_exact(self):
        """taxable + gst must re-add to the gross, to the paisa."""
        for gross in ["150.00", "35.40", "900.00", "109.00", "1.00", "99999.99"]:
            taxable, gst = decompose_inclusive(D(gross), D("18"))
            self.assertEqual(taxable + gst, D(gross), f"drift on {gross}")

    def test_exclusive_split_is_exact(self):
        gst, total = add_exclusive(D("127.12"), D("18"))
        self.assertEqual(D("127.12") + gst, total)

    def test_exempt_rate_adds_nothing(self):
        taxable, gst = decompose_inclusive(D("620.00"), D("0"))
        self.assertEqual(taxable, D("620.00"))
        self.assertEqual(gst, D("0.00"))

    def test_cgst_sgst_always_re_add(self):
        """The odd paisa goes to SGST rather than being printed twice."""
        cgst, sgst = split_cgst_sgst(D("137.29"))
        self.assertEqual(cgst, D("68.64"))
        self.assertEqual(sgst, D("68.65"))
        self.assertEqual(cgst + sgst, D("137.29"))

        for gst in ["22.88", "5.40", "21.36", "0.01", "137.29"]:
            c, s = split_cgst_sgst(D(gst))
            self.assertEqual(c + s, D(gst), f"drift on {gst}")


class TestCommissionSplit(unittest.TestCase):
    def test_80_20_from_customer_rate(self):
        """Customer price in -> nurse gets 80%, platform 20%."""
        line = compute_breakdown(
            [PricingComponent(code="nursing", label="Nursing", input_amount=D("600.00"),
                              basis="customer_rate", gst_rate_pct=D("0"))],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertEqual(line.worker_earning, D("480.00"))
        self.assertEqual(line.platform_fee_gross, D("120.00"))
        self.assertEqual(line.line_total, D("600.00"))

    def test_80_20_from_earning_rate_is_the_exact_inverse(self):
        """Nurse's net in -> customer price grossed up so 20% fits inside."""
        line = compute_breakdown(
            [PricingComponent(code="nursing", label="Nursing", input_amount=D("480.00"),
                              basis="earning_rate", gst_rate_pct=D("0"))],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertEqual(line.worker_earning, D("480.00"))
        self.assertEqual(line.service_value, D("600.00"))
        self.assertEqual(line.platform_fee_gross, D("120.00"))

    def test_platform_fee_carries_its_own_18pct_gst(self):
        """The 20% cut is GST-inclusive: it splits into fee + GST on fee."""
        line = compute_breakdown(
            [PricingComponent(code="nursing", label="Nursing", input_amount=D("600.00"),
                              basis="customer_rate", gst_rate_pct=D("0"))],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertEqual(line.platform_fee_gross, D("120.00"))
        self.assertEqual(line.platform_fee_taxable, D("101.69"))
        self.assertEqual(line.platform_fee_gst, D("18.31"))
        self.assertEqual(line.platform_fee_taxable + line.platform_fee_gst,
                         line.platform_fee_gross)

    def test_platform_owned_line_earns_the_nurse_nothing(self):
        line = compute_breakdown(
            [PricingComponent(code="kit", label="Kit", input_amount=D("150.00"),
                              gst_rate_pct=D("18"), earns_to="platform")],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertEqual(line.worker_earning, D("0.00"))
        self.assertEqual(line.platform_fee_gross, D("127.12"))

    def test_per_component_commission_overrides_the_default(self):
        line = compute_breakdown(
            [PricingComponent(code="n", label="N", input_amount=D("1000.00"),
                              commission_pct=D("30"))],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertEqual(line.platform_fee_gross, D("300.00"))
        self.assertEqual(line.worker_earning, D("700.00"))


class TestMixedGst(unittest.TestCase):
    """18% must NOT be applied everywhere — exempt and taxable lines coexist."""

    def test_exempt_and_taxable_lines_on_one_invoice(self):
        b = compute_breakdown(
            [
                PricingComponent(code="nursing", label="Nursing", input_amount=D("620.00"),
                                 gst_rate_pct=D("0"), sac_code="999314"),
                PricingComponent(code="kit", label="Kit", input_amount=D("150.00"),
                                 gst_rate_pct=D("18"), earns_to="platform"),
            ],
            default_commission_pct=D("20"),
        )
        self.assertEqual(b.exempt_value, D("620.00"))
        self.assertEqual(b.taxable_value, D("127.12"))
        self.assertEqual(b.total_gst, D("22.88"))
        self.assertEqual(b.customer_total, D("770.00"))

    def test_exempt_line_carries_the_statutory_citation(self):
        line = compute_breakdown(
            [PricingComponent(code="n", label="N", input_amount=D("500.00"),
                              gst_rate_pct=D("0"))],
            default_commission_pct=D("20"),
        ).lines[0]
        self.assertIn("12/2017", line.exemption_note)

    def test_taxable_line_has_no_exemption_note(self):
        view = customer_view(compute_breakdown(
            [PricingComponent(code="k", label="K", input_amount=D("150.00"),
                              gst_rate_pct=D("18"))],
            default_commission_pct=D("20"),
        ))
        self.assertIsNone(view["line_items"][0]["exemption_note"])


class TestCustomerVisibility(unittest.TestCase):
    """The patient must never see the internal 80/20 split."""

    def setUp(self):
        self.breakdown = compute_breakdown(
            [
                PricingComponent(code="nursing", label="Nursing", input_amount=D("480.00"),
                                 basis="earning_rate", gst_rate_pct=D("0")),
                PricingComponent(code="kit", label="Kit", input_amount=D("150.00"),
                                 gst_rate_pct=D("18"), earns_to="platform"),
            ],
            default_commission_pct=D("20"),
        )

    def test_no_commission_field_anywhere_in_the_customer_view(self):
        import json

        blob = json.dumps(customer_view(self.breakdown)).lower()
        for leak in ("commission", "platform_fee", "worker_earning", "payout",
                     "worker_gross", "80", "split"):
            self.assertNotIn(leak, blob, f"customer view leaks {leak!r}")

    def test_customer_sees_one_consolidated_value_per_line(self):
        first = customer_view(self.breakdown)["line_items"][0]
        # 480 net grossed up at 20% = 600 — the margin is absorbed, not itemised.
        self.assertEqual(first["amount"], "600.00")
        self.assertEqual(set(first) & {"platform_fee", "worker_earning"}, set())

    def test_admin_sees_the_complete_calculation(self):
        adm = admin_view(self.breakdown)
        self.assertEqual(adm["split"]["worker_gross"], "480.00")
        self.assertEqual(adm["split"]["worker_share_pct"], "80")
        self.assertEqual(adm["split"]["platform_fee_gross"], "247.12")
        row = adm["rate_card"][0]
        # The five modular columns from the charges table.
        for col in ("package_rate", "gst_on_service", "platform_fee",
                    "gst_on_platform_fee", "customer_line_total"):
            self.assertIn(col, row)

    def test_admin_rate_card_columns_sum_to_the_customer_price(self):
        for row in admin_view(self.breakdown)["rate_card"]:
            self.assertEqual(
                money(D(row["package_rate"]) + D(row["gst_on_service"])),
                D(row["customer_line_total"]),
            )


class TestDeductions(unittest.TestCase):
    def test_deductions_reduce_only_the_payout_not_the_customer_price(self):
        b = compute_breakdown(
            [PricingComponent(code="n", label="N", input_amount=D("480.00"),
                              basis="earning_rate", gst_rate_pct=D("0"))],
            default_commission_pct=D("20"),
            deductions=[Deduction(code="estamp", label="E-Stamp advance",
                                  amount=D("50.00"))],
        )
        self.assertEqual(b.customer_total, D("600.00"))   # unchanged
        self.assertEqual(b.worker_gross, D("480.00"))
        self.assertEqual(b.worker_net_payable, D("430.00"))

    def test_multiple_deductions_accumulate(self):
        b = compute_breakdown(
            [PricingComponent(code="n", label="N", input_amount=D("1000.00"))],
            default_commission_pct=D("20"),
            deductions=[
                Deduction(code="tds", label="TDS", amount=D("8.00")),
                Deduction(code="estamp", label="E-Stamp", amount=D("50.00")),
            ],
        )
        self.assertEqual(b.total_deductions, D("58.00"))
        self.assertEqual(b.worker_net_payable, D("742.00"))


# ===========================================================================
# The supplied invoice templates, reproduced end to end.
# ===========================================================================
class TestTemplateClinicalNursing(unittest.TestCase):
    """Template 1 — Clinical Nursing + Materials (patient receipt).

    Nurse nets 480; the platform retains 118.64 + 21.36 GST = 140 gross, so
    the patient sees a single exempt line of 620. Plus an 18% consumables kit
    at 150 and an 18% care-protection fee at 35.40.
    """

    def setUp(self):
        # commission tuned so the 480 net grosses up to the template's 620
        self.breakdown = compute_breakdown(
            [
                PricingComponent(
                    code="nursing", label="Injection Administration (Home Clinical Care)",
                    input_amount=D("620.00"), basis="customer_rate",
                    gst_rate_pct=D("0"), sac_code="999314",
                    commission_pct=D("22.580645"),
                ),
                PricingComponent(
                    code="kit", label="Medical Consumables, Handling & Logistics Kit",
                    input_amount=D("150.00"), gst_rate_pct=D("18"),
                    sac_code="998599", earns_to="platform",
                ),
                PricingComponent(
                    code="protection", label="Platform Care & Incident Protection Fee",
                    input_amount=D("35.40"), gst_rate_pct=D("18"),
                    sac_code="998599", earns_to="platform",
                ),
            ],
            default_commission_pct=D("20"),
        )

    def test_patient_facing_totals(self):
        v = customer_view(self.breakdown)
        self.assertEqual(v["line_items"][0]["amount"], "620.00")
        self.assertEqual(v["line_items"][0]["gst_amount"], "0.00")
        self.assertEqual(v["line_items"][1]["amount"], "127.12")
        self.assertEqual(v["line_items"][1]["gst_amount"], "22.88")
        self.assertEqual(v["line_items"][1]["cgst_amount"], "11.44")
        self.assertEqual(v["line_items"][1]["sgst_amount"], "11.44")
        self.assertEqual(v["line_items"][1]["line_total"], "150.00")
        self.assertEqual(v["line_items"][2]["line_total"], "35.40")
        self.assertEqual(v["total_amount"], "805.40")

    def test_nurse_side_matches_the_payout_template(self):
        line = self.breakdown.lines[0]
        self.assertEqual(line.worker_earning, D("480.00"))
        self.assertEqual(line.platform_fee_gross, D("140.00"))
        self.assertEqual(line.platform_fee_taxable, D("118.64"))
        self.assertEqual(line.platform_fee_gst, D("21.36"))
        cgst, sgst = split_cgst_sgst(line.platform_fee_gst)
        self.assertEqual((cgst, sgst), (D("10.68"), D("10.68")))


class TestTemplateTeleConsult(unittest.TestCase):
    """Template 2 — exempt doctor fee + taxable platform-access line."""

    def test_totals(self):
        b = compute_breakdown(
            [
                PricingComponent(code="consult", label="Medical Tele-Consultation",
                                 input_amount=D("350.00"), gst_rate_pct=D("0"),
                                 sac_code="999312"),
                PricingComponent(code="access", label="Digital Health Platform Access",
                                 input_amount=D("150.00"), gst_rate_pct=D("18"),
                                 sac_code="998413", earns_to="platform"),
            ],
            default_commission_pct=D("20"),
        )
        v = customer_view(b)
        self.assertEqual(v["line_items"][0]["gst_amount"], "0.00")
        self.assertEqual(v["line_items"][1]["amount"], "127.12")
        self.assertEqual(v["line_items"][1]["gst_amount"], "22.88")
        self.assertEqual(v["total_amount"], "500.00")

    def test_flat_fee_variant(self):
        """Doctor takes 50 flat exempt; platform 50 + 9 GST -> patient pays 109."""
        b = compute_breakdown(
            [
                PricingComponent(code="consult", label="Tele-Consultation",
                                 input_amount=D("50.00"), gst_rate_pct=D("0")),
                PricingComponent(code="access", label="Platform Access",
                                 input_amount=D("59.00"), gst_rate_pct=D("18"),
                                 earns_to="platform"),
            ],
            default_commission_pct=D("0"),
        )
        self.assertEqual(b.customer_total, D("109.00"))
        self.assertEqual(b.lines[1].service_value, D("50.00"))
        self.assertEqual(b.lines[1].gst_amount, D("9.00"))


class TestTemplateNonMedic(unittest.TestCase):
    """Template 3 — non-licensed caregiver: the WHOLE package is 18% taxable."""

    def test_fully_taxable_package(self):
        b = compute_breakdown(
            [PricingComponent(code="escort",
                              label="Elder Escort & Doctor Visit Companion (3-Hour Shift)",
                              input_amount=D("900.00"), gst_rate_pct=D("18"),
                              sac_code="999334")],
            default_commission_pct=D("20"),
        )
        v = customer_view(b)
        self.assertEqual(v["line_items"][0]["amount"], "762.71")
        self.assertEqual(v["cgst_amount"], "68.64")
        self.assertEqual(v["sgst_amount"], "68.65")
        self.assertEqual(v["total_gst"], "137.29")
        self.assertEqual(v["total_amount"], "900.00")
        self.assertEqual(v["exempt_value"], "0.00")

    def test_80_20_applies_to_the_taxable_value_not_the_gst(self):
        """GST is the government's money — the nurse's 80% is computed on the
        pre-tax service value, never on the tax collected."""
        b = compute_breakdown(
            [PricingComponent(code="escort", label="Escort", input_amount=D("900.00"),
                              gst_rate_pct=D("18"))],
            default_commission_pct=D("20"),
        )
        self.assertEqual(b.lines[0].service_value, D("762.71"))
        self.assertEqual(b.worker_gross, D("610.17"))       # 80% of 762.71
        self.assertEqual(b.lines[0].platform_fee_gross, D("152.54"))


class TestTemplateNursePayout(unittest.TestCase):
    """Template 4 — payout advice: gross, platform fee, GST on it, take-home,
    e-stamp instalment, final disbursal."""

    def test_full_statement_arithmetic(self):
        b = compute_breakdown(
            [PricingComponent(code="nursing", label="Clinical Service Fee Earned",
                              input_amount=D("620.00"), gst_rate_pct=D("0"),
                              sac_code="999314", commission_pct=D("22.580645"))],
            default_commission_pct=D("20"),
            deductions=[Deduction(code="estamp",
                                  label="Statutory E-Stamp Paper Advance Recovery",
                                  amount=D("50.00"),
                                  note="Inst. 1 of 4 | Bal: 150.00")],
        )
        self.assertEqual(b.lines[0].service_value, D("620.00"))
        self.assertEqual(b.platform_fee_taxable, D("118.64"))
        self.assertEqual(b.platform_fee_gst, D("21.36"))
        self.assertEqual(b.worker_gross, D("480.00"))       # base net take-home
        self.assertEqual(b.total_deductions, D("50.00"))
        self.assertEqual(b.worker_net_payable, D("430.00"))  # final disbursal


class TestInvariants(unittest.TestCase):
    def test_customer_total_always_equals_sum_of_line_totals(self):
        for amounts in ([D("1.00")], [D("999.99"), D("0.01")],
                        [D("333.33"), D("333.33"), D("333.34")]):
            b = compute_breakdown(
                [PricingComponent(code=f"c{i}", label="L", input_amount=a,
                                  gst_rate_pct=D("18"))
                 for i, a in enumerate(amounts)],
                default_commission_pct=D("20"),
            )
            self.assertEqual(b.customer_total, money(sum(amounts)))

    def test_worker_plus_platform_always_equals_service_value(self):
        """No rupee is created or lost by the split, at any rate."""
        for amount in ["1.00", "0.01", "777.77", "12345.67"]:
            for pct in ["0", "20", "33.33", "100"]:
                b = compute_breakdown(
                    [PricingComponent(code="n", label="N", input_amount=D(amount),
                                      gst_rate_pct=D("0"), commission_pct=D(pct))],
                    default_commission_pct=D("20"),
                )
                self.assertEqual(
                    money(b.worker_gross + b.platform_fee_gross),
                    b.subtotal,
                    f"split drift at amount={amount} pct={pct}",
                )

    def test_zero_amount_is_safe(self):
        b = compute_breakdown(
            [PricingComponent(code="n", label="N", input_amount=D("0.00"),
                              gst_rate_pct=D("18"))],
            default_commission_pct=D("20"),
        )
        self.assertEqual(b.customer_total, D("0.00"))
        self.assertEqual(b.worker_net_payable, D("0.00"))

    def test_empty_rate_card_is_safe(self):
        b = compute_breakdown([], default_commission_pct=D("20"))
        self.assertEqual(b.customer_total, D("0.00"))
        self.assertEqual(b.worker_net_payable, D("0.00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
