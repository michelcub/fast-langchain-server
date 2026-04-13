"""A2A (Agent-to-Agent) client middleware.

Adds tools to a LangGraph agent so it can call remote A2A-compatible agents
over HTTP using the JSON-RPC 2.0 A2A protocol.

Usage::

    from fast_langchain_server.a2a_client import A2AClientMiddleware, RemoteAgentConfig
    from langchain.agents import create_agent

    # Explicit config
    middleware = A2AClientMiddleware(agents=[
        RemoteAgentConfig(
            url="http://localhost:8001",
            name="math_agent",
            description="Solves arithmetic and algebra problems.",
        ),
    ])

    # Or auto-discover from the remote server's agent card
    middleware = await A2AClientMiddleware.discover(
        "http://localhost:8001",
        "http://localhost:8002",
    )

    agent = create_agent(model=model, tools=[...], middleware=[middleware])
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional, cast

import httpx
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {"completed", "failed", "canceled"}

A2A_SYSTEM_PROMPT = """## Remote Agent Tools

You have access to tools that let you delegate tasks to specialized remote agents.
Each tool name corresponds to a distinct remote agent with its own capabilities.

**When to use remote agents:**
- When a task clearly falls within a remote agent's specialty
- When you need capabilities you don't have locally
- When you can parallelize independent sub-tasks across multiple agents

**How to use them:**
- Write a complete, self-contained message — remote agents have no context of this conversation
- Include all the information the remote agent needs to fulfill the request
- Use `session_id` if you need to maintain a multi-turn conversation with that agent
- The call is synchronous: wait for the response before continuing

**When NOT to use them:**
- For tasks you can handle yourself with your own tools
- For trivial or conversational requests that don't need specialization"""


# ---------------------------------------------------------------------------
# Config & input models
# ---------------------------------------------------------------------------


class RemoteAgentConfig(BaseModel):
    """Configuration for a single remote A2A agent."""

    url: str
    """Base URL of the remote agent server (e.g. ``http://math-agent:8001``)."""

    name: str
    """Tool name exposed to the LLM. Should be snake_case and unique."""

    description: str = ""
    """Tool description shown to the LLM. Pulled from agent card when using ``discover``."""


