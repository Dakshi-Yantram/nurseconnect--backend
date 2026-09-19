import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import User
from app.core.security import hash_password
from sqlalchemy import select

def _prompt_password(env_var: str) -> str:
    """Read the password from the environment or prompt for it. Never
    hard-code credentials in the repo, and never print them."""
    import getpass
    import os
    import re as _re
    pw = os.environ.get(env_var) or getpass.getpass(f"New password ({env_var}): ")
    if (len(pw) < 12 or not _re.search(r"[A-Z]", pw) or not _re.search(r"[a-z]", pw)
            or not _re.search(r"\d", pw)):
        raise SystemExit("Password must be 12+ chars with upper, lower and a digit.")
    return pw


USER_EMAIL = "testworker@yantram.com"
NEW_PASSWORD = None  # set at runtime from $WORKER_NEW_PASSWORD or an interactive prompt
async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.email == USER_EMAIL))
        user = res.scalar_one_or_none()
        if not user:
            print("User not found")
            return
        user.password_hash = hash_password(_prompt_password("WORKER_NEW_PASSWORD"))
        await session.commit()
        print(f"Password reset for {USER_EMAIL}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())