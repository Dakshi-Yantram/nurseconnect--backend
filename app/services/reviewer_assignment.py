"""Reviewer assignment engine.

Implements the weighted round-robin + workload logic described in the spec:
  1. Only active reviewers who can review nurse documents.
  2. Exclude anyone at max open-ticket capacity.
  3. Pick reviewer with lowest current open-ticket count.
  4. Tie-break: oldest last_assigned_at (round-robin fairness).
  5. Tie-break: lowest daily_assigned_count.

Concurrency-safe: uses SELECT … FOR UPDATE SKIP LOCKED so two simultaneous
submissions can never get assigned to the same reviewer.

Public API:
  auto_assign_ticket(db, ticket)   — called on every new submission.
  manual_reassign_ticket(db, ...)  — admin override with audit log.
  get_or_create_ticket(db, ...)    — idempotent ticket creation + auto-assign.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    NurseReviewTicket,
    ReviewerAssignmentLog,
    ReviewerProfile,
    User,
)

# Statuses that count against a reviewer's open-ticket capacity.
OPEN_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION")

# SLA windows by priority (hours).
_SLA_HOURS = {"HIGH": 12, "MEDIUM": 24, "NORMAL": 48}


def _sla_due(priority: str) -> datetime:
    hours = _SLA_HOURS.get(priority, 48)
    return datetime.now(timezone.utc) + timedelta(hours=hours)

async def _select_reviewer(
    db: AsyncSession,
    ticket_type: str = "NURSE_DOCUMENT_REVIEW",
) -> Optional[ReviewerProfile]:
    """Select the best available reviewer using the weighted algorithm.
    Locks the row to prevent concurrent double-assignment."""

    # Subquery: count open tickets per reviewer.
    open_count_sq = (
        select(
            NurseReviewTicket.assigned_reviewer_id,
            func.count(NurseReviewTicket.id).label("open_count"),
        )
        .where(NurseReviewTicket.status.in_(OPEN_STATUSES))
        .group_by(NurseReviewTicket.assigned_reviewer_id)
        .subquery()
    )

    # Step 1: find the best candidate id WITHOUT locking (GROUP BY + FOR UPDATE
    # is not allowed together in PostgreSQL).
    id_stmt = (
        select(ReviewerProfile.id)
        .outerjoin(
            open_count_sq,
            ReviewerProfile.id == open_count_sq.c.assigned_reviewer_id,
        )
        .where(
            ReviewerProfile.is_active.is_(True),
            ReviewerProfile.can_review_nurse_documents.is_(True),
        )
        .where(
            func.coalesce(open_count_sq.c.open_count, 0) < ReviewerProfile.max_open_tickets
        )
        .order_by(
            func.coalesce(open_count_sq.c.open_count, 0).asc(),
            ReviewerProfile.last_assigned_at.asc().nullsfirst(),
            ReviewerProfile.daily_assigned_count.asc(),
        )
        .limit(1)
    )
    id_result = await db.execute(id_stmt)
    reviewer_id = id_result.scalar_one_or_none()

    if reviewer_id is None:
        return None

    # Step 2: lock that specific row (simple PK lookup — GROUP BY-free, so
    # FOR UPDATE is fine here).
    lock_stmt = (
        select(ReviewerProfile)
        .where(ReviewerProfile.id == reviewer_id)
        .with_for_update(skip_locked=True)
    )
    lock_result = await db.execute(lock_stmt)
    return lock_result.scalar_one_or_none()


async def auto_assign_ticket(
    db: AsyncSession,
    ticket: NurseReviewTicket,
) -> Optional[ReviewerProfile]:
    """Assign ticket to the best reviewer inside the caller's transaction.
    Returns the reviewer assigned, or None if no reviewer was available.
    The caller must commit after this returns."""
    reviewer = await _select_reviewer(db, ticket.ticket_type)

    if reviewer is None:
        # No eligible reviewer — mark UNASSIGNED so admin sees it.
        ticket.status = "UNASSIGNED"
        ticket.assigned_reviewer_id = None
        db.add(
            ReviewerAssignmentLog(
                ticket_id=ticket.id,
                reviewer_id=None,
                assignment_method="AUTO",
                assignment_reason="No active reviewer available — ticket marked UNASSIGNED",
            )
        )
        return None

    # Determine reason string for the audit log.
    reason = "Lowest open ticket count"
    if reviewer.last_assigned_at:
        reason += f"; last assigned at {reviewer.last_assigned_at.isoformat()}"

    ticket.assigned_reviewer_id = reviewer.id
    ticket.assigned_at = datetime.now(timezone.utc)
    ticket.status = "PENDING_REVIEW"

    reviewer.last_assigned_at = datetime.now(timezone.utc)
    reviewer.daily_assigned_count = (reviewer.daily_assigned_count or 0) + 1

    db.add(
        ReviewerAssignmentLog(
            ticket_id=ticket.id,
            reviewer_id=reviewer.id,
            old_reviewer_id=None,
            assignment_method="AUTO",
            assignment_reason=reason,
        )
    )
    return reviewer


async def manual_reassign_ticket(
    db: AsyncSession,
    ticket_id: UUID,
    new_reviewer_id: UUID,
    changed_by: UUID,
    reason: str = "",
) -> NurseReviewTicket:
    """Admin manually reassigns a ticket. Logged with old/new reviewer."""
    t_res = await db.execute(select(NurseReviewTicket).where(NurseReviewTicket.id == ticket_id).with_for_update())
    ticket = t_res.scalar_one_or_none()
    if ticket is None:
        raise ValueError(f"Ticket {ticket_id} not found")

    r_res = await db.execute(select(ReviewerProfile).where(ReviewerProfile.id == new_reviewer_id))
    new_reviewer = r_res.scalar_one_or_none()
    if new_reviewer is None:
        raise ValueError(f"Reviewer {new_reviewer_id} not found")

    old_reviewer_id = ticket.assigned_reviewer_id
    ticket.assigned_reviewer_id = new_reviewer_id
    ticket.assigned_at = datetime.now(timezone.utc)
    ticket.status = "PENDING_REVIEW"

    new_reviewer.last_assigned_at = datetime.now(timezone.utc)
    new_reviewer.daily_assigned_count = (new_reviewer.daily_assigned_count or 0) + 1

    db.add(
        ReviewerAssignmentLog(
            ticket_id=ticket.id,
            reviewer_id=new_reviewer_id,
            old_reviewer_id=old_reviewer_id,
            assignment_method="MANUAL",
            assignment_reason=reason or "Manual reassignment by admin",
            assigned_by=changed_by,
        )
    )
    await db.commit()
    return ticket


async def get_or_create_ticket(
    db: AsyncSession,
    nurse_id: UUID,
    priority: str = "NORMAL",
    ticket_type: str = "NURSE_DOCUMENT_REVIEW",
) -> NurseReviewTicket:
    """Idempotent: create a ticket for this nurse if none is OPEN, then auto-assign.
    Caller must commit after this returns so the assignment lock is released."""
    existing = await db.execute(
        select(NurseReviewTicket).where(
            NurseReviewTicket.nurse_id == nurse_id,
            NurseReviewTicket.status.in_((*OPEN_STATUSES, "UNASSIGNED", "PENDING_REVIEW")),
        )
    )
    ticket = existing.scalar_one_or_none()
    if ticket is not None:
        # Resubmission after rejection: bump priority if currently NORMAL.
        if priority in ("MEDIUM", "HIGH") and ticket.priority == "NORMAL":
            ticket.priority = priority
        return ticket

    ticket = NurseReviewTicket(
        nurse_id=nurse_id,
        ticket_type=ticket_type,
        status="PENDING_REVIEW",
        priority=priority,
        sla_due_at=_sla_due(priority),
    )
    db.add(ticket)
    await db.flush()   # get the ticket.id before assigning
    await auto_assign_ticket(db, ticket)
    return ticket


def serialize_ticket(ticket: NurseReviewTicket) -> dict:
    return {
        "id": str(ticket.id),
        "nurse_id": str(ticket.nurse_id),
        "ticket_type": ticket.ticket_type,
        "status": ticket.status,
        "priority": ticket.priority,
        "assigned_reviewer_id": str(ticket.assigned_reviewer_id) if ticket.assigned_reviewer_id else None,
        "assigned_at": ticket.assigned_at.isoformat() if ticket.assigned_at else None,
        "sla_due_at": ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
        "created_at": ticket.created_at.isoformat(),
    }
