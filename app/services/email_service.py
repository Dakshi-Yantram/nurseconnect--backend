"""Transactional email.

Uses Resend's HTTPS API rather than raw SMTP sockets — Render's free tier
blocks outbound connections on SMTP ports (25/465/587), so a direct smtplib
connection always fails with `OSError: [Errno 101] Network is unreachable`.
Resend sends over HTTPS (port 443), which is never blocked.
"""
import logging

import resend

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_verification_email(email: str, code: str) -> bool:
    """Send a verification code. Returns True only if it was actually sent.

    The return value matters: the previous version returned None whether the
    mail went out, was skipped because dev mode was on, or was skipped
    because no API key was configured. Callers therefore reported "we've
    emailed you a code" in every one of those cases — including the one
    where nothing was sent and the user had no way to obtain a code at all.

    Dev mode is now environment-derived (`settings.email_dev_mode`), so this
    can only take the log-and-skip path outside production.
    """
    if settings.email_dev_mode:
        logger.info("DEV email verification email=%s code=%s", email, code)
        return False

    if not settings.RESEND_API_KEY:
        # Production with no mail provider: the user will never receive a
        # code. Log at ERROR so it surfaces in alerting rather than being
        # mistaken for normal behaviour, and tell the caller it failed.
        logger.error(
            "RESEND_API_KEY is not configured — verification email to %s was NOT sent",
            email,
        )
        return False

    resend.api_key = settings.RESEND_API_KEY

    minutes = settings.EMAIL_VERIFICATION_EXPIRE_MINUTES
    subject = "Verify your NurseConnect account"
    text_body = (
        f"Your verification code is: {code}\n\n"
        f"This code expires in {minutes} minutes.\n\n"
        "If you didn't request this, you can ignore this email."
    )
    html_body = (
        f"<p>Your verification code is: <strong>{code}</strong></p>"
        f"<p>This code expires in {minutes} minutes.</p>"
        "<p>If you didn't request this, you can ignore this email.</p>"
    )

    try:
        resend.Emails.send(
            {
                "from": f"{settings.SMTP_FROM_NAME} <{settings.EMAIL_FROM_ADDRESS}>",
                "to": [email],
                "subject": subject,
                "text": text_body,
                "html": html_body,
            }
        )
        return True
    except Exception:
        # Don't crash registration on a transient provider error — the
        # account exists and "resend code" can retry. But report the failure
        # so the UI can say so instead of claiming success.
        logger.exception("Failed to send verification email to %s via Resend", email)
        return False
