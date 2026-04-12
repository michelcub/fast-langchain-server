"""
Request-scoped context object for fast-langchain-server.

Inspired by FastMCP's Context pattern: a single object that bundles all
per-request metadata and capabilities instead of passing them as loose
positional arguments through the call stack.

Usage
-----
The AgentContext is built in the HTTP handler and passed down to
_run_agent() / _stream_response() and through the middleware chain.
Middlewares can read and enrich it via get_meta() / set_meta().

Progress emission
-----------------
In streaming mode the handler wires ``_emit`` to the SSE generator's send
function, so deep call sites can push progress events without knowing
anything about the transport:

    await ctx.emit_progress("tool_call", "search_web")
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from starlette.requests import Request


@dataclass
class AgentContext:
    """Carries all per-request state through the middleware and agent layers.

    Parameters
    ----------
    session_id:
        Resolved session identifier (already created if new).
    request_id:
        UUID assigned to this specific HTTP request.
    user_input:
        The last user message extracted from the request body.
    model:
        Model name from the request (defaults to ``"agent"``).
    request:
        The raw Starlette/FastAPI Request object. Use ``ctx.headers`` to
        access lower-cased headers, or read any other request attribute
        (body, cookies, client, etc.) directly.  ``None`` for non-HTTP
        entry points such as A2A.
    otel_context:
        W3C TraceContext extracted from incoming headers; passed to the OTel
        tracer so that distributed traces are continued end-to-end.
    _emit:
        Optional async callable used to push SSE progress events.
        Only set during streaming; no-op (None) for non-streaming requests.
    _metadata:
        Arbitrary key/value bag for middleware communication.
        Middlewares upstream can set values; middlewares or handlers downstream
        can read them.  Never use this for large blobs.
    """

    session_id: str
    request_id: str
    user_input: str
    model: str = "agent"
    request: Optional["Request"] = field(default=None, repr=False)
    otel_context: Any = field(default=None, repr=False)
    _emit: Optional[Callable[[dict], Awaitable[None]]] = field(
        default=None, repr=False
    )
    _metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    @property
    def headers(self) -> dict[str, str]:
        """Lower-cased HTTP headers from the request, or an empty dict."""
        if self.request is None:
            return {}
        return {k.lower(): v for k, v in self.request.headers.items()}

    # ------------------------------------------------------------------
    # Progress emission
    # ------------------------------------------------------------------

    async def emit_progress(self, action: str, target: str) -> None:
        """Push a progress event to the SSE client (no-op outside streaming).

        Parameters
        ----------
        action:
            Short verb describing what is happening, e.g. ``"tool_call"``.
        target:
            The subject of the action, e.g. the tool name.
        """
        if self._emit is not None:
            await self._emit({"type": "progress", "action": action, "target": target})

    # ------------------------------------------------------------------
    # Metadata bag — used by middleware to pass data down the chain
    # ------------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        """Store an arbitrary value for downstream consumption."""
        self._metadata[key] = value

    def get_meta(self, key: str, default: Any = None) -> Any:
        """Retrieve a value previously set by ``set_meta``."""
        return self._metadata.get(key, default)

    # ------------------------------------------------------------------
    # Shortcuts for common metadata keys set by AuthMiddleware
    # ------------------------------------------------------------------

    @property
    def auth_token(self) -> Optional[Any]:
        """The verified AuthToken set by AuthMiddleware, or None."""
        return self._metadata.get("auth_token")

    @property
    def endpoint(self) -> str:
        """The HTTP endpoint path set by the request handler."""
        return self._metadata.get("endpoint", "")

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_request(
        cls,
        *,
        session_id: str,
        user_input: str,
        model: str = "agent",
        request: Optional["Request"] = None,
        otel_context: Any = None,
    ) -> "AgentContext":
        """Convenience constructor that auto-generates a request_id."""
        return cls(
            session_id=session_id,
            request_id=str(uuid.uuid4()),
            user_input=user_input,
            model=model,
            request=request,
            otel_context=otel_context,
        )
