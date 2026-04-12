"""
Core HTTP server for LangChain / LangGraph agents.

Architecture
------------
``Server`` wraps any CompiledStateGraph (created with
``langchain.agents.create_agent`` or a custom LangGraph graph) and exposes it
as a production-grade HTTP service.

Usage
-----
    from langchain.agents import create_agent
    from fast_langchain_server import Server

    agent = create_agent(model=model, tools=[add])
    server = Server(agent, tools=[add], agent_name="my-agent")

    # Run directly:
    server.run()

    # Or expose the FastAPI app for an external process manager:
    app = server.app          # uvicorn agent:app

Endpoints
---------
GET  /health                     – liveness probe
GET  /ready                      – readiness probe
POST /v1/chat/completions        – OpenAI-compatible chat (streaming + non-streaming)
GET  /.well-known/agent.json     – A2A discovery card
GET  /memory/sessions            – list active sessions
DELETE /memory/sessions/{id}     – delete a session
POST /                           – A2A JSON-RPC 2.0  (SendMessage/GetTask/CancelTask)
                                   only mounted when a2a=True (default)

Streaming
---------
LangGraph astream() is called with ``stream_mode=["messages","updates"]``.
  "messages" events  → forward LLM tokens to the SSE client in real time.
  "updates"  events  → capture full new messages for session memory.

OpenTelemetry
-------------
A span is created for every /v1/chat/completions request.  Parent context is
extracted from incoming headers (W3C TraceContext) so distributed traces are
continued end-to-end.  Span attributes include session_id, stream mode, and
tool-call count.

A2A
---
When a2a=True (default) the JSON-RPC 2.0 endpoint is mounted at POST /.
The task manager calls the server's own _process_fn, giving A2A clients the
same agent behaviour as the chat API.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage
from opentelemetry import trace as trace_api
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from fast_langchain_server.context import AgentContext
from fast_langchain_server.lifespan import DEFAULT_LIFESPAN, Lifespan
from fast_langchain_server.middleware import AgentMiddleware, build_middleware_chain
from fast_langchain_server.a2a import (
    LocalTaskManager,
    NullTaskManager,
    TaskManager,
    setup_a2a_routes,
)
from fast_langchain_server.memory import Memory, NullMemory, create_memory
from fast_langchain_server.serverutils import (
    AgentServerSettings,
    build_langchain_model,
    configure_logging,
    extract_text_content,
    inspect_agent,
)
from fast_langchain_server.telemetry import (
    SERVICE_NAME,
    extract_context,
    init_otel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    messages: list[ChatMessage]
    model: str = "agent"
    stream: bool = False
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server:
    """Production HTTP server for a LangChain / LangGraph agent.

    This is the single entry point for creating and running an agent server.
    It wires together the FastAPI application, session memory, middleware chain,
    A2A task manager, and OpenTelemetry tracing.

    Parameters
    ----------
    agent:
        A ``CompiledStateGraph`` from ``langchain.agents.create_agent(...)``
        or any LangGraph graph that accepts ``{"messages": [...]}`` input and
        returns ``{"messages": [...]}`` in its state.
    tools:
        LangChain tools exposed on the discovery card.  Not used for execution.
    agent_name:
        Display name for the agent.  Falls back to ``AGENT_NAME`` env var or
        an auto-generated identifier.
    agent_description:
        Short description for the discovery card.  Falls back to
        ``AGENT_DESCRIPTION`` env var.
    a2a:
        Enable Agent-to-Agent JSON-RPC 2.0 support (default ``True``).
        Set to ``False`` to skip mounting the A2A endpoint.
    lifespan:
        Custom composable :class:`Lifespan` to use instead of (or composed
        with) ``DEFAULT_LIFESPAN``::

            from fast_langchain_server import Server, DEFAULT_LIFESPAN, lifespan

            @lifespan
            async def my_db(server):
                server.lifespan_context["db"] = await connect()
                yield {}
                await server.lifespan_context["db"].close()

            server = Server(agent, lifespan=DEFAULT_LIFESPAN | my_db)

    **settings_kwargs:
        Any field accepted by :class:`AgentServerSettings` can be passed here
        (e.g. ``memory_type="redis"``, ``memory_redis_url="redis://..."``,
        ``agent_port=9000``).

    Examples
    --------
    Minimal — everything from env vars:

        server = Server(agent, tools=[add])
        server.run()

    With explicit name and port:

        server = Server(agent, tools=[add], agent_name="math-agent", agent_port=9000)
        server.run(reload=True)

    Expose the FastAPI app for an external process manager:

        server = Server(agent, tools=[add])
        app = server.app            # uvicorn agent:app

    With middleware:

        from fast_langchain_server import AuthMiddleware, APIKeyProvider
        from starlette.middleware.cors import CORSMiddleware

        server = Server(agent, tools=[add])
        server.add_middleware(AuthMiddleware(APIKeyProvider(["secret"])))
        server.add_middleware(CORSMiddleware, allow_origins=["*"])
        server.run()
    """

    def __init__(
        self,
        agent: Any,
        tools: Optional[list] = None,
        agent_name: Optional[str] = None,
        agent_description: Optional[str] = None,
        a2a: bool = True,
        lifespan: Optional[Lifespan] = None,
        memory: Optional[Memory] = None,
        **settings_kwargs: Any,
    ) -> None:
        # ── Settings ──────────────────────────────────────────────────────────
        if not settings_kwargs:
            settings_kwargs = _extract_agent_settings(agent)

        if agent_name:
            settings_kwargs["agent_name"] = agent_name
        if agent_description:
            settings_kwargs["agent_description"] = agent_description

        settings_kwargs["task_manager_type"] = "local" if a2a else "none"

        settings = AgentServerSettings(**settings_kwargs)  # type: ignore[call-arg]

        # ── Logging & OTel ────────────────────────────────────────────────────
        configure_logging(settings.agent_log_level, otel_correlation=settings.otel_active)
        if settings.otel_active:
            init_otel(settings.agent_name)

        # ── Internal state ────────────────────────────────────────────────────
        self._agent = agent
        self._settings = settings

        # Auto-detect tools from the agent graph when not explicitly provided.
        # tools=None  → inspect the agent (default behaviour)
        # tools=[]    → caller explicitly wants no tools in the discovery card
        if tools is None:
            agent_meta = inspect_agent(agent)
            self._tools: list = agent_meta["tools"]
            # Use introspected description only when the caller didn't set one
            if not agent_description and not settings_kwargs.get("agent_description"):
                introspected = agent_meta.get("description", "")
                if introspected:
                    self._settings = settings.model_copy(
                        update={"agent_description": introspected}
                    )
        else:
            self._tools = tools
        self._middlewares: list[AgentMiddleware] = []
        self.lifespan_context: dict = {}
        self._lifespan_obj: Lifespan = lifespan or DEFAULT_LIFESPAN

        # ── Memory ────────────────────────────────────────────────────────────
        self._memory: Memory = memory or create_memory(
            memory_type=settings.memory_type if settings.memory_enabled else "null",
            redis_url=settings.memory_redis_url,
            max_sessions=settings.memory_max_sessions,
            max_messages_per_session=settings.memory_max_messages_per_session,
        )

        # ── FastAPI app ───────────────────────────────────────────────────────
        self._app = FastAPI(
            title=settings.agent_name,
            description=settings.agent_description,
            lifespan=self._lifespan,
        )
        self._setup_routes()

        # ── A2A task manager ──────────────────────────────────────────────────
        self._task_manager: TaskManager = NullTaskManager()
        if a2a:
            tm = LocalTaskManager(
                process_fn=self._process_fn,
                max_tasks=settings.task_manager_max_tasks,
            )
            self._task_manager = tm
            setup_a2a_routes(self._app, tm)
            logger.info("A2A LocalTaskManager wired (max_tasks=%d)", settings.task_manager_max_tasks)

        logger.info(
            "Server '%s' ready  a2a=%s  memory=%s",
            settings.agent_name,
            a2a,
            settings.memory_type if settings.memory_enabled else "disabled",
        )

    # ── Lifespan ──────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """FastAPI lifespan hook — delegates to the composable Lifespan object."""
        async with self._lifespan_obj._as_cm(self) as ctx:
            self.lifespan_context.update(ctx)
            yield
            self.lifespan_context.clear()

    # ── Middleware registration ───────────────────────────────────────────────

    def add_middleware(self, middleware: Any, **kwargs: Any) -> "Server":
        """Add a middleware to the server.

        Routing is determined by the middleware type:

        - :class:`AgentMiddleware` instances are added to the internal
          request-processing chain (auth, rate-limit, timing, etc.).
        - Any other value is treated as a Starlette/ASGI middleware **class**
          and forwarded to ``app.add_middleware()`` (CORS, GZip, TrustedHost…)::

              server.add_middleware(CORSMiddleware, allow_origins=["*"])

        Returns ``self`` to allow chaining::

            (
                server
                .add_middleware(TimingMiddleware())
                .add_middleware(AuthMiddleware(provider))
                .add_middleware(CORSMiddleware, allow_origins=["*"])
            )
        """
        if isinstance(middleware, AgentMiddleware):
            self._middlewares.append(middleware)
        else:
            self._app.add_middleware(middleware, **kwargs)
        return self

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def app(self) -> FastAPI:
        """The underlying FastAPI application.

        Use this to expose the server to an external process manager::

            server = Server(agent, tools=[add])
            app = server.app   # uvicorn agent:app
        """
        return self._app

    def run(
        self,
        host: str = "0.0.0.0",  # nosec B104 - intentional for containerized deployment
        port: Optional[int] = None,
        reload: bool = False,
        workers: Optional[int] = None,
        log_level: Optional[str] = None,
        access_log: Optional[bool] = None,
        **uvicorn_kwargs: Any,
    ) -> None:
        """Start the server with uvicorn.

        Parameters
        ----------
        host:
            Bind host (default ``"0.0.0.0"``).
        port:
            Bind port.  Falls back to ``AGENT_PORT`` env var / settings (default 8000).
        reload:
            Enable auto-reload on file changes (development only).
        workers:
            Number of worker processes.  Cannot be combined with ``reload``.
        log_level:
            Uvicorn log level (``"debug"``, ``"info"``, ``"warning"``, …).
            Falls back to ``AGENT_LOG_LEVEL`` / settings.
        access_log:
            Enable HTTP access logging.  Falls back to ``AGENT_ACCESS_LOG`` / settings.
        **uvicorn_kwargs:
            Any additional keyword argument accepted by :func:`uvicorn.run`
            (e.g. ``ssl_keyfile``, ``ssl_certfile``, ``timeout_keep_alive``).
        """
        import uvicorn

        uvicorn.run(
            self._app,
            host=host,
            port=port if port is not None else self._settings.agent_port,
            reload=reload,
            workers=workers,
            log_level=(log_level or self._settings.agent_log_level).lower(),
            access_log=access_log if access_log is not None else self._settings.agent_access_log,
            **uvicorn_kwargs,
        )

    # ── Route setup ───────────────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        app = self._app

        # ── Health / Readiness ────────────────────────────────────────────────
        @app.get("/health")
        @app.get("/ready")
        async def health():
            return {
                "status": "healthy",
                "name": self._settings.agent_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # ── Agent discovery card ──────────────────────────────────────────────
        @app.get("/.well-known/agent.json")
        async def agent_card():
            return self._build_agent_card()

        # ── Chat completions (OpenAI-compatible) ──────────────────────────────
        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            try:
                body = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid JSON body")

            req = ChatCompletionRequest(**body)

            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == "user"), None
            )
            if not last_user:
                raise HTTPException(
                    status_code=400,
                    detail="Request must contain at least one user message",
                )

            session_id = req.session_id or request.headers.get("X-Session-ID")
            session_id = await self._memory.get_or_create_session(session_id)

            otel_context = extract_context(dict(request.headers))

            ctx = AgentContext.from_request(
                session_id=session_id,
                user_input=last_user,
                model=req.model,
                request=request,
                otel_context=otel_context,
            )
            ctx.set_meta("endpoint", "/v1/chat/completions")
            ctx.set_meta("method", "POST")

            if req.stream:
                async def _stream_handler(c: AgentContext) -> StreamingResponse:
                    return StreamingResponse(
                        self._stream_response(c),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )

                chain = build_middleware_chain(
                    self._middlewares, _stream_handler, hook="on_request"
                )
                return await chain(ctx)

            async def _run_handler(c: AgentContext) -> JSONResponse:
                response_text, _tool_calls = await self._run_agent(c)
                return JSONResponse(
                    self._build_completion(response_text, c.session_id, c.model)
                )

            chain = build_middleware_chain(
                self._middlewares, _run_handler, hook="on_request"
            )
            return await chain(ctx)

        # ── Memory management ─────────────────────────────────────────────────
        @app.get("/memory/sessions")
        async def list_sessions():
            sessions = await self._memory.list_sessions()
            return {"sessions": sessions, "count": len(sessions)}

        @app.delete("/memory/sessions/{session_id}")
        async def delete_session(session_id: str):
            deleted = await self._memory.delete_session(session_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Session not found")
            return {"deleted": session_id}

    # ── Non-streaming execution ───────────────────────────────────────────────

    async def _run_agent(self, ctx: AgentContext) -> Tuple[str, int]:
        """Run the agent and return (response_text, tool_call_count)."""
        tracer = trace_api.get_tracer(SERVICE_NAME)

        with tracer.start_as_current_span(
            "fls.server.run",
            context=ctx.otel_context,
            attributes={
                "agent.name": self._settings.agent_name,
                "session.id": ctx.session_id,
                "stream": False,
            },
        ) as span:
            history = await self._memory.get_messages(
                ctx.session_id, self._settings.memory_context_limit
            )
            input_messages = history + [HumanMessage(content=ctx.user_input)]

            try:
                result = await self._agent.ainvoke({"messages": input_messages})
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("error", True)
                logger.error("Agent invocation failed: %s", exc, exc_info=True)
                raise HTTPException(status_code=500, detail=f"Agent error: {exc}") from exc

            all_messages = result.get("messages", [])
            if not all_messages:
                raise HTTPException(status_code=500, detail="Agent returned no messages")

            await self._memory.save_messages(ctx.session_id, all_messages)

            final = all_messages[-1]
            response_text = extract_text_content(final.content)

            new_msgs = all_messages[len(input_messages):]
            tool_call_count = sum(
                len(getattr(m, "tool_calls", None) or []) for m in new_msgs
            )
            span.set_attribute("tool_calls", tool_call_count)
            return response_text, tool_call_count

    # ── Streaming execution ───────────────────────────────────────────────────

    async def _stream_response(self, ctx: AgentContext) -> AsyncGenerator[str, None]:
        """
        Async generator that yields SSE-formatted strings.

        SSE events
        ~~~~~~~~~~
        Progress (tool call detected):
            data: {"type": "progress", "action": "tool_call", "target": "<name>"}

        Content chunk (LLM token):
            data: {"id": "…", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": "…"}, "finish_reason": null}]}

        End of stream:
            data: [DONE]
        """
        tracer = trace_api.get_tracer(SERVICE_NAME)

        pending_events: list[dict] = []

        async def _emit(event: dict) -> None:
            pending_events.append(event)

        ctx._emit = _emit

        span = tracer.start_span(
            "fls.server.stream",
            context=ctx.otel_context,
            attributes={
                "agent.name": self._settings.agent_name,
                "session.id": ctx.session_id,
                "stream": True,
            },
        )

        history = await self._memory.get_messages(
            ctx.session_id, self._settings.memory_context_limit
        )
        input_messages = history + [HumanMessage(content=ctx.user_input)]
        response_id = ctx.request_id
        accumulated_new: list = []
        announced_tools: set[str] = set()
        total_tool_calls = 0

        try:
            async for mode, data in self._agent.astream(
                {"messages": input_messages},
                stream_mode=["messages", "updates"],
            ):
                for event in pending_events:
                    yield f"data: {json.dumps(event)}\n\n"
                pending_events.clear()

                if mode == "messages":
                    chunk, _metadata = data

                    if isinstance(chunk, AIMessageChunk):
                        for tc in chunk.tool_call_chunks or []:
                            tool_name = tc.get("name", "")
                            if tool_name and tool_name not in announced_tools:
                                announced_tools.add(tool_name)
                                total_tool_calls += 1
                                await ctx.emit_progress("tool_call", tool_name)
                                for event in pending_events:
                                    yield f"data: {json.dumps(event)}\n\n"
                                pending_events.clear()

                        if chunk.content:
                            sse = {
                                "id": response_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": ctx.model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "content": extract_text_content(chunk.content)
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(sse)}\n\n"

                elif mode == "updates":
                    for _node, node_output in data.items():
                        for msg in node_output.get("messages", []):
                            accumulated_new.append(msg)

            await self._memory.save_messages(
                ctx.session_id, input_messages + accumulated_new
            )

            span.set_attribute("tool_calls", total_tool_calls)
            yield "data: [DONE]\n\n"

        except Exception as exc:
            span.record_exception(exc)
            span.set_attribute("error", True)
            logger.error("Streaming error: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            span.end()

    # ── process_fn for A2A task manager ──────────────────────────────────────

    async def _process_fn(self, text: str, session_id: str) -> Tuple[str, int]:
        """Bridge between the A2A task manager and the agent."""
        ctx = AgentContext.from_request(
            session_id=session_id,
            user_input=text,
        )
        ctx.set_meta("endpoint", "a2a")
        return await self._run_agent(ctx)

    # ── Agent discovery card ──────────────────────────────────────────────────

    def _build_agent_card(self) -> dict:
        skills = [
            {
                "id": getattr(t, "name", str(t)),
                "name": getattr(t, "name", str(t)),
                "description": getattr(t, "description", ""),
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
            for t in self._tools
        ]

        a2a_active = not isinstance(self._task_manager, NullTaskManager)

        return {
            "name": self._settings.agent_name,
            "description": self._settings.agent_description,
            "url": f"http://localhost:{self._settings.agent_port}",
            "version": "0.1.0",
            "protocolVersion": "0.3.0",
            "skills": skills,
            "capabilities": {
                "streaming": True,
                "memory": self._settings.memory_enabled,
                "memoryBackend": self._settings.memory_type,
                "a2a": a2a_active,
                "pushNotifications": False,
                "stateTransitionHistory": a2a_active,
            },
            "supportedProtocols": ["jsonrpc"] if a2a_active else [],
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
        }

    # ── OpenAI response builder ───────────────────────────────────────────────

    @staticmethod
    def _build_completion(content: str, session_id: str, model: str) -> dict:
        return {
            "id": session_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


# ---------------------------------------------------------------------------
# Private helpers — model config extraction
# ---------------------------------------------------------------------------


def _extract_agent_settings(agent: Any) -> dict[str, Any]:
    """Extract model configuration from a LangChain agent.

    Attempts to find the ChatOpenAI model instance within the agent graph
    and extract base_url and model name. Also checks environment variables.
    """
    import os

    settings: dict[str, Any] = {}

    try:
        if hasattr(agent, "nodes"):
            for _node_name, node_data in agent.nodes.items():
                if hasattr(node_data, "runnable"):
                    if _extract_model_from_runnable(node_data.runnable, settings):
                        break

        if not settings and hasattr(agent, "runnable"):
            _extract_model_from_runnable(agent.runnable, settings)
    except Exception as exc:
        logger.warning("Could not auto-extract model settings: %s", exc)

    if "model_name" not in settings:
        settings["model_name"] = os.getenv("MODEL_NAME") or os.getenv("OPENAI_MODEL")
    if "model_api_url" not in settings:
        settings["model_api_url"] = os.getenv("MODEL_API_URL") or os.getenv("OPENAI_BASE_URL")

    if "agent_name" not in settings:
        settings["agent_name"] = os.getenv("AGENT_NAME") or f"agent-{uuid.uuid4().hex[:8]}"

    return settings


def _extract_model_from_runnable(runnable: Any, settings: dict[str, Any]) -> bool:
    """Recursively search a runnable for a ChatOpenAI model and extract settings."""
    if runnable is None:
        return False

    if type(runnable).__name__ == "ChatOpenAI":
        if hasattr(runnable, "model_name"):
            settings["model_name"] = runnable.model_name
        if hasattr(runnable, "base_url"):
            settings["model_api_url"] = runnable.base_url
        return bool(settings.get("model_name") and settings.get("model_api_url"))

    for attr in ("first", "last"):
        if hasattr(runnable, attr):
            if _extract_model_from_runnable(getattr(runnable, attr), settings):
                return True

    if hasattr(runnable, "middle"):
        items = runnable.middle if isinstance(runnable.middle, list) else [runnable.middle]
        for item in items:
            if _extract_model_from_runnable(item, settings):
                return True

    if hasattr(runnable, "steps"):
        for step in runnable.steps:
            if _extract_model_from_runnable(step, settings):
                return True

    return False
