"""Notification + audit + ledger services."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import (
    firebase_push_client,
    interakt_client,
    msg91_client,
)
from app.models.enums import (
    LedgerEntryType,
    NotificationChannel,
    NotificationStatus,
)
from app.models.models import (
    AuditLog,
    FinancialLedger,
    NotificationLog,
    User,
    UserSession,
)


# ============================================================================
# Notification
# ============================================================================
async def send_notification(
    db: AsyncSession,
    recipient_id: UUID,
    template_code: str,
    title: str,
    body: str,
    payload: Optional[Dict[str, Any]] = None,
    channels: Optional[List[NotificationChannel]] = None,
) -> List[NotificationLog]:
    """Persist + dispatch a notification across one or more channels."""
    channels = channels or [NotificationChannel.in_app, NotificationChannel.push]
    logs: List[NotificationLog] = []
    res = await db.execute(select(User).where(User.id == recipient_id))
    user = res.scalar_one_or_none()
    if not user:
        return []

    for ch in channels:
        log = NotificationLog(
            recipient_id=recipient_id,
            channel=ch,
            template_code=template_code,
            title=title,
            body=body,
            payload=payload or {},
            status=NotificationStatus.queued,
        )
        db.add(log)
        await db.flush()
        logs.append(log)

        if ch == NotificationChannel.push:
            sess = await db.execute(
                select(UserSession.fcm_token).where(UserSession.user_id == recipient_id, UserSession.revoked.is_(False)).order_by(UserSession.created_at.desc()).limit(1)
            )
            token = sess.scalar_one_or_none()
            if token:
                resp = await firebase_push_client.send_to_token(token, title, body, {k: str(v) for k, v in (payload or {}).items()})
                log.status = NotificationStatus.sent if resp.get("success") else NotificationStatus.failed
                log.provider_message_id = resp.get("message_id")
            else:
                log.status = NotificationStatus.failed
        elif ch == NotificationChannel.sms:
            resp = await msg91_client.send_sms(user.phone_e164, body)
            log.status = NotificationStatus.sent if resp.get("type") == "success" else NotificationStatus.failed
            log.provider_message_id = resp.get("request_id")
        elif ch == NotificationChannel.whatsapp:
            resp = await interakt_client.send_message(user.phone_e164, template_code, {"name": user.full_name or "", "body": body})
            log.status = NotificationStatus.sent if resp.get("result") else NotificationStatus.failed
            log.provider_message_id = resp.get("message_id")
        elif ch == NotificationChannel.in_app:
            log.status = NotificationStatus.sent

    return logs


async def notify_parties(
    db: AsyncSession,
    parties: List[str],
    context: Dict[str, Any],
    template_code: str,
    title: str,
    body: str,
) -> None:
    """Resolve abstract parties (worker | family | ops | doctor | emergency_desk) to users."""
    from app.models.models import Booking, ConsumerProfile, WorkerProfile
    from app.models.enums import UserRole

    booking_id = context.get("booking_id")
    if not booking_id:
        return
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking:
        return

    recipient_ids: List[UUID] = []
    if "worker" in parties and booking.worker_id:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == booking.worker_id))
        wp = wres.scalar_one_or_none()
        if wp:
            recipient_ids.append(wp.user_id)
    if "family" in parties or "consumer" in parties:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.id == booking.consumer_id))
        cp = cres.scalar_one_or_none()
        if cp:
            recipient_ids.append(cp.user_id)
    if "ops" in parties or "admin" in parties or "doctor" in parties or "emergency_desk" in parties:
        admin_role = UserRole.admin
        ares = await db.execute(select(User).where(User.role == admin_role).limit(5))
        for u in ares.scalars().all():
            recipient_ids.append(u.id)

    for rid in set(recipient_ids):
        await send_notification(db, rid, template_code, title, body, context)


# ============================================================================
# Audit log
# ============================================================================
async def audit(
    db: AsyncSession,
    actor_id: Optional[UUID],
    actor_type: str,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    changes: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLog:
    log = AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        changes=changes,
        ip_address=ip_address,
        request_id=request_id,
    )
    db.add(log)
    await db.flush()
    return log


# ============================================================================
# Ledger
# ============================================================================
async def post_ledger_entry(
    db: AsyncSession,
    entry_type: LedgerEntryType,
    amount: Decimal,
    *,
    booking_id: Optional[UUID] = None,
    package_booking_id: Optional[UUID] = None,
    consumer_id: Optional[UUID] = None,
    worker_id: Optional[UUID] = None,
    debit_account: Optional[str] = None,
    credit_account: Optional[str] = None,
    razorpay_payment_id: Optional[str] = None,
    razorpay_payout_id: Optional[str] = None,
    razorpay_refund_id: Optional[str] = None,
    reference_ledger_id: Optional[UUID] = None,
    description: Optional[str] = None,
    created_by: Optional[UUID] = None,
    is_system_entry: bool = True,
) -> FinancialLedger:
    entry = FinancialLedger(
        entry_type=entry_type,
        amount=amount,
        booking_id=booking_id,
        package_booking_id=package_booking_id,
        consumer_id=consumer_id,
        worker_id=worker_id,
        debit_account=debit_account,
        credit_account=credit_account,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_payout_id=razorpay_payout_id,
        razorpay_refund_id=razorpay_refund_id,
        reference_ledger_id=reference_ledger_id,
        description=description,
        created_by=created_by,
        is_system_entry=is_system_entry,
    )
    db.add(entry)
    await db.flush()
    return entry