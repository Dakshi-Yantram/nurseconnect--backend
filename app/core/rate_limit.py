"""Redis-backed brute-force protection for authentication endpoints.

Two primitives:

  enforce_rate_limit(scope, identifier, ...)
      Fixed-window request throttle. Counts EVERY call; raises 429 once the
      window's budget is spent. Use for per-IP throttling and SMS-send caps.

  register_failure / clear_failures / ensure_not_locked
      Failed-attempt lockout for a specific account. Only failures count;
      a successful login clears the counter. Once the threshold is hit the
      account identifier is locked for the window regardless of further
      attempts (so an attacker can't keep probing at the throttle rate).

Fail-open by design: if Redis is unreachable we let the request through
rather than taking down all logins — the same trade-off the visit-OTP flow
already makes. Keys are namespaced ``rl:{scope}:{identifier}``.
"""
import logging

from fastapi import HTTPException, Request

from app.core.redis_client import redis_client

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the proxy chain (CloudFront/ELB set
    X-Forwarded-For; the first entry is the original client)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _key(scope: str, identifier: str) -> str:
    return f"rl:{scope}:{identifier}"


async def enforce_rate_limit(
    scope: str,
    identifier: str,
    max_attempts: int,
    window_seconds: int,
    message: str = "Too many attempts. Please try again later.",
) -> None:
    """Increment the counter for (scope, identifier); 429 once over budget."""
    key = _key(scope, identifier)
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, window_seconds)
        if count > max_attempts:
            ttl = await redis_client.ttl(key)
            raise HTTPException(
                status_code=429,
                detail={
                    "success": False,
                    "code": "RATE_LIMITED",
                    "message": message,
                    "retry_after_seconds": max(ttl, 1),
                },
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — Redis down: fail open, log loudly.
        logger.exception("rate limiter unavailable for scope=%s", scope)


async def ensure_not_locked(scope: str, identifier: str) -> None:
    """403-style 429 if the identifier is currently locked out."""
    try:
        locked = await redis_client.get(_key(f"{scope}:lock", identifier))
    except Exception:  # noqa: BLE001
        logger.exception("lockout check unavailable for scope=%s", scope)
        return
    if locked:
        ttl = await redis_client.ttl(_key(f"{scope}:lock", identifier))
        raise HTTPException(
            status_code=429,
            detail={
                "success": False,
                "code": "ACCOUNT_TEMPORARILY_LOCKED",
                "message": "Too many failed attempts. The account is temporarily locked — try again later or reset your password.",
                "retry_after_seconds": max(ttl, 1),
            },
        )


async def register_failure(
    scope: str,
    identifier: str,
    max_failures: int,
    lock_seconds: int,
) -> None:
    """Record one failed attempt; lock the identifier once over threshold."""
    key = _key(f"{scope}:fail", identifier)
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, lock_seconds)
        if count >= max_failures:
            await redis_client.setex(_key(f"{scope}:lock", identifier), lock_seconds, "1")
            await redis_client.delete(key)
    except Exception:  # noqa: BLE001
        logger.exception("failure counter unavailable for scope=%s", scope)


async def clear_failures(scope: str, identifier: str) -> None:
    try:
        await redis_client.delete(_key(f"{scope}:fail", identifier))
    except Exception:  # noqa: BLE001
        pass
