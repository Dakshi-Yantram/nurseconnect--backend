"""In-app voice calling (Dyte) between consumer and worker on a booking.

Flow:
  1. Caller hits POST /bookings/{id}/call/start
       -> creates (or reuses) a Dyte meeting for this booking
       -> creates a CallSession row (status=ringing)
       -> pushes an "incoming_call" event three ways to the callee:
            a) websocket (works if their app/tab is open)
            b) FCM data push to their registered devices (UserSession.fcm_token)
            c) Web Push to their registered browser subscriptions (PushSubscription)
       -> returns the caller's own Dyte auth token so they can join immediately
  2. Callee hits POST /bookings/{id}/call/{call_session_id}/join when they accept
       -> returns their own Dyte auth token for the SAME meeting
  3. Either side hits POST /bookings/{id}/call/{call_session_id}/end when done

DELIVERY TO A BACKGROUNDED / KILLED APP:
The native mobile app registers device tokens via POST /notifications/devices,
which gives (b) two real paths:
  - iOS     : PushKit VoIP push. The only mechanism Apple provides that can
              ring a force-killed app. The app MUST report the call to CallKit
              the moment it receives one, or iOS will kill it and eventually
              revoke its VoIP push privileges.
  - Android : data-only, high-priority FCM, which wakes a swiped-away app so
              it can raise a full-screen ConnectionService call UI.
Web Push (c) remains genuinely best-effort — it cannot wake a closed browser,
so browser users only ring while a tab is open.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.integrations.providers import apns_voip_client, dyte_client, firebase_push_client
from app.models.enums import UserRole
from app.models.models import (
    Booking,
    CallSession,
    ConsumerProfile,
    PushSubscription,
    User,
    UserSession,
    WorkerProfile,
)
from app.schemas.schemas import (
    CallEndRequest,
    CallSessionOut,
    CallStartResponse,
    DeviceRegisterRequest,
    PushSubscribeRequest,
)
from app.websockets.manager import manager, user_topic

router = APIRouter(prefix="/bookings", tags=["calls"])


async def _load_booking_parties(db: AsyncSession, booking_id: UUID) -> tuple[Booking, User, User]:
    """Returns (booking, consumer_user, worker_user). Raises 404/409 as appropriate."""
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if not booking.worker_id:
        raise HTTPException(status_code=409, detail="No worker assigned to this booking yet")

    res = await db.execute(
        select(User).join(ConsumerProfile, ConsumerProfile.user_id == User.id).where(ConsumerProfile.id == booking.consumer_id)
    )
    consumer_user = res.scalar_one_or_none()
    res = await db.execute(
        select(User).join(WorkerProfile, WorkerProfile.user_id == User.id).where(WorkerProfile.id == booking.worker_id)
    )
    worker_user = res.scalar_one_or_none()
    if not consumer_user or not worker_user:
        raise HTTPException(status_code=500, detail="Booking parties could not be resolved")
    return booking, consumer_user, worker_user


async def _ring_callee(db: AsyncSession, callee: User, booking: Booking, call_session: CallSession, caller_name: str) -> None:
    payload = {
        "type": "incoming_call",
        "booking_id": str(booking.id),
        "call_session_id": str(call_session.id),
        "dyte_meeting_id": call_session.dyte_meeting_id,
        "caller_name": caller_name,
    }
    # a) live websocket ping — instant if their app/tab is already open
    await manager.broadcast(user_topic(callee.id), payload)

    # b) Native push to every device they're signed in on. This is the path
    # that reaches a backgrounded or force-killed app:
    #   - iOS     : PushKit VoIP push -> app must report to CallKit at once
    #   - Android : data-only, high-priority FCM -> full-screen ConnectionService
    # Both are dispatched concurrently; one device failing must not stop the
    # others from ringing, so results are gathered rather than awaited serially.
    res = await db.execute(
        select(UserSession).where(
            UserSession.user_id == callee.id,
            UserSession.revoked.is_(False),
        )
    )
    sessions = list(res.scalars().all())

    async def _push(sess: UserSession) -> None:
        # Prefer PushKit on iOS — it is the only push type that can ring a
        # killed app. Fall back to FCM when no VoIP token is registered
        # (e.g. an older build, or the user declined notification access).
        if sess.apns_voip_token:
            result = await apns_voip_client.send_voip(sess.apns_voip_token, payload)
            if result.get("unregistered"):
                sess.apns_voip_token = None
            if result.get("success"):
                return
        if sess.fcm_token:
            result = await firebase_push_client.send_call_push(
                sess.fcm_token,
                data=payload,
            )
            if result.get("unregistered"):
                sess.fcm_token = None

    if sessions:
        await asyncio.gather(*(_push(s) for s in sessions), return_exceptions=True)
        # Persist any token we just learned was dead, so we stop pushing to it.
        await db.commit()

    # c) Web Push (VAPID) to registered browser subscriptions — best effort,
    # will not wake a fully force-killed browser/tab. See module docstring.
    res = await db.execute(select(PushSubscription).where(PushSubscription.user_id == callee.id))
    subs = res.scalars().all()
    if subs and settings.VAPID_PRIVATE_KEY:
        try:
            from pywebpush import webpush, WebPushException
            import json
            for sub in subs:
                try:
                    webpush(
                        subscription_info={
                            "endpoint": sub.endpoint,
                            "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key},
                        },
                        data=json.dumps({"title": f"Incoming call from {caller_name}", "body": "Tap to join", **payload}),
                        vapid_private_key=settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": settings.VAPID_SUBJECT},
                    )
                except WebPushException:
                    pass  # stale subscription — fine to ignore, cleaned up lazily
        except ImportError:
            pass  # pywebpush not installed in this environment yet


@router.post("/{booking_id}/call/start", response_model=CallStartResponse)
async def start_call(booking_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    booking, consumer_user, worker_user = await _load_booking_parties(db, booking_id)

    if current.id == consumer_user.id:
        caller, callee, caller_role = consumer_user, worker_user, "consumer"
    elif current.id == worker_user.id:
        caller, callee, caller_role = worker_user, consumer_user, "worker"
    else:
        raise HTTPException(status_code=403, detail="Not a party to this booking")

    # Reuse an existing meeting for this booking if one was already created
    # (e.g. redial), otherwise create a fresh Dyte meeting.
    res = await db.execute(
        select(CallSession).where(CallSession.booking_id == booking.id).order_by(CallSession.created_at.desc()).limit(1)
    )
    prior = res.scalar_one_or_none()
    if prior:
        meeting_id = prior.dyte_meeting_id
    else:
        meeting = await dyte_client.create_meeting(title=f"NurseConnect booking {booking.booking_ref}")
        meeting_id = meeting["id"]

    call_session = CallSession(
        booking_id=booking.id,
        dyte_meeting_id=meeting_id,
        initiated_by_user_id=caller.id,
        initiated_by_role=caller_role,
        callee_user_id=callee.id,
        status="ringing",
    )
    db.add(call_session)
    await db.commit()
    await db.refresh(call_session)

    participant = await dyte_client.add_participant(meeting_id, participant_name=caller.full_name or caller_role, participant_id=str(caller.id))

    await _ring_callee(db, callee, booking, call_session, caller_name=caller.full_name or caller_role)

    return CallStartResponse(
        call_session_id=call_session.id,
        dyte_meeting_id=meeting_id,
        dyte_auth_token=participant["authToken"],
        dyte_org_id=settings.DYTE_ORG_ID,
    )


@router.post("/{booking_id}/call/{call_session_id}/join", response_model=CallStartResponse)
async def join_call(booking_id: UUID, call_session_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CallSession).where(CallSession.id == call_session_id, CallSession.booking_id == booking_id))
    call_session = res.scalar_one_or_none()
    if not call_session:
        raise HTTPException(status_code=404, detail="Call not found")
    if current.id != call_session.callee_user_id and current.id != call_session.initiated_by_user_id:
        raise HTTPException(status_code=403, detail="Not a party to this call")

    if call_session.status == "ringing" and current.id == call_session.callee_user_id:
        call_session.status = "joined"
        call_session.callee_joined_at = datetime.now(timezone.utc)
        await db.commit()

    res = await db.execute(select(User).where(User.id == current.id))
    user = res.scalar_one()
    participant = await dyte_client.add_participant(call_session.dyte_meeting_id, participant_name=user.full_name or "participant", participant_id=str(user.id))

    return CallStartResponse(
        call_session_id=call_session.id,
        dyte_meeting_id=call_session.dyte_meeting_id,
        dyte_auth_token=participant["authToken"],
        dyte_org_id=settings.DYTE_ORG_ID,
    )


@router.post("/{booking_id}/call/{call_session_id}/end", response_model=CallSessionOut)
async def end_call(booking_id: UUID, call_session_id: UUID, body: CallEndRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CallSession).where(CallSession.id == call_session_id, CallSession.booking_id == booking_id))
    call_session = res.scalar_one_or_none()
    if not call_session:
        raise HTTPException(status_code=404, detail="Call not found")
    if current.id not in (call_session.callee_user_id, call_session.initiated_by_user_id):
        raise HTTPException(status_code=403, detail="Not a party to this call")

    if call_session.status not in ("ended", "missed", "failed"):
        now = datetime.now(timezone.utc)
        call_session.ended_at = now
        call_session.status = "ended" if call_session.callee_joined_at else "missed"
        call_session.end_reason = body.end_reason
        if call_session.callee_joined_at:
            call_session.duration_seconds = int((now - call_session.callee_joined_at).total_seconds())
        await db.commit()
        await db.refresh(call_session)

    await manager.broadcast(user_topic(call_session.callee_user_id), {"type": "call_ended", "call_session_id": str(call_session.id)})
    await manager.broadcast(user_topic(call_session.initiated_by_user_id), {"type": "call_ended", "call_session_id": str(call_session.id)})

    return CallSessionOut.model_validate(call_session)


push_router = APIRouter(prefix="/notifications", tags=["calls"])


@push_router.post("/devices")
async def register_device(
    body: DeviceRegisterRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register this install's push tokens so calls can ring it.

    Tokens are attached to the user's active session rows for this device_id.
    Re-registering updates in place — tokens rotate (app reinstall, OS
    restore), and a stale one silently swallows every future ring.
    """
    if not body.fcm_token and not body.apns_voip_token:
        raise HTTPException(status_code=400, detail="At least one push token is required")

    res = await db.execute(
        select(UserSession).where(
            UserSession.user_id == current.id,
            UserSession.device_id == body.device_id,
            UserSession.revoked.is_(False),
        )
    )
    sessions = list(res.scalars().all())
    if not sessions:
        # The device signed in before this endpoint existed, or the session
        # was rotated. Fall back to the user's most recent live session so
        # the tokens still land somewhere that _ring_callee will read.
        res = await db.execute(
            select(UserSession)
            .where(UserSession.user_id == current.id, UserSession.revoked.is_(False))
            .order_by(UserSession.created_at.desc())
            .limit(1)
        )
        sessions = list(res.scalars().all())
    if not sessions:
        raise HTTPException(status_code=409, detail="No active session for this account")

    for sess in sessions:
        if body.fcm_token:
            sess.fcm_token = body.fcm_token
        if body.apns_voip_token:
            sess.apns_voip_token = body.apns_voip_token
        if body.platform:
            sess.device_platform = body.platform
        if body.device_id and not sess.device_id:
            sess.device_id = body.device_id

    # The same physical device may have registered a token under a previous
    # session; clear it there so we don't push twice to one handset.
    for token_col, value in (
        (UserSession.fcm_token, body.fcm_token),
        (UserSession.apns_voip_token, body.apns_voip_token),
    ):
        if not value:
            continue
        dupes = await db.execute(
            select(UserSession).where(
                token_col == value,
                UserSession.id.not_in([s.id for s in sessions]),
            )
        )
        for stale in dupes.scalars().all():
            if token_col is UserSession.fcm_token:
                stale.fcm_token = None
            else:
                stale.apns_voip_token = None

    await db.commit()
    return {"registered": True, "voip": bool(body.apns_voip_token)}


