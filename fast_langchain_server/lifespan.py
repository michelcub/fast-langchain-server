"""
Composable lifespan system for fast-langchain-server.

Inspired by FastMCP's Lifespan pattern: each lifespan is an async generator
that runs setup code, yields a context dict, then runs teardown code.
Multiple lifespans compose with the ``|`` operator — they enter left-to-right
and exit right-to-left (LIFO), mirroring Python's nested context managers.

Usage
-----
Define a lifespan with the ``@lifespan`` decorator::

    from fast_langchain_server.lifespan import lifespan

    @lifespan
    async def db_lifespan(server):
        db = await connect_db()
        yield {"db": db}          # dict is merged into server.lifespan_context
        await db.close()          # teardown — always runs, even on cancellation

Compose multiple lifespans::

    @lifespan
    async def cache_lifespan(server):
        cache = Redis()
        yield {"cache": cache}
        await cache.aclose()

    combined = db_lifespan | cache_lifespan

Pass to ``Server``::

    server = Server(agent, tools=[...], lifespan=combined)

Access at runtime::

    db = server.lifespan_context["db"]

Built-in lifespans
------------------
The ``Server`` ships four built-in lifespans that replace the previous
monolithic ``_lifespan`` method:

  _otel_lifespan        — initialises OpenTelemetry
  _log_lifespan         — logs startup / shutdown messages
  _autonomous_lifespan  — launches the autonomous loop when configured
  _shutdown_lifespan    — shuts down task manager and memory on exit

They are pre-composed as ``DEFAULT_LIFESPAN`` and used automatically unless
the caller supplies a custom one.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

if TYPE_CHECKING:
    from fast_langchain_server.server import Server

logger = logging.getLogger(__name__)

# Type alias for the generator function a lifespan wraps
_LifespanFn = Callable[["Server"], AsyncGenerator[dict, None]]


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------


class Lifespan:
    """Wraps an async-generator function as a composable lifespan object.

    Created by the ``@lifespan`` decorator; not normally instantiated directly.
    """

    def __init__(self, fn: _LifespanFn) -> None:
        self._fn = fn
        # Preserve the wrapped function's name for debugging
        self.__name__ = getattr(fn, "__name__", repr(fn))

    def __or__(self, other: "Lifespan") -> "ComposedLifespan":
        """Compose two lifespans: ``self`` enters first, exits last."""
        return ComposedLifespan(self, other)

    @asynccontextmanager
    async def _as_cm(self, server: "Server"):
        """Run this lifespan as an async context manager, yielding its dict."""
        gen = self._fn(server)
        try:
            ctx: dict = await gen.__anext__()
        except StopAsyncIteration:
            ctx = {}
        try:
            yield ctx
        finally:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass

    async def __call__(
        self, server: "Server"
    ) -> AsyncGenerator[dict, None]:
        """Allow using a Lifespan directly as a FastAPI lifespan function."""
        async with self._as_cm(server) as ctx:
            yield ctx


class ComposedLifespan(Lifespan):
    """Two lifespans composed via ``|``.

    Enters ``left`` first, then ``right``.  Exits ``right`` first, then
    ``left`` (standard LIFO / nested context manager semantics).

    The yielded context dicts are merged: if both yield the same key,
    ``right`` wins.
    """

    def __init__(self, left: Lifespan, right: Lifespan) -> None:
        self.left = left
        self.right = right
        self.__name__ = f"{left.__name__} | {right.__name__}"
        # Provide a no-op _fn so the parent __init__ isn't needed
        self._fn = None  # type: ignore[assignment]

    @asynccontextmanager
    async def _as_cm(self, server: "Server"):
        async with self.left._as_cm(server) as lctx:
            async with self.right._as_cm(server) as rctx:
                yield {**lctx, **rctx}

    async def __call__(self, server: "Server") -> AsyncGenerator[dict, None]:
        async with self._as_cm(server) as ctx:
            yield ctx


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def lifespan(fn: _LifespanFn) -> Lifespan:
    """Decorator: turns an async generator into a composable ``Lifespan``.

    The decorated function must:
    - Accept a single positional argument: the ``Server`` instance.
    - ``yield`` exactly once, optionally yielding a ``dict`` that is merged
      into ``server.lifespan_context``.
    - Perform teardown after the ``yield`` (use ``try/finally`` for safety).

    Example::

        @lifespan
        async def my_lifespan(server):
            resource = await setup()
            yield {"resource": resource}
            await resource.close()
    """
    return Lifespan(fn)


# ---------------------------------------------------------------------------
# Built-in lifespans
# ---------------------------------------------------------------------------


@lifespan
async def _otel_lifespan(server: "Server") -> AsyncGenerator[dict, None]:
    """Initialise OpenTelemetry if enabled in settings."""
    from fast_langchain_server.telemetry import init_otel

    if server._settings.otel_active:
        init_otel(server._settings.agent_name)
    yield {}


@lifespan
async def _log_lifespan(server: "Server") -> AsyncGenerator[dict, None]:
    """Log startup and shutdown messages."""
    from fast_langchain_server.a2a import NullTaskManager
    from fast_langchain_server.telemetry import is_otel_enabled

    a2a_active = not isinstance(server._task_manager, NullTaskManager)
    logger.info(
        "Agent '%s' starting on port %d (memory=%s otel=%s a2a=%s)",
        server._settings.agent_name,
        server._settings.agent_port,
        server._settings.memory_type,
        is_otel_enabled(),
        a2a_active,
    )
    yield {}
    logger.info("Agent '%s' shutting down", server._settings.agent_name)


@lifespan
async def _autonomous_lifespan(server: "Server") -> AsyncGenerator[dict, None]:
    """Launch the autonomous loop at startup when configured."""
    from fast_langchain_server.a2a import AutonomousConfig, NullTaskManager

    a2a_active = not isinstance(server._task_manager, NullTaskManager)

    if server._settings.autonomous_goal and a2a_active:
        auto_cfg = AutonomousConfig(
            goal=server._settings.autonomous_goal,
            interval_seconds=server._settings.autonomous_interval_seconds,
            max_iter_runtime_seconds=server._settings.autonomous_max_iter_runtime_seconds,
        )
        logger.info(
            "Starting autonomous loop: goal='%s' interval=%ds",
            server._settings.autonomous_goal[:80],
            server._settings.autonomous_interval_seconds,
        )
        await server._task_manager.submit_autonomous(
            goal=server._settings.autonomous_goal,
            autonomous_config=auto_cfg,
        )
    elif server._settings.autonomous_goal and not a2a_active:
        logger.warning(
            "AUTONOMOUS_GOAL is set but TASK_MANAGER_TYPE=none — "
            "autonomous loop will not start. Set TASK_MANAGER_TYPE=local."
        )
    yield {}


@lifespan
async def _shutdown_lifespan(server: "Server") -> AsyncGenerator[dict, None]:
    """Gracefully shut down the task manager and memory backend on exit."""
    yield {}
    await server._task_manager.shutdown()
    await server._memory.close()


# ---------------------------------------------------------------------------
# Default composed lifespan used by Server
# ---------------------------------------------------------------------------

DEFAULT_LIFESPAN: Lifespan = (
    _otel_lifespan | _log_lifespan | _autonomous_lifespan | _shutdown_lifespan
)
