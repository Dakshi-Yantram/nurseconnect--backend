"""PDF renderer for the patient Payment Receipt.

Distinct from `invoice_pdf.render_customer_invoice_pdf` (the line-item GST
tax invoice): this is the short, print-ready confirmation a patient/family
wants after paying — "what did I pay, for what package, when, how" — without
the line-item tax mechanics. Both documents are generated from the same
`Invoice`/`Booking` rows, so the figures always agree; this module only
lays them out differently.

Same rule as `invoice_pdf.py`: every value is passed in. This module never
reads the database, computes a tax, or has an opinion about what a patient
should be shown. In particular it has NO parameter for platform fee,
commission, nurse payout or any other internal split — there is no field to
plumb one into even by accident.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

from app.core.company import CompanyIdentity, MARKETPLACE_NOTE
from app.services.invoice_pdf import _BODY, _Doc, _MARGIN, _MONO, _MONO_BOLD, _SMALL, _TITLE, _rupees

_BRAND = colors.HexColor("#0f4c81")
_BRAND_LIGHT = colors.HexColor("#eaf1f8")
_BORDER = colors.HexColor("#0f4c81")


def _band_header(doc: _Doc, company: CompanyIdentity, title: str) -> None:
    """A solid brand-colour band with the company + document title in it —
    this document is a patient-facing receipt, so it gets a little more
    visual polish than the plain-rule ledger style used for the tax invoice.
    """
    band_h = 22 * mm
    top = doc.height - _MARGIN + 6 * mm
    doc.c.setFillColor(_BRAND)
    doc.c.rect(0, top - band_h, doc.width, band_h, stroke=0, fill=1)
    doc.c.setFillColor(colors.white)
    doc.c.setFont(_MONO_BOLD, _TITLE + 2)
    doc.c.drawString(_MARGIN, top - 9 * mm, company.legal_name)
    doc.c.setFont(_MONO, _SMALL)
    doc.c.drawString(_MARGIN, top - 15 * mm, company.header_line)
    doc.c.setFont(_MONO_BOLD, _BODY)
    doc.c.drawRightString(doc.right, top - 9 * mm, title)
    doc.c.setFont(_MONO, _SMALL)
    doc.c.drawRightString(doc.right, top - 15 * mm, "Patient Home Healthcare Services")
    doc.y = top - band_h - 6 * mm
    doc.c.setFillColor(colors.black)


def _box(doc: _Doc, lines: list[tuple[str, str]], *, title: str, min_h: float = 0) -> None:
    """A ruled box highlighting a group of fields — used for the Care
    Package block so it stays clearly visible, as required."""
    pad = 3 * mm
    row_h = 4.6 * mm
    box_h = max(min_h, pad * 2 + row_h * (len(lines) + 1))
    doc._room(box_h + 2 * mm)
    top = doc.y + row_h * 0.3
    bottom = top - box_h
    doc.c.setStrokeColor(_BORDER)
    doc.c.setFillColor(_BRAND_LIGHT)
    doc.c.setLineWidth(0.8)
    doc.c.roundRect(_MARGIN, bottom, doc.right - _MARGIN, box_h, 2 * mm, stroke=1, fill=1)
    doc.c.setFillColor(_BRAND)
    doc.c.setFont(_MONO_BOLD, _BODY)
    ty = top - pad - row_h * 0.6
    doc.c.drawString(_MARGIN + pad, ty, title)
    doc.c.setFillColor(colors.black)
    for i, (k, v) in enumerate(lines, start=1):
        ly = ty - row_h * i
        doc.c.setFont(_MONO, _SMALL)
        doc.c.drawString(_MARGIN + pad, ly, k)
        doc.c.setFont(_MONO_BOLD, _SMALL)
        doc.c.drawRightString(doc.right - pad, ly, v)
    doc.y = bottom - 3 * mm


def render_payment_receipt_pdf(
    *,
    company: CompanyIdentity,
    receipt_number: str,
    receipt_date: date,
    booking_ref: str,
    patient_name: str,
    package_name: Optional[str],
    package_code: Optional[str],
    service_period: Optional[str],
    payment_id: Optional[str],
    payment_datetime: Optional[datetime],
    payment_method_label: str,
    payment_status_label: str,
    amount_paid: Decimal,
    taxable_value: Decimal = Decimal("0"),
    exempt_value: Decimal = Decimal("0"),
    cgst_amount: Decimal = Decimal("0"),
    sgst_amount: Decimal = Decimal("0"),
    subsidy_amount: Decimal = Decimal("0"),
    invoice_number: Optional[str] = None,
) -> bytes:
    """The patient-facing payment receipt.

    `amount_paid` is the only figure this document asserts as "what you
    paid" — it is always `invoice.total_amount` / the booking's
    `total_amount`, i.e. the same number the patient's bank statement shows.
    No commission, platform fee or payout figure is ever passed in here.
    """
    buf = io.BytesIO()
    doc = _Doc(buf)

    _band_header(doc, company, "PAYMENT RECEIPT")

    doc.text("")
    doc.row("Receipt No:", receipt_number, bold=True)
    doc.row("Receipt Date:", receipt_date.strftime("%d-%b-%Y"))
    doc.row("Booking ID:", booking_ref)
    doc.row("Patient Name:", patient_name)
    if invoice_number:
        doc.row("Tax Invoice No:", invoice_number, size=_SMALL)
    doc.gap()

    dash = "\u2014"
    _box(
        doc,
        [
            ("Package Code", package_code or dash),
            ("Service Period", service_period or dash),
        ],
        title=f"Care Package: {package_name or dash}",
    )

    doc.text("PAYMENT DETAILS:", bold=True)
    doc.rule()
    doc.row("Payment ID:", payment_id or "\u2014")
    doc.row(
        "Payment Date/Time:",
        payment_datetime.strftime("%d-%b-%Y, %I:%M %p") if payment_datetime else "\u2014",
    )
    doc.row("Payment Method:", payment_method_label)
    doc.row("Payment Status:", payment_status_label, bold=True)
    doc.rule()
    doc.gap()

    doc.text("AMOUNT:", bold=True)
    doc.rule()
    if exempt_value > 0:
        doc.row("Healthcare Service (GST Exempt):", _rupees(exempt_value), size=_SMALL)
    if taxable_value > 0:
        doc.row("Taxable Value:", _rupees(taxable_value), size=_SMALL)
        doc.row("CGST:", _rupees(cgst_amount), size=_SMALL)
        doc.row("SGST:", _rupees(sgst_amount), size=_SMALL)
    if taxable_value <= 0 and exempt_value <= 0:
        # No GST breakdown was supplied (e.g. cash booking priced outside the
        # invoice flow) — still make the tax status explicit rather than
        # silently omitting it.
        doc.row("GST:", "Not applicable / see tax invoice", size=_SMALL)
    if subsidy_amount > 0:
        gross = amount_paid + subsidy_amount
        doc.row("Amount Paid:", _rupees(gross), bold=True)
        doc.row("Less: Subsidy Applied:", f"-{_rupees(subsidy_amount)}", size=_SMALL)
    else:
        doc.row("Amount Paid:", _rupees(amount_paid), bold=True)
    doc.rule()
    doc.row("TOTAL PAID:", _rupees(amount_paid), bold=True, size=_TITLE - 2)
    doc.rule(heavy=True)

    if exempt_value > 0:
        doc.wrapped(f"*Note: {MARKETPLACE_NOTE}")
    doc.wrapped(
        "This receipt confirms payment received for the care package/service listed "
        "above. This is a computer-generated document and does not require a "
        f"signature. For queries contact {company.support_email}."
    )
    doc.rule(heavy=True)
    return doc.finish()
