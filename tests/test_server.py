"""
Integration tests for Server.

All tests use a mock CompiledStateGraph — no real LLM required.
"""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from fast_langchain_server.memory import LocalMemory
from fast_langchain_server.server import Server

from .conftest import _make_mock_agent, _make_mock_agent_with_tool, _SERVER_KWARGS


# ---------------------------------------------------------------------------
# Health & discovery
# ---------------------------------------------------------------------------


class TestHealthAndDiscovery:
    def test_health_returns_healthy(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["name"] == "test-agent"
        assert "timestamp" in body

    def test_ready_returns_healthy(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_agent_card_structure(self, client):
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        card = resp.json()
        assert card["name"] == "test-agent"
        assert "capabilities" in card
        assert card["capabilities"]["streaming"] is True
        assert "skills" in card

    def test_agent_card_includes_tools(self, memory):
        from langchain_core.tools import tool

        @tool
        def my_calculator(x: int) -> int:
            "A simple calculator."
            return x * 2

        server = Server(
            agent=_make_mock_agent(),
            tools=[my_calculator],
            memory=memory,
            **_SERVER_KWARGS,
        )
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        card = c.get("/.well-known/agent.json").json()
        skill_names = [s["name"] for s in card["skills"]]
        assert "my_calculator" in skill_names


# ---------------------------------------------------------------------------
# Non-streaming chat completions
# ---------------------------------------------------------------------------


class TestChatCompletions:
    def test_basic_response(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "Hello from mock agent!" in body["choices"][0]["message"]["content"]

    def test_session_id_in_header(self, client):
        resp = client.post(
            "/v1/chat/completions",
            headers={"X-Session-ID": "my-session"},
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 200

    def test_session_id_in_body(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "session_id": "body-session",
            },
        )
        assert resp.status_code == 200

    def test_no_messages_returns_400(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": []},
        )
        assert resp.status_code == 400

    def test_no_user_message_returns_400(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "assistant", "content": "Hi"}]},
        )
        assert resp.status_code == 400

    def test_multi_turn_memory(self):
        """Session memory persists across multiple requests."""
        memory = LocalMemory()
        server = Server(
            agent=_make_mock_agent("Turn response"),
            memory=memory,
            **_SERVER_KWARGS,
        )
        from fastapi.testclient import TestClient
        c = TestClient(server.app)

        session = "multi-turn-test"
        for i in range(3):
            resp = c.post(
                "/v1/chat/completions",
                headers={"X-Session-ID": session},
                json={"messages": [{"role": "user", "content": f"Message {i}"}]},
            )
            assert resp.status_code == 200

        sessions_resp = c.get("/memory/sessions")
        assert session in sessions_resp.json()["sessions"]


# ---------------------------------------------------------------------------
# Streaming chat completions
# ---------------------------------------------------------------------------


class TestStreaming:
    @pytest.mark.asyncio
    async def test_streaming_returns_sse_events(self, server: Server):
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "Stream me"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]

                events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            json_str = line[6:].strip()
                            if json_str:
                                events.append(json.loads(json_str))
                        except json.JSONDecodeError:
                            pass

                content_events = [
                    e for e in events
                    if e.get("object") == "chat.completion.chunk"
                ]
                assert len(content_events) > 0
                full_content = "".join(
                    e["choices"][0]["delta"]["content"]
                    for e in content_events
                    if e["choices"][0]["delta"].get("content")
                )
                assert full_content == "Hello from mock agent!"

    @pytest.mark.asyncio
    async def test_streaming_emits_progress_for_tool_calls(self, memory):
        server = Server(
            agent=_make_mock_agent_with_tool(),
            memory=memory,
            **_SERVER_KWARGS,
        )

        async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "Capital of France?"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200

                events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            json_str = line[6:].strip()
                            if json_str:
                                events.append(json.loads(json_str))
                        except json.JSONDecodeError:
                            pass

                progress_events = [e for e in events if e.get("type") == "progress"]
                assert len(progress_events) >= 1
                assert progress_events[0]["action"] == "tool_call"
                assert progress_events[0]["target"] == "search"


# ---------------------------------------------------------------------------
# Memory management endpoints
# ---------------------------------------------------------------------------


class TestMemoryEndpoints:
    def test_list_sessions_empty(self, client):
        resp = client.get("/memory/sessions")
        assert resp.status_code == 200
        assert "sessions" in resp.json()

    def test_list_sessions_after_chat(self, client):
        client.post(
            "/v1/chat/completions",
            headers={"X-Session-ID": "to-list"},
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        resp = client.get("/memory/sessions")
        assert "to-list" in resp.json()["sessions"]

    def test_delete_session(self, client):
        client.post(
            "/v1/chat/completions",
            headers={"X-Session-ID": "to-delete"},
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        resp = client.delete("/memory/sessions/to-delete")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "to-delete"

    def test_delete_nonexistent_session_returns_404(self, client):
        resp = client.delete("/memory/sessions/nonexistent")
        assert resp.status_code == 404
