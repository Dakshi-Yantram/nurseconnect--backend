"""PDF renderer for a completed visit's care summary.

One template, two views, exactly like the split already drawn in
`invoice_pdf.py` between the customer invoice and the nurse payout advice:

  * the nurse's own copy carries her clinical notes (her working record);
  * the family's copy carries the family summary only.

`include_clinical_notes` is what separates the two — never a role check
inside this module, since this module renders whatever it is given. The
caller (the API layer) is the only place that decides which fields a given
requester is allowed to see.

Every copy is watermarked for the person who downloaded it (see
`PdfWatermark`). The watermark is REAL traceability, not prevention: it is
baked into the page content stream of every page, so a leaked copy (or a
photo of a printed copy) identifies whose download it came from, and the
`ref` printed on it is the id of the audit-log row that recorded the
download (who / when / IP).

The PDF is also flagged no-copy / no-modify. That flag is a DETERRENT only:
compliant viewers (Acrobat, Chrome, Preview) honour it, but any PDF tool can
strip it. Printing stays allowed — a family legitimately prints the summary
for a doctor; the watermark goes onto the paper with it.

reportlab is already a dependency, so this adds no new package.
"""
from __future__ import annotations

import io
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.company import CompanyIdentity
<<<<<<< HEAD
from app.services.invoice_pdf import _SMALL, _Doc, _header, _meta_pairs
=======
from app.services.invoice_pdf import _Doc, _header, _meta_pairs
>>>>>>> origin/staging

# All user-facing times are shown in IST. Timestamps in the DB are UTC and the
# EC2 host clock is UTC; before this change the PDF printed raw UTC times with
# no zone, i.e. 5h30m off for every family.
DISPLAY_TZ = ZoneInfo("Asia/Kolkata")
DISPLAY_TZ_LABEL = "IST"

_WM_FONT = "Helvetica-Bold"
_WM_SIZE = 13
_WM_ALPHA = 0.10
_FOOTER_FONT = "Helvetica"
_FOOTER_SIZE = 6.5
_MAX_WM_FIELD = 60


@dataclass(frozen=True)
class PdfWatermark:
    """Who this copy was rendered for. All fields are plain display strings."""

    viewer_name: str
    viewer_role: str
    generated_at: datetime
    ref: str
    viewer_hint: Optional[str] = None  # e.g. masked phone "******3210"


def _to_display_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # Naive values in this codebase are UTC (server clock / DB default).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "\u2014"
    return _to_display_tz(dt).strftime("%d-%b-%Y, %I:%M %p ") + DISPLAY_TZ_LABEL


def _fmt_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "\u2014"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m} min"


def pdf_safe(text: Optional[str], *, limit: int = _MAX_WM_FIELD) -> str:
    """Make a string renderable by reportlab's built-in Latin-1 fonts.

    Standard Type-1 fonts cannot draw Devanagari/Tamil/etc.; unencodable
    characters would otherwise render as empty boxes and a watermark made of
    boxes identifies nobody. They are replaced with '?' — which is why the
    watermark ALSO always carries the audit ref and a masked phone, so a copy
    stays traceable even for a non-Latin name.
    """
    if not text:
        return ""
    cleaned = " ".join(str(text).split())  # collapse newlines/tabs
    cleaned = cleaned.encode("latin-1", "replace").decode("latin-1")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "\u2026".encode("latin-1", "replace").decode("latin-1")
    return cleaned


class _WatermarkedDoc(_Doc):
    """_Doc that stamps the watermark on every page before it is closed.

    `_Doc` breaks pages in exactly two places (`_room` and `finish`), both via
    `self.c.showPage()`. Wrapping the canvas' showPage guarantees no page can
    ever be emitted without the stamp — including pages added by future edits
    to the template.
    """

    def __init__(self, buf: io.BytesIO, watermark: PdfWatermark):
        super().__init__(buf)
        # DETERRENT: no copy / no modify. Owner password is random and thrown
        # away; empty user password so the file opens without prompting.
        enc = StandardEncryption(
            userPassword="",
            ownerPassword=secrets.token_urlsafe(24),
            canPrint=1,
            canModify=0,
            canCopy=0,
            canAnnotate=0,
            strength=128,
        )
        self.c = pdf_canvas.Canvas(buf, pagesize=A4, encrypt=enc)
        self.wm = watermark
        self.page_no = 1
        self._set_metadata()
        original_show_page = self.c.showPage

        def _stamped_show_page() -> None:
            self._stamp()
            original_show_page()
            self.page_no += 1

        self.c.showPage = _stamped_show_page  # type: ignore[method-assign]

    def _identity_line(self) -> str:
        wm = self.wm
        parts = [
            pdf_safe(wm.viewer_name) or "Unknown user",
            pdf_safe(wm.viewer_role, limit=30),
        ]
        if wm.viewer_hint:
            parts.append(pdf_safe(wm.viewer_hint, limit=20))
        parts.append(_fmt_dt(wm.generated_at))
        parts.append(f"Ref {pdf_safe(wm.ref, limit=20)}")
        return "  \u00b7  ".join(p for p in parts if p)

    def _set_metadata(self) -> None:
        # Also traceable from File > Properties in any viewer.
        self.c.setTitle("Visit Care Summary")
        self.c.setAuthor("NurseConnect")
        self.c.setSubject(f"Confidential - issued to {pdf_safe(self.wm.viewer_name) or 'unknown'}")
        self.c.setKeywords(f"ref:{pdf_safe(self.wm.ref, limit=20)}")

    def _stamp(self) -> None:
        c = self.c
        line = self._identity_line()
        c.saveState()
