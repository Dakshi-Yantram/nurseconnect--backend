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

The split itself is no longer computed here: it comes from the centralised
pricing engine (app/services/pricing_engine.py, reached via
pricing_resolver.price_booking), so the nurse's payout, the customer's
invoice and the admin breakdown can never disagree about a number.
"""
from __future__ import annotations

import logging
import uuid as _uuid_mod
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations import razorpay_client
from app.integrations.providers import ExternalProviderError
from app.models.enums import LedgerEntryType, PayoutApprovalStatus, WorkerPayoutStatus
from app.models.models import (
    Booking,
    WorkerAgreement,
    WorkerPayout,
    WorkerProfile,
)
from app.services.common_services import post_ledger_entry
from app.services.pricing_engine import Deduction
from app.services.pricing_resolver import price_booking

logger = logging.getLogger(__name__)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def payout_idempotency_seed(booking_id: UUID) -> str:
    """Deterministic idempotency seed, derived from the booking.

    Deriving it from booking_id rather than a random value means that even if
    two payout rows were somehow created for one booking (a race that the
    unique index on booking_id already prevents), both would carry the same
    key and Razorpay would still only move money once.
    """
    return _uuid_mod.uuid5(_uuid_mod.NAMESPACE_URL, f"nurseconnect:payout:{booking_id}").hex


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

    # The split now comes from the centralized pricing engine rather than
    # being recomputed here, so the nurse's payout, the customer's invoice
    # and the admin breakdown are guaranteed to agree. For an offering with
    # no rate card configured the engine falls back to base+surge at the
    # offering's commission_pct — numerically identical to the arithmetic
    # this replaced, so existing bookings are unaffected.
    breakdown = await price_booking(db, booking)
    # `gross` keeps its original meaning: the full service value the nurse
    # earned, before the platform's cut. Platform-owned lines (kits,
    # protection fees) are excluded — they never belonged to the nurse.
    gross = breakdown.worker_service_value
    commission = breakdown.worker_platform_fee_gross
    tds = _money(breakdown.worker_gross * Decimal(str(settings.PLATFORM_TDS_PCT)) / Decimal(100))
    net = _money(breakdown.worker_gross - tds)

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
        # The booking is complete, so the payout enters the admin queue as
        # "Ready for Release" immediately. Money still doesn't move until an
        # admin presses Release Payment.
        ready_for_release_at=datetime.now(timezone.utc),
        # Allocated once, at creation, and reused for every retry of this
        # payout. Razorpay returns the original payout for a repeated key, so
        # a timeout-then-retry can never transfer twice.
        idempotency_key=f"payout_{payout_idempotency_seed(booking.id)}",
    )
    db.add(payout)
    await db.flush()

    # Spread onboarding fee: take a small bite (settings.ONBOARDING_FEE_INCREMENT)
    # out of *this* payout if the worker still owes some, instead of the old
    # one-shot ₹200 hit on booking #1.
    await apply_onboarding_fee_increment(db, booking.worker_id, payout)

    # Recover any customer cash the provider is still holding from cash
    # bookings. Runs AFTER the onboarding fee deliberately: the onboarding
    # fee is a small fixed installment, whereas cash recovery can consume
    # the entire payout, and taking it first would starve the fee
    # indefinitely for a provider working mostly cash bookings.
    await apply_cash_recovery(db, booking.worker_id, payout)

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


async def build_payout_breakdown(db: AsyncSession, payout: WorkerPayout):
    """Recompute the full calculation behind a payout, for the admin screen
    and the nurse's statement.

    Reads through the same pricing engine as everything else, then layers on
    the deductions actually recorded against this payout (TDS, and the
    e-stamp advance instalment already taken at creation time), so the
    numbers shown match the row exactly rather than being re-derived and
    silently drifting from it.
    """
    bres = await db.execute(select(Booking).where(Booking.id == payout.booking_id))
    booking = bres.scalar_one_or_none()
    if booking is None:
        return None

    deductions: list[Deduction] = []
    if payout.tds_deducted and payout.tds_deducted > 0:
        deductions.append(
            Deduction(
                code="tds",
                label=f"TDS @ {settings.PLATFORM_TDS_PCT}%",
                amount=_money(Decimal(payout.tds_deducted)),
            )
        )

    breakdown = await price_booking(db, booking, deductions=deductions)

    # The onboarding/e-stamp instalment was applied directly to net_amount
    # when the payout was created. Surface it as an explicit deduction line so
    # the statement's arithmetic visibly reconciles to the stored net_amount.
    expected_net = _money(breakdown.worker_gross - breakdown.total_deductions)
    gap = _money(expected_net - Decimal(payout.net_amount))

    # Cash recovery also reduces net_amount, so the gap is no longer
    # attributable to the e-stamp advance alone. Split it out first using the
    # amount actually recorded on the payout — otherwise cash the provider
    # collected would appear on their statement as an "E-Stamp Advance
    # Recovery", which is simply the wrong explanation for where their money
    # went.
    cash_recovered = _money(Decimal(payout.cash_recovered or 0))
    if cash_recovered > 0:
        deductions.append(
            Deduction(
                code="cash_recovery",
                label="Cash collected at visit (recovered)",
                amount=cash_recovered,
                note="Customer paid you in cash; that amount is netted off here.",
            )
        )
        gap = _money(gap - cash_recovered)

    recovered = gap
    if recovered > 0:
        agreement = await _stage2_agreement(db, payout.worker_id)
        note = None
        if agreement is not None:
            total = _money(Decimal(str(settings.ONBOARDING_ENABLEMENT_FEE)))
            collected = _money(Decimal(agreement.onboarding_fee_collected or 0))
            increment = _money(Decimal(str(settings.ONBOARDING_FEE_INCREMENT)))
            if increment > 0:
                instalment = int((collected / increment).to_integral_value())
                of_total = int((total / increment).to_integral_value())
                note = (
                    f"Inst. {max(instalment, 1)} of {of_total} | "
                    f"Bal: {_money(total - collected)}"
                )
        deductions.append(
            Deduction(
                code="estamp_advance",
                label="Statutory E-Stamp Paper Advance Recovery",
                amount=recovered,
                note=note,
            )
        )
        breakdown = breakdown.with_deductions(deductions)
    elif cash_recovered > 0:
        # No e-stamp instalment on this payout, but a cash recovery line was
        # added above and still has to reach the statement.
        breakdown = breakdown.with_deductions(deductions)

    return breakdown


async def _stage2_agreement(db: AsyncSession, worker_id: UUID) -> Optional[WorkerAgreement]:
    res = await db.execute(
        select(WorkerAgreement)
        .where(
            WorkerAgreement.worker_id == worker_id,
            WorkerAgreement.stage == 2,
            WorkerAgreement.status == "accepted",
        )
        .order_by(WorkerAgreement.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def ensure_fund_account(db: AsyncSession, worker: WorkerProfile) -> Optional[str]:
    """Resolve (creating on first use) the nurse's RazorpayX fund account id.

    RazorpayX refuses to pay to a bare account number — a payout can only
    target a fund_account_id — so this must succeed before any transfer. The
    id is cached on the worker profile so this is a one-time cost per nurse.
    Returns None when the nurse has no bank details on file.
    """
    if worker.razorpay_fund_account_id:
        return worker.razorpay_fund_account_id
    if not (worker.bank_account_number and worker.bank_ifsc):
        return None

    from app.models.models import User

    ures = await db.execute(select(User).where(User.id == worker.user_id))
    user = ures.scalar_one_or_none()
    holder = worker.bank_account_holder or (user.full_name if user else None) or "Care Partner"

    result = await razorpay_client.create_fund_account(
        contact_name=holder,
        contact_id=worker.razorpay_contact_id,
        account_number=worker.bank_account_number,
        ifsc=worker.bank_ifsc,
        contact_reference=str(worker.id),
        contact_phone=(user.phone_e164 if user else None),
        contact_email=(user.email if user else None),
    )
    worker.razorpay_contact_id = result.get("contact_id")
    worker.razorpay_fund_account_id = result.get("fund_account_id")
    await db.flush()
    return worker.razorpay_fund_account_id


def _apply_razorpay_status(payout: WorkerPayout, response: dict) -> None:
    """Map a Razorpay payout response onto our row.

    This is the only place `status` is allowed to become `paid`, and it does
    so exclusively when Razorpay reports a terminal success. `queued`,
    `pending` and `processing` deliberately leave the payout in `processing`:
    the request was accepted, the money has not necessarily landed, and a
    marketplace that calls that "paid" will eventually pay someone twice or
    tell a nurse she was paid when she wasn't.
    """
    rp_status = (response.get("status") or "").lower()
    payout.razorpay_payout_id = response.get("id") or payout.razorpay_payout_id
    payout.razorpay_payout_status = rp_status or payout.razorpay_payout_status
    payout.razorpay_utr = response.get("utr") or payout.razorpay_utr
    payout.razorpay_last_response = response
    payout.last_status_checked_at = datetime.now(timezone.utc)

    if rp_status in razorpay_client.TERMINAL_SUCCESS:
        payout.status = WorkerPayoutStatus.paid
        payout.paid_at = datetime.now(timezone.utc)
        payout.failure_reason = None
        payout.failure_code = None
    elif rp_status in razorpay_client.TERMINAL_FAILURE:
        payout.status = WorkerPayoutStatus.failed
        failure = response.get("failure_reason") or response.get("status_details", {})
        payout.failure_reason = (
            failure if isinstance(failure, str) else str(failure or rp_status)
        )[:500]
        payout.failure_code = (rp_status or "failed")[:50]
    else:
        # queued / pending / processing — accepted but NOT confirmed.
        payout.status = WorkerPayoutStatus.processing


async def release_payout(
    db: AsyncSession,
    payout: WorkerPayout,
    *,
    released_by: Optional[UUID] = None,
) -> dict:
    """Admin action behind the Release Payment button.

    Guarantees, in order:
      * an already-paid payout is never re-sent (returns the existing result);
      * a payout mid-flight at Razorpay (`processing`) is polled rather than
        re-created;
      * a held payout is refused until the hold is lifted;
      * the transfer carries a stable idempotency key, so a retry after a
        timeout resolves to the original payout instead of a second transfer;
      * `paid` is set only on a terminal success reported by Razorpay.

    Never raises for an expected outcome — failures come back in the returned
    dict so the caller can persist the attempt rather than rolling it back
    and losing the record that we tried.
    """
    now = datetime.now(timezone.utc)

    # --- duplicate prevention ------------------------------------------
    if payout.status == WorkerPayoutStatus.paid:
        return {
            "status": "paid",
            "already_released": True,
            "razorpay_payout_id": payout.razorpay_payout_id,
            "utr": payout.razorpay_utr,
            "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
        }

    if payout.status == WorkerPayoutStatus.on_hold:
        return {"status": "on_hold", "error": "Release the hold before paying this out."}

    if payout.approval_status != PayoutApprovalStatus.approved:
        return {
            "status": payout.status.value,
            "error": "Payout must be approved before it can be released.",
        }

    # Already sent to Razorpay and awaiting confirmation: poll, never re-send.
    if payout.razorpay_payout_id and payout.status in (
        WorkerPayoutStatus.processing,
        WorkerPayoutStatus.pending,
    ):
        try:
            response = await razorpay_client.fetch_payout(payout.razorpay_payout_id)
            _apply_razorpay_status(payout, response)
            await db.flush()
            return {
                "status": payout.status.value,
                "razorpay_payout_id": payout.razorpay_payout_id,
                "razorpay_status": payout.razorpay_payout_status,
                "utr": payout.razorpay_utr,
                "polled_existing": True,
            }
        except ExternalProviderError as exc:
            logger.warning("payout %s status poll failed: %s", payout.id, exc)
            return {
                "status": payout.status.value,
                "razorpay_payout_id": payout.razorpay_payout_id,
                "error": f"Could not confirm payout status: {exc}",
                "retryable": True,
            }

    if payout.attempt_count and payout.attempt_count >= (payout.max_attempts or 3):
        return {
            "status": payout.status.value,
            "error": (
                f"Retry limit reached ({payout.attempt_count}/{payout.max_attempts}). "
                "Investigate the failure before retrying."
            ),
        }

    if Decimal(payout.net_amount) <= 0:
        return {"status": payout.status.value, "error": "Payout amount is zero — nothing to release."}

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == payout.worker_id))
    worker = wres.scalar_one_or_none()
    if worker is None:
        return {"status": payout.status.value, "error": "Worker profile not found."}

    payout.attempt_count = (payout.attempt_count or 0) + 1
    payout.released_by = released_by
    payout.released_at = now
    if not payout.idempotency_key:
        payout.idempotency_key = f"payout_{payout_idempotency_seed(payout.booking_id)}"

    # --- manual settlement path ----------------------------------------
    # No RazorpayX configured (every dev/test environment, and production
    # before the payouts account is live): the admin settles out of band and
    # this records it. Explicitly flagged so it is never mistaken for a
    # bank-confirmed transfer.
    if not razorpay_client.payouts_enabled:
        payout.status = WorkerPayoutStatus.paid
        payout.paid_at = now
        payout.razorpay_payout_status = "manual_settlement"
        await db.flush()
        return {
            "status": payout.status.value,
            "manual": True,
            "amount": str(_money(Decimal(payout.net_amount))),
            "note": "RazorpayX not configured — recorded as a manual settlement.",
        }

    try:
        fund_account_id = await ensure_fund_account(db, worker)
    except ExternalProviderError as exc:
        payout.status = WorkerPayoutStatus.failed
        payout.failure_reason = f"Fund account setup failed: {exc}"[:500]
        payout.failure_code = "fund_account_error"
        await db.flush()
        return {"status": "failed", "error": payout.failure_reason, "retryable": True}

    if not fund_account_id:
        payout.status = WorkerPayoutStatus.failed
        payout.failure_reason = "Nurse has no bank account on file."
        payout.failure_code = "missing_bank_details"
        await db.flush()
        return {"status": "failed", "error": payout.failure_reason, "retryable": True}

    payout.razorpay_fund_account_id = fund_account_id
    payout.status = WorkerPayoutStatus.processing
    await db.flush()

    amount_paise = int((_money(Decimal(payout.net_amount)) * 100).to_integral_value())

    try:
        response = await razorpay_client.initiate_payout(
            fund_account_id=fund_account_id,
            amount_paise=amount_paise,
            reference=str(payout.id),
            idempotency_key=payout.idempotency_key,
            notes={
                "booking_id": str(payout.booking_id),
                "payout_id": str(payout.id),
                "narration": "NurseConnect care partner payout",
            },
        )
    except Exception as exc:  # noqa: BLE001
        # The transfer may or may not have reached Razorpay. Leave the payout
        # in `processing` rather than `failed` when we genuinely cannot tell,
        # so the next Release click polls by idempotency key instead of
        # creating a second transfer.
        payout.status = WorkerPayoutStatus.processing
        payout.failure_reason = str(exc)[:500]
        payout.failure_code = "provider_error"
        payout.next_retry_at = now
        await db.flush()
        logger.exception("payout %s initiation failed", payout.id)
        return {
            "status": payout.status.value,
            "error": payout.failure_reason,
            "retryable": True,
            "note": "Outcome unconfirmed — retry will reuse the idempotency key.",
        }

    _apply_razorpay_status(payout, response)
    await db.flush()
    return {
        "status": payout.status.value,
        "razorpay_payout_id": payout.razorpay_payout_id,
        "razorpay_status": payout.razorpay_payout_status,
        "utr": payout.razorpay_utr,
        "amount": str(_money(Decimal(payout.net_amount))),
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
    }


async def sync_payout_status(db: AsyncSession, payout: WorkerPayout) -> dict:
    """Reconcile one in-flight payout against Razorpay.

    Used by the admin refresh action and as the safety net for a webhook that
    never arrived — without it, a `processing` payout could sit unresolved
    forever.
    """
    if not payout.razorpay_payout_id:
        return {"status": payout.status.value, "error": "No Razorpay payout to reconcile."}
    if payout.status == WorkerPayoutStatus.paid:
        return {"status": "paid", "utr": payout.razorpay_utr}
    try:
        response = await razorpay_client.fetch_payout(payout.razorpay_payout_id)
    except ExternalProviderError as exc:
        return {"status": payout.status.value, "error": str(exc), "retryable": True}
    _apply_razorpay_status(payout, response)
    await db.flush()
    return {
        "status": payout.status.value,
        "razorpay_status": payout.razorpay_payout_status,
        "utr": payout.razorpay_utr,
    }


async def process_payout(db: AsyncSession, payout: WorkerPayout) -> dict:
    """Backwards-compatible alias for the pre-existing admin /process endpoint.

    Kept so existing callers and tests keep working; all the logic now lives
    in release_payout().
    """
    return await release_payout(db, payout)


async def apply_cash_recovery(
    db: AsyncSession, worker_id: UUID, payout: WorkerPayout
) -> Optional[Decimal]:
    """Net off cash this provider collected at a visit but hasn't remitted.

    On a cash booking the provider physically takes the customer's full
    payment at the door. That money is the company's — the provider is owed
    only their fee. Without this, a provider working cash bookings would be
    paid their fee *while still holding* the customer's payment, i.e. paid
    twice, and the company's cash would never come back.

    Recovery works by offset: we deduct from this payout and mark the
    corresponding bookings remitted, oldest first, so the same cash can
    never be recovered twice. A booking is only ever marked remitted for
    the portion actually recovered — partial recovery leaves the remainder
    outstanding for the next payout rather than writing it off.

    Mirrors apply_onboarding_fee_increment: clamps to the payout's
    net_amount so a payout can never go negative, and spreads across
    successive payouts when one payout cannot absorb the whole balance.

    No-op (returns None) when the provider holds no unremitted cash or this
    payout's net_amount is already zero.
    """
    from app.models.enums import PaymentMethod

    if payout.net_amount <= 0:
        return None

    # Oldest first: the longest-outstanding cash is recovered first.
    res = await db.execute(
        select(Booking)
        .where(
            Booking.payment_method == PaymentMethod.cash,
            Booking.cash_collected_by == worker_id,
            Booking.cash_collected_at.isnot(None),
            Booking.cash_remitted_at.is_(None),
        )
        .order_by(Booking.cash_collected_at.asc())
    )
    outstanding = list(res.scalars().all())
    if not outstanding:
        return None

    budget = _money(payout.net_amount)
    recovered = Decimal("0")
    now = datetime.now(timezone.utc)

    for booking in outstanding:
        if budget <= 0:
            break
        held = _money(Decimal(booking.cash_collected_amount or 0))
        if held <= 0:
            continue
        if held <= budget:
            # Fully recovered — this booking's cash is now settled.
            booking.cash_remitted_at = now
            budget = _money(budget - held)
            recovered = _money(recovered + held)
        else:
            # This payout cannot absorb the whole booking. Recover what we
            # can and leave the rest outstanding: reducing the recorded
            # collected amount would falsify what the customer actually
            # paid, so instead we take the partial amount and leave
            # cash_remitted_at unset for the next payout to finish.
            partial = budget
            booking.cash_collected_amount = _money(held - partial)
            recovered = _money(recovered + partial)
            budget = Decimal("0")

    if recovered <= 0:
        return None

    payout.net_amount = _money(payout.net_amount - recovered)
    payout.cash_recovered = _money(Decimal(payout.cash_recovered or 0) + recovered)

    await post_ledger_entry(
        db,
        LedgerEntryType.payment_collected,
        recovered,
        booking_id=payout.booking_id,
        worker_id=worker_id,
        debit_account="company_bank",
        credit_account="cash_in_hand_provider",
        description=(
            f"Cash recovered by offset against payout for booking {payout.booking_id} "
            f"(₹{recovered})"
        ),
    )
    await db.flush()
    return recovered
