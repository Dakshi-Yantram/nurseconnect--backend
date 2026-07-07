"""Tracking: live location updates + WebSocket subscription."""
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.deps import CurrentUser, get_current_user, get_worker_profile
from app.core.security import decode_token
from app.models.models import Booking, ConsumerProfile, User, WorkerLocationLog, WorkerProfile
from app.schemas.schemas import LocationUpdate
from app.security.access_control import assert_user_can_access_booking
from app.services.security_audit_service import (
    log_access_denied,
    log_ws_unauthorized,
)
from app.websockets.manager import booking_topic, manager, user_topic

router = APIRouter(tags=["tracking"])


@router.post("/tracking/location")
async def post_location(
    payload: LocationUpdate,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    log = WorkerLocationLog(
        worker_id=profile.id,
        booking_id=payload.booking_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_metres=payload.accuracy_metres,
        is_offline=payload.is_offline,
        synced_at=None if payload.is_offline else datetime.now(timezone.utc),
    )
    db.add(log)
    # Also stamp the worker's live location so geo-dispatch (which prefers a
    # fresh current location over the registered home) can actually see it.
    if not payload.is_offline:
        profile.current_latitude = payload.latitude
        profile.current_longitude = payload.longitude
        profile.current_location_updated_at = datetime.now(timezone.utc)
    await db.commit()
    if payload.booking_id:
        await manager.broadcast(
            booking_topic(payload.booking_id),
            {
                "type": "location.update",
                "worker_id": str(profile.id),
                "latitude": float(payload.latitude),
                "longitude": float(payload.longitude),
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    return {"ok": True}


@router.get("/tracking/booking/{booking_id}/latest")
async def latest_location(
    booking_id: UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    # Patch 5B — enforce booking ownership / assigned worker / admin.
    try:
        await assert_user_can_access_booking(db, current, booking_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            await log_access_denied(
                db,
                user_id=current.id,
                role=current.role.value,
                endpoint="GET /tracking/booking/{id}/latest",
                reason="tracking_booking_ownership",
                entity_type="booking",
                entity_id=booking_id,
            )
            await db.commit()
        raise
    res = await db.execute(
        select(WorkerLocationLog)
        .where(WorkerLocationLog.booking_id == booking_id)
        .order_by(WorkerLocationLog.recorded_at.desc())
        .limit(1)
    )
    last = res.scalar_one_or_none()
    if not last:
        return None
    return {
        "worker_id": str(last.worker_id),
        "latitude": float(last.latitude),
        "longitude": float(last.longitude),
        "accuracy_metres": last.accuracy_metres,
        "recorded_at": last.recorded_at.isoformat(),
    }


# ----- WebSocket: live tracking + escalation feed -----
async def _audit_ws_reject(user_id, role: str, endpoint: str, entity_type: str, entity_id, reason: str) -> None:
    """Patch 5B — record unauthorized websocket subscription attempts.

    Uses its own session because WS rejections happen before a request
    DB session is opened.
    """
    try:
        async with AsyncSessionLocal() as db:
            await log_ws_unauthorized(
                db,
                user_id=user_id,
                role=role,
                endpoint=endpoint,
                entity_type=entity_type,
                entity_id=entity_id,
                reason=reason,
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        pass


@router.websocket("/ws/booking/{booking_id}")
async def ws_booking(websocket: WebSocket, booking_id: UUID, token: str | None = None, db: AsyncSession = Depends(get_db)):
    # Auth via query token
    if not token:
        await _audit_ws_reject(None, "anonymous", "/ws/booking/{id}", "booking", booking_id, "missing_token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        claims = decode_token(token)
    except ValueError:
        await _audit_ws_reject(None, "anonymous", "/ws/booking/{id}", "booking", booking_id, "invalid_token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user_id = UUID(claims["sub"])
    role = claims.get("role", "")
    # Verify user has access
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    b = bres.scalar_one_or_none()
    if not b:
        await _audit_ws_reject(user_id, role, "/ws/booking/{id}", "booking", booking_id, "booking_not_found")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    cres = await db.execute(select(ConsumerProfile).where(ConsumerProfile.id == b.consumer_id))
    cp = cres.scalar_one_or_none()
    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == b.worker_id)) if b.worker_id else None
    wp = wres.scalar_one_or_none() if wres else None
    allowed = (cp and cp.user_id == user_id) or (wp and wp.user_id == user_id) or role.startswith("admin")
    if not allowed:
        await _audit_ws_reject(user_id, role, "/ws/booking/{id}", "booking", booking_id, "not_owner_or_assigned")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    topic = booking_topic(booking_id)
    await manager.connect(websocket, topic)
    try:
        while True:
            # keep-alive; client may send pings
            msg = await websocket.receive_text()
            # Echo pong for client heartbeat
            try:
                import json as _json
                parsed = _json.loads(msg) if msg else None
                if isinstance(parsed, dict) and parsed.get("type") == "ping":
                    await websocket.send_text(_json.dumps({"type": "pong", "ts": parsed.get("ts")}))
            except Exception:
                # Non-JSON frames are ignored
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket, topic)


@router.websocket("/ws/user")
async def ws_user(websocket: WebSocket, token: str | None = None):
    if not token:
        await _audit_ws_reject(None, "anonymous", "/ws/user", "ws_user", None, "missing_token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        claims = decode_token(token)
    except ValueError:
        await _audit_ws_reject(None, "anonymous", "/ws/user", "ws_user", None, "invalid_token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user_id = UUID(claims["sub"])
    topic = user_topic(user_id)
    await manager.connect(websocket, topic)
    try:
        while True:
            msg = await websocket.receive_text()
            try:
                import json as _json
                parsed = _json.loads(msg) if msg else None
                if isinstance(parsed, dict) and parsed.get("type") == "ping":
                    await websocket.send_text(_json.dumps({"type": "pong", "ts": parsed.get("ts")}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket, topic)