@push_router.delete("/devices/{device_id}")
async def unregister_device(
    device_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Drop this device's push tokens — called on sign-out so the handset
    stops ringing for an account that is no longer signed in on it."""
    res = await db.execute(
        select(UserSession).where(
            UserSession.user_id == current.id,
            UserSession.device_id == device_id,
        )
    )
    for sess in res.scalars().all():
        sess.fcm_token = None
        sess.apns_voip_token = None
    await db.commit()
    return {"unregistered": True}


@push_router.post("/push-subscribe")
async def push_subscribe(body: PushSubscribeRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Called from the frontend after the browser grants Notification
    permission and creates a PushSubscription via the service worker."""
    res = await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    existing = res.scalar_one_or_none()
    if existing:
        existing.user_id = current.id
        existing.p256dh_key = body.p256dh_key
        existing.auth_key = body.auth_key
        existing.user_agent = body.user_agent
    else:
        db.add(PushSubscription(
            user_id=current.id,
            endpoint=body.endpoint,
            p256dh_key=body.p256dh_key,
            auth_key=body.auth_key,
            user_agent=body.user_agent,
        ))
    await db.commit()
    return {"subscribed": True}
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.integrations.providers import dyte_client, firebase_push_client
from app.models.enums import UserRole
from app.models.models import (
    Booking,
    CallSession,
    ConsumerProfile,
    PushSubscription,
    User,
    UserSession,
    WorkerProfile,
)
from app.schemas.schemas import (
    CallEndRequest,
    CallSessionOut,
    CallStartResponse,
    PushSubscribeRequest,
)
from app.websockets.manager import manager, user_topic

router = APIRouter(prefix="/bookings", tags=["calls"])


async def _load_booking_parties(db: AsyncSession, booking_id: UUID) -> tuple[Booking, User, User]:
    """Returns (booking, consumer_user, worker_user). Raises 404/409 as appropriate."""
    res = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if not booking.worker_id:
        raise HTTPException(status_code=409, detail="No worker assigned to this booking yet")

    res = await db.execute(
        select(User).join(ConsumerProfile, ConsumerProfile.user_id == User.id).where(ConsumerProfile.id == booking.consumer_id)
    )
    consumer_user = res.scalar_one_or_none()
    res = await db.execute(
        select(User).join(WorkerProfile, WorkerProfile.user_id == User.id).where(WorkerProfile.id == booking.worker_id)
    )
    worker_user = res.scalar_one_or_none()
    if not consumer_user or not worker_user:
        raise HTTPException(status_code=500, detail="Booking parties could not be resolved")
    return booking, consumer_user, worker_user


async def _ring_callee(db: AsyncSession, callee: User, booking: Booking, call_session: CallSession, caller_name: str) -> None:
    payload = {
        "type": "incoming_call",
        "booking_id": str(booking.id),
        "call_session_id": str(call_session.id),
        "dyte_meeting_id": call_session.dyte_meeting_id,
        "caller_name": caller_name,
    }
    # a) live websocket ping — instant if their app/tab is open
    await manager.broadcast(user_topic(callee.id), payload)

    # b) FCM data push to every device session they're logged in on
    res = await db.execute(
        select(UserSession).where(UserSession.user_id == callee.id, UserSession.revoked.is_(False), UserSession.fcm_token.is_not(None))
    )
    for sess in res.scalars().all():
        await firebase_push_client.send_to_token(
            sess.fcm_token,
            title=f"Incoming call from {caller_name}",
            body="Tap to join",
            data={k: str(v) for k, v in payload.items()},
        )

    # c) Web Push (VAPID) to registered browser subscriptions — best effort,
    # will not wake a fully force-killed browser/tab. See module docstring.
    res = await db.execute(select(PushSubscription).where(PushSubscription.user_id == callee.id))
    subs = res.scalars().all()
    if subs and settings.VAPID_PRIVATE_KEY:
        try:
            from pywebpush import webpush, WebPushException
            import json
            for sub in subs:
                try:
                    webpush(
                        subscription_info={
                            "endpoint": sub.endpoint,
                            "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key},
                        },
                        data=json.dumps({"title": f"Incoming call from {caller_name}", "body": "Tap to join", **payload}),
                        vapid_private_key=settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": settings.VAPID_SUBJECT},
                    )
                except WebPushException:
                    pass  # stale subscription — fine to ignore, cleaned up lazily
        except ImportError:
            pass  # pywebpush not installed in this environment yet


@router.post("/{booking_id}/call/start", response_model=CallStartResponse)
async def start_call(booking_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    booking, consumer_user, worker_user = await _load_booking_parties(db, booking_id)

    if current.id == consumer_user.id:
        caller, callee, caller_role = consumer_user, worker_user, "consumer"
    elif current.id == worker_user.id:
        caller, callee, caller_role = worker_user, consumer_user, "worker"
    else:
        raise HTTPException(status_code=403, detail="Not a party to this booking")

    # Reuse an existing meeting for this booking if one was already created
    # (e.g. redial), otherwise create a fresh Dyte meeting.
    res = await db.execute(
        select(CallSession).where(CallSession.booking_id == booking.id).order_by(CallSession.created_at.desc()).limit(1)
    )
    prior = res.scalar_one_or_none()
    if prior:
        meeting_id = prior.dyte_meeting_id
    else:
        meeting = await dyte_client.create_meeting(title=f"NurseConnect booking {booking.booking_ref}")
        meeting_id = meeting["id"]

    call_session = CallSession(
        booking_id=booking.id,
        dyte_meeting_id=meeting_id,
        initiated_by_user_id=caller.id,
        initiated_by_role=caller_role,
        callee_user_id=callee.id,
        status="ringing",
    )
    db.add(call_session)
    await db.commit()
    await db.refresh(call_session)

    participant = await dyte_client.add_participant(meeting_id, participant_name=caller.full_name or caller_role, participant_id=str(caller.id))

    await _ring_callee(db, callee, booking, call_session, caller_name=caller.full_name or caller_role)

    return CallStartResponse(
        call_session_id=call_session.id,
        dyte_meeting_id=meeting_id,
        dyte_auth_token=participant["authToken"],
        dyte_org_id=settings.DYTE_ORG_ID,
    )


@router.post("/{booking_id}/call/{call_session_id}/join", response_model=CallStartResponse)
async def join_call(booking_id: UUID, call_session_id: UUID, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CallSession).where(CallSession.id == call_session_id, CallSession.booking_id == booking_id))
    call_session = res.scalar_one_or_none()
    if not call_session:
        raise HTTPException(status_code=404, detail="Call not found")
    if current.id != call_session.callee_user_id and current.id != call_session.initiated_by_user_id:
        raise HTTPException(status_code=403, detail="Not a party to this call")

    if call_session.status == "ringing" and current.id == call_session.callee_user_id:
        call_session.status = "joined"
        call_session.callee_joined_at = datetime.now(timezone.utc)
        await db.commit()

    res = await db.execute(select(User).where(User.id == current.id))
    user = res.scalar_one()
    participant = await dyte_client.add_participant(call_session.dyte_meeting_id, participant_name=user.full_name or "participant", participant_id=str(user.id))

    return CallStartResponse(
        call_session_id=call_session.id,
        dyte_meeting_id=call_session.dyte_meeting_id,
        dyte_auth_token=participant["authToken"],
        dyte_org_id=settings.DYTE_ORG_ID,
    )


@router.post("/{booking_id}/call/{call_session_id}/end", response_model=CallSessionOut)
async def end_call(booking_id: UUID, call_session_id: UUID, body: CallEndRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CallSession).where(CallSession.id == call_session_id, CallSession.booking_id == booking_id))
    call_session = res.scalar_one_or_none()
    if not call_session:
        raise HTTPException(status_code=404, detail="Call not found")
    if current.id not in (call_session.callee_user_id, call_session.initiated_by_user_id):
        raise HTTPException(status_code=403, detail="Not a party to this call")

    if call_session.status not in ("ended", "missed", "failed"):
        now = datetime.now(timezone.utc)
        call_session.ended_at = now
        call_session.status = "ended" if call_session.callee_joined_at else "missed"
        call_session.end_reason = body.end_reason
        if call_session.callee_joined_at:
            call_session.duration_seconds = int((now - call_session.callee_joined_at).total_seconds())
        await db.commit()
        await db.refresh(call_session)

    await manager.broadcast(user_topic(call_session.callee_user_id), {"type": "call_ended", "call_session_id": str(call_session.id)})
    await manager.broadcast(user_topic(call_session.initiated_by_user_id), {"type": "call_ended", "call_session_id": str(call_session.id)})

    return CallSessionOut.model_validate(call_session)


push_router = APIRouter(prefix="/notifications", tags=["calls"])


@push_router.post("/push-subscribe")
async def push_subscribe(body: PushSubscribeRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Called from the frontend after the browser grants Notification
    permission and creates a PushSubscription via the service worker."""
    res = await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    existing = res.scalar_one_or_none()
    if existing:
        existing.user_id = current.id
        existing.p256dh_key = body.p256dh_key
        existing.auth_key = body.auth_key
        existing.user_agent = body.user_agent
    else:
        db.add(PushSubscription(
            user_id=current.id,
            endpoint=body.endpoint,
            p256dh_key=body.p256dh_key,
            auth_key=body.auth_key,
            user_agent=body.user_agent,
        ))
    await db.commit()
    return {"subscribed": True}