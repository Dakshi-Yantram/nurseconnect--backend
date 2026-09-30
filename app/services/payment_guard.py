"""Server-side gate for anything that takes or promises money for a booking.

ROOT CAUSE: ``POST /payments/order`` (and ``/payments/cash/select``) looked up
the booking and created a Razorpay order with no check of the booking's status,
its slot time, or whether the purchased catalogue item is still sellable.  The
only expiry protection was in the apps, so a stale screen, a replayed request
or a direct API call could take money for a slot that had already passed (or
for a cancelled booking, or a test-only package).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import is_booking_expired
from app.models.enums import BookingStatus
from app.models.models import Booking, CarePackage, ServiceCatalogue
from app.services import catalog_guard

# Only a booking that is still waiting for payment may be paid for.
PAYABLE_STATUSES = (BookingStatus.pending_payment, BookingStatus.draft)


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def assert_booking_payable(
    db: AsyncSession, booking: Booking, *, now: Optional[datetime] = None
) -> None:
    """Raise a stable-coded HTTPException unless the booking may take payment."""
    if booking.status not in PAYABLE_STATUSES:
        if booking.status == BookingStatus.cancelled:
            raise _err(409, "BOOKING_CANCELLED", "This booking was cancelled and can't be paid.")
        raise _err(409, "BOOKING_NOT_PAYABLE", "This booking isn't awaiting payment.")

    if is_booking_expired(booking, now=now):
        raise _err(
            409,
            "BOOKING_SLOT_EXPIRED",
            "This booking's time slot has already passed. Please book a new slot.",
        )

    # Re-validate the purchased item at payment time: it may have been
    # deactivated, deleted or flagged test-only since the booking was created.
    item = None
    fallback = ()
    if booking.package_id:
        r = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        item = r.scalar_one_or_none()
        if item is not None and item.primary_service_id:
            sr = await db.execute(
                select(ServiceCatalogue).where(ServiceCatalogue.id == item.primary_service_id)
            )
            fallback = (sr.scalar_one_or_none(),)
    elif booking.service_id:
        r = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        item = r.scalar_one_or_none()
    reason = catalog_guard.unbookable_reason(item, fallback_items=fallback)
    if reason:
        raise _err(409, reason, "This service is no longer available for booking.")
