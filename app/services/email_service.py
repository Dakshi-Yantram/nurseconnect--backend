import logging

import resend

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_verification_email(email: str, code: str) -> None:
    """Send a verification code to the user's email.

    Uses Resend's HTTPS API rather than raw SMTP sockets — Render's free
    tier blocks outbound connections on SMTP ports (25/465/587), so a
    direct smtplib connection always fails with
    `OSError: [Errno 101] Network is unreachable`. Resend sends over
    HTTPS (port 443), which is never blocked.
    """
    if settings.EMAIL_DEV_MODE or not settings.RESEND_API_KEY:
        logger.info("DEV email verification email=%s code=%s", email, code)
        return

    resend.api_key = settings.RESEND_API_KEY

    subject = "Verify your NurseConnect account"
    text_body = (
        f"Your verification code is: {code}\n\n"
        f"This code expires in {settings.EMAIL_VERIFICATION_EXPIRE_MINUTES} minutes."
    )
    html_body = (
        f"<p>Your verification code is: <strong>{code}</strong></p>"
        f"<p>This code expires in {settings.EMAIL_VERIFICATION_EXPIRE_MINUTES} minutes.</p>"
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
    except Exception:
        # Log the failure but don't crash the registration flow on a
        # transient email-provider error — the user can still use
        # "resend verification code" if the email genuinely never arrives.
        logger.exception("Failed to send verification email to %s via Resend", email)