"""Application configuration."""
from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    APP_NAME: str = "NurseConnect"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str
    DATABASE_URL_SYNC: str

    # Redis
    REDIS_URL: str

    # Celery
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # OTP
    OTP_DEV_MODE: bool = True

    # OCR (degree/license name auto-extraction for onboarding + contracts)
    OCR_PROVIDER: str = ""  # "google_vision" | "tesseract" | "" (disabled)
    GOOGLE_VISION_API_KEY: str = ""
    OTP_DEV_FIXED_CODE: str = "123456"
    OTP_EXPIRE_MINUTES: int = 5

    # Email verification
    EMAIL_VERIFICATION_EXPIRE_MINUTES: int = 15
    EMAIL_DEV_MODE: bool = True
    EMAIL_DEV_FIXED_CODE: str = "654321"

    # Legacy SMTP settings â€” kept for reference / fallback. Render's free
    # tier blocks outbound SMTP ports (25/465/587), so these are unused
    # by email_service.py now in favour of the Resend HTTP API below.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@nurseconnect.app"
    SMTP_FROM_NAME: str = "NurseConnect"
    SMTP_USE_TLS: bool = True

    # Resend (transactional email over HTTPS â€” works on Render free tier)
    RESEND_API_KEY: str = ""
    EMAIL_FROM_ADDRESS: str = "onboarding@resend.dev"

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ---------------------------------------------------------------------
    # Worker payouts.
    #
    # When a visit is completed a payout is generated for the nurse:
    #   gross  = booking base + surge amount (the service value)
    #   comm   = gross * commission%  (from the service/package, or the
    #            platform default below when the offering doesn't set one)
    #   tds    = (gross - comm) * TDS%   (India: 194O e-commerce, often 1%)
    #   net    = gross - comm - tds
    #
    # The payout is created as `pending`. Admin reviews and processes it â€”
    # optionally auto-transferring via RazorpayX when RAZORPAYX_* is set and
    # the nurse has bank details on file. Nothing leaves the platform without
    # an admin action, which is what marketplaces want for hold/dispute control.
    # ---------------------------------------------------------------------
    PLATFORM_COMMISSION_PCT: float = 20.0
    PLATFORM_TDS_PCT: float = 0.0
    ONBOARDING_ENABLEMENT_FEE: float = 200.0  # â‚¹ total collected over several bookings once Stage 2 is e-signed
    # Spread the onboarding fee across bookings instead of taking it all from
    # booking #1 â€” deduct this much per completed booking's payout until the
    # running total reaches ONBOARDING_ENABLEMENT_FEE. â‚¹50/booking by default
    # (4 bookings to clear â‚¹200); raise to 100 for a faster 2-booking payoff.
    ONBOARDING_FEE_INCREMENT: float = 50.0

    # App URL used to build the public e-prescription verification link
    # embedded in the Rx PDF's QR code (e.g. https://app.nurseconnect.in).
    PUBLIC_APP_URL: str = "https://app.nurseconnect.in"

    # RazorpayX (payouts) â€” separate product from Razorpay payments above.
    # Leave blank to keep payouts manual (admin marks them paid after an
    # out-of-band bank transfer). When set, admin "process" attempts a real
    # RazorpayX transfer to the nurse's fund account.
    RAZORPAYX_ACCOUNT_NUMBER: str = ""

    # Cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # MSG91
    MSG91_AUTH_KEY: str = ""
    MSG91_SENDER_ID: str = "NRSCNC"
    MSG91_TEMPLATE_ID: str = ""

    # Interakt
    INTERAKT_API_KEY: str = ""
    INTERAKT_BASE_URL: str = "https://api.interakt.ai"
    # Shared secret configured in the Interakt dashboard (Settings > Webhooks)
    # so we can verify inbound webhook calls actually come from Interakt.
    INTERAKT_WEBHOOK_SECRET: str = ""
    # WhatsApp template used to ask the family for feedback right after a
    # visit is checked out. Must be a pre-approved template on Interakt.
    INTERAKT_FEEDBACK_TEMPLATE: str = "service_feedback_request"

    # Deep link base the family taps from the WhatsApp feedback message.
    FEEDBACK_LINK_BASE_URL: str = "https://app.nurseconnect.in/feedback"

    # Firebase
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # ABHA
    ABHA_BASE_URL: str = ""
    ABHA_CLIENT_ID: str = ""
    ABHA_CLIENT_SECRET: str = ""

    # Cloudflare RealtimeKit (in-app voice calling) â€” replaces Dyte.
    #
    # As of the Cloudflare-native integration, the old Dyte-style
    # "org_id : api_key" Basic-auth scheme against api.realtime.cloudflare.com/v2
    # no longer applies to new accounts (that developer portal has been
    # retired). RealtimeKit now lives under the standard Cloudflare API:
    #   https://api.cloudflare.com/client/v4/accounts/{account_id}/realtime/kit/{app_id}/...
    # authenticated with a Cloudflare API Token (Bearer), scoped to the
    # "Realtime / Realtime Admin" permission.
    REALTIMEKIT_ACCOUNT_ID: str = ""
    REALTIMEKIT_APP_ID: str = ""
    REALTIMEKIT_API_TOKEN: str = ""
    REALTIMEKIT_BASE_URL: str = "https://api.cloudflare.com/client/v4"
    # Deprecated Dyte-era fields â€” kept only so a pre-migration .env doesn't
    # crash on load. No longer read by RealtimeKitClient.
    REALTIMEKIT_ORG_ID: str = ""
    REALTIMEKIT_API_KEY: str = ""
    DYTE_ORG_ID: str = ""
    DYTE_API_KEY: str = ""
    DYTE_BASE_URL: str = ""

    # Web Push (VAPID) â€” best-effort background call ping for browser tabs.
    # NOTE: this does NOT wake a fully force-killed browser; only the native
    # PushKit / FCM paths below can ring a killed mobile app.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:support@nurseconnect.app"

    # ---------------------------------------------------------------------
    # APNs â€” iOS VoIP (PushKit) push.
    #
    # This is what lets a *force-killed* iOS app ring. It uses token-based
    # auth: download a .p8 key from the Apple Developer portal (Keys â†’ new key
    # with "Apple Push Notifications service" enabled) and set the three
    # values below. APNS_KEY_P8 accepts either the PEM contents directly or a
    # path to the .p8 file.
    #
    # The push topic is always "<APNS_BUNDLE_ID>.voip" â€” Apple requires the
    # .voip suffix for PushKit, and rejects the plain bundle id.
    # ---------------------------------------------------------------------
    APNS_KEY_P8: str = ""
    APNS_KEY_ID: str = ""
    APNS_TEAM_ID: str = ""
    APNS_BUNDLE_ID: str = "com.yantrammedtech.nurseconnect"
    # Apple has separate hosts for sandbox (dev builds) and production
    # (TestFlight / App Store). A token minted for one is rejected by the
    # other, so this must match how the installed app was signed.
    APNS_USE_SANDBOX: bool = True

    # Mocks
    MOCK_EXTERNAL_PROVIDERS: bool = True

    # CORS
    CORS_ORIGINS: str = "*"

    @property
    def cors_origin_list(self) -> List[str]:
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
