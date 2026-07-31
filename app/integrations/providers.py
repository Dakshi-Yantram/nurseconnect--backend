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
from datetime import timedelta
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class ExternalProviderError(RuntimeError):
    """Raised when an upstream provider is unavailable or rejects a request."""


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
# Firebase Push (Android)
# ============================================================================
class FirebasePushClient:
    """FCM sender, used for both ordinary notifications and call ringing.

    Two message shapes matter here:

    * ``send_to_token``    — normal notification. The OS renders it; the app
                             does not need to be running.
    * ``send_call_push``   — **data-only, priority=high**. Android only lets a
                             data-only high-priority message wake an app that
                             the user has swiped away, and only a data-only
                             message reaches the app's own handler so it can
                             raise a full-screen CallKeep/ConnectionService UI
                             rather than a plain notification banner. Adding a
                             ``notification`` block here would break that: the
                             system tray would swallow it and the app would
                             never be invoked.
    """

    def __init__(self) -> None:
        self.mock = settings.MOCK_EXTERNAL_PROVIDERS or not settings.FIREBASE_SERVICE_ACCOUNT_JSON
        self.project_id = settings.FIREBASE_PROJECT_ID
        self._app = None

    def _get_app(self):
        """Lazily initialise the firebase-admin app.

        Done on first use rather than at import so a deployment without
        Firebase configured still boots — push simply reports itself as
        unconfigured instead of taking the process down.
        """
        if self._app is not None:
            return self._app
        import json as _json

        import firebase_admin
        from firebase_admin import credentials

        raw = settings.FIREBASE_SERVICE_ACCOUNT_JSON.strip()
        # Accept either the JSON blob itself or a path to the file.
        if raw.startswith("{"):
            cred = credentials.Certificate(_json.loads(raw))
        else:
            cred = credentials.Certificate(raw)

        try:
            self._app = firebase_admin.get_app("nurseconnect")
        except ValueError:
            self._app = firebase_admin.initialize_app(cred, name="nurseconnect")
        return self._app

    async def _send(self, message) -> Dict[str, Any]:
        """Dispatch on a worker thread — the firebase-admin SDK is blocking."""
        import asyncio

        from firebase_admin import messaging

        def _do() -> str:
            return messaging.send(message, app=self._get_app())

        try:
            message_id = await asyncio.to_thread(_do)
            return {"success": True, "message_id": message_id}
        except Exception as e:  # noqa: BLE001
            # A token goes stale whenever the app is reinstalled. Report it so
            # the caller can prune it rather than retrying forever.
            name = type(e).__name__
            unregistered = name in ("UnregisteredError", "SenderIdMismatchError")
            if not unregistered:
                logger.exception("FCM send failed")
            return {"success": False, "reason": name, "unregistered": unregistered}

    async def send_to_token(
        self,
        fcm_token: str,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK Firebase push token=%s title=%s", fcm_token[:12] if fcm_token else None, title)
            return {"success": True, "message_id": f"fcm_mock_{uuid.uuid4().hex[:10]}"}
        if not fcm_token:
            return {"success": False, "reason": "no_token"}

        from firebase_admin import messaging

        message = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
            ),
        )
        return await self._send(message)

    async def send_call_push(self, fcm_token: str, data: Dict[str, str]) -> Dict[str, Any]:
        """Data-only, high-priority ring for Android. See the class docstring
        for why this must not carry a ``notification`` block."""
        if self.mock:
            logger.info("MOCK Firebase CALL push token=%s", fcm_token[:12] if fcm_token else None)
            return {"success": True, "message_id": f"fcm_mock_call_{uuid.uuid4().hex[:10]}"}
        if not fcm_token:
            return {"success": False, "reason": "no_token"}

        from firebase_admin import messaging

        message = messaging.Message(
            token=fcm_token,
            data={k: str(v) for k, v in data.items()},
            android=messaging.AndroidConfig(
                priority="high",
                # A ring is worthless if it arrives late, and pointless if it
                # arrives after the caller gave up — so never let it queue.
                ttl=timedelta(seconds=45),
            ),
        )
        return await self._send(message)


