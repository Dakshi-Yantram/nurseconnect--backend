"""Payments: Razorpay order creation, signature verification, webhook, history, refunds."""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import (
    CurrentUser,
    get_consumer_profile,
    get_current_user,
    require_roles,
)

from app.integrations import razorpay_client
from app.models.enums import (
    BookingStatus,
    LedgerEntryType,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    WorkerPayoutStatus,
)

# Request schema used by /payments/refund/{booking_id}
from pydantic import BaseModel


class RefundRequest(BaseModel):
    amount: float
    reason: str


from app.models.models import (
    Booking,
    ConsumerProfile,
    FinancialLedger,
    Invoice,
    User,
    WorkerPayout,
    WorkerProfile,
)
from app.schemas.schemas import (
    PaymentOrderRequest,
    PaymentOrderResponse,
    PaymentVerifyRequest,
)
from app.services.common_services import audit, post_ledger_entry
from app.services.composite_care_workflow import is_guarded_workflow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/order", response_model=PaymentOrderResponse)
async def create_order(
    payload: PaymentOrderRequest,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == payload.booking_id, Booking.consumer_id == profile.id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.payment_status == PaymentStatus.captured:
        raise HTTPException(status_code=400, detail="Already paid")

    amount_paise = int(booking.total_amount * 100)
    order = await razorpay_client.create_order(
        amount_paise=amount_paise,
        currency="INR",
        receipt=booking.booking_ref,
        notes={"booking_id": str(booking.id), "consumer_id": str(profile.id)},
    )
    booking.razorpay_order_id = order["id"]
    booking.payment_status = PaymentStatus.initiated
    await db.commit()

    return PaymentOrderResponse(
        razorpay_order_id=order["id"],
        razorpay_key_id=settings.RAZORPAY_KEY_ID or "rzp_test_placeholder",
        amount=amount_paise,
        currency="INR",
        booking_id=booking.id,
    )


# ===========================================================================
# Payment settlement
#
# ROOT-CAUSE NOTE (the "Razorpay succeeded but the app showed
# 'Verification failed / Request failed (500)'" bug):
#
# /verify used to do its best-effort post-payment work — dispatch-notify and
# invoice generation — on the SAME AsyncSession as the request, each guarded
# by `except Exception: await db.rollback()`. That guard was the bug.
# `AsyncSession.rollback()` EXPIRES every ORM instance loaded in that
# session, `booking` included. The very next line built the response with
# `booking.status.value`, which is a *synchronous* attribute read on an
# expired instance, so SQLAlchemy tried to lazy-refresh it from an async
# engine outside greenlet context and raised MissingGreenlet. FastAPI turned
# that into a 500.
#
# By then the payment row was already committed, which is exactly what the
# screenshots show: Razorpay says "Payment Successful", the booking reads
# "Paid ₹1 / Finding a nurse", and the app still shows a 500. The money was
# never at risk; the response was.
#
# The fix has three parts:
#   1. Commit the money-critical state (payment_status, booking status,
#      ledger) on its own and nothing else.
#   2. Snapshot the response into plain primitives IMMEDIATELY after that
#      commit, so building the response can never touch the ORM again.
#   3. Run every best-effort step in an ISOLATED session, so a failure in
#      dispatch-notify or invoicing cannot expire, poison or roll back the
#      request's session.
# ===========================================================================
def _payment_state(booking: Booking, *, replay: bool = False) -> dict:
    """Snapshot the verify/status response as plain primitives.

    Must be called while `booking` is live (freshly loaded or just
    committed on a session with expire_on_commit=False). Everything
    downstream uses this dict, never the ORM instance.
    """
    state = {
        "verified": booking.payment_status == PaymentStatus.captured,
        "booking_id": str(booking.id),
        "booking_ref": booking.booking_ref,
        "booking_status": booking.status.value,
        "payment_status": booking.payment_status.value,
        "payment_method": booking.payment_method.value,
        "razorpay_payment_id": booking.razorpay_payment_id,
        # True when the booking is arranged and dispatchable but the money
        # is still to be collected at the visit. Clients use this to show
        # "Pay ₹X in cash at the visit" instead of a payment failure —
        # `verified` is correctly False here, but nothing is wrong.
        "cash_due": booking.payment_status == PaymentStatus.cash_due,
    }
    if replay:
        state["idempotent_replay"] = True
    return state


async def _run_post_payment_side_effects(
    booking_id: UUID, *, issue_invoice: bool = True
) -> None:
    """Dispatch-notify + invoice, on a session of their own.

    `issue_invoice=False` is used by the cash-selection path. A cash booking
    is confirmed and must be dispatched immediately, but no money has been
    received yet, so raising the customer's tax invoice at that point would
    document a payment that has not happened. For cash the invoice is raised
    at collection instead.

    Isolated deliberately — see the ROOT-CAUSE NOTE above. Nothing in here
    may propagate to the caller: the payment is already captured and
    committed, so an invoice or push-notification problem must never be
    reported to the customer as a failed payment.
    """
    from app.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            bres = await session.execute(select(Booking).where(Booking.id == booking_id))
            booking = bres.scalar_one_or_none()
            if booking is None:
                return

            # Guarded workflows sit in prescription_pending until a
            # pharmacist approves the Rx — dispatch must not start yet.
            if not is_guarded_workflow(booking):
                try:
                    from app.services.dispatch import notify_nearby_workers

                    notified = await notify_nearby_workers(session, booking)
                    await session.commit()
                    if notified == 0:
                        # Nobody was reachable, so this booking would sit on
                        # "Finding a nurse" indefinitely with no one aware of
                        # it. Escalate to ops so it gets assigned by hand
                        # instead of silently stalling.
                        await _escalate_undispatched_booking(session, booking_id)
                except Exception:  # noqa: BLE001
                    await session.rollback()
                    logger.exception("dispatch notify failed for booking %s", booking_id)

            # Re-load: the block above may have rolled back and expired it.
            if issue_invoice:
                bres = await session.execute(select(Booking).where(Booking.id == booking_id))
                booking = bres.scalar_one_or_none()
                if booking is not None:
                    await _issue_invoice(session, booking)
    except Exception:  # noqa: BLE001
        logger.exception("post-payment side effects failed for booking %s", booking_id)


async def _escalate_undispatched_booking(db: AsyncSession, booking_id: UUID) -> None:
    """Tell ops a paid booking found no eligible worker."""
    try:
        from app.services.common_services import notify_admins

        bres = await db.execute(select(Booking).where(Booking.id == booking_id))
        booking = bres.scalar_one_or_none()
        if booking is None:
            return
        ref = booking.booking_ref
        await notify_admins(
            db,
            "booking.no_worker_found",
            "Paid booking has no available provider",
            f"Booking {ref} is paid and confirmed but no approved, online, "
            f"qualified provider was reachable. Assign one manually.",
            {"booking_id": str(booking_id), "booking_ref": ref},
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("undispatched-booking escalation failed for %s", booking_id)


@router.post("/verify")
async def verify_payment(
    payload: PaymentVerifyRequest,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == payload.booking_id, Booking.consumer_id == profile.id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    # Phase 4 hardening: idempotent re-verify — if already captured, return current state.
    if booking.payment_status == PaymentStatus.captured:
        state = _payment_state(booking, replay=True)
        # A replay usually means the first attempt 500'd after committing the
        # payment. Re-run the side effects so a booking that missed dispatch
        # or its invoice the first time still gets both.
        await _run_post_payment_side_effects(booking.id)
        return state
    ok = razorpay_client.verify_payment_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid signature")
    # Hardening: prevent duplicate payment_collected ledger entries for the same razorpay_payment_id.
    dup = await db.execute(
        select(FinancialLedger.id)
        .where(
            FinancialLedger.razorpay_payment_id == payload.razorpay_payment_id,
            FinancialLedger.entry_type == LedgerEntryType.payment_collected,
        )
        .limit(1)
    )
    if dup.scalar_one_or_none():
        # Webhook (or earlier /verify) already processed this payment id.
        booking.razorpay_payment_id = payload.razorpay_payment_id
        booking.payment_status = PaymentStatus.captured
        booking.status = BookingStatus.prescription_pending if is_guarded_workflow(booking) else BookingStatus.confirmed
        if booking.dispatch_started_at is None:
            booking.dispatch_started_at = datetime.now(timezone.utc)
        await db.commit()
        state = _payment_state(booking, replay=True)
        # The webhook captured it; it may not have managed the invoice or
        # dispatch. Re-run both idempotently.
        await _run_post_payment_side_effects(booking.id)
        return state

    booking.razorpay_payment_id = payload.razorpay_payment_id
    booking.payment_status = PaymentStatus.captured
    # Both guarded workflows (Composite Care Package and Service-Only):
    # payment unlocks pharmacist Rx review, NOT dispatch. Dispatch only starts
    # once Rx is approved (see composite_care.py: approve_prescription ->
    # searching_nurse).
    booking.status = BookingStatus.prescription_pending if is_guarded_workflow(booking) else BookingStatus.confirmed
    # Start the dispatch wave clock now — workers only see the booking from
    # this moment, so waves must not count time spent on the payment screen.
    if booking.dispatch_started_at is None:
        booking.dispatch_started_at = datetime.now(timezone.utc)
    # Ledger: payment_collected, commission_retained
    # The post_ledger_entry calls below issue db.flush(), which is where the
    # partial unique index ux_financial_ledger_payment_collected_per_pid
    # raises IntegrityError if a concurrent /webhook already wrote the row.
    try:
        await post_ledger_entry(
            db,
            LedgerEntryType.payment_collected,
            booking.total_amount,
            booking_id=booking.id,
            consumer_id=booking.consumer_id,
            debit_account="razorpay_escrow",
            credit_account="consumer_payment",
            razorpay_payment_id=payload.razorpay_payment_id,
            description=f"Payment for booking {booking.booking_ref}",
        )
    except IntegrityError:
        await db.rollback()
        logger.info(
            "verify race resolved by DB unique index for pid=%s",
            payload.razorpay_payment_id,
        )
        bres = await db.execute(select(Booking).where(Booking.id == payload.booking_id))
        b2 = bres.scalar_one_or_none()
        if not b2:
            raise HTTPException(status_code=409, detail={"code": "concurrency_conflict"}) from None
        state = _payment_state(b2, replay=True)
        await _run_post_payment_side_effects(b2.id)
        return state
    # Commission calculation (use 20% default if no service)
    commission_pct = Decimal("20")
    if booking.service_id:
        from app.models.models import ServiceCatalogue
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        s = sres.scalar_one_or_none()
        if s:
            commission_pct = s.commission_pct
    commission = (booking.total_amount * commission_pct / 100).quantize(Decimal("0.01"))
    await post_ledger_entry(
        db,
        LedgerEntryType.commission_retained,
        commission,
        booking_id=booking.id,
        debit_account="consumer_payment",
        credit_account="platform_revenue",
        description=f"Platform commission @ {commission_pct}%",
    )
    if booking.subsidy_amount and booking.subsidy_amount > 0:
        await post_ledger_entry(
            db,
            LedgerEntryType.subsidy_applied,
            booking.subsidy_amount,
            booking_id=booking.id,
            consumer_id=booking.consumer_id,
            debit_account="subsidy_pool",
            credit_account="consumer_payment",
            description="Subsidy applied",
        )
    await audit(db, profile.user_id, "consumer", "payment.verify", "booking", booking.id, {"amount": str(booking.total_amount)})
    try:
        await db.commit()
    except IntegrityError as e:
        # Concurrency race: the partial unique index on FinancialLedger
        # (ux_financial_ledger_payment_collected_per_pid) caught a duplicate
        # payment_collected row — the parallel /webhook or another /verify
        # already wrote the ledger entry for this razorpay_payment_id.
        # Roll back and return the same idempotent_replay shape the application
        # guard returns for sequential replays.
        await db.rollback()
        logger.info("verify race resolved by DB unique index for pid=%s", payload.razorpay_payment_id)
        # Re-load the booking — webhook may have already flipped it to captured/confirmed.
        bres = await db.execute(select(Booking).where(Booking.id == payload.booking_id))
        b2 = bres.scalar_one_or_none()
        if not b2:
            raise HTTPException(status_code=409, detail={"code": "concurrency_conflict", "error": str(e.orig)}) from None
        state = _payment_state(b2, replay=True)
        await _run_post_payment_side_effects(b2.id)
        return state

    # ---------------------------------------------------------------
    # The money is now committed. From this line on, NOTHING may raise
    # out of this handler: the customer has paid, so the only correct
    # response is success. Snapshot first, then do best-effort work on
    # an isolated session.
    # ---------------------------------------------------------------
    state = _payment_state(booking)
    booking_id = booking.id

    # Dispatch-notify (unless a guarded workflow is waiting on Rx review)
    # and the customer's tax invoice.
    await _run_post_payment_side_effects(booking_id)

    return state


async def _issue_invoice(db: AsyncSession, booking: Booking) -> None:
    """Generate + deliver the customer's tax invoice for a captured payment.

    Best-effort and always in its own try/except: the payment is already
    captured and the booking already confirmed by the time this runs, so an
    invoice or PDF problem must never turn a successful payment into an error
    for the customer. `generate_customer_invoice` is idempotent, so the
    /verify and /webhook paths racing each other still produce one invoice.
    """
    try:
        from app.services.billing_service import (
            generate_customer_invoice,
            notify_invoice_ready,
        )

        invoice = await generate_customer_invoice(db, booking)
        await db.commit()
        if invoice is not None:
            await notify_invoice_ready(db, booking, invoice)
            await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("invoice generation failed for booking %s", booking.id)


# ===========================================================================
# Status + reconciliation
#
# These two exist so that a payment can never be stranded. If /verify is
# interrupted for ANY reason — the app is killed on the Razorpay redirect,
# the network drops, or the server errors — the client can ask what actually
# happened instead of showing the customer a failure for money that left
# their account.
# ===========================================================================
@router.get("/status/{booking_id}")
async def payment_status(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Current, authoritative payment + booking state. Read-only."""
    res = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return _payment_state(booking)


@router.post("/reconcile/{booking_id}")
async def reconcile_payment(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Settle a booking against Razorpay's own record of the order.

    Called by the app when /verify did not return a clean success. It asks
    Razorpay — not the client — whether a payment was actually captured for
    this booking's order, and if so completes exactly the same settlement
    /verify would have done. Safe to call repeatedly.

    This is what turns the reported failure mode ("amount deducted, booking
    still unpaid") into a self-healing state rather than a support ticket.
    """
    res = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.payment_status == PaymentStatus.captured:
        state = _payment_state(booking, replay=True)
        await _run_post_payment_side_effects(booking.id)
        return state

    if not booking.razorpay_order_id:
        return {**_payment_state(booking), "reconciled": False, "reason": "no_order"}

    payments = await razorpay_client.fetch_order_payments(booking.razorpay_order_id)
    captured = next((p for p in payments if p.get("status") == "captured"), None)
    if captured is None:
        # No captured payment exists at Razorpay — the customer genuinely
        # has not been charged, so the booking correctly stays unpaid.
        return {**_payment_state(booking), "reconciled": False, "reason": "not_captured"}

    payment_id = captured.get("id")

    # Same settlement as /verify, minus the signature check — Razorpay's
    # own API is a stronger proof than a client-supplied signature.
    dup = await db.execute(
        select(FinancialLedger.id)
        .where(
            FinancialLedger.razorpay_payment_id == payment_id,
            FinancialLedger.entry_type == LedgerEntryType.payment_collected,
        )
        .limit(1)
    )
    ledger_exists = dup.scalar_one_or_none() is not None

    booking.razorpay_payment_id = payment_id
    booking.payment_status = PaymentStatus.captured
    booking.status = (
        BookingStatus.prescription_pending if is_guarded_workflow(booking) else BookingStatus.confirmed
    )
    if booking.dispatch_started_at is None:
        booking.dispatch_started_at = datetime.now(timezone.utc)

    try:
        if not ledger_exists:
            await post_ledger_entry(
                db,
                LedgerEntryType.payment_collected,
                booking.total_amount,
                booking_id=booking.id,
                consumer_id=booking.consumer_id,
                debit_account="razorpay_escrow",
                credit_account="consumer_payment",
                razorpay_payment_id=payment_id,
                description=f"Reconciled payment for booking {booking.booking_ref}",
            )
        await audit(
            db,
            profile.user_id,
            "consumer",
            "payment.reconcile",
            "booking",
            booking.id,
            {"razorpay_payment_id": payment_id},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        bres = await db.execute(select(Booking).where(Booking.id == booking_id))
        b2 = bres.scalar_one_or_none()
        if b2 is None:
            raise HTTPException(status_code=409, detail={"code": "concurrency_conflict"}) from None
        state = _payment_state(b2, replay=True)
        await _run_post_payment_side_effects(b2.id)
        return {**state, "reconciled": True}

    state = _payment_state(booking)
    await _run_post_payment_side_effects(booking.id)
    return {**state, "reconciled": True}


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(None), db: AsyncSession = Depends(get_db)):
    body = await request.body()
    if not razorpay_client.verify_webhook_signature(body, x_razorpay_signature or ""):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    payload = json.loads(body.decode() or "{}")
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {}) or payload.get("payload", {}).get("refund", {}).get("entity", {})
    razorpay_payment_id = entity.get("id") if event.startswith("payment.") else None
    order_id = entity.get("order_id")

    # Idempotency: refuse to double-process the same payment id
    if razorpay_payment_id:
        dup = await db.execute(
            select(FinancialLedger.id)
            .where(
                FinancialLedger.razorpay_payment_id == razorpay_payment_id,
                FinancialLedger.entry_type == LedgerEntryType.payment_collected,
            )
            .limit(1)
        )
        if dup.scalar_one_or_none():
            return {"received": True, "duplicate": True}

    if event == "payment.captured" and order_id:
        bres = await db.execute(select(Booking).where(Booking.razorpay_order_id == order_id))
        b = bres.scalar_one_or_none()
        if b and b.payment_status != PaymentStatus.captured:
            b.payment_status = PaymentStatus.captured
            b.razorpay_payment_id = razorpay_payment_id
            b.status = BookingStatus.prescription_pending if is_guarded_workflow(b) else BookingStatus.confirmed
            if b.dispatch_started_at is None:
                b.dispatch_started_at = datetime.now(timezone.utc)
            # post_ledger_entry flushes immediately; wrap to catch the partial
            # unique-index violation when /verify won the race.
            try:
                await post_ledger_entry(
                    db,
                    LedgerEntryType.payment_collected,
                    b.total_amount,
                    booking_id=b.id,
                    consumer_id=b.consumer_id,
                    debit_account="razorpay_escrow",
                    credit_account="consumer_payment",
                    razorpay_payment_id=razorpay_payment_id,
                    description=f"Webhook-captured payment for {b.booking_ref}",
                )
                await db.commit()
                # Isolated session — a failure in dispatch or invoicing must
                # not roll back (and expire) the webhook's own session.
                await _run_post_payment_side_effects(b.id)
            except IntegrityError:
                await db.rollback()
                logger.info(
                    "webhook race resolved by DB unique index for pid=%s",
                    razorpay_payment_id,
                )
                return {"received": True, "duplicate": True}
    return {"received": True}


@router.get("/consumer/history")
async def consumer_payment_history(profile: ConsumerProfile = Depends(get_consumer_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Booking)
        .where(Booking.consumer_id == profile.id, Booking.payment_status.in_([PaymentStatus.captured, PaymentStatus.refunded, PaymentStatus.partially_refunded]))
        .order_by(Booking.created_at.desc())
    )
    return [
        {
            "booking_id": str(b.id),
            "booking_ref": b.booking_ref,
            "total_amount": float(b.total_amount),
            "payment_status": b.payment_status.value,
            "razorpay_payment_id": b.razorpay_payment_id,
            "created_at": b.created_at.isoformat(),
        }
        for b in res.scalars().all()
    ]


@router.post("/refund/{booking_id}")
async def issue_refund(
    booking_id: UUID,
    payload: RefundRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = res.scalar_one_or_none()
    if not b or not b.razorpay_payment_id:
        raise HTTPException(status_code=404, detail="Booking not paid")

    # Authorization:
    # - consumers may refund only their own bookings
    # - staff/admin can refund any booking
    # Staff/admin OR the consumer who owns the booking can refund.
    is_staff = current.role == UserRole.admin
    if is_staff:
        pass
    else:
        # Ensure current user is allowed to refund only their own booking.
        # For consumers, bookings.consumer_id is consumer_profiles.id (not users.id).
        # Compare against the consumer profile id.
        consumer_profile = await get_consumer_profile(current, db)
        if b.consumer_id != consumer_profile.id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_owner",
                    "booking_consumer_id": str(b.consumer_id),
                    "consumer_profile_id": str(consumer_profile.id),
                    "current_id": str(current.id),
                },
            )
        # A consumer-initiated refund IS the "cancel booking" action (the UI
        # button is literally "Cancel booking & request refund"), so the same
        # cancellation policy applies: not allowed inside the 6-hour window
        # before the scheduled visit. Admin refunds stay exempt for support.
        from app.api.v1.bookings import _CANCELLATION_CUTOFF_HOURS, _scheduled_start_utc
        if b.status not in (BookingStatus.completed, BookingStatus.cancelled):
            now = datetime.now(timezone.utc)
            if now > _scheduled_start_utc(b) - timedelta(hours=_CANCELLATION_CUTOFF_HOURS):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "success": False,
                        "code": "CANCELLATION_WINDOW_CLOSED",
                        "message": (
                            f"Cancellations are only allowed up to {_CANCELLATION_CUTOFF_HOURS} hours "
                            "before the scheduled visit. Please contact support for help."
                        ),
                    },
                )

    if b.payment_status not in {PaymentStatus.captured, PaymentStatus.partially_refunded}:
        raise HTTPException(status_code=400, detail="Booking is not in a refundable payment state")

    amount = payload.amount
    reason = payload.reason

    refund = await razorpay_client.create_refund(b.razorpay_payment_id, int(amount * 100))
    entry_type = LedgerEntryType.refund_full if Decimal(str(amount)) >= b.total_amount else LedgerEntryType.refund_partial
    b.payment_status = PaymentStatus.refunded if entry_type == LedgerEntryType.refund_full else PaymentStatus.partially_refunded
    await post_ledger_entry(
        db,
        entry_type,
        Decimal(str(amount)),
        booking_id=b.id,
        consumer_id=b.consumer_id,
        debit_account="platform_revenue",
        credit_account="consumer_refund",
        razorpay_refund_id=refund.get("id"),
        description=reason,
        created_by=current.id,
        is_system_entry=False,
    )
    # A consumer refund cancels the booking itself — previously only the
    # money moved and the booking stayed live, so a nurse could still be
    # dispatched to (or show up for) a visit the customer had "cancelled".
    if not is_staff and b.status not in (BookingStatus.completed, BookingStatus.cancelled):
        b.status = BookingStatus.cancelled
        b.cancelled_by = current.id
        b.cancelled_at = datetime.now(timezone.utc)
        b.cancellation_reason = reason or "Consumer cancelled with refund"
    await audit(db, current.id, current.role.value, "payment.refund", "booking", b.id, {"amount": amount, "reason": reason})
    await db.commit()
    return {"refund_id": refund.get("id"), "status": refund.get("status"), "amount": amount}

