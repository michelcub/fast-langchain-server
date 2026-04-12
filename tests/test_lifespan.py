"""Tests for fast_langchain_server.lifespan — composable lifespans."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fast_langchain_server.lifespan import (
    DEFAULT_LIFESPAN,
    ComposedLifespan,
    Lifespan,
    lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(**kwargs) -> MagicMock:
    """Return a minimal mock of AgentServer for lifespan tests."""
    server = MagicMock()
    server._settings = MagicMock(
        otel_active=False,
        agent_name="test-agent",
        agent_port=8000,
        memory_type="local",
        autonomous_goal="",
        autonomous_interval_seconds=0,
        autonomous_max_iter_runtime_seconds=60,
    )
    for k, v in kwargs.items():
        setattr(server._settings, k, v)
    server._task_manager = MagicMock()
    server._task_manager.__class__.__name__ = "NullTaskManager"
    server.lifespan_context = {}
    return server


# ---------------------------------------------------------------------------
# @lifespan decorator
# ---------------------------------------------------------------------------


class TestLifespanDecorator:
    def test_returns_lifespan_instance(self):
        @lifespan
        async def my_ls(server):
            yield {}

        assert isinstance(my_ls, Lifespan)

    def test_preserves_function_name(self):
        @lifespan
        async def my_custom_lifespan(server):
            yield {}

        assert my_custom_lifespan.__name__ == "my_custom_lifespan"

    async def test_runs_setup_and_teardown(self):
        order = []

        @lifespan
        async def tracked(server):
            order.append("setup")
            yield {}
            order.append("teardown")

        server = _make_server()
        async with tracked._as_cm(server):
            order.append("running")

        assert order == ["setup", "running", "teardown"]

    async def test_teardown_runs_on_exception(self):
        teardown_ran = []

        @lifespan
        async def safe_ls(server):
            yield {}
            teardown_ran.append(True)

        server = _make_server()
        with pytest.raises(RuntimeError):
            async with safe_ls._as_cm(server):
                raise RuntimeError("boom")

        assert teardown_ran == [True]

    async def test_yields_context_dict(self):
        @lifespan
        async def ctx_ls(server):
            yield {"key": "value"}

        server = _make_server()
        async with ctx_ls._as_cm(server) as ctx:
            assert ctx == {"key": "value"}

    async def test_yields_empty_dict_by_default(self):
        @lifespan
        async def empty_ls(server):
            yield {}

        server = _make_server()
        async with empty_ls._as_cm(server) as ctx:
            assert ctx == {}


# ---------------------------------------------------------------------------
# Lifespan | operator (ComposedLifespan)
# ---------------------------------------------------------------------------


class TestComposedLifespan:
    def test_pipe_produces_composed_lifespan(self):
        @lifespan
        async def a(server):
            yield {}

        @lifespan
        async def b(server):
            yield {}

        composed = a | b
        assert isinstance(composed, ComposedLifespan)

    def test_triple_pipe_flattens_names(self):
        @lifespan
        async def a(server):
            yield {}

        @lifespan
        async def b(server):
            yield {}

        @lifespan
        async def c(server):
            yield {}

        composed = a | b | c
        assert isinstance(composed, ComposedLifespan)

    async def test_composed_enters_left_first(self):
        order = []

        @lifespan
        async def left(server):
            order.append("left:setup")
            yield {}
            order.append("left:teardown")

        @lifespan
        async def right(server):
            order.append("right:setup")
            yield {}
            order.append("right:teardown")

        server = _make_server()
        async with (left | right)._as_cm(server):
            pass

        assert order[0] == "left:setup"
        assert order[1] == "right:setup"

    async def test_composed_exits_right_first(self):
        order = []

        @lifespan
        async def left(server):
            yield {}
            order.append("left:teardown")

        @lifespan
        async def right(server):
            yield {}
            order.append("right:teardown")

        server = _make_server()
        async with (left | right)._as_cm(server):
            pass

        assert order[0] == "right:teardown"
        assert order[1] == "left:teardown"

    async def test_composed_merges_context_dicts(self):
        @lifespan
        async def left(server):
            yield {"a": 1, "shared": "left"}

        @lifespan
        async def right(server):
            yield {"b": 2, "shared": "right"}

        server = _make_server()
        async with (left | right)._as_cm(server) as ctx:
            assert ctx["a"] == 1
            assert ctx["b"] == 2
            # Right wins on collision
            assert ctx["shared"] == "right"

    async def test_composed_teardown_runs_on_exception(self):
        teardowns = []

        @lifespan
        async def left(server):
            yield {}
            teardowns.append("left")

        @lifespan
        async def right(server):
            yield {}
            teardowns.append("right")

        server = _make_server()
        with pytest.raises(RuntimeError):
            async with (left | right)._as_cm(server):
                raise RuntimeError("boom")

        assert "left" in teardowns
        assert "right" in teardowns


# ---------------------------------------------------------------------------
# DEFAULT_LIFESPAN
# ---------------------------------------------------------------------------


class TestDefaultLifespan:
    def test_default_lifespan_is_composed(self):
        assert isinstance(DEFAULT_LIFESPAN, (Lifespan, ComposedLifespan))

    async def test_default_lifespan_runs_without_error(self):
        from fast_langchain_server.a2a import NullTaskManager
        from fast_langchain_server.memory import NullMemory

        server = _make_server()
        server._task_manager = NullTaskManager()
        server._memory = NullMemory()
        server._task_manager.shutdown = AsyncMock()
        server._memory.close = AsyncMock()

        async with DEFAULT_LIFESPAN._as_cm(server):
            pass  # just verify it doesn't raise

        server._task_manager.shutdown.assert_called_once()
        server._memory.close.assert_called_once()
