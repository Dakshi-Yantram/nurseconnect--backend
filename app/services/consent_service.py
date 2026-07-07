"""Patch 5A — Consent enforcement helpers.

Centralises the "do we have an active consent of type X for this patient /
booking?" check. Backend-only enforcement — there is no consent UI here.

A consent is considered *active* iff:
    status == ConsentStatus.given
    AND (expires_at is NULL or expires_at > now)
    AND (revoked_at is NULL)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ConsentStatus, ConsentType
from app.models.models import ConsentRecord


class ConsentMissingError(Exception):
    """Raised when a required consent is absent or revoked.

    The API layer converts this into a 403 with a stable machine-readable
    ``code`` so the mobile / family apps can surface a contextual UX.
    """

    def __init__(self, consent_type: ConsentType, message: str, *, code: str = "CONSENT_MISSING"):
        super().__init__(message)
        self.consent_type = consent_type
        self.message = message
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def has_active_consent(
    db: AsyncSession,
    *,
    patient_id: UUID,
    consent_type: ConsentType,
    booking_id: Optional[UUID] = None,
) -> bool:
    """Return True iff an active consent of ``consent_type`` exists.

    Booking-scoped consents (e.g. ``medication``) MUST match the booking_id.
    Patient-scoped consents (e.g. ``photo``, ``service`` when not booking-bound)
    can be granted at patient level — we accept booking-id NULL rows as fallbacks.
    """
    now = _now()
    stmt = (
        select(ConsentRecord)
        .where(
            ConsentRecord.patient_id == patient_id,
            ConsentRecord.consent_type == consent_type,
            ConsentRecord.status == ConsentStatus.given,
        )
        .order_by(ConsentRecord.given_at.desc())
    )
    res = await db.execute(stmt)
    rows = list(res.scalars().all())
    for r in rows:
        if r.expires_at is not None and r.expires_at <= now:
            continue
        if r.revoked_at is not None:
            continue
        # Booking-scoped: prefer a row matching this booking, else accept
        # patient-level (booking_id IS NULL) as a fallback.
        if booking_id is not None:
            if r.booking_id == booking_id:
                return True
            if r.booking_id is None:
                return True
        else:
            return True
    return False


async def require_consent(
    db: AsyncSession,
    *,
    patient_id: UUID,
    consent_type: ConsentType,
    booking_id: Optional[UUID] = None,
    action: str = "perform this action",
) -> None:
    """Raise ``ConsentMissingError`` if the consent is absent/revoked.

    ``action`` is a human-friendly verb used in the error message.
    """
    ok = await has_active_consent(
        db, patient_id=patient_id, consent_type=consent_type, booking_id=booking_id
    )
    if not ok:
        # Map consent_type -> stable error code
        code_map = {
            ConsentType.service: "SERVICE_CONSENT_MISSING",
            ConsentType.photo: "PHOTO_CONSENT_MISSING",
            ConsentType.medication: "MEDICATION_CONSENT_MISSING",
        }
        code = code_map.get(consent_type, "CONSENT_MISSING")
        raise ConsentMissingError(
            consent_type,
            f"{consent_type.value} consent is required to {action}.",
            code=code,
        )