# ===========================================================================
# RazorpayX payout webhook
#
# The authoritative confirmation that money actually moved. A payout is only
# ever marked `paid` from a terminal Razorpay status — either here, or by the
# status poll in payout_service.sync_payout_status when a webhook is missed.
# ===========================================================================
@router.post("/webhook/razorpay-payout")
async def razorpay_payout_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    if not razorpay_client.verify_payout_webhook_signature(body, x_razorpay_signature or ""):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(body.decode() or "{}")
    event = payload.get("event", "")
    if not event.startswith("payout."):
        return {"received": True, "ignored": True}

    entity = payload.get("payload", {}).get("payout", {}).get("entity", {}) or {}
    razorpay_payout_id = entity.get("id")
    if not razorpay_payout_id:
        return {"received": True, "ignored": True}

    res = await db.execute(
        select(WorkerPayout).where(WorkerPayout.razorpay_payout_id == razorpay_payout_id)
    )
    payout = res.scalar_one_or_none()
    if payout is None:
        # Unknown payout id — acknowledge so Razorpay stops retrying, but log
        # it: this means a transfer exists that we have no row for.
        logger.warning("payout webhook for unknown payout id %s", razorpay_payout_id)
        return {"received": True, "unmatched": True}

    if payout.status == WorkerPayoutStatus.paid:
        return {"received": True, "duplicate": True}

    from app.services.payout_service import _apply_razorpay_status

    _apply_razorpay_status(payout, entity)
    await db.commit()

    # Refresh the nurse's statement so it picks up the confirmed UTR.
    if payout.status == WorkerPayoutStatus.paid:
        try:
            from app.services.billing_service import (
                generate_payout_statement,
                notify_payout_released,
            )

            statement = await generate_payout_statement(db, payout)
            await db.commit()
            await notify_payout_released(db, payout, statement)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("post-payout statement refresh failed for %s", payout.id)

    return {"received": True, "status": payout.status.value}


