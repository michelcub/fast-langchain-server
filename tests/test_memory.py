"""
Tests for memory backends.

LocalMemory  — full coverage (session lifecycle, context trimming, eviction)
RedisMemory  — full coverage with an in-process aioredis mock (no real Redis needed)
NullMemory   — basic no-op contract
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from fast_langchain_server.memory import LocalMemory, NullMemory, RedisMemory, create_memory, _safe_trim


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human(text: str) -> HumanMessage:
    return HumanMessage(content=text)


def _ai(text: str) -> AIMessage:
    return AIMessage(content=text)


def _tool(text: str, call_id: str = "call_1") -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id)


# ---------------------------------------------------------------------------
# _safe_trim helper
# ---------------------------------------------------------------------------


class TestSafeTrim:
    def test_no_limit_returns_all(self):
        raw = [{"type": "human"}, {"type": "ai"}]
        assert _safe_trim(raw, 0) == raw

    def test_limit_trims_oldest(self):
        raw = [{"type": "human", "id": str(i)} for i in range(10)]
        result = _safe_trim(raw, 3)
        assert len(result) == 3
        assert result[-1]["id"] == "9"

    def test_strips_leading_tool_message(self):
        raw = [
            {"type": "tool"},
            {"type": "human", "data": {"content": "hi"}},
            {"type": "ai", "data": {"content": "ok"}},
        ]
        result = _safe_trim(raw, 0)
        assert result[0]["type"] == "human"

    def test_strips_multiple_leading_tool_messages(self):
        raw = [{"type": "tool"}] * 3 + [{"type": "human"}]
        result = _safe_trim(raw, 0)
        assert result[0]["type"] == "human"


# ---------------------------------------------------------------------------
# LocalMemory
# ---------------------------------------------------------------------------


class TestLocalMemory:
    @pytest.mark.asyncio
    async def test_get_or_create_generates_id(self):
        mem = LocalMemory()
        sid = await mem.get_or_create_session()
        assert isinstance(sid, str) and len(sid) > 0

    @pytest.mark.asyncio
    async def test_get_or_create_uses_provided_id(self):
        mem = LocalMemory()
        sid = await mem.get_or_create_session("my-session")
        assert sid == "my-session"

    @pytest.mark.asyncio
    async def test_get_or_create_idempotent(self):
        mem = LocalMemory()
        sid1 = await mem.get_or_create_session("s1")
        sid2 = await mem.get_or_create_session("s1")
        assert sid1 == sid2 == "s1"

    @pytest.mark.asyncio
    async def test_save_and_get_messages(self):
        mem = LocalMemory()
        await mem.get_or_create_session("s1")
        msgs = [_human("Hello"), _ai("Hi there")]
        await mem.save_messages("s1", msgs)
        loaded = await mem.get_messages("s1")
        assert len(loaded) == 2
        assert loaded[0].content == "Hello"
        assert loaded[1].content == "Hi there"

    @pytest.mark.asyncio
    async def test_get_messages_empty_session(self):
        mem = LocalMemory()
        await mem.get_or_create_session("empty")
        result = await mem.get_messages("empty")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_messages_unknown_session(self):
        mem = LocalMemory()
        result = await mem.get_messages("ghost")
        assert result == []

    @pytest.mark.asyncio
    async def test_context_limit_trims_oldest(self):
        mem = LocalMemory()
        await mem.get_or_create_session("s1")
        msgs = [_human(f"msg {i}") for i in range(10)]
        await mem.save_messages("s1", msgs)
        loaded = await mem.get_messages("s1", context_limit=3)
        assert len(loaded) == 3
        assert loaded[0].content == "msg 7"

    @pytest.mark.asyncio
    async def test_context_limit_strips_leading_tool_message(self):
        """After trimming, the first message must not be a ToolMessage."""
        mem = LocalMemory()
        await mem.get_or_create_session("s1")
        # Build a realistic conversation ending in a tool response
        msgs = [
            _human("What is the weather?"),
            _ai(""),   # tool-calling turn (empty content)
            _tool("Sunny 25°C"),
            _ai("It is sunny and 25°C."),
        ]
        await mem.save_messages("s1", msgs)
        # context_limit=3 would cut to [ai(""), tool(...), ai(...)]
        # The ToolMessage at the start should be stripped
        loaded = await mem.get_messages("s1", context_limit=3)
        assert not isinstance(loaded[0], ToolMessage)

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        mem = LocalMemory()
        for sid in ["a", "b", "c"]:
            await mem.get_or_create_session(sid)
        sessions = await mem.list_sessions()
        assert set(sessions) >= {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_delete_session(self):
        mem = LocalMemory()
        await mem.get_or_create_session("to-delete")
        deleted = await mem.delete_session("to-delete")
        assert deleted is True
        assert "to-delete" not in await mem.list_sessions()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        mem = LocalMemory()
        assert await mem.delete_session("ghost") is False

    @pytest.mark.asyncio
    async def test_evicts_oldest_session_at_max(self):
        mem = LocalMemory(max_sessions=3)
        for sid in ["a", "b", "c"]:
            await mem.get_or_create_session(sid)
        # "a" was created first — should be evicted when "d" is added
        await mem.get_or_create_session("d")
        sessions = await mem.list_sessions()
        assert len(sessions) <= 3
        assert "d" in sessions

    @pytest.mark.asyncio
    async def test_max_messages_per_session_trims_on_save(self):
        mem = LocalMemory(max_messages_per_session=5)
        await mem.get_or_create_session("s1")
        msgs = [_human(f"msg {i}") for i in range(10)]
        await mem.save_messages("s1", msgs)
        raw = mem._store["s1"]
        assert len(raw) <= 5

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        mem = LocalMemory()
        await mem.close()  # should not raise


# ---------------------------------------------------------------------------
# RedisMemory  (mocked aioredis)
# ---------------------------------------------------------------------------


def _make_redis_mock() -> MagicMock:
    """Return a mock that behaves like a minimal aioredis client."""
    redis = MagicMock()

    # Internal store
    _store: dict[str, str] = {}
    _zset: dict[str, float] = {}

    async def get(key):
        return _store.get(key)

    async def set(key, value):
        _store[key] = value

    async def delete(*keys):
        deleted = sum(1 for k in keys if k in _store)
        for k in keys:
            _store.pop(k, None)
        return deleted

    async def zadd(key, mapping):
        _zset.update(mapping)

    async def zcard(key):
        return len(_zset)

    async def zrange(key, start, stop):
        sorted_keys = sorted(_zset, key=_zset.get)
        if stop == -1:
            return sorted_keys[start:]
        return sorted_keys[start: stop + 1]

    async def zrem(key, *members):
        for m in members:
            _zset.pop(m, None)

    async def zremrangebyrank(key, start, stop):
        sorted_keys = sorted(_zset, key=_zset.get)
        to_remove = sorted_keys[start: stop + 1]
        for k in to_remove:
            _zset.pop(k, None)

    async def aclose():
        pass

    # Pipeline mock
    pipeline_ops: list = []

    class Pipeline:
        def set(self, key, value):
            pipeline_ops.append(("set", key, value))
            return self

        def zadd(self, key, mapping):
            pipeline_ops.append(("zadd", key, mapping))
            return self

        def delete(self, *keys):
            pipeline_ops.append(("delete", keys))
            return self

        def zrem(self, key, *members):
            pipeline_ops.append(("zrem", key, members))
            return self

        async def execute(self):
            results = []
            for op in pipeline_ops:
                if op[0] == "set":
                    await set(op[1], op[2])
                    results.append(True)
                elif op[0] == "zadd":
                    await zadd(op[1], op[2])
                    results.append(1)
                elif op[0] == "delete":
                    r = await delete(*op[1])
                    results.append(r)
                elif op[0] == "zrem":
                    await zrem(op[1], *op[2])
                    results.append(1)
            pipeline_ops.clear()
            return results

    redis.get = get
    redis.set = set
    redis.delete = delete
    redis.zadd = zadd
    redis.zcard = zcard
    redis.zrange = zrange
    redis.zrem = zrem
    redis.zremrangebyrank = zremrangebyrank
    redis.aclose = aclose
    redis.pipeline = lambda: Pipeline()

    return redis


@pytest.fixture
def redis_memory():
    """RedisMemory with a fully mocked aioredis client."""
    mem = RedisMemory.__new__(RedisMemory)
    mem._redis = _make_redis_mock()
    mem._max_sessions = 100
    mem._max_msgs = 500
    mem._PREFIX = "fls"
    return mem


class TestRedisMemory:
    @pytest.mark.asyncio
    async def test_get_or_create_generates_id(self, redis_memory):
        sid = await redis_memory.get_or_create_session()
        assert isinstance(sid, str) and len(sid) > 0

    @pytest.mark.asyncio
    async def test_get_or_create_uses_provided_id(self, redis_memory):
        sid = await redis_memory.get_or_create_session("my-redis-session")
        assert sid == "my-redis-session"

    @pytest.mark.asyncio
    async def test_save_and_get_messages(self, redis_memory):
        await redis_memory.get_or_create_session("s1")
        msgs = [_human("Hello Redis"), _ai("Hi from Redis")]
        await redis_memory.save_messages("s1", msgs)
        loaded = await redis_memory.get_messages("s1")
        assert len(loaded) == 2
        assert loaded[0].content == "Hello Redis"
        assert loaded[1].content == "Hi from Redis"

    @pytest.mark.asyncio
    async def test_get_messages_empty_returns_empty_list(self, redis_memory):
        result = await redis_memory.get_messages("nonexistent")
        assert result == []

    @pytest.mark.asyncio
    async def test_context_limit_applied_on_get(self, redis_memory):
        await redis_memory.get_or_create_session("s1")
        msgs = [_human(f"msg {i}") for i in range(10)]
        await redis_memory.save_messages("s1", msgs)
        loaded = await redis_memory.get_messages("s1", context_limit=4)
        assert len(loaded) == 4

    @pytest.mark.asyncio
    async def test_list_sessions(self, redis_memory):
        for sid in ["r1", "r2", "r3"]:
            await redis_memory.get_or_create_session(sid)
        sessions = await redis_memory.list_sessions()
        assert set(sessions) >= {"r1", "r2", "r3"}

    @pytest.mark.asyncio
    async def test_delete_session(self, redis_memory):
        await redis_memory.get_or_create_session("to-delete")
        deleted = await redis_memory.delete_session("to-delete")
        assert deleted is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, redis_memory):
        deleted = await redis_memory.delete_session("ghost")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_max_messages_per_session_trims_on_save(self, redis_memory):
        redis_memory._max_msgs = 3
        await redis_memory.get_or_create_session("s1")
        msgs = [_human(f"msg {i}") for i in range(6)]
        await redis_memory.save_messages("s1", msgs)
        loaded = await redis_memory.get_messages("s1")
        assert len(loaded) <= 3

    @pytest.mark.asyncio
    async def test_round_trip_tool_message(self, redis_memory):
        """ToolMessage must serialise/deserialise correctly (has tool_call_id)."""
        await redis_memory.get_or_create_session("s1")
        msgs = [
            _human("search for X"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "search", "args": {"q": "X"}}]),
            _tool("result X", call_id="c1"),
            _ai("The answer is X."),
        ]
        await redis_memory.save_messages("s1", msgs)
        loaded = await redis_memory.get_messages("s1")
        assert len(loaded) == 4
        assert isinstance(loaded[2], ToolMessage)
        assert loaded[2].tool_call_id == "c1"

    @pytest.mark.asyncio
    async def test_close(self, redis_memory):
        await redis_memory.close()  # should not raise


# ---------------------------------------------------------------------------
# NullMemory
# ---------------------------------------------------------------------------


class TestNullMemory:
    @pytest.mark.asyncio
    async def test_get_or_create_returns_provided_id(self):
        mem = NullMemory()
        sid = await mem.get_or_create_session("my-id")
        assert sid == "my-id"

    @pytest.mark.asyncio
    async def test_get_or_create_generates_id_when_none(self):
        mem = NullMemory()
        sid = await mem.get_or_create_session()
        assert isinstance(sid, str) and len(sid) > 0

    @pytest.mark.asyncio
    async def test_get_messages_always_empty(self):
        mem = NullMemory()
        await mem.get_or_create_session("s1")
        await mem.save_messages("s1", [_human("hi"), _ai("ho")])
        # NullMemory discards everything
        result = await mem.get_messages("s1")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_sessions_always_empty(self):
        mem = NullMemory()
        assert await mem.list_sessions() == []

    @pytest.mark.asyncio
    async def test_delete_always_true(self):
        mem = NullMemory()
        assert await mem.delete_session("any") is True


# ---------------------------------------------------------------------------
# create_memory factory
# ---------------------------------------------------------------------------


class TestCreateMemory:
    def test_returns_local_memory_by_default(self):
        mem = create_memory("local")
        assert isinstance(mem, LocalMemory)

    def test_returns_null_memory(self):
        mem = create_memory("null")
        assert isinstance(mem, NullMemory)

    def test_returns_redis_memory(self):
        with patch("fast_langchain_server.memory.RedisMemory.__init__", return_value=None):
            mem = create_memory("redis", redis_url="redis://localhost:6379")
        assert isinstance(mem, RedisMemory)

    def test_raises_when_redis_url_missing(self):
        with pytest.raises(ValueError, match="MEMORY_REDIS_URL"):
            create_memory("redis", redis_url="")
