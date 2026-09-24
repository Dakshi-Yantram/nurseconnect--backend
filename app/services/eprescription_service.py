"""E-prescription generation.

Flow (see app/api/v1/eprescriptions.py for the endpoints that call this):
  1. Doctor writes the Rx inside the app during/after a Dyte call (drugs,
     diet notes, patient issues).
  2. build_verification_hash() produces a SHA-256 over the canonical Rx
     payload — this is what the QR code / verify link checks against, so
     any tampering with the stored PDF or DB row is detectable.
  3. render_prescription_pdf() lays out a one-page PDF: clinic/doctor
     header, patient details, drug table, diet + issues notes, the doctor's
     saved signature PNG stamped bottom-left, and a QR code (linking to the
     public verify endpoint) stamped bottom-right.
  4. The PDF bytes are uploaded to Cloudinary (via the existing
     cloudinary_client, resource_type="auto") and the returned URL is saved
     on the Prescription row alongside the hash.

Kept deliberately dependency-light: reportlab for the PDF, qrcode+Pillow for
the QR image. No external PDF service, no headless browser.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import date, datetime
from typing import Any, Optional

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

from app.core.config import settings


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def build_verification_hash(payload: dict) -> str:
    """SHA-256 over the canonical (sorted-key) JSON of the Rx payload.

    payload should include everything that matters for authenticity:
    prescription_id, patient_id, issued_by_worker_id, doctor name/reg no,
    drugs_listed, diet_notes, patient_issues, prescribed_date. Two calls
    with the same content always produce the same hash, so a verifier can
    independently recompute it from the DB row and compare.
    """
    canonical = json.dumps(payload, sort_keys=True, default=_json_default, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_verify_url(verification_hash: str) -> str:
    base = settings.PUBLIC_APP_URL.rstrip("/")
    return f"{base}/verify-prescription/{verification_hash}"


def make_qr_png_bytes(url: str) -> bytes:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_prescription_pdf(
    *,
    patient_name: str,
    patient_age_gender: Optional[str],
    doctor_name: str,
    doctor_reg_no: Optional[str],
    hospital_clinic: Optional[str],
    prescribed_date: date,
    drugs_listed: list[dict],
    diet_notes: Optional[str],
    patient_issues: Optional[str],
    signature_png_bytes: Optional[bytes],
    verification_hash: str,
    booking_ref: Optional[str] = None,
) -> bytes:
    """Returns the finished PDF as raw bytes."""
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    y = height - margin

    # --- Header ----------------------------------------------------------
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, "NurseConnect — E-Prescription")
    y -= 8 * mm
    c.setStrokeColor(colors.HexColor("#2563eb"))
    c.setLineWidth(1)
    c.line(margin, y, width - margin, y)
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, doctor_name)
    c.setFont("Helvetica", 9)
    if doctor_reg_no:
        c.drawRightString(width - margin, y, f"Reg. No: {doctor_reg_no}")
    y -= 5 * mm
    if hospital_clinic:
        c.setFont("Helvetica", 9)
        c.drawString(margin, y, hospital_clinic)
        y -= 5 * mm
    c.drawString(margin, y, f"Date: {prescribed_date.isoformat()}")
    if booking_ref:
        c.drawRightString(width - margin, y, f"Booking: {booking_ref}")
    y -= 8 * mm

    # --- Patient -----------------------------------------------------------
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, f"Patient: {patient_name}")
    if patient_age_gender:
        c.setFont("Helvetica", 10)
        c.drawRightString(width - margin, y, patient_age_gender)
    y -= 8 * mm

    # --- Rx / drug table -----------------------------------------------------
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "℞")
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 9)
    col_x = [margin, margin + 65 * mm, margin + 105 * mm, margin + 140 * mm]
    headers = ["Drug", "Dose", "Frequency", "Duration"]
    for x, h in zip(col_x, headers):
        c.drawString(x, y, h)
    y -= 3 * mm
    c.line(margin, y, width - margin, y)
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    for drug in drugs_listed or []:
        if y < margin + 60 * mm:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica", 9)
        c.drawString(col_x[0], y, str(drug.get("name", ""))[:35])
        c.drawString(col_x[1], y, str(drug.get("dose", ""))[:20])
        c.drawString(col_x[2], y, str(drug.get("frequency", ""))[:20])
        c.drawString(col_x[3], y, str(drug.get("duration", ""))[:20])
        y -= 6 * mm
    y -= 4 * mm

    # --- Notes -----------------------------------------------------------
    if diet_notes:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, y, "Diet notes:")
        y -= 5 * mm
        c.setFont("Helvetica", 9)
        for line in _wrap(diet_notes, 100):
            c.drawString(margin, y, line)
            y -= 5 * mm
        y -= 2 * mm

    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y, "Patient issues:")
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    for line in _wrap(patient_issues or "All okay — nothing flagged at this consultation.", 100):
        c.drawString(margin, y, line)
        y -= 5 * mm
    y -= 6 * mm

    # --- Signature + QR footer -------------------------------------------
    footer_y = margin + 35 * mm
    if y > footer_y:
        y = footer_y

    if signature_png_bytes:
        try:
            from reportlab.lib.utils import ImageReader
            sig_img = ImageReader(io.BytesIO(signature_png_bytes))
            c.drawImage(sig_img, margin, footer_y - 5 * mm, width=45 * mm, height=20 * mm,
                        preserveAspectRatio=True, mask="auto")
        except Exception:  # noqa: BLE001 — never let a bad signature image break Rx generation
            pass
    c.setFont("Helvetica", 8)
    c.line(margin, footer_y - 6 * mm, margin + 45 * mm, footer_y - 6 * mm)
    c.drawString(margin, footer_y - 10 * mm, f"{doctor_name} (digitally signed)")

    qr_bytes = make_qr_png_bytes(build_verify_url(verification_hash))
    from reportlab.lib.utils import ImageReader
    qr_img = ImageReader(io.BytesIO(qr_bytes))
    qr_size = 22 * mm
    c.drawImage(qr_img, width - margin - qr_size, footer_y - 6 * mm, width=qr_size, height=qr_size)
    c.setFont("Helvetica", 6)
    c.drawRightString(width - margin, footer_y - 8 * mm - qr_size, f"Verify: {verification_hash[:16]}…")

    c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        if len(current) + len(w) + 1 <= width:
            current = f"{current} {w}".strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines or [""]
