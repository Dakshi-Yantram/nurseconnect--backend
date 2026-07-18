"""In-app chat between a consumer and their assigned nurse/caregiver.

Two conversation scopes:
  - /messages/booking/{booking_id}          — single one-off visit
  - /messages/package/{package_booking_id}  — a whole care package (same
    worker/consumer thread persists across every visit in the package)

Sending is blocked once the booking/package reaches a terminal status —
checked live against the real Booking/CarePackageBooking row on every
request, never a cached "conversation closed" flag.
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, is_admin
from app.models.enums import BookingStatus, PackageBookingStatus, UserRole
from app.models.models import Booking, CarePackageBooking, ConsumerProfile, Message, User, WorkerProfile

router = APIRouter(prefix="/messages", tags=["messaging"])

_BOOKING_TERMINAL = (BookingStatus.completed, BookingStatus.cancelled, BookingStatus.missed)
_PACKAGE_TERMINAL = (PackageBookingStatus.completed, PackageBookingStatus.cancelled)


class MessageSendRequest(BaseModel):
    body: str


def _serialize_message(m: Message, sender_name: str) -> dict:
    return {
        "id": str(m.id),
        "sender_id": str(m.sender_id),
        "sender_name": sender_name,
        "sender_role": m.sender_role,
        "body": m.body,
        "is_read": m.is_read,
        "created_at": m.created_at.isoformat(),
    }


async def _load_booking_thread(booking_id: UUID, current: CurrentUser, db: AsyncSession):
    res = await db.execute(
        select(Booking, ConsumerProfile, WorkerProfile)
        .join(ConsumerProfile, ConsumerProfile.id == Booking.consumer_id)
        .outerjoin(WorkerProfile, WorkerProfile.id == Booking.worker_id)
        .where(Booking.id == booking_id)
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking, consumer_profile, worker_profile = row
    is_participant = (
        consumer_profile.user_id == current.id
        or (worker_profile is not None and worker_profile.user_id == current.id)
    )
    if not is_participant and not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Not a participant in this booking")
    if worker_profile is None:
        raise HTTPException(status_code=409, detail="No nurse assigned to this booking yet")
    can_send = booking.status not in _BOOKING_TERMINAL
    disabled_reason = None if can_send else f"This visit is {booking.status.value} — messaging is closed."
    return booking, can_send, disabled_reason


async def _load_package_thread(package_booking_id: UUID, current: CurrentUser, db: AsyncSession):
    res = await db.execute(
        select(CarePackageBooking, ConsumerProfile, WorkerProfile)
        .join(ConsumerProfile, ConsumerProfile.id == CarePackageBooking.consumer_id)
        .outerjoin(WorkerProfile, WorkerProfile.id == CarePackageBooking.worker_id)
        .where(CarePackageBooking.id == package_booking_id)
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Care package booking not found")
    pkg, consumer_profile, worker_profile = row
    is_participant = (
        consumer_profile.user_id == current.id
        or (worker_profile is not None and worker_profile.user_id == current.id)
    )
    if not is_participant and not is_admin(current.role):
        raise HTTPException(status_code=403, detail="Not a participant in this care package")
    if worker_profile is None:
        raise HTTPException(status_code=409, detail="No nurse assigned to this package yet")
    can_send = pkg.status not in _PACKAGE_TERMINAL
    disabled_reason = None if can_send else f"This care package is {pkg.status.value} — messaging is closed."
    return pkg, can_send, disabled_reason


async def _names_for(sender_ids: set[UUID], db: AsyncSession) -> dict:
    if not sender_ids:
        return {}
    res = await db.execute(select(User).where(User.id.in_(sender_ids)))
    return {u.id: (u.full_name or u.email or str(u.id)) for u in res.scalars().all()}


@router.get("/booking/{booking_id}")
async def get_booking_thread(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, can_send, disabled_reason = await _load_booking_thread(booking_id, current, db)
    rows = (await db.execute(
        select(Message).where(Message.booking_id == booking_id).order_by(Message.created_at.asc())
    )).scalars().all()
    names = await _names_for({m.sender_id for m in rows}, db)
    return {
        "can_send": can_send,
        "disabled_reason": disabled_reason,
        "messages": [_serialize_message(m, names.get(m.sender_id, "Unknown")) for m in rows],
    }


@router.post("/booking/{booking_id}")
async def send_booking_message(
    booking_id: UUID,
    payload: MessageSendRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    _, can_send, disabled_reason = await _load_booking_thread(booking_id, current, db)
    if not can_send:
        raise HTTPException(status_code=409, detail=disabled_reason)
    msg = Message(booking_id=booking_id, sender_id=current.id, sender_role=current.role.value, body=payload.body.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return _serialize_message(msg, current.user.full_name or current.user.email or "You")


@router.get("/package/{package_booking_id}")
async def get_package_thread(
    package_booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, can_send, disabled_reason = await _load_package_thread(package_booking_id, current, db)
    rows = (await db.execute(
        select(Message).where(Message.package_booking_id == package_booking_id).order_by(Message.created_at.asc())
    )).scalars().all()
    names = await _names_for({m.sender_id for m in rows}, db)
    return {
        "can_send": can_send,
        "disabled_reason": disabled_reason,
        "messages": [_serialize_message(m, names.get(m.sender_id, "Unknown")) for m in rows],
    }


@router.post("/package/{package_booking_id}")
async def send_package_message(
    package_booking_id: UUID,
    payload: MessageSendRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    _, can_send, disabled_reason = await _load_package_thread(package_booking_id, current, db)
    if not can_send:
        raise HTTPException(status_code=409, detail=disabled_reason)
    msg = Message(package_booking_id=package_booking_id, sender_id=current.id, sender_role=current.role.value, body=payload.body.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return _serialize_message(msg, current.user.full_name or current.user.email or "You")
