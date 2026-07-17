"""RBAC + auth dependencies."""
from typing import Iterable, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.enums import UserRole, UserStatus
from app.models.models import ConsumerProfile, User, WorkerProfile

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, user: User, claims: dict):
        self.user = user
        self.claims = claims
        self.id: UUID = user.id
        self.role: UserRole = user.role


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        claims = decode_token(credentials.credentials)
        print("DEBUG claims:", claims)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    if claims.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")
    res = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.status == UserStatus.suspended:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")
    if user.status == UserStatus.deactivated:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")
    if user.status == UserStatus.pending_verification:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required")
    request.state.user_id = user.id
    request.state.user_role = user.role.value
    return CurrentUser(user=user, claims=claims)


def require_roles(*roles: UserRole):
    async def _checker(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Role {current.role.value} not allowed")
        return current

    return _checker


async def get_consumer_profile(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ConsumerProfile:
    if current.role != UserRole.consumer:
        raise HTTPException(status_code=403, detail="Consumer role required")
    res = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == current.id))
    profile = res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Consumer profile not found")
    return profile


async def get_worker_profile(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> WorkerProfile:
    if current.role != UserRole.worker:
        raise HTTPException(status_code=403, detail="Worker role required")
    res = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    profile = res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Worker profile not found")
    return profile


def is_admin(role: UserRole) -> bool:
    return role == UserRole.admin


def require_admin(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not is_admin(current.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current


def require_reviewer(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    # admin is a superuser and can always do reviewer tasks
    if current.role not in (UserRole.admin, UserRole.reviewer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer or admin role required")
    return current


def require_operations(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Operations accounts, or admin (superuser)."""
    if current.role not in (UserRole.admin, UserRole.operations):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operations or admin role required")
    return current


def require_support(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Support-desk accounts, or operations/admin (who can always cover support)."""
    if current.role not in (UserRole.admin, UserRole.operations, UserRole.support):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Support role required")
    return current


def require_clinical_training_lead(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Clinical training leads (approve training content), or admin."""
    if current.role not in (UserRole.admin, UserRole.clinical_training_lead):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Clinical training lead role required")
    return current


def require_clinical_trainer(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Clinical trainers (author training content) — training leads and admin can also author."""
    if current.role not in (UserRole.admin, UserRole.clinical_training_lead, UserRole.clinical_trainer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Clinical trainer role required")
    return current


def is_staff(role: UserRole) -> bool:
    """Any internal (non-consumer, non-worker) account."""
    return role in (
        UserRole.admin,
        UserRole.reviewer,
        UserRole.operations,
        UserRole.support,
        UserRole.clinical_training_lead,
        UserRole.clinical_trainer,
    )