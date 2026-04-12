"""
Middleware system for fast-langchain-server.

Inspired by FastMCP's Middleware pattern: a chain of objects that intercept
the request/response cycle at the agent level, not just the HTTP level.

Architecture
------------
Each middleware in the chain receives an ``AgentContext`` and a ``call_next``
callable.  Calling ``call_next(ctx)`` passes control to the next middleware
(or to the actual agent handler at the end of the chain).

Execution order (3 middlewares A, B, C added in that order):

    Request → A.on_request → B.on_request → C.on_request → handler
    Response ← A.on_request ← B.on_request ← C.on_request ←

To reject a request: raise ``HTTPException`` (or any exception).
Do NOT return ``None`` to signal rejection — the chain expects a result.

Built-in middlewares
--------------------
AuthMiddleware          — token verification via any AuthProvider
TimingMiddleware        — logs elapsed time per request
RateLimitMiddleware     — per-session token-bucket rate limiting
"""
from __future__ import annotations

import logging
import time
from abc import ABC
from collections import defaultdict
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from fast_langchain_server.context import AgentContext

logger = logging.getLogger(__name__)

# Type alias: a function that takes a context and returns a result (awaitable)
CallNext = Callable[[AgentContext], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Base middleware
# ---------------------------------------------------------------------------


class AgentMiddleware(ABC):
    """Base class for all agent-level middleware.

    Subclass and override the hooks you need.  All hooks default to a simple
    pass-through (call_next) so you only override what you need.

    Hooks
    -----
    on_request(ctx, call_next)
        Wraps the entire request lifecycle — from header parsing to response
        returned.  Use this for auth, rate-limiting, timing, etc.

    on_agent_run(ctx, call_next)
        Wraps only the agent invocation step (after session/history is loaded).
        Useful for response inspection or input/output transformation.
    """

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        """Called once per chat request, before the agent is invoked."""
        return await call_next(ctx)

    async def on_agent_run(self, ctx: AgentContext, call_next: CallNext) -> Any:
        """Called immediately before (and after) the LangGraph agent runs."""
        return await call_next(ctx)


# ---------------------------------------------------------------------------
# Middleware chain executor
# ---------------------------------------------------------------------------


def build_middleware_chain(
    middlewares: list[AgentMiddleware],
    handler: Callable[[AgentContext], Awaitable[Any]],
    hook: str = "on_request",
) -> Callable[[AgentContext], Awaitable[Any]]:
    """Build a callable chain from a list of middlewares and a terminal handler.

    Parameters
    ----------
    middlewares:
        Ordered list of middlewares.  First in list = outermost wrapper.
    handler:
        The final handler invoked when all middlewares call ``call_next``.
    hook:
        Name of the method on each middleware to use (e.g. ``"on_request"``).

    Returns
    -------
    A single async callable ``(ctx) -> Any`` that runs the full chain.
    """

    async def _final(ctx: AgentContext) -> Any:
        return await handler(ctx)

    chain = _final
    for mw in reversed(middlewares):
        method = getattr(mw, hook)
        # Capture current chain in closure
        _next = chain

        async def _wrap(ctx: AgentContext, _m=method, _n=_next) -> Any:
            return await _m(ctx, _n)

        chain = _wrap

    return chain


# ---------------------------------------------------------------------------
# Built-in: AuthMiddleware
# ---------------------------------------------------------------------------


class AuthMiddleware(AgentMiddleware):
    """Verifies incoming requests using an ``AuthProvider``.

    Reads the ``Authorization: Bearer <token>`` header (or the ``X-API-Key``
    header as a fallback).  On success, sets ``ctx.set_meta("auth_token", ...)``
    so downstream middlewares and handlers can access the token.

    Parameters
    ----------
    provider:
        Any ``AuthProvider`` instance (or a ``MultiAuth`` chain built with
        ``provider_a | provider_b``).
    exclude:
        Set of endpoint paths that bypass authentication.  Health/readiness
        probes and the A2A discovery card are excluded by default.
    header:
        Primary header to read the token from.  Default: ``Authorization``
        (expects ``Bearer <token>`` format).
    fallback_header:
        Secondary header checked when the primary is absent.
        Default: ``X-API-Key`` (raw key, no prefix needed).

    Example
    -------
    from fast_langchain_server.auth import EnvAPIKeyProvider
    from fast_langchain_server.middleware import AuthMiddleware

    server.add_middleware(AuthMiddleware(provider=EnvAPIKeyProvider()))
    """

    _DEFAULT_EXCLUDE = frozenset({
        "/health",
        "/ready",
        "/.well-known/agent.json",
    })

    def __init__(
        self,
        provider: Any,  # AuthProvider — avoid circular import in type hint
        exclude: set[str] | None = None,
        header: str = "authorization",
        fallback_header: str = "x-api-key",
    ) -> None:
        self._provider = provider
        self._exclude = self._DEFAULT_EXCLUDE | (exclude or set())
        self._header = header.lower()
        self._fallback_header = fallback_header.lower()

    def _extract_token(self, ctx: AgentContext) -> str:
        """Pull the raw token string from request headers."""
        # Primary header: "Authorization: Bearer sk-abc"
        auth_value = ctx.headers.get(self._header, "")
        if auth_value:
            # Strip "Bearer " prefix if present
            return auth_value.removeprefix("Bearer ").removeprefix("bearer ").strip()

        # Fallback: "X-API-Key: sk-abc"
        return ctx.headers.get(self._fallback_header, "").strip()

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        if ctx.endpoint in self._exclude:
            return await call_next(ctx)

        token_str = self._extract_token(ctx)
        token = await self._provider.verify_token(token_str)

        if token is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        ctx.set_meta("auth_token", token)
        return await call_next(ctx)


# ---------------------------------------------------------------------------
# Built-in: TimingMiddleware
# ---------------------------------------------------------------------------


class TimingMiddleware(AgentMiddleware):
    """Logs the wall-clock time taken to process each request.

    Example log output::

        [INFO] POST /v1/chat/completions session=abc123 elapsed=1.234s

    Parameters
    ----------
    log_level:
        Python logging level name.  Default: ``"INFO"``.
    """

    def __init__(self, log_level: str = "INFO") -> None:
        self._level = getattr(logging, log_level.upper(), logging.INFO)

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        start = time.monotonic()
        try:
            result = await call_next(ctx)
            elapsed = time.monotonic() - start
            logger.log(
                self._level,
                "%s session=%s elapsed=%.3fs",
                ctx.endpoint or "request",
                ctx.session_id,
                elapsed,
            )
            return result
        except Exception:
            elapsed = time.monotonic() - start
            logger.log(
                self._level,
                "%s session=%s elapsed=%.3fs [error]",
                ctx.endpoint or "request",
                ctx.session_id,
                elapsed,
            )
            raise


# ---------------------------------------------------------------------------
# Built-in: RateLimitMiddleware
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Simple token-bucket implementation (not thread-safe — async only)."""

    def __init__(self, max_per_minute: int) -> None:
        self._capacity = max_per_minute
        self._tokens = float(max_per_minute)
        self._last_refill = time.monotonic()
        self._rate = max_per_minute / 60.0  # tokens per second

    def acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class RateLimitMiddleware(AgentMiddleware):
    """Per-session token-bucket rate limiter.

    Parameters
    ----------
    max_rpm:
        Maximum requests per minute per session.  Default: 60.

    Example
    -------
    server.add_middleware(RateLimitMiddleware(max_rpm=30))
    """

    def __init__(self, max_rpm: int = 60) -> None:
        self._max_rpm = max_rpm
        self._buckets: dict[str, _TokenBucket] = defaultdict(
            lambda: _TokenBucket(max_rpm)
        )

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        bucket = self._buckets[ctx.session_id]
        if not bucket.acquire():
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({self._max_rpm} req/min per session)",
            )
        return await call_next(ctx)
