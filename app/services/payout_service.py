"""Worker payout generation and processing.

Before this existed, no WorkerPayout row was ever created — so a nurse's
earnings always showed zero no matter how many visits they completed. A payout
is now generated the moment a visit is checked out, and admin processes it
(optionally via RazorpayX).

The split, per booking:
    gross = base_amount + surge_amount          (the service value)
    commission = gross * commission%            (platform's cut)
    tds = (gross - commission) * TDS%           (statutory withholding)
    net = gross - commission - tds              (what the nurse receives)

Commission% comes from the booked service/package; when the offering doesn't
set one, settings.PLATFORM_COMMISSION_PCT is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations import razorpay_client
from app.models.enums import LedgerEntryType, WorkerPayoutStatus
from app.models.models import (
    Booking,
    CarePackage,
    ServiceCatalogue,
    WorkerAgreement,
    WorkerPayout,
    WorkerProfile,
)
from app.services.common_services import post_ledger_entry


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _commission_pct(db: AsyncSession, booking: Booking) -> Decimal:
    """Commission% for the booked offering, falling back to the platform default."""
    pct: Optional[Decimal] = None
    if booking.service_id:
        res = await db.execute(
            select(ServiceCatalogue.commission_pct).where(ServiceCatalogue.id == booking.service_id)
        )
        pct = res.scalar_one_or_none()
    elif booking.package_id:
        res = await db.execute(
            select(CarePackage.commission_pct).where(CarePackage.id == booking.package_id)
        )
        pct = res.scalar_one_or_none()
    if pct is None:
        pct = Decimal(str(settings.PLATFORM_COMMISSION_PCT))
    return Decimal(pct)


async def create_payout_for_booking(db: AsyncSession, booking: Booking) -> Optional[WorkerPayout]:
    """Create a pending payout for a completed booking.

    Idempotent: returns the existing row if one already exists for this
    booking, so a duplicate checkout (retry, offline replay) never pays twice.
    No-op when the booking has no assigned worker. Flushes but does not commit —
    the caller owns the transaction so the payout lands atomically with checkout.
    """
    if not booking.worker_id:
        return None

    existing = await db.execute(
        select(WorkerPayout).where(WorkerPayout.booking_id == booking.id)
    )
    prior = existing.scalar_one_or_none()
    if prior is not None:
        return prior

    gross = _money(Decimal(booking.base_amount or 0) + Decimal(booking.surge_amount or 0))
    commission_pct = await _commission_pct(db, booking)
    commission = _money(gross * commission_pct / Decimal(100))
    after_commission = gross - commission
    tds = _money(after_commission * Decimal(str(settings.PLATFORM_TDS_PCT)) / Decimal(100))
    net = _money(after_commission - tds)

    # Ledger: record the commission the platform keeps and the payout owed, so
    # finance reconciliation stays balanced against the payment collected.
    if commission > 0:
        await post_ledger_entry(
            db,
            LedgerEntryType.commission_retained,
            commission,
            booking_id=booking.id,
            worker_id=booking.worker_id,
            description=f"Commission on booking {booking.booking_ref}",
        )
    if tds > 0:
        await post_ledger_entry(
            db,
            LedgerEntryType.tds_deducted,
            tds,
            booking_id=booking.id,
            worker_id=booking.worker_id,
            description=f"TDS on payout for booking {booking.booking_ref}",
        )
    ledger = await post_ledger_entry(
        db,
        LedgerEntryType.worker_payout,
        net,
        booking_id=booking.id,
        worker_id=booking.worker_id,
        description=f"Payout owed for booking {booking.booking_ref}",
    )

    payout = WorkerPayout(
        worker_id=booking.worker_id,
        booking_id=booking.id,
        gross_amount=gross,
        tds_deducted=tds,
        net_amount=net,
        status=WorkerPayoutStatus.pending,
        ledger_entry_id=ledger.id,
    )
    db.add(payout)
    await db.flush()

    # Spread onboarding fee: take a small bite (settings.ONBOARDING_FEE_INCREMENT)
    # out of *this* payout if the worker still owes some, instead of the old
    # one-shot ₹200 hit on booking #1.
    await apply_onboarding_fee_increment(db, booking.worker_id, payout)

    return payout


async def apply_onboarding_fee_increment(db: AsyncSession, worker_id: UUID, payout: WorkerPayout) -> Optional[Decimal]:
    """Collect one increment (settings.ONBOARDING_FEE_INCREMENT, ₹50 by
    default) of the Stage 2 onboarding enablement fee from `payout`'s
    net_amount, if the worker has an accepted Stage 2 (e-stamp Master
    Agreement) and hasn't fully paid the fee yet.

    Spreads the total (settings.ONBOARDING_ENABLEMENT_FEE, ₹200 by default)
    across successive bookings' payouts instead of taking it all from one —
    so booking #1 doesn't take the full brunt. Deducts less than the full
    increment on the final bite if that's all that's left to collect, and
    clamps to the payout's net_amount so a payout never goes negative.

    No-op (returns None) when: no Stage 2 agreement, already fully
    collected, or this payout's net_amount is already zero.
    """
    ares = await db.execute(
        select(WorkerAgreement)
        .where(
            WorkerAgreement.worker_id == worker_id,
            WorkerAgreement.stage == 2,
            WorkerAgreement.status == "accepted",
        )
        .order_by(WorkerAgreement.created_at.desc())
        .limit(1)
    )
    agreement = ares.scalar_one_or_none()
    if agreement is None or agreement.onboarding_fee_deducted:
        return None

    total_fee = _money(Decimal(str(settings.ONBOARDING_ENABLEMENT_FEE)))
    already_collected = _money(Decimal(agreement.onboarding_fee_collected or 0))
    remaining = total_fee - already_collected
    if remaining <= 0:
        agreement.onboarding_fee_deducted = True
        return None

    increment = _money(Decimal(str(settings.ONBOARDING_FEE_INCREMENT)))
    bite = min(increment, remaining)
    actual_deduction = min(bite, payout.net_amount)
    if actual_deduction <= 0:
        return None

    payout.net_amount = _money(payout.net_amount - actual_deduction)
    agreement.onboarding_fee_collected = _money(already_collected + actual_deduction)
    if agreement.onboarding_fee_collected >= total_fee:
        agreement.onboarding_fee_deducted = True

    await post_ledger_entry(
        db,
        LedgerEntryType.platform_fee,
        actual_deduction,
        booking_id=payout.booking_id,
        worker_id=worker_id,
        description=(
            f"Onboarding enablement fee installment ₹{actual_deduction} "
            f"(₹{agreement.onboarding_fee_collected}/₹{total_fee} collected)"
        ),
    )
    await db.flush()
    return actual_deduction


async def process_payout(db: AsyncSession, payout: WorkerPayout) -> dict:
    """Admin action: pay a pending/failed payout.

    Attempts a real RazorpayX transfer when RazorpayX is configured and the
    nurse has bank details; otherwise marks it paid (the admin having settled
    it out of band). Never pays an on_hold payout — a hold must be released
    first, which is the whole point of the hold.
    """
    if payout.status == WorkerPayoutStatus.paid:
        return {"status": "paid", "already": True}
    if payout.status == WorkerPayoutStatus.on_hold:
        return {"status": "on_hold", "error": "Release the hold before processing"}

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == payout.worker_id))
    worker = wres.scalar_one_or_none()
    has_bank = bool(worker and worker.bank_account_number and worker.bank_ifsc)

    payout.attempt_count = (payout.attempt_count or 0) + 1
    payout.status = WorkerPayoutStatus.processing
    await db.flush()

    razorpayx_on = bool(settings.RAZORPAYX_ACCOUNT_NUMBER) and not razorpay_client.mock

    if razorpayx_on and has_bank:
        try:
            result = await razorpay_client.initiate_payout(
                fund_account_id=str(payout.worker_id),
                amount_paise=int((payout.net_amount * 100).to_integral_value()),
                reference=str(payout.id),
                notes={"booking_id": str(payout.booking_id)},
            )
            payout.razorpay_payout_id = result.get("id")
            status = result.get("status", "processed")
            if status in ("processed", "paid"):
                payout.status = WorkerPayoutStatus.paid
                payout.paid_at = datetime.now(timezone.utc)
            else:
                payout.status = WorkerPayoutStatus.processing
        except Exception as e:  # noqa: BLE001
            payout.status = WorkerPayoutStatus.failed
            payout.failure_reason = str(e)[:500]
            await db.flush()
            return {"status": "failed", "error": payout.failure_reason}
    else:
        # No RazorpayX (or in mock mode): admin settles manually and this marks
        # it done. This is also the path every dev/test environment takes.
        payout.status = WorkerPayoutStatus.paid
        payout.paid_at = datetime.now(timezone.utc)
        if not has_bank:
            payout.failure_reason = None  # cleared: manual settlement is fine

    await db.flush()
    return {
        "status": payout.status.value,
        "razorpay_payout_id": payout.razorpay_payout_id,
        "manual": not (razorpayx_on and has_bank),
    }

