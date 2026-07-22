"""HTTP endpoints for the reviewer assignment system.

Reviewer-facing:
  GET  /api/review/my-queue               — tickets assigned to me
  GET  /api/review/tickets/{id}           — single ticket detail
  POST /api/review/tickets/{id}/status    — update status (IN_REVIEW, NEEDS_CLARIFICATION, etc.)

Admin-facing:
  GET  /api/admin/review/tickets          — all tickets (filterable)
  GET  /api/admin/review/unassigned       — tickets with no reviewer
  POST /api/admin/review/tickets/{id}/reassign   — manual reassign
  POST /api/admin/review/tickets/{id}/priority   — change priority
  GET  /api/admin/review/tickets/{id}/logs       — assignment audit log
  GET  /api/admin/reviewers               — list reviewer profiles + workload
  POST /api/admin/reviewers               — create reviewer profile
  PUT  /api/admin/reviewers/{id}          — update capacity / availability
  POST /api/admin/review/tickets/{id}/retry-assign   — retry auto-assign on UNASSIGNED

Mount in app/main.py:
    from app.api.v1 import review_tickets
    ... app.include_router(review_tickets.router, prefix=_API_PREFIX)
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, require_admin, require_reviewer
from app.models.models import (
    NurseReviewTicket,
    ReviewerAssignmentLog,
    ReviewerProfile,
    User,
    WorkerProfile,
)
from app.services.reviewer_assignment import (
    auto_assign_ticket,
    manual_reassign_ticket,
    serialize_ticket,
)

router = APIRouter(tags=["review"])

OPEN_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION", "UNASSIGNED")
VALID_REVIEWER_STATUSES = ("IN_REVIEW", "NEEDS_CLARIFICATION", "APPROVED", "REJECTED")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_reviewer_profile(user_id: UUID, db: AsyncSession) -> ReviewerProfile:
    res = await db.execute(select(ReviewerProfile).where(ReviewerProfile.user_id == user_id))
    rp = res.scalar_one_or_none()
    if not rp:
        raise HTTPException(status_code=404, detail="Reviewer profile not found for your account")
    return rp


async def _ticket_or_404(ticket_id: UUID, db: AsyncSession) -> NurseReviewTicket:
    res = await db.execute(select(NurseReviewTicket).where(NurseReviewTicket.id == ticket_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t


def _serialize_reviewer(rp: ReviewerProfile, user: User, open_count: int) -> dict:
    return {
        "id": str(rp.id),
        "user_id": str(rp.user_id),
        "name": user.full_name,
        "email": user.email,
        "is_active": rp.is_active,
        "can_review_nurse_documents": rp.can_review_nurse_documents,
        "max_open_tickets": rp.max_open_tickets,
        "open_tickets": open_count,
        "daily_assigned_count": rp.daily_assigned_count,
        "specialization": rp.specialization,
        "last_assigned_at": rp.last_assigned_at.isoformat() if rp.last_assigned_at else None,
    }


# ---------------------------------------------------------------------------
# REVIEWER — their own queue
# ---------------------------------------------------------------------------

@router.get("/review/my-queue")
async def my_queue(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """All tickets currently assigned to the logged-in reviewer, newest SLA first."""
    res = await db.execute(select(ReviewerProfile).where(ReviewerProfile.user_id == current.id))
    rp = res.scalar_one_or_none()
    if not rp:
        # Not every user with reviewer-tier access (e.g. admins browsing this
        # screen) has a reviewer profile. Treat that as "no queue" instead of
        # a 404 so the frontend doesn't log noisy network errors.
        return []
    stmt = (
        select(NurseReviewTicket)
        .where(NurseReviewTicket.assigned_reviewer_id == rp.id)
        .order_by(
            NurseReviewTicket.priority.desc(),
            NurseReviewTicket.sla_due_at.asc().nullslast(),
            NurseReviewTicket.created_at.asc(),
        )
    )
    if status:
        stmt = stmt.where(NurseReviewTicket.status == status.upper())
    if priority:
        stmt = stmt.where(NurseReviewTicket.priority == priority.upper())
    rows = (await db.execute(stmt)).scalars().all()

    # Enrich with nurse name + profile summary (used by onboarding review UI).
    result = []
    for t in rows:
        wp_res = await db.execute(
            select(WorkerProfile, User)
            .join(User, User.id == WorkerProfile.user_id)
            .where(WorkerProfile.id == t.nurse_id)
        )
        row = wp_res.first()
        entry = serialize_ticket(t)
        entry["nurse_name"] = row[1].full_name if row else None
        entry["nurse_email"] = row[1].email if row else None
        entry["specialty"] = (row[0].specialisations[0] if row and row[0].specialisations else None)
        entry["experience_years"] = row[0].years_of_experience if row else None
        entry["city"] = row[0].base_city if row else None
        result.append(entry)
    return result


@router.get("/review/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    rp = await _get_reviewer_profile(current.id, db)
    t = await _ticket_or_404(ticket_id, db)
    # Admin can see any; reviewer can only see their own.
    from app.core.deps import is_admin
    if not is_admin(current.role) and t.assigned_reviewer_id != rp.id:
        raise HTTPException(status_code=403, detail="This ticket is not assigned to you")
    return serialize_ticket(t)


class TicketStatusUpdate(BaseModel):
    status: str      # IN_REVIEW | NEEDS_CLARIFICATION | APPROVED | REJECTED
    note: Optional[str] = None


@router.post("/review/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: UUID,
    payload: TicketStatusUpdate,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Reviewer updates the status of a ticket assigned to them."""
    rp = await _get_reviewer_profile(current.id, db)
    t = await _ticket_or_404(ticket_id, db)
    from app.core.deps import is_admin
    if not is_admin(current.role) and t.assigned_reviewer_id != rp.id:
        raise HTTPException(status_code=403, detail="Not your ticket")
    new_status = payload.status.upper()
    if new_status not in VALID_REVIEWER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Choose: {VALID_REVIEWER_STATUSES}")
    t.status = new_status
    await db.commit()
    return serialize_ticket(t)


