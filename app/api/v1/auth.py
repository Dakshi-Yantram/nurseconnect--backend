"""Auth endpoints: email signup/login, refresh, me."""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.enums import UserRole, UserStatus
from app.models.models import (
    ConsumerProfile,
    EmailVerificationCode,
    User,
    UserSession,
    WorkerProfile,
)
from app.schemas.schemas import (
    AuthResponse,
    PasswordLoginRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendEmailVerificationRequest,
    TokenPair,
    UserOut,
    VerifyEmailRequest,
)
from app.services.common_services import audit
from app.services.email_service import send_verification_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "")
    if not p.startswith("+"):
        # Assume India +91 if 10 digits
        if len(p) == 10 and p.isdigit():
            p = f"+91{p}"
        else:
            p = f"+{p}"
    return p


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _validate_signup_role(role: UserRole) -> None:
    if role not in (UserRole.consumer, UserRole.worker):
        raise HTTPException(status_code=400, detail="Only consumer and worker accounts can self-register")


def _validate_password(password: str) -> None:
    if (
        len(password) < 8
        or len(password.encode("utf-8")) > 72
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"\d", password)
    ):
        raise HTTPException(
            status_code=400,
            detail="Password must be 8-72 bytes and include uppercase, lowercase, and a number",
        )


async def _ensure_role_profile(db: AsyncSession, user: User) -> None:
    if user.role == UserRole.consumer:
        res = await db.execute(select(ConsumerProfile).where(ConsumerProfile.user_id == user.id))
        if not res.scalar_one_or_none():
            db.add(ConsumerProfile(user_id=user.id))
    elif user.role == UserRole.worker:
        res = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == user.id))
        if not res.scalar_one_or_none():
            db.add(WorkerProfile(user_id=user.id))
    await db.flush()


async def _create_email_verification(db: AsyncSession, user: User) -> str:
    code = (
        settings.EMAIL_DEV_FIXED_CODE
        if settings.EMAIL_DEV_MODE
        else f"{secrets.randbelow(1000000):06d}"
    )
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.EMAIL_VERIFICATION_EXPIRE_MINUTES
    )
    db.add(
        EmailVerificationCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_password(code),
            expires_at=expires_at,
        )
    )
    await db.flush()
    return code


async def _persist_session(
    db: AsyncSession,
    user: User,
    tokens: TokenPair,
    device_id: str | None = None,
    device_platform: str | None = None,
    fcm_token: str | None = None,
) -> None:
    refresh_payload = decode_token(tokens.refresh_token)
    db.add(
        UserSession(
            user_id=user.id,
            refresh_token_jti=refresh_payload["jti"],
            device_id=device_id,
            device_platform=device_platform,
            fcm_token=fcm_token,
            expires_at=datetime.fromtimestamp(refresh_payload["exp"], tz=timezone.utc),
        )
    )