class _CallAgentInput(BaseModel):
    message: str
    """Complete, self-contained message for the remote agent."""

    session_id: Optional[str] = None
    """Optional session ID to maintain multi-turn context with the remote agent."""


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def _make_tool(config: RemoteAgentConfig, http_client: httpx.AsyncClient) -> StructuredTool:
    """Build a StructuredTool that calls *config* via A2A JSON-RPC."""
    base_url = config.url.rstrip("/")

    async def _acall(message: str, session_id: Optional[str] = None) -> str:
        params: dict[str, Any] = {
            "message": {"parts": [{"type": "text", "text": message}]},
            "configuration": {"mode": "interactive"},
        }
        if session_id:
            params["contextId"] = session_id

        payload = {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "params": params,
            "id": uuid.uuid4().hex[:12],
        }

        logger.debug("A2A → %s  message=%r", base_url, message[:80])
        resp = await http_client.post(f"{base_url}/", json=payload)
        resp.raise_for_status()
        body = resp.json()

        if body.get("error"):
            err = body["error"]
            raise RuntimeError(f"[{config.name}] A2A error {err['code']}: {err['message']}")

        task = body["result"]
        task_id = task["id"]

        # Poll until the task reaches a terminal state.
        # Interactive-mode tasks usually complete on the first response,
        # but we poll defensively in case the server runs async internally.
        while task["status"]["state"] not in _TERMINAL_STATES:
            await asyncio.sleep(0.3)
            poll_resp = await http_client.post(
                f"{base_url}/",
                json={
                    "jsonrpc": "2.0",
                    "method": "GetTask",
                    "params": {"id": task_id},
                    "id": uuid.uuid4().hex[:12],
                },
            )
            poll_resp.raise_for_status()
            poll_body = poll_resp.json()
            if poll_body.get("error"):
                err = poll_body["error"]
                raise RuntimeError(f"[{config.name}] poll error {err['code']}: {err['message']}")
            task = poll_body["result"]

        state = task["status"]["state"]
        if state == "failed":
            raise RuntimeError(f"[{config.name}] task failed: {task['status'].get('message', '')}")
        if state == "canceled":
            raise RuntimeError(f"[{config.name}] task was canceled")

        # Return the last agent message from history
        for msg in reversed(task.get("history", [])):
            if msg["role"] == "agent":
                return " ".join(
                    p.get("text", "")
                    for p in msg.get("parts", [])
                    if p.get("type") == "text"
                )
        return task.get("output", "")

    return StructuredTool.from_function(
        name=config.name,
        description=config.description or f"Call the {config.name} remote agent.",
        coroutine=_acall,
        args_schema=_CallAgentInput,
        infer_schema=False,
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class A2AClientMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Middleware that lets an agent call remote A2A-compatible agents as tools.

    Each configured remote agent is exposed as a LangChain ``StructuredTool``.
    The LLM can invoke them by name, passing a ``message`` and an optional
    ``session_id`` for multi-turn context.

    The middleware also injects a system prompt that explains when and how to
    use remote agents, and lists all available agents by name and description.

    Example — explicit config::

        from fast_langchain_server.a2a_client import A2AClientMiddleware, RemoteAgentConfig
        from langchain.agents import create_agent

        middleware = A2AClientMiddleware(agents=[
            RemoteAgentConfig(
                url="http://math-agent:8001",
                name="math_agent",
                description="Solves arithmetic and algebra problems.",
            ),
            RemoteAgentConfig(
                url="http://search-agent:8002",
                name="search_agent",
                description="Searches the web for up-to-date information.",
            ),
        ])
        agent = create_agent(model=model, tools=[...], middleware=[middleware])

    Example — auto-discover from agent cards::

        middleware = await A2AClientMiddleware.discover(
            "http://math-agent:8001",
            "http://search-agent:8002",
        )
        agent = create_agent(model=model, tools=[...], middleware=[middleware])
    """

    def __init__(
        self,
        agents: list[RemoteAgentConfig | dict[str, str]],
        *,
        system_prompt: str = A2A_SYSTEM_PROMPT,
        timeout: float = 60.0,
    ) -> None:
        """
        Parameters
        ----------
        agents:
            List of :class:`RemoteAgentConfig` objects (or plain dicts with the
            same fields) describing each remote agent.
        system_prompt:
            Injected into the system message to guide the LLM on when to
            delegate to remote agents.
        timeout:
            HTTP timeout in seconds for all A2A calls (default 60 s).
        """
        super().__init__()
        self._http = httpx.AsyncClient(timeout=timeout)
        self._configs: list[RemoteAgentConfig] = [
            RemoteAgentConfig(**a) if isinstance(a, dict) else a for a in agents
        ]
        self.system_prompt = system_prompt
        self.tools: list[StructuredTool] = [_make_tool(cfg, self._http) for cfg in self._configs]

    # ── Auto-discovery ────────────────────────────────────────────────────────

    @classmethod
    async def discover(
        cls,
        *urls: str,
        system_prompt: str = A2A_SYSTEM_PROMPT,
        timeout: float = 60.0,
    ) -> "A2AClientMiddleware":
        """Create a middleware by fetching agent cards from remote servers.

        Makes a ``GET /.well-known/agent.json`` request to each URL and uses
        the returned ``name`` and ``description`` fields.  Servers that fail
        discovery are skipped with a warning.

        Parameters
        ----------
        *urls:
            Base URLs of the remote agent servers.
        system_prompt:
            Override the default system prompt.
        timeout:
            HTTP timeout for agent card fetches and subsequent A2A calls.
        """
        configs: list[RemoteAgentConfig] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for url in urls:
                base = url.rstrip("/")
                try:
                    resp = await client.get(f"{base}/.well-known/agent.json")
                    resp.raise_for_status()
                    card = resp.json()
                    configs.append(
                        RemoteAgentConfig(
                            url=url,
                            name=card["name"],
                            description=card.get("description", ""),
                        )
                    )
                    logger.info("A2A discovered: %s @ %s", card["name"], url)
                except Exception as exc:
                    logger.warning("A2A discovery failed for %s: %s", url, exc)

        return cls(configs, system_prompt=system_prompt, timeout=timeout)

    # ── System prompt injection ───────────────────────────────────────────────

    def _build_system_message(self, request: ModelRequest[ContextT]) -> SystemMessage:
        agent_list = "\n".join(
            f"- **{cfg.name}**: {cfg.description}" for cfg in self._configs
        )
        full_prompt = f"{self.system_prompt}\n\n### Available Remote Agents\n{agent_list}"

        if request.system_message is not None:
            content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{full_prompt}"},
            ]
        else:
            content = [{"type": "text", "text": full_prompt}]

        return SystemMessage(content=cast("list[str | dict[str, str]]", content))

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        return handler(request.override(system_message=self._build_system_message(request)))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        return await handler(request.override(system_message=self._build_system_message(request)))
