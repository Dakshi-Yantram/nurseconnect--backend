"""Offline sync queue endpoints — for nurse partner app."""
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.deps import get_worker_profile
from app.models.enums import OfflineSyncStatus
from app.models.models import OfflineSyncQueue, WorkerProfile
from app.schemas.schemas import OfflineSyncBatch, OfflineSyncResult

router = APIRouter(prefix="/offline-sync", tags=["offline-sync"])


@router.post("/", response_model=List[OfflineSyncResult])
async def submit_batch(
    payload: OfflineSyncBatch,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    results: List[OfflineSyncResult] = []
    for item in payload.items:
        # idempotency via (device_id, local_id)
        existing_res = await db.execute(
            select(OfflineSyncQueue).where(OfflineSyncQueue.device_id == item.device_id, OfflineSyncQueue.local_id == item.local_id)
        )
        existing = existing_res.scalar_one_or_none()
        if existing:
            results.append(OfflineSyncResult(
                local_id=item.local_id,
                sync_status="duplicate",
                server_record_id=existing.server_record_id,
                error=f"already_recorded:{existing.sync_status.value}",
            ))
            continue
        q = OfflineSyncQueue(
            device_id=item.device_id,
            worker_id=profile.id,
            booking_id=item.booking_id,
            record_type=item.record_type,
            local_id=item.local_id,
            payload=item.payload,
            locally_recorded_at=item.locally_recorded_at,
            sync_status=OfflineSyncStatus.pending,
        )
        db.add(q)
        try:
            await db.flush()
            results.append(OfflineSyncResult(local_id=item.local_id, sync_status="pending"))
        except IntegrityError:
            await db.rollback()
            results.append(OfflineSyncResult(local_id=item.local_id, sync_status="conflict", error="duplicate"))
    await db.commit()
    return results


@router.get("/pending")
async def list_pending(profile: WorkerProfile = Depends(get_worker_profile), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(OfflineSyncQueue).where(OfflineSyncQueue.worker_id == profile.id, OfflineSyncQueue.sync_status == OfflineSyncStatus.pending)
    )
    return [
        {"id": str(q.id), "local_id": q.local_id, "record_type": q.record_type.value, "created_at": q.created_at.isoformat()}
        for q in res.scalars().all()
    ]
