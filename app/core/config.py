"""Application configuration."""
import logging
from functools import lru_cache
from typing import List, Optional
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

    # Play Store reviewer access (family/consumer test number only).
    # Leave both empty to disable. Set as environment variables on the server;
    # never commit real values. Use a non-personal test number and a random OTP.
    REVIEW_TEST_PHONE: str = ""
    REVIEW_TEST_OTP: str = ""

    # Email verification
    EMAIL_VERIFICATION_EXPIRE_MINUTES: int = 15
    EMAIL_DEV_MODE: bool = True
    EMAIL_DEV_FIXED_CODE: str = "654321"

    # Legacy SMTP settings — kept for reference / fallback. Render's free
    # tier blocks outbound SMTP ports (25/465/587), so these are unused
    # by email_service.py now in favour of the Resend HTTP API below.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@nurseconnect.in"
    SMTP_FROM_NAME: str = "NurseConnect"
    SMTP_USE_TLS: bool = True

    # Resend (transactional email over HTTPS — works on Render free tier)
    RESEND_API_KEY: str = ""
    EMAIL_FROM_ADDRESS: str = "onboarding@resend.dev"

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # Digio — Aadhaar eSign on state e-Stamp paper for the Stage 2 Master
    # Agreement (see app/integrations/providers.py::DigioClient and
    # app/api/v1/contracts.py). CLIENT_ID/CLIENT_SECRET authenticate our
    # server to Digio's API; WEBHOOK_SECRET verifies that a completion
    # notification actually came from Digio rather than being forged by a
    # client that wants Stage 2 marked signed without ever signing anything.
    DIGIO_BASE_URL: str = "https://api.digio.in"
    DIGIO_CLIENT_ID: str = ""
    DIGIO_CLIENT_SECRET: str = ""
    DIGIO_WEBHOOK_SECRET: str = ""
    # Where Digio's hosted signing page redirects the signer's browser once
    # they finish (or abandon) the flow. The mobile app opens this inside an
    # in-app browser/WebView and watches for navigation to this URL to know
    # the session ended — see mobile app/(nurse)/contract.tsx.
    DIGIO_REDIRECT_URL: str = "nurseconnect://esign-complete"
    # A signing session left untouched this long is treated as abandoned so
    # it doesn't block a worker from starting a fresh one indefinitely.
    DIGIO_SESSION_EXPIRE_MINUTES: int = 60

    # ---------------------------------------------------------------------
    # Company identity printed on every invoice and payout statement.
    # Centralised here so the GSTIN/address exist in exactly one place and
    # can differ per environment — no PDF template hardcodes any of them.
    # See app/core/company.py.
    # ---------------------------------------------------------------------
    COMPANY_LEGAL_NAME: str = "YANTRAM MEDTECH PVT LTD"
    COMPANY_ADDRESS_LINE: str = "HITEC City, Hyderabad, Telangana - 500081"
    COMPANY_GSTIN: str = ""
    COMPANY_STATE_NAME: str = "Telangana"
    COMPANY_STATE_CODE: str = "36"
    COMPANY_SUPPORT_EMAIL: str = "support@nurseconnect.in"

    # Invoice / statement number prefixes. Numbers are allocated per financial
    # year per series (see app/services/billing_service.py).
    INVOICE_NUMBER_PREFIX: str = "YM-INV"
    PAYOUT_STATEMENT_PREFIX: str = "YM-COMM"
<<<<<<< HEAD
    # The patient payment receipt shares the invoice's financial-year/sequence
    # suffix (see app/services/receipt_service.py) so the two documents for
    # the same booking are trivially cross-referenced by a support agent.
    RECEIPT_NUMBER_PREFIX: str = "YM-RCPT"
