"""
Shared fixtures for the test suite.

Uses a mock CompiledStateGraph so tests run without a real LLM endpoint.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fast_langchain_server.memory import LocalMemory, NullMemory
from fast_langchain_server.server import AgentServer
from fast_langchain_server.serverutils import AgentServerSettings


# ---------------------------------------------------------------------------
# Mock agent that simulates a CompiledStateGraph
# ---------------------------------------------------------------------------


def _make_mock_agent(response: str = "Hello from mock agent!") -> MagicMock:
    """Return a mock that behaves like a compiled LangGraph agent."""
    agent = MagicMock()

    # ainvoke returns {"messages": [input..., AIMessage(response)]}
    async def mock_ainvoke(input_dict: dict, **kwargs) -> dict:
        msgs = list(input_dict.get("messages", []))
        msgs.append(AIMessage(content=response))
        return {"messages": msgs}

    agent.ainvoke = mock_ainvoke

    # astream yields ("messages", chunk) and ("updates", delta) events
    async def mock_astream(input_dict: dict, stream_mode=None, **kwargs):
        msgs = list(input_dict.get("messages", []))
        ai_msg = AIMessage(content=response)
        # Simulate token-by-token streaming
        from langchain_core.messages import AIMessageChunk
        for char in response:
            yield ("messages", (AIMessageChunk(content=char), {"langgraph_node": "agent"}))
        # Emit update with full message
        yield ("updates", {"agent": {"messages": [ai_msg]}})

    agent.astream = mock_astream

    return agent


def _make_mock_agent_with_tool(
    tool_name: str = "search",
    tool_response: str = "Paris",
    final_response: str = "The answer is Paris.",
) -> MagicMock:
    """Return a mock agent that simulates one tool call before responding."""
    agent = MagicMock()

    async def mock_ainvoke(input_dict: dict, **kwargs) -> dict:
        msgs = list(input_dict.get("messages", []))
        tool_call_id = "call_abc123"
        ai_tool_call = AIMessage(
            content="",
            tool_calls=[
                {"id": tool_call_id, "name": tool_name, "args": {"query": "capital"}}
            ],
        )
        tool_msg = ToolMessage(content=tool_response, tool_call_id=tool_call_id)
        final_ai = AIMessage(content=final_response)
        msgs.extend([ai_tool_call, tool_msg, final_ai])
        return {"messages": msgs}

    agent.ainvoke = mock_ainvoke

    async def mock_astream(input_dict: dict, stream_mode=None, **kwargs):
        from langchain_core.messages import AIMessageChunk
        tool_call_id = "call_abc123"

        # Chunk with tool_call_chunk
        yield (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"id": tool_call_id, "name": tool_name, "args": "", "index": 0}
                    ],
                ),
                {"langgraph_node": "agent"},
            ),
        )
        # Tool result update
        tool_msg = ToolMessage(content=tool_response, tool_call_id=tool_call_id)
        yield ("updates", {"tools": {"messages": [tool_msg]}})

        # Final response tokens
        for char in final_response:
            yield (
                "messages",
                (AIMessageChunk(content=char), {"langgraph_node": "agent"}),
            )

        final_ai = AIMessage(content=final_response)
        yield ("updates", {"agent": {"messages": [final_ai]}})

    agent.astream = mock_astream

    return agent


# ---------------------------------------------------------------------------
# Settings fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> AgentServerSettings:
    return AgentServerSettings(
        agent_name="test-agent",
        model_api_url="http://localhost:11434/v1",
        model_name="test-model",
        agent_port=8765,
        memory_enabled=True,
        memory_type="local",
    )


# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_agent():
    return _make_mock_agent()


@pytest.fixture
def mock_agent_with_tool():
    return _make_mock_agent_with_tool()


@pytest.fixture
def memory():
    return LocalMemory(max_sessions=10)


@pytest.fixture
def server(mock_agent, settings, memory) -> AgentServer:
    return AgentServer(agent=mock_agent, settings=settings, memory=memory)


@pytest.fixture
def client(server: AgentServer) -> TestClient:
    return TestClient(server.app)


@pytest.fixture
async def async_client(server: AgentServer) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(app=server.app, base_url="http://test") as ac:
        yield ac
