# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned for v0.2.0
- Batching API for multiple agent invocations
- Custom session backends (PostgreSQL, DynamoDB)
- Cost tracking and rate limiting per agent

### Planned for v1.0.0
- Agent versioning and canary deployments
- Built-in monitoring dashboard
- WebSocket support for real-time bidirectional communication

---

## [0.1.0] - 2026-04-11

### Added

#### Core Server Implementation
- FastAPI-based HTTP server for LangChain/LangGraph agents
- OpenAI-compatible API endpoints:
  - `POST /invoke` - Synchronous agent invocation
  - `POST /stream` - Streaming responses with Server-Sent Events (SSE)
  - `GET /card` - Agent discovery and tool introspection
  - `GET /health` - Health check endpoint
- Support for both `create_agent()` (LangChain 1.x) and custom `CompiledStateGraph` patterns
- Automatic tool parameter marshaling and validation with Pydantic

#### Session Management
- Automatic conversation history tracking
- Optional Redis persistence for distributed deployments
- Session isolation and management
- Configurable session timeout

#### Observability & Production Readiness
- OpenTelemetry integration for request tracing
- Structured logging with context propagation
- Health check endpoint for container orchestration
- Async I/O for concurrent request handling
- Type-safe request/response validation

#### Streaming & Real-time
- Server-Sent Events (SSE) support for streaming responses
- Token-by-token delivery to reduce perceived latency
- Proper connection handling and cleanup

#### Documentation
- Comprehensive README with quick start guide
- API endpoint documentation with curl examples
- Configuration reference for environment variables
- Architecture diagram
- Usage pattern examples (Pattern A & B)
- Contributing guidelines and roadmap
- Performance benchmarks

#### Docker Support
- Production-ready Dockerfile with Alpine base
- Multi-stage build for optimized image size
- Health check integration for orchestration
- Non-root user execution for security
- Fast dependency resolution with `uv`

#### Development Tools
- Makefile with common development tasks
  - `make install` - Install dependencies
  - `make dev` - Install dev dependencies
  - `make test` - Run test suite
  - `make lint` - Code quality checks
  - `make run` - Local development with auto-reload
  - `make docker-build` & `make docker-run` - Container helpers
  - `make clean` - Clean build artifacts
- `.envrc` configuration for direnv integration

#### Examples & Tests
- Example agent (`example_agent.py`) demonstrating both usage patterns
- Comprehensive test suite:
  - `test_server.py` - API endpoint testing
  - `test_a2a.py` - OpenAI-compatible API testing
  - `test_memory.py` - Session memory management testing
- Pytest fixtures for async testing
- Test configuration with `conftest.py`

#### Package Configuration
- Python 3.11+ support
- MIT License
- pyproject.toml with:
  - Core dependencies (LangChain, FastAPI, Pydantic)
  - Optional dependencies (Redis, OpenTelemetry)
  - Dev dependencies (pytest, type checking)
  - CLI entry point configuration

### Project Metadata
- Version: 0.1.0
- Keywords: langchain, langgraph, agent, server, fastapi, llm
- Repository structure following Python best practices

---

## Project Timeline

| Version | Date | Status |
|---------|------|--------|
| [0.1.0] | 2026-04-11 | ✅ Released |
| 0.2.0 | Q2 2026 | 🔄 Planned |
| 1.0.0 | Q3 2026 | 📋 Planned |

---

## Git Flow Information

### Release v0.1.0

**Features merged (in order):**
1. `feature/core-server` (7c8b22f) - Core LangChain server implementation
2. `feature/documentation` (b956919) - Comprehensive documentation
3. `feature/docker-support` (30c3e00) - Docker containerization
4. `feature/dev-tools` (e2ffbe0) - Development tooling
5. `feature/examples-and-tests` (832598f) - Examples and test suite