<<<<<<< HEAD
        # 1) A single centred diagonal watermark stamp, drawn ON TOP of the
        #    content (semi-transparent) so it can't be hidden by covering it
        #    with a white box and can't be cropped off.
        #
        #    This used to tile the identity line dozens of times across the
        #    page — technically a stronger anti-leak deterrent, but in
        #    practice it read as "the same patient/audit info repeated many
        #    times" on every page, which is not the professional document a
        #    family or nurse should be handed. One clearly legible stamp per
        #    page is still real traceability: the same identity line, plus
        #    the one-line footer below, plus the ref baked into the PDF's
        #    own metadata (see `_set_metadata`) — a leaked copy is still
        #    attributable, it just isn't visually cluttered to get there.
        c.setFillColor(colors.HexColor("#1f2937"))
        c.setFillAlpha(_WM_ALPHA)
        c.setFont(_WM_FONT, _WM_SIZE * 1.6)
        c.translate(self.width / 2, self.height / 2)
        c.rotate(32)
        c.drawCentredString(0, 0, line)
=======
        # 1) Tiled diagonal watermark across the whole page, drawn ON TOP of
        #    the content (semi-transparent) so it can't be hidden by covering
        #    it with a white box and can't be cropped off.
        c.setFillColor(colors.HexColor("#1f2937"))
        c.setFillAlpha(_WM_ALPHA)
        c.setFont(_WM_FONT, _WM_SIZE)
        text_w = c.stringWidth(line, _WM_FONT, _WM_SIZE)
        step_x = text_w + 40 * mm
        step_y = 38 * mm
        c.translate(self.width / 2, self.height / 2)
        c.rotate(32)
        span = max(self.width, self.height) * 1.2
        y = -span
        row = 0
        while y < span:
            x = -span - (row % 2) * (step_x / 2)
            while x < span:
                c.drawString(x, y, line)
                x += step_x
            y += step_y
            row += 1
>>>>>>> origin/staging
        c.restoreState()

        # 2) Solid footer on every page: readable even on a low-quality photo.
        c.saveState()
        c.setFillColor(colors.HexColor("#555555"))
        c.setFont(_FOOTER_FONT, _FOOTER_SIZE)
        footer = (
            f"CONFIDENTIAL - issued to {line} \u00b7 Page {self.page_no} "
            "\u00b7 Do not share; downloads are logged."
        )
        footer = footer.encode("latin-1", "replace").decode("latin-1")
        margin = 15 * mm
        max_w = self.width - 2 * margin
        while c.stringWidth(footer, _FOOTER_FONT, _FOOTER_SIZE) > max_w and len(footer) > 20:
            footer = footer[:-2]
        c.drawString(margin, 8 * mm, footer)
        c.restoreState()


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
    watermark: PdfWatermark,
    care_notes: Optional[str] = None,
    generated_at: Optional[datetime] = None,
<<<<<<< HEAD
    nurse_id: Optional[str] = None,
    package_name: Optional[str] = None,
    package_code: Optional[str] = None,
    questionnaire: Optional[list[tuple[str, str]]] = None,
    next_visit_label: Optional[str] = None,
    document_title: Optional[str] = None,
