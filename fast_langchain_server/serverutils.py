"""
Settings and utility helpers for the LangChain Agent Server.

All configuration is driven by environment variables so the same agent.py
can run in local dev, Docker, and Kubernetes without code changes.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class AgentServerSettings(BaseSettings):
    """All settings are read from environment variables (or a .env file)."""

    # ── Identity ──────────────────────────────────────────────────────────────
    agent_name: str = Field(..., description="Unique identifier for this agent")
    agent_description: str = Field("AI Agent", description="Human-readable description")
    agent_instructions: str = Field(
        "You are a helpful assistant.",
        description="System prompt injected into every conversation",
    )

    # ── Network ───────────────────────────────────────────────────────────────
    agent_port: int = Field(8000, description="HTTP port to listen on")
    agent_log_level: str = Field("INFO", description="Logging level")
    agent_access_log: bool = Field(False, description="Enable uvicorn access log")

    # ── Model ─────────────────────────────────────────────────────────────────
    model_api_url: str = Field(
        ..., description="Base URL for the OpenAI-compatible LLM endpoint"
    )
    model_name: str = Field(..., description="Model identifier (e.g. llama3.2, gpt-4o)")
    model_api_key: str = Field(
        "not-needed",
        description="API key — use 'not-needed' for local/private endpoints",
    )
    model_temperature: float = Field(0.7, description="LLM temperature")
    model_max_tokens: Optional[int] = Field(None, description="Max tokens per response")

    # ── Memory ────────────────────────────────────────────────────────────────
    memory_enabled: bool = Field(True, description="Enable session message history")
    memory_type: str = Field(
        "local", description="Memory backend: 'local', 'redis', or 'null'"
    )
    memory_redis_url: str = Field("", description="Redis connection URL")
    memory_context_limit: int = Field(
        20, description="Max messages loaded into each LLM call from history"
    )
    memory_max_sessions: int = Field(1000, description="Max in-memory sessions")
    memory_max_messages_per_session: int = Field(
        500, description="Max messages stored per session before oldest are trimmed"
    )

    # ── A2A task manager ──────────────────────────────────────────────────────
    task_manager_type: str = Field(
        "none",
        description="A2A task manager: 'none' disables A2A, 'local' enables JSON-RPC endpoint",
    )
    task_manager_max_tasks: int = Field(
        10_000, description="Max tracked tasks for LocalTaskManager"
    )

    # ── Autonomous execution ──────────────────────────────────────────────────
    autonomous_goal: str = Field(
        "",
        description="If set, the agent runs this goal autonomously on startup via the A2A task manager",
    )
    autonomous_interval_seconds: int = Field(
        0, description="Seconds between autonomous iterations (0 = no pause)"
    )
    autonomous_max_iter_runtime_seconds: int = Field(
        60, description="Max seconds per autonomous iteration before timeout"
    )

    # ── OpenTelemetry ─────────────────────────────────────────────────────────
    otel_service_name: str = Field("", description="OTEL service name")
    otel_exporter_otlp_endpoint: str = Field("", description="OTLP gRPC collector endpoint")
    otel_enabled: bool = Field(False, description="Explicitly enable OTel even without OTLP endpoint")
    otel_sdk_disabled: bool = Field(False, description="Explicitly disable OTel SDK")
    otel_include_http_server: bool = Field(
        False, description="Auto-instrument FastAPI HTTP spans (opt-in)"
    )
    otel_include_http_client: bool = Field(
        False, description="Auto-instrument httpx HTTP client spans (opt-in)"
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }

    @property
    def otel_active(self) -> bool:
        """True when OTel should be initialised."""
        if self.otel_sdk_disabled:
            return False
        return self.otel_enabled or bool(
            self.otel_service_name and self.otel_exporter_otlp_endpoint
        )


def configure_logging(level: str = "INFO", otel_correlation: bool = False) -> None:
    """Configure root logger with a structured format."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_langchain_model(settings: AgentServerSettings) -> Any:
    """Create a ChatOpenAI-compatible model from server settings.

    Uses the OpenAI-compatible endpoint — works with Ollama, vLLM, LM Studio,
    and any other provider that speaks the OpenAI API.
    """
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "base_url": settings.model_api_url,
        "model": settings.model_name,
        "api_key": settings.model_api_key,
        "temperature": settings.model_temperature,
        "streaming": True,  # required for token-level streaming in LangGraph
    }
    if settings.model_max_tokens is not None:
        kwargs["max_tokens"] = settings.model_max_tokens

    logger.info("Model: '%s' at %s", settings.model_name, settings.model_api_url)
    return ChatOpenAI(**kwargs)


def inspect_agent(agent: Any) -> dict[str, Any]:
    """Introspect a CompiledStateGraph and extract metadata without executing it.

    Works for agents created with ``create_react_agent`` / ``create_agent``
    (LangGraph prebuilt pattern).  Returns an empty/partial dict gracefully
    for custom graphs that don't follow the standard structure.

    Returns
    -------
    dict with keys:
        tools       : list of LangChain BaseTool objects (may be empty)
        description : system prompt string extracted from the agent (may be "")
        model_name  : LLM model name string (may be "")
    """
    import inspect as _inspect

    result: dict[str, Any] = {"tools": [], "description": "", "model_name": ""}

    try:
        nodes = getattr(agent, "nodes", {})

        # ── Tools ─────────────────────────────────────────────────────────────
        # The 'tools' node is a PregelNode whose .bound is a ToolNode.
        # ToolNode.tools_by_name is a dict[str, BaseTool].
        tools_pregel = nodes.get("tools")
        if tools_pregel is not None:
            tool_node = getattr(tools_pregel, "bound", None)
            if tool_node is not None:
                tools_by_name = getattr(tool_node, "tools_by_name", {})
                result["tools"] = list(tools_by_name.values())
    except Exception:
        pass

    try:
        nodes = getattr(agent, "nodes", {})

        # ── System prompt & model name ─────────────────────────────────────────
        # The 'agent' node is a RunnableCallable whose .func closure contains
        # 'static_model' — a RunnableSequence with:
        #   steps[0]: RunnableCallable (prompt) — closure has '_system_message'
        #   steps[1]: RunnableBinding wrapping the ChatModel
        agent_pregel = nodes.get("agent")
        if agent_pregel is not None:
            runnable = getattr(agent_pregel, "bound", None)
            if runnable is not None:
                closure = _inspect.getclosurevars(runnable.func)
                static_model = closure.nonlocals.get("static_model")
                if static_model is not None and hasattr(static_model, "steps"):
                    steps = static_model.steps
                    # System prompt
                    if steps:
                        prompt_step = steps[0]
                        prompt_closure = _inspect.getclosurevars(prompt_step.func)
                        sys_msg = prompt_closure.nonlocals.get("_system_message")
                        if sys_msg is not None:
                            result["description"] = getattr(sys_msg, "content", "") or ""
                    # Model name
                    if len(steps) > 1:
                        model_binding = steps[1]
                        bound_model = getattr(model_binding, "bound", None)
                        result["model_name"] = getattr(bound_model, "model_name", "") or ""
    except Exception:
        pass

    return result


def extract_text_content(content: Any) -> str:
    """Safely extract plain text from a LangChain message content field.

    Content can be a plain string or a list of content blocks
    (multimodal messages with text/image parts).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content) if content else ""
