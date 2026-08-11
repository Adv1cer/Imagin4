"""Create (or reuse) a service-account user and mint a new API key for it.

For machine-to-machine callers (see app/api/v1/agent_router.py,
app/domain/auth/api_keys.py) -- e.g. a university's own chatbot workflow that forwards
many real end users' messages through this backend using one shared credential. Never
gives that credential a real person's password/login: the created user has
password_hash=None (see users.password_hash's "nullable when using external IdP" design)
so it can never log in through POST /v1/auth/login at all, only ever authenticate via
the minted API key.

The raw key is printed ONCE and never stored anywhere (only its hash is persisted) --
copy it immediately into wherever the caller needs it (e.g. the HTTP Request node's
Authorization header). Re-running this script for the same --email adds a NEW key for
the same service user rather than reusing/reprinting the old one; the old key keeps
working until separately revoked.

Usage (from inside the api container, where DATABASE_URL points at pgbouncer/postgres):
    docker compose run --rm api python -m scripts.create_api_key \\
        --email utcc-agent@service.internal --label "UTCC agent workflow"
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import ApiKey, User
from app.domain.auth.api_keys import issue_api_key

DEFAULT_LABEL = "service account"


async def create_key(email: str, display_name: str, label: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                password_hash=None,  # service account: API-key-only, can never log in
                display_name=display_name,
                status="active",
                plan_code="standard",
            )
            session.add(user)
            await session.flush()
            print(f"create_api_key: created service user email={email!r} id={user.id}")
        else:
            print(f"create_api_key: reusing existing user email={email!r} id={user.id}")

        issued = issue_api_key()
        session.add(ApiKey(user_id=user.id, key_hash=issued.key_hash, label=label))
        await session.commit()

        print(f"create_api_key: minted key label={label!r} for user_id={user.id}")
        print()
        print("Raw key (shown once -- copy it now, it is not recoverable afterward):")
        print(f"  {issued.raw_key}")
        print()
        print("Use it as:  Authorization: Bearer " + issued.raw_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="service-account user email (unique)")
    parser.add_argument(
        "--name",
        default=None,
        dest="display_name",
        help="display name for a newly-created user (defaults to the email's local part)",
    )
    parser.add_argument("--label", default=DEFAULT_LABEL, help="human-readable key label")
    args = parser.parse_args()
    display_name = args.display_name or args.email.split("@")[0]

    asyncio.run(create_key(args.email, display_name, args.label))


if __name__ == "__main__":
    main()
