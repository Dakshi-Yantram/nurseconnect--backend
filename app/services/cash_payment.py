"""Cash-on-delivery payment module.

Deliberately a module of its own rather than branches inside the Razorpay
path. The two methods share a *lifecycle shape* but almost none of their
mechanics:

    Razorpay : order -> customer pays -> signature verify -> captured
    Cash     : select -> booking confirmed -> provider collects at visit
               -> remitted to company

Mixing them would mean every step of the online flow growing an
`if payment_method == cash` arm. Keeping them separate lets both stay
readable, and lets shared concerns (booking confirmation, dispatch,
invoicing, ledger) be called by both rather than reimplemented.

Lifecycle
---------
    pending    customer has not chosen / not confirmed
    cash_due   booking CONFIRMED and dispatchable; money not yet received
    captured   provider collected the money at the visit
    (failed)   customer refused / visit cancelled without collection

Important accounting property: `cash_due` is NOT revenue. Only the move to
`captured` posts a ledger entry, so cash bookings never inflate collections.
Between collection and remittance the money is a receivable owed by the
provider, which `outstanding_cash_for_worker` reports and the payout
deducts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    BookingStatus,
    LedgerEntryType,
    PaymentMethod,
    PaymentStatus,
)
from app.models.models import Booking
from app.services.common_services import post_ledger_entry

logger = logging.getLogger(__name__)


class CashPaymentError(Exception):
    """Raised when a cash operation is not valid for the booking's state."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def is_cash_eligible(booking: Booking) -> tuple[bool, Optional[str]]:
    """Whether this booking may be paid in cash.

    Cash requires a person to hand money over at a visit, so anything that
    isn't an attended in-person visit cannot use it. Returns (ok, reason).
    """
    if booking.payment_status == PaymentStatus.captured:
        return False, "Already paid."
    if booking.razorpay_payment_id:
        return False, "An online payment has already been made for this booking."
    return True, None


# ---------------------------------------------------------------------------
# Step 1 — customer selects cash
# ---------------------------------------------------------------------------
async def select_cash_payment(db: AsyncSession, booking: Booking) -> Booking:
    """Mark the booking as cash and confirm it.

    Mirrors what a captured Razorpay payment does to booking state — the
    booking becomes confirmed and dispatchable — because operationally a
    cash booking is just as real. What it deliberately does NOT do is post
    a ledger entry: no money has moved yet.
    """
    ok, reason = is_cash_eligible(booking)
    if not ok:
        raise CashPaymentError("CASH_NOT_ELIGIBLE", reason or "Cash is not available.")

    # Idempotent: re-selecting cash on an already-cash booking is a no-op.
    if booking.payment_status == PaymentStatus.cash_due:
        return booking

    booking.payment_method = PaymentMethod.cash
    booking.payment_status = PaymentStatus.cash_due
    # Guarded workflows still wait on prescription review, exactly as they
    # do for an online payment — imported here to avoid a circular import.
    from app.services.composite_care_workflow import is_guarded_workflow

    booking.status = (
        BookingStatus.prescription_pending
        if is_guarded_workflow(booking)
        else BookingStatus.confirmed
    )
    if booking.dispatch_started_at is None:
        booking.dispatch_started_at = datetime.now(timezone.utc)
    return booking


# ---------------------------------------------------------------------------
# Step 2 — provider collects at the visit
# ---------------------------------------------------------------------------
async def record_cash_collection(
    db: AsyncSession,
    booking: Booking,
    *,
    worker_id: UUID,
    amount: Optional[Decimal] = None,
) -> Booking:
    """Record that the provider took the money. This is the revenue event.

    Posts the ledger entry here and only here, so collections reflect money
    actually received.
    """
    if booking.payment_method != PaymentMethod.cash:
        raise CashPaymentError("NOT_A_CASH_BOOKING", "This booking is not a cash booking.")
    if booking.payment_status == PaymentStatus.captured:
        # Idempotent — a double tap on "collected" must not double-post.
        return booking
    if booking.payment_status != PaymentStatus.cash_due:
        raise CashPaymentError(
            "CASH_NOT_DUE",
            f"Cash cannot be collected while the booking is '{booking.payment_status.value}'.",
        )

    collected = Decimal(amount if amount is not None else booking.total_amount)
    if collected <= 0:
        raise CashPaymentError("INVALID_AMOUNT", "Collected amount must be positive.")
    if collected > Decimal(booking.total_amount):
        raise CashPaymentError(
            "AMOUNT_EXCEEDS_TOTAL",
            "Collected amount is greater than the booking total.",
        )

    booking.cash_collected_at = datetime.now(timezone.utc)
    booking.cash_collected_by = worker_id
    booking.cash_collected_amount = collected
    booking.payment_status = PaymentStatus.captured

    await post_ledger_entry(
        db,
        LedgerEntryType.payment_collected,
        collected,
        booking_id=booking.id,
        consumer_id=booking.consumer_id,
        worker_id=worker_id,
        debit_account="cash_in_hand_provider",
        credit_account="consumer_payment",
        description=f"Cash collected at visit for booking {booking.booking_ref}",
    )
    return booking


# ---------------------------------------------------------------------------
# Step 3 — provider remits the cash to the company
# ---------------------------------------------------------------------------
async def record_cash_remittance(db: AsyncSession, booking: Booking) -> Booking:
    """Finance confirms the collected cash reached the company account."""
    if booking.payment_method != PaymentMethod.cash:
        raise CashPaymentError("NOT_A_CASH_BOOKING", "This booking is not a cash booking.")
    if booking.payment_status != PaymentStatus.captured:
        raise CashPaymentError("CASH_NOT_COLLECTED", "Cash has not been collected yet.")
    if booking.cash_remitted_at is not None:
        return booking  # idempotent

    booking.cash_remitted_at = datetime.now(timezone.utc)
    await post_ledger_entry(
        db,
        LedgerEntryType.payment_collected,
        Decimal(booking.cash_collected_amount or booking.total_amount),
        booking_id=booking.id,
        consumer_id=booking.consumer_id,
        worker_id=booking.cash_collected_by,
        debit_account="company_bank",
        credit_account="cash_in_hand_provider",
        description=f"Cash remitted to company for booking {booking.booking_ref}",
    )
    return booking


# ---------------------------------------------------------------------------
# Receivables
# ---------------------------------------------------------------------------
async def outstanding_cash_for_worker(db: AsyncSession, worker_id: UUID) -> Decimal:
    """Cash this provider has collected but not yet remitted.

    The provider is holding company money, so this is netted off their
    payout. Without it, a provider on cash bookings would be paid their fee
    while still holding the customer's full payment.
    """
    res = await db.execute(
        select(func.coalesce(func.sum(Booking.cash_collected_amount), 0)).where(
            Booking.payment_method == PaymentMethod.cash,
            Booking.cash_collected_by == worker_id,
            Booking.cash_collected_at.isnot(None),
            Booking.cash_remitted_at.is_(None),
        )
    )
    return Decimal(res.scalar_one() or 0)
