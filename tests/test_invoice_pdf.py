"""Invoice and payout-statement PDF rendering.

    python -m unittest tests.test_invoice_pdf -v

reportlab is a real dependency of this repo and is present, so these tests
render actual PDF bytes and read the text back out of them. That makes them a
genuine check that the documents generate and carry the right figures — not a
mock of a renderer.

Text extraction uses reportlab's own text operators rather than a PDF parser:
the content stream stores drawn strings in `(...) Tj` form, which is enough to
assert that an amount made it onto the page.
"""
from __future__ import annotations

import base64
import re
import unittest
import zlib
from datetime import date
from decimal import Decimal

from tests import _offline_stubs

_offline_stubs.install()

from app.core.company import CompanyIdentity  # noqa: E402
from app.services.invoice_pdf import (  # noqa: E402
    render_customer_invoice_pdf,
    render_payout_statement_pdf,
)
from app.services.pricing_engine import (  # noqa: E402
    PricingComponent,
    compute_breakdown,
    customer_view,
)

D = Decimal

COMPANY = CompanyIdentity(
    legal_name="YANTRAM MEDTECH PVT LTD",
    address_line="HITEC City, Hyderabad, Telangana - 500081",
    gstin="36AABCY1234F1Z5",
    state_name="Telangana",
    state_code="36",
    support_email="support@nurseconnect.app",
)


def _decode_stream(raw: bytes) -> bytes:
    """Undo reportlab's default ASCII85 + Flate stream encoding.

    Both layers are optional depending on rl_config, so each is attempted and
    skipped if it doesn't apply.
    """
    data = raw.strip()
    if data.endswith(b"~>"):
        try:
            data = base64.a85decode(data, adobe=True)
        except ValueError:
            pass
    try:
        data = zlib.decompress(data)
    except zlib.error:
        pass
    return data


def extract_text(pdf: bytes) -> str:
    """Pull drawn strings out of a reportlab PDF's content streams.

    Handles both `(text) Tj` and the `[(a) -10 (b)] TJ` array form, since
    reportlab emits either depending on the drawing call used.
    """
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        data = _decode_stream(match.group(1))
        for text in re.findall(rb"\((?:\\.|[^\\()])*\)", data, re.S):
            body = text[1:-1]
            body = re.sub(rb"\\([()\\])", rb"\1", body)
            chunks.append(body.decode("latin-1"))
    return "\n".join(chunks)


