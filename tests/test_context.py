"""Tests for fast_langchain_server.context — AgentContext."""
from __future__ import annotations

import pytest

from fast_langchain_server.context import AgentContext


class TestAgentContextFactory:
    def test_from_request_generates_request_id(self):
        ctx = AgentContext.from_request(
            session_id="sess-1",
            user_input="hello",
        )
        assert ctx.request_id
        assert len(ctx.request_id) == 36  # UUID format

    def test_from_request_two_calls_produce_different_ids(self):
        ctx1 = AgentContext.from_request(session_id="s", user_input="hi")
        ctx2 = AgentContext.from_request(session_id="s", user_input="hi")
        assert ctx1.request_id != ctx2.request_id

    def test_from_request_defaults(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        assert ctx.model == "agent"
        assert ctx.otel_context is None
        assert ctx._emit is None
        assert ctx.request is None

    def test_from_request_custom_model(self):
        ctx = AgentContext.from_request(
            session_id="s", user_input="hi", model="gpt-4o"
        )
        assert ctx.model == "gpt-4o"


class TestAgentContextMetadata:
    def test_set_and_get_meta(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx.set_meta("key", "value")
        assert ctx.get_meta("key") == "value"

    def test_get_meta_default(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        assert ctx.get_meta("missing") is None
        assert ctx.get_meta("missing", "fallback") == "fallback"

    def test_set_meta_overwrites(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx.set_meta("k", 1)
        ctx.set_meta("k", 2)
        assert ctx.get_meta("k") == 2

    def test_auth_token_property_none_by_default(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        assert ctx.auth_token is None

    def test_auth_token_property_reads_from_metadata(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx.set_meta("auth_token", "tok")
        assert ctx.auth_token == "tok"

    def test_endpoint_property(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx.set_meta("endpoint", "/v1/chat/completions")
        assert ctx.endpoint == "/v1/chat/completions"

    def test_endpoint_defaults_to_empty_string(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        assert ctx.endpoint == ""


class TestAgentContextEmitProgress:
    async def test_emit_progress_calls_emit_fn(self):
        received = []

        async def fake_emit(event: dict) -> None:
            received.append(event)

        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx._emit = fake_emit

        await ctx.emit_progress("tool_call", "search_web")

        assert len(received) == 1
        assert received[0] == {"type": "progress", "action": "tool_call", "target": "search_web"}

    async def test_emit_progress_noop_without_emit_fn(self):
        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        # Should not raise even when _emit is None
        await ctx.emit_progress("tool_call", "some_tool")

    async def test_emit_progress_multiple_calls(self):
        received = []

        async def fake_emit(event: dict) -> None:
            received.append(event)

        ctx = AgentContext.from_request(session_id="s", user_input="hi")
        ctx._emit = fake_emit

        await ctx.emit_progress("tool_call", "tool_a")
        await ctx.emit_progress("tool_call", "tool_b")

        assert len(received) == 2
        assert received[0]["target"] == "tool_a"
        assert received[1]["target"] == "tool_b"
