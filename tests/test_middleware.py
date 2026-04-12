"""Tests for fast_langchain_server.middleware."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from fast_langchain_server.auth import APIKeyProvider, AuthToken
from fast_langchain_server.context import AgentContext
from fast_langchain_server.middleware import (
    AgentMiddleware,
    AuthMiddleware,
    RateLimitMiddleware,
    TimingMiddleware,
    build_middleware_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(headers: dict | None = None) -> Request:
    """Build a minimal Starlette Request with the given headers."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "POST", "headers": raw}
    return Request(scope)


def _ctx(
    session_id: str = "sess-1",
    endpoint: str = "/v1/chat/completions",
    headers: dict | None = None,
) -> AgentContext:
    ctx = AgentContext.from_request(
        session_id=session_id,
        user_input="hello",
        request=_make_request(headers),
    )
    ctx.set_meta("endpoint", endpoint)
    ctx.set_meta("method", "POST")
    return ctx


async def _noop_handler(ctx: AgentContext) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# build_middleware_chain
# ---------------------------------------------------------------------------


class TestBuildMiddlewareChain:
    async def test_empty_chain_calls_handler(self):
        chain = build_middleware_chain([], _noop_handler)
        result = await chain(_ctx())
        assert result == "ok"

    async def test_single_middleware_wraps_handler(self):
        order = []

        class TrackMiddleware(AgentMiddleware):
            async def on_request(self, ctx, call_next):
                order.append("before")
                result = await call_next(ctx)
                order.append("after")
                return result

        chain = build_middleware_chain([TrackMiddleware()], _noop_handler)
        await chain(_ctx())
        assert order == ["before", "after"]

    async def test_multiple_middlewares_execute_in_order(self):
        order = []

        class M(AgentMiddleware):
            def __init__(self, name):
                self.name = name

            async def on_request(self, ctx, call_next):
                order.append(f"{self.name}:before")
                result = await call_next(ctx)
                order.append(f"{self.name}:after")
                return result

        chain = build_middleware_chain([M("A"), M("B"), M("C")], _noop_handler)
        await chain(_ctx())
        assert order == [
            "A:before", "B:before", "C:before",
            "C:after", "B:after", "A:after",
        ]

    async def test_middleware_can_short_circuit(self):
        reached_handler = []

        class BlockMiddleware(AgentMiddleware):
            async def on_request(self, ctx, call_next):
                raise HTTPException(status_code=403, detail="blocked")

        async def handler(ctx):
            reached_handler.append(True)
            return "ok"

        chain = build_middleware_chain([BlockMiddleware()], handler)
        with pytest.raises(HTTPException) as exc_info:
            await chain(_ctx())
        assert exc_info.value.status_code == 403
        assert not reached_handler

    async def test_middleware_can_modify_context(self):
        class EnrichMiddleware(AgentMiddleware):
            async def on_request(self, ctx, call_next):
                ctx.set_meta("enriched", True)
                return await call_next(ctx)

        captured = {}

        async def handler(ctx):
            captured["enriched"] = ctx.get_meta("enriched")
            return "ok"

        chain = build_middleware_chain([EnrichMiddleware()], handler)
        await chain(_ctx())
        assert captured["enriched"] is True

    async def test_on_agent_run_hook(self):
        order = []

        class M(AgentMiddleware):
            async def on_agent_run(self, ctx, call_next):
                order.append("agent:before")
                result = await call_next(ctx)
                order.append("agent:after")
                return result

        chain = build_middleware_chain([M()], _noop_handler, hook="on_agent_run")
        await chain(_ctx())
        assert order == ["agent:before", "agent:after"]


# ---------------------------------------------------------------------------
# AuthMiddleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    async def test_valid_bearer_token_passes(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={"authorization": "Bearer sk-valid"})
        chain = build_middleware_chain([mw], _noop_handler)
        result = await chain(ctx)
        assert result == "ok"

    async def test_valid_token_sets_auth_token_in_context(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={"authorization": "Bearer sk-valid"})
        chain = build_middleware_chain([mw], _noop_handler)
        await chain(ctx)
        assert ctx.auth_token is not None
        assert ctx.auth_token.subject == "svc"

    async def test_missing_token_raises_401(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={})
        chain = build_middleware_chain([mw], _noop_handler)
        with pytest.raises(HTTPException) as exc_info:
            await chain(ctx)
        assert exc_info.value.status_code == 401

    async def test_invalid_token_raises_401(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={"authorization": "Bearer sk-wrong"})
        chain = build_middleware_chain([mw], _noop_handler)
        with pytest.raises(HTTPException) as exc_info:
            await chain(ctx)
        assert exc_info.value.status_code == 401

    async def test_x_api_key_fallback_header(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={"x-api-key": "sk-valid"})
        chain = build_middleware_chain([mw], _noop_handler)
        result = await chain(ctx)
        assert result == "ok"

    async def test_excluded_endpoints_bypass_auth(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        for endpoint in ["/health", "/ready", "/.well-known/agent.json"]:
            ctx = _ctx(endpoint=endpoint, headers={})
            chain = build_middleware_chain([mw], _noop_handler)
            result = await chain(ctx)
            assert result == "ok"

    async def test_custom_excluded_endpoint(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider, exclude={"/public"})
        ctx = _ctx(endpoint="/public", headers={})
        chain = build_middleware_chain([mw], _noop_handler)
        result = await chain(ctx)
        assert result == "ok"

    async def test_bearer_prefix_stripped(self):
        provider = APIKeyProvider({"sk-valid": "svc"})
        mw = AuthMiddleware(provider=provider)
        # With "Bearer " prefix
        ctx = _ctx(headers={"authorization": "Bearer sk-valid"})
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(ctx) == "ok"

    async def test_token_without_bearer_prefix_via_api_key_header(self):
        provider = APIKeyProvider({"sk-raw": "svc"})
        mw = AuthMiddleware(provider=provider)
        ctx = _ctx(headers={"x-api-key": "sk-raw"})
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(ctx) == "ok"


# ---------------------------------------------------------------------------
# TimingMiddleware
# ---------------------------------------------------------------------------


class TestTimingMiddleware:
    async def test_passes_through(self):
        mw = TimingMiddleware()
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(_ctx()) == "ok"

    async def test_reraises_exceptions(self):
        mw = TimingMiddleware()

        async def failing_handler(ctx):
            raise HTTPException(500, "boom")

        chain = build_middleware_chain([mw], failing_handler)
        with pytest.raises(HTTPException):
            await chain(_ctx())


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


class TestRateLimitMiddleware:
    async def test_allows_requests_under_limit(self):
        mw = RateLimitMiddleware(max_rpm=60)
        chain = build_middleware_chain([mw], _noop_handler)
        # First request should always pass
        assert await chain(_ctx()) == "ok"

    async def test_blocks_when_bucket_empty(self):
        mw = RateLimitMiddleware(max_rpm=1)
        chain = build_middleware_chain([mw], _noop_handler)
        # Drain the bucket
        await chain(_ctx(session_id="sess-rl"))
        # Second request should be blocked
        with pytest.raises(HTTPException) as exc_info:
            await chain(_ctx(session_id="sess-rl"))
        assert exc_info.value.status_code == 429

    async def test_different_sessions_have_separate_buckets(self):
        mw = RateLimitMiddleware(max_rpm=1)
        chain = build_middleware_chain([mw], _noop_handler)
        await chain(_ctx(session_id="sess-a"))
        # Different session should still pass
        assert await chain(_ctx(session_id="sess-b")) == "ok"
