# FastMCP Inspiration: Roadmap de Features

Análisis de cómo FastMCP Python implementa Context, Lifespan, Middleware, Auth y Authorization,
y cómo podemos inspirarnos para mejorar `fast-langchain-server`.

---

## Tabla de contenidos

1. [Context](#1-context)
2. [Lifespan](#2-lifespan)
3. [Middleware](#3-middleware)
4. [Authentication](#4-authentication)
5. [Authorization](#5-authorization)
6. [Arquitectura integrada](#6-arquitectura-integrada)
7. [Prioridad de implementación](#7-prioridad-de-implementación)

---

## 1. Context

### Cómo lo hace FastMCP

FastMCP inyecta un objeto `Context` en cada tool vía el sistema de DI de FastAPI
(`Annotated[Context, CurrentContext()]`). El objeto está **scoped por request** y vive en un
`ContextVar` de Python que se setea al inicio de cada request.

```python
# FastMCP — uso en un tool
@mcp.tool
async def my_tool(x: int, ctx: Context) -> str:
    await ctx.info(f"Processing {x}")
    await ctx.report_progress(50, 100, "halfway")
    await ctx.set_state("key", "value")
    return await ctx.get_state("key")
```

Responsabilidades del `Context`:
- Logging estructurado con destino al cliente (`ctx.info/debug/warning/error`)
- Progress reporting (`ctx.report_progress(current, total, message)`)
- Estado de sesión con TTL (`ctx.get_state / set_state`)
- Metadata del request (`ctx.request_id`, `ctx.session_id`, `ctx.client_id`)
- Acceso a recursos y prompts registrados
- Detección del transport (SSE, HTTP, STDIO)

### Estado actual en fast-langchain-server

El context se pasa como parámetros sueltos:

```python
# Actual — parámetros dispersos
async def _run_agent(self, user_input: str, session_id: str, parent_ctx=None)
async def _stream_response(self, user_input: str, session_id: str, model: str, parent_ctx=None)
```

### Propuesta

Crear `fast_langchain_server/context.py`:

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Awaitable

@dataclass
class AgentContext:
    session_id: str
    request_id: str
    user_input: str
    model: str
    headers: dict[str, str]
    otel_context: Any = None                          # W3C trace context
    _emit: Optional[Callable[..., Awaitable[None]]] = field(default=None, repr=False)
    _metadata: dict[str, Any] = field(default_factory=dict)

    async def emit_progress(self, action: str, target: str) -> None:
        """Emite un evento de progreso al cliente SSE (no-op en modo no-streaming)."""
        if self._emit:
            await self._emit({"type": "progress", "action": action, "target": target})

    def set_meta(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self._metadata.get(key, default)
```

Con esto:
- `_run_agent(ctx: AgentContext)` — firma limpia
- `_stream_response(ctx: AgentContext)` — el `emit_progress` reemplaza el yield manual
- Los middlewares pueden enriquecer el context antes de pasarlo al agente

---

## 2. Lifespan

### Cómo lo hace FastMCP

FastMCP implementa lifespans como objetos composables con el operador `|`.
Cada lifespan es un async generator que yielda un dict de contexto.
Al componer, los dicts se mergean y el teardown ocurre en orden inverso (LIFO).

```python
# FastMCP — lifespan.py

class Lifespan:
    def __or__(self, other) -> "ComposedLifespan":
        return ComposedLifespan(self, other)

class ComposedLifespan(Lifespan):
    async def __call__(self, server):
        async with self.left._as_cm(server) as lctx:
            async with self.right._as_cm(server) as rctx:
                yield {**lctx, **rctx}   # merge; right wins en colisión

@lifespan
async def db_lifespan(server):
    db = await connect_db()
    yield {"db": db}          # disponible en server.lifespan_context["db"]
    await db.close()          # garantizado incluso ante cancelación

mcp = FastMCP("server", lifespan=db_lifespan | cache_lifespan)
```

### Estado actual en fast-langchain-server

Un único método monolítico `_lifespan` mezcla 4 responsabilidades:
1. Inicializar OTel
2. Loggear startup
3. Lanzar autonomous loop
4. Shutdown de task manager y memory

### Propuesta

Crear `fast_langchain_server/lifespan.py`:

```python
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable, Any

class Lifespan:
    def __init__(self, fn: Callable):
        self._fn = fn

    def __or__(self, other: "Lifespan") -> "ComposedLifespan":
        return ComposedLifespan(self, other)

    @asynccontextmanager
    async def _as_cm(self, server):
        gen = self._fn(server)
        ctx = await gen.__anext__()
        try:
            yield ctx
        finally:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass

def lifespan(fn: Callable) -> Lifespan:
    """Decorator que convierte un async generator en un Lifespan composable."""
    return Lifespan(fn)
```

Descomponiendo `_lifespan` en piezas:

```python
# fast_langchain_server/server.py

@lifespan
async def _otel_lifespan(server: "AgentServer"):
    if server._settings.otel_active:
        init_otel(server._settings.agent_name)
    yield {}

@lifespan
async def _log_lifespan(server: "AgentServer"):
    logger.info("Agent '%s' starting on port %d ...", ...)
    yield {}
    logger.info("Agent '%s' shutting down", server._settings.agent_name)

@lifespan
async def _autonomous_lifespan(server: "AgentServer"):
    if server._settings.autonomous_goal and server._has_task_manager():
        await server._task_manager.submit_autonomous(goal=server._settings.autonomous_goal, ...)
    yield {}

@lifespan
async def _shutdown_lifespan(server: "AgentServer"):
    yield {}
    await server._task_manager.shutdown()
    await server._memory.close()

# En AgentServer.__init__:
_server_lifespan = _otel_lifespan | _log_lifespan | _autonomous_lifespan | _shutdown_lifespan
```

**Beneficio:** cada lifespan es testeable en aislamiento, y terceros pueden agregar sus propios
lifespans sin modificar `AgentServer`:

```python
@lifespan
async def my_db_lifespan(server):
    db = await connect()
    server.lifespan_context["db"] = db
    yield {"db": db}
    await db.close()

server = create_agent_server(lifespan=my_db_lifespan)
```

---

## 3. Middleware

### Cómo lo hace FastMCP

FastMCP implementa una cadena de middlewares con dispatch jerárquico en 3 niveles:

```
Request
  → Middleware A: on_message
    → Middleware A: on_request
      → Middleware A: on_call_tool   ← nivel más específico
        → Handler
      ← Middleware A: on_call_tool
    ← Middleware A: on_request
  ← Middleware A: on_message
Response
```

```python
# FastMCP — middleware.py

class Middleware:
    async def on_message(self, ctx: MiddlewareContext, call_next: CallNext): 
        return await call_next(ctx)
    async def on_request(self, ctx, call_next): 
        return await call_next(ctx)
    async def on_call_tool(self, ctx, call_next): 
        return await call_next(ctx)
    # on_read_resource, on_get_prompt, on_list_*, etc.

# Para denegar: raise ToolError("forbidden"), NO retornar None
```

### Estado actual en fast-langchain-server

No existe ningún sistema de middleware. La lógica de OTel, logging y errores está
hardcodeada dentro de `_run_agent` y `_stream_response`.

### Propuesta

Crear `fast_langchain_server/middleware.py`:

```python
from abc import ABC
from typing import Callable, Awaitable, Any
from fast_langchain_server.context import AgentContext

CallNext = Callable[[AgentContext], Awaitable[Any]]

class AgentMiddleware(ABC):
    """Base para middleware del ciclo de vida del agente."""

    async def on_request(self, ctx: AgentContext, call_next: CallNext) -> Any:
        """Intercepta todo request de chat antes de ejecutar el agente."""
        return await call_next(ctx)

    async def on_agent_run(self, ctx: AgentContext, call_next: CallNext) -> Any:
        """Intercepta la invocación del agente (antes y después)."""
        return await call_next(ctx)

    async def on_stream_chunk(self, chunk: str, ctx: AgentContext, call_next: CallNext) -> str:
        """Intercepta cada chunk SSE antes de enviarlo al cliente."""
        return await call_next(chunk)
```

#### Middlewares built-in incluidos

```python
class TimingMiddleware(AgentMiddleware):
    """Loggea duración de cada request."""
    async def on_request(self, ctx, call_next):
        start = time.monotonic()
        result = await call_next(ctx)
        logger.info("session=%s elapsed=%.3fs", ctx.session_id, time.monotonic() - start)
        return result

class RateLimitMiddleware(AgentMiddleware):
    """Token bucket por session_id o client_id."""
    def __init__(self, max_rpm: int):
        self._buckets: dict[str, TokenBucket] = {}
        self._max_rpm = max_rpm

    async def on_request(self, ctx, call_next):
        bucket = self._buckets.setdefault(ctx.session_id, TokenBucket(self._max_rpm))
        if not bucket.acquire():
            raise HTTPException(429, "Rate limit exceeded")
        return await call_next(ctx)

class ResponseSizeLimitMiddleware(AgentMiddleware):
    """Trunca o rechaza respuestas que excedan un tamaño máximo."""
    def __init__(self, max_chars: int = 10_000):
        self._max = max_chars

    async def on_agent_run(self, ctx, call_next):
        result = await call_next(ctx)
        if len(result) > self._max:
            raise HTTPException(413, f"Response exceeds {self._max} chars")
        return result
```

#### Integración en `AgentServer`

```python
server = create_agent_server(tools=[...])
server.add_middleware(TimingMiddleware())
server.add_middleware(RateLimitMiddleware(max_rpm=60))
server.add_middleware(ResponseSizeLimitMiddleware(max_chars=8000))
```

---

## 4. Authentication

### Cómo lo hace FastMCP

FastMCP define una jerarquía de `AuthProvider` con responsabilidades separadas:

| Clase | Responsabilidad |
|---|---|
| `JWTVerifier` | Solo valida tokens JWT contra JWKS externo |
| `RemoteAuthProvider` | Delega a un IdP con Dynamic Client Registration |
| `OAuthProxy` | Puente para providers sin DCR (GitHub, Google) |
| `OAuthProvider` | Servidor OAuth completo (air-gapped) |
| `MultiAuth` | Encadena múltiples providers en orden |

```python
# FastMCP — auth.py (simplificado)

class AuthProvider(ABC):
    @abstractmethod
    async def verify_token(self, token: str) -> AccessToken | None:
        """None = inválido. AccessToken = válido con claims."""

class MultiAuth(AuthProvider):
    async def verify_token(self, token: str) -> AccessToken | None:
        for verifier in self.verifiers:
            if result := await verifier.verify_token(token):
                return result
        return None
```

El `AuthProvider` se configura en el servidor y Starlette lo aplica como middleware
automáticamente en todos los endpoints HTTP.

### Propuesta

Crear `fast_langchain_server/auth.py`:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class AuthToken:
    subject: str            # Identificador del usuario/servicio
    scopes: list[str]       # Permisos del token
    claims: dict            # Claims completos (JWT payload u otros)
    raw: str                # Token original (para auditoría)

class AuthProvider(ABC):
    @abstractmethod
    async def verify_token(self, token: str) -> Optional[AuthToken]:
        """Retorna None si el token es inválido o ha expirado."""

    def __or__(self, other: "AuthProvider") -> "MultiAuth":
        return MultiAuth(self, other)


class APIKeyProvider(AuthProvider):
    """Validación simple contra un dict de API keys conocidas."""
    def __init__(self, keys: dict[str, str]):
        # {"sk-abc123": "service-name"}
        self._keys = keys

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        if owner := self._keys.get(token):
            return AuthToken(subject=owner, scopes=["*"], claims={}, raw=token)
        return None


class EnvAPIKeyProvider(AuthProvider):
    """Lee API keys de una variable de entorno (comma-separated)."""
    def __init__(self, env_var: str = "AGENT_API_KEYS"):
        import os
        raw = os.getenv(env_var, "")
        self._keys = {k.strip() for k in raw.split(",") if k.strip()}

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        if token in self._keys:
            return AuthToken(subject="env-key", scopes=["*"], claims={}, raw=token)
        return None


class JWTProvider(AuthProvider):
    """Valida JWT Bearer tokens contra un JWKS público."""
    def __init__(self, jwks_url: str, audience: str, issuer: Optional[str] = None):
        self._jwks_url = jwks_url
        self._audience = audience
        self._issuer = issuer

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        # Implementar con PyJWT + httpx para fetching del JWKS
        ...


class MultiAuth(AuthProvider):
    """Prueba múltiples providers en orden; el primero que acepta gana."""
    def __init__(self, *providers: AuthProvider):
        self._providers = providers

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        for provider in self._providers:
            if result := await provider.verify_token(token):
                return result
        return None
```

#### Integración como middleware

```python
class AuthMiddleware(AgentMiddleware):
    def __init__(self, provider: AuthProvider, exclude: list[str] = None):
        self._provider = provider
        self._exclude = set(exclude or ["/health", "/ready", "/.well-known/agent.json"])

    async def on_request(self, ctx: AgentContext, call_next: CallNext):
        # Endpoints excluidos pasan sin autenticación
        if ctx.get_meta("endpoint") in self._exclude:
            return await call_next(ctx)

        raw = ctx.headers.get("authorization", "")
        token_str = raw.removeprefix("Bearer ").strip()
        
        token = await self._provider.verify_token(token_str)
        if not token:
            raise HTTPException(401, "Invalid or missing authentication token")

        ctx.set_meta("auth_token", token)   # Disponible en el resto de la cadena
        return await call_next(ctx)
```

#### Uso

```python
# API keys desde env var (AGENT_API_KEYS=sk-abc,sk-xyz)
server = create_agent_server(
    auth=EnvAPIKeyProvider(),
    tools=[...]
)

# JWT + fallback a API key
server = create_agent_server(
    auth=JWTProvider(jwks_url="https://auth.example.com/.well-known/jwks.json",
                     audience="my-agent") | EnvAPIKeyProvider(),
)
```

---

## 5. Authorization

### Cómo lo hace FastMCP

FastMCP separa **autenticación** (¿quién eres?) de **autorización** (¿qué puedes hacer?).
Usa callables que reciben un `AuthContext` y retornan `bool`:

```python
# FastMCP — authorization.py

@dataclass
class AuthContext:
    token: AccessToken | None
    component: FastMCPComponent    # La herramienta/recurso que se está accediendo

AuthCheck = Callable[[AuthContext], bool]  # también acepta async

def require_scopes(*scopes: str) -> AuthCheck:
    def check(ctx: AuthContext) -> bool:
        if not ctx.token: return False
        return all(s in ctx.token.scopes for s in scopes)
    return check

def restrict_tag(tag: str, *, scopes: list[str]) -> AuthCheck:
    """Solo aplica el check si el componente tiene el tag dado."""
    def check(ctx: AuthContext) -> bool:
        if tag not in ctx.component.tags: return True   # no aplica → permitir
        return require_scopes(*scopes)(ctx)
    return check

# Por componente:
@mcp.tool(auth=require_scopes("admin"))
async def delete_user(user_id: str): ...
```

### Propuesta

Crear `fast_langchain_server/authorization.py`:

```python
from dataclasses import dataclass
from typing import Callable, Optional, Awaitable, Union
from fast_langchain_server.auth import AuthToken

@dataclass
class AuthContext:
    token: Optional[AuthToken]
    endpoint: str               # "/v1/chat/completions", "/memory/sessions", etc.
    session_id: Optional[str]
    method: str                 # "GET", "POST", "DELETE"

AuthCheck = Union[
    Callable[[AuthContext], bool],
    Callable[[AuthContext], Awaitable[bool]],
]

async def run_auth_check(check: AuthCheck, ctx: AuthContext) -> bool:
    """Ejecuta sync o async AuthCheck de forma uniforme."""
    import asyncio, inspect
    result = check(ctx)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


# ── Built-in checks ───────────────────────────────────────────────────────────

def require_scopes(*scopes: str) -> AuthCheck:
    """Todos los scopes indicados deben estar presentes en el token."""
    def check(ctx: AuthContext) -> bool:
        if not ctx.token: return False
        return all(s in ctx.token.scopes for s in scopes)
    return check

def allow_any_authenticated() -> AuthCheck:
    """Cualquier token válido es suficiente."""
    def check(ctx: AuthContext) -> bool:
        return ctx.token is not None
    return check

def allow_own_session() -> AuthCheck:
    """El subject del token debe ser el propietario de la sesión."""
    def check(ctx: AuthContext) -> bool:
        if not ctx.token: return False
        if ctx.session_id is None: return True
        return ctx.session_id.startswith(ctx.token.subject)
    return check

def deny_all() -> AuthCheck:
    """Bloquea todos los accesos (útil para endpoints en mantenimiento)."""
    return lambda _: False
```

#### Middleware de autorización

```python
class AuthorizationMiddleware(AgentMiddleware):
    """Aplica reglas de autorización por endpoint."""
    
    def __init__(self, rules: dict[str, AuthCheck]):
        # {"/v1/chat/completions": require_scopes("chat"), ...}
        self._rules = rules

    async def on_request(self, ctx: AgentContext, call_next: CallNext):
        endpoint = ctx.get_meta("endpoint", "")
        check = self._rules.get(endpoint)
        
        if check is not None:
            auth_ctx = AuthContext(
                token=ctx.get_meta("auth_token"),
                endpoint=endpoint,
                session_id=ctx.session_id,
                method=ctx.get_meta("method", "POST"),
            )
            if not await run_auth_check(check, auth_ctx):
                raise HTTPException(403, "Insufficient permissions")
        
        return await call_next(ctx)
```

#### Uso combinado

```python
server = create_agent_server(tools=[...])

# Autenticación
server.add_middleware(AuthMiddleware(
    provider=JWTProvider(...) | EnvAPIKeyProvider(),
    exclude=["/health", "/ready", "/.well-known/agent.json"],
))

# Autorización
server.add_middleware(AuthorizationMiddleware({
    "/v1/chat/completions":       require_scopes("chat"),
    "/memory/sessions":           require_scopes("admin"),
    "DELETE /memory/sessions/*":  require_scopes("admin"),
}))
```

---

## 6. Arquitectura integrada

### Flujo de un request con todo implementado

```
HTTP Request
     │
     ▼
 [FastAPI router]
     │  builds AgentContext(session_id, request_id, user_input, headers, ...)
     ▼
 [AuthMiddleware]             → verifica token con AuthProvider
     │  ctx.set_meta("auth_token", token)
     ▼
 [AuthorizationMiddleware]    → chequea scopes según endpoint
     │
     ▼
 [RateLimitMiddleware]        → token bucket por session_id
     │
     ▼
 [TimingMiddleware]           → inicia timer
     │
     ▼
 [_run_agent(ctx) / _stream_response(ctx)]
     │  ctx.emit_progress("tool_call", "tool_name")
     ▼
 [LangGraph agent.ainvoke / astream]
     │
     ▼
 [TimingMiddleware]           → loggea elapsed time
     │
     ▼
 HTTP Response
```

### Lifespans compuestos

```
server start
  ├── _otel_lifespan:       init_otel()
  ├── _log_lifespan:        log startup info
  ├── _autonomous_lifespan: submit_autonomous() si configurado
  └── _shutdown_lifespan:   (no setup; teardown cierra memory + task_manager)
server stop (LIFO)
  ├── _shutdown_lifespan:   task_manager.shutdown() + memory.close()
  ├── _autonomous_lifespan: (cleanup si aplica)
  ├── _log_lifespan:        log shutdown
  └── _otel_lifespan:       (flush OTel si aplica)
```

### Estructura de archivos propuesta

```
fast_langchain_server/
├── __init__.py
├── server.py           ← AgentServer + factory (refactor: usa AgentContext)
├── context.py          ← AgentContext dataclass               [NUEVO]
├── lifespan.py         ← Lifespan, ComposedLifespan, @lifespan [NUEVO]
├── middleware.py       ← AgentMiddleware base + built-ins      [NUEVO]
├── auth.py             ← AuthToken, AuthProvider, providers    [NUEVO]
├── authorization.py    ← AuthContext, AuthCheck, built-ins     [NUEVO]
├── memory.py           (sin cambios)
├── a2a.py              (sin cambios)
├── serverutils.py      (mínimos cambios: agregar auth settings)
├── telemetry.py        (sin cambios)
└── cli.py              (sin cambios)
```

---

## 7. Prioridad de implementación

| # | Feature | Archivo nuevo | Esfuerzo | Impacto | Prioridad |
|---|---|---|---|---|---|
| 1 | `AgentContext` dataclass + refactor `_run_agent`/`_stream_response` | `context.py` | Bajo | Alto — limpia toda la capa de ejecución | **Alta** |
| 2 | `AuthProvider` + `APIKeyProvider` + `EnvAPIKeyProvider` | `auth.py` | Bajo | Alto — necesidad real en producción | **Alta** |
| 3 | `AuthMiddleware` (usa AuthProvider) | `middleware.py` | Bajo | Alto | **Alta** |
| 4 | `AgentMiddleware` base + cadena de middlewares en `AgentServer` | `middleware.py` | Medio | Alto | **Media** |
| 5 | Composable lifespans | `lifespan.py` | Medio | Medio — mejora testabilidad | **Media** |
| 6 | `AuthorizationMiddleware` + `AuthCheck` built-ins | `authorization.py` | Bajo | Medio | **Media** |
| 7 | `JWTProvider` (JWKS validation) | `auth.py` | Alto | Alto para enterprise | **Baja** |
| 8 | `TimingMiddleware`, `RateLimitMiddleware` built-ins | `middleware.py` | Medio | Medio | **Baja** |

### Orden de implementación recomendado

```
Fase 1 (MVP auth):
  context.py → refactor server.py → auth.py → AuthMiddleware en middleware.py

Fase 2 (Middleware system):
  middleware.py base → add_middleware() en AgentServer → built-in middlewares

Fase 3 (Lifespans + Authorization):
  lifespan.py → authorization.py → JWTProvider
```

---

*Basado en análisis de [fastmcp Python source](https://github.com/jlowin/fastmcp) y la
documentación en [gofastmcp.com](https://gofastmcp.com/servers/context).*
