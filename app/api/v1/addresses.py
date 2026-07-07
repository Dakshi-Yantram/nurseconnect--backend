"""Consumer address book — save / list / edit / delete service addresses.

Works like Swiggy/Zepto: multiple saved addresses, one default, and the
ability to book for someone else (recipient_name/phone on the address).

Mount in app/main.py:
    from app.api.v1 import addresses
    ... app.include_router(addresses.router, prefix=_API_PREFIX)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_consumer_profile
from app.models.models import ConsumerAddress, ConsumerProfile

router = APIRouter(prefix="/consumers/me/addresses", tags=["addresses"])


class AddressIn(BaseModel):
    label: str = "Home"
    recipient_name: Optional[str] = None
    recipient_phone: Optional[str] = None
    line1: str
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    landmark: Optional[str] = None
    is_default: bool = False


def _serialize(a: ConsumerAddress) -> dict:
    return {
        "id": str(a.id),
        "label": a.label,
        "recipient_name": a.recipient_name,
        "recipient_phone": a.recipient_phone,
        "line1": a.line1,
        "line2": a.line2,
        "city": a.city,
        "state": a.state,
        "pincode": a.pincode,
        "latitude": float(a.latitude) if a.latitude is not None else None,
        "longitude": float(a.longitude) if a.longitude is not None else None,
        "landmark": a.landmark,
        "is_default": a.is_default,
    }


async def _clear_defaults(db: AsyncSession, consumer_id: UUID) -> None:
    await db.execute(
        update(ConsumerAddress)
        .where(ConsumerAddress.consumer_id == consumer_id, ConsumerAddress.is_default.is_(True))
        .values(is_default=False)
    )


@router.get("")
async def list_addresses(
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ConsumerAddress)
        .where(ConsumerAddress.consumer_id == profile.id)
        .order_by(ConsumerAddress.is_default.desc(), ConsumerAddress.created_at.desc())
    )
    return [_serialize(a) for a in res.scalars().all()]


@router.post("")
async def create_address(
    payload: AddressIn,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    # First address is automatically the default.
    count = (await db.execute(
        select(ConsumerAddress).where(ConsumerAddress.consumer_id == profile.id)
    )).scalars().first()
    make_default = payload.is_default or count is None

    if make_default:
        await _clear_defaults(db, profile.id)

    addr = ConsumerAddress(consumer_id=profile.id, **payload.model_dump(exclude={"is_default"}), is_default=make_default)
    db.add(addr)
    await db.flush()

    # Mirror the default onto the profile for back-compat with older code paths.
    if make_default:
        profile.address_line1 = addr.line1
        profile.address_line2 = addr.line2
        profile.city = addr.city
        profile.state = addr.state
        profile.pincode = addr.pincode
        profile.latitude = addr.latitude
        profile.longitude = addr.longitude

    await db.commit()
    return _serialize(addr)


@router.put("/{address_id}")
async def update_address(
    address_id: UUID,
    payload: AddressIn,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ConsumerAddress).where(
            ConsumerAddress.id == address_id, ConsumerAddress.consumer_id == profile.id
        )
    )
    addr = res.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    if payload.is_default:
        await _clear_defaults(db, profile.id)

    for field, value in payload.model_dump().items():
        setattr(addr, field, value)
    await db.commit()
    return _serialize(addr)


@router.post("/{address_id}/default")
async def set_default(
    address_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ConsumerAddress).where(
            ConsumerAddress.id == address_id, ConsumerAddress.consumer_id == profile.id
        )
    )
    addr = res.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")
    await _clear_defaults(db, profile.id)
    addr.is_default = True
    await db.commit()
    return _serialize(addr)


@router.delete("/{address_id}")
async def delete_address(
    address_id: UUID,
    profile: ConsumerProfile = Depends(get_consumer_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ConsumerAddress).where(
            ConsumerAddress.id == address_id, ConsumerAddress.consumer_id == profile.id
        )
    )
    addr = res.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")
    was_default = addr.is_default
    await db.delete(addr)
    await db.flush()
    # Promote another address to default if we removed the default one.
    if was_default:
        nxt = (await db.execute(
            select(ConsumerAddress).where(ConsumerAddress.consumer_id == profile.id).limit(1)
        )).scalar_one_or_none()
        if nxt:
            nxt.is_default = True
    await db.commit()
    return {"deleted": True}
