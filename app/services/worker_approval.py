"""Single source of truth for approving / rejecting a worker's onboarding.

Both admin approval and reviewer-ticket approval use these functions so that
worker onboarding status, user status, qualifications, badges, and reviewer
tickets stay synchronized.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.provider_types import required_docs as _required_docs_for_type
from app.models.enums import (
    ProviderStatusChangeReason,
    UserStatus,
    WorkerOnboardingStatus,
    WorkerType,
)
from app.models.models import (
    NurseReviewTicket,
    ProviderStatusHistory,
    User,
    WorkerDocument,
    WorkerProfile,
)


# Ticket statuses that are still open in the reviewer queue.
_OPEN_TICKET_STATUSES = (
    "PENDING_REVIEW",
    "IN_REVIEW",
    "NEEDS_CLARIFICATION",
    "UNASSIGNED",
)


REQUIRED_DOCUMENTS_BY_WORKER_TYPE = {
    WorkerType.nurse: {
        "aadhaar",
        "nursing_license",
        "degree_certificate",
        "police_verification",
    },
    WorkerType.caregiver: {
        "aadhaar",
        "police_verification",
    },
}


async def _record_status_change(
    db: AsyncSession,
    worker_id: UUID,
    from_status: Optional[str],
    to_status: str,
    reason: ProviderStatusChangeReason,
    changed_by: Optional[UUID] = None,
    notes: Optional[str] = None,
) -> None:
    db.add(
        ProviderStatusHistory(
            worker_id=worker_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            changed_by=changed_by,
            notes=notes,
        )
    )


async def sync_ticket_status(
    db: AsyncSession,
    worker_id: UUID,
    new_status: str,
) -> None:
    """Keep the reviewer queue ticket in sync with worker status."""

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
    db: AsyncSession,
    worker_id: UUID,
    changed_by: Optional[UUID] = None,
) -> WorkerProfile:
    """Run the complete worker approval flow.

    Does NOT commit. The caller is responsible for committing.
    """

    res = await db.execute(
        select(WorkerProfile).where(
            WorkerProfile.id == worker_id
        )
    )

    wp = res.scalar_one_or_none()

    if not wp:
        raise HTTPException(
            status_code=404,
            detail="Worker not found",
        )

    # Already approved = idempotent success.
    if wp.onboarding_status == WorkerOnboardingStatus.approved:
        return wp

    if wp.onboarding_status != WorkerOnboardingStatus.pending_review:
        raise HTTPException(
            status_code=409,
            detail="Worker has not submitted onboarding for review",
        )

    # Get worker documents.
    docs_res = await db.execute(
        select(WorkerDocument).where(
            WorkerDocument.worker_id == wp.id
        )
    )

    docs = list(docs_res.scalars().all())

    # Determine required documents.
    worker_type = getattr(
        wp,
        "worker_type",
        WorkerType.nurse,
    )

    required = REQUIRED_DOCUMENTS_BY_WORKER_TYPE.get(
        worker_type,
        REQUIRED_DOCUMENTS_BY_WORKER_TYPE[WorkerType.nurse],
    )

    verified_types = {
        d.document_type
        for d in docs
        if d.verification_status == "verified"
        and (
            d.valid_until is None
            or d.valid_until >= date.today()
        )
    }

    missing_verified = sorted(
        required - verified_types
    )

    if missing_verified:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Required documents are not verified",
                "documents": missing_verified,
            },
        )

    # Background check must pass.
    if wp.background_check_status != "passed":
        raise HTTPException(
            status_code=409,
            detail="Background check has not passed",
        )

    # Save previous status for history.
    prev_status = wp.onboarding_status.value

    # Approve worker.
    wp.onboarding_status = WorkerOnboardingStatus.approved
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)
    wp.onboarding_rejection_reason = None

    # Activate user account.
    ures = await db.execute(
        select(User).where(
            User.id == wp.user_id
        )
    )

    wuser = ures.scalar_one_or_none()

    if wuser and wuser.status == UserStatus.onboarding:
        wuser.status = UserStatus.active

    # Award tier badge.
    from app.services.badges import award_tier_badge

    await award_tier_badge(
        db,
        wp,
    )

    # Sync tier qualifications.
    from app.services.qualification import sync_tier_qualifications

    await sync_tier_qualifications(
        db,
        wp,
    )

    # Sync reviewer ticket.
    await sync_ticket_status(
        db,
        wp.id,
        "APPROVED",
    )

    # Record status change.
    await _record_status_change(
        db,
        wp.id,
        prev_status,
        "approved",
        ProviderStatusChangeReason.admin_approved,
        changed_by,
    )

    return wp


async def reject_worker_profile(
    db: AsyncSession,
    worker_id: UUID,
    reason: Optional[str],
    changed_by: Optional[UUID] = None,
) -> WorkerProfile:
    """Run the complete worker rejection flow.

    Does NOT commit. The caller is responsible for committing.
    """

    res = await db.execute(
        select(WorkerProfile).where(
            WorkerProfile.id == worker_id
        )
    )

    wp = res.scalar_one_or_none()

    if not wp:
        raise HTTPException(
            status_code=404,
            detail="Worker not found",
        )

    prev_status = wp.onboarding_status.value

    wp.onboarding_status = WorkerOnboardingStatus.rejected
    wp.onboarding_reviewed_at = datetime.now(timezone.utc)

    wp.onboarding_rejection_reason = (
        (reason or "").strip()
        or "Rejected during review"
    )

    wp.availability = "offline"

    # Sync reviewer ticket.
    await sync_ticket_status(
        db,
        wp.id,
        "REJECTED",
    )

    # Record status change.
    await _record_status_change(
        db,
        wp.id,
        prev_status,
        "rejected",
        ProviderStatusChangeReason.admin_rejected,
        changed_by,
        notes=wp.onboarding_rejection_reason,
    )

    return wp