=======
>>>>>>> origin/staging
) -> bytes:
    """Render a visit's care summary, watermarked for `watermark`'s viewer.

    `watermark` is required (keyword-only, no default) on purpose: there is
    no code path that can produce an un-attributed copy of this document.
    `care_notes` is only rendered when `include_clinical_notes` is True, so a
    family PDF can never carry them even if a caller passed them in.
<<<<<<< HEAD

    `document_title` lets the caller distinguish the two audiences at the
    top of the page ("NURSE VISIT REPORT" vs "VISIT CARE SUMMARY") without
    this module knowing anything about roles — the API layer still decides
    who gets which title, exactly as it decides `include_clinical_notes`.
    """
    # Belt-and-suspenders: recompute from the actual check-in/check-out
    # timestamps whenever both are present, rather than trusting whatever
    # `duration_minutes` the caller passed in. This is what used to surface
    # as a hard-coded-looking "0 min" — the stored column could be stale or
    # never backfilled, while the timestamps are always the source of truth.
    if check_in_at and check_out_at and check_out_at > check_in_at:
        duration_minutes = int((check_out_at - check_in_at).total_seconds() // 60)

=======
    """
>>>>>>> origin/staging
    buf = io.BytesIO()
    doc = _WatermarkedDoc(buf, watermark)

    _header(
        doc,
        company,
<<<<<<< HEAD
        document_title or ("NURSE VISIT REPORT" if include_clinical_notes else "VISIT CARE SUMMARY"),
        "(Nurse's working copy — internal use only)" if include_clinical_notes else "(Family copy)",
    )

    meta = [
        ("Booking Ref", pdf_safe(booking_ref, limit=40) or "\u2014"),
        ("Generated", _fmt_dt(generated_at or watermark.generated_at)),
        ("Patient", pdf_safe(patient_name) or "\u2014"),
        (
            "Care Provider",
            (pdf_safe(nurse_name) or "\u2014")
            + (f" ({pdf_safe(nurse_council_no, limit=30)})" if nurse_council_no else ""),
        ),
    ]
    if include_clinical_notes and nurse_id:
        meta.append(("Nurse ID", pdf_safe(nurse_id, limit=30)))
    _meta_pairs(doc, meta)
    doc.rule()

    if package_name or package_code:
        doc.text("CARE PACKAGE:", bold=True)
        doc.rule()
        doc.row("Package:", pdf_safe(package_name, limit=60) or "\u2014")
        if package_code:
            doc.row("Package Code:", pdf_safe(package_code, limit=40))
        doc.rule()

=======
        "VISIT CARE SUMMARY",
        "(Nurse's working copy)" if include_clinical_notes else "(Family copy)",
    )

    _meta_pairs(
        doc,
        [
            ("Booking Ref", pdf_safe(booking_ref, limit=40) or "\u2014"),
            ("Generated", _fmt_dt(generated_at or watermark.generated_at)),
            ("Patient", pdf_safe(patient_name) or "\u2014"),
            (
                "Care Provider",
                (pdf_safe(nurse_name) or "\u2014")
                + (f" ({pdf_safe(nurse_council_no, limit=30)})" if nurse_council_no else ""),
            ),
        ],
    )
    doc.rule()

>>>>>>> origin/staging
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
    doc.text("VITALS RECORDED:", bold=True)
    doc.rule()
    if has_vitals:
        bp_s, bp_d = vitals.get("bp_systolic"), vitals.get("bp_diastolic")
        if bp_s is not None and bp_d is not None:
            doc.row("Blood pressure:", f"{bp_s} / {bp_d} mmHg")
        elif bp_s is not None or bp_d is not None:
            # Half a BP reading used to be silently dropped from the PDF.
            doc.row("Blood pressure:", f"{bp_s if bp_s is not None else '?'} / "
                                       f"{bp_d if bp_d is not None else '?'} mmHg (incomplete)")
        if vitals.get("spo2") is not None:
            doc.row("SpO2:", f"{vitals['spo2']}%")
        if vitals.get("pulse") is not None:
            doc.row("Heart rate:", f"{vitals['pulse']} bpm")
        if vitals.get("temperature_f") is not None:
            doc.row("Temperature:", f"{vitals['temperature_f']} \u00b0F")
    else:
        # Explicit, rather than silently omitting the section — a reader of a
        # leaked/forwarded copy should not wonder whether a page is missing.
        doc.wrapped("No vitals were recorded during this visit.")
    doc.rule()

<<<<<<< HEAD
    if questionnaire:
        doc.gap()
        doc.text("PACKAGE QUESTIONNAIRE:", bold=True)
        doc.rule()
        for label, answer in questionnaire:
            doc.text(pdf_safe(label, limit=90), size=_SMALL, bold=True)
            doc.text(pdf_safe(answer, limit=90) or "\u2014", size=_SMALL, indent=5 * mm)
        doc.rule()

=======
>>>>>>> origin/staging
    doc.text(f"{summary_label.upper()}:", bold=True)
    doc.rule()
    doc.wrapped(pdf_safe(summary_text, limit=20000).strip() or "Not recorded.")
    doc.rule()

    if include_clinical_notes:
        doc.gap()
<<<<<<< HEAD
        doc.text("CLINICAL NOTES / OBSERVATIONS (INTERNAL):", bold=True)
=======
        doc.text("CLINICAL NOTES (INTERNAL):", bold=True)
>>>>>>> origin/staging
        doc.rule()
        doc.wrapped(pdf_safe(care_notes, limit=20000).strip() or "No clinical notes recorded.")
        doc.rule()

    doc.gap()
<<<<<<< HEAD
    doc.text("NEXT VISIT:" if not include_clinical_notes else "NEXT ACTION / NEXT VISIT:", bold=True)
    doc.rule()
    doc.wrapped(pdf_safe(next_visit_label, limit=80) or "No further visit is currently scheduled.")
    doc.rule()

    doc.gap()
=======
>>>>>>> origin/staging
    doc.wrapped(
        "This is a computer-generated visit summary and does not require a signature. "
        f"For queries contact {company.support_email}."
    )
    doc.rule(heavy=True)
    return doc.finish()
