"""Inbound WhatsApp webhook (Interakt).

Interakt calls this URL for two kinds of events on the same endpoint:

* message status updates ("sent" / "delivered" / "read" / "failed") for
  WhatsApp messages we sent out — e.g. the post-visit feedback request fired
  from POST /api/visits/{booking_id}/checkout — so we can keep NotificationLog
  in sync with what actually happened on the family's phone.
* inbound messages — the family replying to that feedback prompt.

Configure this URL (``https://<your-domain>/api/webhooks/whatsapp/interakt``)
in the Interakt dashboard under Settings > Webhooks, along with a shared
secret matching ``INTERAKT_WEBHOOK_SECRET`` so we can verify the call really
came from Interakt and not a spoofed request.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.enums import NotificationStatus
from app.models.models import NotificationLog

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp-webhook"])
logger = logging.getLogger(__name__)

# Interakt's own status strings, normalised to our NotificationStatus enum.
_STATUS_MAP: Dict[str, NotificationStatus] = {
    "sent": NotificationStatus.sent,
    "delivered": NotificationStatus.delivered,
    "read": NotificationStatus.read,
    "failed": NotificationStatus.failed,
    "undelivered": NotificationStatus.failed,
}

# Ranks so a late-arriving "sent" callback can never downgrade a message we
# already know was "read" — webhook delivery order isn't guaranteed.
_STATUS_RANK: Dict[NotificationStatus, int] = {
    NotificationStatus.queued: 0,
    NotificationStatus.sent: 1,
    NotificationStatus.failed: 1,
    NotificationStatus.delivered: 2,
    NotificationStatus.read: 3,
}


def _extract_message_id(payload: Dict[str, Any]) -> Optional[str]:
    return (
        payload.get("message_id")
        or payload.get("messageId")
        or (payload.get("data") or {}).get("message_id")
    )


def _extract_status(payload: Dict[str, Any]) -> Optional[str]:
    status = payload.get("status") or (payload.get("data") or {}).get("status")
    return status.lower() if isinstance(status, str) else None


@router.post("/interakt")
async def interakt_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
) -> Dict[str, Any]:
    # Verify the call actually came from Interakt. Skipped only if no secret
    # has been configured yet (dev / not-yet-onboarded environments).
    if settings.INTERAKT_WEBHOOK_SECRET and x_webhook_secret != settings.INTERAKT_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type = (payload.get("type") or payload.get("event") or "").lower()

    # --- Delivery / read status callback for a message we sent ------------
    message_id = _extract_message_id(payload)
    status_raw = _extract_status(payload)
    if message_id and status_raw:
        new_status = _STATUS_MAP.get(status_raw)
        if new_status:
            res = await db.execute(
                select(NotificationLog).where(NotificationLog.provider_message_id == message_id)
            )
            log = res.scalar_one_or_none()
            if log and _STATUS_RANK.get(new_status, 0) >= _STATUS_RANK.get(log.status, 0):
                log.status = new_status
                await db.commit()
        return {"received": True}

    # --- Inbound message (family replying to the feedback prompt) ---------
    if event_type in ("message_received", "inbound_message") or payload.get("message"):
        customer = payload.get("customer") or {}
        phone = customer.get("phone_number") or payload.get("phoneNumber")
        message = payload.get("message") or {}
        text = message.get("text") if isinstance(message, dict) else payload.get("text")
        # Logged for ops visibility for now. A future iteration can match the
        # phone number to a booking/consumer and route this into the support
        # / review-ticket flow automatically.
        logger.info("WhatsApp inbound reply phone=%s text=%s", phone, (text or "")[:200])
        return {"received": True}

    logger.info("Unhandled Interakt webhook payload: %s", payload)
    return {"received": True}
