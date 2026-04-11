# Fast LangChain Server

> 🚀 Production HTTP server for LangChain/LangGraph agents with OpenAI-compatible API, streaming, session memory, and agent discovery.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/badge/version-0.1.0-brightgreen)](https://pypi.org/project/fast-langchain-server/)

## Overview

**Fast LangChain Server** transforms your LangChain/LangGraph agents into production-ready HTTP services. Deploy agents with a single line of code, get streaming responses, automatic session management, and built-in observability.

Perfect for building:
- 🤖 Agent APIs for client applications
- 🔗 Multi-agent orchestration backends
- 📊 LLM-powered microservices
- 🎯 RAG systems with tool calling

## Features

- **OpenAI-Compatible API** — Drop-in compatible with OpenAI clients and third-party tools
- **Streaming Responses** — Real-time token streaming with Server-Sent Events (SSE)
- **Session Memory** — Automatic conversation history management with optional Redis persistence
- **Agent Discovery** — Built-in card endpoint to inspect agent capabilities and tools
- **Tool Calling** — Native LangChain tool integration with automatic marshaling
- **Production Ready** — Health checks, structured logging, request tracing, OpenTelemetry support
- **Type Safe** — Pydantic validation on all endpoints
- **Multiple Patterns** — Works with `create_agent()` or custom `CompiledStateGraph`

## Quick Start

### Installation

```bash
pip install fast-langchain-server
```

Or with development dependencies:

```bash
git clone https://github.com/yourusername/fast-langchain-server.git
cd fast-langchain-server
make dev
```

### Minimal Example

Create an `agent.py`:

```python
from langchain.agents import create_agent
from langchain_core.tools import tool
from fast_langchain_server import serve

@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b

@tool
def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"

# Create your agent
agent = create_agent(
    model="openai:gpt-4o",
    tools=[add, greet],
    system_prompt="You are a helpful assistant."
)

# Wrap it as an HTTP service
app = serve(agent, tools=[add, greet])
```

Run it:

```bash
AGENT_NAME=my-agent \
  MODEL_API_URL=https://api.openai.com/v1 \
  MODEL_NAME=gpt-4o \
  OPENAI_API_KEY=sk-... \
  fast-langchain-server run agent.py
```

Visit `http://localhost:8000/health` to verify it's running.

## Usage Patterns

### Pattern A: Using `create_agent`

The modern LangChain 1.x API. Uses LangGraph under the hood:

```python
from langchain.agents import create_agent
from fast_langchain_server import serve

agent = create_agent(
    model="openai:gpt-4o",
    tools=TOOLS,
    system_prompt="Your system prompt here"
)

app = serve(agent, tools=TOOLS)
```

### Pattern B: Custom LangGraph Graph

For more control, bring your own `CompiledStateGraph`:

```python
from langgraph.prebuilt import create_react_agent
from fast_langchain_server import serve

graph = create_react_agent(model, tools=TOOLS)
app = serve(graph, tools=TOOLS)
```

## API Endpoints

### POST `/invoke`

Invoke the agent synchronously.

```bash
curl -X POST http://localhost:8000/invoke \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is 2+2?"}]}'
```

**Response:**
```json
{
  "output": "4",
  "session_id": "sess_abc123...",
  "messages": [...]
}
```

### POST `/stream`

Invoke the agent with streaming (SSE).

```bash
curl -N http://localhost:8000/stream \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Tell me a joke"}]}'
```

Streams delimited JSON events in real-time.

### GET `/card`

Inspect the agent's configuration and available tools.

```bash
curl http://localhost:8000/card
```

**Response:**
```json
{
  "name": "my-agent",
  "description": "A helpful assistant",
  "tools": [
    {
      "name": "add",
      "description": "Add two numbers",
      "input_schema": {...}
    }
  ]
}
```

### GET `/health`

Health check endpoint.

```bash
curl http://localhost:8000/health
```

## Configuration

Set via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_NAME` | ✓ | — | Identifier for your agent |
| `MODEL_API_URL` | ✓ | — | Base URL for LLM API (e.g., OpenAI, Ollama) |
| `MODEL_NAME` | ✓ | — | Model identifier (e.g., `gpt-4o`, `llama3.2`) |
| `OPENAI_API_KEY` | For OpenAI | — | API key (or use model-specific auth) |
| `AGENT_TIMEOUT` | — | `300` | Request timeout in seconds |
| `REDIS_URL` | For persistence | — | Redis connection URL for session memory |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | For tracing | — | OpenTelemetry collector endpoint |

## Docker

### Build

```bash
make docker-build
```

Or manually:

```bash
docker build -t langchain-agent-server .
```

### Run

```bash
docker run -p 8000:8000 \
  -e AGENT_NAME=my-agent \
  -e MODEL_API_URL=https://api.openai.com/v1 \
  -e MODEL_NAME=gpt-4o \
  -e OPENAI_API_KEY=sk-... \
  -v $(pwd)/agent.py:/app/agent.py \
  langchain-agent-server
```

## Development

### Install dev dependencies

```bash
make dev
```

### Run tests

```bash
make test
```

### Lint

```bash
make lint
```

### Run locally with reload

```bash
make run
```

(Edit `agent.py` and it will reload automatically)

### Clean build artifacts

```bash
make clean
```

## Architecture

```
┌─────────────────────┐
│  Client / LLM App   │
└──────────┬──────────┘
           │
      HTTP │ OpenAI-compatible
           │
┌──────────▼──────────────────────┐
│  FastAPI Server                 │
│  ├─ /invoke                     │
│  ├─ /stream                     │
│  ├─ /card                       │
│  └─ /health                     │
└──────────┬──────────────────────┘
           │
    ┌──────┴──────┐
    │             │
┌───▼────┐   ┌───▼──────────┐
│ Agent  │   │ Session Mem  │
│ (User) │   │ (Optional    │
│        │   │  Redis)      │
└────────┘   └──────────────┘
```

## Performance

- **~100ms** latency for simple tool calls (OpenAI API)
- **Streaming** reduces perceived latency by delivering tokens in real-time
- **Async I/O** handles concurrent requests efficiently
- **Redis caching** of session memory reduces round-trips

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Roadmap

- [ ] Batching API for multiple agent invocations
- [ ] Custom session backends (PostgreSQL, DynamoDB)
- [ ] Cost tracking and rate limiting per agent
- [ ] Agent versioning and canary deployments
- [ ] Built-in monitoring dashboard
- [ ] WebSocket support for real-time bidirectional communication

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## Support

- 📖 [Documentation](https://github.com/yourusername/fast-langchain-server/wiki)
- 🐛 [Issues](https://github.com/yourusername/fast-langchain-server/issues)
- 💬 [Discussions](https://github.com/yourusername/fast-langchain-server/discussions)

---

**Made with ❤️ for the AI community**