"""Celery task definitions.

Implementation note: these tasks use sync SQLAlchemy via the sync URL because
Celery doesn't natively run async event loops. Each task opens a short-lived
session.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from celery.utils.log import get_task_logger
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import (
    BookingStatus,
    EscalationStatus,
    OfflineSyncStatus,
    WorkerPayoutStatus,
)
from app.models.models import (
    Booking,
    DataRetentionSchedule,
    Escalation,
    OfflineSyncQueue,
    WorkerPayout,
)
from app.workers.celery_app import celery_app

logger = get_task_logger(__name__)

_engine = create_engine(settings.DATABASE_URL_SYNC, pool_pre_ping=True)


def _session() -> Session:
    return Session(bind=_engine)


@celery_app.task
def escalation_sla_check() -> dict:
    now = datetime.now(timezone.utc)
    with _session() as s:
        breaches = s.execute(
            select(Escalation).where(
                Escalation.status != EscalationStatus.resolved,
                Escalation.sla_breach_at.is_not(None),
                Escalation.sla_breach_at < now,
            )
        ).scalars().all()
        for esc in breaches:
            logger.warning("SLA breach on escalation %s level=%s", esc.id, esc.level.value)
            # In a real impl, notify admin + emit ws event
        s.commit()
        return {"checked_at": now.isoformat(), "breached": len(breaches)}


@celery_app.task
def process_payout_batch() -> dict:
    """Mark eligible pending payouts as scheduled (real impl would call Razorpay payouts)."""
    with _session() as s:
        pending = s.execute(
            select(WorkerPayout).where(WorkerPayout.status == WorkerPayoutStatus.pending)
        ).scalars().all()
        for p in pending:
            p.status = WorkerPayoutStatus.processing
            p.scheduled_at = datetime.now(timezone.utc)
        s.commit()
        return {"processed": len(pending)}


@celery_app.task
def retention_cleanup() -> dict:
    """Honour configured data retention schedules."""
    today = date.today()
    with _session() as s:
        schedules = s.execute(
            select(DataRetentionSchedule).where(DataRetentionSchedule.is_active.is_(True))
        ).scalars().all()
        total = 0
        for sched in schedules:
            cutoff = today - timedelta(days=sched.retention_days)
            # left as no-op in dev — real impl would delete/archive per data_type
            sched.last_run_at = datetime.now(timezone.utc)
            sched.records_processed = sched.records_processed or 0
            total += 1
        s.commit()
        return {"schedules_run": total}


@celery_app.task
def detect_missed_visits() -> dict:
    """Mark scheduled bookings whose scheduled_start_time + grace is past as missed."""
    now = datetime.now(timezone.utc)
    grace_minutes = 30
    with _session() as s:
        scheduled = s.execute(
            select(Booking).where(Booking.status == BookingStatus.assigned)
        ).scalars().all()
        missed = 0
        for b in scheduled:
            start_dt = datetime.combine(b.scheduled_date, b.scheduled_start_time, tzinfo=timezone.utc)
            if start_dt + timedelta(minutes=grace_minutes) < now:
                b.status = BookingStatus.missed
                missed += 1
        s.commit()
        return {"missed": missed}


@celery_app.task
def process_offline_sync_item(queue_id: str) -> dict:
    """Mark a single queue item as synced (used as callback after server materializes record)."""
    with _session() as s:
        s.execute(
            update(OfflineSyncQueue)
            .where(OfflineSyncQueue.id == queue_id)
            .values(sync_status=OfflineSyncStatus.synced, synced_at=datetime.now(timezone.utc))
        )
        s.commit()
        return {"queue_id": queue_id, "status": "synced"}


def _maps_url_for_booking(b) -> str:
    """Google Maps deep-link the caregiver's app/phone can open for directions."""
    if b.latitude is not None and b.longitude is not None:
        return f"https://www.google.com/maps/dir/?api=1&destination={b.latitude},{b.longitude}"
    addr = b.address_snapshot if isinstance(b.address_snapshot, dict) else {}
    q = ", ".join(str(addr.get(k)) for k in ("line1", "city", "state", "pincode") if addr.get(k))
    from urllib.parse import quote
    return f"https://www.google.com/maps/search/?api=1&query={quote(q or 'destination')}"


@celery_app.task
def send_visit_reminders() -> dict:
    """Remind the assigned caregiver ~30 min before each upcoming visit.

    Runs every 5 minutes; picks bookings whose start falls in the (now+25, now+30]
    band so each booking is reminded about once. The notification payload carries
    a Google Maps directions link the caregiver app opens for navigation.
    """
    import asyncio
    return asyncio.run(_send_visit_reminders_async())


async def _send_visit_reminders_async() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.models.enums import BookingStatus as _BS
    from app.models.models import Booking as _B, User as _U, WorkerProfile as _WP
    from app.services.common_services import send_notification
    from sqlalchemy import select as _select

    sent = 0
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        lo, hi = now + timedelta(minutes=25), now + timedelta(minutes=30)
        res = await db.execute(
            _select(_B).where(
                _B.worker_id.isnot(None),
                _B.status.in_([_BS.assigned, _BS.worker_en_route, _BS.worker_arrived]),
            )
        )
        for b in res.scalars().all():
            start = datetime.combine(b.scheduled_date, b.scheduled_start_time, tzinfo=timezone.utc)
            if not (lo <= start < hi):
                continue
            wres = await db.execute(_select(_WP).where(_WP.id == b.worker_id))
            wp = wres.scalar_one_or_none()
            if not wp:
                continue
            ures = await db.execute(_select(_U).where(_U.id == wp.user_id))
            u = ures.scalar_one_or_none()
            if not u:
                continue
            await send_notification(
                db, u.id, "visit_reminder", "Upcoming visit",
                f"Your visit {b.booking_ref} starts at {b.scheduled_start_time.strftime('%H:%M')}. Tap for directions.",
                {"booking_id": str(b.id), "booking_ref": b.booking_ref, "maps_url": _maps_url_for_booking(b)},
            )
            sent += 1
        await db.commit()
    return {"reminders_sent": sent}
