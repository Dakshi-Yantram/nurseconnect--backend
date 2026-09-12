import asyncio
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.models import Faq


async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Faq)
            .where(
                Faq.question == "What if I'm not happy with a visit?",
                Faq.audience == "consumer",
            )
            .order_by(Faq.id)
        )

        rows = result.scalars().all()

        for faq in rows:
            print("ID:", faq.id)
            print("Question:", faq.question)
            print("Audience:", faq.audience)
            print("Answer:", faq.answer)
            print("Active:", faq.is_active)
            print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())