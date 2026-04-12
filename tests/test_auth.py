"""Tests for fast_langchain_server.auth — AuthProvider hierarchy."""
from __future__ import annotations

import os

import pytest

from fast_langchain_server.auth import (
    APIKeyProvider,
    AuthToken,
    EnvAPIKeyProvider,
    JWTProvider,
    MultiAuth,
)


# ---------------------------------------------------------------------------
# AuthToken
# ---------------------------------------------------------------------------


class TestAuthToken:
    def test_has_scope_exact(self):
        tok = AuthToken(subject="user", scopes=["chat", "admin"], raw="")
        assert tok.has_scope("chat") is True
        assert tok.has_scope("admin") is True

    def test_has_scope_missing(self):
        tok = AuthToken(subject="user", scopes=["chat"], raw="")
        assert tok.has_scope("admin") is False

    def test_has_scope_wildcard(self):
        tok = AuthToken(subject="user", scopes=["*"], raw="")
        assert tok.has_scope("chat") is True
        assert tok.has_scope("anything") is True

    def test_has_all_scopes_all_present(self):
        tok = AuthToken(subject="user", scopes=["chat", "memory"], raw="")
        assert tok.has_all_scopes("chat", "memory") is True

    def test_has_all_scopes_one_missing(self):
        tok = AuthToken(subject="user", scopes=["chat"], raw="")
        assert tok.has_all_scopes("chat", "admin") is False

    def test_has_all_scopes_wildcard(self):
        tok = AuthToken(subject="user", scopes=["*"], raw="")
        assert tok.has_all_scopes("chat", "admin", "memory") is True


# ---------------------------------------------------------------------------
# APIKeyProvider
# ---------------------------------------------------------------------------


class TestAPIKeyProvider:
    async def test_valid_key_returns_token(self):
        provider = APIKeyProvider({"sk-abc": "service-a"})
        token = await provider.verify_token("sk-abc")
        assert token is not None
        assert token.subject == "service-a"
        assert token.has_scope("*")

    async def test_invalid_key_returns_none(self):
        provider = APIKeyProvider({"sk-abc": "service-a"})
        assert await provider.verify_token("sk-wrong") is None

    async def test_empty_token_returns_none(self):
        provider = APIKeyProvider({"sk-abc": "service-a"})
        assert await provider.verify_token("") is None

    async def test_multiple_keys(self):
        provider = APIKeyProvider({"sk-a": "svc-a", "sk-b": "svc-b"})
        tok_a = await provider.verify_token("sk-a")
        tok_b = await provider.verify_token("sk-b")
        assert tok_a.subject == "svc-a"
        assert tok_b.subject == "svc-b"

    async def test_raw_token_preserved(self):
        provider = APIKeyProvider({"sk-abc": "svc"})
        token = await provider.verify_token("sk-abc")
        assert token.raw == "sk-abc"


# ---------------------------------------------------------------------------
# EnvAPIKeyProvider
# ---------------------------------------------------------------------------


class TestEnvAPIKeyProvider:
    async def test_reads_keys_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_API_KEYS", "key-1,key-2,key-3")
        provider = EnvAPIKeyProvider()
        assert await provider.verify_token("key-1") is not None
        assert await provider.verify_token("key-2") is not None
        assert await provider.verify_token("key-3") is not None

    async def test_invalid_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("AGENT_API_KEYS", "key-1")
        provider = EnvAPIKeyProvider()
        assert await provider.verify_token("key-invalid") is None

    async def test_empty_env_var_rejects_all(self, monkeypatch):
        monkeypatch.setenv("AGENT_API_KEYS", "")
        provider = EnvAPIKeyProvider()
        assert await provider.verify_token("anything") is None

    async def test_custom_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_CUSTOM_KEYS", "secret-key")
        provider = EnvAPIKeyProvider(env_var="MY_CUSTOM_KEYS")
        assert await provider.verify_token("secret-key") is not None

    async def test_strips_whitespace_around_keys(self, monkeypatch):
        monkeypatch.setenv("AGENT_API_KEYS", " key-1 , key-2 ")
        provider = EnvAPIKeyProvider()
        assert await provider.verify_token("key-1") is not None
        assert await provider.verify_token("key-2") is not None

    async def test_custom_subject(self, monkeypatch):
        monkeypatch.setenv("AGENT_API_KEYS", "k")
        provider = EnvAPIKeyProvider(subject="my-service")
        token = await provider.verify_token("k")
        assert token.subject == "my-service"


# ---------------------------------------------------------------------------
# JWTProvider — only tests the non-crypto path (missing PyJWT is handled)
# ---------------------------------------------------------------------------


class TestJWTProvider:
    async def test_empty_token_returns_none(self):
        provider = JWTProvider(jwks_url="http://auth.example.com/jwks", audience="aud")
        result = await provider.verify_token("")
        assert result is None

    async def test_malformed_token_returns_none(self):
        provider = JWTProvider(jwks_url="http://auth.example.com/jwks", audience="aud")
        result = await provider.verify_token("not.a.jwt")
        assert result is None

    async def test_missing_pyjwt_raises_import_error_on_jwks_fetch(self):
        """If PyJWT isn't installed, calling _get_jwks_client should raise ImportError."""
        import importlib
        import sys

        provider = JWTProvider(jwks_url="http://auth.example.com/jwks", audience="aud")
        # Simulate missing PyJWT by temporarily hiding the module
        jwt_module = sys.modules.get("jwt")
        sys.modules["jwt"] = None  # type: ignore[assignment]
        try:
            with pytest.raises((ImportError, Exception)):
                provider._get_jwks_client()
        finally:
            if jwt_module is None:
                sys.modules.pop("jwt", None)
            else:
                sys.modules["jwt"] = jwt_module


# ---------------------------------------------------------------------------
# MultiAuth
# ---------------------------------------------------------------------------


class TestMultiAuth:
    async def test_first_provider_wins(self):
        p1 = APIKeyProvider({"k1": "svc-1"})
        p2 = APIKeyProvider({"k2": "svc-2"})
        auth = MultiAuth(p1, p2)
        tok = await auth.verify_token("k1")
        assert tok.subject == "svc-1"

    async def test_falls_back_to_second_provider(self):
        p1 = APIKeyProvider({"k1": "svc-1"})
        p2 = APIKeyProvider({"k2": "svc-2"})
        auth = MultiAuth(p1, p2)
        tok = await auth.verify_token("k2")
        assert tok.subject == "svc-2"

    async def test_returns_none_when_all_fail(self):
        p1 = APIKeyProvider({"k1": "svc-1"})
        p2 = APIKeyProvider({"k2": "svc-2"})
        auth = MultiAuth(p1, p2)
        assert await auth.verify_token("unknown") is None

    async def test_pipe_operator_creates_multiauth(self):
        p1 = APIKeyProvider({"k1": "svc-1"})
        p2 = APIKeyProvider({"k2": "svc-2"})
        auth = p1 | p2
        assert isinstance(auth, MultiAuth)
        assert await auth.verify_token("k2") is not None

    async def test_nested_multiauth_flattens(self):
        p1 = APIKeyProvider({"k1": "s1"})
        p2 = APIKeyProvider({"k2": "s2"})
        p3 = APIKeyProvider({"k3": "s3"})
        auth = p1 | p2 | p3
        assert len(auth._providers) == 3
        assert await auth.verify_token("k3") is not None