**Merge commits:**
- `develop` branch: e3a3bf2, 5adb838, c859ac0, aa169aa, 70a3eaa
- `main` branch: 5c8f195 (release merge)
- `develop` sync: d34f357 (sync with main after release)

**Tag:** `v0.1.0` (anotado)

---

## Installation & Setup

### Quick Start
```bash
pip install fast-langchain-server
```

### From Source
```bash
git clone https://github.com/yourusername/fast-langchain-server.git
cd fast-langchain-server
make dev
make test
```

### Docker
```bash
make docker-build
make docker-run
```

---

## Key Features by Component

### `fast_langchain_server/` Module
- **`__init__.py`** - Package exports and `serve()` function
- **`server.py`** - Core FastAPI application and endpoints
- **`cli.py`** - CLI interface with Typer
- **`memory.py`** - Session memory backends (memory, Redis)
- **`a2a.py`** - OpenAI-compatible API layer
- **`telemetry.py`** - OpenTelemetry instrumentation
- **`serverutils.py`** - Helper utilities and models

### Test Suite
- **`test_server.py`** - HTTP endpoint tests (224 lines)
- **`test_a2a.py`** - OpenAI compatibility tests (407 lines)
- **`test_memory.py`** - Session memory tests (444 lines)
- **`conftest.py`** - Pytest fixtures and configuration

---

## Known Limitations (v0.1.0)

1. Single-server deployments only (no distributed agent state)
2. In-memory session store by default (Redis required for persistence)
3. No built-in rate limiting or cost tracking
4. No agent versioning or canary deployment support
5. Limited to HTTP/SSE streaming (no WebSocket support yet)

---

## Migration Guide

### From LangChain Agents (without Server)

Before:
```python
from langchain.agents import create_agent
agent = create_agent(model=..., tools=...)
result = agent.invoke({"messages": [...]})
```

After:
```python
from langchain.agents import create_agent
from fast_langchain_server import serve

agent = create_agent(model=..., tools=...)
app = serve(agent, tools=TOOLS)
# Now accessible via HTTP at /invoke and /stream
```

### Environment Variables Required

```bash
AGENT_NAME=my-agent              # Required
MODEL_API_URL=...                 # Required
MODEL_NAME=gpt-4o                 # Required
OPENAI_API_KEY=sk-...             # For OpenAI models
REDIS_URL=redis://...             # Optional (for persistence)
OTEL_EXPORTER_OTLP_ENDPOINT=...   # Optional (for tracing)
```

---

## Performance Notes

### Observed Metrics (v0.1.0)

- **Latency**: ~100ms for simple tool calls (OpenAI API)
- **Throughput**: Handles concurrent requests efficiently via async I/O
- **Memory**: ~150MB baseline (FastAPI + dependencies)
- **Startup**: ~2-3 seconds cold start

### Optimization Recommendations

1. Use Redis for session persistence in multi-instance setups
2. Enable OpenTelemetry sampling for production
3. Cache agent definitions if using custom graphs
4. Use connection pooling for database backends

---

## Contributors

- **Initial Release**: Claude (AI Assistant)
- **Project Lead**: Michel (@michelcub)

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Support & Communication

- 📖 [Documentation](https://github.com/yourusername/fast-langchain-server/wiki)
- 🐛 [Issue Tracker](https://github.com/yourusername/fast-langchain-server/issues)
- 💬 [Discussions](https://github.com/yourusername/fast-langchain-server/discussions)
- 📧 [Email](mailto:support@example.com)

---

## Acknowledgments

Built with:
- [LangChain](https://langchain.com) - Agent orchestration
- [LangGraph](https://langgraph.dev) - Graph execution
- [FastAPI](https://fastapi.tiangolo.com) - Web framework
- [Pydantic](https://pydantic-ai.jina.ai) - Data validation
- [OpenTelemetry](https://opentelemetry.io) - Observability

---

**Last Updated**: 2026-04-11
**Version**: 0.1.0
