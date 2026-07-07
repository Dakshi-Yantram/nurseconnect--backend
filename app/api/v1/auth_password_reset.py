"""Forgot / reset password — self-contained router.

Flow (SMS-based, reusing the same Redis + MSG91 infra as the visit OTP):
  1. POST /auth/forgot-password  { email }
     -> if the account exists, a 6-digit code is SMSed to the registered phone.
        Always returns 200 (never reveals whether an email is registered).
  2. POST /auth/reset-password   { email, code, new_password }
     -> verifies the code and sets the new password.

Mount in app/main.py alongside the other routers:
    from app.api.v1 import auth_password_reset
    ... app.include_router(auth_password_reset.router, prefix=_API_PREFIX)
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis_client import redis_client
from app.core.security import hash_password
from app.integrations import msg91_client
from app.models.models import User

router = APIRouter(tags=["auth"])

_RESET_TTL_SECONDS = 600          # 10 minutes
_MAX_ATTEMPTS = 5


def _code_key(user_id) -> str:
    return f"pwreset:code:{user_id}"


def _attempts_key(user_id) -> str:
    return f"pwreset:attempts:{user_id}"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


@router.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = res.scalar_one_or_none()

    # Only act if the account exists — but always return the same response so
    # attackers can't enumerate registered emails.
    if user and user.phone_e164:
        code = f"{secrets.randbelow(1_000_000):06d}"
        await redis_client.setex(_code_key(user.id), _RESET_TTL_SECONDS, code)
        await redis_client.delete(_attempts_key(user.id))
        try:
            await msg91_client.send_sms(
                user.phone_e164,
                f"Your NurseConnect password reset code is {code}. It expires in 10 minutes.",
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "sent": True,
        "message": "If that account exists, a reset code has been sent to the registered phone number.",
    }


@router.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    res = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid code or email")

    attempts_raw = await redis_client.get(_attempts_key(user.id))
    attempts = int(attempts_raw) if attempts_raw else 0
    if attempts >= _MAX_ATTEMPTS:
        await redis_client.delete(_code_key(user.id))
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

    stored = await redis_client.get(_code_key(user.id))
    if not stored:
        raise HTTPException(status_code=400, detail="Code expired. Request a new one.")

    stored_code = stored.decode() if isinstance(stored, (bytes, bytearray)) else str(stored)
    if payload.code.strip() != stored_code:
        await redis_client.setex(_attempts_key(user.id), _RESET_TTL_SECONDS, attempts + 1)
        raise HTTPException(status_code=400, detail="Invalid code")

    # Success — set the new password and clear the code.
    user.password_hash = hash_password(payload.new_password)
    await db.commit()
    await redis_client.delete(_code_key(user.id))
    await redis_client.delete(_attempts_key(user.id))
    return {"reset": True, "message": "Password updated. You can now sign in."}
