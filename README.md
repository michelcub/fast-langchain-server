# Fast LangChain Server

> Production HTTP server for LangChain/LangGraph agents — OpenAI-compatible API, streaming, session memory, authentication, middleware, and agent discovery.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/badge/version-0.5.0-brightgreen)](https://pypi.org/project/fast-langchain-server/)

## Overview

**Fast LangChain Server** transforms any LangChain/LangGraph agent into a production-ready HTTP service. Instantiate `Server`, call `run()` — or expose `server.app` to any ASGI server.

## Features

- **Single entry point** — one class (`Server`) to create, configure, and run
- **OpenAI-Compatible API** — Drop-in compatible with OpenAI clients (`/v1/chat/completions`)
- **Streaming** — Real-time token streaming via Server-Sent Events (SSE)
- **Session Memory** — Conversation history with local or Redis backends
- **Authentication** — Pluggable `AuthProvider`: API keys, JWT Bearer tokens, or compose multiple via `|`
- **Middleware** — Composable chain: `AgentMiddleware` instances go through the internal chain; Starlette/ASGI classes go to `app.add_middleware()`
- **Authorization** — Per-endpoint `AuthCheck` rules with built-ins: `require_scopes`, `allow_own_session`, etc.
- **Lifespan** — Composable startup/shutdown lifecycle via `@lifespan` decorator and `|` operator
- **A2A Protocol** — Agent-to-Agent JSON-RPC 2.0 with autonomous execution support (enabled by default)
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
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from fast_langchain_server import Server
import os

@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b

model = ChatOpenAI(
    model=os.getenv("MODEL_NAME"),
    api_key=os.getenv("MODEL_API_KEY"),
    base_url=os.getenv("MODEL_API_URL"),
)

agent = create_agent(model=model, tools=[add])

server = Server(
    agent,
    tools=[add],
    agent_name="my-agent",
    agent_description="A helpful assistant with math capabilities",
)

# Expose for uvicorn / gunicorn
app = server.app
```

Configure environment variables in `.env`:
```bash
MODEL_NAME=gpt-4o
MODEL_API_URL=https://api.openai.com/v1
MODEL_API_KEY=sk-...
```

Run:
```bash
uvicorn agent:app
```

Or run directly from code:
```python
if __name__ == "__main__":
    server.run()                    # uses AGENT_PORT / default 8000
    server.run(port=9000)           # explicit port
    server.run(port=9000, reload=True)  # dev mode
```

### CLI

```bash
# Auto-discover a Server instance or agent in agent.py
fast-langchain-server run agent.py

# Explicit attribute
fast-langchain-server run agent.py:server --port 9000 --reload
```

---

## Server configuration

All constructor parameters fall back to environment variables when not provided:

```python
server = Server(
    agent,                                    # required
    tools=[add, search],                      # exposed in discovery card
    agent_name="my-agent",                    # AGENT_NAME env var
    agent_description="Does math and search", # AGENT_DESCRIPTION env var
    a2a=True,                                 # enable A2A JSON-RPC (default True)
    lifespan=DEFAULT_LIFESPAN | my_lifespan,  # composable startup/shutdown
    memory=my_memory_backend,                 # inject a custom Memory instance
    # any AgentServerSettings field:
    agent_port=9000,                          # AGENT_PORT
    memory_type="redis",                      # MEMORY_TYPE
    memory_redis_url="redis://localhost:6379",
)
```

### run() parameters

`run()` accepts all `uvicorn.run()` keyword arguments:

```python
server.run(
    host="0.0.0.0",
    port=8080,
    reload=True,           # dev only — incompatible with workers
    workers=4,
    log_level="warning",
    access_log=True,
    ssl_keyfile="key.pem",
    ssl_certfile="cert.pem",
)
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
| `POST` | `/` | A2A JSON-RPC 2.0 *(when `a2a=True`, default)* |

### Chat completions

```bash
# Non-streaming
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is 2+2?"}]}'

# Streaming
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Tell me a joke"}], "stream": true}'
```

Pass `session_id` in the body or via `X-Session-ID` header to continue a conversation:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -d '{"messages": [{"role": "user", "content": "And 3+3?"}], "session_id": "abc123"}'
```

### Agent discovery card

```bash
curl http://localhost:8000/.well-known/agent.json | jq
```

```json
{
  "name": "math-agent",
  "description": "A helpful math agent",
  "url": "http://localhost:8000",
  "skills": [{"id": "add", "name": "add", "description": "Add two numbers."}],
  "capabilities": {"streaming": true, "memory": true, "a2a": true}
}
```

Skills are populated from the `tools` parameter. Each tool's name and docstring become the skill's `name` and `description`.

---

## Authentication

```python
from fast_langchain_server import Server, AuthMiddleware, EnvAPIKeyProvider, JWTProvider

server = Server(agent, tools=[...])

# Option A: API keys from AGENT_API_KEYS env var (comma-separated)
server.add_middleware(AuthMiddleware(provider=EnvAPIKeyProvider()))

# Option B: JWT Bearer tokens
server.add_middleware(AuthMiddleware(
    provider=JWTProvider(
        jwks_url="https://auth.example.com/.well-known/jwks.json",
        audience="my-agent",
    )
))

# Option C: compose providers (JWT first, API key fallback)
server.add_middleware(AuthMiddleware(
    provider=JWTProvider(...) | EnvAPIKeyProvider()
))
```

Requests must include the token in one of:
```
Authorization: Bearer <token>
X-API-Key: <token>
```

`/health`, `/ready`, `/.well-known/agent.json` are excluded from auth by default.

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

`add_middleware()` routes by type:
- **`AgentMiddleware` instance** → added to the internal request-processing chain
- **Any other class** → forwarded to `app.add_middleware()` (Starlette/ASGI level)

```python
from fast_langchain_server import Server, AuthMiddleware, TimingMiddleware, RateLimitMiddleware, EnvAPIKeyProvider
from starlette.middleware.cors import CORSMiddleware

server = Server(agent, tools=[...])

(
    server
    .add_middleware(TimingMiddleware())
    .add_middleware(AuthMiddleware(provider=EnvAPIKeyProvider()))
    .add_middleware(RateLimitMiddleware(max_rpm=60))
    .add_middleware(CORSMiddleware, allow_origins=["*"])  # ASGI-level
)
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
        print(f"Request: session={ctx.session_id}")
        result = await call_next(ctx)
        print(f"Done: session={ctx.session_id}")
        return result

server.add_middleware(LoggingMiddleware())
```

**Hooks:**

| Hook | When it runs |
|------|-------------|
| `on_request(ctx, call_next)` | Once per chat request — wraps the full lifecycle |
| `on_agent_run(ctx, call_next)` | Immediately before/after the LangGraph agent runs |

---

## Authorization

```python
from fast_langchain_server import AuthorizationMiddleware, require_scopes, allow_own_session

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

---

## Lifespan

```python
from fast_langchain_server import Server, lifespan, DEFAULT_LIFESPAN

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

server = Server(
    agent,
    tools=[...],
    lifespan=DEFAULT_LIFESPAN | db_lifespan | cache_lifespan,
)
```

Composes left-to-right (enters left first, exits right first — LIFO). `DEFAULT_LIFESPAN` handles OTel init, startup/shutdown logging, the autonomous loop, and graceful teardown.

---

## A2A Protocol

Agent-to-Agent is **enabled by default** (`a2a=True`). Disable it with `a2a=False`:

```python
server = Server(agent, tools=[...])          # a2a=True, POST / mounted
server = Server(agent, tools=[...], a2a=False)  # no A2A endpoint
```

When enabled, the `/.well-known/agent.json` card includes `"a2a": true` and `"supportedProtocols": ["jsonrpc"]`.

---

## AgentContext

Available throughout the middleware chain and agent execution:

```python
ctx.session_id      # resolved session identifier
ctx.request_id      # UUID for this request
ctx.user_input      # last user message
ctx.model           # model name from the request
ctx.request         # raw Starlette Request object (None for A2A entry points)
ctx.headers         # lowercased HTTP headers dict (derived from ctx.request)
ctx.otel_context    # W3C TraceContext for distributed tracing
ctx.auth_token      # set by AuthMiddleware (or None)

ctx.set_meta("key", value)
ctx.get_meta("key", default)

await ctx.emit_progress("tool_call", "search_web")  # push SSE progress event
```

The raw `request` object exposes anything from the incoming HTTP request:

```python
async def on_request(self, ctx: AgentContext, call_next):
    if ctx.request is not None:
        client_ip = ctx.request.client.host
        cookies = ctx.request.cookies
        query_params = dict(ctx.request.query_params)
    return await call_next(ctx)
```

> `ctx.headers` is a convenience property that returns a lower-cased dict of all request headers.  It returns `{}` when `ctx.request` is `None` (e.g. A2A calls).

---

## Configuration reference

All settings fall back to environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_NAME` | auto-generated | Agent identifier |
| `MODEL_API_URL` | — | LLM API base URL |
| `MODEL_NAME` | — | Model name (`gpt-4o`, `llama3.2`…) |
| `MODEL_API_KEY` | `not-needed` | API key |
| `MODEL_TEMPERATURE` | `0.7` | Sampling temperature |
| `AGENT_DESCRIPTION` | `"AI Agent"` | Human-readable description |
| `AGENT_INSTRUCTIONS` | — | System prompt |
| `AGENT_PORT` | `8000` | HTTP port |
| `AGENT_LOG_LEVEL` | `INFO` | Log level |
| `AGENT_ACCESS_LOG` | `false` | Uvicorn access log |
| `MEMORY_ENABLED` | `true` | Enable session memory |
| `MEMORY_TYPE` | `local` | `local` \| `redis` \| `null` |
| `MEMORY_REDIS_URL` | — | Redis connection URL |
| `MEMORY_CONTEXT_LIMIT` | `20` | Messages per request |
| `AGENT_API_KEYS` | — | Comma-separated API keys (`EnvAPIKeyProvider`) |
| `TASK_MANAGER_TYPE` | `local` | `local` \| `none` (overridden by `a2a=`) |
| `AUTONOMOUS_GOAL` | — | Goal for autonomous execution loop |
| `AUTONOMOUS_INTERVAL_SECONDS` | `0` | Interval between autonomous runs |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTel collector endpoint |

---

## Docker

```bash
docker run -p 8000:8000 \
  -e MODEL_API_URL=https://api.openai.com/v1 \
  -e MODEL_NAME=gpt-4o \
  -e MODEL_API_KEY=sk-... \
  -v $(pwd)/agent.py:/app/agent.py \
  fast-langchain-server
```

---

## Architecture

```
HTTP Request
     │
     ▼
 [FastAPI app]  — builds AgentContext
     │
     ▼
 [AuthMiddleware]           verify token → ctx.auth_token
     │
     ▼
 [AuthorizationMiddleware]  check scopes per endpoint
     │
     ▼
 [RateLimitMiddleware]      token bucket per session
     │
     ▼
 [TimingMiddleware]         measure elapsed time
     │
     ▼
 [Server._run_agent / _stream_response]
     │
     ▼
 [LangGraph agent.ainvoke / astream]
     │
     ▼
 [Memory backend]           save messages
     │
     ▼
 HTTP Response / SSE stream
```

**Module map:**

| Module | Purpose |
|--------|---------|
| `server.py` | `Server` — single entry point |
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
make dev     # install dev dependencies
make test    # run tests (185 tests)
make lint    # lint
make run     # local hot-reload
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
