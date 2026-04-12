# Fast LangChain Server

> Production HTTP server for LangChain/LangGraph agents — OpenAI-compatible API, streaming, session memory, authentication, middleware, and agent discovery.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/badge/version-0.1.3-brightgreen)](https://pypi.org/project/fast-langchain-server/)

## Overview

**Fast LangChain Server** transforms any LangChain/LangGraph agent into a production-ready HTTP service. Deploy with a single line of code and get streaming responses, automatic session management, pluggable authentication, a composable middleware chain, and built-in observability.

## Features

- **OpenAI-Compatible API** — Drop-in compatible with OpenAI clients (`/v1/chat/completions`)
- **Streaming** — Real-time token streaming via Server-Sent Events (SSE)
- **Session Memory** — Conversation history with local or Redis backends
- **Authentication** — Pluggable `AuthProvider`: API keys, JWT Bearer tokens, or compose multiple via `|`
- **Middleware** — Composable chain with built-ins: `AuthMiddleware`, `TimingMiddleware`, `RateLimitMiddleware`
- **Authorization** — Per-endpoint `AuthCheck` rules with built-ins: `require_scopes`, `allow_own_session`, etc.
- **Lifespan** — Composable startup/shutdown lifecycle via `@lifespan` decorator and `|` operator
- **A2A Protocol** — Agent-to-Agent JSON-RPC 2.0 with autonomous execution support
- **OpenTelemetry** — Distributed tracing with W3C TraceContext propagation
- **Production Ready** — Health checks, structured logging, Docker-ready

---

## Quick Start

### Installation

```bash
pip install fast-langchain-server
```

### Minimal example

```python
# agent.py
from langchain.agents import create_agent
from langchain_core.tools import tool
from fast_langchain_server import serve

@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b

agent = create_agent(
    model="openai:gpt-4o",
    tools=[add],
    system_prompt="You are a helpful assistant.",
)

app = serve(agent, tools=[add])
```

```bash
AGENT_NAME=my-agent \
MODEL_API_URL=https://api.openai.com/v1 \
MODEL_NAME=gpt-4o \
MODEL_API_KEY=sk-... \
uvicorn agent:app
```

### CLI

```bash
# Auto-discover the agent in agent.py and start the server
fast-langchain-server run agent.py

# Explicit attribute
fast-langchain-server run agent.py:app
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness probe |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (streaming + non-streaming) |
| `GET` | `/.well-known/agent.json` | A2A agent discovery card |
| `GET` | `/memory/sessions` | List active sessions |
| `DELETE` | `/memory/sessions/{id}` | Delete a session |
| `POST` | `/` | A2A JSON-RPC 2.0 *(when `TASK_MANAGER_TYPE=local`)* |

### Chat completions

```bash
# Non-streaming
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "model": "agent"
  }'
```

```bash
# Streaming
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Tell me a joke"}],
    "stream": true
  }'
```

**Session continuity** — pass `session_id` in the body or via `X-Session-ID` header to continue a conversation:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "And 3+3?"}], "session_id": "abc123"}'
```

---

## Authentication

Add token verification to every request with any `AuthProvider`:

```python
from fast_langchain_server import (
    create_agent_server,
    AuthMiddleware,
    EnvAPIKeyProvider,
    JWTProvider,
)

server = create_agent_server(tools=[...])

# Option A: API keys from environment variable AGENT_API_KEYS=sk-a,sk-b
server.add_middleware(AuthMiddleware(provider=EnvAPIKeyProvider()))

# Option B: JWT Bearer tokens validated against a JWKS endpoint
server.add_middleware(AuthMiddleware(
    provider=JWTProvider(
        jwks_url="https://auth.example.com/.well-known/jwks.json",
        audience="my-agent",
    )
))

# Option C: compose both (JWT first, API key fallback)
server.add_middleware(AuthMiddleware(
    provider=JWTProvider(...) | EnvAPIKeyProvider()
))
```

Requests must include the token in one of these headers:

```
Authorization: Bearer <token>
X-API-Key: <token>
```

The following endpoints are excluded from auth by default:
`/health`, `/ready`, `/.well-known/agent.json`

### Available providers

| Provider | Description |
|----------|-------------|
| `APIKeyProvider(keys)` | Static dict `{"key": "owner"}` |
| `EnvAPIKeyProvider(env_var)` | Comma-separated keys from env var (default: `AGENT_API_KEYS`) |
| `JWTProvider(jwks_url, audience)` | Validates Bearer tokens against JWKS. Requires `PyJWT[crypto]` |
| `MultiAuth(*providers)` | Tries providers in order — created automatically via `\|` |

### Custom provider

