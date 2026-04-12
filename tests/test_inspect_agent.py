"""Tests for fast_langchain_server.serverutils.inspect_agent."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.tools import tool

from fast_langchain_server.serverutils import inspect_agent


# ---------------------------------------------------------------------------
# Helpers — minimal mocks that mirror the LangGraph compiled graph structure
# ---------------------------------------------------------------------------


def _make_tool(name: str, description: str):
    """Create a mock tool object with the given name and description."""
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

    sys_msg = SystemMessage(content=system_prompt) if system_prompt else None

    def _prompt_func():
        pass  # closure vars set below

    # Inject _system_message into the closure of a real function
    # by building a proper closure via a factory
    def _make_prompt_func(msg):
        _system_message = msg
        def inner():
            return _system_message
        return inner

    prompt_func = _make_prompt_func(sys_msg)

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

    agent_func = _make_agent_func(static_model)

    agent_runnable = MagicMock()
    agent_runnable.func = agent_func

    agent_pregel = MagicMock()
    agent_pregel.bound = agent_runnable

    # ── Compiled graph ────────────────────────────────────────────────────────
    graph = MagicMock()
    graph.nodes = {
        "tools": tools_pregel,
        "agent": agent_pregel,
    }
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInspectAgentTools:
    def test_extracts_tools_from_compiled_graph(self):
        t1 = _make_tool("search", "Search the web")
        t2 = _make_tool("calculator", "Do math")
        graph = _make_compiled_graph(tools=[t1, t2])

        result = inspect_agent(graph)
        names = [t.name for t in result["tools"]]
        assert "search" in names
        assert "calculator" in names

    def test_returns_empty_tools_when_no_tools_node(self):
        graph = MagicMock()
        graph.nodes = {}
        result = inspect_agent(graph)
        assert result["tools"] == []

    def test_returns_empty_tools_for_arbitrary_object(self):
        result = inspect_agent(object())
        assert result["tools"] == []

    def test_tools_list_length_matches(self):
        tools = [_make_tool(f"tool_{i}", f"Tool {i}") for i in range(5)]
        graph = _make_compiled_graph(tools=tools)
        result = inspect_agent(graph)
        assert len(result["tools"]) == 5


class TestInspectAgentDescription:
    def test_extracts_system_prompt(self):
        graph = _make_compiled_graph(system_prompt="You are a math assistant.")
        result = inspect_agent(graph)
        assert result["description"] == "You are a math assistant."

    def test_returns_empty_string_when_no_system_prompt(self):
        graph = _make_compiled_graph(system_prompt="")
        result = inspect_agent(graph)
        assert result["description"] == ""

    def test_returns_empty_string_for_arbitrary_object(self):
        result = inspect_agent(object())
        assert result["description"] == ""


class TestInspectAgentModelName:
    def test_extracts_model_name(self):
        graph = _make_compiled_graph(model_name="gpt-4o")
        result = inspect_agent(graph)
        assert result["model_name"] == "gpt-4o"

    def test_returns_empty_string_when_no_model(self):
        graph = _make_compiled_graph(model_name="")
        result = inspect_agent(graph)
        assert result["model_name"] == ""

    def test_returns_empty_string_for_arbitrary_object(self):
        result = inspect_agent(object())
        assert result["model_name"] == ""


class TestInspectAgentCombined:
    def test_full_extraction(self):
        t = _make_tool("search", "Search the web")
        graph = _make_compiled_graph(
            tools=[t],
            system_prompt="You are a helpful assistant.",
            model_name="gpt-4o-mini",
        )
        result = inspect_agent(graph)
        assert len(result["tools"]) == 1
        assert result["tools"][0].name == "search"
        assert result["description"] == "You are a helpful assistant."
        assert result["model_name"] == "gpt-4o-mini"

    def test_never_raises_on_malformed_graph(self):
        """inspect_agent must never raise regardless of input."""
        for bad in [None, 42, "string", [], {}, object()]:
            result = inspect_agent(bad)
            assert isinstance(result, dict)
            assert "tools" in result
            assert "description" in result
            assert "model_name" in result


class TestAgentCardAutoDetect:
    """Integration: AgentServer uses inspect_agent when tools=None."""

    def test_card_auto_detects_tools(self, settings, memory):
        from fastapi.testclient import TestClient
        from fast_langchain_server.server import AgentServer

        t = _make_tool("my_auto_tool", "Auto detected tool")
        graph = _make_compiled_graph(
            tools=[t],
            system_prompt="Auto description.",
        )

        server = AgentServer(agent=graph, settings=settings, memory=memory, tools=None)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        skill_names = [s["name"] for s in card["skills"]]
        assert "my_auto_tool" in skill_names

    def test_card_uses_introspected_description_when_default(self, settings, memory):
        from fastapi.testclient import TestClient
        from fast_langchain_server.server import AgentServer

        graph = _make_compiled_graph(system_prompt="I am a specialized agent.")
        server = AgentServer(agent=graph, settings=settings, memory=memory)
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        assert card["description"] == "I am a specialized agent."

    def test_explicit_tools_override_auto_detection(self, settings, memory):
        from fastapi.testclient import TestClient
        from langchain_core.tools import tool
        from fast_langchain_server.server import AgentServer

        @tool
        def explicit_tool(x: int) -> int:
            "Explicit tool."
            return x

        auto_tool = _make_tool("auto_tool", "Would be auto-detected")
        graph = _make_compiled_graph(tools=[auto_tool])

        server = AgentServer(
            agent=graph, settings=settings, memory=memory, tools=[explicit_tool]
        )
        card = TestClient(server.app).get("/.well-known/agent.json").json()
        skill_names = [s["name"] for s in card["skills"]]
        assert "explicit_tool" in skill_names
        assert "auto_tool" not in skill_names