# ===========================================================================
# Document access
# ===========================================================================
@router.get("/bookings/{booking_id}/invoice")
async def get_booking_invoice(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """The customer's own tax invoice.

    Returns the customer view only. `pricing_snapshot` — which holds the
    internal 80/20 split — is deliberately never serialised here; the
    commission split is not something a patient may see.
    """
    bres = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    ires = await db.execute(select(Invoice).where(Invoice.booking_id == booking_id))
    invoice = ires.scalar_one_or_none()

    if invoice is None:
        if booking.payment_status != PaymentStatus.captured:
            raise HTTPException(
                status_code=404,
                detail="Invoice is generated once payment is completed.",
            )
        # Payment captured but the invoice never landed (e.g. a transient
        # failure during the webhook). Generate it on demand rather than
        # leaving the customer without a receipt.
        from app.services.billing_service import generate_customer_invoice

        invoice = await generate_customer_invoice(db, booking)
        await db.commit()
        if invoice is None:
            raise HTTPException(status_code=500, detail="Could not generate invoice")

    if not invoice.pdf_url:
        from app.services.billing_service import attach_invoice_pdf

        await attach_invoice_pdf(db, invoice, booking)
        await db.commit()

    return {
        "invoice_number": invoice.invoice_number,
        "booking_ref": booking.booking_ref,
        "invoice_date": invoice.generated_at.isoformat() if invoice.generated_at else None,
        "place_of_supply": invoice.place_of_supply,
        "line_items": invoice.line_items,
        "taxable_value": float(invoice.taxable_value or 0),
        "exempt_value": float(invoice.exempt_value or 0),
        "cgst_amount": float(invoice.cgst_amount or 0),
        "sgst_amount": float(invoice.sgst_amount or 0),
        "total_gst": float(invoice.tax_amount or 0),
        "total_amount": float(invoice.total_amount),
        "pdf_url": invoice.pdf_url,
    }


@router.get("/worker/payout-statements")
async def worker_payout_statements(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The nurse's own payout advices."""
    from app.models.models import PayoutStatement

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    worker = wres.scalar_one_or_none()
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker profile not found")

    rows = (
        await db.execute(
            select(PayoutStatement, WorkerPayout, Booking)
            .join(WorkerPayout, WorkerPayout.id == PayoutStatement.payout_id)
            .join(Booking, Booking.id == PayoutStatement.booking_id)
            .where(PayoutStatement.worker_id == worker.id)
            .order_by(PayoutStatement.generated_at.desc())
        )
    ).all()

    return [
        {
            "statement_number": st.statement_number,
            "booking_ref": bk.booking_ref,
            "generated_at": st.generated_at.isoformat() if st.generated_at else None,
            "gross_earned": float(st.gross_earned),
            "platform_fee": float(st.platform_fee or 0),
            "platform_fee_gst": float(st.platform_fee_gst or 0),
            "net_take_home": float(st.net_take_home),
            "total_deductions": float(st.total_deductions or 0),
            "final_disbursal": float(st.final_disbursal),
            "line_items": st.line_items,
            # Reflects Razorpay's confirmation, not our intent to pay.
            "payout_status": po.status.value,
            "utr": po.razorpay_utr,
            "paid_at": po.paid_at.isoformat() if po.paid_at else None,
            "pdf_url": st.pdf_url,
        }
        for st, po, bk in rows
    ]


# ===========================================================================
# Cash on delivery
#
# Thin HTTP layer only — all state transitions live in
# app/services/cash_payment.py so the rules stay in one place and can be
# reused by admin tooling and the provider app without duplication.
# ===========================================================================
class CashSelectRequest(BaseModel):
    booking_id: UUID


class CashCollectRequest(BaseModel):
    booking_id: UUID
    amount: Optional[float] = None  # defaults to the booking total


@router.get("/methods/{booking_id}")
async def available_payment_methods(
    booking_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Which payment methods this booking may use, and why not if not.

    Driven by the booking rather than hardcoded in the apps, so adding or
    restricting a method is a backend change and every client follows.
    """
    res = await db.execute(
        select(Booking).where(Booking.id == booking_id, Booking.consumer_id == profile.id)
    )
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    from app.services.cash_payment import is_cash_eligible

    cash_ok, cash_reason = is_cash_eligible(booking)
    online_ok = booking.payment_status not in (PaymentStatus.captured, PaymentStatus.cash_due)

    return {
        "booking_id": str(booking.id),
        "amount": float(booking.total_amount),
        "current_method": booking.payment_method.value,
        "methods": [
            {
                "method": PaymentMethod.razorpay.value,
                "label": "Pay online",
                "description": "UPI, card, net banking or wallet.",
                "available": online_ok,
                "reason": None if online_ok else "This booking is already settled.",
            },
            {
                "method": PaymentMethod.cash.value,
                "label": "Pay cash at the visit",
                "description": "Hand the amount to your care professional when they arrive.",
                "available": cash_ok,
                "reason": cash_reason,
            },
        ],
    }


@router.post("/cash/select")
async def choose_cash_payment(
    payload: CashSelectRequest,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    """Customer opts to pay cash. Confirms the booking and starts dispatch.

    Runs the same post-confirmation work as a successful online payment
    (dispatch-notify + invoice) through the shared isolated-session runner,
    so a cash booking is dispatched and invoiced exactly like an online one.
    """
    res = await db.execute(
        select(Booking).where(
            Booking.id == payload.booking_id, Booking.consumer_id == profile.id
        )
    )
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    from app.services.cash_payment import CashPaymentError, select_cash_payment

    try:
        await select_cash_payment(db, booking)
        await audit(
            db, profile.user_id, "consumer", "payment.cash_selected", "booking", booking.id
        )
        await db.commit()
    except CashPaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": e.code, "message": e.message}) from None

    state = _payment_state(booking)
    # Dispatch now so a nurse starts being found immediately, but hold the
    # invoice until the money is actually collected at the visit.
    await _run_post_payment_side_effects(booking.id, issue_invoice=False)
    return state


@router.post("/cash/collect")
async def collect_cash(
    payload: CashCollectRequest,
    current: CurrentUser = Depends(require_roles(UserRole.worker)),
    db: AsyncSession = Depends(get_db),
):
    """Provider records that they took the cash at the visit.

    This is the revenue event for a cash booking — the ledger entry is
    posted here, not when the customer chose cash.
    """
    from app.models.models import WorkerProfile

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    worker = wres.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=403, detail="Worker profile required")

    res = await db.execute(select(Booking).where(Booking.id == payload.booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    # Only the assigned provider may collect against this booking.
    if booking.worker_id != worker.id:
        raise HTTPException(status_code=403, detail="This booking is not assigned to you")

    from app.services.cash_payment import CashPaymentError, record_cash_collection

    try:
        await record_cash_collection(
            db,
            booking,
            worker_id=worker.id,
            amount=Decimal(str(payload.amount)) if payload.amount is not None else None,
        )
        await audit(db, current.id, "worker", "payment.cash_collected", "booking", booking.id)
        await db.commit()
    except CashPaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": e.code, "message": e.message}) from None

    state = _payment_state(booking)
    # Cash bookings are invoiced at collection — that is when money moved.
    await _run_post_payment_side_effects(booking.id)
    return state


@router.post("/cash/remit/{booking_id}")
async def remit_cash(
    booking_id: UUID,
    current: CurrentUser = Depends(require_roles(UserRole.admin, UserRole.operations)),
    db: AsyncSession = Depends(get_db),
):
    """Finance confirms collected cash reached the company account."""
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    from app.services.cash_payment import CashPaymentError, record_cash_remittance

    try:
        await record_cash_remittance(db, booking)
        await audit(db, current.id, "admin", "payment.cash_remitted", "booking", booking.id)
        await db.commit()
    except CashPaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": e.code, "message": e.message}) from None

    return {**_payment_state(booking), "remitted": True}


@router.get("/cash/outstanding/all")
async def list_outstanding_cash(
    current: CurrentUser = Depends(require_roles(UserRole.admin, UserRole.operations)),
    db: AsyncSession = Depends(get_db),
):
    """Every booking with cash collected but not yet remitted, for the
    finance / admin remittance queue.

    This is the list an admin needs before they can call
    POST /payments/cash/remit/{booking_id} on anything — without it there
    was a working remit action with no way to discover what needed
    remitting. Grouped implicitly by worker_id in the response so the UI
    can show "this provider owes ₹X across N bookings" without a second
    round trip.
    """
    res = await db.execute(
        select(Booking)
        .where(
            Booking.payment_method == PaymentMethod.cash,
            Booking.cash_collected_at.isnot(None),
            Booking.cash_remitted_at.is_(None),
        )
        .order_by(Booking.cash_collected_at.asc())
    )
    bookings = list(res.scalars().all())
    if not bookings:
        return {"total_outstanding": 0.0, "bookings": []}

    worker_ids = {b.cash_collected_by for b in bookings if b.cash_collected_by}
    names: dict = {}
    if worker_ids:
        wres = await db.execute(
            select(WorkerProfile, User)
            .join(User, User.id == WorkerProfile.user_id)
            .where(WorkerProfile.id.in_(worker_ids))
        )
        for wp, u in wres.all():
            names[wp.id] = u.full_name or u.email

    rows = [
        {
            "booking_id": str(b.id),
            "booking_ref": b.booking_ref,
            "worker_id": str(b.cash_collected_by) if b.cash_collected_by else None,
            "worker_name": names.get(b.cash_collected_by),
            "amount": float(b.cash_collected_amount or 0),
            "collected_at": b.cash_collected_at.isoformat() if b.cash_collected_at else None,
        }
        for b in bookings
    ]
    return {
        "total_outstanding": sum(r["amount"] for r in rows),
        "bookings": rows,
    }


@router.get("/cash/outstanding")
async def my_outstanding_cash(
    current: CurrentUser = Depends(require_roles(UserRole.worker)),
    db: AsyncSession = Depends(get_db),
):
    """Cash this provider is holding that has not yet been remitted."""
    from app.models.models import WorkerProfile
    from app.services.cash_payment import outstanding_cash_for_worker

    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    worker = wres.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=403, detail="Worker profile required")

    amount = await outstanding_cash_for_worker(db, worker.id)
    return {"worker_id": str(worker.id), "outstanding_cash": float(amount)}
