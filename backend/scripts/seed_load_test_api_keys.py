"""Seed N distinct load-test users AND mint one API key each, for
hundred_concurrent_burst.js's --auth=bearer mode.

Why this exists: the first run of hundred_concurrent_burst.js drove 100 concurrent
POST /v1/auth/login calls (Argon2id password verification, one DB write per session) and
got back 500s under that burst -- see app/domain/auth/passwords.py (Argon2id is
deliberately CPU-heavy) and APP_DB_POOL_SIZE=5/APP_DB_MAX_OVERFLOW=5 (10 total
connections) in .env. That's a real finding worth its own investigation (see README's
new "Known issue" note), but it also isn't what this test is trying to measure --
Bearer API keys skip password verification and session-row creation entirely
(app/api/deps.py::_resolve_api_key_user is a pure key-hash lookup), so this sidesteps
the login bottleneck to isolate ComfyUI/GPU dispatch capacity instead.

Reuses the SAME 100 distinct users as scripts/seed_load_test_users.py (one API key per
user, not one shared service-account key) -- deliberately, so admission fairness
(max_active_jobs_per_user=1 etc.) is still exercised per-distinct-user, not collapsed
onto a single account.

Raw keys are only ever shown once (see scripts/create_api_key.py's docstring) -- this
script prints them as JSON to STDOUT (redirect it), with progress on STDERR so
redirecting stdout gives you clean JSON:

    docker compose run --rm api python -m scripts.seed_load_test_api_keys --count 100 \\
        > load_tests/loadtest_api_keys.json

Re-running ADDS a new key per user each time (create_api_key.py's existing behavior) --
old keys for the same user keep working. If you just want to regenerate the JSON file
without minting fresh keys, this script has no "list existing keys" mode (raw keys are
never stored, by design) -- re-run it to get a fresh valid set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import ApiKey, User
from app.domain.auth.api_keys import issue_api_key

EMAIL_TEMPLATE = "loadtest-user-{i:04d}@example.edu"
DEFAULT_COUNT = 100


async def _seed_one(factory, email: str, display_name: str, label: str) -> str:
    async with factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                password_hash=None,
                display_name=display_name,
                status="active",
                plan_code="standard",
            )
            session.add(user)
            await session.flush()
        elif user.status != "active":
            user.status = "active"

        issued = issue_api_key()
        session.add(ApiKey(user_id=user.id, key_hash=issued.key_hash, label=label))
        await session.commit()
        return issued.raw_key


async def seed_many(count: int) -> list[dict]:
    factory = get_session_factory()
    keys: list[dict] = []
    for i in range(1, count + 1):
        email = EMAIL_TEMPLATE.format(i=i)
        raw_key = await _seed_one(factory, email, f"Load Test User {i:04d}", "hundred_concurrent_burst.js")
        keys.append({"email": email, "api_key": raw_key})
        if i % 20 == 0 or i == count:
            print(f"seed_load_test_api_keys: {i}/{count} done", file=sys.stderr)
    return keys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    args = parser.parse_args()

    keys = asyncio.run(seed_many(args.count))
    print(f"seed_load_test_api_keys: minted {len(keys)} keys, writing JSON to stdout", file=sys.stderr)
    print(json.dumps(keys, indent=2))


if __name__ == "__main__":
    main()
