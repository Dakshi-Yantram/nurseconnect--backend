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
    OTP_DEV_FIXED_CODE: str = "123456"
    OTP_EXPIRE_MINUTES: int = 5

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
    SMTP_FROM_EMAIL: str = "no-reply@nurseconnect.app"
    SMTP_FROM_NAME: str = "NurseConnect"
    SMTP_USE_TLS: bool = True

    # Resend (transactional email over HTTPS — works on Render free tier)
    RESEND_API_KEY: str = ""
    EMAIL_FROM_ADDRESS: str = "onboarding@resend.dev"

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

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

    # Firebase
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # ABHA
    ABHA_BASE_URL: str = ""
    ABHA_CLIENT_ID: str = ""
    ABHA_CLIENT_SECRET: str = ""

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