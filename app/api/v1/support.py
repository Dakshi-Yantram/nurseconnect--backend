"""Help center: FAQs + consumer/worker-raised support tickets.

Distinct from clinical escalations (app/api/v1/escalations.py) and the
internal complaint/dispute workflow — this is the "raise a ticket, get
help" surface for consumers and nurses, with a plain queue that the
`support` role (created by operations) works through.

FAQ management is operations-only. Tickets can be raised by any consumer
or worker; the support queue is visible to support/operations/admin.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, require_operations, require_support
from app.models.enums import SupportTicketStatus, UserRole
from app.models.models import Faq, SupportTicket, SupportTicketMessage, User

router = APIRouter(tags=["support"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# FAQs
# ============================================================================
class FaqUpsertRequest(BaseModel):
    audience: str  # consumer | worker | all
    category: Optional[str] = None
    question: str
    answer: str
    display_order: int = 0
    is_active: bool = True


def _serialize_faq(f: Faq) -> dict:
    return {
        "id": str(f.id),
        "audience": f.audience,
        "category": f.category,
        "question": f.question,
        "answer": f.answer,
        "display_order": f.display_order,
        "is_active": f.is_active,
        "updated_at": f.updated_at.isoformat(),
    }


@router.get("/faqs")
async def list_faqs(
    audience: Optional[str] = None,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Published FAQs for the current user's portal. Defaults `audience`
    to consumer/worker based on role if not passed explicitly."""
    if not audience:
        audience = "worker" if current.role == UserRole.worker else "consumer"
    stmt = (
        select(Faq)
        .where(Faq.is_active.is_(True), Faq.audience.in_([audience, "all"]))
        .order_by(Faq.display_order.asc(), Faq.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_serialize_faq(f) for f in rows]


@router.get("/operations/faqs")
async def list_faqs_admin(current: CurrentUser = Depends(require_operations), db: AsyncSession = Depends(get_db)):
    """All FAQs, including inactive — operations management view."""
    rows = (await db.execute(select(Faq).order_by(Faq.audience, Faq.display_order.asc()))).scalars().all()
    return [_serialize_faq(f) for f in rows]


@router.post("/operations/faqs")
async def create_faq(
    payload: FaqUpsertRequest,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    if payload.audience not in ("consumer", "worker", "all"):
        raise HTTPException(status_code=400, detail="audience must be consumer, worker, or all")
    f = Faq(
        audience=payload.audience,
        category=payload.category,
        question=payload.question,
        answer=payload.answer,
        display_order=payload.display_order,
        is_active=payload.is_active,
        created_by=current.id,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return _serialize_faq(f)


@router.put("/operations/faqs/{faq_id}")
async def update_faq(
    faq_id: UUID,
    payload: FaqUpsertRequest,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Faq).where(Faq.id == faq_id))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="FAQ not found")
    if payload.audience not in ("consumer", "worker", "all"):
        raise HTTPException(status_code=400, detail="audience must be consumer, worker, or all")
    f.audience = payload.audience
    f.category = payload.category
    f.question = payload.question
    f.answer = payload.answer
    f.display_order = payload.display_order
    f.is_active = payload.is_active
    await db.commit()
    await db.refresh(f)
    return _serialize_faq(f)


@router.delete("/operations/faqs/{faq_id}")
async def delete_faq(
    faq_id: UUID,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Faq).where(Faq.id == faq_id))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="FAQ not found")
    await db.delete(f)
    await db.commit()
    return {"deleted": True}


# ============================================================================
# Support tickets — raised by consumer/worker, worked by support/operations/admin.
# ============================================================================
class TicketCreateRequest(BaseModel):
    category: str = "other"
    subject: str
    description: str
    booking_id: Optional[UUID] = None


class TicketMessageRequest(BaseModel):
    message: str


class TicketStatusUpdateRequest(BaseModel):
    status: str
    resolution_notes: Optional[str] = None


