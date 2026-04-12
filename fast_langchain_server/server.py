"""
Core HTTP server for LangChain / LangGraph agents.

Architecture
------------
AgentServer wraps any CompiledStateGraph (created with
``langchain.agents.create_agent`` or a custom LangGraph graph) and exposes it
as a production-grade HTTP service.

Endpoints
---------
GET  /health                     – liveness probe
GET  /ready                      – readiness probe
POST /v1/chat/completions        – OpenAI-compatible chat (streaming + non-streaming)
GET  /.well-known/agent.json     – A2A discovery card
GET  /memory/sessions            – list active sessions
DELETE /memory/sessions/{id}     – delete a session
POST /                           – A2A JSON-RPC 2.0  (SendMessage/GetTask/CancelTask)
                                   only mounted when task_manager_type != "none"

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
When task_manager_type="local" the JSON-RPC 2.0 endpoint is mounted at
POST /.  The task manager calls the server's own _process_fn, giving A2A
clients the same agent behaviour as the chat API.
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
# AgentServer
# ---------------------------------------------------------------------------


class AgentServer:
    """Wraps a compiled LangGraph/LangChain agent with a production HTTP server.

    Parameters
    ----------
    agent:
        A ``CompiledStateGraph`` from ``langchain.agents.create_agent(...)``
        or any LangGraph graph that accepts ``{"messages": [...]}`` input and
        returns ``{"messages": [...]}`` in its state.
    settings:
        Server configuration (all fields env-var driven).
    memory:
        Session message-history backend.
    tools:
        LangChain tools exposed on the discovery card.  Not used for execution.
    task_manager:
        A2A task manager.  ``NullTaskManager`` disables A2A.
    """

    def __init__(
        self,
        agent: Any,
        settings: AgentServerSettings,
        memory: Optional[Memory] = None,
        tools: Optional[list] = None,
        task_manager: Optional[TaskManager] = None,
        lifespan: Optional[Lifespan] = None,
    ) -> None:
        self._agent = agent
        self._settings = settings
        self._memory: Memory = memory or NullMemory()
        self._tools: list = tools or []
        self._task_manager: TaskManager = task_manager or NullTaskManager()
        self._middlewares: list[AgentMiddleware] = []
        self.lifespan_context: dict = {}
        self._lifespan_obj: Lifespan = lifespan or DEFAULT_LIFESPAN

        self._app = FastAPI(
            title=settings.agent_name,
            description=settings.agent_description,
            lifespan=self._lifespan,
        )
        self._setup_routes()

    # ── Lifespan ──────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """FastAPI lifespan hook — delegates to the composable Lifespan object."""
        async with self._lifespan_obj._as_cm(self) as ctx:
            self.lifespan_context.update(ctx)
            yield
            self.lifespan_context.clear()

    # ── Middleware registration ───────────────────────────────────────────────

    def add_middleware(self, middleware: AgentMiddleware) -> "AgentServer":
        """Append a middleware to the chain.

        Middlewares execute in the order they are added (first added = outermost
        wrapper).  Returns ``self`` to allow chaining::

            server.add_middleware(TimingMiddleware()).add_middleware(RateLimitMiddleware())
        """
        self._middlewares.append(middleware)
        return self

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

            headers = {k.lower(): v for k, v in request.headers.items()}
            otel_context = extract_context(dict(request.headers))

            ctx = AgentContext.from_request(
                session_id=session_id,
                user_input=last_user,
                model=req.model,
                headers=headers,
                otel_context=otel_context,
            )
            ctx.set_meta("endpoint", "/v1/chat/completions")
            ctx.set_meta("method", "POST")

            if req.stream:
                # For streaming we only run the middleware chain for the
                # on_request hook (auth, rate-limit, etc.) before handing off
                # the generator.  The generator itself is not wrapped because
                # it yields bytes incrementally.
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

        # ── A2A JSON-RPC (only when task manager is active) ───────────────────
        if not isinstance(self._task_manager, NullTaskManager):
            setup_a2a_routes(app, self._task_manager)
            logger.info("A2A JSON-RPC endpoint mounted at POST /")

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

            # Count tool calls in the new messages (everything after input)
            new_msgs = all_messages[len(input_messages):]
            tool_call_count = sum(
                len(getattr(m, "tool_calls", None) or []) for m in new_msgs
            )
            span.set_attribute("tool_calls", tool_call_count)
            return response_text, tool_call_count

    # ── Streaming execution ───────────────────────────────────────────────────

    async def _stream_response(
        self, ctx: AgentContext
    ) -> AsyncGenerator[str, None]:
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

        # Wire ctx._emit so that deep call sites can push progress events via
        # ctx.emit_progress() without knowing about the SSE transport.
        pending_events: list[dict] = []

        async def _emit(event: dict) -> None:
            pending_events.append(event)

        ctx._emit = _emit

        # The span must stay open across the whole generator, so we use a
        # context-manager approach without 'with' (enter/exit manually).
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
                # Flush any progress events queued by ctx.emit_progress()
                for event in pending_events:
                    yield f"data: {json.dumps(event)}\n\n"
                pending_events.clear()

                if mode == "messages":
                    chunk, _metadata = data

                    if isinstance(chunk, AIMessageChunk):
                        # Detect and announce new tool calls
                        for tc in chunk.tool_call_chunks or []:
                            tool_name = tc.get("name", "")
                            if tool_name and tool_name not in announced_tools:
                                announced_tools.add(tool_name)
                                total_tool_calls += 1
                                await ctx.emit_progress("tool_call", tool_name)
                                # Flush immediately so the client sees it now
                                for event in pending_events:
                                    yield f"data: {json.dumps(event)}\n\n"
                                pending_events.clear()

                        # Stream LLM tokens
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
        """Bridge between the A2A task manager and the agent.

        The task manager calls this function with each iteration message and
        expects ``(response_text, tool_call_count)``.
        """
        ctx = AgentContext.from_request(
            session_id=session_id,
            user_input=text,
            headers={},
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

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def app(self) -> FastAPI:
        return self._app

    def run(self, host: str = "0.0.0.0") -> None:  # nosec B104 - intentional for containerized deployment
        import uvicorn
        uvicorn.run(
            self._app,
            host=host,
            port=self._settings.agent_port,
            log_level=self._settings.agent_log_level.lower(),
            access_log=self._settings.agent_access_log,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_agent_server(
    settings: Optional[AgentServerSettings] = None,
    tools: Optional[list] = None,
    custom_agent: Any = None,
    system_prompt: Optional[str] = None,
    lifespan: Optional[Lifespan] = None,
) -> AgentServer:
    """Build a fully wired AgentServer from environment variables.

    Parameters
    ----------
    settings:
        Pre-built settings.  Loaded from env vars / .env when ``None``.
    tools:
        LangChain tools to attach.  Ignored when ``custom_agent`` is provided.
    custom_agent:
        An already-compiled LangGraph/LangChain agent (``CompiledStateGraph``).
        When provided, model and agent creation are skipped.
    system_prompt:
        Override the system prompt from settings.  Ignored when
        ``custom_agent`` is provided.
    lifespan:
        Custom composable ``Lifespan`` (or ``ComposedLifespan``) to use instead
        of the built-in ``DEFAULT_LIFESPAN``.  Compose additional lifespans with
        ``|``::

            from fast_langchain_server.lifespan import lifespan, DEFAULT_LIFESPAN

            @lifespan
            async def my_db(server):
                server.lifespan_context["db"] = await connect()
                yield {}
                await server.lifespan_context["db"].close()

            server = create_agent_server(lifespan=DEFAULT_LIFESPAN | my_db)

    Examples
    --------
    # Everything from env vars
    server = create_agent_server(tools=[my_tool])
    server.run()

    # With a pre-built agent
    from langchain.agents import create_agent
    agent = create_agent("openai:gpt-4o", tools=[my_tool])
    server = create_agent_server(custom_agent=agent, tools=[my_tool])
    server.run()
    """
    if settings is None:
        settings = AgentServerSettings()  # type: ignore[call-arg]

    configure_logging(settings.agent_log_level, otel_correlation=settings.otel_active)

    # ── OTel (early init so factory logging is traced) ────────────────────────
    if settings.otel_active:
        init_otel(settings.agent_name)

    # ── Memory backend ────────────────────────────────────────────────────────
    memory = create_memory(
        memory_type=settings.memory_type if settings.memory_enabled else "null",
        redis_url=settings.memory_redis_url,
        max_sessions=settings.memory_max_sessions,
        max_messages_per_session=settings.memory_max_messages_per_session,
    )

    # ── Agent ─────────────────────────────────────────────────────────────────
    if custom_agent is not None:
        agent = custom_agent
        logger.info("Using provided agent: %s", type(agent).__name__)
    else:
        from langchain.agents import create_agent as lc_create_agent

        model = build_langchain_model(settings)
        prompt = system_prompt or settings.agent_instructions

        logger.info(
            "Creating agent '%s' with %d tool(s)", settings.agent_name, len(tools or [])
        )
        agent = lc_create_agent(
            model=model,
            tools=tools or [],
            system_prompt=prompt,
        )

    # ── A2A task manager ──────────────────────────────────────────────────────
    # We need a forward reference to the server's _process_fn, so we build a
    # placeholder and wire it after the server is created.
    server = AgentServer(
        agent=agent,
        settings=settings,
        memory=memory,
        tools=tools or [],
        task_manager=NullTaskManager(),  # replaced below if needed
        lifespan=lifespan,
    )

    if settings.task_manager_type == "local":
        tm = LocalTaskManager(
            process_fn=server._process_fn,
            max_tasks=settings.task_manager_max_tasks,
        )
        server._task_manager = tm
        setup_a2a_routes(server.app, tm)
        logger.info("A2A LocalTaskManager wired (max_tasks=%d)", settings.task_manager_max_tasks)

    return server


# ---------------------------------------------------------------------------
# ASGI factory entry point
# ---------------------------------------------------------------------------


def get_app() -> FastAPI:
    """ASGI factory used by: uvicorn fast_langchain_server.server:get_app --factory"""
    return create_agent_server().app


def serve(
    agent: Any,
    tools: Optional[list] = None,
    agent_name: Optional[str] = None,
    agent_description: Optional[str] = None,
    **kwargs: Any
) -> FastAPI:
    """One-liner to wrap an existing agent as a FastAPI ASGI app.

    Parameters
    ----------
    agent : CompiledStateGraph
        A LangChain/LangGraph agent (from create_agent()).
    tools : list, optional
        List of tools to expose in the agent discovery card.
    agent_name : str, optional
        Name for the agent. Falls back to AGENT_NAME env var or auto-generated.
    agent_description : str, optional
        Description for the agent. Falls back to AGENT_DESCRIPTION env var.
    **kwargs
        Additional settings passed to AgentServerSettings.

    Example
    -------
    from langchain.agents import create_agent
    from fast_langchain_server import serve

    agent = create_agent(model=model, tools=[my_tool])
    app = serve(
        agent,
        tools=[my_tool],
        agent_name="my-agent",
        agent_description="My custom agent"
    )
    """
    # Extract model info from agent if not provided in kwargs
    if not kwargs:
        kwargs = _extract_agent_settings(agent)

    # Allow overriding agent name and description via parameters
    if agent_name:
        kwargs["agent_name"] = agent_name
    if agent_description:
        kwargs["agent_description"] = agent_description

    settings = AgentServerSettings(**kwargs)  # type: ignore[call-arg]
    server = create_agent_server(settings=settings, custom_agent=agent, tools=tools or [])
    return server.app


def _extract_agent_settings(agent: Any) -> dict[str, Any]:
    """Extract model configuration from a LangChain agent.

    Attempts to find the ChatOpenAI model instance within the agent graph
    and extract base_url and model name. Also checks environment variables.
    """
    import os

    settings = {}

    try:
        # Try to find the model in the agent's graph nodes
        if hasattr(agent, "nodes"):
            for node_name, node_data in agent.nodes.items():
                if hasattr(node_data, "runnable"):
                    runnable = node_data.runnable
                    # Look for ChatOpenAI in the runnable
                    if _extract_model_from_runnable(runnable, settings):
                        break

        # Fallback: check if agent has a direct reference to the model
        if not settings and hasattr(agent, "runnable"):
            _extract_model_from_runnable(agent.runnable, settings)
    except Exception as e:
        logger.warning(f"Could not auto-extract model settings: {e}")

    # Try to get model info from environment variables if not found in agent
    if "model_name" not in settings:
        settings["model_name"] = os.getenv("MODEL_NAME") or os.getenv("OPENAI_MODEL")
    if "model_api_url" not in settings:
        settings["model_api_url"] = os.getenv("MODEL_API_URL") or os.getenv("OPENAI_BASE_URL")

    # Generate a default agent name if not found
    if "agent_name" not in settings:
        agent_name = os.getenv("AGENT_NAME")
        if not agent_name:
            agent_name = f"agent-{uuid.uuid4().hex[:8]}"
        settings["agent_name"] = agent_name

    return settings


def _extract_model_from_runnable(runnable: Any, settings: dict[str, Any]) -> bool:
    """Recursively search a runnable for ChatOpenAI model and extract settings."""
    if runnable is None:
        return False

    # Check if this is a ChatOpenAI instance
    if type(runnable).__name__ == "ChatOpenAI":
        if hasattr(runnable, "model_name"):
            settings["model_name"] = runnable.model_name
        if hasattr(runnable, "base_url"):
            settings["model_api_url"] = runnable.base_url
        return bool(settings.get("model_name") and settings.get("model_api_url"))

    # Check nested runnables
    if hasattr(runnable, "first"):
        if _extract_model_from_runnable(runnable.first, settings):
            return True
    if hasattr(runnable, "middle"):
        if isinstance(runnable.middle, list):
            for item in runnable.middle:
                if _extract_model_from_runnable(item, settings):
                    return True
        else:
            if _extract_model_from_runnable(runnable.middle, settings):
                return True
    if hasattr(runnable, "last"):
        if _extract_model_from_runnable(runnable.last, settings):
            return True

    # Check steps in a sequence
    if hasattr(runnable, "steps"):
        for step in runnable.steps:
            if _extract_model_from_runnable(step, settings):
                return True

    return False
