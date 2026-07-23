"""
One-time backfill — re-run the tier→qualification bridge for every worker
who was already APPROVED before `sync_tier_qualifications` existed / was
wired into `approve_worker` and `set_worker_tier`.

Unlike `qualify_all_workers.py` (which force-approves every worker for
every service and is dev/test-only), this script calls the REAL
`sync_tier_qualifications()` used in production — it only unlocks what a
worker's current tier/training/certificates/assessments actually qualify
them for. Safe to run against production. Safe to re-run.

Symptom this fixes: an already-approved worker's "My Services" page shows
every service Locked with reason QUALIFICATION_RECORD_MISSING and no
"Request unlock" option, because no WorkerServiceQualification row was ever
created for them.

USAGE (from backend/ folder):

    python backfill_worker_qualifications.py
"""
import asyncio
import sys

from app.core.database import AsyncSessionLocal, engine
from app.models.enums import WorkerOnboardingStatus
from app.models.models import WorkerProfile
from app.services.qualification import sync_tier_qualifications
from sqlalchemy import select


async def main():
    print("NurseConnect — backfill qualification rows for approved workers")
    print("=" * 65)

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(WorkerProfile).where(
                WorkerProfile.onboarding_status == WorkerOnboardingStatus.approved
            )
        )
        workers = res.scalars().all()
        if not workers:
            print("No approved workers found. Nothing to do.")
            return

        print(f"Found {len(workers)} approved worker(s).\n")

        for worker in workers:
            updated = await sync_tier_qualifications(session, worker)
            print(f"Worker {worker.id} (tier={worker.tier.value}): {len(updated)} rows synced")

        await session.commit()

    print("\n" + "=" * 65)
    print("Done. Re-synced qualifications for all approved workers based on")
    print("their real tier/training/certificate/assessment status.")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)