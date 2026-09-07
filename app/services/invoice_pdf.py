"""PDF renderers for the customer tax invoice and the nurse payout advice.

Two reusable templates, both laid out to match the supplied examples: a
fixed-pitch, rule-separated statement in the style of a printed receipt.
Courier is used throughout because the layout relies on a right-hand amount
column lining up — a proportional face makes those columns drift.

Every value is passed in. Neither renderer reads the database, computes a
tax, or knows a rupee figure of its own: amounts arrive already computed by
`pricing_engine`, and the company header comes from `app.core.company`. That
separation is deliberate — a template must never become a second place where
money is calculated.

reportlab is already a dependency (used by eprescription_service), so this
adds no new package.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.company import (
    MARKETPLACE_NOTE,
    PURE_AGENT_NOTE,
    CompanyIdentity,
)

# Layout constants — one place to retune the whole document.
_MONO = "Courier"
_MONO_BOLD = "Courier-Bold"
_BODY = 8.5
_SMALL = 7.5
_TITLE = 11
_LINE_H = 4.6 * mm
_MARGIN = 15 * mm
_RULE = colors.HexColor("#333333")


def _rupees(amount: Decimal | float | int | str) -> str:
    """Indian-format an amount with the rupee sign.

    reportlab's Courier has no glyph for U+20B9, so '₹' renders as a black
    box. 'Rs.' is used instead — the same compromise the e-prescription PDF
    makes for its currency lines.
    """
    d = Decimal(str(amount)).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    d = abs(d)
    whole, frac = f"{d:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}Rs.{whole}.{frac}"


class _Doc:
    """Thin cursor over a reportlab canvas: write a line, move down.

    Exists so the two templates below read as a sequence of rows rather than
    a pile of coordinate arithmetic, and so page breaks are handled once.
    """

    def __init__(self, buf: io.BytesIO):
        self.buf = buf
        self.c = pdf_canvas.Canvas(buf, pagesize=A4)
        self.width, self.height = A4
        self.y = self.height - _MARGIN
        self.right = self.width - _MARGIN

    def _room(self, needed: float = _LINE_H) -> None:
        if self.y - needed < _MARGIN + 12 * mm:
            self.c.showPage()
            self.y = self.height - _MARGIN

    def text(self, s: str, *, bold: bool = False, size: float = _BODY,
             indent: float = 0.0) -> None:
        self._room()
        self.c.setFont(_MONO_BOLD if bold else _MONO, size)
        self.c.setFillColor(colors.black)
        self.c.drawString(_MARGIN + indent, self.y, s)
        self.y -= _LINE_H

    def row(self, left: str, right: str, *, bold: bool = False,
            size: float = _BODY, indent: float = 0.0) -> None:
        """Left label with a right-aligned amount — the invoice workhorse."""
        self._room()
        font = _MONO_BOLD if bold else _MONO
        self.c.setFont(font, size)
        self.c.setFillColor(colors.black)
        self.c.drawString(_MARGIN + indent, self.y, left)
        self.c.drawRightString(self.right, self.y, right)
        self.y -= _LINE_H

    def centered(self, s: str, *, bold: bool = False, size: float = _BODY) -> None:
        self._room()
        self.c.setFont(_MONO_BOLD if bold else _MONO, size)
        self.c.setFillColor(colors.black)
        self.c.drawCentredString(self.width / 2, self.y, s)
        self.y -= _LINE_H

    def rule(self, *, heavy: bool = False) -> None:
        self._room(_LINE_H)
        self.y += _LINE_H * 0.35
        self.c.setStrokeColor(_RULE)
        self.c.setLineWidth(1.1 if heavy else 0.4)
        if not heavy:
            self.c.setDash(1, 2)
        self.c.line(_MARGIN, self.y, self.right, self.y)
        self.c.setDash()
        self.y -= _LINE_H * 0.9

    def gap(self, factor: float = 0.6) -> None:
        self.y -= _LINE_H * factor

    def wrapped(self, s: str, *, width: int = 92, size: float = _SMALL,
                indent: float = 0.0) -> None:
        for line in _wrap(s, width):
            self.text(line, size=size, indent=indent)

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _header(doc: _Doc, company: CompanyIdentity, title: str,
            subtitle: Optional[str] = None) -> None:
    doc.rule(heavy=True)
    doc.centered(company.legal_name, bold=True, size=_TITLE)
    doc.centered(company.header_line, size=_SMALL)
    doc.centered(title, bold=True, size=_BODY)
    if subtitle:
        doc.centered(subtitle, size=_SMALL)
    doc.rule(heavy=True)


def _meta_pairs(doc: _Doc, pairs: list[tuple[str, str]]) -> None:
    """Two metadata columns, as in the examples (label left, label mid-page)."""
    mid = doc.width / 2 - 10 * mm
    for i in range(0, len(pairs), 2):
        doc._room()
        doc.c.setFont(_MONO, _BODY)
        doc.c.setFillColor(colors.black)
        k, v = pairs[i]
        doc.c.drawString(_MARGIN, doc.y, f"{k}: {v}")
        if i + 1 < len(pairs):
            k2, v2 = pairs[i + 1]
            doc.c.drawString(mid, doc.y, f"{k2}: {v2}")
        doc.y -= _LINE_H


# ===========================================================================
# 1. Customer receipt / tax invoice
# ===========================================================================
def render_customer_invoice_pdf(
    *,
    company: CompanyIdentity,
    invoice_number: str,
    invoice_date: date,
    booking_ref: str,
    customer_name: str,
    line_items: list[dict],
    taxable_value: Decimal,
    exempt_value: Decimal,
    cgst_amount: Decimal,
    sgst_amount: Decimal,
    total_amount: Decimal,
    subsidy_amount: Decimal = Decimal("0"),
    place_of_supply: Optional[str] = None,
    provider_line: Optional[str] = None,
    document_title: str = "BOOKING RECEIPT & TAX INVOICE",
) -> bytes:
    """The patient-facing document.

    `line_items` must come from `pricing_engine.customer_view`, which by
    construction contains no commission or payout field — so there is no way
    for the internal split to reach this page even by accident.
    """
    buf = io.BytesIO()
    doc = _Doc(buf)

    _header(doc, company, document_title)

    _meta_pairs(doc, [
        ("Invoice No", invoice_number),
        ("Date", invoice_date.strftime("%d-%b-%Y")),
        ("Booking ID", booking_ref),
        ("Place of Supply", place_of_supply or company.place_of_supply),
        ("Customer Name", customer_name),
        ("Reverse Charge", "No"),
    ])
    doc.rule()

    doc.text("LINE ITEMS:", bold=True)
    doc.rule()

    for idx, item in enumerate(line_items, start=1):
        is_exempt = bool(item.get("is_exempt"))
        amount = Decimal(str(item.get("amount", "0")))
        gst = Decimal(str(item.get("gst_amount", "0")))
        line_total = Decimal(str(item.get("line_total", "0")))

        doc.row(f"{idx}. {item.get('label', '')}", _rupees(amount))

        detail = []
        if item.get("sac_code"):
            detail.append(f"SAC: {item['sac_code']}")
        detail.append("Exempt Supply" if is_exempt else "Taxable Value")
        doc.text(" | ".join(detail), size=_SMALL, indent=5 * mm)

        if idx == 1 and provider_line:
            doc.text(provider_line, size=_SMALL, indent=5 * mm)

        if is_exempt:
            note = item.get("exemption_note") or ""
            doc.text(f"GST @ 0% [{note}]", size=_SMALL, indent=5 * mm)
        else:
            rate = Decimal(str(item.get("gst_rate_pct", "0")))
            half = (rate / 2).quantize(Decimal("0.01")).normalize()
            doc.row(
                f"CGST @ {half}%: {_rupees(item.get('cgst_amount', 0))} | "
                f"SGST @ {half}%: {_rupees(item.get('sgst_amount', 0))} "
                f"(Total GST @ {rate.normalize()}%: {_rupees(gst)})",
                _rupees(line_total),
                size=_SMALL,
                indent=5 * mm,
            )
        doc.gap()

    doc.rule()
    if exempt_value > 0:
        doc.row("Exempt Supply Value:", _rupees(exempt_value), size=_SMALL)
    if taxable_value > 0:
        doc.row("Taxable Value:", _rupees(taxable_value), size=_SMALL)
        doc.row("CGST:", _rupees(cgst_amount), size=_SMALL)
        doc.row("SGST:", _rupees(sgst_amount), size=_SMALL)
    if subsidy_amount > 0:
        doc.row("Less: Subsidy Applied:", f"-{_rupees(subsidy_amount)}", size=_SMALL)
    doc.rule()
    doc.row("TOTAL AMOUNT PAID (All-Inclusive):", _rupees(total_amount), bold=True)
    doc.rule(heavy=True)

    if exempt_value > 0:
        doc.wrapped(f"*Note: {MARKETPLACE_NOTE}")
    doc.wrapped(
        "This is a computer-generated invoice and does not require a signature. "
        f"For queries contact {company.support_email}."
    )
    doc.rule(heavy=True)
    return doc.finish()


# ===========================================================================
# 2. Nurse payout advice & platform tax invoice
# ===========================================================================
def render_payout_statement_pdf(
    *,
    company: CompanyIdentity,
    statement_number: str,
    statement_date: date,
    booking_ref: str,
    partner_name: str,
    partner_id: str,
    partner_council_no: Optional[str],
    gross_earned: Decimal,
    gross_label: str,
    gross_sac: Optional[str],
    platform_fee: Decimal,
    platform_fee_gst: Decimal,
    platform_fee_sac: Optional[str],
    net_take_home: Decimal,
    deductions: list[dict],
    final_disbursal: Decimal,
    transfer_mode: Optional[str] = None,
    utr: Optional[str] = None,
    payout_reference: Optional[str] = None,
    payout_status: Optional[str] = None,
) -> bytes:
    """The partner-facing document: what was earned, what the platform billed
    back, and what actually reaches the bank account.

    `utr` / `payout_status` are only populated once Razorpay has confirmed the
    transfer, so an unconfirmed payout's statement shows the disbursal as
    pending rather than asserting money moved.
    """
    buf = io.BytesIO()
    doc = _Doc(buf)

    _header(doc, company, "PAYOUT ADVICE & TAX INVOICE",
            "(Issued to Care Partner / Nurse)")

    _meta_pairs(doc, [
        ("Platform", company.legal_name),
        ("Invoice No", statement_number),
        ("GSTIN", company.gstin),
        ("Date", statement_date.strftime("%d-%b-%Y")),
        ("Partner Name", partner_name),
        ("Partner ID", partner_id),
        ("Council No", partner_council_no or "-"),
        ("Booking Ref", booking_ref),
    ])
    doc.rule()

    doc.text("LINE ITEMS:", bold=True)
    doc.rule()

    doc.row(f"1. {gross_label} (Booking Ref #{booking_ref})", _rupees(gross_earned))
    if gross_sac:
        doc.text(f"Exempt Paramedic Healthcare (SAC {gross_sac})",
                 size=_SMALL, indent=5 * mm)
    doc.gap()

    idx = 2
    if platform_fee > 0:
        sac = f" (SAC {platform_fee_sac})" if platform_fee_sac else ""
        doc.row(f"{idx}. Less: Platform Technology Fee{sac}", f"-{_rupees(platform_fee)}")
        doc.text(f"(Billed to Partner by {company.legal_name})", size=_SMALL, indent=5 * mm)
        doc.gap()
        idx += 1

    if platform_fee_gst > 0:
        half = (platform_fee_gst / 2).quantize(Decimal("0.01"))
        doc.row(f"{idx}. Less: 18% GST on Platform Technology Fee",
                f"-{_rupees(platform_fee_gst)}")
        doc.text(f"(CGST 9%: {_rupees(half)} + SGST 9%: {_rupees(platform_fee_gst - half)})",
                 size=_SMALL, indent=5 * mm)
        doc.gap()
        idx += 1

    doc.rule()
    doc.row("BASE NET TAKE-HOME EARNED:", _rupees(net_take_home), bold=True)
    doc.rule()

    for deduction in deductions:
        amount = Decimal(str(deduction.get("amount", "0")))
        doc.row(f"{idx}. Less: {deduction.get('label', '')}", f"-{_rupees(amount)}")
        if deduction.get("note"):
            doc.text(f"[{PURE_AGENT_NOTE} | {deduction['note']}]",
                     size=_SMALL, indent=5 * mm)
        doc.gap()
        idx += 1

    if deductions:
        doc.rule()
    doc.row("FINAL DISBURSAL TO BANK ACCOUNT:", _rupees(final_disbursal), bold=True)
    doc.rule(heavy=True)

    if utr:
        doc.row(f"Transfer Mode: {transfer_mode or 'Razorpay Payouts'}", f"UTR: {utr}",
                size=_SMALL)
    elif payout_status:
        doc.text(f"Transfer Status: {payout_status.upper()} "
                 "— UTR will appear once the bank confirms settlement.",
                 size=_SMALL)
    if payout_reference:
        doc.text(f"Payout Reference: {payout_reference}", size=_SMALL)
    doc.rule(heavy=True)
    return doc.finish()
