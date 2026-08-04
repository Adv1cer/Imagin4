"""Seed a safe local development user.

Not for production use -- this is meant for local docker-compose / k6 smoke testing
only. Idempotent: re-running with the same email just updates the password hash
instead of failing on the unique constraint.

Usage (from inside the api container, where DATABASE_URL points at pgbouncer/postgres):
    docker compose run --rm api python -m scripts.seed_dev_user
    docker compose run --rm api python -m scripts.seed_dev_user --email a@b.edu --password x --name "A B"

Defaults match backend/load_tests/smoke.js so `docker compose --profile load-test
run --rm k6 run /scripts/smoke.js` works out of the box after seeding.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import User
from app.domain.auth.passwords import hash_password

DEFAULT_EMAIL = "student@example.edu"
DEFAULT_PASSWORD = "correct-horse-battery-staple"
DEFAULT_NAME = "Smoke Test Student"


async def seed(email: str, password: str, display_name: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing is not None:
            existing.password_hash = hash_password(password)
            existing.display_name = display_name
            existing.status = "active"
            action = "updated"
        else:
            session.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    display_name=display_name,
                    status="active",
                    plan_code="standard",
                )
            )
            action = "created"

        await session.commit()
        print(f"seed_dev_user: {action} user email={email!r} display_name={display_name!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--name", default=DEFAULT_NAME, dest="display_name")
    args = parser.parse_args()

    asyncio.run(seed(args.email, args.password, args.display_name))


if __name__ == "__main__":
    main()
