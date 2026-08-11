"""Unit tests for the pure/importable parts of app/api/v1/agent_router.py that don't
require a database connection -- request-model validation and route wiring. The
DB-touching find-or-create-conversation-by-external_ref behavior and full end-to-end
conversation isolation need a real Postgres instance (this repo's existing
CITEXT/JSONB-backed auth/conversations code has the same limitation -- see
backend/README.md's Tests section) and are documented as needing manual/integration
verification rather than covered here.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.api.v1 import agent_router
from app.api.v1.agent_router import AgentMessageIn
from app.api.v1.chat_router import process_routed_message


def test_agent_router_is_mounted_under_agent_prefix():
    assert agent_router.router.prefix == "/agent"


def test_agent_message_route_exists_as_post():
    paths = {route.path: route.methods for route in agent_router.router.routes}
    assert "/agent/message" in paths
    assert "POST" in paths["/agent/message"]


def test_agent_message_in_accepts_a_normal_payload():
    payload = AgentMessageIn(external_conversation_id="utcc-student-2142", text="hi")
    assert payload.external_conversation_id == "utcc-student-2142"
    assert payload.text == "hi"
    assert payload.client_message_id is None


def test_agent_message_in_rejects_empty_external_conversation_id():
    with pytest.raises(ValidationError):
        AgentMessageIn(external_conversation_id="", text="hi")


def test_agent_message_in_rejects_empty_text():
    with pytest.raises(ValidationError):
        AgentMessageIn(external_conversation_id="utcc-student-2142", text="")


def test_agent_message_in_rejects_overlong_external_conversation_id():
    with pytest.raises(ValidationError):
        AgentMessageIn(external_conversation_id="x" * 201, text="hi")


def test_process_routed_message_is_shared_between_smart_message_and_agent_message():
    """Guards the refactor that pulled the classify/research/decide/enqueue pipeline out
    of smart_message() into a standalone function agent_router.py also calls -- if this
    import ever breaks, both entry points silently diverge instead of failing loudly."""
    assert inspect.iscoroutinefunction(process_routed_message)
    assert process_routed_message is agent_router.process_routed_message
