"""Dynamic form schemas.

Clients ask the server what to render rather than hardcoding a form per
role. Adding a provider type, or moving a field, is then a backend change
that every client picks up without a release.

    GET /forms/onboarding/{provider_type}   registration form for a type
    GET /forms/profile/{provider_type}      editable profile form
    GET /forms/me                           the form for the caller's own type
    GET /forms/provider-types               types a new applicant may choose
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.provider_types import (
    PROVIDER_TYPE_LABELS,
    is_physical_capable,
    is_tele_capable,
)
from app.models.enums import WorkerType
from app.models.models import WorkerProfile
from app.services.form_schema import onboarding_form_schema, profile_form_schema

router = APIRouter(prefix="/forms", tags=["forms"])


def _parse_type(raw: str) -> WorkerType:
    try:
        return WorkerType(raw.strip().lower())
    except ValueError:
        valid = ", ".join(t.value for t in WorkerType)
        raise HTTPException(
            status_code=404,
            detail=f"Unknown provider type '{raw}'. Expected one of: {valid}",
        ) from None


@router.get("/provider-types")
async def list_provider_types():
    """Provider types an applicant can register as, with their capabilities.

    Drives the role picker. `capabilities` lets a client preview what the
    role involves (video consultations vs home visits) without knowing
    anything role-specific itself.
    """
    return [
        {
            "value": t.value,
            "label": PROVIDER_TYPE_LABELS.get(t, t.value),
            "capabilities": {
                "tele": is_tele_capable(t),
                "physical": is_physical_capable(t),
            },
        }
        # `doctor` is the pre-split generic type. It still works for every
        # existing account, but new applicants should pick a specific mode,
        # so it is not offered here.
        for t in WorkerType
        if t is not WorkerType.doctor
    ]


@router.get("/onboarding/{provider_type}")
async def onboarding_form(provider_type: str):
    return onboarding_form_schema(_parse_type(provider_type))


@router.get("/profile/{provider_type}")
async def profile_form(provider_type: str):
    return profile_form_schema(_parse_type(provider_type))


@router.get("/me")
async def my_form(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The profile form for the signed-in provider's own type."""
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    worker = res.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail="No provider profile for this account")
    return profile_form_schema(worker.worker_type)
