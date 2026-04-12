"""
fast-langchain-server
~~~~~~~~~~~~~~~~~~~~~
Production HTTP server for LangChain / LangGraph agents.

Quick start
-----------
    # agent.py
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from fast_langchain_server import Server

    @tool
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b

    agent = create_agent("openai:gpt-4o", tools=[add])
    server = Server(agent, tools=[add])

    # Run directly:   python agent.py  (call server.run())
    # Or with uvicorn: uvicorn agent:server.app
"""

from fast_langchain_server.server import Server
from fast_langchain_server.serverutils import inspect_agent
from fast_langchain_server.context import AgentContext
from fast_langchain_server.auth import (
    AuthToken,
    AuthProvider,
    APIKeyProvider,
    EnvAPIKeyProvider,
    JWTProvider,
    MultiAuth,
)
from fast_langchain_server.middleware import (
    AgentMiddleware,
    AuthMiddleware,
    TimingMiddleware,
    RateLimitMiddleware,
)
from fast_langchain_server.lifespan import (
    Lifespan,
    lifespan,
    DEFAULT_LIFESPAN,
)
from fast_langchain_server.authorization import (
    AuthContext,
    AuthCheck,
    AuthorizationMiddleware,
    require_scopes,
    allow_any_authenticated,
    allow_own_session,
    deny_all,
    all_of,
    any_of,
)

__all__ = [
    # Server
    "Server",
    "inspect_agent",
    # Context
    "AgentContext",
    # Auth
    "AuthToken",
    "AuthProvider",
    "APIKeyProvider",
    "EnvAPIKeyProvider",
    "JWTProvider",
    "MultiAuth",
    # Middleware
    "AgentMiddleware",
    "AuthMiddleware",
    "TimingMiddleware",
    "RateLimitMiddleware",
    # Lifespan
    "Lifespan",
    "lifespan",
    "DEFAULT_LIFESPAN",
    # Authorization
    "AuthContext",
    "AuthCheck",
    "AuthorizationMiddleware",
    "require_scopes",
    "allow_any_authenticated",
    "allow_own_session",
    "deny_all",
    "all_of",
    "any_of",
]
__version__ = "0.5.0"
