"""
Session message history backends for the LangChain Agent Server.

Unlike LangGraph's built-in checkpointer (which is injected at compile time),
these stores are managed *outside* the graph so the same compiled agent can be
used across many concurrent sessions without coupling memory to graph internals.

Backends
--------
LocalMemory  – in-process dict (default, great for single-replica deployments)
RedisMemory  – Redis HASH + sorted-set index (works across replicas)
NullMemory   – no-op (for stateless / unit-test scenarios)

All backends serialise LangChain messages with the official
``message_to_dict`` / ``messages_from_dict`` helpers so tool-call content,
multi-modal blocks, etc. are round-tripped correctly.
"""
from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Memory(ABC):
    """Async message-history store interface."""

    @abstractmethod
    async def get_or_create_session(self, session_id: Optional[str] = None) -> str:
        """Return *session_id* (creating the session if it does not exist).

        If *session_id* is ``None`` a new UUID is generated.
        """

    @abstractmethod
    async def get_messages(
        self, session_id: str, context_limit: int = 0
    ) -> list[BaseMessage]:
        """Return the last *context_limit* messages for *session_id*.

        When *context_limit* is 0 the full history is returned.
        The list is guaranteed **not** to start with a ``ToolMessage`` or an
        ``AIMessageChunk`` so that LangGraph never receives a dangling tool
        response at the head of the conversation.
        """

    @abstractmethod
    async def save_messages(
        self, session_id: str, messages: list[BaseMessage]
    ) -> None:
        """Persist *messages* as the complete current history for *session_id*.

        Callers pass the full message list (``input_messages + new_messages``)
        returned by LangGraph.  The backend may trim old messages to stay
        within its configured per-session limit.
        """

    @abstractmethod
    async def list_sessions(self) -> list[str]:
        """Return all known session IDs."""

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        """Remove *session_id* from the store. Returns ``True`` if it existed."""

    @abstractmethod
    async def close(self) -> None:
        """Release any underlying connections."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_trim(raw: list[dict], context_limit: int) -> list[dict]:
    """Apply *context_limit* and remove leading tool / chunk messages."""
    if context_limit and len(raw) > context_limit:
        raw = raw[-context_limit:]
    # Never start with a ToolMessage or a dangling AIMessageChunk
    while raw and raw[0].get("type") in ("tool", "AIMessageChunk"):
        raw = raw[1:]
    return raw


# ---------------------------------------------------------------------------
# LocalMemory
# ---------------------------------------------------------------------------

class LocalMemory(Memory):
    """In-process dictionary backed session store.

    Automatically evicts the **oldest** session when *max_sessions* is reached
    and trims per-session message lists to *max_messages_per_session*.
    """

    def __init__(
        self,
        max_sessions: int = 1000,
        max_messages_per_session: int = 500,
    ) -> None:
        self._max_sessions = max_sessions
        self._max_msgs = max_messages_per_session
        # session_id → list[dict]  (serialised LangChain messages)
        self._store: dict[str, list[dict]] = {}
        # session_id → last-accessed datetime (for LRU eviction)
        self._touched: dict[str, datetime] = {}

    # ── internal ──────────────────────────────────────────────────────────────

    def _evict_if_needed(self) -> None:
        if len(self._store) < self._max_sessions:
            return
        oldest = min(self._touched, key=lambda k: self._touched[k])
        del self._store[oldest]
        del self._touched[oldest]

    def _touch(self, session_id: str) -> None:
        self._touched[session_id] = datetime.now(timezone.utc)

    # ── Memory interface ──────────────────────────────────────────────────────

    async def get_or_create_session(self, session_id: Optional[str] = None) -> str:
        if not session_id:
            session_id = str(uuid.uuid4())
        if session_id not in self._store:
            self._evict_if_needed()
            self._store[session_id] = []
        self._touch(session_id)
        return session_id

    async def get_messages(
        self, session_id: str, context_limit: int = 0
    ) -> list[BaseMessage]:
        raw = list(self._store.get(session_id, []))
        raw = _safe_trim(raw, context_limit)
        return messages_from_dict(raw) if raw else []

    async def save_messages(
        self, session_id: str, messages: list[BaseMessage]
    ) -> None:
        serialised = [message_to_dict(m) for m in messages]
        # Trim oldest messages if the session exceeds the per-session cap
        if len(serialised) > self._max_msgs:
            serialised = serialised[-self._max_msgs :]
        self._store[session_id] = serialised
        self._touch(session_id)

    async def list_sessions(self) -> list[str]:
        return list(self._store.keys())

    async def delete_session(self, session_id: str) -> bool:
        if session_id in self._store:
            del self._store[session_id]
            self._touched.pop(session_id, None)
            return True
        return False

    async def close(self) -> None:
        pass  # nothing to close

    async def stats(self) -> dict[str, Any]:
        return {
            "sessions": len(self._store),
            "max_sessions": self._max_sessions,
        }


# ---------------------------------------------------------------------------
# RedisMemory
# ---------------------------------------------------------------------------

class RedisMemory(Memory):
    """Redis-backed session store — suitable for multi-replica deployments.

    Data layout
    ~~~~~~~~~~~
    ``fls:session:<id>``   – JSON-encoded ``list[dict]`` (message history)
    ``fls:sessions``       – Sorted set of session IDs scored by last-update
                             timestamp (Unix epoch, float).
    """

    _PREFIX = "fls"

    def __init__(
        self,
        redis_url: str,
        max_sessions: int = 1000,
        max_messages_per_session: int = 500,
    ) -> None:
        import redis.asyncio as aioredis  # lazy import — optional dependency

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._max_sessions = max_sessions
        self._max_msgs = max_messages_per_session

    def _session_key(self, session_id: str) -> str:
        return f"{self._PREFIX}:session:{session_id}"

    @property
    def _index_key(self) -> str:
        return f"{self._PREFIX}:sessions"

    def _now_ts(self) -> float:
        return datetime.now(timezone.utc).timestamp()

    async def _update_index(self, session_id: str) -> None:
        await self._redis.zadd(self._index_key, {session_id: self._now_ts()})
        # Evict sessions beyond the cap
        total = await self._redis.zcard(self._index_key)
        if total > self._max_sessions:
            overflow = total - self._max_sessions
            oldest_ids: list[str] = await self._redis.zrange(
                self._index_key, 0, overflow - 1
            )
            if oldest_ids:
                keys_to_del = [self._session_key(sid) for sid in oldest_ids]
                pipe = self._redis.pipeline()
                pipe.delete(*keys_to_del)
                pipe.zrem(self._index_key, *oldest_ids)
                await pipe.execute()

    # ── Memory interface ──────────────────────────────────────────────────────

    async def get_or_create_session(self, session_id: Optional[str] = None) -> str:
        if not session_id:
            session_id = str(uuid.uuid4())
        await self._update_index(session_id)
        return session_id

    async def get_messages(
        self, session_id: str, context_limit: int = 0
    ) -> list[BaseMessage]:
        data = await self._redis.get(self._session_key(session_id))
        if not data:
            return []
        raw: list[dict] = json.loads(data)
        raw = _safe_trim(raw, context_limit)
        return messages_from_dict(raw) if raw else []

    async def save_messages(
        self, session_id: str, messages: list[BaseMessage]
    ) -> None:
        serialised = [message_to_dict(m) for m in messages]
        if len(serialised) > self._max_msgs:
            serialised = serialised[-self._max_msgs :]
        pipe = self._redis.pipeline()
        pipe.set(self._session_key(session_id), json.dumps(serialised))
        pipe.zadd(self._index_key, {session_id: self._now_ts()})
        await pipe.execute()

    async def list_sessions(self) -> list[str]:
        return await self._redis.zrange(self._index_key, 0, -1)

    async def delete_session(self, session_id: str) -> bool:
        pipe = self._redis.pipeline()
        pipe.delete(self._session_key(session_id))
        pipe.zrem(self._index_key, session_id)
        results = await pipe.execute()
        return any(bool(r) for r in results)

    async def close(self) -> None:
        await self._redis.aclose()


# ---------------------------------------------------------------------------
# NullMemory
# ---------------------------------------------------------------------------

class NullMemory(Memory):
    """No-op memory backend — every conversation starts fresh.

    Useful for stateless deployments, integration tests, or agents that
    maintain their own state via tools.
    """

    async def get_or_create_session(self, session_id: Optional[str] = None) -> str:
        return session_id or str(uuid.uuid4())

    async def get_messages(
        self, session_id: str, context_limit: int = 0
    ) -> list[BaseMessage]:
        return []

    async def save_messages(
        self, session_id: str, messages: list[BaseMessage]
    ) -> None:
        pass

    async def list_sessions(self) -> list[str]:
        return []

    async def delete_session(self, session_id: str) -> bool:
        return True

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_memory(
    memory_type: str,
    redis_url: str = "",
    max_sessions: int = 1000,
    max_messages_per_session: int = 500,
) -> Memory:
    """Return the appropriate Memory backend based on *memory_type*."""
    if memory_type == "redis":
        if not redis_url:
            raise ValueError("MEMORY_REDIS_URL must be set when MEMORY_TYPE=redis")
        logger.info("Using RedisMemory at %s", redis_url)
        return RedisMemory(
            redis_url=redis_url,
            max_sessions=max_sessions,
            max_messages_per_session=max_messages_per_session,
        )
    if memory_type == "null":
        logger.info("Using NullMemory (stateless)")
        return NullMemory()
    logger.info("Using LocalMemory (max_sessions=%d)", max_sessions)
    return LocalMemory(
        max_sessions=max_sessions,
        max_messages_per_session=max_messages_per_session,
    )
