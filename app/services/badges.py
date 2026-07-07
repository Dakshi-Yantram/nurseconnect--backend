"""Skill-based badge awarding.

Badges are the worker-facing representation of proven skill:
  - a TIER badge when onboarding is approved (their base skill level), and
  - an ASSESSMENT_<code> badge each time they pass a GRE-style assessment.

Badges are additive and idempotent: awarding the same code twice just
refreshes it instead of creating duplicates (enforced by the unique index
on (worker_id, code)).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AssessmentModule, WorkerAssessmentAttempt, WorkerBadge, WorkerProfile


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _upsert_badge(
    db: AsyncSession,
    worker: WorkerProfile,
    *,
    code: str,
    label: str,
    source: str,
    tier=None,
    service_scope: Optional[list[str]] = None,
) -> WorkerBadge:
    res = await db.execute(
        select(WorkerBadge).where(
            WorkerBadge.worker_id == worker.id, WorkerBadge.code == code
        )
    )
    badge = res.scalar_one_or_none()
    if badge is None:
        badge = WorkerBadge(
            worker_id=worker.id,
            code=code,
            label=label,
            source=source,
            tier=tier,
            service_scope=service_scope,
        )
        db.add(badge)
    else:
        badge.label = label
        badge.source = source
        badge.tier = tier
        badge.service_scope = service_scope
        badge.revoked_at = None
        badge.awarded_at = _now()
    await db.flush()
    return badge


async def award_tier_badge(db: AsyncSession, worker: WorkerProfile) -> Optional[WorkerBadge]:
    """Award the worker's current tier as a badge (their base skill level)."""
    if not worker.tier:
        return None
    tier_num = worker.tier.value.replace("tier", "Tier ")
    return await _upsert_badge(
        db,
        worker,
        code=worker.tier.value.upper(),        # e.g. TIER3
        label=f"{tier_num} Certified",
        source="tier",
        tier=worker.tier,
    )


async def award_assessment_badge(
    db: AsyncSession,
    worker: WorkerProfile,
    assessment: AssessmentModule,
    attempt: WorkerAssessmentAttempt,
) -> Optional[WorkerBadge]:
    """Award a skill badge for a passed assessment. No-op if not passed."""
    if not attempt.passed:
        return None
    scope = list(getattr(assessment, "unlocks_service_codes", None) or [])
    return await _upsert_badge(
        db,
        worker,
        code=f"ASSESSMENT_{assessment.code}".upper(),
        label=f"{assessment.title or assessment.code} — Passed",
        source="assessment",
        service_scope=scope or None,
    )


def serialize_badge(b: WorkerBadge) -> dict:
    return {
        "id": str(b.id),
        "code": b.code,
        "label": b.label,
        "source": b.source,
        "tier": b.tier.value if b.tier else None,
        "service_scope": b.service_scope or [],
        "awarded_at": b.awarded_at.isoformat() if b.awarded_at else None,
        "revoked": b.revoked_at is not None,
    }
