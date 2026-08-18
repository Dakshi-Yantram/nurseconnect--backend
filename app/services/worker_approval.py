"""Single source of truth for "approve / reject a worker's onboarding".

Previously this logic lived only inside `POST /admin/workers/{id}/approve`
(app/api/v1/admin.py). The reviewer-facing ticket endpoint
`POST /review/tickets/{id}/status` (app/api/v1/review_tickets.py) updated
only the `NurseReviewTicket.status` column and never touched
`WorkerProfile.onboarding_status` / `User.status`.

That meant a reviewer could move a ticket all the way to APPROVED (the admin
UI's "stage 10 / Activated" step) while the worker's actual account stayed
stuck on `onboarding_status = pending_review`:
  - the nurse app kept showing "Your profile is under review", and
  - the worker was invisible to dispatch (`WorkerProfile.onboarding_status
    == approved` gates matching), so consumers booking care could get no
    eligible nurse.

Both entry points now call the functions below, so approving/rejecting a
worker always does the full job: validation, activation, tier badge, and
qualification sync, and always keeps the reviewer-queue ticket in sync.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ProviderStatusChangeReason, UserStatus, WorkerOnboardingStatus, WorkerType
from app.models.models import NurseReviewTicket, ProviderStatusHistory, User, WorkerDocument, WorkerProfile
from app.core.provider_types import required_docs as _required_docs_for_type

# Ticket statuses that are still "open" in the reviewer queue — used to find
# the live ticket for a worker when syncing status after a review action.
_OPEN_TICKET_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION", "UNASSIGNED")


async def _record_status_change(
    db: AsyncSession,
    worker_id: UUID,
    from_status: Optional[str],
    to_status: str,
    reason: ProviderStatusChangeReason,
    changed_by: Optional[UUID] = None,
    notes: Optional[str] = None,
) -> None:
    db.add(ProviderStatusHistory(
        worker_id=worker_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        changed_by=changed_by,
        notes=notes,
    ))


async def sync_ticket_status(db: AsyncSession, worker_id: UUID, new_status: str) -> None:
    """Keep the reviewer-queue ticket (NurseReviewTicket) in sync with worker
    onboarding / document review actions, whichever side triggered them.
    """
    res = await db.execute(
        select(NurseReviewTicket).where(
            NurseReviewTicket.nurse_id == worker_id,
            NurseReviewTicket.status.in_(_OPEN_TICKET_STATUSES),
        )
    )
    ticket = res.scalar_one_or_none()
    if ticket:
        ticket.status = new_status


async def approve_worker_profile(
    db: AsyncSession, worker_id: UUID, changed_by: Optional[UUID] = None
) -> WorkerProfile:
    """Run the full worker-approval flow. Raises HTTPException on failure.

    Does NOT commit — caller commits (so it can be composed with other
    changes, e.g. a ticket status update, in one transaction).
    """
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    if wp.onboarding_status == WorkerOnboardingStatus.approved:
        # Already approved (e.g. admin approved first, ticket update arrives
        # second) — nothing left to do, but keep it idempotent.
        return wp
    if wp.onboarding_status != WorkerOnboardingStatus.pending_review:
        raise HTTPException(status_code=409, detail="Worker has not submitted onboarding for review")

    docs_res = await db.execute(select(WorkerDocument).where(WorkerDocument.worker_id == wp.id))
    docs = list(docs_res.scalars().all())
    required = _required_docs_for_type(getattr(wp, "worker_type", WorkerType.nurse))
    verified_types = {
        d.document_type
        for d in docs
        if d.verification_status == "verified"
        and (d.valid_until is None or d.valid_until >= date.today())
    }
    missing_verified = sorted(required - verified_types)
    if missing_verified:
        raise HTTPException(
            status_code=409,
            detail={"message": "Required documents are not verified", "documents": missing_verified},
        )
    if wp.background_check_status != "passed":
        raise HTTPException(status_code=409, detail="Background check has not passed")

    prev_status = wp.onboarding_status.value
    wp.onboarding_status = WorkerOnboardingStatus.approved
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = None

    # ACTIVATE the account: the worker was held in `onboarding` until now.
    ures = await db.execute(select(User).where(User.id == wp.user_id))
    wuser = ures.scalar_one_or_none()
    if wuser and wuser.status == UserStatus.onboarding:
        wuser.status = UserStatus.active

    # Award the worker's tier badge on first approval (skill-based badge).
    from app.services.badges import award_tier_badge
    await award_tier_badge(db, wp)

    # Create/refresh WorkerServiceQualification rows so tier-gated
    # services/packages unlock immediately instead of staying locked.
    from app.services.qualification import sync_tier_qualifications
    await sync_tier_qualifications(db, wp)

    # Keep the reviewer-queue ticket in sync so the card leaves "PENDING REVIEW".
    await sync_ticket_status(db, wp.id, "APPROVED")
    await _record_status_change(
        db, wp.id, prev_status, "approved", ProviderStatusChangeReason.admin_approved, changed_by,
    )
    return wp


async def reject_worker_profile(
    db: AsyncSession, worker_id: UUID, reason: Optional[str], changed_by: Optional[UUID] = None
) -> WorkerProfile:
    """Run the full worker-rejection flow. Raises HTTPException on failure.

    Does NOT commit — caller commits.
    """
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    prev_status = wp.onboarding_status.value
    wp.onboarding_status = WorkerOnboardingStatus.rejected
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = (reason or "").strip() or "Rejected during review"
    wp.availability = "offline"
    await sync_ticket_status(db, wp.id, "REJECTED")
    await _record_status_change(
        db, wp.id, prev_status, "rejected", ProviderStatusChangeReason.admin_rejected,
        changed_by, notes=wp.onboarding_rejection_reason,
    )
    return wp