"""Document generation for the money side of a booking.

Two documents, two moments:

  * customer tax invoice  — generated when payment is captured;
  * nurse payout advice   — generated when the payout is released.

Both are rendered from live booking/payout data through the shared templates
in `invoice_pdf.py`, with every amount coming from `pricing_engine`. No
figure in either PDF is written by this module.

Both generators are idempotent on their natural key (booking for an invoice,
payout for a statement), so a replayed webhook or a retried release produces
one document, not two.

PDF upload and notification are best-effort by design: a storage hiccup must
never roll back a captured payment or a confirmed bank transfer. The row is
always written first and the PDF attached afterwards, so a failed upload
leaves a recoverable invoice with a null pdf_url rather than no invoice.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company import (
    PLATFORM_FEE_GST_RATE,
    SAC_NURSING_EXEMPT,
    SAC_PLATFORM_FEE_B2B,
    get_company,
)
from app.core.config import settings
from app.integrations.providers import ExternalProviderError, cloudinary_client
from app.models.models import (
    Booking,
    ConsumerProfile,
    Invoice,
    Patient,
    PayoutStatement,
    User,
    WorkerPayout,
    WorkerProfile,
)
from app.services.invoice_pdf import (
    render_customer_invoice_pdf,
    render_payout_statement_pdf,
)
from app.services.pricing_engine import customer_view, money, to_storable
from app.services.pricing_resolver import price_booking

logger = logging.getLogger(__name__)


def _financial_year(when: datetime) -> str:
    """Indian FY label (Apr-Mar) used in document numbers, e.g. 2026 for
    01-Apr-2026 to 31-Mar-2027."""
    return str(when.year if when.month >= 4 else when.year - 1)


async def _next_number(db: AsyncSession, model, column, prefix: str) -> str:
    """Allocate the next document number in a per-financial-year series.

    Counts existing rows in the series rather than keeping a counter table.
    Under concurrency two callers could compute the same number; the unique
    index on the column is what actually enforces uniqueness, and the caller
    retries. Good enough at this volume, and it avoids a sequence that would
    drift from the row count after a failed transaction.
    """
    fy = _financial_year(datetime.now(timezone.utc))
    series = f"{prefix}-{fy}-"
    count = (
        await db.execute(
            select(func.count()).select_from(model).where(column.like(f"{series}%"))
        )
    ).scalar_one()
    return f"{series}{count + 1:05d}"


async def _upload_pdf(pdf_bytes: bytes, folder: str, filename: str) -> Optional[str]:
    """Store a PDF and return its URL, or None if storage is unavailable."""
    try:
        payload = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
        result = await cloudinary_client.upload_base64(
            payload, folder=folder, resource_type="auto"
        )
        return result.get("secure_url")
    except (ExternalProviderError, Exception) as exc:  # noqa: BLE001
        logger.warning("PDF upload failed for %s: %s", filename, exc)
        return None


# ===========================================================================
# Customer invoice
# ===========================================================================
async def generate_customer_invoice(
    db: AsyncSession,
    booking: Booking,
    *,
    render_pdf: bool = True,
) -> Optional[Invoice]:
    """Create (or return) the customer's tax invoice for a paid booking.

    Idempotent on booking_id — the table's unique index means a replayed
    payment webhook returns the existing invoice instead of issuing a second
    tax document for the same supply, which would be a compliance problem as
    much as a data one.
    """
    existing = await db.execute(select(Invoice).where(Invoice.booking_id == booking.id))
    invoice = existing.scalar_one_or_none()
    if invoice is not None:
        if render_pdf and not invoice.pdf_url:
            await attach_invoice_pdf(db, invoice, booking)
        return invoice

    breakdown = await price_booking(db, booking)
    view = customer_view(breakdown)
    company = get_company()

    invoice = Invoice(
        booking_id=booking.id,
        invoice_number=await _next_number(
            db, Invoice, Invoice.invoice_number, settings.INVOICE_NUMBER_PREFIX
        ),
        invoice_type=(
            "composite_healthcare_service"
            if booking.material_included
            else "professional_service"
        ),
        gst_percent=(
            Decimal(view["line_items"][0]["gst_rate_pct"]) if view["line_items"] else Decimal("0")
        ),
        subtotal_amount=breakdown.subtotal,
        tax_amount=breakdown.total_gst,
        total_amount=breakdown.customer_total,
        taxable_value=breakdown.taxable_value,
        exempt_value=breakdown.exempt_value,
        cgst_amount=breakdown.total_cgst,
        sgst_amount=breakdown.total_sgst,
        place_of_supply=company.place_of_supply,
        line_items=view["line_items"],
        # Full calculation, including the internal split, frozen at issue time
        # so a later rate-card edit can't rewrite billing history.
        pricing_snapshot=to_storable(breakdown),
    )
    db.add(invoice)
    await db.flush()

    if render_pdf:
        await attach_invoice_pdf(db, invoice, booking)
    return invoice


async def attach_invoice_pdf(
    db: AsyncSession, invoice: Invoice, booking: Booking
) -> Optional[str]:
    """Render and store the invoice PDF. Best-effort; never raises."""
    try:
        customer_name = await _customer_name(db, booking)
        provider_line = await _provider_line(db, booking)
        pdf = render_customer_invoice_pdf(
            company=get_company(),
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.generated_at.date()
            if invoice.generated_at
            else datetime.now(timezone.utc).date(),
            booking_ref=booking.booking_ref,
            customer_name=customer_name,
            line_items=invoice.line_items,
            taxable_value=Decimal(invoice.taxable_value or 0),
            exempt_value=Decimal(invoice.exempt_value or 0),
            cgst_amount=Decimal(invoice.cgst_amount or 0),
            sgst_amount=Decimal(invoice.sgst_amount or 0),
            total_amount=Decimal(invoice.total_amount),
            subsidy_amount=money(Decimal(booking.subsidy_amount or 0)),
            place_of_supply=invoice.place_of_supply,
            provider_line=provider_line,
        )
        url = await _upload_pdf(pdf, "invoices", invoice.invoice_number)
        if url:
            invoice.pdf_url = url
            invoice.pdf_generated_at = datetime.now(timezone.utc)
            await db.flush()
        return url
    except Exception as exc:  # noqa: BLE001
        # An unrenderable PDF must not undo a captured payment. The invoice
        # row stands and the PDF can be regenerated from it on demand.
        logger.exception("invoice PDF generation failed for %s: %s", invoice.invoice_number, exc)
        return None


async def _customer_name(db: AsyncSession, booking: Booking) -> str:
    pres = await db.execute(select(Patient).where(Patient.id == booking.patient_id))
    patient = pres.scalar_one_or_none()
    if patient is not None and getattr(patient, "full_name", None):
        return patient.full_name
    cres = await db.execute(
        select(User)
        .join(ConsumerProfile, ConsumerProfile.user_id == User.id)
        .where(ConsumerProfile.id == booking.consumer_id)
    )
    user = cres.scalar_one_or_none()
    return (user.full_name if user and user.full_name else "Customer")


async def _provider_line(db: AsyncSession, booking: Booking) -> Optional[str]:
    """'Care Provider: <name> (<council no>)' — printed under the exempt
    healthcare line, which is what supports the marketplace position."""
    if not booking.worker_id:
        return None
    res = await db.execute(
        select(User)
        .join(WorkerProfile, WorkerProfile.user_id == User.id)
        .where(WorkerProfile.id == booking.worker_id)
    )
    user = res.scalar_one_or_none()
    if user is None or not user.full_name:
        return None
    return f"Care Provider: {user.full_name}"


# ===========================================================================
# Nurse payout statement
# ===========================================================================
async def generate_payout_statement(
    db: AsyncSession,
    payout: WorkerPayout,
    *,
    render_pdf: bool = True,
) -> Optional[PayoutStatement]:
    """Create (or return) the nurse's payout advice for a released payout.

    Idempotent on payout_id. Called after a release attempt regardless of
    outcome — the statement reflects whatever Razorpay has confirmed so far,
    and `attach_statement_pdf` refreshes it once a UTR arrives.
    """
    from app.services.payout_service import build_payout_breakdown

    existing = await db.execute(
        select(PayoutStatement).where(PayoutStatement.payout_id == payout.id)
    )
    statement = existing.scalar_one_or_none()
    if statement is not None:
        if render_pdf:
            await attach_statement_pdf(db, statement, payout)
        return statement

    breakdown = await build_payout_breakdown(db, payout)
    if breakdown is None:
        return None

    line_items = [
        {
            "label": "Clinical Service Fee Earned",
            "sac_code": SAC_NURSING_EXEMPT,
            "amount": str(breakdown.worker_service_value),
            "type": "earning",
        },
        {
            "label": "Less: Platform Technology Fee",
            "sac_code": SAC_PLATFORM_FEE_B2B,
            "amount": str(-breakdown.worker_platform_fee_taxable),
            "type": "platform_fee",
        },
        {
            "label": f"Less: {PLATFORM_FEE_GST_RATE.normalize()}% GST on Platform Technology Fee",
            "amount": str(-breakdown.worker_platform_fee_gst),
            "type": "platform_fee_gst",
        },
    ] + [
        {
            "label": f"Less: {d.label}",
            "amount": str(-d.amount),
            "note": d.note,
            "type": "deduction",
        }
        for d in breakdown.deductions
    ]

    statement = PayoutStatement(
        payout_id=payout.id,
        booking_id=payout.booking_id,
        worker_id=payout.worker_id,
        statement_number=await _next_number(
            db,
            PayoutStatement,
            PayoutStatement.statement_number,
            settings.PAYOUT_STATEMENT_PREFIX,
        ),
        gross_earned=breakdown.worker_service_value,
        platform_fee=breakdown.worker_platform_fee_taxable,
        platform_fee_gst=breakdown.worker_platform_fee_gst,
        net_take_home=breakdown.worker_gross,
        total_deductions=breakdown.total_deductions,
        # The stored net_amount is authoritative — it is what was actually
        # sent to Razorpay — so the statement reports it rather than the
        # recomputed figure.
        final_disbursal=money(Decimal(payout.net_amount)),
        line_items=line_items,
    )
    db.add(statement)
    await db.flush()

    if render_pdf:
        await attach_statement_pdf(db, statement, payout)
    return statement


async def attach_statement_pdf(
    db: AsyncSession, statement: PayoutStatement, payout: WorkerPayout
) -> Optional[str]:
    """Render and store the payout advice PDF. Best-effort; never raises.

    Regenerates when a UTR has arrived since the last render, so the nurse's
    copy gains its bank reference once settlement is confirmed rather than
    being frozen at the un-confirmed state.
    """
    # Re-render only when a UTR has landed since the last render — that's the
    # one change worth a new document. Otherwise keep the existing PDF.
    already_final = bool(statement.pdf_url and payout.razorpay_utr)
    if statement.pdf_url and (already_final or not payout.razorpay_utr):
        return statement.pdf_url

    try:
        bres = await db.execute(select(Booking).where(Booking.id == statement.booking_id))
        booking = bres.scalar_one_or_none()

        wres = await db.execute(
            select(WorkerProfile, User)
            .join(User, User.id == WorkerProfile.user_id)
            .where(WorkerProfile.id == statement.worker_id)
        )
        row = wres.first()
        worker, user = row if row else (None, None)

        deductions = [
            {"label": i["label"].replace("Less: ", ""),
             "amount": abs(Decimal(i["amount"])),
             "note": i.get("note")}
            for i in statement.line_items
            if i.get("type") == "deduction"
        ]

        pdf = render_payout_statement_pdf(
            company=get_company(),
            statement_number=statement.statement_number,
            statement_date=(statement.generated_at or datetime.now(timezone.utc)).date(),
            booking_ref=booking.booking_ref if booking else str(statement.booking_id),
            partner_name=(user.full_name if user and user.full_name else "Care Partner"),
            # No human-readable partner code exists on WorkerProfile, so the
            # profile id's prefix is used — stable, unique enough to quote in
            # support, and not a new column just for a PDF header.
            partner_id=f"NUR-{str(statement.worker_id)[:8].upper()}",
            partner_council_no=(worker.registration_no if worker is not None else None),
            gross_earned=Decimal(statement.gross_earned),
            gross_label="Clinical Service Fee Earned",
            gross_sac=SAC_NURSING_EXEMPT,
            platform_fee=Decimal(statement.platform_fee or 0),
            platform_fee_gst=Decimal(statement.platform_fee_gst or 0),
            platform_fee_sac=SAC_PLATFORM_FEE_B2B,
            net_take_home=Decimal(statement.net_take_home),
            deductions=deductions,
            final_disbursal=Decimal(statement.final_disbursal),
            transfer_mode=f"Razorpay Payouts / {settings.RAZORPAYX_PAYOUT_MODE}",
            utr=payout.razorpay_utr,
            payout_reference=payout.razorpay_payout_id,
            payout_status=payout.razorpay_payout_status or payout.status.value,
        )
        url = await _upload_pdf(pdf, "payout_statements", statement.statement_number)
        if url:
            statement.pdf_url = url
            statement.pdf_generated_at = datetime.now(timezone.utc)
            await db.flush()
        return url
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "payout statement PDF failed for %s: %s", statement.statement_number, exc
        )
        return None


# ===========================================================================
# Delivery
# ===========================================================================
async def notify_invoice_ready(db: AsyncSession, booking: Booking, invoice: Invoice) -> None:
    """Tell the customer their receipt is available. Best-effort."""
    try:
        from app.services.common_services import notify_parties

        await notify_parties(
            db,
            ["family"],
            {
                "booking_id": str(booking.id),
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "pdf_url": invoice.pdf_url or "",
            },
            "invoice.ready",
            "Your receipt is ready",
            f"Invoice {invoice.invoice_number} for booking {booking.booking_ref} "
            f"(Rs.{invoice.total_amount}) is available in the app.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoice notification failed for %s: %s", invoice.invoice_number, exc)


async def notify_payout_released(
    db: AsyncSession, payout: WorkerPayout, statement: Optional[PayoutStatement]
) -> None:
    """Tell the nurse. Wording tracks the confirmed state — a payout that
    Razorpay hasn't confirmed is announced as on its way, never as paid."""
    try:
        from app.models.enums import WorkerPayoutStatus
        from app.services.common_services import notify_parties

        confirmed = payout.status == WorkerPayoutStatus.paid
        title = "Payment released" if confirmed else "Payment in progress"
        if confirmed:
            body = (
                f"Rs.{payout.net_amount} has been transferred to your bank account."
                + (f" UTR: {payout.razorpay_utr}." if payout.razorpay_utr else "")
            )
        else:
            body = (
                f"Your payout of Rs.{payout.net_amount} has been initiated and is "
                "awaiting bank confirmation."
            )

        await notify_parties(
            db,
            ["worker"],
            {
                "payout_id": str(payout.id),
                "booking_id": str(payout.booking_id),
                "statement_number": statement.statement_number if statement else "",
                "pdf_url": (statement.pdf_url if statement else "") or "",
            },
            "payout.released",
            title,
            body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("payout notification failed for %s: %s", payout.id, exc)
