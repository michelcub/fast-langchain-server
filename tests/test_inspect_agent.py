"""Tests for inspect_agent and Server auto-discovery of tools."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.tools import tool

from fast_langchain_server.serverutils import inspect_agent
from fast_langchain_server.server import Server
from fast_langchain_server.memory import LocalMemory


# ---------------------------------------------------------------------------
# Helpers — minimal mocks that mirror the LangGraph compiled graph structure
# ---------------------------------------------------------------------------


def _make_tool(name: str, description: str):
    t = MagicMock()
    t.name = name
    t.description = description
    return t


def _make_compiled_graph(tools=None, system_prompt: str = "", model_name: str = ""):
    """Build a minimal mock of a CompiledStateGraph."""
    import inspect as _inspect

    # ── ToolNode mock ─────────────────────────────────────────────────────────
    tool_node = MagicMock()
    tool_node.tools_by_name = {t.name: t for t in (tools or [])}

    tools_pregel = MagicMock()
    tools_pregel.bound = tool_node

    # ── Prompt closure mock ───────────────────────────────────────────────────
    from langchain_core.messages import SystemMessage

    def _make_prompt_func(msg):
        _system_message = msg
        def inner():
            return _system_message
        return inner

    prompt_func = _make_prompt_func(
        SystemMessage(content=system_prompt) if system_prompt else None
    )
    prompt_step = MagicMock()
    prompt_step.func = prompt_func

    # ── Model mock ────────────────────────────────────────────────────────────
    bound_model = MagicMock()
    bound_model.model_name = model_name

    model_step = MagicMock()
    model_step.bound = bound_model

    # ── RunnableSequence mock ─────────────────────────────────────────────────
    static_model = MagicMock()
    static_model.steps = [prompt_step, model_step]

    def _make_agent_func(sm):
        static_model = sm
        def inner():
            return static_model
        return inner

    agent_runnable = MagicMock()
    agent_runnable.func = _make_agent_func(static_model)

    agent_pregel = MagicMock()
    agent_pregel.bound = agent_runnable

    # ── Compiled graph ────────────────────────────────────────────────────────
    graph = MagicMock()
    graph.nodes = {"tools": tools_pregel, "agent": agent_pregel}
    # ainvoke / astream needed by Server internals (never called in these tests)
    graph.ainvoke = MagicMock()
    graph.astream = MagicMock()
    return graph


_SERVER_KWARGS = dict(
    agent_name="inspect-test",
    model_api_url="http://localhost:11434/v1",
    model_name="test-model",
    agent_port=8765,
    memory_enabled=False,
    a2a=False,
)


# ---------------------------------------------------------------------------
# inspect_agent unit tests
# ---------------------------------------------------------------------------


class TestInspectAgentTools:
    def test_extracts_tools_from_compiled_graph(self):
        t1 = _make_tool("search", "Search the web")
        t2 = _make_tool("calculator", "Do math")
        result = inspect_agent(_make_compiled_graph(tools=[t1, t2]))
        names = [t.name for t in result["tools"]]
        assert "search" in names
        assert "calculator" in names

    def test_returns_empty_tools_when_no_tools_node(self):
        graph = MagicMock()
        graph.nodes = {}
        assert inspect_agent(graph)["tools"] == []

    def test_returns_empty_tools_for_arbitrary_object(self):
        assert inspect_agent(object())["tools"] == []

    def test_tools_list_length_matches(self):
        tools = [_make_tool(f"tool_{i}", f"Tool {i}") for i in range(5)]
        result = inspect_agent(_make_compiled_graph(tools=tools))
        assert len(result["tools"]) == 5


class TestInspectAgentDescription:
    def test_extracts_system_prompt(self):
        result = inspect_agent(_make_compiled_graph(system_prompt="You are a math assistant."))
        assert result["description"] == "You are a math assistant."

    def test_returns_empty_string_when_no_system_prompt(self):
        assert inspect_agent(_make_compiled_graph(system_prompt=""))["description"] == ""

    def test_returns_empty_string_for_arbitrary_object(self):
        assert inspect_agent(object())["description"] == ""


class TestInspectAgentModelName:
    def test_extracts_model_name(self):
        assert inspect_agent(_make_compiled_graph(model_name="gpt-4o"))["model_name"] == "gpt-4o"

    def test_returns_empty_string_when_no_model(self):
        assert inspect_agent(_make_compiled_graph(model_name=""))["model_name"] == ""

    def test_returns_empty_string_for_arbitrary_object(self):
        assert inspect_agent(object())["model_name"] == ""


class TestInspectAgentCombined:
    def test_full_extraction(self):
        t = _make_tool("search", "Search the web")
        result = inspect_agent(
            _make_compiled_graph(tools=[t], system_prompt="You are helpful.", model_name="gpt-4o-mini")
        )
        assert len(result["tools"]) == 1
        assert result["tools"][0].name == "search"
        assert result["description"] == "You are helpful."
        assert result["model_name"] == "gpt-4o-mini"

    def test_never_raises_on_malformed_graph(self):
        for bad in [None, 42, "string", [], {}, object()]:
            result = inspect_agent(bad)
            assert isinstance(result, dict)
            assert "tools" in result and "description" in result and "model_name" in result


# ---------------------------------------------------------------------------
# Integration: Server auto-discovery via inspect_agent
# ---------------------------------------------------------------------------


class TestServerAutoDiscovery:
    def test_card_auto_detects_tools_when_tools_not_passed(self):
        from fastapi.testclient import TestClient

        t = _make_tool("my_auto_tool", "Auto detected tool")
        graph = _make_compiled_graph(tools=[t])

        server = Server(graph, memory=LocalMemory(), **_SERVER_KWARGS)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        skill_names = [s["name"] for s in card["skills"]]
        assert "my_auto_tool" in skill_names

    def test_card_uses_introspected_description_when_not_set(self):
        from fastapi.testclient import TestClient

        graph = _make_compiled_graph(system_prompt="I am a specialized agent.")
        server = Server(graph, memory=LocalMemory(), **_SERVER_KWARGS)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        assert card["description"] == "I am a specialized agent."

    def test_explicit_tools_override_auto_detection(self):
        from fastapi.testclient import TestClient

        @tool
        def explicit_tool(x: int) -> int:
            "Explicit tool."
            return x

        auto_tool = _make_tool("auto_tool", "Would be auto-detected")
        graph = _make_compiled_graph(tools=[auto_tool])

        server = Server(graph, tools=[explicit_tool], memory=LocalMemory(), **_SERVER_KWARGS)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        skill_names = [s["name"] for s in card["skills"]]
        assert "explicit_tool" in skill_names
        assert "auto_tool" not in skill_names

    def test_explicit_empty_tools_disables_auto_detection(self):
        """tools=[] must be respected and NOT trigger auto-detection."""
        from fastapi.testclient import TestClient

        auto_tool = _make_tool("would_be_detected", "Should not appear")
        graph = _make_compiled_graph(tools=[auto_tool])

        server = Server(graph, tools=[], memory=LocalMemory(), **_SERVER_KWARGS)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        assert card["skills"] == []
