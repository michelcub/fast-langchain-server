"""
fast-langchain-server
~~~~~~~~~~~~~~~~~~~~~
Production HTTP server for LangChain / LangGraph agents.

Quick start
-----------
# agent.py
from langchain.agents import create_agent
from langchain_core.tools import tool
from fast_langchain_server import serve

@tool
def add(a: int, b: int) -> int:
    "Add two numbers."
    return a + b

agent = create_agent("openai:gpt-4o", tools=[add])
app = serve(agent, tools=[add])   # FastAPI ASGI app

# Run: uvicorn agent:app --reload
# Or:  AGENT_NAME=my-agent MODEL_API_URL=... MODEL_NAME=... fast-langchain-server run agent.py
"""

from fast_langchain_server.server import create_agent_server, serve
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
    "serve",
    "create_agent_server",
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
__version__ = "0.1.3"
