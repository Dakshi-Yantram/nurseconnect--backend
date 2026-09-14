import asyncio
from datetime import datetime, timezone
from app.core.database import AsyncSessionLocal, engine
from app.models.models import ConsentRecord, ConsumerProfile
from sqlalchemy import select

PATIENT_ID = "470ae8ab-fafd-4903-b3c3-db97e1cf586c"
BOOKING_ID = "467ef2b3-55e8-4f74-86d6-bf909efa51c7"
CONSUMER_PROFILE_ID = "0f3776a3-c7bb-4723-a83c-42c5243e7076"


async def main():
    async with AsyncSessionLocal() as session:
        # consumer_profiles.id se user_id nikalna hai
        result = await session.execute(
            select(ConsumerProfile).where(ConsumerProfile.id == CONSUMER_PROFILE_ID)
        )
        profile = result.scalar_one_or_none()

        if not profile:
            print(f"Consumer profile not found for id={CONSUMER_PROFILE_ID}")
            return

        if not profile.user_id:
            print("Consumer profile has no linked user_id — cannot create consent record")
            return

        # Duplicate consent na bane, isliye check kar lo
        existing = await session.execute(
            select(ConsentRecord).where(
                ConsentRecord.patient_id == PATIENT_ID,
                ConsentRecord.booking_id == BOOKING_ID,
                ConsentRecord.consent_type == "service",
            )
        )
        if existing.scalar_one_or_none():
            print("Service consent already exists for this booking — skipping insert")
            return

        consent = ConsentRecord(
            patient_id=PATIENT_ID,
            booking_id=BOOKING_ID,
            consent_type="service",
            consented_by_user_id=profile.user_id,
            consented_by_name="Test Consumer",
            capture_method="digital_checkbox",
            status="given",
            given_at=datetime.now(timezone.utc),
        )
        session.add(consent)
        await session.commit()
        print(f"Consent created: id={consent.id}, type=service, patient={PATIENT_ID}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())