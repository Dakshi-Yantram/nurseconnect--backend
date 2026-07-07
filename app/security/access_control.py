"""Patch 5B — Centralized RBAC + ownership helpers.

This module consolidates authorization logic that was previously duplicated
across routers (visits, care, care_workflow, tracking, escalations,
training, insurance). It does NOT change business rules — it only
centralizes the existing checks so all routers raise HTTP 403 with a
standardized response shape and so that each denial can be optionally
audited via ``app.services.security_audit_service``.

Conventions:
  * Every helper accepts ``CurrentUser`` (the authenticated principal) plus
    the entity it must access. Helpers either return the entity (so callers
    can reuse it) or raise ``HTTPException(403)``.
  * Admin role helpers are pure (no DB hit).
  * Ownership helpers perform a single targeted DB lookup and never
    leak unrelated rows.
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, is_admin
from app.models.enums import UserRole
from app.models.models import (
    Booking,
    ConsumerProfile,
    Patient,
    VisitRecord,
    WorkerProfile,
)


# ---------------------------------------------------------------------------
# Standardized 403
# ---------------------------------------------------------------------------
def _forbidden(code: str, message: str) -> HTTPException:
    """Build a standardized 403 the security audit service can consume."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "message": message},
    )


# ---------------------------------------------------------------------------
# Admin role assertions — pure (no DB)
# ---------------------------------------------------------------------------
def assert_admin_role(current: CurrentUser) -> CurrentUser:
    """Any admin_* role passes."""
    if not is_admin(current.role):
        raise _forbidden("ADMIN_REQUIRED", "Admin role required")
    return current


def _require(current: CurrentUser, allowed: Iterable[UserRole], code: str, message: str) -> CurrentUser:
    allowed_set = set(allowed)
    if current.role not in allowed_set:
        raise _forbidden(code, message)
    return current


def assert_admin_clinical(current: CurrentUser) -> CurrentUser:
    return assert_admin_role(current)


def assert_admin_ops(current: CurrentUser) -> CurrentUser:
    return assert_admin_role(current)


def assert_admin_finance(current: CurrentUser) -> CurrentUser:
    return assert_admin_role(current)


def assert_admin_super(current: CurrentUser) -> CurrentUser:
    return assert_admin_role(current)


# ---------------------------------------------------------------------------
# Consumer / Worker ownership lookups (single targeted query each)
# ---------------------------------------------------------------------------
async def _consumer_profile(db: AsyncSession, user_id: UUID) -> Optional[ConsumerProfile]:
    res = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == user_id))
    return res.scalar_one_or_none()


async def _worker_profile(db: AsyncSession, user_id: UUID) -> Optional[WorkerProfile]:
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == user_id))
    return res.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Patient access
# ---------------------------------------------------------------------------
async def assert_user_can_access_patient(
    db: AsyncSession, current: CurrentUser, patient_id: UUID
) -> Patient:
    """Centralized patient access:
      * admins → allowed
      * consumer → only if the patient belongs to their profile
      * worker  → only if they are the assigned worker on at least one
                  of that patient's bookings
    """
    res = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = res.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if is_admin(current.role):
        return patient
    if current.role == UserRole.consumer:
        cp = await _consumer_profile(db, current.id)
        if not cp or patient.consumer_id != cp.id:
            raise _forbidden("PATIENT_OWNERSHIP", "Patient does not belong to this consumer")
        return patient
    if current.role == UserRole.worker:
        wp = await _worker_profile(db, current.id)
        if not wp:
            raise _forbidden("WORKER_PROFILE_MISSING", "Worker profile missing")
        bres = await db.execute(
            select(Booking).where(
                Booking.patient_id == patient.id,
                Booking.worker_id == wp.id,
            ).limit(1)
        )
        if bres.scalar_one_or_none() is None:
            raise _forbidden("WORKER_NOT_ASSIGNED", "Worker not assigned to any booking for this patient")
        return patient
    raise _forbidden("ROLE_NOT_ALLOWED", "Role not allowed to access patient")


async def assert_consumer_owns_patient(
    db: AsyncSession, current: CurrentUser, patient_id: UUID
) -> Patient:
    """Stricter variant: must be the owning consumer (admins still bypass)."""
    res = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = res.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if is_admin(current.role):
        return patient
    if current.role != UserRole.consumer:
        raise _forbidden("CONSUMER_REQUIRED", "Consumer role required")
    cp = await _consumer_profile(db, current.id)
    if not cp or patient.consumer_id != cp.id:
        raise _forbidden("PATIENT_OWNERSHIP", "Patient does not belong to this consumer")
    return patient


# ---------------------------------------------------------------------------
# Booking access
# ---------------------------------------------------------------------------
async def assert_user_can_access_booking(
    db: AsyncSession, current: CurrentUser, booking_id: UUID
) -> Booking:
    """admins | owning consumer | assigned worker may access a booking."""
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if is_admin(current.role):
        return booking
    if current.role == UserRole.consumer:
        cp = await _consumer_profile(db, current.id)
        if not cp or booking.consumer_id != cp.id:
            raise _forbidden("BOOKING_OWNERSHIP", "Booking does not belong to this consumer")
        return booking
    if current.role == UserRole.worker:
        wp = await _worker_profile(db, current.id)
        if not wp or booking.worker_id != wp.id:
            raise _forbidden("WORKER_NOT_ASSIGNED", "Worker is not assigned to this booking")
        return booking
    raise _forbidden("ROLE_NOT_ALLOWED", "Role not allowed to access booking")


async def assert_worker_assigned_to_booking(
    db: AsyncSession, current: CurrentUser, booking_id: UUID
) -> Tuple[Booking, WorkerProfile]:
    """Strict worker assignment guard (admins bypass and may pass ``None``-equivalent)."""
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if is_admin(current.role):
        # Admins may proceed but return a placeholder WorkerProfile fetch if any.
        wp = await _worker_profile(db, current.id)
        return booking, wp  # type: ignore[return-value]
    if current.role != UserRole.worker:
        raise _forbidden("WORKER_REQUIRED", "Worker role required")
    wp = await _worker_profile(db, current.id)
    if not wp or booking.worker_id != wp.id:
        raise _forbidden("WORKER_NOT_ASSIGNED", "Worker is not assigned to this booking")
    return booking, wp


async def assert_consumer_owns_booking(
    db: AsyncSession, current: CurrentUser, booking_id: UUID
) -> Booking:
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if is_admin(current.role):
        return booking
    if current.role != UserRole.consumer:
        raise _forbidden("CONSUMER_REQUIRED", "Consumer role required")
    cp = await _consumer_profile(db, current.id)
    if not cp or booking.consumer_id != cp.id:
        raise _forbidden("BOOKING_OWNERSHIP", "Booking does not belong to this consumer")
    return booking


# ---------------------------------------------------------------------------
# Visit record access
# ---------------------------------------------------------------------------
async def assert_user_can_access_visit_record(
    db: AsyncSession, current: CurrentUser, booking_id: UUID
) -> Tuple[Booking, Optional[VisitRecord]]:
    """Re-uses booking ownership rules; returns the booking + visit (if any)."""
    booking = await assert_user_can_access_booking(db, current, booking_id)
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()
    return booking, visit