=======
>>>>>>> origin/staging

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
    # The payout is created as `pending`. Admin reviews and processes it —
    # optionally auto-transferring via RazorpayX when RAZORPAYX_* is set and
    # the nurse has bank details on file. Nothing leaves the platform without
    # an admin action, which is what marketplaces want for hold/dispute control.
    # ---------------------------------------------------------------------
    PLATFORM_COMMISSION_PCT: float = 20.0
    PLATFORM_TDS_PCT: float = 0.0
    ONBOARDING_ENABLEMENT_FEE: float = 200.0  # ₹ total collected over several bookings once Stage 2 is e-signed
    # Spread the onboarding fee across bookings instead of taking it all from
    # booking #1 — deduct this much per completed booking's payout until the
    # running total reaches ONBOARDING_ENABLEMENT_FEE. ₹50/booking by default
    # (4 bookings to clear ₹200); raise to 100 for a faster 2-booking payoff.
    ONBOARDING_FEE_INCREMENT: float = 50.0

    # App URL used to build the public e-prescription verification link
    # embedded in the Rx PDF's QR code (e.g. https://app.nurseconnect.in).
    PUBLIC_APP_URL: str = "https://app.nurseconnect.in"
    # Public base URL of THIS API (e.g. the CloudFront domain). Used to build
    # absolute, short-lived visit-report download links for the mobile apps.
    # When unset, it is derived from X-Forwarded-Proto/Host on the request.
    PUBLIC_API_URL: str = ""
    # Lifetime of a one-time visit-report PDF download link.
    REPORT_DOWNLOAD_TOKEN_TTL_SECONDS: int = 60

    # RazorpayX (payouts) — separate product from Razorpay payments above.
    # Leave blank to keep payouts manual (admin marks them paid after an
    # out-of-band bank transfer). When set, admin "process" attempts a real
    # RazorpayX transfer to the nurse's fund account.
    RAZORPAYX_ACCOUNT_NUMBER: str = ""
    # RazorpayX API credentials. When blank, the payment-side RAZORPAY_KEY_*
    # pair is reused — RazorpayX accepts the same key/secret when Payouts is
    # enabled on the account, but a dedicated pair is preferred in production
    # so a leaked checkout key can't move money out.
    RAZORPAYX_KEY_ID: str = ""
    RAZORPAYX_KEY_SECRET: str = ""
    # IMPS / NEFT / RTGS / UPI / card. IMPS settles in seconds and is the
    # sensible default for per-booking nurse payouts.
    RAZORPAYX_PAYOUT_MODE: str = "IMPS"
    # "payout" processes immediately; "payout_composite" lets Razorpay resolve
    # the fund account from a VPA/bank detail payload.
    RAZORPAYX_PAYOUT_PURPOSE: str = "payout"
    # Webhook secret for payout.processed / payout.failed / payout.reversed.
    # Distinct from the payments webhook secret above — Razorpay signs each
    # webhook endpoint with its own secret.
    RAZORPAYX_WEBHOOK_SECRET: str = ""

    # Cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # MSG91
    MSG91_AUTH_KEY: str = ""
    MSG91_SENDER_ID: str = "NRSCNC"
    MSG91_TEMPLATE_ID: str = ""
    # Separate DLT template for the visit-start OTP. The visit code was
    # being relayed through MSG91_TEMPLATE_ID above, whose approved text
    # reads "Your OTP for login is ...", so patients received door codes
    # labelled as login codes. Register a visit-code template in the
    # MSG91 dashboard and set this; until then the code falls back to the
    # login template and logs a warning.
    MSG91_VISIT_OTP_TEMPLATE_ID: str = ""
    # DLT template for password-reset codes. MSG91 (India/DLT) will not
    # deliver free-text SMS, so the old send_sms() path silently dropped
    # every reset code. Falls back to MSG91_TEMPLATE_ID when unset.
    MSG91_PASSWORD_RESET_TEMPLATE_ID: str = ""

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

    # Cloudflare RealtimeKit (in-app voice calling) — replaces Dyte.
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
    # Deprecated Dyte-era fields — kept only so a pre-migration .env doesn't
    # crash on load. No longer read by RealtimeKitClient.
    REALTIMEKIT_ORG_ID: str = ""
    REALTIMEKIT_API_KEY: str = ""
    DYTE_ORG_ID: str = ""
    DYTE_API_KEY: str = ""
    DYTE_BASE_URL: str = ""

    # Web Push (VAPID) — best-effort background call ping for browser tabs.
    # NOTE: this does NOT wake a fully force-killed browser; only the native
    # PushKit / FCM paths below can ring a killed mobile app.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:support@nurseconnect.in"

    # ---------------------------------------------------------------------
    # APNs — iOS VoIP (PushKit) push.
    #
    # This is what lets a *force-killed* iOS app ring. It uses token-based
    # auth: download a .p8 key from the Apple Developer portal (Keys → new key
    # with "Apple Push Notifications service" enabled) and set the three
    # values below. APNS_KEY_P8 accepts either the PEM contents directly or a
    # path to the .p8 file.
    #
    # The push topic is always "<APNS_BUNDLE_ID>.voip" — Apple requires the
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
    # Comma-separated exact origins, e.g.
    #   CORS_ORIGINS=https://app.nurseconnect.in,https://nurseconnect-web.<acct>.workers.dev
    # "*" is only honoured outside production (see cors_origin_list).
    CORS_ORIGINS: str = "*"
    # Optional regex for additional origins. Empty by default. Previously the
    # app hard-coded r"https://.*\.workers\.dev" — i.e. ANY site anyone
    # deploys on Cloudflare Workers — with allow_credentials=True.
    CORS_ORIGIN_REGEX: str = ""

    # Number of reverse proxies in front of the app that append to
    # X-Forwarded-For (CloudFront -> app = 1). See app/core/rate_limit.client_ip.
    TRUSTED_PROXY_HOPS: int = 1

    # Run app.seed on every boot. Defaults to on outside production only.
    # Set RUN_SEED_ON_STARTUP=true in production if you still rely on it to
    # create tables / reference data.
    RUN_SEED_ON_STARTUP: Optional[bool] = None

    # Clinical documentation uploads
    MAX_UPLOAD_MB: int = 10

    # Booking date validation (no past slots, max 365 days ahead). Keep True
    # in every real environment; the CI workflow sets it to false because
    # the test-suite books far-future slots on purpose.
    ENFORCE_BOOKING_SCHEDULE_LIMITS: bool = True

    @property
    def cors_origin_list(self) -> List[str]:
        origins = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        if "*" in origins:
            # A wildcard is never acceptable in production.
            return [] if self.is_production else ["*"]
        return origins

    @property
    def cors_origin_regex(self) -> Optional[str]:
        if self.CORS_ORIGIN_REGEX.strip():
            return self.CORS_ORIGIN_REGEX.strip()
        return None if self.is_production else r"http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?"

    @property
    def run_seed_on_startup(self) -> bool:
        if self.RUN_SEED_ON_STARTUP is not None:
            return bool(self.RUN_SEED_ON_STARTUP)
        return not self.is_production

    def fatal_config_errors(self) -> List[str]:
        """Misconfigurations that make production UNSAFE. The app refuses to
        boot if any are present (startup_warnings() only logs)."""
        errors: List[str] = []
        if not self.is_production:
            return errors
        if self.MOCK_EXTERNAL_PROVIDERS:
            errors.append(
                "MOCK_EXTERNAL_PROVIDERS is true in production: payment signatures and "
                "webhooks would be accepted without verification. Set MOCK_EXTERNAL_PROVIDERS=false."
            )
        if not self.JWT_SECRET_KEY or len(self.JWT_SECRET_KEY) < 32:
            errors.append("JWT_SECRET_KEY is missing or shorter than 32 characters.")
        if not (self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET and self.RAZORPAY_WEBHOOK_SECRET):
            errors.append(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAY_WEBHOOK_SECRET must all be set in production."
            )
        if not self.cors_origin_list and not self.CORS_ORIGIN_REGEX.strip():
            errors.append(
                "CORS_ORIGINS must list the frontend origin(s) explicitly in production (wildcard is not allowed)."
            )
        return errors

    # -----------------------------------------------------------------
    # Environment
    #
    # OTP_DEV_MODE and EMAIL_DEV_MODE both default to True, and both were
    # previously independent of APP_ENV. That is why the live site showed
    # "Dev mode code: 654321" next to a real customer's email address and
    # no mail was ever sent: the deployment simply never set them to False,
    # and nothing forced the issue.
    #
    # Dev mode is now a property of the environment, not a standalone flag.
    # It can only be on in development, so a missing env var can no longer
    # downgrade production auth to a fixed, publicly-visible code.
    # -----------------------------------------------------------------
    _PRODUCTION_ENVS = {"production", "prod", "staging", "stage", "uat"}

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in self._PRODUCTION_ENVS

    @property
    def otp_dev_mode(self) -> bool:
        """Fixed OTP + code echoed in the API response. Never in production."""
        return bool(self.OTP_DEV_MODE) and not self.is_production

    @property
    def email_dev_mode(self) -> bool:
        """Fixed email code + no mail dispatched. Never in production."""
        return bool(self.EMAIL_DEV_MODE) and not self.is_production

    @property
    def email_delivery_configured(self) -> bool:
        return bool(self.RESEND_API_KEY and self.EMAIL_FROM_ADDRESS)

    def startup_warnings(self) -> List[str]:
        """Misconfigurations that silently break user-facing flows.

        Surfaced at boot (see app/main.py) so they are caught on deploy
        rather than by a customer who never receives a verification code.
        """
        problems: List[str] = []
        if self.is_production:
            if self.OTP_DEV_MODE:
                problems.append(
                    "OTP_DEV_MODE is set in a production environment — ignoring it. "
                    "Remove it from the environment."
                )
            if self.EMAIL_DEV_MODE:
                problems.append(
                    "EMAIL_DEV_MODE is set in a production environment — ignoring it. "
                    "Remove it from the environment."
                )
            if not self.email_delivery_configured:
                problems.append(
                    "RESEND_API_KEY/EMAIL_FROM_ADDRESS are not set — verification "
                    "emails cannot be delivered."
                )
            if not (self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET):
                problems.append(
                    "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are not both set — payment "
                    "signature verification will reject every payment."
                )
            if not self.RAZORPAY_WEBHOOK_SECRET:
                problems.append(
                    "RAZORPAY_WEBHOOK_SECRET is not set — Razorpay webhooks will be "
                    "rejected, so payments captured out-of-band will not settle."
                )
            if not (self.DIGIO_CLIENT_ID and self.DIGIO_CLIENT_SECRET):
                problems.append(
                    "DIGIO_CLIENT_ID/DIGIO_CLIENT_SECRET are not both set — Stage 2 "
                    "e-Stamp agreements cannot be sent for signing."
                )
            if not self.DIGIO_WEBHOOK_SECRET:
                problems.append(
                    "DIGIO_WEBHOOK_SECRET is not set — Digio's signing-completion "
                    "webhook will be rejected, so Stage 2 agreements will never "
                    "finalize automatically."
                )
            if self.MOCK_EXTERNAL_PROVIDERS:
                problems.append(
                    "MOCK_EXTERNAL_PROVIDERS is on in a production environment — "
                    "payments are being faked and SMS OTPs will be reported as "
                    "FAILED (never silently 'sent')."
                )
            if not (self.MSG91_AUTH_KEY and self.MSG91_TEMPLATE_ID):
                problems.append(
                    "MSG91_AUTH_KEY/MSG91_TEMPLATE_ID are not both set — OTP SMS "
                    "cannot be delivered."
                )
            if self.APP_DEBUG:
                problems.append(
                    "APP_DEBUG is on in a production environment — ignoring it "
                    "(tracebacks are never returned to clients in production)."
                )
            if not self.PUBLIC_API_URL:
                problems.append(
                    "PUBLIC_API_URL is not set — visit-report download links for "
                    "the mobile apps will be derived from proxy headers."
                )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Safety net: dev shortcuts must never be active in production.
# Warning only for now so a misconfigured deploy still boots; once the
# production environment variables are confirmed, change this to raise.
if settings.APP_ENV == "production" and (settings.OTP_DEV_MODE or settings.EMAIL_DEV_MODE):
    logging.getLogger(__name__).warning(
        "OTP_DEV_MODE/EMAIL_DEV_MODE is enabled in production. Disable it."
    )