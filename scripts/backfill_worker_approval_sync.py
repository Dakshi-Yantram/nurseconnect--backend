"""
One-time backfill: repair workers whose reviewer ticket already shows
APPROVED/REJECTED (e.g. Raghava's "Stage 10 / Activated" ticket) but whose
WorkerProfile.onboarding_status was never updated, because that ticket
action was taken BEFORE the review_tickets.py <-> worker_approval.py sync
fix was deployed.

This does NOT need to run again after this one time — going forward,
approve/reject always goes through app/services/worker_approval.py from
both the admin endpoints and the reviewer-ticket endpoint, so ticket status
and WorkerProfile.onboarding_status can no longer drift apart.

Usage:
    python -m scripts.backfill_worker_approval_sync           # dry run (default)
    python -m scripts.backfill_worker_approval_sync --apply    # actually fix records
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.enums import WorkerOnboardingStatus
from app.models.models import NurseReviewTicket, WorkerProfile
from app.services.worker_approval import approve_worker_profile, reject_worker_profile


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        # Find every ticket that already says APPROVED but whose worker
        # profile disagrees.
        res = await db.execute(
            select(NurseReviewTicket, WorkerProfile)
            .join(WorkerProfile, WorkerProfile.id == NurseReviewTicket.nurse_id)
            .where(NurseReviewTicket.status == "APPROVED")
            .where(WorkerProfile.onboarding_status != WorkerOnboardingStatus.approved)
        )
        approved_mismatches = res.all()

        res = await db.execute(
            select(NurseReviewTicket, WorkerProfile)
            .join(WorkerProfile, WorkerProfile.id == NurseReviewTicket.nurse_id)
            .where(NurseReviewTicket.status == "REJECTED")
            .where(WorkerProfile.onboarding_status != WorkerOnboardingStatus.rejected)
        )
        rejected_mismatches = res.all()

        print(f"Found {len(approved_mismatches)} ticket(s) marked APPROVED whose worker is not approved:")
        for ticket, wp in approved_mismatches:
            print(f"  - worker_id={wp.id} name={getattr(wp, 'full_name', '?')!r} "
                  f"current onboarding_status={wp.onboarding_status!r} ticket={ticket.id}")

        print(f"\nFound {len(rejected_mismatches)} ticket(s) marked REJECTED whose worker is not rejected:")
        for ticket, wp in rejected_mismatches:
            print(f"  - worker_id={wp.id} name={getattr(wp, 'full_name', '?')!r} "
                  f"current onboarding_status={wp.onboarding_status!r} ticket={ticket.id}")

        if not apply:
            print("\nDry run only — no changes made. Re-run with --apply to fix these records.")
            return

        fixed, failed = 0, []
        for ticket, wp in approved_mismatches:
            try:
                await approve_worker_profile(db, wp.id)
                fixed += 1
            except Exception as e:  # noqa: BLE001 — report and keep going
                failed.append((wp.id, str(e)))

        for ticket, wp in rejected_mismatches:
            try:
                await reject_worker_profile(db, wp.id, "Backfilled: ticket was already rejected")
                fixed += 1
            except Exception as e:  # noqa: BLE001
                failed.append((wp.id, str(e)))

        await db.commit()
        print(f"\nFixed {fixed} worker(s).")
        if failed:
            print(f"{len(failed)} could not be auto-fixed (needs manual review):")
            for worker_id, err in failed:
                print(f"  - worker_id={worker_id}: {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write the fix (default is dry-run).")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
    sys.exit(0)
