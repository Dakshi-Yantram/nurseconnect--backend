"""Notifications: list, mark read.

DUPLICATE-NOTIFICATION FIX
--------------------------
``send_notification`` (app/services/common_services.py) persists ONE
``NotificationLog`` row *per delivery channel*, and its default channel set
is ``[in_app, push]``. Those two rows are the same logical event -- one is
the inbox entry, the other is the delivery receipt for the push message.
This endpoint used to return every row, so the notification centre showed
every event twice ("Nurse Confirmed" / "Nurse Confirmed", "Visit report is
ready" / "Visit report is ready"), and marking one read left its twin
unread -- which is exactly the read/unread pairing seen in the reported
screenshot.

We can't simply filter to ``channel == in_app``: some events are dispatched
over a single non-in-app channel on purpose (e.g. the post-visit WhatsApp
feedback request sent from ``visits.checkout``), and those must still appear
in the inbox. So we collapse by *logical event* instead, preferring the
in-app row and falling back to whatever channel did carry the event.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.models.enums import NotificationChannel, NotificationStatus
from app.models.models import NotificationLog
from app.schemas.schemas import NotificationOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


# Channel preference when the same logical event was fanned out over several
# channels. The in-app row is the canonical inbox entry.
_CHANNEL_RANK = {
    NotificationChannel.in_app: 0,
    NotificationChannel.push: 1,
    NotificationChannel.whatsapp: 2,
    NotificationChannel.sms: 3,
}


def _event_key(n: NotificationLog) -> tuple:
    """Identity of the logical event behind a NotificationLog row.

    Rows created by one ``send_notification`` call share recipient, template,
    title, body and payload, and are written inside the same transaction, so
    truncating the timestamp to the minute is enough to group them without
    merging two genuinely separate events (the same template firing twice in
    the same minute for the same booking *is* a duplicate).
    """
    payload = n.payload or {}
    return (
        n.template_code,
        n.title,
        n.body,
        str(payload.get("booking_id") or ""),
        str(payload.get("visit_id") or ""),
        n.created_at.replace(second=0, microsecond=0) if n.created_at else None,
    )


def _route_for(n: NotificationLog) -> Optional[str]:
    """In-app destination for a notification, so tapping it goes somewhere.

    Returns an expo-router path. ``None`` means "no specific destination" and
    the client leaves the row inert rather than guessing.
    """
    payload: Dict[str, Any] = n.payload or {}
    code = (n.template_code or "").lower()
    booking_id = payload.get("booking_id")
    ticket_id = payload.get("ticket_id")

    # An explicit route on the payload always wins -- it lets a caller aim a
    # notification at a screen this mapping doesn't know about.
    explicit = payload.get("route")
    if isinstance(explicit, str) and explicit.startswith("/"):
        return explicit

    if booking_id:
        if "feedback" in code or "rating" in code:
            return f"/visit/rate/{booking_id}"
        # report ready / invoice / receipt / payout / accepted / en-route /
        # arrived / cancelled all resolve to the booking's visit screen.
        return f"/visit/{booking_id}"
    if ticket_id:
        return f"/support/ticket/{ticket_id}"
    if "contract" in code or "agreement" in code:
        return "/(nurse)/contract"
    if "onboarding" in code or "verification" in code or "approved" in code:
        return "/onboarding-status"
    return None


def _serialize(n: NotificationLog) -> NotificationOut:
    out = NotificationOut.model_validate(n)
    out.route = _route_for(n)
    return out


@router.get("/", response_model=List[NotificationOut])
async def list_my_notifications(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(NotificationLog)
        .where(NotificationLog.recipient_id == current.id)
        .order_by(NotificationLog.created_at.desc())
        # Over-fetch: we collapse duplicates below, so the pre-dedupe limit
        # has to be higher than the page we intend to return.
        .limit(300)
    )
    rows = list(res.scalars().all())

    best: Dict[tuple, NotificationLog] = {}
    order: List[tuple] = []
    for n in rows:
        key = _event_key(n)
        if key not in best:
            best[key] = n
            order.append(key)
            continue
        incumbent = best[key]
        if _CHANNEL_RANK.get(n.channel, 9) < _CHANNEL_RANK.get(incumbent.channel, 9):
            # Carry over a read receipt from the row we're dropping so the
            # collapsed entry doesn't resurrect as unread.
            if incumbent.read_at and not n.read_at:
                n.read_at = incumbent.read_at
            best[key] = n

    return [_serialize(best[k]) for k in order[:100]]


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(NotificationLog).where(
            NotificationLog.id == notification_id,
            NotificationLog.recipient_id == current.id,
        )
    )
    n = res.scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="Not found")

    now = datetime.now(timezone.utc)
    n.read_at = now
    n.status = NotificationStatus.read

    # Mark the sibling rows for the same logical event read too. Without this
    # the collapsed inbox entry pops back to unread on the next refresh,
    # because the row returned last time may not be the one picked next time.
    sib_res = await db.execute(
        select(NotificationLog).where(
            NotificationLog.recipient_id == current.id,
            NotificationLog.template_code == n.template_code,
            NotificationLog.read_at.is_(None),
        )
    )
    key = _event_key(n)
    for sib in sib_res.scalars().all():
        if sib.id != n.id and _event_key(sib) == key:
            sib.read_at = now
            sib.status = NotificationStatus.read

    await db.commit()
    return {"read": True}


@router.post("/mark-all-read")
async def mark_all_read(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    await db.execute(
        update(NotificationLog)
        .where(NotificationLog.recipient_id == current.id, NotificationLog.read_at.is_(None))
        .values(read_at=now, status=NotificationStatus.read)
    )
    await db.commit()
    return {"marked": True}