def _issue_token_pair(user: User, claims_extra: dict | None = None) -> TokenPair:
    extras = {"role": user.role.value}
    if claims_extra:
        extras.update(claims_extra)
    access = create_access_token(str(user.id), extras)
    refresh = create_refresh_token(str(user.id), extras)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Create a pending account and send a code to the supplied email."""
    _validate_signup_role(payload.role)
    _validate_password(payload.password)
    email = _normalize_email(str(payload.email))
    phone = _normalize_phone(payload.phone_e164)

    email_res = await db.execute(select(User).where(User.email == email))
    existing_email = email_res.scalar_one_or_none()
    if existing_email and existing_email.status == UserStatus.active:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    phone_res = await db.execute(select(User).where(User.phone_e164 == phone))
    existing_phone = phone_res.scalar_one_or_none()
    if existing_phone and existing_phone is not existing_email:
        raise HTTPException(status_code=409, detail="An account with this phone number already exists")

    user = existing_email
    if not user:
        user = User(
            phone_e164=phone,
            email=email,
            full_name=payload.full_name.strip(),
            role=payload.role,
            status=UserStatus.pending_verification,
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        await db.flush()
    else:
        user.phone_e164 = phone
        user.full_name = payload.full_name.strip()
        user.role = payload.role
        user.password_hash = hash_password(payload.password)

    # For workers, create their profile now with the chosen worker_type
    # (nurse vs caregiver) so the correct required-documents set applies.
    if user.role == UserRole.worker:
        from app.models.enums import WorkerType
        wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == user.id))
        wp = wres.scalar_one_or_none()
        wtype = payload.worker_type or WorkerType.nurse
        if not wp:
            db.add(WorkerProfile(user_id=user.id, worker_type=wtype))
        else:
            wp.worker_type = wtype
        await db.flush()

    code = await _create_email_verification(db, user)
    await audit(db, user.id, user.role.value, "auth.register", "user", user.id)
    await db.commit()
    await send_verification_email(email, code)
    return RegisterResponse(
        registered=True,
        email=email,
        expires_in_seconds=settings.EMAIL_VERIFICATION_EXPIRE_MINUTES * 60,
        dev_verification_code=code if settings.EMAIL_DEV_MODE else None,
    )


@router.post("/verify-email")
async def verify_email(payload: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    email = _normalize_email(str(payload.email))
    user_res = await db.execute(select(User).where(User.email == email))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid verification request")
    if user.email_verified_at:
        return {"verified": True, "role": user.role.value}

    code_res = await db.execute(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.user_id == user.id,
            EmailVerificationCode.consumed.is_(False),
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .limit(1)
    )
    verification = code_res.scalar_one_or_none()
    if not verification:
        raise HTTPException(status_code=400, detail="No active verification code")
    if verification.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Verification code expired")
    if verification.attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

    verification.attempts += 1
    if not verify_password(payload.code, verification.code_hash):
        await db.commit()
        raise HTTPException(status_code=400, detail="Invalid verification code")

    verification.consumed = True
    user.email_verified_at = datetime.now(timezone.utc)
    # Workers must pass reviewer doc-review + approval before the account opens.
    # They land in `onboarding` (restricted: upload docs / view status only).
    user.status = UserStatus.onboarding if user.role == UserRole.worker else UserStatus.active
    await _ensure_role_profile(db, user)
    await audit(db, user.id, user.role.value, "auth.email_verified", "user", user.id)
    await db.commit()
    return {"verified": True, "role": user.role.value}


@router.post("/resend-email-verification", response_model=RegisterResponse)
async def resend_email_verification(
    payload: ResendEmailVerificationRequest,
    db: AsyncSession = Depends(get_db),
):
    email = _normalize_email(str(payload.email))
    user_res = await db.execute(select(User).where(User.email == email))
    user = user_res.scalar_one_or_none()
    if not user or user.email_verified_at:
        raise HTTPException(status_code=400, detail="Account does not require email verification")
    code = await _create_email_verification(db, user)
    await db.commit()
    await send_verification_email(email, code)
    return RegisterResponse(
        registered=True,
        email=email,
        expires_in_seconds=settings.EMAIL_VERIFICATION_EXPIRE_MINUTES * 60,
        dev_verification_code=code if settings.EMAIL_DEV_MODE else None,
    )


@router.post("/login", response_model=AuthResponse)
async def login(payload: PasswordLoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate a verified account using email and password."""
    email = _normalize_email(str(payload.email))
    ures = await db.execute(select(User).where(User.email == email))
    user = ures.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.email_verified_at:
        raise HTTPException(status_code=403, detail="Verify your email before signing in")
    if user.status not in (UserStatus.active, UserStatus.onboarding):
        raise HTTPException(status_code=403, detail="Account is not active")

    user.last_login_at = datetime.now(timezone.utc)
    tokens = _issue_token_pair(user)
    await _persist_session(
        db,
        user,
        tokens,
        device_id=payload.device_id,
        device_platform=payload.device_platform,
        fcm_token=payload.fcm_token,
    )
    await audit(db, user.id, user.role.value, "auth.password_login", "user", user.id)
    await db.commit()
    await db.refresh(user)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        claims = decode_token(payload.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if claims.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")
    jti = claims.get("jti")
    sres = await db.execute(select(UserSession).where(UserSession.refresh_token_jti == jti))
    session = sres.scalar_one_or_none()
    if not session or session.revoked or session.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired or revoked")

    ures = await db.execute(select(User).where(User.id == session.user_id))
    user = ures.scalar_one()
    if user.status not in (UserStatus.active, UserStatus.onboarding):
        raise HTTPException(status_code=403, detail="Account is not active")
    tokens = _issue_token_pair(user)
    # Rotate: revoke old, persist new
    session.revoked = True
    new_payload = decode_token(tokens.refresh_token)
    db.add(UserSession(
        user_id=user.id,
        refresh_token_jti=new_payload["jti"],
        device_id=session.device_id,
        device_platform=session.device_platform,
        fcm_token=session.fcm_token,
        expires_at=datetime.fromtimestamp(new_payload["exp"], tz=timezone.utc),
    ))
    await db.commit()
    return tokens


@router.post("/logout")
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        claims = decode_token(payload.refresh_token)
        jti = claims.get("jti")
        await db.execute(update(UserSession).where(UserSession.refresh_token_jti == jti).values(revoked=True))
        await db.commit()
    except ValueError:
        pass
    return {"logged_out": True}


@router.get("/me", response_model=UserOut)
async def me(current: CurrentUser = Depends(get_current_user)):
    return UserOut.model_validate(current.user)