"""
CLI for fast-langchain-server.

Commands
--------
fast-langchain-server run [target] [options]   – start an agent server
fast-langchain-server version                  – print package version

Target format
-------------
  agent.py              auto-discovers a CompiledStateGraph instance
  agent.py:my_agent     explicit attribute name
  agent:my_agent        module:attribute (module on PYTHONPATH)

Discovery order (when no attribute is specified)
-------------------------------------------------
1. Any attribute annotated / subclassed as CompiledStateGraph
2. Any attribute named ``agent``
3. Any attribute named ``app`` that is a FastAPI instance (served as-is)
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="fast-langchain-server",
    help="fast-langchain-server — run LangChain/LangGraph agents as HTTP services",
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_module(target: str):
    """Import a module from a file path or dotted module name."""
    if ":" in target:
        module_part, attr = target.rsplit(":", 1)
    else:
        module_part, attr = target, None

    path = Path(module_part)
    if path.exists():
        # Load from file path
        spec = importlib.util.spec_from_file_location("_agent_module", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    else:
        # Try as dotted module name
        mod = importlib.import_module(module_part)

    return mod, attr


def _discover_agent(mod, explicit_attr: Optional[str]):
    """Find the agent/app object inside *mod*."""
    # Try to import lazily to avoid hard dependency at CLI startup
    try:
        from langgraph.graph.state import CompiledStateGraph
        graph_type = CompiledStateGraph
    except ImportError:
        graph_type = None  # type: ignore[assignment]

    if explicit_attr:
        obj = getattr(mod, explicit_attr, None)
        if obj is None:
            raise typer.BadParameter(
                f"Attribute '{explicit_attr}' not found in module"
            )
        return obj

    candidates = []
    for name, obj in inspect.getmembers(mod):
        if name.startswith("_"):
            continue
        if graph_type and isinstance(obj, graph_type):
            candidates.append((name, obj))

    if len(candidates) == 1:
        return candidates[0][1]
    if len(candidates) > 1:
        names = [c[0] for c in candidates]
        raise typer.BadParameter(
            f"Multiple agents found: {names}. "
            "Specify one explicitly: fast-langchain-server run agent.py:<name>"
        )

    # Fallback: look for a FastAPI app attribute named 'app'
    from fastapi import FastAPI
    fastapi_app = getattr(mod, "app", None)
    if isinstance(fastapi_app, FastAPI):
        return fastapi_app

    # Last resort: attribute named 'agent'
    agent = getattr(mod, "agent", None)
    if agent is not None:
        return agent

    raise typer.BadParameter(
        "No agent found in module. "
        "Make sure you have a CompiledStateGraph attribute or "
        "use the explicit form: fast-langchain-server run agent.py:my_agent"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    target: str = typer.Argument(
        "agent.py",
        help="Python file (or file:attr / module:attr) containing the agent",
    ),
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind host"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Bind port (overrides AGENT_PORT)"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Auto-reload on file changes"),
) -> None:
    """Start an agent HTTP server.

    Examples:\n
      fast-langchain-server run\n
      fast-langchain-server run agent.py\n
      fast-langchain-server run agent.py:my_agent --port 9000 --reload
    """
    import uvicorn

    mod, attr = _load_module(target)
    obj = _discover_agent(mod, attr)

    # obj can be a CompiledStateGraph or an already-built FastAPI app
    from fastapi import FastAPI

    if isinstance(obj, FastAPI):
        asgi_app = obj
        effective_port = port or 8000
    else:
        # Wrap the agent with our server
        from fast_langchain_server.server import create_agent_server
        from fast_langchain_server.serverutils import AgentServerSettings

        settings = AgentServerSettings()  # type: ignore[call-arg]
        if port:
            settings = settings.model_copy(update={"agent_port": port})

        server = create_agent_server(settings=settings, custom_agent=obj)
        asgi_app = server.app
        effective_port = settings.agent_port

    typer.echo(f"Starting agent server on {host}:{effective_port}")
    uvicorn.run(
        asgi_app,
        host=host,
        port=effective_port,
        reload=reload,
        log_level="info",
    )


@app.command()
def version() -> None:
    """Print the fast-langchain-server version."""
    from fast_langchain_server import __version__
    typer.echo(f"fast-langchain-server {__version__}")


if __name__ == "__main__":
    app()
