import asyncio
from app.core.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            ALTER TABLE worker_profiles
            ALTER COLUMN date_of_birth SET DEFAULT '1995-01-01',
            ALTER COLUMN registration_no SET DEFAULT 'PENDING',
            ALTER COLUMN registration_authority SET DEFAULT 'Pending Verification',
            ALTER COLUMN registration_valid_until SET DEFAULT '2030-01-01',
            ALTER COLUMN base_city SET DEFAULT 'Not Set'
        """))
        await session.commit()
        print("Database defaults set! All FUTURE new workers will auto-fill these fields.")

asyncio.run(main())