"""PDF renderer for a completed visit's care summary.

One template, two views, exactly like the split already drawn in
`invoice_pdf.py` between the customer invoice and the nurse payout advice:

  * the nurse's own copy carries her clinical notes (her working record);
  * the family's copy carries the family summary only.

`include_clinical_notes` is what separates the two — never a role check
inside this module, since this module renders whatever it is given. The
caller (the API layer) is the only place that decides which fields a given
requester is allowed to see, exactly as `render_customer_invoice_pdf` never
sees the internal commission split because the caller never passes it in.

reportlab is already a dependency (used by invoice_pdf.py / eprescription_
service), so this adds no new package. Layout constants and the `_Doc`
cursor are reused from invoice_pdf.py rather than duplicated, so the two
document families stay visually consistent and any future retune of the
shared look only has to happen once.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

from app.core.company import CompanyIdentity
from app.services.invoice_pdf import _Doc, _header, _meta_pairs


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%d-%b-%Y, %I:%M %p") if dt else "\u2014"


def _fmt_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "\u2014"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m} min"


def render_visit_report_pdf(
    *,
    company: CompanyIdentity,
    booking_ref: str,
    patient_name: str,
    nurse_name: str,
    nurse_council_no: Optional[str],
    check_in_at: Optional[datetime],
    check_out_at: Optional[datetime],
    duration_minutes: Optional[int],
    vitals: Optional[dict],
    summary_text: Optional[str],
    summary_label: str,
    include_clinical_notes: bool,
    care_notes: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> bytes:
    """Render a visit's care summary.

    `vitals` is the plain dict shape already used elsewhere for a single
    reading (bp_systolic, bp_diastolic, pulse, spo2, temperature_f), or
    None if nothing was recorded. `summary_text` / `summary_label` let the
    caller supply either the family summary ("Summary for the family") or,
    for the nurse's own copy, the same field under its own heading -- the
    nurse's clinical notes are passed separately via `care_notes` and are
    only rendered when `include_clinical_notes` is True, so a family PDF
    can never carry them even if a caller passed them in by mistake for
    some other reason.
    """
    buf = io.BytesIO()
    doc = _Doc(buf)

    _header(
        doc,
        company,
        "VISIT CARE SUMMARY",
        "(Nurse's working copy)" if include_clinical_notes else "(Family copy)",
    )

    _meta_pairs(
        doc,
        [
            ("Booking Ref", booking_ref),
            ("Generated", (generated_at or datetime.now()).strftime("%d-%b-%Y, %I:%M %p")),
            ("Patient", patient_name),
            ("Care Provider", nurse_name + (f" ({nurse_council_no})" if nurse_council_no else "")),
        ],
    )
    doc.rule()

    doc.text("VISIT TIMING:", bold=True)
    doc.rule()
    doc.row("Checked in:", _fmt_dt(check_in_at))
    doc.row("Checked out:", _fmt_dt(check_out_at))
    doc.row("Duration:", _fmt_duration(duration_minutes))
    doc.rule()

    has_vitals = bool(vitals) and any(
        vitals.get(k) is not None
        for k in ("bp_systolic", "bp_diastolic", "pulse", "spo2", "temperature_f")
    )
    if has_vitals:
        doc.text("VITALS RECORDED:", bold=True)
        doc.rule()
        bp_s, bp_d = vitals.get("bp_systolic"), vitals.get("bp_diastolic")
        if bp_s is not None and bp_d is not None:
            doc.row("Blood pressure:", f"{bp_s} / {bp_d} mmHg")
        if vitals.get("spo2") is not None:
            doc.row("SpO2:", f"{vitals['spo2']}%")
        if vitals.get("pulse") is not None:
            doc.row("Heart rate:", f"{vitals['pulse']} bpm")
        if vitals.get("temperature_f") is not None:
            doc.row("Temperature:", f"{vitals['temperature_f']} \u00b0F")
        doc.rule()

    doc.text(f"{summary_label.upper()}:", bold=True)
    doc.rule()
    doc.wrapped(summary_text.strip() if summary_text and summary_text.strip() else "Not recorded.")
    doc.rule()

    if include_clinical_notes:
        doc.gap()
        doc.text("CLINICAL NOTES (INTERNAL):", bold=True)
        doc.rule()
        doc.wrapped(care_notes.strip() if care_notes and care_notes.strip() else "No clinical notes recorded.")
        doc.rule()

    doc.gap()
    doc.wrapped(
        "This is a computer-generated visit summary and does not require a signature. "
        f"For queries contact {company.support_email}."
    )
    doc.rule(heavy=True)
    return doc.finish()