```python
from fast_langchain_server import AuthProvider, AuthToken

class MyDatabaseProvider(AuthProvider):
    async def verify_token(self, token: str) -> AuthToken | None:
        user = await db.lookup_token(token)
        if user:
            return AuthToken(subject=user.id, scopes=user.scopes, raw=token)
        return None

server.add_middleware(AuthMiddleware(provider=MyDatabaseProvider()))
```

---

## Middleware

Middleware intercepts requests before and after the agent runs. Add them with `add_middleware()` — they execute in the order added (first = outermost).

```python
from fast_langchain_server import (
    create_agent_server,
    AuthMiddleware,
    TimingMiddleware,
    RateLimitMiddleware,
    EnvAPIKeyProvider,
)

server = create_agent_server(tools=[...])
server.add_middleware(TimingMiddleware())
server.add_middleware(AuthMiddleware(provider=EnvAPIKeyProvider()))
server.add_middleware(RateLimitMiddleware(max_rpm=60))
```

### Built-in middlewares

| Middleware | Description |
|------------|-------------|
| `AuthMiddleware(provider)` | Verifies tokens and sets `ctx.auth_token` |
| `TimingMiddleware(log_level)` | Logs elapsed time per request |
| `RateLimitMiddleware(max_rpm)` | Token-bucket rate limiter per session (default: 60 req/min) |

### Custom middleware

```python
from fast_langchain_server import AgentMiddleware

class LoggingMiddleware(AgentMiddleware):
    async def on_request(self, ctx, call_next):
        print(f"Request: session={ctx.session_id} input={ctx.user_input[:50]}")
        result = await call_next(ctx)
        print(f"Done: session={ctx.session_id}")
        return result

server.add_middleware(LoggingMiddleware())
```

**Hooks available:**

| Hook | When it runs |
|------|-------------|
| `on_request(ctx, call_next)` | Once per chat request — wraps the full lifecycle |
| `on_agent_run(ctx, call_next)` | Immediately before/after the LangGraph agent runs |

---

## Authorization

Separate from authentication — controls *what* an authenticated user can do.

```python
from fast_langchain_server import (
    AuthorizationMiddleware,
    require_scopes,
    allow_own_session,
    all_of,
)

server.add_middleware(AuthorizationMiddleware({
    "/v1/chat/completions": require_scopes("chat"),
    "/memory/sessions":     require_scopes("admin"),
}))
```

### Built-in checks

| Check | Description |
|-------|-------------|
| `require_scopes(*scopes)` | All listed scopes must be present in the token |
| `allow_any_authenticated()` | Any valid token is sufficient |
| `allow_own_session()` | Token subject must own the requested session |
| `deny_all()` | Always denies (maintenance mode) |
| `all_of(*checks)` | AND — all checks must pass |
| `any_of(*checks)` | OR — at least one check must pass |

### Custom check

```python
from fast_langchain_server.authorization import AuthContext

def my_ip_allowlist(ctx: AuthContext) -> bool:
    # ctx.token carries the verified AuthToken
    return ctx.token and ctx.token.subject in ALLOWED_SUBJECTS
```

---

## Lifespan

Manage startup and shutdown of resources with composable lifecycle hooks.

```python
from fast_langchain_server import create_agent_server, lifespan, DEFAULT_LIFESPAN

@lifespan
async def db_lifespan(server):
    server.lifespan_context["db"] = await connect_db()
    yield {}
    await server.lifespan_context["db"].close()

@lifespan
async def cache_lifespan(server):
    server.lifespan_context["cache"] = await connect_redis()
    yield {}
    await server.lifespan_context["cache"].aclose()

# Compose with | — enters left-to-right, exits right-to-left (LIFO)
server = create_agent_server(
    tools=[...],
    lifespan=DEFAULT_LIFESPAN | db_lifespan | cache_lifespan,
)
```

Access the context at runtime:

```python
db = server.lifespan_context["db"]
```

`DEFAULT_LIFESPAN` includes OpenTelemetry init, startup/shutdown logging, the autonomous loop launcher, and graceful shutdown of memory and task manager.

---

## AgentContext

Every request carries an `AgentContext` through the middleware chain and into the agent execution layer. Middlewares can read and enrich it:

```python
ctx.session_id      # resolved session identifier
ctx.request_id      # UUID for this specific request
ctx.user_input      # last user message
ctx.model           # model name from the request
ctx.headers         # lowercased HTTP headers dict
ctx.otel_context    # W3C TraceContext for distributed tracing
ctx.auth_token      # AuthToken set by AuthMiddleware (or None)
ctx.endpoint        # HTTP path ("/v1/chat/completions")

ctx.set_meta("key", value)    # store custom data for downstream use
ctx.get_meta("key", default)  # retrieve it

await ctx.emit_progress("tool_call", "search_web")  # push SSE progress event
```

