"""Patch 5B — Security-sensitive audit logging.

Thin wrapper over the existing ``app.services.common_services.audit`` helper
that writes to ``AuditLog``. Centralizes the action codes for the events
the patch requires so they can be queried as a coherent stream:

  * security.access_denied
  * security.ownership_violation
  * security.ws_unauthorized
  * security.insurance_override

Storage shape (AuditLog row):
  actor_id      → current.id (or None for anonymous/ws rejects)
  actor_type    → role.value (or "anonymous")
  action        → security.*
  entity_type   → e.g. "booking" | "patient" | "insurance_assessment" | "ws"
  entity_id     → string form of the entity UUID (or None)
  changes       → free-form JSON payload (endpoint, reason, etc.)

The function intentionally swallows DB errors so a logging failure can
never bubble up and block the security check it is recording.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.common_services import audit

logger = logging.getLogger(__name__)


ACTION_ACCESS_DENIED = "security.access_denied"
ACTION_OWNERSHIP_VIOLATION = "security.ownership_violation"
ACTION_WS_UNAUTHORIZED = "security.ws_unauthorized"
ACTION_INSURANCE_OVERRIDE = "security.insurance_override"


async def _safe_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    actor_type: str,
    action: str,
    entity_type: Optional[str],
    entity_id: Optional[Any],
    changes: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        await audit(
            db,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=changes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("security_audit_service failed to record %s: %s", action, exc)


async def log_access_denied(
    db: AsyncSession,
    *,
    user_id: Optional[UUID],
    role: str,
    endpoint: str,
    reason: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
) -> None:
    await _safe_audit(
        db,
        actor_id=user_id,
        actor_type=role or "anonymous",
        action=ACTION_ACCESS_DENIED,
        entity_type=entity_type,
        entity_id=entity_id,
        changes={"endpoint": endpoint, "reason": reason},
    )


async def log_ownership_violation(
    db: AsyncSession,
    *,
    user_id: Optional[UUID],
    role: str,
    endpoint: str,
    entity_type: str,
    entity_id: Optional[Any],
    reason: str = "ownership_check_failed",
) -> None:
    await _safe_audit(
        db,
        actor_id=user_id,
        actor_type=role or "anonymous",
        action=ACTION_OWNERSHIP_VIOLATION,
        entity_type=entity_type,
        entity_id=entity_id,
        changes={"endpoint": endpoint, "reason": reason},
    )


async def log_ws_unauthorized(
    db: AsyncSession,
    *,
    user_id: Optional[UUID],
    role: str,
    endpoint: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    reason: str = "unauthorized_ws_subscription",
) -> None:
    await _safe_audit(
        db,
        actor_id=user_id,
        actor_type=role or "anonymous",
        action=ACTION_WS_UNAUTHORIZED,
        entity_type=entity_type or "ws",
        entity_id=entity_id,
        changes={"endpoint": endpoint, "reason": reason},
    )


async def log_insurance_override(
    db: AsyncSession,
    *,
    user_id: UUID,
    role: str,
    assessment_id: UUID,
    previous_decision: Optional[str],
    new_decision: Optional[str],
    previous_coverage_status: Optional[str],
    new_coverage_status: Optional[str],
    previous_coverage_percent: Optional[float],
    new_coverage_percent: Optional[float],
    justification: str,
) -> None:
    """Lightweight insurance override audit entry (per Patch 5B contract)."""
    await _safe_audit(
        db,
        actor_id=user_id,
        actor_type=role,
        action=ACTION_INSURANCE_OVERRIDE,
        entity_type="insurance_assessment",
        entity_id=assessment_id,
        changes={
            "assessment_id": str(assessment_id),
            "previous_decision": previous_decision,
            "new_decision": new_decision,
            "previous_coverage_status": previous_coverage_status,
            "new_coverage_status": new_coverage_status,
            "previous_coverage_percent": previous_coverage_percent,
            "new_coverage_percent": new_coverage_percent,
            "justification": justification,
        },
    )