class TestCustomerInvoicePdf(unittest.TestCase):
    def setUp(self):
        # Mixed GST: an exempt nursing line beside an 18% consumables line.
        self.breakdown = compute_breakdown(
            [
                PricingComponent(code="nursing",
                                 label="Injection Administration (Home Clinical Care)",
                                 input_amount=D("480.00"), basis="earning_rate",
                                 gst_rate_pct=D("0"), sac_code="999314"),
                PricingComponent(code="kit",
                                 label="Medical Consumables, Handling & Logistics Kit",
                                 input_amount=D("150.00"), gst_rate_pct=D("18"),
                                 sac_code="998599", earns_to="platform"),
            ],
            default_commission_pct=D("20"),
        )
        self.view = customer_view(self.breakdown)
        self.pdf = render_customer_invoice_pdf(
            company=COMPANY,
            invoice_number="YM-INV-2026-00001",
            invoice_date=date(2026, 9, 2),
            booking_ref="BK-994821",
            customer_name="Rajesh Sharma",
            line_items=self.view["line_items"],
            taxable_value=D(self.view["taxable_value"]),
            exempt_value=D(self.view["exempt_value"]),
            cgst_amount=D(self.view["cgst_amount"]),
            sgst_amount=D(self.view["sgst_amount"]),
            total_amount=D(self.view["total_amount"]),
            provider_line="Care Provider: Sister Kavitha Rani",
        )
        self.text = extract_text(self.pdf)

    def test_produces_a_valid_pdf(self):
        self.assertTrue(self.pdf.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", self.pdf)
        self.assertGreater(len(self.pdf), 1000)

    def test_company_header_is_present(self):
        self.assertIn("YANTRAM MEDTECH PVT LTD", self.text)
        self.assertIn("36AABCY1234F1Z5", self.text)
        self.assertIn("BOOKING RECEIPT & TAX INVOICE", self.text)

    def test_invoice_metadata_is_present(self):
        self.assertIn("YM-INV-2026-00001", self.text)
        self.assertIn("BK-994821", self.text)
        self.assertIn("Rajesh Sharma", self.text)
        self.assertIn("02-Sep-2026", self.text)
        self.assertIn("Telangana (36)", self.text)

    def test_amounts_are_rendered(self):
        # 480 net grossed up at 20% -> 600.00 exempt line; kit 127.12 + 22.88.
        self.assertIn("600.00", self.text)
        self.assertIn("127.12", self.text)
        self.assertIn("22.88", self.text)
        self.assertIn("750.00", self.text)  # 600 + 150

    def test_exempt_line_cites_the_notification(self):
        self.assertIn("999314", self.text)
        self.assertIn("12/2017", self.text)
        self.assertIn("Entry 74", self.text)

    def test_taxable_line_shows_the_cgst_sgst_split(self):
        self.assertIn("CGST", self.text)
        self.assertIn("SGST", self.text)
        self.assertIn("11.44", self.text)

    def test_customer_pdf_never_shows_the_commission_split(self):
        """The whole point of the customer/admin boundary.

        150.00 (the nurse's platform fee at 20% of 750) and the word
        'commission' must not appear anywhere on the patient's document.
        """
        lowered = self.text.lower()
        for leak in ("commission", "platform fee", "payout", "take-home",
                     "nurse earning", "80%", "20%"):
            self.assertNotIn(leak, lowered, f"customer invoice leaks {leak!r}")

    def test_marketplace_note_appears_when_a_line_is_exempt(self):
        self.assertIn("marketplace", self.text.lower())

    def test_fully_taxable_invoice_omits_the_exempt_note(self):
        breakdown = compute_breakdown(
            [PricingComponent(code="escort", label="Elder Escort Companion",
                              input_amount=D("900.00"), gst_rate_pct=D("18"),
                              sac_code="999334")],
            default_commission_pct=D("20"),
        )
        view = customer_view(breakdown)
        pdf = render_customer_invoice_pdf(
            company=COMPANY, invoice_number="YM-INV-2026-00002",
            invoice_date=date(2026, 9, 2), booking_ref="BK-773120",
            customer_name="Vikram Malhotra", line_items=view["line_items"],
            taxable_value=D(view["taxable_value"]),
            exempt_value=D(view["exempt_value"]),
            cgst_amount=D(view["cgst_amount"]), sgst_amount=D(view["sgst_amount"]),
            total_amount=D(view["total_amount"]),
        )
        text = extract_text(pdf)
        self.assertIn("762.71", text)
        self.assertIn("900.00", text)
        self.assertNotIn("Entry 74", text)
        self.assertNotIn("marketplace", text.lower())


class TestPayoutStatementPdf(unittest.TestCase):
    def _render(self, **overrides):
        kwargs = dict(
            company=COMPANY,
            statement_number="YM-COMM-2026-00001",
            statement_date=date(2026, 9, 2),
            booking_ref="BK-994821",
            partner_name="Sister Kavitha Rani",
            partner_id="NUR-A1B2C3D4",
            partner_council_no="TSNC-98432",
            gross_earned=D("620.00"),
            gross_label="Clinical Service Fee Earned",
            gross_sac="999314",
            platform_fee=D("118.64"),
            platform_fee_gst=D("21.36"),
            platform_fee_sac="998314",
            net_take_home=D("480.00"),
            deductions=[{
                "label": "Statutory E-Stamp Paper Advance Recovery",
                "amount": D("50.00"),
                "note": "Inst. 1 of 4 | Bal: 150.00",
            }],
            final_disbursal=D("430.00"),
            transfer_mode="Razorpay Payouts / IMPS",
            utr="20260902192837194",
            payout_reference="pout_TEST123",
            payout_status="processed",
        )
        kwargs.update(overrides)
        return extract_text(render_payout_statement_pdf(**kwargs))

    def test_full_statement_arithmetic_appears(self):
        text = self._render()
        for amount in ("620.00", "118.64", "21.36", "480.00", "50.00", "430.00"):
            self.assertIn(amount, text, f"missing {amount}")

    def test_partner_identity_and_sac_codes(self):
        text = self._render()
        self.assertIn("Sister Kavitha Rani", text)
        self.assertIn("TSNC-98432", text)
        self.assertIn("999314", text)
        self.assertIn("998314", text)

    def test_confirmed_payout_shows_the_utr(self):
        text = self._render()
        self.assertIn("20260902192837194", text)
        self.assertIn("Razorpay Payouts", text)

    def test_unconfirmed_payout_does_not_claim_money_moved(self):
        """A queued payout must not print a UTR or imply settlement."""
        text = self._render(utr=None, payout_status="queued")
        self.assertNotIn("UTR:", text)
        self.assertIn("QUEUED", text.upper())
        self.assertIn("430.00", text)  # amount still shown

    def test_e_stamp_recovery_cites_pure_agent_rule(self):
        text = self._render()
        self.assertIn("Rule 33", text)
        self.assertIn("Inst. 1 of 4", text)

    def test_statement_without_deductions_renders(self):
        text = self._render(deductions=[], final_disbursal=D("480.00"))
        self.assertIn("480.00", text)
        self.assertNotIn("E-Stamp", text)

    def test_zero_platform_fee_omits_the_fee_lines(self):
        """A flat-fee offering where the platform takes nothing from the nurse
        must not print an empty 'Less: Platform Technology Fee' row."""
        text = self._render(platform_fee=D("0.00"), platform_fee_gst=D("0.00"),
                            net_take_home=D("620.00"), deductions=[],
                            final_disbursal=D("620.00"))
        self.assertNotIn("Platform Technology Fee", text)
        self.assertIn("620.00", text)


class TestAmountFormatting(unittest.TestCase):
    def test_indian_digit_grouping(self):
        from app.services.invoice_pdf import _rupees

        self.assertEqual(_rupees(D("620.00")), "Rs.620.00")
        self.assertEqual(_rupees(D("1234.56")), "Rs.1,234.56")
        self.assertEqual(_rupees(D("123456.78")), "Rs.1,23,456.78")
        self.assertEqual(_rupees(D("0.00")), "Rs.0.00")
        self.assertEqual(_rupees(D("-50.00")), "-Rs.50.00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
