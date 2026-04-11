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

__all__ = ["serve", "create_agent_server"]
__version__ = "0.1.0"
