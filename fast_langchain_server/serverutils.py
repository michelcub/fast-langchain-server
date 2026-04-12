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
        "local",
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
