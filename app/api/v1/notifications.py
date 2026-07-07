"""Notifications: list, mark read."""
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.models.enums import NotificationStatus
from app.models.models import NotificationLog
from app.schemas.schemas import NotificationOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/", response_model=List[NotificationOut])
async def list_my_notifications(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(NotificationLog).where(NotificationLog.recipient_id == current.id).order_by(NotificationLog.created_at.desc()).limit(100)
    )
    return [NotificationOut.model_validate(n) for n in res.scalars().all()]


@router.post("/{notification_id}/read")
async def mark_read(notification_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(NotificationLog).where(NotificationLog.id == notification_id, NotificationLog.recipient_id == current.id))
    n = res.scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="Not found")
    n.read_at = datetime.now(timezone.utc)
    n.status = NotificationStatus.read
    await db.commit()
    return {"read": True}


@router.post("/mark-all-read")
async def mark_all_read(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    await db.execute(
        update(NotificationLog)
        .where(NotificationLog.recipient_id == current.id, NotificationLog.read_at.is_(None))
        .values(read_at=now, status=NotificationStatus.read)
    )
    await db.commit()
    return {"marked": True}
