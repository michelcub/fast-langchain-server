"""
Authorization system for fast-langchain-server.

Inspired by FastMCP's authorization pattern: thin callables (AuthChecks) that
receive an AuthContext and return a bool.  They compose cleanly, are easy to
test in isolation, and can be async or sync.

Separation of concerns
----------------------
- **Authentication** (``auth.py``) answers "who are you?" — verifies a token
  and returns an ``AuthToken``.
- **Authorization** (this module) answers "what can you do?" — checks whether
  a verified identity is allowed to perform a given action.

AuthCheck
---------
Any callable with the signature ``(AuthContext) -> bool`` (sync or async).
Multiple checks can be composed with ``all_of`` / ``any_of`` helpers, or just
applied in a list inside ``AuthorizationMiddleware``.

Built-in checks
---------------
require_scopes(*scopes)     — ALL listed scopes must be in the token
allow_any_authenticated()   — any valid token is enough
allow_own_session()         — token subject must own the requested session
deny_all()                  — always denies (useful for maintenance mode)

Usage
-----
from fast_langchain_server.authorization import (
    AuthorizationMiddleware,
    require_scopes,
    allow_any_authenticated,
)

server.add_middleware(AuthorizationMiddleware({
    "/v1/chat/completions": require_scopes("chat"),
    "/memory/sessions":     require_scopes("admin"),
}))
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

from fastapi import HTTPException

from fast_langchain_server.context import AgentContext
from fast_langchain_server.middleware import AgentMiddleware, CallNext

if TYPE_CHECKING:
    from fast_langchain_server.auth import AuthToken

logger = logging.getLogger(__name__)

# Type alias: sync or async callable that receives an AuthContext
AuthCheck = Union[
    Callable[["AuthContext"], bool],
    Callable[["AuthContext"], Awaitable[bool]],
]


# ---------------------------------------------------------------------------
# AuthContext
# ---------------------------------------------------------------------------


@dataclass
class AuthContext:
    """Snapshot of authorization-relevant state for a single request.

    Built by ``AuthorizationMiddleware`` from the ``AgentContext`` and passed
    to every ``AuthCheck``.

    Attributes
    ----------
    token:
        The verified token set by ``AuthMiddleware``, or ``None`` if the
        request was not authenticated (e.g. an excluded endpoint).
    endpoint:
        HTTP path of the current request (e.g. ``"/v1/chat/completions"``).
    method:
        HTTP method (``"GET"``, ``"POST"``, ``"DELETE"``, …).
    session_id:
        Session identifier from the request, if any.
    """

    token: Optional["AuthToken"]
    endpoint: str
    method: str
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Check executor
# ---------------------------------------------------------------------------


async def run_auth_check(check: AuthCheck, ctx: AuthContext) -> bool:
    """Execute a sync or async ``AuthCheck`` uniformly.

    Any exception raised inside a check (other than ``HTTPException``) is
    caught, logged, and treated as a denial — following FastMCP's principle
    that unexpected errors should fail closed.
    """
    try:
        result = check(ctx)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except HTTPException:
        raise  # let HTTP errors propagate as-is
    except Exception as exc:
        logger.warning(
            "AuthCheck raised an unexpected exception — treating as denial: %s",
            exc,
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Built-in AuthChecks
# ---------------------------------------------------------------------------


def require_scopes(*scopes: str) -> AuthCheck:
    """Require ALL of the listed scopes to be present in the token.

    Returns ``False`` (deny) if the token is missing or any scope is absent.

    Example::

        check = require_scopes("chat", "memory:read")
    """

    def _check(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        return ctx.token.has_all_scopes(*scopes)

    return _check


def allow_any_authenticated() -> AuthCheck:
    """Allow any request that carries a valid (non-None) token."""

    def _check(ctx: AuthContext) -> bool:
        return ctx.token is not None

    return _check


def allow_own_session() -> AuthCheck:
    """Allow access only when the token subject owns the requested session.

    The session is considered "owned" if ``session_id`` starts with the
    token's ``subject``.  Requests with no session_id pass through.

    Example: token subject ``"user-42"`` may access session ``"user-42-abc"``
    but not ``"user-99-xyz"``.
    """

    def _check(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        if ctx.session_id is None:
            return True
        return ctx.session_id.startswith(ctx.token.subject)

    return _check


def deny_all() -> AuthCheck:
    """Deny every request.  Useful to put an endpoint in maintenance mode."""
    return lambda _: False


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------


def all_of(*checks: AuthCheck) -> AuthCheck:
    """Returns True only when ALL checks pass (AND logic)."""

    async def _check(ctx: AuthContext) -> bool:
        for check in checks:
            if not await run_auth_check(check, ctx):
                return False
        return True

    return _check


def any_of(*checks: AuthCheck) -> AuthCheck:
    """Returns True when ANY check passes (OR logic)."""

    async def _check(ctx: AuthContext) -> bool:
        for check in checks:
            if await run_auth_check(check, ctx):
                return True
        return False

    return _check


# ---------------------------------------------------------------------------
# AuthorizationMiddleware
# ---------------------------------------------------------------------------


class AuthorizationMiddleware(AgentMiddleware):
    """Applies per-endpoint authorization rules after authentication.

    Works in tandem with ``AuthMiddleware`` (which must be added first so
    that ``ctx.auth_token`` is populated before this middleware runs).

    Parameters
    ----------
    rules:
        Mapping of ``endpoint_path → AuthCheck``.  The endpoint path should
        match the value stored in ``ctx.endpoint`` (set by the chat handler).
        Requests to paths not listed in ``rules`` pass through without a check.

    Example
    -------
    from fast_langchain_server.authorization import (
        AuthorizationMiddleware, require_scopes, allow_any_authenticated,
    )

    server.add_middleware(AuthorizationMiddleware({
        "/v1/chat/completions": require_scopes("chat"),
        "/memory/sessions":     require_scopes("admin"),
    }))
    """

    def __init__(self, rules: dict[str, AuthCheck]) -> None:
        self._rules = rules

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        check = self._rules.get(ctx.endpoint)

        if check is not None:
            auth_ctx = AuthContext(
                token=ctx.auth_token,
                endpoint=ctx.endpoint,
                method=ctx.get_meta("method", "POST"),
                session_id=ctx.session_id,
            )
            allowed = await run_auth_check(check, auth_ctx)
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Insufficient permissions for this endpoint",
                )

        return await call_next(ctx)
