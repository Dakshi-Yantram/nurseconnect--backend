"""Payments: Razorpay order creation, signature verification, webhook, history, refunds."""
import json
import logging
from decimal import Decimal
from typing import List
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
    WorkerPayout,
    WorkerProfile,
)
from app.schemas.schemas import (
    PaymentOrderRequest,
    PaymentOrderResponse,
    PaymentVerifyRequest,
)
from app.services.common_services import audit, post_ledger_entry

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
        return {
            "verified": True,
            "booking_status": booking.status.value,
            "payment_status": booking.payment_status.value,
            "idempotent_replay": True,
        }
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
        booking.status = BookingStatus.confirmed
        await db.commit()
        return {
            "verified": True,
            "booking_status": booking.status.value,
            "payment_status": booking.payment_status.value,
            "idempotent_replay": True,
        }

    booking.razorpay_payment_id = payload.razorpay_payment_id
    booking.payment_status = PaymentStatus.captured
    booking.status = BookingStatus.confirmed
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
        return {
            "verified": True,
            "booking_status": b2.status.value,
            "payment_status": b2.payment_status.value,
            "idempotent_replay": True,
        }
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
        return {
            "verified": True,
            "booking_status": b2.status.value,
            "payment_status": b2.payment_status.value,
            "idempotent_replay": True,
        }
    # Booking is now CONFIRMED — push the request to nearby, qualified,
    # free, online workers (best-effort; must not fail the payment).
    try:
        from app.services.dispatch import notify_nearby_workers
        await notify_nearby_workers(db, booking)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
    return {"verified": True, "booking_status": booking.status.value, "payment_status": booking.payment_status.value}


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
            b.status = BookingStatus.confirmed
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
                try:
                    from app.services.dispatch import notify_nearby_workers
                    await notify_nearby_workers(db, b)
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
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
    await audit(db, current.id, current.role.value, "payment.refund", "booking", b.id, {"amount": amount, "reason": reason})
    await db.commit()
    return {"refund_id": refund.get("id"), "status": refund.get("status"), "amount": amount}