# ============================================================================
# APNs VoIP (iOS PushKit)
# ============================================================================
class ApnsVoipClient:
    """Sends PushKit VoIP pushes so a force-killed iOS app can ring.

    This is the only mechanism Apple provides for that. A few hard rules are
    baked in below because getting them wrong fails silently or, worse, gets
    the app's VoIP push privileges revoked:

    * the topic MUST be ``<bundle-id>.voip`` — the bare bundle id is rejected;
    * ``apns-push-type`` MUST be ``voip`` and ``apns-priority`` 10;
    * the payload carries no ``aps.alert`` — iOS does not display a VoIP push,
      it hands it to the app, which must then report an incoming call to
      CallKit **immediately**. iOS terminates apps that receive a VoIP push
      without reporting a call, and repeat offenders stop receiving them.

    Sandbox and production are different hosts and a device token from one is
    invalid on the other; ``APNS_USE_SANDBOX`` must match how the app was
    signed (dev build vs TestFlight/App Store).
    """

    _SANDBOX_HOST = "https://api.sandbox.push.apple.com"
    _PROD_HOST = "https://api.push.apple.com"

    def __init__(self) -> None:
        self.mock = (
            settings.MOCK_EXTERNAL_PROVIDERS
            or not settings.APNS_KEY_P8
            or not settings.APNS_KEY_ID
            or not settings.APNS_TEAM_ID
        )
        self.bundle_id = settings.APNS_BUNDLE_ID
        self._jwt: Optional[str] = None
        self._jwt_issued_at: float = 0.0

    @property
    def host(self) -> str:
        return self._SANDBOX_HOST if settings.APNS_USE_SANDBOX else self._PROD_HOST

    def _private_key(self) -> str:
        raw = settings.APNS_KEY_P8.strip()
        if raw.startswith("-----BEGIN"):
            return raw
        # Treat anything else as a path to the .p8 file.
        with open(raw, "r", encoding="utf-8") as fh:
            return fh.read()

    def _auth_token(self) -> str:
        """ES256 JWT for APNs.

        Apple rejects tokens older than 1 hour and throttles clients that mint
        a new one per request, so it's cached and refreshed at ~50 minutes.
        """
        import time

        now = time.time()
        if self._jwt and (now - self._jwt_issued_at) < 3000:
            return self._jwt

        import jwt as pyjwt

        self._jwt = pyjwt.encode(
            {"iss": settings.APNS_TEAM_ID, "iat": int(now)},
            self._private_key(),
            algorithm="ES256",
            headers={"kid": settings.APNS_KEY_ID},
        )
        self._jwt_issued_at = now
        return self._jwt

    async def send_voip(self, device_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.mock:
            logger.info(
                "MOCK APNs VoIP push token=%s payload=%s",
                device_token[:12] if device_token else None,
                payload.get("type"),
            )
            return {"success": True, "apns_id": f"apns_mock_{uuid.uuid4().hex[:10]}"}
        if not device_token:
            return {"success": False, "reason": "no_token"}

        import httpx

        try:
            # APNs requires HTTP/2.
            async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
                resp = await client.post(
                    f"{self.host}/3/device/{device_token}",
                    headers={
                        "authorization": f"bearer {self._auth_token()}",
                        "apns-topic": f"{self.bundle_id}.voip",
                        "apns-push-type": "voip",
                        "apns-priority": "10",
                        "apns-expiration": "0",  # deliver now or drop it
                    },
                    json=payload,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("APNs VoIP push failed to send")
            return {"success": False, "reason": type(e).__name__}

        if resp.status_code == 200:
            return {"success": True, "apns_id": resp.headers.get("apns-id")}

        reason = ""
        try:
            reason = resp.json().get("reason", "")
        except Exception:  # noqa: BLE001
            reason = resp.text[:200]
        # BadDeviceToken/Unregistered mean the install is gone — the caller
        # should drop the token rather than keep pushing to it.
        stale = reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")
        if not stale:
            logger.warning("APNs VoIP push rejected: %s %s", resp.status_code, reason)
        return {"success": False, "reason": reason, "unregistered": stale}


# ============================================================================
# Cloudflare RealtimeKit (in-app voice calling)
#
# Migrated off Dyte, which Cloudflare acquired and put into maintenance mode.
# RealtimeKit kept Dyte's REST shape verbatim, so this is a base-URL + auth
# swap: POST /meetings, then POST /meetings/{id}/participants (with a
# preset_name) which returns the participant's authToken. Auth is HTTP Basic
# over base64(orgId:apiKey), same as before.
# ============================================================================
class RealtimeKitClient:
    """Thin wrapper over the Cloudflare-native RealtimeKit REST API.

    Cloudflare retired the old Dyte-style developer portal (org_id + api_key,
    Basic auth, api.realtime.cloudflare.com/v2). RealtimeKit now lives under
    the standard Cloudflare API, scoped to an account and a RealtimeKit "app":

        https://api.cloudflare.com/client/v4/accounts/{account_id}/realtime/kit/{app_id}/...

    authenticated with a Cloudflare API Token (Bearer) that has the
    "Realtime / Realtime Admin" permission.

    Flow used by this app:
      1. create_meeting()  -> once per booking, when the call is first started
      2. add_participant() -> once per side (nurse / customer) each time they
                               join; returns an authToken the frontend hands to
                               the RealtimeKit SDK.
    """

    def __init__(self) -> None:
        self.account_id = settings.REALTIMEKIT_ACCOUNT_ID
        self.app_id = settings.REALTIMEKIT_APP_ID
        self.api_token = settings.REALTIMEKIT_API_TOKEN
        self.base_url = settings.REALTIMEKIT_BASE_URL or "https://api.cloudflare.com/client/v4"
        self.mock = (
            settings.MOCK_EXTERNAL_PROVIDERS
            or not self.account_id
            or not self.app_id
            or not self.api_token
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _kit_url(self, path: str) -> str:
        return f"{self.base_url}/accounts/{self.account_id}/realtime/kit/{self.app_id}{path}"

    async def _post(self, path: str, payload: Dict[str, Any], operation: str) -> Dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
                resp = await client.post(
                    self._kit_url(path),
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            logger.exception("realtimekit %s timed out", operation)
            raise ExternalProviderError("RealtimeKit request timed out") from exc
        except httpx.RequestError as exc:
            logger.exception("realtimekit %s request failed", operation)
            raise ExternalProviderError("RealtimeKit is unreachable") from exc

        if resp.status_code >= 400:
            logger.error("realtimekit %s failed status=%s body=%s", operation, resp.status_code, resp.text)
            raise ExternalProviderError(f"RealtimeKit rejected {operation}")

        body = resp.json()
        data = body.get("result") or body.get("data") or body
        if not isinstance(data, dict):
            raise ExternalProviderError(f"RealtimeKit returned an invalid {operation} response")
        return data

    async def create_meeting(self, title: str) -> Dict[str, Any]:
        if self.mock:
            meeting_id = f"meeting_mock_{uuid.uuid4().hex[:16]}"
            logger.info("MOCK realtimekit create_meeting title=%s -> %s", title, meeting_id)
            return {"id": meeting_id, "title": title, "status": "ACTIVE"}
        return await self._post(
            "/meetings",
            {"title": title, "record_on_start": False},
            "create_meeting",
        )

    async def add_participant(self, meeting_id: str, participant_name: str, participant_id: str, preset_name: str = "group_call_host") -> Dict[str, Any]:
        if self.mock:
            auth_token = f"rtk_mock_token_{uuid.uuid4().hex}"
            logger.info("MOCK realtimekit add_participant meeting=%s participant=%s", meeting_id, participant_id)
            return {"token": auth_token, "authToken": auth_token, "id": participant_id}
        data = await self._post(
            f"/meetings/{meeting_id}/participants",
            {
                "name": participant_name,
                "preset_name": preset_name,
                "custom_participant_id": participant_id,
            },
            "add_participant",
        )
        # Normalise so callers can rely on `authToken` regardless of the
        # exact key the API returns (`token` on some responses).
        if "authToken" not in data and "token" in data:
            data["authToken"] = data["token"]
        if not data.get("authToken"):
            raise ExternalProviderError("RealtimeKit did not return an auth token")
        return data

    async def deactivate_meeting(self, meeting_id: str) -> Dict[str, Any]:
        if self.mock:
            logger.info("MOCK realtimekit deactivate_meeting %s", meeting_id)
            return {"id": meeting_id, "status": "INACTIVE"}
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                self._kit_url(f"/meetings/{meeting_id}"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            return {"id": meeting_id, "status": "INACTIVE"}


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
apns_voip_client = ApnsVoipClient()
abha_client = AbhaClient()
realtimekit_client = RealtimeKitClient()
