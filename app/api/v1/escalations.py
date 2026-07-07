"""Escalations: list, ack, resolve. Admin-facing."""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, require_admin, require_roles
from app.models.enums import EscalationStatus, UserRole
from app.models.models import Booking, ConsumerProfile, Escalation, WorkerProfile
from app.schemas.schemas import (
    EscalationOut,
    EscalationResolveRequest,
    EscalationAssignRequest,
    EscalationNoteRequest,
    EscalationSummaryOut,
)
from app.services.common_services import audit

router = APIRouter(prefix="/escalations", tags=["escalations"])


_ADMIN_ROLES = {UserRole.admin}
_SUPPORT_ROLES = {UserRole.admin}

@router.get("/open", response_model=List[EscalationOut])
async def list_open(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.status != EscalationStatus.resolved).order_by(Escalation.level.desc(), Escalation.created_at.desc()))
    return [EscalationOut.model_validate(e) for e in res.scalars().all()]


@router.get("/", response_model=List[EscalationOut])
async def list_escalations(
    status: Optional[EscalationStatus] = None,
    booking_id: Optional[UUID] = None,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Production hardening: per-role scoping.
      - admin*  → unrestricted
      - worker  → only escalations on bookings assigned to them
      - consumer → only escalations on their own bookings
    booking_id filter is enforced AFTER scoping, so cross-tenant probing returns [].
    """
    conds = []
    if status:
        conds.append(Escalation.status == status)
    if booking_id:
        conds.append(Escalation.booking_id == booking_id)

    # Per-role scoping
    if current.role in _ADMIN_ROLES:
        pass  # full access
    elif current.role == UserRole.worker:
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
        wp = wres.scalar_one_or_none()
        if not wp:
            return []
        conds.append(Escalation.worker_id == wp.id)
    elif current.role == UserRole.consumer:
        cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
        cp = cres.scalar_one_or_none()
        if not cp:
            return []
        # Join via booking to ensure consumer owns the booking
        scoped = (
            select(Escalation)
            .join(Booking, Booking.id == Escalation.booking_id)
            .where(Booking.consumer_id == cp.id, *conds)
        )
        res = await db.execute(scoped)
        return [EscalationOut.model_validate(e) for e in res.scalars().all()]
    else:
        return []

    res = await db.execute(select(Escalation).where(and_(*conds)) if conds else select(Escalation))
    return [EscalationOut.model_validate(e) for e in res.scalars().all()]


@router.post("/{escalation_id}/acknowledge", response_model=EscalationOut)
async def acknowledge(
    escalation_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.id == escalation_id))
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    e.status = EscalationStatus.acknowledged
    e.acknowledged_by = current.id
    e.acknowledged_at = datetime.now(timezone.utc)
    await audit(db, current.id, current.role.value, "escalation.acknowledge", "escalation", e.id)
    await db.commit()
    await db.refresh(e)
    return EscalationOut.model_validate(e)


@router.post("/{escalation_id}/resolve", response_model=EscalationOut)
async def resolve(
    escalation_id: UUID,
    payload: EscalationResolveRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.id == escalation_id))
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    e.status = EscalationStatus.resolved
    e.resolved_by = current.id
    e.resolved_at = datetime.now(timezone.utc)
    e.resolution_notes = payload.resolution_notes
    await audit(db, current.id, current.role.value, "escalation.resolve", "escalation", e.id, {"notes": payload.resolution_notes})
    await db.commit()
    await db.refresh(e)
    return EscalationOut.model_validate(e)
# ── Patch 6: Support dashboard endpoints ──────────────────────────────────────

@router.get("/summary", response_model=EscalationSummaryOut)
async def get_summary(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation))
    all_e = res.scalars().all()
    now = datetime.now(timezone.utc)
    return EscalationSummaryOut(
        total=len(all_e),
        open=sum(1 for e in all_e if e.status == EscalationStatus.open),
        acknowledged=sum(1 for e in all_e if e.status == EscalationStatus.acknowledged),
        investigating=sum(1 for e in all_e if e.status == EscalationStatus.investigating),
        resolved=sum(1 for e in all_e if e.status == EscalationStatus.resolved),
        emergency=sum(1 for e in all_e if e.level.value == "emergency"),
        contact_doctor=sum(1 for e in all_e if e.level.value == "contact_doctor"),
        sla_breached=sum(1 for e in all_e if e.sla_breach_at and e.sla_breach_at < now and e.status != EscalationStatus.resolved),
        unassigned=sum(1 for e in all_e if e.assigned_to is None and e.status != EscalationStatus.resolved),
    )


@router.get("/assigned-to-me", response_model=List[EscalationOut])
async def assigned_to_me(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Escalation)
        .where(Escalation.assigned_to == current.id)
        .order_by(Escalation.created_at.desc())
    )
    return [EscalationOut.model_validate(e) for e in res.scalars().all()]


@router.get("/unassigned", response_model=List[EscalationOut])
async def unassigned(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Escalation)
        .where(
            Escalation.assigned_to == None,
            Escalation.status != EscalationStatus.resolved,
        )
        .order_by(Escalation.created_at.desc())
    )
    return [EscalationOut.model_validate(e) for e in res.scalars().all()]


@router.post("/{escalation_id}/assign", response_model=EscalationOut)
async def assign(
    escalation_id: UUID,
    payload: EscalationAssignRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.id == escalation_id))
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    e.assigned_to = payload.assigned_to
    e.assigned_at = datetime.now(timezone.utc)
    if e.status == EscalationStatus.open:
        e.status = EscalationStatus.acknowledged
        e.acknowledged_by = current.id
        e.acknowledged_at = datetime.now(timezone.utc)
    await audit(db, current.id, current.role.value, "escalation.assign", "escalation", e.id, {"assigned_to": str(payload.assigned_to)})
    await db.commit()
    await db.refresh(e)
    return EscalationOut.model_validate(e)


@router.post("/{escalation_id}/investigate", response_model=EscalationOut)
async def investigate(
    escalation_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.id == escalation_id))
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    e.status = EscalationStatus.investigating
    await audit(db, current.id, current.role.value, "escalation.investigate", "escalation", e.id)
    await db.commit()
    await db.refresh(e)
    return EscalationOut.model_validate(e)


@router.post("/{escalation_id}/note", response_model=EscalationOut)
async def add_note(
    escalation_id: UUID,
    payload: EscalationNoteRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Escalation).where(Escalation.id == escalation_id))
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_note = f"[{timestamp} — {current.id}] {payload.note}"
    e.internal_notes = f"{e.internal_notes}\n{new_note}" if e.internal_notes else new_note
    await audit(db, current.id, current.role.value, "escalation.note", "escalation", e.id)
    await db.commit()
    await db.refresh(e)
    return EscalationOut.model_validate(e)