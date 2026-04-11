"""
Example agent — shows the two main usage patterns.

Pattern A: create_agent (new LangChain 1.x API, backed by LangGraph)
Pattern B: custom LangGraph StateGraph (any CompiledStateGraph works)

Run with:
    AGENT_NAME=example MODEL_API_URL=http://localhost:11434/v1 MODEL_NAME=llama3.2 \
        fast-langchain-server run example_agent.py
"""
from langchain.agents import create_agent
from langchain_core.tools import tool

from fast_langchain_server import serve

# ── Tools ─────────────────────────────────────────────────────────────────────


@tool
def add(a: float, b: float) -> float:
    "Add two numbers and return the result."
    return a + b


@tool
def greet(name: str) -> str:
    "Return a friendly greeting for the given name."
    return f"Hello, {name}! Nice to meet you."


TOOLS = [add, greet]

# ── Agent (Pattern A — langchain.agents.create_agent) ─────────────────────────
# create_agent is the new LangChain 1.x API.
# Under the hood it compiles a LangGraph CompiledStateGraph.
# It is fully compatible with any model that supports tool calling.

agent = create_agent(
    model="openai:gpt-4o",      # or: ChatOpenAI(base_url=..., model=...)
    tools=TOOLS,
    system_prompt="You are a helpful assistant. Use tools when appropriate.",
)

# ── ASGI app (for uvicorn / gunicorn) ─────────────────────────────────────────
# serve() wraps the agent as a FastAPI app.
# Session memory, streaming, health probes and the discovery card are all
# wired automatically.

app = serve(agent, tools=TOOLS)


# ── Pattern B — custom LangGraph graph ────────────────────────────────────────
# Any CompiledStateGraph that accepts {"messages": [...]} works.
#
# from langgraph.prebuilt import create_react_agent
# react_agent = create_react_agent(model, tools=TOOLS)
# app = serve(react_agent, tools=TOOLS)
