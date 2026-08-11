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

from app.models.enums import UserStatus, WorkerOnboardingStatus, WorkerType
from app.models.models import NurseReviewTicket, User, WorkerDocument, WorkerProfile

# Ticket statuses that are still "open" in the reviewer queue — used to find
# the live ticket for a worker when syncing status after a review action.
_OPEN_TICKET_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "NEEDS_CLARIFICATION", "UNASSIGNED")

REQUIRED_DOCUMENTS_BY_WORKER_TYPE = {
    WorkerType.nurse: {"aadhaar", "nursing_license", "degree_certificate", "police_verification"},
    WorkerType.caregiver: {"aadhaar", "police_verification"},
}


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


async def approve_worker_profile(db: AsyncSession, worker_id: UUID) -> WorkerProfile:
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
    required = REQUIRED_DOCUMENTS_BY_WORKER_TYPE.get(
        getattr(wp, "worker_type", WorkerType.nurse),
        REQUIRED_DOCUMENTS_BY_WORKER_TYPE[WorkerType.nurse],
    )
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
    return wp


async def reject_worker_profile(
    db: AsyncSession, worker_id: UUID, reason: Optional[str]
) -> WorkerProfile:
    """Run the full worker-rejection flow. Raises HTTPException on failure.

    Does NOT commit — caller commits.
    """
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.id == worker_id))
    wp = res.scalar_one_or_none()
    if not wp:
        raise HTTPException(status_code=404, detail="Worker not found")
    wp.onboarding_status = WorkerOnboardingStatus.rejected
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = (reason or "").strip() or "Rejected during review"
    wp.availability = "offline"
    await sync_ticket_status(db, wp.id, "REJECTED")
    return wp
