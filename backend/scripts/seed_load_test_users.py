"""Seed N distinct users for the "100 concurrent users" load-test scenario
(load_tests/hundred_concurrent_burst.js).

Why distinct users, not one user at N VUs: app/core/config.py's
`max_active_jobs_per_user=1` and `rl_generation_per_min` are PER-USER limits. Hammering
POST /v1/generations with one login at 100 VUs mostly measures rate-limit shedding (like
spike.js already does), not "100 different people generating at once" -- it needs 100
distinct accounts, each submitting their own one job, to actually model that.

Deterministic naming so the k6 script (VU index -> email) never has to read a file this
script writes:
    loadtest-user-0001@example.edu .. loadtest-user-0100@example.edu
    password: correct-horse-battery-staple  (same for all -- load-test only, not prod)

Idempotent (same upsert-by-email logic as seed_dev_user.py) -- safe to re-run.

Usage (from inside the api container/venv, where DATABASE_URL points at pgbouncer/postgres):
    docker compose run --rm api python -m scripts.seed_load_test_users
    docker compose run --rm api python -m scripts.seed_load_test_users --count 100
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import User
from app.domain.auth.passwords import hash_password

EMAIL_TEMPLATE = "loadtest-user-{i:04d}@example.edu"
DEFAULT_PASSWORD = "correct-horse-battery-staple"
DEFAULT_COUNT = 100


async def _seed_one(factory, email: str, password: str, display_name: str) -> str:
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
        return action


async def seed_many(count: int, password: str) -> None:
    factory = get_session_factory()
    created = 0
    updated = 0
    for i in range(1, count + 1):
        email = EMAIL_TEMPLATE.format(i=i)
        action = await _seed_one(factory, email, password, f"Load Test User {i:04d}")
        if action == "created":
            created += 1
        else:
            updated += 1
        if i % 20 == 0 or i == count:
            print(f"seed_load_test_users: {i}/{count} done (created={created} updated={updated})")

    print(
        f"seed_load_test_users: finished. {created} created, {updated} updated. "
        f"Emails: {EMAIL_TEMPLATE.format(i=1)} .. {EMAIL_TEMPLATE.format(i=count)}, "
        f"password={password!r}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    asyncio.run(seed_many(args.count, args.password))


if __name__ == "__main__":
    main()
