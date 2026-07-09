"""External provider abstraction layer.

All integrations live behind these interfaces. Business services NEVER call
provider SDKs directly — they go through these adapters.

In dev / MOCK_EXTERNAL_PROVIDERS=true mode, all methods return deterministic
mock responses suitable for end-to-end testing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# Razorpay
# ============================================================================
class RazorpayClient:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.RAZORPAY_KEY_ID or settings.RAZORPAY_KEY_ID.endswith("_placeholder")
        self.key_id = settings.RAZORPAY_KEY_ID
        self.key_secret = settings.RAZORPAY_KEY_SECRET
        self.webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    async def create_order(self, amount_paise: int, currency: str = "INR", receipt: Optional[str] = None, notes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.mock:
            order_id = f"order_mock_{uuid.uuid4().hex[:14]}"
            logger.info("MOCK razorpay create_order amount=%s receipt=%s -> %s", amount_paise, receipt, order_id)
            return {
                "id": order_id,
                "entity": "order",
                "amount": amount_paise,
                "amount_paid": 0,
                "amount_due": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "status": "created",
                "notes": notes or {},
            }
        # Real SDK call (production)
        import razorpay  # local import
        client = razorpay.Client(auth=(self.key_id, self.key_secret))
        return client.order.create({"amount": amount_paise, "currency": currency, "receipt": receipt, "notes": notes or {}})

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        if self.mock:
            # Accept any signature in dev for ease of testing
            return signature.startswith("mock_") or signature == "mock_signature" or len(signature) >= 32
        msg = f"{order_id}|{payment_id}".encode()
        expected = hmac.new(self.key_secret.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        if self.mock:
            return True
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def initiate_payout(self, fund_account_id: str, amount_paise: int, reference: str, notes: Optional[Dict] = None) -> Dict[str, Any]:
        if self.mock:
            payout_id = f"pout_mock_{uuid.uuid4().hex[:14]}"
            return {"id": payout_id, "status": "processed", "amount": amount_paise, "reference_id": reference}
        # Real impl would use razorpay.Client(...).payout.create(...)
        raise NotImplementedError("Configure real Razorpay credentials")

    async def create_refund(self, payment_id: str, amount_paise: int) -> Dict[str, Any]:
        if self.mock:
            refund_id = f"rfnd_mock_{uuid.uuid4().hex[:14]}"
            return {"id": refund_id, "payment_id": payment_id, "amount": amount_paise, "status": "processed"}
        import razorpay
        client = razorpay.Client(auth=(self.key_id, self.key_secret))
        return client.payment.refund(payment_id, {"amount": amount_paise})


# ============================================================================
# Cloudinary
# ============================================================================
class CloudinaryClient:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or settings.CLOUDINARY_CLOUD_NAME in ("", "placeholder")
        self.cloud_name = settings.CLOUDINARY_CLOUD_NAME
        self.api_key = settings.CLOUDINARY_API_KEY
        self.api_secret = settings.CLOUDINARY_API_SECRET

    async def upload_base64(self, b64_payload: str, folder: str = "nurseconnect", resource_type: str = "image") -> Dict[str, Any]:
        if self.mock:
            public_id = f"{folder}/{uuid.uuid4().hex[:12]}"
            return {
                "public_id": public_id,
                "secure_url": f"https://res.cloudinary.com/mock/{resource_type}/upload/{public_id}",
                "resource_type": resource_type,
                "bytes": len(b64_payload),
            }
        import cloudinary  # type: ignore
        import cloudinary.uploader  # type: ignore
        cloudinary.config(cloud_name=self.cloud_name, api_key=self.api_key, api_secret=self.api_secret)
        # The SDK requires a URL, local file path, or a proper `data:` URI —
        # a bare base64 string (no prefix) gets misinterpreted as a file path,
        # which raises FileNotFoundError since no such file exists on disk.
        payload = b64_payload
        if not payload.startswith("data:") and not payload.startswith("http"):
            payload = f"data:application/octet-stream;base64,{payload}"
        return cloudinary.uploader.upload(payload, folder=folder, resource_type=resource_type)

    async def delete(self, public_id: str) -> Dict[str, Any]:
        if self.mock:
            return {"result": "ok", "public_id": public_id}
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloud_name=self.cloud_name, api_key=self.api_key, api_secret=self.api_secret)
        return cloudinary.uploader.destroy(public_id)


# ============================================================================
# SMS (MSG91)
# ============================================================================
class Msg91Client:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.MSG91_AUTH_KEY or settings.MSG91_AUTH_KEY == "placeholder"
        self.auth_key = settings.MSG91_AUTH_KEY
        self.sender_id = settings.MSG91_SENDER_ID
        self.template_id = settings.MSG91_TEMPLATE_ID

    async def send_otp(self, phone_e164: str, otp: str) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK MSG91 send_otp phone=%s code=%s", phone_e164, otp)
            return {"type": "success", "request_id": f"msg91_mock_{uuid.uuid4().hex[:10]}"}
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://control.msg91.com/api/v5/otp",
                headers={"authkey": self.auth_key},
                json={"template_id": self.template_id, "mobile": phone_e164.lstrip("+"), "otp": otp, "sender": self.sender_id},
                timeout=10,
            )
            return resp.json()

    async def send_sms(self, phone_e164: str, message: str) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK MSG91 send_sms phone=%s msg=%s", phone_e164, message[:80])
            return {"type": "success", "request_id": f"msg91_mock_{uuid.uuid4().hex[:10]}"}
        # Real impl
        return {"type": "skipped"}


# ============================================================================
# WhatsApp (Interakt)
# ============================================================================
class InteraktClient:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.INTERAKT_API_KEY or settings.INTERAKT_API_KEY == "placeholder"
        self.api_key = settings.INTERAKT_API_KEY
        self.base_url = settings.INTERAKT_BASE_URL

    async def send_message(self, phone_e164: str, template_name: str, variables: Dict[str, str]) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK Interakt send_message phone=%s template=%s", phone_e164, template_name)
            return {"result": True, "message_id": f"wa_mock_{uuid.uuid4().hex[:10]}"}
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/public/message/",
                headers={"Authorization": f"Basic {self.api_key}"},
                json={"countryCode": "+91", "phoneNumber": phone_e164.lstrip("+91"), "type": "Template", "template": {"name": template_name, "languageCode": "en", "bodyValues": list(variables.values())}},
                timeout=10,
            )
            return resp.json()


# ============================================================================
# Firebase Push
# ============================================================================
class FirebasePushClient:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.FIREBASE_SERVICE_ACCOUNT_JSON
        self.project_id = settings.FIREBASE_PROJECT_ID

    async def send_to_token(self, fcm_token: str, title: str, body: str, data: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK Firebase push token=%s title=%s", fcm_token[:12] if fcm_token else None, title)
            return {"success": True, "message_id": f"fcm_mock_{uuid.uuid4().hex[:10]}"}
        # Real impl via firebase_admin
        return {"success": False, "reason": "not_configured"}


# ============================================================================
# ABHA (Sandbox)
# ============================================================================
class AbhaClient:
    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.ABHA_CLIENT_ID or settings.ABHA_CLIENT_ID == "placeholder"
        self.base_url = settings.ABHA_BASE_URL
        self.client_id = settings.ABHA_CLIENT_ID
        self.client_secret = settings.ABHA_CLIENT_SECRET

    async def link_health_id(self, abha_id: str, patient_metadata: Dict[str, Any]) -> Dict[str, Any]:
        if self.mock:
            return {"linked": True, "abha_id": abha_id, "link_token": secrets.token_hex(16)}
        return {"linked": False, "reason": "not_configured"}

    async def fetch_records(self, abha_id: str) -> Dict[str, Any]:
        if self.mock:
            return {"abha_id": abha_id, "records": []}
        return {"abha_id": abha_id, "records": []}


# Singletons
razorpay_client = RazorpayClient()
cloudinary_client = CloudinaryClient()
msg91_client = Msg91Client()
interakt_client = InteraktClient()
firebase_push_client = FirebasePushClient()
abha_client = AbhaClient()