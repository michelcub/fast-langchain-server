"""Tests for fast_langchain_server.authorization."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from fast_langchain_server.auth import AuthToken
from fast_langchain_server.authorization import (
    AuthContext,
    AuthorizationMiddleware,
    all_of,
    allow_any_authenticated,
    allow_own_session,
    any_of,
    deny_all,
    require_scopes,
    run_auth_check,
)
from fast_langchain_server.context import AgentContext
from fast_langchain_server.middleware import build_middleware_chain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token(subject: str = "user-1", scopes: list[str] | None = None) -> AuthToken:
    return AuthToken(subject=subject, scopes=scopes or ["*"], raw="tok")


def _auth_ctx(
    token: AuthToken | None = None,
    endpoint: str = "/v1/chat/completions",
    method: str = "POST",
    session_id: str | None = None,
) -> AuthContext:
    return AuthContext(
        token=token,
        endpoint=endpoint,
        method=method,
        session_id=session_id,
    )


def _agent_ctx(
    session_id: str = "sess-1",
    endpoint: str = "/v1/chat/completions",
    token: AuthToken | None = None,
) -> AgentContext:
    ctx = AgentContext.from_request(
        session_id=session_id,
        user_input="hello",
        headers={},
    )
    ctx.set_meta("endpoint", endpoint)
    ctx.set_meta("method", "POST")
    if token is not None:
        ctx.set_meta("auth_token", token)
    return ctx


async def _noop_handler(ctx):
    return "ok"


# ---------------------------------------------------------------------------
# run_auth_check
# ---------------------------------------------------------------------------


class TestRunAuthCheck:
    async def test_sync_check_returning_true(self):
        assert await run_auth_check(lambda _: True, _auth_ctx()) is True

    async def test_sync_check_returning_false(self):
        assert await run_auth_check(lambda _: False, _auth_ctx()) is False

    async def test_async_check_returning_true(self):
        async def check(ctx):
            return True

        assert await run_auth_check(check, _auth_ctx()) is True

    async def test_async_check_returning_false(self):
        async def check(ctx):
            return False

        assert await run_auth_check(check, _auth_ctx()) is False

    async def test_unexpected_exception_treated_as_denial(self):
        def bad_check(ctx):
            raise ValueError("internal error")

        result = await run_auth_check(bad_check, _auth_ctx())
        assert result is False

    async def test_http_exception_propagates(self):
        def check(ctx):
            raise HTTPException(403, "forbidden")

        with pytest.raises(HTTPException):
            await run_auth_check(check, _auth_ctx())


# ---------------------------------------------------------------------------
# require_scopes
# ---------------------------------------------------------------------------


class TestRequireScopes:
    async def test_token_has_required_scope(self):
        check = require_scopes("chat")
        ctx = _auth_ctx(token=_token(scopes=["chat", "memory"]))
        assert await run_auth_check(check, ctx) is True

    async def test_token_missing_scope(self):
        check = require_scopes("admin")
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is False

    async def test_wildcard_scope_grants_all(self):
        check = require_scopes("admin", "chat", "memory")
        ctx = _auth_ctx(token=_token(scopes=["*"]))
        assert await run_auth_check(check, ctx) is True

    async def test_no_token_denied(self):
        check = require_scopes("chat")
        ctx = _auth_ctx(token=None)
        assert await run_auth_check(check, ctx) is False

    async def test_multiple_scopes_all_required(self):
        check = require_scopes("read", "write")
        ctx_both = _auth_ctx(token=_token(scopes=["read", "write"]))
        ctx_one = _auth_ctx(token=_token(scopes=["read"]))
        assert await run_auth_check(check, ctx_both) is True
        assert await run_auth_check(check, ctx_one) is False


# ---------------------------------------------------------------------------
# allow_any_authenticated
# ---------------------------------------------------------------------------


class TestAllowAnyAuthenticated:
    async def test_with_token(self):
        check = allow_any_authenticated()
        assert await run_auth_check(check, _auth_ctx(token=_token())) is True

    async def test_without_token(self):
        check = allow_any_authenticated()
        assert await run_auth_check(check, _auth_ctx(token=None)) is False


# ---------------------------------------------------------------------------
# allow_own_session
# ---------------------------------------------------------------------------


class TestAllowOwnSession:
    async def test_session_owned_by_subject(self):
        check = allow_own_session()
        ctx = _auth_ctx(token=_token(subject="user-42"), session_id="user-42-abc")
        assert await run_auth_check(check, ctx) is True

    async def test_session_not_owned(self):
        check = allow_own_session()
        ctx = _auth_ctx(token=_token(subject="user-42"), session_id="user-99-xyz")
        assert await run_auth_check(check, ctx) is False

    async def test_no_session_passes(self):
        check = allow_own_session()
        ctx = _auth_ctx(token=_token(subject="user-42"), session_id=None)
        assert await run_auth_check(check, ctx) is True

    async def test_no_token_denied(self):
        check = allow_own_session()
        ctx = _auth_ctx(token=None, session_id="user-42-abc")
        assert await run_auth_check(check, ctx) is False


# ---------------------------------------------------------------------------
# deny_all
# ---------------------------------------------------------------------------


class TestDenyAll:
    async def test_always_denies_with_token(self):
        check = deny_all()
        assert await run_auth_check(check, _auth_ctx(token=_token())) is False

    async def test_always_denies_without_token(self):
        check = deny_all()
        assert await run_auth_check(check, _auth_ctx(token=None)) is False


# ---------------------------------------------------------------------------
# all_of / any_of
# ---------------------------------------------------------------------------


class TestAllOf:
    async def test_all_pass(self):
        check = all_of(allow_any_authenticated(), require_scopes("chat"))
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is True

    async def test_one_fails(self):
        check = all_of(allow_any_authenticated(), require_scopes("admin"))
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is False

    async def test_short_circuits_on_first_failure(self):
        called = []

        def track_check(ctx):
            called.append(True)
            return True

        check = all_of(lambda _: False, track_check)
        ctx = _auth_ctx(token=_token())
        await run_auth_check(check, ctx)
        assert not called  # second check never ran


class TestAnyOf:
    async def test_first_passes(self):
        check = any_of(require_scopes("chat"), require_scopes("admin"))
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is True

    async def test_second_passes(self):
        check = any_of(require_scopes("admin"), require_scopes("chat"))
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is True

    async def test_none_pass(self):
        check = any_of(require_scopes("admin"), require_scopes("superuser"))
        ctx = _auth_ctx(token=_token(scopes=["chat"]))
        assert await run_auth_check(check, ctx) is False


# ---------------------------------------------------------------------------
# AuthorizationMiddleware
# ---------------------------------------------------------------------------


class TestAuthorizationMiddleware:
    async def test_allows_when_check_passes(self):
        mw = AuthorizationMiddleware({
            "/v1/chat/completions": require_scopes("chat"),
        })
        ctx = _agent_ctx(token=_token(scopes=["chat"]))
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(ctx) == "ok"

    async def test_raises_403_when_check_fails(self):
        mw = AuthorizationMiddleware({
            "/v1/chat/completions": require_scopes("admin"),
        })
        ctx = _agent_ctx(token=_token(scopes=["chat"]))
        chain = build_middleware_chain([mw], _noop_handler)
        with pytest.raises(HTTPException) as exc_info:
            await chain(ctx)
        assert exc_info.value.status_code == 403

    async def test_no_rule_for_endpoint_passes(self):
        mw = AuthorizationMiddleware({
            "/memory/sessions": require_scopes("admin"),
        })
        # Different endpoint — no rule → passes
        ctx = _agent_ctx(endpoint="/v1/chat/completions", token=_token(scopes=["chat"]))
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(ctx) == "ok"

    async def test_multiple_rules(self):
        mw = AuthorizationMiddleware({
            "/v1/chat/completions": require_scopes("chat"),
            "/memory/sessions":     require_scopes("admin"),
        })
        chat_ctx = _agent_ctx(endpoint="/v1/chat/completions", token=_token(scopes=["chat"]))
        admin_ctx = _agent_ctx(endpoint="/memory/sessions", token=_token(scopes=["admin"]))
        insufficient_ctx = _agent_ctx(endpoint="/memory/sessions", token=_token(scopes=["chat"]))

        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(chat_ctx) == "ok"
        assert await chain(admin_ctx) == "ok"
        with pytest.raises(HTTPException) as exc_info:
            await chain(insufficient_ctx)
        assert exc_info.value.status_code == 403

    async def test_works_without_auth_token_when_no_rule(self):
        mw = AuthorizationMiddleware({})
        ctx = _agent_ctx(token=None)
        chain = build_middleware_chain([mw], _noop_handler)
        assert await chain(ctx) == "ok"