# ---------------------------------------------------------------------------
# ADMIN — full ticket management
# ---------------------------------------------------------------------------

@router.get("/admin/review/tickets")
async def admin_list_tickets(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    reviewer_id: Optional[UUID] = Query(None),
    limit: int = Query(50, le=200),
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """All tickets — filterable. Priority HIGH appears first, then by SLA."""
    stmt = (
        select(NurseReviewTicket)
        .order_by(
            NurseReviewTicket.priority.desc(),
            NurseReviewTicket.sla_due_at.asc().nullslast(),
            NurseReviewTicket.created_at.asc(),
        )
        .limit(limit)
    )
    if status:
        stmt = stmt.where(NurseReviewTicket.status == status.upper())
    if priority:
        stmt = stmt.where(NurseReviewTicket.priority == priority.upper())
    if reviewer_id:
        rp_res = await db.execute(select(ReviewerProfile).where(ReviewerProfile.id == reviewer_id))
        rp = rp_res.scalar_one_or_none()
        if rp:
            stmt = stmt.where(NurseReviewTicket.assigned_reviewer_id == rp.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_ticket(t) for t in rows]


@router.get("/admin/review/unassigned")
async def admin_unassigned_tickets(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Tickets with no reviewer — need manual attention."""
    rows = (await db.execute(
        select(NurseReviewTicket)
        .where(NurseReviewTicket.status == "UNASSIGNED")
        .order_by(NurseReviewTicket.created_at.asc())
    )).scalars().all()
    return [serialize_ticket(t) for t in rows]


class ReassignRequest(BaseModel):
    reviewer_profile_id: UUID
    reason: Optional[str] = None


@router.post("/admin/review/tickets/{ticket_id}/reassign")
async def admin_reassign(
    ticket_id: UUID,
    payload: ReassignRequest,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually reassign a ticket to a specific reviewer profile."""
    ticket = await manual_reassign_ticket(
        db,
        ticket_id=ticket_id,
        new_reviewer_id=payload.reviewer_profile_id,
        changed_by=current.id,
        reason=payload.reason or "Manual reassignment by admin",
    )
    return serialize_ticket(ticket)


class PriorityUpdate(BaseModel):
    priority: str   # NORMAL | MEDIUM | HIGH


@router.post("/admin/review/tickets/{ticket_id}/priority")
async def admin_set_priority(
    ticket_id: UUID,
    payload: PriorityUpdate,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    t = await _ticket_or_404(ticket_id, db)
    t.priority = payload.priority.upper()
    await db.commit()
    return serialize_ticket(t)


@router.post("/admin/review/tickets/{ticket_id}/retry-assign")
async def admin_retry_assign(
    ticket_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retry auto-assignment on a ticket that was left UNASSIGNED."""
    t = await _ticket_or_404(ticket_id, db)
    if t.status != "UNASSIGNED":
        raise HTTPException(status_code=400, detail="Ticket is not UNASSIGNED")
    reviewer = await auto_assign_ticket(db, t)
    await db.commit()
    result = serialize_ticket(t)
    result["assigned_to"] = str(reviewer.id) if reviewer else None
    return result


@router.get("/admin/review/tickets/{ticket_id}/logs")
async def admin_ticket_logs(
    ticket_id: UUID,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full assignment audit log for a ticket."""
    rows = (await db.execute(
        select(ReviewerAssignmentLog)
        .where(ReviewerAssignmentLog.ticket_id == ticket_id)
        .order_by(ReviewerAssignmentLog.created_at.asc())
    )).scalars().all()
    return [
        {
            "id": str(r.id),
            "reviewer_id": str(r.reviewer_id) if r.reviewer_id else None,
            "old_reviewer_id": str(r.old_reviewer_id) if r.old_reviewer_id else None,
            "method": r.assignment_method,
            "reason": r.assignment_reason,
            "assigned_by": str(r.assigned_by) if r.assigned_by else None,
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# ADMIN — reviewer profile management
# ---------------------------------------------------------------------------

@router.get("/admin/reviewers")
async def admin_list_reviewers(
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """All reviewer profiles with live open-ticket count."""
    from sqlalchemy import func as sqlfunc
    open_sq = (
        select(
            NurseReviewTicket.assigned_reviewer_id,
            sqlfunc.count(NurseReviewTicket.id).label("open_count"),
        )
        .where(NurseReviewTicket.status.in_(OPEN_STATUSES))
        .group_by(NurseReviewTicket.assigned_reviewer_id)
        .subquery()
    )
    rows = (await db.execute(
        select(ReviewerProfile, User, open_sq.c.open_count)
        .join(User, User.id == ReviewerProfile.user_id)
        .outerjoin(open_sq, ReviewerProfile.id == open_sq.c.assigned_reviewer_id)
        .order_by(ReviewerProfile.is_active.desc(), ReviewerProfile.created_at.asc())
    )).all()
    return [_serialize_reviewer(rp, user, open_count or 0) for rp, user, open_count in rows]


class ReviewerProfileIn(BaseModel):
    user_id: UUID
    is_active: bool = True
    can_review_nurse_documents: bool = True
    max_open_tickets: int = 20
    specialization: Optional[str] = None


@router.post("/admin/reviewers")
async def admin_create_reviewer(
    payload: ReviewerProfileIn,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a reviewer profile for an existing user (must have role=reviewer)."""
    u_res = await db.execute(select(User).where(User.id == payload.user_id))
    user = u_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from app.models.enums import UserRole
    if user.role != UserRole.reviewer:
        raise HTTPException(status_code=400, detail="User must have the reviewer role")
    rp = ReviewerProfile(**payload.model_dump())
    db.add(rp)
    await db.commit()
    await db.refresh(rp)
    return _serialize_reviewer(rp, user, 0)


class ReviewerProfileUpdate(BaseModel):
    is_active: Optional[bool] = None
    can_review_nurse_documents: Optional[bool] = None
    max_open_tickets: Optional[int] = None
    specialization: Optional[str] = None


@router.put("/admin/reviewers/{reviewer_profile_id}")
async def admin_update_reviewer(
    reviewer_profile_id: UUID,
    payload: ReviewerProfileUpdate,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update capacity, availability, or specialization of a reviewer."""
    res = await db.execute(select(ReviewerProfile).where(ReviewerProfile.id == reviewer_profile_id))
    rp = res.scalar_one_or_none()
    if not rp:
        raise HTTPException(status_code=404, detail="Reviewer profile not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(rp, field, val)
    await db.commit()
    u_res = await db.execute(select(User).where(User.id == rp.user_id))
    user = u_res.scalar_one()
    return _serialize_reviewer(rp, user, 0)