"""
Pluggable authentication for fast-langchain-server.

Inspired by FastMCP's AuthProvider hierarchy: a simple ABC that every auth
backend must implement, composable via the ``|`` operator (MultiAuth).

Providers
---------
APIKeyProvider      — static dict of {key: owner_name}
EnvAPIKeyProvider   — reads comma-separated keys from an environment variable
JWTProvider         — validates JWT Bearer tokens against a JWKS endpoint
MultiAuth           — tries providers in order; first match wins

Usage
-----
# Single provider
auth = EnvAPIKeyProvider()          # reads AGENT_API_KEYS env var

# Composed providers (JWT first, API key fallback)
auth = JWTProvider(jwks_url="...", audience="my-agent") | EnvAPIKeyProvider()

# Pass to create_agent_server (wired in Fase 3 via AuthMiddleware)
server = create_agent_server(tools=[...])
server.add_middleware(AuthMiddleware(provider=auth))
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token model
# ---------------------------------------------------------------------------


@dataclass
class AuthToken:
    """Represents a verified identity after successful authentication.

    Parameters
    ----------
    subject:
        Identifies the caller — user ID, service name, or API key owner.
    scopes:
        List of permission scopes granted to this token.  Use ``["*"]`` to
        indicate unrestricted access.
    claims:
        Raw token payload (JWT claims dict, or arbitrary metadata for non-JWT
        providers).
    raw:
        The original token string (useful for audit logging).
    """

    subject: str
    scopes: list[str]
    claims: dict = field(default_factory=dict)
    raw: str = field(default="", repr=False)

    def has_scope(self, scope: str) -> bool:
        """Return True if this token grants ``scope`` or wildcard ``*``."""
        return "*" in self.scopes or scope in self.scopes

    def has_all_scopes(self, *scopes: str) -> bool:
        """Return True if this token grants ALL of the given scopes."""
        return all(self.has_scope(s) for s in scopes)


# ---------------------------------------------------------------------------
# AuthProvider ABC
# ---------------------------------------------------------------------------


class AuthProvider(ABC):
    """Base class for all authentication backends.

    Every concrete provider must implement ``verify_token``.  If the token is
    invalid or unrecognised the method must return ``None``; it must NOT raise
    an exception (except for unexpected infrastructure errors).

    Composition
    -----------
    Two providers can be chained with the ``|`` operator::

        auth = JWTProvider(...) | EnvAPIKeyProvider()

    The resulting ``MultiAuth`` will try each provider in order and return the
    first successful result.
    """

    @abstractmethod
    async def verify_token(self, token: str) -> Optional[AuthToken]:
        """Validate *token* and return an ``AuthToken`` on success, else None."""

    def __or__(self, other: "AuthProvider") -> "MultiAuth":
        return MultiAuth(self, other)


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


class APIKeyProvider(AuthProvider):
    """Validates against a static mapping of ``{api_key: owner_name}``.

    Example
    -------
    auth = APIKeyProvider({
        "sk-prod-abc123": "prod-service",
        "sk-dev-xyz789": "dev-local",
    })
    """

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        if owner := self._keys.get(token):
            return AuthToken(subject=owner, scopes=["*"], raw=token)
        return None


class EnvAPIKeyProvider(AuthProvider):
    """Reads valid API keys from an environment variable at startup.

    The variable must contain a comma-separated list of raw key strings.
    All keys share the same subject name (``env-key``) and full scope (``*``).

    Parameters
    ----------
    env_var:
        Name of the environment variable to read.  Default: ``AGENT_API_KEYS``.
    subject:
        Subject name assigned to tokens verified by this provider.

    Example
    -------
    # .env
    AGENT_API_KEYS=sk-abc,sk-xyz,sk-other

    auth = EnvAPIKeyProvider()
    """

    def __init__(
        self,
        env_var: str = "AGENT_API_KEYS",
        subject: str = "env-key",
    ) -> None:
        raw = os.getenv(env_var, "")
        self._keys: set[str] = {k.strip() for k in raw.split(",") if k.strip()}
        self._subject = subject
        if self._keys:
            logger.debug(
                "EnvAPIKeyProvider: loaded %d key(s) from %s", len(self._keys), env_var
            )
        else:
            logger.warning(
                "EnvAPIKeyProvider: no keys found in %s — all requests will be rejected",
                env_var,
            )

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        if token in self._keys:
            return AuthToken(subject=self._subject, scopes=["*"], raw=token)
        return None


class JWTProvider(AuthProvider):
    """Validates JWT Bearer tokens against a JWKS endpoint.

    Requires ``PyJWT>=2.0`` and ``httpx`` (both already in the dependency tree
    via LangChain).  The JWKS is fetched lazily on the first request and cached
    in memory.

    Parameters
    ----------
    jwks_url:
        URL of the JSON Web Key Set endpoint (e.g.
        ``https://auth.example.com/.well-known/jwks.json``).
    audience:
        Expected ``aud`` claim.  Tokens with a different audience are rejected.
    issuer:
        Optional expected ``iss`` claim.
    algorithms:
        List of accepted signing algorithms.  Default: ``["RS256"]``.
    scopes_claim:
        Name of the JWT claim that carries the list of scopes.
        Default: ``"scope"`` (space-separated string, as per RFC 8693) or
        ``"scp"`` (list, common in Okta/Auth0).

    Example
    -------
    auth = JWTProvider(
        jwks_url="https://accounts.google.com/.well-known/openid-configuration",
        audience="my-agent-service",
        issuer="https://accounts.google.com",
    )
    """

    def __init__(
        self,
        jwks_url: str,
        audience: str,
        issuer: Optional[str] = None,
        algorithms: Optional[list[str]] = None,
        scopes_claim: str = "scope",
    ) -> None:
        self._jwks_url = jwks_url
        self._audience = audience
        self._issuer = issuer
        self._algorithms = algorithms or ["RS256"]
        self._scopes_claim = scopes_claim
        self._jwks_client: Optional[object] = None  # jwt.PyJWKClient, lazy

    def _get_jwks_client(self):
        if self._jwks_client is None:
            try:
                import jwt  # PyJWT

                self._jwks_client = jwt.PyJWKClient(self._jwks_url, cache_keys=True)
            except ImportError as exc:
                raise ImportError(
                    "JWTProvider requires PyJWT>=2.0. "
                    "Install it with: pip install 'PyJWT[crypto]'"
                ) from exc
        return self._jwks_client

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        if not token:
            return None
        try:
            import jwt  # PyJWT

            client = self._get_jwks_client()
            signing_key = client.get_signing_key_from_jwt(token)

            options: dict = {}
            if self._issuer is None:
                options["verify_iss"] = False

            payload: dict = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options=options,
            )

            # Extract scopes — handle both space-separated string and list
            raw_scopes = payload.get(self._scopes_claim, "")
            if isinstance(raw_scopes, str):
                scopes = raw_scopes.split() if raw_scopes else []
            elif isinstance(raw_scopes, list):
                scopes = raw_scopes
            else:
                scopes = []

            # Fallback: also check "scp" list (Okta / Auth0 style)
            if not scopes and self._scopes_claim == "scope":
                scp = payload.get("scp", [])
                scopes = scp if isinstance(scp, list) else []

            subject = payload.get("sub", "")
            return AuthToken(
                subject=subject,
                scopes=scopes,
                claims=payload,
                raw=token,
            )

        except Exception as exc:
            logger.debug("JWTProvider: token rejected — %s", exc)
            return None


# ---------------------------------------------------------------------------
# MultiAuth — composition
# ---------------------------------------------------------------------------


class MultiAuth(AuthProvider):
    """Chains multiple providers; the first successful verification wins.

    Created automatically via ``provider_a | provider_b``.

    Example
    -------
    auth = JWTProvider(...) | APIKeyProvider({"sk-local": "dev"})
    """

    def __init__(self, *providers: AuthProvider) -> None:
        # Flatten nested MultiAuth instances to keep a flat chain
        flat: list[AuthProvider] = []
        for p in providers:
            if isinstance(p, MultiAuth):
                flat.extend(p._providers)
            else:
                flat.append(p)
        self._providers = flat

    async def verify_token(self, token: str) -> Optional[AuthToken]:
        for provider in self._providers:
            result = await provider.verify_token(token)
            if result is not None:
                return result
        return None
