"""Ownership guard: every resource query must be scoped to the authenticated user_id.
Never trust a client-supplied user_id anywhere in the request body/query string."""

from __future__ import annotations


class NotOwnerError(PermissionError):
    pass


def assert_owner(resource_owner_user_id: str, authenticated_user_id: str) -> None:
    if str(resource_owner_user_id) != str(authenticated_user_id):
        raise NotOwnerError("resource does not belong to the authenticated user")