---

## Configuration

All settings are driven by environment variables (or `.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_NAME` | ✓ | — | Agent identifier |
| `MODEL_API_URL` | ✓ | — | LLM API base URL (OpenAI, Ollama, vLLM…) |
| `MODEL_NAME` | ✓ | — | Model name (`gpt-4o`, `llama3.2`…) |
| `MODEL_API_KEY` | — | `not-needed` | API key for the model endpoint |
| `MODEL_TEMPERATURE` | — | `0.7` | Sampling temperature |
| `MODEL_MAX_TOKENS` | — | *(none)* | Max tokens per response |
| `AGENT_DESCRIPTION` | — | `"AI Agent"` | Human-readable description |
| `AGENT_INSTRUCTIONS` | — | — | System prompt |
| `AGENT_PORT` | — | `8000` | HTTP port |
| `AGENT_LOG_LEVEL` | — | `INFO` | Log level |
| `AGENT_ACCESS_LOG` | — | `false` | Enable uvicorn access log |
| `MEMORY_ENABLED` | — | `true` | Enable session memory |
| `MEMORY_TYPE` | — | `local` | `local` \| `redis` \| `null` |
| `MEMORY_REDIS_URL` | For Redis | — | Redis connection URL |
| `MEMORY_CONTEXT_LIMIT` | — | `20` | Messages to load per request |
| `MEMORY_MAX_SESSIONS` | — | `1000` | Max sessions in local memory |
| `AGENT_API_KEYS` | — | — | Comma-separated API keys (used by `EnvAPIKeyProvider`) |
| `TASK_MANAGER_TYPE` | — | `none` | `none` \| `local` (enables A2A) |
| `AUTONOMOUS_GOAL` | — | — | Goal for autonomous execution loop |
| `AUTONOMOUS_INTERVAL_SECONDS` | — | `0` | Interval between autonomous runs |
| `OTEL_ENABLED` | — | `false` | Enable OpenTelemetry |
| `OTEL_SERVICE_NAME` | — | — | OTel service name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | — | OTel collector endpoint |

---

## Docker

```bash
# Build
docker build -t fast-langchain-server .

# Run
docker run -p 8000:8000 \
  -e AGENT_NAME=my-agent \
  -e MODEL_API_URL=https://api.openai.com/v1 \
  -e MODEL_NAME=gpt-4o \
  -e MODEL_API_KEY=sk-... \
  -e AGENT_API_KEYS=my-secret-key \
  -v $(pwd)/agent.py:/app/agent.py \
  fast-langchain-server
```

---

## Architecture

```
HTTP Request
     │
     ▼
 [FastAPI]  — builds AgentContext(session_id, request_id, user_input, headers…)
     │
     ▼
 [AuthMiddleware]          verify token → ctx.auth_token
     │
     ▼
 [AuthorizationMiddleware] check scopes per endpoint
     │
     ▼
 [RateLimitMiddleware]     token bucket per session
     │
     ▼
 [TimingMiddleware]        measure elapsed time
     │
     ▼
 [AgentServer._run_agent / _stream_response]
     │
     ▼
 [LangGraph agent.ainvoke / astream]
     │
     ▼
 [Memory backend]          save messages
     │
     ▼
 HTTP Response / SSE stream
```

**Module map:**

| Module | Purpose |
|--------|---------|
| `server.py` | `AgentServer`, `create_agent_server`, `serve` |
| `context.py` | `AgentContext` — per-request state object |
| `auth.py` | `AuthProvider`, `APIKeyProvider`, `EnvAPIKeyProvider`, `JWTProvider`, `MultiAuth` |
| `middleware.py` | `AgentMiddleware`, `AuthMiddleware`, `TimingMiddleware`, `RateLimitMiddleware` |
| `authorization.py` | `AuthorizationMiddleware`, `require_scopes`, `allow_own_session`, … |
| `lifespan.py` | `@lifespan`, `ComposedLifespan`, `DEFAULT_LIFESPAN` |
| `memory.py` | `LocalMemory`, `RedisMemory`, `NullMemory` |
| `a2a.py` | A2A protocol, `LocalTaskManager`, JSON-RPC handlers |
| `telemetry.py` | OpenTelemetry init and trace context propagation |
| `cli.py` | `fast-langchain-server` CLI |

---

## Development

```bash
# Install dev dependencies
make dev

# Run tests (185 tests)
make test

# Lint
make lint

# Run locally with hot-reload
make run
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
