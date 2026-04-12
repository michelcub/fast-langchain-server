"""
CLI for fast-langchain-server.

Commands
--------
fast-langchain-server run [target] [options]   – start an agent server
fast-langchain-server version                  – print package version

Target format
-------------
  agent.py              auto-discovers a CompiledStateGraph or Server instance
  agent.py:my_agent     explicit attribute name
  agent:my_agent        module:attribute (module on PYTHONPATH)

Discovery order (when no attribute is specified)
-------------------------------------------------
1. Any attribute that is a Server instance
2. Any attribute annotated / subclassed as CompiledStateGraph
3. Any attribute named ``agent``
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
        spec = importlib.util.spec_from_file_location("_agent_module", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    else:
        mod = importlib.import_module(module_part)

    return mod, attr


def _discover_object(mod, explicit_attr: Optional[str]):
    """Find a Server or CompiledStateGraph inside *mod*."""
    from fast_langchain_server.server import Server

    if explicit_attr:
        obj = getattr(mod, explicit_attr, None)
        if obj is None:
            raise typer.BadParameter(
                f"Attribute '{explicit_attr}' not found in module"
            )
        return obj

    # 1. Prefer an already-constructed Server instance
    for name, obj in inspect.getmembers(mod):
        if not name.startswith("_") and isinstance(obj, Server):
            return obj

    # 2. Look for a CompiledStateGraph
    try:
        from langgraph.graph.state import CompiledStateGraph
        graph_type = CompiledStateGraph
    except ImportError:
        graph_type = None  # type: ignore[assignment]

    if graph_type:
        candidates = [
            (name, obj)
            for name, obj in inspect.getmembers(mod)
            if not name.startswith("_") and isinstance(obj, graph_type)
        ]
        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            names = [c[0] for c in candidates]
            raise typer.BadParameter(
                f"Multiple agents found: {names}. "
                "Specify one explicitly: fast-langchain-server run agent.py:<name>"
            )

    # 3. Attribute named 'agent' as last resort
    agent = getattr(mod, "agent", None)
    if agent is not None:
        return agent

    raise typer.BadParameter(
        "No agent or Server found in module. "
        "Make sure you have a Server instance or a CompiledStateGraph attribute, "
        "or use the explicit form: fast-langchain-server run agent.py:my_server"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    target: str = typer.Argument(
        "agent.py",
        help="Python file (or file:attr / module:attr) containing the agent or server",
    ),
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind host"),  # nosec B104 - intentional for containerized deployment
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Bind port (overrides AGENT_PORT)"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Auto-reload on file changes"),
) -> None:
    """Start an agent HTTP server.

    Examples:\n
      fast-langchain-server run\n
      fast-langchain-server run agent.py\n
      fast-langchain-server run agent.py:my_server --port 9000 --reload
    """
    from fast_langchain_server.server import Server

    mod, attr = _load_module(target)
    obj = _discover_object(mod, attr)

    if isinstance(obj, Server):
        obj.run(host=host, port=port, reload=reload)
    else:
        # Wrap a bare CompiledStateGraph
        from fast_langchain_server.serverutils import AgentServerSettings

        settings_kwargs: dict = {}
        if port:
            settings_kwargs["agent_port"] = port

        server = Server(obj, **settings_kwargs)
        typer.echo(f"Starting agent server on {host}:{server._settings.agent_port}")
        server.run(host=host, reload=reload)


@app.command()
def version() -> None:
    """Print the fast-langchain-server version."""
    from fast_langchain_server import __version__
    typer.echo(f"fast-langchain-server {__version__}")


if __name__ == "__main__":
    app()