def _serialize_ticket(t: SupportTicket, raiser_name: Optional[str] = None, assignee_name: Optional[str] = None) -> dict:
    return {
        "id": str(t.id),
        "ticket_ref": t.ticket_ref,
        "raised_by": str(t.raised_by),
        "raiser_name": raiser_name,
        "raiser_role": t.raiser_role,
        "category": t.category,
        "subject": t.subject,
        "description": t.description,
        "booking_id": str(t.booking_id) if t.booking_id else None,
        "status": t.status.value,
        "priority": t.priority,
        "assigned_to": str(t.assigned_to) if t.assigned_to else None,
        "assignee_name": assignee_name,
        "resolution_notes": t.resolution_notes,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


@router.post("/tickets")
async def create_ticket(
    payload: TicketCreateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role not in (UserRole.consumer, UserRole.worker):
        raise HTTPException(status_code=403, detail="Only consumers and nurses can raise support tickets")
    ticket = SupportTicket(
        ticket_ref=f"TKT-{secrets.randbelow(900000) + 100000}",
        raised_by=current.id,
        raiser_role=current.role.value,
        category=payload.category,
        subject=payload.subject,
        description=payload.description,
        booking_id=payload.booking_id,
        status=SupportTicketStatus.open,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return _serialize_ticket(ticket)


@router.get("/tickets/mine")
async def list_my_tickets(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(SupportTicket).where(SupportTicket.raised_by == current.id).order_by(SupportTicket.created_at.desc())
    )).scalars().all()
    return [_serialize_ticket(t) for t in rows]


async def _get_ticket_or_404(ticket_id: UUID, db: AsyncSession) -> SupportTicket:
    res = await db.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t


def _is_support_staff(role: UserRole) -> bool:
    return role in (UserRole.admin, UserRole.operations, UserRole.support)


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_ticket_or_404(ticket_id, db)
    if t.raised_by != current.id and not _is_support_staff(current.role):
        raise HTTPException(status_code=403, detail="Not your ticket")
    mres = await db.execute(
        select(SupportTicketMessage).where(SupportTicketMessage.ticket_id == ticket_id).order_by(SupportTicketMessage.created_at.asc())
    )
    messages = mres.scalars().all()
    sender_ids = {m.sender_id for m in messages} | {t.raised_by} | ({t.assigned_to} if t.assigned_to else set())
    ures = await db.execute(select(User).where(User.id.in_(sender_ids)))
    names = {u.id: (u.full_name or u.email or str(u.id)) for u in ures.scalars().all()}
    out = _serialize_ticket(t, raiser_name=names.get(t.raised_by), assignee_name=names.get(t.assigned_to) if t.assigned_to else None)
    out["messages"] = [
        {
            "id": str(m.id),
            "sender_id": str(m.sender_id),
            "sender_name": names.get(m.sender_id, "Unknown"),
            "sender_role": m.sender_role,
            "message": m.message,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]
    return out


@router.post("/tickets/{ticket_id}/messages")
async def add_ticket_message(
    ticket_id: UUID,
    payload: TicketMessageRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_ticket_or_404(ticket_id, db)
    if t.raised_by != current.id and not _is_support_staff(current.role):
        raise HTTPException(status_code=403, detail="Not your ticket")
    if t.status == SupportTicketStatus.closed:
        raise HTTPException(status_code=409, detail="Ticket is closed")
    msg = SupportTicketMessage(
        ticket_id=ticket_id, sender_id=current.id, sender_role=current.role.value, message=payload.message,
    )
    db.add(msg)
    if t.status == SupportTicketStatus.open and _is_support_staff(current.role):
        t.status = SupportTicketStatus.in_progress
    await db.commit()
    return {"id": str(msg.id), "created_at": msg.created_at.isoformat()}


@router.get("/support/tickets")
async def list_support_queue(
    status: Optional[str] = None,
    current: CurrentUser = Depends(require_support),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(SupportTicket).order_by(SupportTicket.created_at.desc())
    if status:
        stmt = stmt.where(SupportTicket.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    raiser_ids = {t.raised_by for t in rows}
    ures = await db.execute(select(User).where(User.id.in_(raiser_ids)))
    names = {u.id: (u.full_name or u.email or str(u.id)) for u in ures.scalars().all()}
    return [_serialize_ticket(t, raiser_name=names.get(t.raised_by)) for t in rows]


@router.post("/support/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: UUID,
    current: CurrentUser = Depends(require_support),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_ticket_or_404(ticket_id, db)
    t.assigned_to = current.id
    if t.status == SupportTicketStatus.open:
        t.status = SupportTicketStatus.in_progress
    await db.commit()
    return _serialize_ticket(t)


@router.post("/support/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: UUID,
    payload: TicketStatusUpdateRequest,
    current: CurrentUser = Depends(require_support),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_ticket_or_404(ticket_id, db)
    try:
        new_status = SupportTicketStatus(payload.status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")
    t.status = new_status
    if payload.resolution_notes:
        t.resolution_notes = payload.resolution_notes
    if new_status in (SupportTicketStatus.resolved, SupportTicketStatus.closed):
        t.resolved_at = _now()
    await db.commit()
    return _serialize_ticket(t)
